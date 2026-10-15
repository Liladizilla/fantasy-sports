"""``add``, ``drop``, and ``claim``, offline (jwulff/fantasy-sports#18).

Every test runs the real command against the real ESPN adapter, exactly as
``test_lineup_set.py`` does. Reads replay the synthetic league ``99``; the
write host is :class:`_harness.WritableEspn`, which answers a ``FREEAGENT``
body by moving players between the free-agent pool and the roster, and a
``WAIVER`` body by queueing a pending claim that ``mTransactions2`` lists. The
``FREEAGENT``/``WAIVER`` answers follow the community-attested shapes (brief
§10); #108 captures the real ones. No test sends a live write.

Team 1, owned by the session:

====== ================== ====== ===================================
id     name               slot   eligible
====== ================== ====== ===================================
2001   Ada Lovelace       QB     QB, BE, IR
2002   Grace Hopper       RB     RB, RB/WR, RB/WR/TE, BE, IR
2003   Katherine Johnson  BE     RB/WR, WR, WR/TE, RB/WR/TE, BE, IR
====== ================== ====== ===================================

The pool (``WritableEspn.POOL``): 3001 Barbara Liskov (WR, free agent), 3002
Radia Perlman (TE, on waivers), 3003 Frances Allen (RB, free agent), 3004
Frances Spence (WR, free agent).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _harness import (
    PROPOSED_DATE_MS,
    TRANSACTION_ID,
    WritableEspn,
    install_writable_espn,
    isolate_home,
)

from fantasy_sports.commands import REGISTRY, DataShape
from fantasy_sports.commands.moves import NOT_IN_QUEUE_NOTE
from fantasy_sports.core.errors import (
    ConfigInvalidError,
    NotAvailableError,
    ProviderUnavailableError,
    WriteDivergedError,
    WriteRejectedError,
)
from fantasy_sports.core.journal import Journal, JournalEntry
from fantasy_sports.core.redaction import forget_secrets
from fantasy_sports.output.errors import exit_code_for

BENCH, WR = 20, 4
OTHER_MEMBER = "{9F8E7D6C-5B4A-3928-1706-F5E4D3C2B1A0}"
WHY = "Johnson is out for the season; Liskov starts this week (sanity-gate: PROCEED)"

SUCCESS_KEYS = {
    "team_id",
    "kind",
    "provider_transaction_id",
    "status",
    "is_pending",
    "proposed_at",
    "items",
    "journal_entry_id",
    "reversible",
    "acknowledged_irreversible",
    "cost",
    "credentials",
    "read_back",
    "notified",
    "dry_run",
}

NOTIFY_KEYS = {"enabled", "attempted", "ok", "exit_status", "error"}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield isolate_home(tmp_path, monkeypatch)
    forget_secrets()


@pytest.fixture
def espn(monkeypatch: pytest.MonkeyPatch) -> WritableEspn:
    return install_writable_espn(monkeypatch)


def _run(command: str, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("team", "1")
    return REGISTRY[command].resolve()(**kwargs).to_dict()


def _add(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("player", "Barbara Liskov")
    return _run("add", **kwargs)


def _drop(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("player", "Katherine Johnson")
    return _run("drop", **kwargs)


def _claim(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("player", "Radia Perlman")
    kwargs.setdefault("bid", 7)
    return _run("claim", **kwargs)


def _entries() -> list[JournalEntry]:
    with Journal.open() as journal:
        return journal.list(limit=50)


def _journal_file(home: Path) -> Path:
    return home / "state" / "fantasy-sports" / "journal.sqlite"


def _notify_to(home: Path, command: str) -> None:
    config = home / "config" / "fantasy-sports" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + f"\n[notify]\nirreversible_command = {json.dumps(command)}\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "params"),
    [
        ("add", ["team", "player", "drop", "slot", "acknowledge_irreversible", "dry_run"]),
        ("drop", ["team", "player", "acknowledge_irreversible", "dry_run"]),
        ("claim", ["team", "player", "bid", "drop", "acknowledge_irreversible", "dry_run"]),
    ],
)
def test_each_move_is_a_top_level_object_command_that_takes_a_league(command, params):
    spec = REGISTRY[command]
    assert spec.group is None
    assert spec.shape is DataShape.OBJECT
    assert spec.takes_league
    assert [param.name for param in spec.params] == params
    assert "Irreversible" in spec.summary
    flags = {param.name: param.cli_flags for param in spec.params}
    assert flags["acknowledge_irreversible"] == ("--acknowledge-irreversible",)
    required = {param.name for param in spec.params if param.required}
    assert "acknowledge_irreversible" not in required, "--dry-run must work without it"


# --------------------------------------------------------------------------- #
# The acknowledgement gate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("flag", [None, "", "   \t"])
@pytest.mark.parametrize(
    ("call", "kind", "statements"),
    [
        (
            lambda **kw: _drop(**kw),
            "drop",
            [
                "Katherine Johnson (2003) goes to waivers and can be claimed by any team; "
                "this cannot be reversed"
            ],
        ),
        (
            lambda **kw: _claim(**kw),
            "claim",
            ["consumes waiver priority (and FAAB where enabled)"],
        ),
        (
            lambda **kw: _add(drop="2003", **kw),
            "add",
            [
                "Barbara Liskov (3001) joins the roster, and undoing that later is a drop",
                "Katherine Johnson (2003) goes to waivers and can be claimed by any team; "
                "this cannot be reversed",
            ],
        ),
        (
            lambda **kw: _claim(drop="Grace Hopper", **kw),
            "claim",
            [
                "consumes waiver priority (and FAAB where enabled)",
                "Grace Hopper (2002) goes to waivers",
            ],
        ),
    ],
)
def test_without_a_justification_nothing_is_sent_and_the_refusal_says_what_is_lost(
    espn: WritableEspn, isolated_home: Path, call, kind, statements, flag
):
    with pytest.raises(ConfigInvalidError) as caught:
        call(acknowledge_irreversible=flag)
    error = caught.value
    assert exit_code_for(error) == 6
    assert error.details["kind"] == "argument"
    assert error.details["move"] == kind
    assert error.details["flag"] == "--acknowledge-irreversible"
    assert f"This {kind} cannot be undone" in error.message
    for statement in statements:
        assert statement in error.message
        assert statement in error.details["irreversible_because"]
    assert "--acknowledge-irreversible" in error.remediation
    assert "/sanity-gate" in error.remediation
    assert espn.posts == []
    assert not _journal_file(isolated_home).exists()


def test_the_justification_is_journaled_and_echoed_verbatim(espn: WritableEspn):
    why = "  bye-week cover; gate PROCEED 2026-10-14  "
    data = _add(acknowledge_irreversible=why)["data"]
    assert data["acknowledged_irreversible"] == why
    (entry,) = _entries()
    assert entry.request["acknowledged_irreversible"] == why
    assert entry.command[-2:] == ("--acknowledge-irreversible", why)


# --------------------------------------------------------------------------- #
# --dry-run
# --------------------------------------------------------------------------- #


def test_dry_run_needs_no_flag_and_reports_items_cost_and_what_is_lost(
    espn: WritableEspn, isolated_home: Path
):
    data = _claim(drop="2003", bid=12, dry_run=True)["data"]
    assert set(data) == SUCCESS_KEYS | {
        "note",
        "irreversible_because",
        "acquisition_type",
        "uses_budget",
    }
    assert data["uses_budget"] is True
    assert data["acquisition_type"] is None, "the synthetic league does not say"
    assert data["dry_run"] is True
    assert data["kind"] == "claim"
    assert data["reversible"] is False
    assert data["acknowledged_irreversible"] is None
    assert data["items"] == [
        {"player_id": "3002", "type": "add", "from_slot": None, "to_slot": None},
        {"player_id": "2003", "type": "drop", "from_slot": "BE", "to_slot": None},
    ]
    assert data["cost"] == {"bid_amount": 12, "displaced_player_id": "2003"}
    assert "consumes waiver priority" in data["irreversible_because"]
    assert "Katherine Johnson (2003) goes to waivers" in data["irreversible_because"]
    assert "nothing was sent and nothing was journaled" in data["note"]
    assert "whether it would accept them" in data["note"] and "not checked" in data["note"]
    assert set(data["credentials"]) == {"espn_s2_age_seconds", "warning"}
    for key in ("provider_transaction_id", "journal_entry_id", "read_back", "notified"):
        assert data[key] is None
    assert espn.posts == []
    assert not _journal_file(isolated_home).exists()


def test_dry_run_echoes_a_justification_it_was_given(espn: WritableEspn):
    data = _drop(dry_run=True, acknowledge_irreversible=WHY)["data"]
    assert data["acknowledged_irreversible"] == WHY
    assert data["cost"] == {"bid_amount": None, "displaced_player_id": "2003"}
    assert espn.posts == []


def test_dry_run_still_runs_every_preflight(espn: WritableEspn):
    with pytest.raises(ConfigInvalidError, match="on waivers"):
        _add(player="Radia Perlman", dry_run=True)


def test_dry_run_still_refuses_a_team_the_session_does_not_own(
    espn: WritableEspn, isolated_home: Path
):
    with pytest.raises(WriteRejectedError) as caught:
        _drop(team="Team Bravo", player="2011", dry_run=True)
    assert caught.value.kind == "not_owned"
    assert not _journal_file(isolated_home).exists()


# --------------------------------------------------------------------------- #
# The happy paths
# --------------------------------------------------------------------------- #


def test_an_add_with_a_drop_is_one_freeagent_transaction_journaled_with_its_cost(
    espn: WritableEspn,
):
    payload = _add(drop="Katherine Johnson", acknowledge_irreversible=WHY)
    data = payload["data"]

    assert payload["error"] is None
    assert set(data) == SUCCESS_KEYS
    assert data["kind"] == "add"
    assert data["team_id"] == "1"
    assert data["provider_transaction_id"] == TRANSACTION_ID
    assert data["status"] == "EXECUTED"
    assert data["is_pending"] is False
    assert data["proposed_at"].endswith("Z")
    assert data["reversible"] is False
    assert data["acknowledged_irreversible"] == WHY
    assert data["cost"] == {"bid_amount": None, "displaced_player_id": "2003"}
    assert data["read_back"] == {"verified": True, "diverged": []}
    assert set(data["notified"]) == NOTIFY_KEYS
    assert data["notified"]["enabled"] is False and data["notified"]["attempted"] is False
    assert data["dry_run"] is False

    (body,) = espn.posts
    assert body["type"] == "FREEAGENT" and body["executionType"] == "EXECUTE"
    assert body["teamId"] == 1 and body["scoringPeriodId"] == 2
    assert body["isLeagueManager"] is False and "memberId" not in body
    assert "bidAmount" not in body
    assert body["items"] == [
        {"playerId": 3001, "type": "ADD", "toTeamId": 1},
        {"playerId": 2003, "type": "DROP", "fromTeamId": 1},
    ]
    assert "3001" in espn.slots() and "2003" not in espn.slots()
    assert espn.pool_ids()["2003"] == "WAIVERS"

    (entry,) = _entries()
    assert entry.id == data["journal_entry_id"]
    assert entry.kind == "add" and entry.reversible is False
    assert entry.status == "executed"
    assert entry.cost == {"bid_amount": None, "displaced_player_id": "2003"}
    assert entry.prior_state == {"roster": {"2003": "BE"}, "pool": {"3001": "FREEAGENT"}}
    assert entry.applied_state is not None
    assert entry.applied_state["roster"] == {"3001": "BE", "2003": None}
    assert entry.applied_state["is_pending"] is False
    assert [item["type"] for item in entry.applied_state["items"]] == ["ADD", "DROP"]
    assert entry.provider_transaction_id == TRANSACTION_ID
    assert entry.member_id is not None and entry.role == "creator"
    assert entry.scoring_period == 2
    assert entry.command[:2] == ("fantasy-sports", "add")


def test_an_add_names_a_slot_only_when_asked(espn: WritableEspn):
    data = _add(player="3003", slot="rb/wr/te", acknowledge_irreversible=WHY)["data"]
    assert espn.posts[0]["items"] == [
        {"playerId": 3003, "type": "ADD", "toTeamId": 1, "toLineupSlotId": 23}
    ]
    assert data["items"][0]["to_slot"] == "RB/WR/TE"
    assert data["read_back"] == {"verified": True, "diverged": []}
    assert espn.slots()["3003"] == 23


def test_a_drop_sends_one_drop_item_and_the_player_lands_on_waivers(espn: WritableEspn):
    data = _drop(player="hopper", acknowledge_irreversible=WHY)["data"]
    assert data["kind"] == "drop"
    assert data["items"] == [
        {"player_id": "2002", "type": "drop", "from_slot": "RB", "to_slot": None}
    ]
    assert data["cost"] == {"bid_amount": None, "displaced_player_id": "2002"}
    assert data["read_back"] == {"verified": True, "diverged": []}
    # A FREEAGENT envelope must carry exactly one ADD (brief 06), so a drop
    # alone goes as ROSTER.
    assert espn.posts[0]["type"] == "ROSTER"
    assert espn.posts[0]["items"] == [{"playerId": 2002, "type": "DROP", "fromTeamId": 1}]
    assert "2002" not in espn.slots()
    (entry,) = _entries()
    assert entry.kind == "drop" and entry.prior_state == {"roster": {"2002": "RB"}, "pool": {}}


def test_a_claim_is_pending_carries_its_bid_and_is_found_in_the_queue_by_id(espn: WritableEspn):
    before = espn.slots()
    data = _claim(bid=7, acknowledge_irreversible=WHY)["data"]
    assert data["kind"] == "claim"
    assert data["status"] == "PENDING"
    assert data["is_pending"] is True
    assert data["provider_transaction_id"] == TRANSACTION_ID
    assert data["cost"] == {"bid_amount": 7, "displaced_player_id": None}
    assert data["read_back"] == {
        "verified": True,
        "diverged": [],
        "matched_by": ["provider_transaction_id"],
        "queue_size": 1,
    }
    (body,) = espn.posts
    assert body["type"] == "WAIVER" and body["bidAmount"] == 7
    assert body["items"] == [{"playerId": 3002, "type": "ADD", "toTeamId": 1}]
    assert espn.slots() == before, "a pending claim changes nothing yet"
    (entry,) = _entries()
    assert entry.kind == "claim" and entry.status == "executed"
    assert entry.cost == {"bid_amount": 7, "displaced_player_id": None}
    assert entry.prior_state == {"roster": {}, "pool": {"3002": "WAIVERS"}}
    assert entry.applied_state is not None and entry.applied_state["is_pending"] is True
    assert entry.request["bid"] == 7


def test_a_claim_without_a_bid_sends_no_bid_amount(espn: WritableEspn):
    """A league with no acquisition budget rejects every bid, even 1 (brief 06)."""
    payload = _claim(bid=None, acknowledge_irreversible=WHY)
    data = payload["data"]
    (body,) = espn.posts
    assert body["type"] == "WAIVER"
    assert "bidAmount" not in body
    assert body["items"] == [{"playerId": 3002, "type": "ADD", "toTeamId": 1}]
    assert data["is_pending"] is True
    assert data["cost"] == {"bid_amount": None, "displaced_player_id": None}
    assert data["read_back"]["verified"] is True
    (entry,) = _entries()
    assert "--bid" not in entry.command
    assert entry.cost["bid_amount"] is None


def test_a_bidless_claim_still_needs_the_flag_and_says_what_it_spends(espn: WritableEspn):
    with pytest.raises(ConfigInvalidError, match="consumes waiver priority"):
        _claim(bid=None)
    assert espn.posts == []


def _without_budget(espn: WritableEspn) -> None:
    espn.bootstrap["settings"]["acquisitionSettings"] = {
        "acquisitionType": "WAIVERS_TRADITIONAL",
        "isUsingAcquisitionBudget": False,
    }
    espn._publish(espn.bootstrap)


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_bid_in_a_league_without_a_budget_is_refused_before_sending(
    espn: WritableEspn, isolated_home: Path, dry_run: bool
):
    _without_budget(espn)
    with pytest.raises(ConfigInvalidError) as caught:
        _claim(bid=1, dry_run=dry_run, acknowledge_irreversible=WHY)
    error = caught.value
    assert error.message == "This league does not use an acquisition budget; omit --bid."
    assert error.details["kind"] == "argument"
    assert error.details["uses_budget"] is False
    assert error.details["acquisition_type"] == "WAIVERS_TRADITIONAL"
    assert espn.posts == []
    assert not _journal_file(isolated_home).exists()


def test_a_bidless_claim_in_a_league_without_a_budget_is_sent(espn: WritableEspn):
    _without_budget(espn)
    dry = _claim(bid=None, dry_run=True)["data"]
    assert dry["uses_budget"] is False
    assert dry["acquisition_type"] == "WAIVERS_TRADITIONAL"
    _claim(bid=None, acknowledge_irreversible=WHY)
    (body,) = espn.posts
    assert body["type"] == "WAIVER" and "bidAmount" not in body


def test_a_bid_in_a_league_with_a_budget_is_sent(espn: WritableEspn):
    assert espn.bootstrap["settings"]["acquisitionSettings"]["isUsingAcquisitionBudget"]
    _claim(bid=9, acknowledge_irreversible=WHY)
    assert espn.posts[0]["bidAmount"] == 9


def test_acquisition_settings_the_provider_does_not_publish_refuse_nothing():
    from types import SimpleNamespace

    from fantasy_sports.commands.moves import acquisition_settings

    unknown = {"acquisition_type": None, "uses_budget": None}
    assert acquisition_settings(SimpleNamespace(raw={})) == unknown
    assert acquisition_settings(SimpleNamespace()) == unknown
    assert acquisition_settings(SimpleNamespace(raw={"settings": []})) == unknown
    odd = {"settings": {"acquisitionSettings": {"isUsingAcquisitionBudget": "no"}}}
    assert acquisition_settings(SimpleNamespace(raw=odd)) == unknown


def test_add_and_drop_dry_runs_do_not_report_acquisition_settings(espn: WritableEspn):
    for data in (_add(dry_run=True)["data"], _drop(dry_run=True)["data"]):
        assert "uses_budget" not in data and "acquisition_type" not in data


def test_a_zero_bid_is_a_bid(espn: WritableEspn):
    data = _claim(bid=0, acknowledge_irreversible=WHY)["data"]
    assert espn.posts[0]["bidAmount"] == 0
    assert data["cost"]["bid_amount"] == 0


def test_a_claim_with_a_drop_names_the_displaced_player(espn: WritableEspn):
    data = _claim(drop="2003", acknowledge_irreversible=WHY)["data"]
    assert data["cost"] == {"bid_amount": 7, "displaced_player_id": "2003"}
    assert [item["type"] for item in espn.posts[0]["items"]] == ["ADD", "DROP"]
    assert "2003" in espn.slots(), "the drop rides on the claim and waits with it"


def test_a_claim_espn_does_not_list_is_unverified_not_verified(monkeypatch: pytest.MonkeyPatch):
    install_writable_espn(monkeypatch, show_pending=False)
    data = _claim(acknowledge_irreversible=WHY)["data"]
    read_back = data["read_back"]
    assert read_back == {
        "verified": None,
        "diverged": [],
        "queue_size": 0,
        "note": NOT_IN_QUEUE_NOTE.format(claim_id=TRANSACTION_ID, team_id="1"),
    }
    assert "`fantasy-sports claims --team 1`" in read_back["note"]
    assert "warnings" not in data


def _queued(bid: int | None, *drops: int) -> dict[str, Any]:
    items = [{"playerId": 3002, "type": "ADD"}]
    items += [{"playerId": d, "type": "DROP"} for d in drops]
    return {
        "id": "0a1b2c3d-older-claim",
        "teamId": 1,
        "type": "WAIVER",
        "status": "PENDING",
        "scoringPeriodId": 2,
        "proposedDate": 1789041500000,
        "bidAmount": 0 if bid is None else bid,
        "items": items,
    }


@pytest.mark.parametrize(
    ("older", "claim"),
    [
        (_queued(5), {"bid": 9}),
        (_queued(7), {"bid": 7, "drop": "2003"}),
        (_queued(7, 2003), {"bid": 7}),
        (_queued(None), {"bid": 3}),
    ],
)
def test_an_older_pending_claim_does_not_verify_a_different_new_one(
    monkeypatch: pytest.MonkeyPatch, older: dict[str, Any], claim: dict[str, Any]
):
    """ESPN answers PENDING but the new claim is not visible (``ignore``); an
    earlier claim for the same player with a different bid or drop must not
    stand in for it (Codex on #122). The queue lists the older one by its own
    id, so the new one is unverified with a note, not matched."""
    espn = install_writable_espn(monkeypatch, mode="ignore")
    espn.pending.append(older)
    espn._publish(espn.bootstrap)
    data = _claim(acknowledge_irreversible=WHY, **claim)["data"]
    assert data["read_back"] == {
        "verified": None,
        "diverged": [],
        "queue_size": 1,
        "note": NOT_IN_QUEUE_NOTE.format(claim_id=TRANSACTION_ID, team_id="1"),
    }


def test_an_identical_earlier_claim_no_longer_hides_the_new_one(espn: WritableEspn):
    """#109: the queue carries ESPN's ids, so the join is exact. Under #18's
    details match two identical claims were indistinguishable."""
    espn.pending.append(_queued(7))
    espn._publish(espn.bootstrap)
    data = _claim(bid=7, acknowledge_irreversible=WHY)["data"]
    assert data["read_back"] == {
        "verified": True,
        "diverged": [],
        "matched_by": ["provider_transaction_id"],
        "queue_size": 2,
    }


def test_a_stale_cancelled_claim_in_transactions_never_verifies_a_new_one(
    monkeypatch: pytest.MonkeyPatch,
):
    """Codex on #122: a claim cancelled earlier keeps an identical ``PENDING``
    row in ``mTransactions2`` forever. With the new claim not yet visible and
    the queue empty, #18's details match took that ghost for the new claim
    and reported ``verified: true``. The read-back reads the queue only."""
    espn = install_writable_espn(monkeypatch, pending_view=False)
    stale = _claim(bid=7, acknowledge_irreversible=WHY)["data"]["provider_transaction_id"]
    espn.post(  # cancel it on the write host; `cancel-claim` would refuse a hidden id
        "",
        json={"type": "WAIVER", "executionType": "CANCEL", "relatedTransactionId": stale},
    )
    logged = espn.overrides["mTransactions2"]["transactions"]
    assert sorted(row["status"] for row in logged) == ["CANCELED", "PENDING"]
    assert espn.overrides["mPendingTransactions"] == {"pendingTransactions": []}

    espn.mode = "ignore"  # ESPN answers PENDING; the new row is not visible yet
    data = _claim(bid=7, acknowledge_irreversible=WHY)["data"]
    assert data["read_back"] == {
        "verified": None,
        "diverged": [],
        "queue_size": 0,
        "note": NOT_IN_QUEUE_NOTE.format(claim_id=data["provider_transaction_id"], team_id="1"),
    }


def test_a_second_claim_gets_its_own_id(espn: WritableEspn):
    first = _claim(bid=7, acknowledge_irreversible=WHY)["data"]
    second = _claim(bid=7, drop="2003", acknowledge_irreversible=WHY)["data"]
    assert first["provider_transaction_id"] == TRANSACTION_ID
    assert second["provider_transaction_id"] != TRANSACTION_ID
    assert second["read_back"]["verified"] is True
    assert second["read_back"]["queue_size"] == 2
    assert {row["proposedDate"] for row in espn.pending} == {PROPOSED_DATE_MS}


def test_a_bidless_claim_matches_the_zero_espn_records(espn: WritableEspn):
    data = _claim(bid=None, drop="2003", acknowledge_irreversible=WHY)["data"]
    assert data["read_back"]["verified"] is True


def test_a_claim_that_executes_at_once_is_read_back_against_the_roster(
    espn: WritableEspn, monkeypatch: pytest.MonkeyPatch
):
    """``is_pending`` decides the read-back, not the command: a claim ESPN
    processes immediately is verified like an add."""
    original = espn.post

    def immediate(url: str, json: Any = None, **kwargs: Any) -> Any:
        response = original(url, json=json, **kwargs)
        espn.pending.clear()
        updated = dict(json, type="FREEAGENT")
        espn.posts.pop()
        espn._apply(updated, "FREEAGENT", "EXECUTE")
        body = response.json()
        body.update(isPending=False, status="EXECUTED")
        response._body = body
        return response

    import requests

    monkeypatch.setattr(requests, "post", immediate)
    data = _claim(acknowledge_irreversible=WHY)["data"]
    assert data["is_pending"] is False
    assert data["read_back"] == {"verified": True, "diverged": []}


def test_the_journal_entry_exists_before_the_post_is_sent(
    espn: WritableEspn, monkeypatch: pytest.MonkeyPatch
):
    seen: list[tuple[str, bool, dict]] = []
    original = espn.post

    def post(url: str, **kwargs: Any) -> Any:
        seen.extend((e.status, e.reversible, dict(e.cost)) for e in _entries())
        return original(url, **kwargs)

    import requests

    monkeypatch.setattr(requests, "post", post)
    _drop(acknowledge_irreversible=WHY)
    assert seen == [("pending", False, {"bid_amount": None, "displaced_player_id": "2003"})]


# --------------------------------------------------------------------------- #
# Resolving players: refused by name, before anything is sent
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("call", "match"),
    [
        (lambda: _add(player="Ada Lovelace"), "No free agent in the current pool"),
        (lambda: _add(player="2001"), "No free agent in the current pool"),
        (lambda: _add(player="Frances"), "matches 2 free agents"),
        (lambda: _add(player="Radia Perlman"), "on waivers, so it cannot be added"),
        (lambda: _claim(player="Barbara Liskov"), "a free agent, not on waivers"),
        (lambda: _add(slot="QB"), "not eligible for the QB slot"),
        (lambda: _add(drop="Barbara Liskov"), "No player on this roster matches"),
        (lambda: _drop(player="3001"), "No player on this roster matches"),
        (lambda: _drop(player="a"), "matches 3 players on this roster"),
        (lambda: _claim(bid=-1), "whole number of 0 or more"),
        (lambda: _claim(bid=True), "whole number of 0 or more"),
    ],
)
def test_a_bad_move_is_refused_before_anything_is_sent(
    espn: WritableEspn, isolated_home: Path, call, match
):
    with pytest.raises(ConfigInvalidError, match=match) as caught:
        call()
    assert caught.value.details["kind"] == "argument"
    assert espn.posts == []
    assert not _journal_file(isolated_home).exists()


def test_a_waivers_refusal_points_at_claim(espn: WritableEspn):
    with pytest.raises(ConfigInvalidError) as caught:
        _add(player="3002", acknowledge_irreversible=WHY)
    assert "fantasy-sports claim" in caught.value.remediation
    assert caught.value.details["pool_status"] == "WAIVERS"


def test_a_player_not_in_the_pool_suggests_free_agents(espn: WritableEspn):
    with pytest.raises(ConfigInvalidError) as caught:
        _add(player="Nobody At All", acknowledge_irreversible=WHY)
    assert "free-agents" in caught.value.remediation
    assert caught.value.details["pool_size"] == 4


def test_a_team_the_session_does_not_own_is_refused_without_a_journal_row(
    espn: WritableEspn, isolated_home: Path
):
    with pytest.raises(WriteRejectedError) as caught:
        _add(team="Team Bravo", acknowledge_irreversible=WHY)
    assert caught.value.kind == "not_owned"
    assert caught.value.details["owned_team_ids"] == ["1"]
    assert espn.posts == []
    assert not _journal_file(isolated_home).exists()


# --------------------------------------------------------------------------- #
# What ESPN says back
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("provider_type", "kind"),
    [
        ("TRAN_LINEUP_LOCKED", "roster_lock"),
        ("TRAN_ROSTER_SLOT_LIMIT_EXCEEDED", "slot_limit"),
        ("TRAN_ROSTER_SAME_SLOT", "same_slot"),
        ("TRAN_INVALID_SCORINGPERIOD_NOT_CURRENT", "not_current_period"),
        ("TRAN_ROSTER_LIMIT_EXCEEDED_ONE", "roster_full"),
        ("TRAN_PLAYER_NOT_ON_TEAM", "already_dropped"),
    ],
)
def test_a_rejection_the_map_knows_passes_through_with_its_kind(
    monkeypatch: pytest.MonkeyPatch, provider_type: str, kind: str
):
    espn = install_writable_espn(monkeypatch, mode="reject", reject_type=provider_type)
    with pytest.raises(WriteRejectedError) as caught:
        _add(drop="2003", acknowledge_irreversible=WHY)
    error = caught.value
    assert error.kind == kind
    assert error.provider_type == provider_type
    assert error.http_status == 409
    assert exit_code_for(error) == 11
    assert len(espn.posts) == 1
    (entry,) = _entries()
    assert entry.status == "rejected"
    assert entry.error is not None and entry.error["code"] == "WRITE_REJECTED"
    assert entry.error["details"]["kind"] == kind


@pytest.mark.parametrize(
    ("capture", "call", "provider_type", "kind", "message"),
    [
        (
            "p1-freeagent-add-no-drop",
            lambda: _add(acknowledge_irreversible=WHY),
            "TRAN_PLAYER_NOT_FREEAGENT",
            "player_on_waivers",
            "is not a free agent",
        ),
        (
            "p2-waiver-claim-no-drop-full-roster",
            lambda: _claim(acknowledge_irreversible=WHY),
            "TRAN_ACQUISITION_BUDGET_NOT_SUPPORTED",
            "unknown",
            "does not support bids",
        ),
        (
            "p3-waiver-claim-no-bid-no-drop",
            lambda: _claim(bid=None, acknowledge_irreversible=WHY),
            "TRAN_ROSTER_LIMIT_EXCEEDED_ONE",
            "roster_full",
            "Too many players on roster",
        ),
        (
            "p6c-roster-drop-player-not-on-team",
            lambda: _drop(acknowledge_irreversible=WHY),
            "TRAN_PLAYER_NOT_ON_TEAM",
            "already_dropped",
            "is not on team",
        ),
        (
            "p6d-freeagent-add-rostered-elsewhere",
            lambda: _add(drop="2003", acknowledge_irreversible=WHY),
            "TRAN_PLAYER_NOT_AVAILABLE",
            "player_unavailable",
            "is not available",
        ),
    ],
)
def test_a_captured_rejection_passes_through_with_espns_message(
    monkeypatch: pytest.MonkeyPatch, capture, call, provider_type, kind, message
):
    """Brief 06's real 409s, replayed. An unmapped type stays ``unknown`` with
    ESPN's string verbatim; ESPN's message is the remediation, not re-mapped."""
    install_writable_espn(monkeypatch, mode="reject", reject_capture=capture)
    with pytest.raises(WriteRejectedError) as caught:
        call()
    error = caught.value
    assert error.kind == kind
    assert error.provider_type == provider_type
    assert error.details["provider_type"] == provider_type
    assert message in (error.remediation or "")
    assert [entry.status for entry in _entries()] == ["rejected"]


def test_a_rejected_move_is_not_announced(monkeypatch: pytest.MonkeyPatch, isolated_home: Path):
    calls: list[Any] = []
    monkeypatch.setattr(
        "fantasy_sports.reports.notify.notify_irreversible", lambda *a, **k: calls.append(a)
    )
    install_writable_espn(monkeypatch, mode="reject")
    with pytest.raises(WriteRejectedError):
        _drop(acknowledge_irreversible=WHY)
    assert calls == []


def test_a_timeout_is_never_retried_and_leaves_the_entry_pending(
    espn: WritableEspn, monkeypatch: pytest.MonkeyPatch
):
    import requests

    attempts: list[dict] = []

    def timeout(*_: Any, **kwargs: Any) -> Any:
        attempts.append(kwargs.get("json"))
        raise requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setattr(requests, "post", timeout)
    with pytest.raises(ProviderUnavailableError) as caught:
        _claim(acknowledge_irreversible=WHY)
    error = caught.value
    assert len(attempts) == 1, "a claim whose outcome is unknown is never re-sent"
    assert error.details["transaction_state"] == "unknown"
    assert "claims" in error.remediation and "twice" in error.remediation
    (entry,) = _entries()
    assert entry.status == "pending"
    with Journal.open() as journal:
        assert [e.id for e in journal.pending()] == [entry.id]


def test_a_read_back_that_disagrees_is_write_diverged_and_announced(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path
):
    out = isolated_home / "told.json"
    _notify_to(isolated_home, f"cat > '{out}'")
    install_writable_espn(monkeypatch, mode="ignore")
    with pytest.raises(WriteDivergedError) as caught:
        _add(drop="2003", acknowledge_irreversible=WHY)
    error = caught.value
    assert exit_code_for(error) == 12
    assert error.details["diverged"] == ["2003", "3001"]
    assert error.expected == {"2003": None, "3001": "rostered"}
    assert error.observed == {"2003": "BE", "3001": None}
    assert error.details["provider_transaction_id"] == TRANSACTION_ID
    assert error.details["transaction_state"] == "applied"
    assert error.details["notified"]["ok"] is True
    (entry,) = _entries()
    assert error.details["journal_entry_id"] == entry.id
    assert entry.status == "diverged"
    assert entry.applied_state is not None
    assert entry.applied_state["roster"] == {"3001": None, "2003": "BE"}
    assert entry.provider_transaction_id == TRANSACTION_ID
    # The mismatch is stored before the owner is told (Codex on #122).
    assert entry.error is not None and entry.error["code"] == "WRITE_DIVERGED"
    assert entry.error["details"]["expected"] == {"2003": None, "3001": "rostered"}
    told = json.loads(out.read_text())
    assert told["id"] == entry.id and told["status"] == "diverged"
    assert told["error"]["details"]["observed"] == {"2003": "BE", "3001": None}


def test_a_diverged_read_back_whose_journal_close_fails_still_raises_the_divergence(
    monkeypatch: pytest.MonkeyPatch,
):
    install_writable_espn(monkeypatch, mode="ignore")

    def broken(self: Journal, *_: Any, **__: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(Journal, "complete", broken)
    with pytest.raises(WriteDivergedError) as caught:
        _drop(acknowledge_irreversible=WHY)
    assert "could not be closed (OSError)" in caught.value.details["journal"]
    assert caught.value.details["notified"]["attempted"] is False
    assert [entry.status for entry in _entries()] == ["pending"]


def test_a_slot_that_reads_back_differently_diverges(
    espn: WritableEspn, monkeypatch: pytest.MonkeyPatch
):
    original = espn._apply

    def bench_instead(body: Any, kind: str, execution: str, *rest: Any) -> None:
        items = [dict(item, toLineupSlotId=BENCH) for item in body["items"]]
        original(dict(body, items=items), kind, execution, *rest)

    monkeypatch.setattr(espn, "_apply", bench_instead)
    with pytest.raises(WriteDivergedError) as caught:
        _add(player="3001", slot="WR", acknowledge_irreversible=WHY)
    assert caught.value.expected == {"3001": "WR"}
    assert caught.value.observed == {"3001": "BE"}


def test_a_move_applied_as_another_account_is_journaled_diverged_and_announced(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path
):
    out = isolated_home / "told.json"
    _notify_to(isolated_home, f"cat > '{out}'")
    install_writable_espn(monkeypatch, echo_member=OTHER_MEMBER)
    with pytest.raises(WriteDivergedError) as caught:
        _drop(acknowledge_irreversible=WHY)
    assert caught.value.details["transaction_state"] == "applied"
    assert caught.value.details["notified"]["ok"] is True
    (entry,) = _entries()
    assert entry.status == "diverged"
    assert entry.applied_state is not None
    assert [item["playerId"] for item in entry.applied_state["items"]] == [2003]
    assert json.loads(out.read_text())["id"] == entry.id


@pytest.mark.parametrize(
    ("mode", "closer", "raised"),
    [("reject", "reject", WriteRejectedError), ("identity", "diverge", WriteDivergedError)],
)
def test_a_journal_that_fails_to_close_never_replaces_the_write_outcome(
    monkeypatch: pytest.MonkeyPatch, mode: str, closer: str, raised: type
):
    if mode == "identity":
        install_writable_espn(monkeypatch, echo_member=OTHER_MEMBER)
    else:
        install_writable_espn(monkeypatch, mode="reject")

    def broken(self: Journal, *_: Any, **__: Any) -> Any:
        raise OSError("database is locked")

    monkeypatch.setattr(Journal, closer, broken)
    with pytest.raises(raised) as caught:
        _drop(acknowledge_irreversible=WHY)
    assert "could not be closed (OSError)" in caught.value.details["journal"]
    assert [entry.status for entry in _entries()] == ["pending"]


def test_a_journal_that_cannot_be_written_stops_the_write(espn: WritableEspn, isolated_home: Path):
    state = isolated_home / "state" / "fantasy-sports"
    state.mkdir(parents=True)
    (state / "journal.sqlite").mkdir()  # a directory where the file should be
    with pytest.raises(ConfigInvalidError):
        _drop(acknowledge_irreversible=WHY)
    assert espn.posts == []


def test_a_journal_that_cannot_be_completed_is_a_warning_and_nothing_is_announced(
    espn: WritableEspn, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
):
    _notify_to(isolated_home, "true")

    def broken(self: Journal, *_: Any, **__: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(Journal, "complete", broken)
    data = _drop(acknowledge_irreversible=WHY)["data"]
    assert data["provider_transaction_id"] == TRANSACTION_ID
    assert data["read_back"]["verified"] is True
    assert len(data["warnings"]) == 1 and "pending" in data["warnings"][0]
    assert data["notified"]["enabled"] is True
    assert data["notified"]["attempted"] is False
    assert "pending" in data["notified"]["error"]


@pytest.mark.parametrize("command", ["drop", "claim"])
def test_a_read_back_that_fails_is_an_unverified_success(
    espn: WritableEspn, monkeypatch: pytest.MonkeyPatch, command: str
):
    from fantasy_sports.providers.espn import EspnProvider

    method = "fetch_roster" if command == "drop" else "fetch_pending_claims"
    original = getattr(EspnProvider, method)
    calls: list[int] = []

    def flaky(self: EspnProvider, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if method == "fetch_pending_claims" or len(calls) > 1:
            raise ProviderUnavailableError("down")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(EspnProvider, method, flaky)
    data = (_drop if command == "drop" else _claim)(acknowledge_irreversible=WHY)["data"]
    assert data["read_back"] == {
        "verified": False,
        "diverged": [],
        "error": "PROVIDER_UNAVAILABLE",
    }
    assert "could not be read back" in data["warnings"][0]
    (entry,) = _entries()
    assert entry.status == "executed"


# --------------------------------------------------------------------------- #
# Telling the owner (#113)
# --------------------------------------------------------------------------- #


def test_the_owner_is_told_with_the_stored_entry(espn: WritableEspn, isolated_home: Path):
    out = isolated_home / "told.json"
    _notify_to(isolated_home, f"cat > '{out}'")
    data = _claim(acknowledge_irreversible=WHY)["data"]
    assert data["notified"] == {
        "enabled": True,
        "attempted": True,
        "ok": True,
        "exit_status": 0,
        "error": None,
    }
    told = json.loads(out.read_text())
    with Journal.open() as journal:
        stored = journal.get(data["journal_entry_id"])
    assert stored is not None
    assert told == stored.to_dict()
    assert told["status"] == "executed" and told["reversible"] is False
    assert told["cost"] == {"bid_amount": 7, "displaced_player_id": None}


def test_a_failing_notify_command_does_not_fail_the_write(espn: WritableEspn, isolated_home: Path):
    _notify_to(isolated_home, "exit 3")
    payload = _drop(acknowledge_irreversible=WHY)
    assert payload["error"] is None
    assert payload["data"]["notified"]["ok"] is False
    assert payload["data"]["notified"]["exit_status"] == 3
    assert [entry.status for entry in _entries()] == ["executed"]


def test_a_notifier_that_raises_is_reported_not_raised(
    espn: WritableEspn, monkeypatch: pytest.MonkeyPatch
):
    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr("fantasy_sports.reports.notify.notify_irreversible", boom)
    data = _drop(acknowledge_irreversible=WHY)["data"]
    assert data["notified"]["attempted"] is False
    assert data["notified"]["error"] == "notify skipped (RuntimeError)"


def test_an_unreadable_notify_config_reads_as_disabled(monkeypatch: pytest.MonkeyPatch):
    from fantasy_sports.commands import moves
    from fantasy_sports.config import notify as config_notify

    def broken(*_: Any, **__: Any) -> Any:
        raise ConfigInvalidError("bad")

    monkeypatch.setattr(config_notify, "load", broken)
    assert moves._notify_enabled() is False


# --------------------------------------------------------------------------- #
# The cache and the journal afterwards
# --------------------------------------------------------------------------- #


def test_a_success_purges_cached_rosters_and_free_agents(espn: WritableEspn):
    free_agents = REGISTRY["free-agents"].resolve()
    roster = REGISTRY["roster"].resolve()
    before = [fa["provider_id"] for fa in free_agents(week=2, limit=50).to_dict()["data"]]
    held = [slot["provider_id"] for slot in roster(team="1").to_dict()["data"]]
    assert "3001" in before and "3001" not in held

    _add(drop="2003", acknowledge_irreversible=WHY)

    # Default cache mode: a stale entry would still list 3001 as a free agent.
    after = [fa["provider_id"] for fa in free_agents(week=2, limit=50).to_dict()["data"]]
    now_held = [slot["provider_id"] for slot in roster(team="1").to_dict()["data"]]
    assert "3001" not in after and "2003" in after
    assert "3001" in now_held and "2003" not in now_held


def test_an_irreversible_entry_cannot_be_reversed(espn: WritableEspn):
    entry_id = _drop(acknowledge_irreversible=WHY)["data"]["journal_entry_id"]
    posts = len(espn.posts)
    with pytest.raises(NotAvailableError):
        REGISTRY["journal reverse"].resolve()(id=entry_id)
    assert len(espn.posts) == posts


def test_digest_lists_the_move_as_irreversible(espn: WritableEspn):
    _claim(acknowledge_irreversible=WHY)
    listed = REGISTRY["journal digest"].resolve()(since="24h", irreversible=True).to_dict()
    (row,) = listed["data"]
    assert row["kind"] == "claim" and row["reversible"] is False


# --------------------------------------------------------------------------- #
# The pieces, directly
# --------------------------------------------------------------------------- #


def test_the_statement_for_each_kind():
    from fantasy_sports.commands.moves import irreversible_because

    assert irreversible_because("drop", None, "A (1)") == (
        "A (1) goes to waivers and can be claimed by any team; this cannot be reversed."
    )
    claim = irreversible_because("claim", "B (2)", None)
    assert claim.startswith("A processed claim for B (2) consumes waiver priority")
    both = irreversible_because("add", "B (2)", "A (1)")
    assert both.startswith("B (2) joins the roster") and both.endswith("cannot be reversed.")


@pytest.mark.parametrize(
    ("uses_budget", "mentions_bid"),
    [(True, True), (False, False), (None, False)],
)
def test_waiver_add_hint_only_recommends_bid_when_budget_is_supported(uses_budget, mentions_bid):
    from types import SimpleNamespace

    from fantasy_sports.commands.moves import plan_move

    agent = SimpleNamespace(
        provider_id="9",
        player=SimpleNamespace(name="Nine", provider_id="9", eligible_slots=()),
        raw={"status": "WAIVERS"},
    )
    with pytest.raises(ConfigInvalidError) as caught:
        plan_move(
            "add",
            roster=[],
            pool=[agent],
            player="9",
            drop=None,
            slot=None,
            bid=None,
            acquisition={"uses_budget": uses_budget},
        )
    assert "fantasy-sports claim" in caught.value.remediation
    assert ("--bid N" in caught.value.remediation) is mentions_bid


def test_a_pool_entry_without_a_status_is_not_refused_on_its_account():
    from types import SimpleNamespace

    from fantasy_sports.commands.moves import plan_move

    agent = SimpleNamespace(
        provider_id="9",
        player=SimpleNamespace(name="Nine", provider_id="9", eligible_slots=()),
        raw={},
    )
    plan = plan_move("claim", roster=[], pool=[agent], player="9", drop=None, slot="WR", bid=0)
    assert plan.prior_state == {"roster": {}, "pool": {"9": None}}
    assert plan.adds[0].to_slot == "WR"
