"""``lineup set`` and ``journal reverse``, offline (jwulff/fantasy-sports#15, #16).

Every test runs the real command against the real ESPN adapter. Reads replay
the synthetic league ``99`` through the same ``requests.get`` stub the read
commands use; the write host is :class:`_harness.WritableEspn`, which answers
a POST the way ``docs/research/05-espn-write-surface/`` captured ESPN doing it
and applies the items to the rosters later reads return, so the read-back has
something real to compare. The clock is fixed before the week-2 kickoffs so no
synthetic player reads as locked unless a test says so.

Team 1 in the synthetic league, owned by the session here:

====== ================== ====== ===================================
id     name               slot   eligible
====== ================== ====== ===================================
2001   Ada Lovelace       QB     QB, BE, IR
2002   Grace Hopper       RB     RB, RB/WR, RB/WR/TE, BE, IR
2003   Katherine Johnson  BE     RB/WR, WR, WR/TE, RB/WR/TE, BE, IR
====== ================== ====== ===================================

``pytest-socket`` is on; nothing here reaches ESPN.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _harness import (
    TRANSACTION_ID,
    WritableEspn,
    install_writable_espn,
    isolate_home,
)

from fantasy_sports.commands import REGISTRY, DataShape
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

# ESPN slot ids, for asserting on the request body.
QB, RB, WR, FLEX, BENCH = 0, 2, 4, 23, 20

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
    "dry_run",
}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield isolate_home(tmp_path, monkeypatch)
    forget_secrets()


@pytest.fixture
def espn(monkeypatch: pytest.MonkeyPatch) -> WritableEspn:
    return install_writable_espn(monkeypatch)


def _set(*assignments: str, team: str = "1", **kwargs: Any) -> dict[str, Any]:
    envelope = REGISTRY["lineup set"].resolve()(team=team, set=list(assignments), **kwargs)
    return envelope.to_dict()


def _reverse(entry_id: str, **kwargs: Any) -> dict[str, Any]:
    return REGISTRY["journal reverse"].resolve()(id=entry_id, **kwargs).to_dict()


def _entries() -> list[JournalEntry]:
    with Journal.open() as journal:
        return journal.list(limit=50)


def _journal_file(home: Path) -> Path:
    return home / "state" / "fantasy-sports" / "journal.sqlite"


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_lineup_set_is_registered_as_an_object_command_that_takes_a_league():
    spec = REGISTRY["lineup set"]
    assert spec.shape is DataShape.OBJECT
    assert spec.takes_league
    names = [param.name for param in spec.params]
    assert names == ["team", "set", "dry_run"]
    assert "week" not in names, "a lineup write is for the current period only (brief §4)"
    assert "acknowledge_irreversible" not in names


def test_journal_reverse_is_registered_and_selects_its_league_from_the_entry():
    spec = REGISTRY["journal reverse"]
    assert spec.shape is DataShape.OBJECT
    assert not spec.takes_league
    assert [param.name for param in spec.params] == ["id", "dry_run"]


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


def test_a_swap_sends_one_transaction_journals_it_and_reads_it_back(espn: WritableEspn):
    payload = _set("2002=BE", "Katherine Johnson=WR")
    data = payload["data"]

    assert payload["error"] is None
    assert set(data) == SUCCESS_KEYS
    assert data["team_id"] == "1"
    assert data["kind"] == "lineup"
    assert data["provider_transaction_id"] == TRANSACTION_ID
    assert data["status"] == "EXECUTED"
    assert data["is_pending"] is False
    assert data["proposed_at"].endswith("Z")
    assert data["reversible"] is True
    assert data["acknowledged_irreversible"] is None
    assert data["cost"] == {"bid_amount": None, "displaced_player_id": None}
    assert set(data["credentials"]) == {"espn_s2_age_seconds", "warning"}
    assert data["read_back"] == {"verified": True, "diverged": []}
    assert data["dry_run"] is False
    # Vacates before fills.
    assert data["items"] == [
        {"player_id": "2002", "from_slot": "RB", "to_slot": "BE"},
        {"player_id": "2003", "from_slot": "BE", "to_slot": "WR"},
    ]

    # One POST, for exactly the moved players, never a memberId or an LM flag.
    assert len(espn.posts) == 1
    body = espn.posts[0]
    assert body["teamId"] == 1 and body["type"] == "ROSTER"
    assert body["isLeagueManager"] is False and "memberId" not in body
    assert body["scoringPeriodId"] == 2
    assert {(i["playerId"], i["fromLineupSlotId"], i["toLineupSlotId"]) for i in body["items"]} == {
        (2002, RB, BENCH),
        (2003, BENCH, WR),
    }
    assert espn.slots() == {"2001": QB, "2002": BENCH, "2003": WR}

    (entry,) = _entries()
    assert entry.id == data["journal_entry_id"]
    assert entry.status == "executed"
    assert entry.kind == "lineup" and entry.reversible is True
    assert entry.team_id == "1" and entry.league_id == "99" and entry.season == 2026
    assert entry.scoring_period == 2
    assert entry.role == "creator"
    assert entry.prior_state == {"2002": "RB", "2003": "BE"}
    assert entry.applied_state == {"2002": "BE", "2003": "WR"}
    assert entry.provider_transaction_id == TRANSACTION_ID
    assert entry.proposed_at is not None and entry.member_id is not None
    assert entry.command[:3] == ("fantasy-sports", "lineup", "set")


def test_the_journal_entry_exists_before_the_post_is_sent(
    espn: WritableEspn, monkeypatch: pytest.MonkeyPatch
):
    seen: list[str] = []
    original = espn.post

    def post(url: str, **kwargs: Any) -> Any:
        seen.extend(entry.status for entry in _entries())
        return original(url, **kwargs)

    import requests

    monkeypatch.setattr(requests, "post", post)
    _set("2003=RB/WR/TE")
    assert seen == ["pending"]
    assert [entry.status for entry in _entries()] == ["executed"]


def test_a_name_resolves_case_insensitively_and_by_a_unique_fragment(espn: WritableEspn):
    data = _set("grace hopper=be", "katherine=rb/wr/te")["data"]
    assert [item["player_id"] for item in data["items"]] == ["2002", "2003"]


def test_a_named_player_already_in_place_rides_along_without_an_item(espn: WritableEspn):
    data = _set("2001=QB", "2003=RB/WR/TE")["data"]
    assert data["items"] == [{"player_id": "2003", "from_slot": "BE", "to_slot": "RB/WR/TE"}]
    assert [item["playerId"] for item in espn.posts[0]["items"]] == [2003]
    (entry,) = _entries()
    assert entry.prior_state == {"2001": "QB", "2003": "BE"}


# --------------------------------------------------------------------------- #
# --dry-run
# --------------------------------------------------------------------------- #


def test_dry_run_reports_the_items_and_sends_and_journals_nothing(
    espn: WritableEspn, isolated_home: Path
):
    data = _set("2002=BE", "2003=WR", dry_run=True)["data"]
    assert espn.posts == []
    assert not _journal_file(isolated_home).exists()
    assert data["dry_run"] is True
    assert data["items"] == [
        {"player_id": "2002", "from_slot": "RB", "to_slot": "BE"},
        {"player_id": "2003", "from_slot": "BE", "to_slot": "WR"},
    ]
    for key in ("provider_transaction_id", "journal_entry_id", "status", "read_back"):
        assert data[key] is None, key
    assert set(data["credentials"]) == {"espn_s2_age_seconds", "warning"}
    assert "not checked" in data["note"] and "nothing was sent" in data["note"]


def test_dry_run_still_runs_every_preflight(espn: WritableEspn):
    with pytest.raises(ConfigInvalidError):
        _set("2001=WR", dry_run=True)


def test_dry_run_still_refuses_a_team_the_session_does_not_own(espn: WritableEspn):
    with pytest.raises(WriteRejectedError) as caught:
        _set("2011=BE", team="2", dry_run=True)
    assert caught.value.kind == "not_owned"


# --------------------------------------------------------------------------- #
# Preflight: each a named CONFIG_INVALID before any request or journal row
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("assignments", "fragment", "detail"),
    [
        (("2001=QB",), "already matches", None),
        (("2001=WR",), "not eligible", "2001"),
        (("2002=RB/WR/TE", "2003=RB/WR/TE"), "would hold 2 players", None),
        (("2002=BE", "Grace Hopper=RB"), "more than once", "2002"),
        (("9999=BE",), "No player on this roster", None),
        (("o=BE",), "matches 3 players", None),
        (("2002",), "not PLAYER=SLOT", None),
        (("=BE",), "not PLAYER=SLOT", None),
        (("2003=IR",), "IR slot would hold 1", None),
    ],
    ids=[
        "empty-diff",
        "ineligible",
        "over-count",
        "named-twice",
        "unknown",
        "ambiguous",
        "no-equals",
        "no-player",
        "slot-the-league-lacks",
    ],
)
def test_a_bad_target_is_refused_before_anything_is_sent(
    espn: WritableEspn, isolated_home: Path, assignments, fragment, detail
):
    with pytest.raises(ConfigInvalidError) as caught:
        _set(*assignments)
    error = caught.value
    assert error.kind == "argument"
    assert fragment in str(error)
    assert error.remediation
    if detail is not None:
        assert error.details["player_id"] == detail
    assert espn.posts == []
    assert not _journal_file(isolated_home).exists()


def test_a_locked_player_is_refused_by_name(
    espn: WritableEspn, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
):
    after_kickoff = datetime(2026, 9, 13, 18, 0, tzinfo=UTC)  # 2002 kicked off, 2003 has not
    monkeypatch.setattr("fantasy_sports.providers.espn.utc_now", lambda: after_kickoff)
    with pytest.raises(ConfigInvalidError) as caught:
        _set("2002=BE")
    assert "locked" in str(caught.value)
    assert caught.value.details == {"player_id": "2002", "slot": "RB", "kind": "argument"}
    assert espn.posts == []
    # An unlocked player on the same roster still moves.
    assert _set("2003=RB/WR/TE")["data"]["read_back"]["verified"] is True


def test_a_team_the_session_does_not_own_is_refused_without_a_journal_row(
    espn: WritableEspn, isolated_home: Path
):
    with pytest.raises(WriteRejectedError) as caught:
        _set("2011=BE", team="Team Bravo")
    assert caught.value.kind == "not_owned"
    assert caught.value.details["owned_team_ids"] == ["1"]
    assert espn.posts == []
    assert not _journal_file(isolated_home).exists()


# --------------------------------------------------------------------------- #
# What ESPN says back
# --------------------------------------------------------------------------- #


def test_an_espn_rejection_is_write_rejected_and_journaled_as_rejected(
    monkeypatch: pytest.MonkeyPatch,
):
    espn = install_writable_espn(monkeypatch, mode="reject")
    with pytest.raises(WriteRejectedError) as caught:
        _set("2002=BE", "2003=WR")
    error = caught.value
    assert error.kind == "roster_lock"
    assert error.provider_type == "TRAN_LINEUP_LOCKED"
    assert error.http_status == 409
    assert exit_code_for(error) == 11
    assert len(espn.posts) == 1
    (entry,) = _entries()
    assert entry.status == "rejected"
    assert entry.error is not None and entry.error["code"] == "WRITE_REJECTED"
    assert entry.applied_state is None


def test_a_read_back_that_disagrees_is_write_diverged_with_the_differing_players(
    monkeypatch: pytest.MonkeyPatch,
):
    install_writable_espn(monkeypatch, mode="ignore")
    with pytest.raises(WriteDivergedError) as caught:
        _set("2002=BE", "2003=WR")
    error = caught.value
    assert exit_code_for(error) == 12
    assert error.details["diverged"] == ["2002", "2003"]
    assert error.expected == {"2002": "BE", "2003": "WR"}
    assert error.observed == {"2002": "RB", "2003": "BE"}
    assert error.details["provider_transaction_id"] == TRANSACTION_ID
    (entry,) = _entries()
    assert error.details["journal_entry_id"] == entry.id
    assert entry.status == "diverged"
    assert entry.provider_transaction_id == TRANSACTION_ID
    assert entry.applied_state == {"2002": "RB", "2003": "BE"}
    assert entry.error is not None and entry.error["code"] == "WRITE_DIVERGED"
    assert entry.error["details"]["diverged"] == ["2002", "2003"]


def test_a_write_applied_as_another_account_is_journaled_as_diverged(
    monkeypatch: pytest.MonkeyPatch,
):
    other = "{9F8E7D6C-5B4A-3928-1706-F5E4D3C2B1A0}"
    install_writable_espn(monkeypatch, echo_member=other)
    with pytest.raises(WriteDivergedError) as caught:
        _set("2003=RB/WR/TE")
    assert caught.value.details["transaction_state"] == "applied"
    (entry,) = _entries()
    assert entry.status == "diverged"
    assert entry.applied_state is not None
    assert [item["playerId"] for item in entry.applied_state["items"]] == [2003]
    assert entry.error is not None and entry.error["code"] == "WRITE_DIVERGED"


def test_a_write_whose_outcome_is_unknown_leaves_the_entry_pending(
    espn: WritableEspn, monkeypatch: pytest.MonkeyPatch
):
    import requests

    def timeout(*_: Any, **__: Any) -> Any:
        raise requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setattr(requests, "post", timeout)
    with pytest.raises(ProviderUnavailableError) as caught:
        _set("2003=RB/WR/TE")
    assert caught.value.details["transaction_state"] == "unknown"
    (entry,) = _entries()
    assert entry.status == "pending"
    with Journal.open() as journal:
        assert [e.id for e in journal.pending()] == [entry.id]


def test_a_roster_that_moved_before_the_adapter_reread_closes_the_entry_rejected(
    espn: WritableEspn,
):
    """The adapter re-reads ``mRoster`` live and finds 2003 already in place,
    so it refuses an empty diff before any POST. A known no-send is not an
    unknown outcome: the entry closes ``rejected``, never ``pending``."""
    import copy

    moved = copy.deepcopy(espn.bootstrap)
    team = next(t for t in moved["teams"] if t["id"] == 1)
    next(e for e in team["roster"]["entries"] if e["playerId"] == 2003)["lineupSlotId"] = FLEX
    espn.overrides["mRoster"] = moved
    with pytest.raises(ConfigInvalidError, match="nothing to send"):
        _set("2003=RB/WR/TE")
    assert espn.posts == []
    (entry,) = _entries()
    assert entry.status == "rejected"
    assert entry.error is not None and entry.error["code"] == "CONFIG_INVALID"
    with Journal.open() as journal:
        assert journal.pending() == []


@pytest.mark.parametrize(
    ("mode", "closer", "raised"),
    [
        ("reject", "reject", WriteRejectedError),
        ("identity", "diverge", WriteDivergedError),
    ],
)
def test_a_journal_that_fails_to_close_never_replaces_the_write_outcome(
    monkeypatch: pytest.MonkeyPatch, mode: str, closer: str, raised: type
):
    if mode == "identity":
        install_writable_espn(monkeypatch, echo_member="{9F8E7D6C-5B4A-3928-1706-F5E4D3C2B1A0}")
    else:
        install_writable_espn(monkeypatch, mode="reject")

    def broken(self: Journal, *_: Any, **__: Any) -> Any:
        raise OSError("database is locked")

    monkeypatch.setattr(Journal, closer, broken)
    with pytest.raises(raised) as caught:
        _set("2003=RB/WR/TE")
    assert "could not be closed (OSError)" in caught.value.details["journal"]
    assert [entry.status for entry in _entries()] == ["pending"]


def test_a_journal_that_cannot_be_written_stops_the_write(espn: WritableEspn, isolated_home: Path):
    state = isolated_home / "state" / "fantasy-sports"
    state.mkdir(parents=True)
    (state / "journal.sqlite").mkdir()  # a directory where the file should be
    with pytest.raises(ConfigInvalidError):
        _set("2003=RB/WR/TE")
    assert espn.posts == []


def test_a_journal_that_cannot_be_completed_is_a_warning_not_a_lost_write(
    espn: WritableEspn, monkeypatch: pytest.MonkeyPatch
):
    def broken(self: Journal, *_: Any, **__: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(Journal, "complete", broken)
    data = _set("2003=RB/WR/TE")["data"]
    assert data["provider_transaction_id"] == TRANSACTION_ID
    assert data["read_back"]["verified"] is True
    assert len(data["warnings"]) == 1
    assert data["journal_entry_id"] in data["warnings"][0]
    assert "pending" in data["warnings"][0]


def test_a_read_back_that_fails_reports_an_unverified_success(
    espn: WritableEspn, monkeypatch: pytest.MonkeyPatch
):
    from fantasy_sports.providers.espn import EspnProvider

    original = EspnProvider.fetch_roster
    calls: list[int] = []

    def flaky(self: EspnProvider, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) > 1:
            raise ProviderUnavailableError("down")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(EspnProvider, "fetch_roster", flaky)
    data = _set("2003=RB/WR/TE")["data"]
    assert data["read_back"] == {
        "verified": False,
        "diverged": [],
        "error": "PROVIDER_UNAVAILABLE",
    }
    assert "could not be re-read" in data["warnings"][0]
    (entry,) = _entries()
    assert entry.status == "executed"


# --------------------------------------------------------------------------- #
# journal reverse
# --------------------------------------------------------------------------- #


def test_reverse_restores_the_prior_lineup_and_journals_a_reverse_entry(espn: WritableEspn):
    original = espn.slots()
    first = _set("2002=BE", "2003=WR")["data"]
    payload = _reverse(first["journal_entry_id"])
    data = payload["data"]

    assert payload["error"] is None
    assert set(data) == SUCCESS_KEYS | {"reverses"}
    assert data["kind"] == "reverse"
    assert data["reverses"] == first["journal_entry_id"]
    assert data["read_back"] == {"verified": True, "diverged": []}
    assert data["items"] == [
        {"player_id": "2003", "from_slot": "WR", "to_slot": "BE"},
        {"player_id": "2002", "from_slot": "BE", "to_slot": "RB"},
    ]
    assert espn.slots() == original
    assert len(espn.posts) == 2

    newest, oldest = _entries()
    assert oldest.id == first["journal_entry_id"] and oldest.status == "executed"
    assert newest.kind == "reverse" and newest.reverses == oldest.id
    assert newest.status == "executed" and newest.reversible is True
    assert newest.prior_state == {"2002": "BE", "2003": "WR"}
    assert newest.applied_state == {"2002": "RB", "2003": "BE"}
    assert newest.command == ("fantasy-sports", "journal", "reverse", "--id", oldest.id)


def test_a_reverse_can_itself_be_reversed(espn: WritableEspn):
    first = _set("2003=RB/WR/TE")["data"]
    undo = _reverse(first["journal_entry_id"])["data"]
    redo = _reverse(undo["journal_entry_id"])["data"]
    assert redo["items"] == [{"player_id": "2003", "from_slot": "BE", "to_slot": "RB/WR/TE"}]
    assert espn.slots()["2003"] == FLEX


def test_reverse_dry_run_sends_nothing(espn: WritableEspn):
    first = _set("2003=RB/WR/TE")["data"]
    data = _reverse(first["journal_entry_id"], dry_run=True)["data"]
    assert data["dry_run"] is True and data["reverses"] == first["journal_entry_id"]
    assert len(espn.posts) == 1
    assert len(_entries()) == 1


def test_reversing_a_lineup_that_already_moved_back_is_an_empty_diff(espn: WritableEspn):
    first = _set("2003=RB/WR/TE")["data"]
    _reverse(first["journal_entry_id"])
    with pytest.raises(ConfigInvalidError, match="already matches"):
        _reverse(first["journal_entry_id"])


def _seed(**overrides: Any) -> JournalEntry:
    fields: dict[str, Any] = {
        "provider": "espn",
        "league_id": "99",
        "season": 2026,
        "command": ["fantasy-sports", "add"],
        "team_id": "1",
        "kind": "add",
        "reversible": False,
        "prior_state": {},
        "request": {"type": "FREEAGENT"},
    }
    fields.update(overrides)
    with Journal.open() as journal:
        return journal.begin(JournalEntry(**fields))


@pytest.mark.parametrize("kind", ["add", "drop", "claim", "cancel"])
def test_reversing_an_irreversible_entry_is_not_available_and_sends_nothing(
    kind: str, monkeypatch: pytest.MonkeyPatch
):
    import requests

    def no_network(*_: Any, **__: Any) -> Any:
        raise AssertionError("nothing may be sent for an irreversible entry")

    monkeypatch.setattr(requests, "get", no_network)
    monkeypatch.setattr(requests, "post", no_network)
    entry = _seed(kind=kind)
    with pytest.raises(NotAvailableError) as caught:
        _reverse(entry.id)
    assert "Nothing was sent" in str(caught.value)
    assert caught.value.remediation and "cannot" in caught.value.remediation
    assert caught.value.details["kind"] == kind
    assert len(_entries()) == 1


@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_reversing_an_entry_that_never_applied_is_refused(status: str, espn: WritableEspn):
    entry = _seed(kind="lineup", reversible=True, prior_state={"2003": "BE"})
    if status == "rejected":
        with Journal.open() as journal:
            journal.reject(entry.id, WriteRejectedError("no", kind="roster_lock"))
    with pytest.raises(ConfigInvalidError) as caught:
        _reverse(entry.id)
    assert caught.value.details["status"] == status
    assert espn.posts == []


def test_reversing_an_unknown_id_names_the_nearest(espn: WritableEspn):
    entry = _seed(kind="lineup", reversible=True, prior_state={"2003": "BE"})
    with pytest.raises(ConfigInvalidError) as caught:
        _reverse(entry.id[:8])
    assert caught.value.details["nearest"] == [entry.id]


def test_reversing_an_entry_with_no_prior_state_is_refused(espn: WritableEspn):
    entry = _seed(kind="lineup", reversible=True, prior_state={})
    with Journal.open() as journal:
        journal.complete(
            entry.id,
            member_id=None,
            provider_transaction_id="x",
            proposed_at=None,
            applied_state={},
        )
    with pytest.raises(ConfigInvalidError, match="no prior lineup"):
        _reverse(entry.id)


def test_reversing_an_entry_for_an_unconfigured_league_is_a_config_failure(
    espn: WritableEspn,
):
    entry = _seed(kind="lineup", reversible=True, prior_state={"2003": "BE"}, league_id="5")
    with Journal.open() as journal:
        journal.complete(
            entry.id,
            member_id=None,
            provider_transaction_id="x",
            proposed_at=None,
            applied_state={"2003": "WR"},
        )
    with pytest.raises(ConfigInvalidError) as caught:
        _reverse(entry.id)
    assert caught.value.kind == "config"
    assert caught.value.details["league_id"] == "5"
    assert espn.posts == []


# --------------------------------------------------------------------------- #
# The plan, in isolation
# --------------------------------------------------------------------------- #


def _slot(pid: str, slot: str, eligible: tuple[str, ...], locked: bool | None = False) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        provider_id=pid,
        slot=slot,
        is_locked=locked,
        player=SimpleNamespace(name=f"P{pid}", eligible_slots=eligible),
    )


def test_a_provider_without_slot_counts_skips_the_count_check():
    from fantasy_sports.commands.lineup import plan_lineup

    roster = [_slot("1", "WR", ("WR", "BE")), _slot("2", "BE", ("WR", "BE"))]
    plan = plan_lineup(roster, {"2": "WR"}, {})
    assert plan.items == ({"player_id": "2", "from_slot": "BE", "to_slot": "WR"},)


def test_an_unlocked_player_with_unknown_lock_state_may_move():
    """``is_locked`` is ``None`` for a player on bye: unknown is not locked."""
    from fantasy_sports.commands.lineup import plan_lineup

    roster = [_slot("1", "BE", ("WR", "BE"), locked=None)]
    assert plan_lineup(roster, {"1": "WR"}, {"WR": 1}).items


def test_vacates_come_before_fills_whatever_order_they_were_named():
    from fantasy_sports.commands.lineup import plan_lineup

    roster = [_slot("1", "WR", ("WR", "BE")), _slot("2", "BE", ("WR", "BE"))]
    plan = plan_lineup(roster, {"2": "WR", "1": "BE"}, {"WR": 1, "BE": 1})
    assert [item["to_slot"] for item in plan.items] == ["BE", "WR"]
    assert plan.prior_state == {"2": "BE", "1": "WR"}


def test_a_full_slot_with_a_locked_occupant_says_so():
    from fantasy_sports.commands.lineup import plan_lineup

    roster = [_slot("1", "WR", ("WR", "BE"), locked=True), _slot("2", "BE", ("WR", "BE"))]
    with pytest.raises(ConfigInvalidError) as caught:
        plan_lineup(roster, {"2": "WR"}, {"WR": 1, "BE": 1})
    assert caught.value.details["locked"] == ["1"]
    assert "Locked in that slot already: 1" in str(caught.value)


def test_a_player_missing_from_the_roster_is_refused_by_the_plan():
    from fantasy_sports.commands.lineup import plan_lineup

    with pytest.raises(ConfigInvalidError) as caught:
        plan_lineup([_slot("1", "WR", ("WR",))], {"9": "WR"}, {})
    assert caught.value.details["player_id"] == "9"


def test_an_empty_set_is_refused():
    from fantasy_sports.commands.lineup import parse_assignments

    with pytest.raises(ConfigInvalidError, match="names no players"):
        parse_assignments([], [])


def test_dry_run_without_a_credential_report_says_the_age_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
):
    """A provider that cannot report cookie age still yields the block's shape."""
    from _fake_provider import WritingFake

    from fantasy_sports.commands import lineup as lineup_command

    class Ctx:
        provider = WritingFake()
        target = ("99", 2026)
        league_id = "99"
        season = 2026

    monkeypatch.setattr(
        WritingFake,
        "fetch_league",
        lambda self, *a: type("L", (), {"roster_slots": {}, "current_week": 1})(),
        raising=False,
    )
    data = lineup_command.execute_lineup(
        Ctx(),  # type: ignore[arg-type]
        "1",
        {"2": "WR"},
        roster=[_slot("2", "BE", ("WR", "BE"))],
        command=["x"],
        dry_run=True,
        kind="lineup",
    )
    assert data["credentials"] == {"espn_s2_age_seconds": None, "warning": "credential age unknown"}
