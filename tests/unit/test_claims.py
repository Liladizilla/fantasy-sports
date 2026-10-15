"""``claims`` and ``cancel-claim``, offline (jwulff/fantasy-sports#109).

The adapter half parses ``view=mPendingTransactions`` from brief 06's own
capture (``pending-transactions.json``); the command half runs the real
commands against the real ESPN adapter and :class:`_harness.WritableEspn`,
which queues a ``WAIVER`` body where that view lists it and answers a
``CANCEL`` the way p5 was captured: a new id, ``CANCELED``, the claim's items
echoed, and ``relatedTransactionId``. No test sends a live write.

Team 1 is the session's; team 2 is another member's. 3002 Radia Perlman is
the one pool player on waivers.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _harness import (
    ADD_DROP_CAPTURES,
    CANCEL_TRANSACTION_ID,
    FAKE_SWID,
    PROPOSED_DATE_MS,
    TRANSACTION_ID,
    WritableEspn,
    install_writable_espn,
    isolate_home,
)

from fantasy_sports.cache.tags import Resource
from fantasy_sports.commands import REGISTRY, DataShape
from fantasy_sports.core.errors import (
    ConfigInvalidError,
    ProviderUnavailableError,
    SchemaDriftError,
    WriteDivergedError,
    WriteRejectedError,
)
from fantasy_sports.core.journal import Journal, JournalEntry
from fantasy_sports.core.models import PendingClaim
from fantasy_sports.core.redaction import forget_secrets
from fantasy_sports.output.errors import exit_code_for

WHY = "Perlman is the best TE on waivers this week (sanity-gate: PROCEED)"
OTHER_MEMBER = "{9F8E7D6C-5B4A-3928-1706-F5E4D3C2B1A0}"

CANCEL_KEYS = {
    "team_id",
    "kind",
    "provider_transaction_id",
    "cancelled_claim_id",
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

CLAIM_KEYS = {
    "provider",
    "provider_id",
    "team_id",
    "players_in",
    "players_out",
    "bid_amount",
    "proposed_at",
    "processes_at",
    "status",
    "sub_order",
    "raw",
}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield isolate_home(tmp_path, monkeypatch)
    forget_secrets()


@pytest.fixture
def espn(monkeypatch: pytest.MonkeyPatch) -> WritableEspn:
    return install_writable_espn(monkeypatch)


def _run(command: str, **kwargs: Any) -> dict[str, Any]:
    return REGISTRY[command].resolve()(**kwargs).to_dict()


def _claim(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("team", "1")
    kwargs.setdefault("player", "Radia Perlman")
    kwargs.setdefault("acknowledge_irreversible", WHY)
    return _run("claim", **kwargs)["data"]


def _claims(**kwargs: Any) -> list[dict[str, Any]]:
    return _run("claims", **kwargs)["data"]


def _cancel(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("team", "1")
    kwargs.setdefault("id", TRANSACTION_ID)
    return _run("cancel-claim", **kwargs)["data"]


def _entries() -> list[JournalEntry]:
    with Journal.open() as journal:
        return journal.list(limit=50)


def _journal_file(home: Path) -> Path:
    return home / "state" / "fantasy-sports" / "journal.sqlite"


def _queued(claim_id: str, team: int, *, proposed: int = PROPOSED_DATE_MS) -> dict[str, Any]:
    """A pending claim another invocation (or another team) queued."""
    return {
        "id": claim_id,
        "teamId": team,
        "type": "WAIVER",
        "status": "PENDING",
        "isPending": True,
        "scoringPeriodId": 2,
        "proposedDate": proposed,
        "bidAmount": 0,
        "items": [{"playerId": 3002, "type": "ADD"}],
    }


def _capture() -> dict[str, Any]:
    return json.loads((ADD_DROP_CAPTURES / "pending-transactions.json").read_text("utf-8"))


# --------------------------------------------------------------------------- #
# The adapter: ESPN's queue, parsed from the capture
# --------------------------------------------------------------------------- #


def test_the_captured_queue_row_becomes_a_pending_claim():
    """Brief 06's p7c claim, as ``mPendingTransactions`` listed it while live."""
    from fantasy_sports.providers.espn import _pending_claims

    rows = _capture()["snapshots"]["after-p7c-mPendingTransactions"]["team_7_rows"]
    (claim,) = _pending_claims({"pendingTransactions": rows}, team_id=None)
    assert claim.provider == "espn"
    assert claim.provider_id == "<uuid-3>"
    assert claim.team_id == "7"
    assert claim.players_in == ("9000030",) and claim.players_out == ("9000023",)
    assert claim.bid_amount == 0, "ESPN records a bidless claim as 0"
    assert claim.proposed_at == datetime.fromtimestamp(1792046096566 / 1000, tz=UTC)
    assert claim.processes_at == datetime.fromtimestamp(1792134000000 / 1000, tz=UTC)
    assert claim.status == "PENDING"
    assert claim.sub_order is None, "the queue view carries no subOrder"
    assert claim.raw == rows[0]


def test_the_captured_empty_queues_are_empty():
    from fantasy_sports.providers.espn import _pending_claims

    for name in ("after-p5-mPendingTransactions", "after-p7c-r-mPendingTransactions"):
        rows = _capture()["snapshots"][name]["team_7_rows"]
        assert _pending_claims({"pendingTransactions": rows}, team_id="7") == []


def test_an_empty_queue_is_an_empty_list_and_an_absent_one_is_drift():
    """ESPN sends ``pendingTransactions: []`` for an empty queue (seen live in
    the test league), so a body without the key is not "nothing queued"."""
    from fantasy_sports.providers.espn import _pending_claims

    assert _pending_claims({"pendingTransactions": []}, team_id=None) == []
    with pytest.raises(SchemaDriftError, match="has no pendingTransactions") as caught:
        _pending_claims({"id": 99}, team_id=None)
    assert caught.value.details["path"] == ["mPendingTransactions", "pendingTransactions"]


def test_only_pending_waiver_rows_are_claims_and_team_narrows():
    from fantasy_sports.providers.espn import _pending_claims

    rows = [
        _queued("a", 1),
        _queued("b", 2),
        {**_queued("c", 1), "status": "CANCELED"},
        {**_queued("d", 1), "type": "TRADE_PROPOSAL"},
        # Not a claim, so not validated: a row nobody reads is not drift.
        {"type": "TRADE_PROPOSAL", "status": "PENDING"},
    ]
    payload = {"pendingTransactions": rows}
    assert [c.provider_id for c in _pending_claims(payload, team_id=None)] == ["a", "b"]
    assert [c.provider_id for c in _pending_claims(payload, team_id="2")] == ["b"]


@pytest.mark.parametrize(
    ("payload", "path"),
    [
        ([], ["mPendingTransactions"]),
        ({"pendingTransactions": {}}, ["mPendingTransactions", "pendingTransactions"]),
        ({"pendingTransactions": ["x"]}, ["mPendingTransactions", "pendingTransactions[0]"]),
        (
            {"pendingTransactions": [{**_queued("a", 1), "id": None}]},
            ["mPendingTransactions", "pendingTransactions[0].id"],
        ),
        (
            {"pendingTransactions": [{**_queued("a", 1), "teamId": "seven"}]},
            ["mPendingTransactions", "pendingTransactions[0].teamId"],
        ),
        (
            {"pendingTransactions": [_queued("a", 1), {**_queued("b", 1), "items": None}]},
            ["mPendingTransactions", "pendingTransactions[1].items"],
        ),
    ],
)
def test_a_queue_of_another_shape_is_drift_with_a_path(payload: Any, path: list[str]):
    from fantasy_sports.providers.espn import _pending_claims

    with pytest.raises(SchemaDriftError) as caught:
        _pending_claims(payload, team_id=None)
    assert caught.value.details["path"] == path
    assert "raw --view mPendingTransactions" in (caught.value.remediation or "")


def test_the_adapter_reads_the_queue_view_through_the_transactions_ttl(espn: WritableEspn):
    from fantasy_sports.providers.espn import _VIEW_RESOURCES, EspnProvider

    assert _VIEW_RESOURCES["mPendingTransactions"] is Resource.TRANSACTIONS
    espn.pending.append(_queued("q", 1))
    espn._publish(espn.bootstrap)
    provider = EspnProvider({"espn_s2": "AEB" + "q" * 200, "swid": FAKE_SWID})
    (claim,) = provider.fetch_pending_claims("99", 2026, "1")
    assert claim.provider_id == "q"
    assert "mPendingTransactions" in espn.views()
    assert provider.fetch_pending_claims("99", 2026, "2") == []


def test_the_pending_claim_model_is_frozen_and_built_from_a_payload():
    import dataclasses

    claim = PendingClaim.from_payload(
        {"provider": "espn", "provider_id": "x", "team_id": "1", "status": "PENDING", "raw": {}}
    )
    assert claim.players_in == () and claim.bid_amount is None
    assert set(claim.to_dict()) == CLAIM_KEYS
    with pytest.raises(dataclasses.FrozenInstanceError):
        claim.status = "EXECUTED"  # type: ignore[misc]
    with pytest.raises(SchemaDriftError):
        PendingClaim.from_payload({"provider": "espn", "provider_id": "x"})


def test_the_writing_fake_serves_its_queue_by_team():
    from _fake_provider import WritingFake

    queued = [
        PendingClaim(provider="fake", provider_id=n, team_id=t, status="PENDING", raw={})
        for n, t in (("a", "1"), ("b", "2"))
    ]
    fake = WritingFake(pending_claims=queued)
    assert [c.provider_id for c in fake.fetch_pending_claims("99", 2026, "2")] == ["b"]
    assert len(fake.fetch_pending_claims("99", 2026)) == 2


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "shape", "params"),
    [
        ("claims", DataShape.COLLECTION, ["team"]),
        ("cancel-claim", DataShape.OBJECT, ["team", "id", "dry_run"]),
    ],
)
def test_both_are_top_level_commands_that_take_a_league(command, shape, params):
    spec = REGISTRY[command]
    assert spec.group is None and spec.shape is shape and spec.takes_league
    assert [p.name for p in spec.params] == params


def test_claim_and_cancel_claim_both_parse_on_the_cli(espn: WritableEspn):
    """``cancel-claim`` rather than a ``claim cancel`` group, which would shadow
    ``claim`` itself so that ``claim --player`` stopped parsing."""
    from typer.testing import CliRunner

    from fantasy_sports.cli.app import build_app

    cli = CliRunner()
    claim = ["claim", "--team", "1", "--player", "3002", "--acknowledge-irreversible", WHY]
    assert cli.invoke(build_app(), [*claim, "-o", "json"]).exit_code == 0
    listed = cli.invoke(build_app(), ["claims", "-o", "json"])
    assert listed.exit_code == 0, listed.stderr
    assert [c["provider_id"] for c in json.loads(listed.stdout)["data"]] == [TRANSACTION_ID]
    cancel = ["cancel-claim", "--team", "1", "--id", TRANSACTION_ID, "-o", "json"]
    result = cli.invoke(build_app(), cancel)
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["data"]["kind"] == "cancel"


# --------------------------------------------------------------------------- #
# claims
# --------------------------------------------------------------------------- #


def test_claims_lists_the_sessions_own_queue_by_default(espn: WritableEspn):
    assert _claims() == []
    _claim(bid=7, drop="2003")
    (claim,) = _claims()
    assert set(claim) == CLAIM_KEYS
    assert claim["provider_id"] == TRANSACTION_ID
    assert claim["team_id"] == "1"
    assert claim["players_in"] == ["3002"] and claim["players_out"] == ["2003"]
    assert claim["bid_amount"] == 7
    assert claim["status"] == "PENDING"
    assert claim["proposed_at"] == "2026-09-10T12:00:01Z"  # PROPOSED_DATE_MS
    assert claim["processes_at"] == "2026-09-13T09:00:00Z"  # PROCESS_DATE_MS


def test_claims_are_newest_first_with_undated_ones_last(espn: WritableEspn):
    espn.pending += [
        _queued("old", 1, proposed=PROPOSED_DATE_MS - 60_000),
        _queued("new", 1, proposed=PROPOSED_DATE_MS + 60_000),
        {**_queued("undated", 1), "proposedDate": None},
        _queued("mid", 1),
    ]
    espn._publish(espn.bootstrap)
    assert [c["provider_id"] for c in _claims()] == ["new", "mid", "old", "undated"]


def test_claims_reads_another_teams_queue_when_asked(espn: WritableEspn):
    """A read ESPN allows any member, and useful to an agent planning around it."""
    espn.pending += [_queued("mine", 1), _queued("theirs", 2)]
    espn._publish(espn.bootstrap)
    assert [c["provider_id"] for c in _claims(team="Team Bravo")] == ["theirs"]
    assert [c["provider_id"] for c in _claims(team="1")] == ["mine"]


def test_claims_without_team_needs_a_team_the_session_owns(monkeypatch: pytest.MonkeyPatch):
    install_writable_espn(monkeypatch, swid=OTHER_MEMBER)
    with pytest.raises(ConfigInvalidError) as caught:
        _claims()
    assert caught.value.details["kind"] == "argument"
    assert caught.value.details["owned_team_ids"] == []
    assert "--team" in (caught.value.remediation or "")


def test_claims_without_team_is_ambiguous_for_a_session_owning_two(espn: WritableEspn):
    espn.overrides["mNav"]["teams"][1]["owners"] = [FAKE_SWID]
    with pytest.raises(ConfigInvalidError) as caught:
        _claims()
    assert caught.value.details["owned_team_ids"] == ["1", "2"]
    assert "1, 2" in (caught.value.remediation or "")


# --------------------------------------------------------------------------- #
# cancel-claim: the happy path
# --------------------------------------------------------------------------- #


def test_a_cancel_withdraws_the_claim_journals_both_ids_and_reads_back(espn: WritableEspn):
    claimed = _claim(bid=7)
    assert claimed["provider_transaction_id"] == TRANSACTION_ID

    data = _cancel()
    assert set(data) == CANCEL_KEYS
    assert data["kind"] == "cancel"
    assert data["provider_transaction_id"] == CANCEL_TRANSACTION_ID, "the cancel's own id"
    assert data["cancelled_claim_id"] == TRANSACTION_ID
    assert data["status"] == "CANCELED" and data["is_pending"] is False
    assert data["reversible"] is False
    assert data["acknowledged_irreversible"] is None
    assert data["notified"] is None
    assert data["cost"] == {"bid_amount": None, "displaced_player_id": None}
    assert data["items"] == [
        {"player_id": "3002", "type": "add", "from_slot": None, "to_slot": None}
    ]
    assert data["read_back"] == {
        "verified": True,
        "diverged": [],
        "matched_by": ["provider_transaction_id"],
        "queue_size": 0,
    }
    assert data["dry_run"] is False and "warnings" not in data

    body = espn.posts[-1]
    assert body == {
        "isLeagueManager": False,
        "teamId": 1,
        "type": "WAIVER",
        "scoringPeriodId": body["scoringPeriodId"],
        "executionType": "CANCEL",
        "relatedTransactionId": TRANSACTION_ID,
    }
    assert _claims() == []

    cancel, claim = _entries()
    assert cancel.kind == "cancel" and cancel.status == "executed"
    assert cancel.reversible is False
    assert cancel.provider_transaction_id == CANCEL_TRANSACTION_ID
    assert cancel.prior_state["claim"]["provider_transaction_id"] == TRANSACTION_ID
    assert cancel.prior_state["claim"]["bid_amount"] == 7
    assert cancel.request == {"team_id": "1", "cancelled_claim_id": TRANSACTION_ID}
    assert cancel.applied_state is not None
    assert cancel.applied_state["cancelled_claim_id"] == TRANSACTION_ID
    assert cancel.applied_state["related_transaction_id"] == TRANSACTION_ID
    assert cancel.applied_state["read_back"]["verified"] is True
    assert cancel.command == (
        "fantasy-sports",
        "cancel-claim",
        "--team",
        "1",
        "--id",
        TRANSACTION_ID,
    )
    assert claim.kind == "claim" and claim.provider_transaction_id == TRANSACTION_ID


def test_after_a_cancel_the_transaction_log_still_reads_the_claim_pending(espn: WritableEspn):
    """Why the queue is the source: ``transactions`` shows the ghost (brief 06 §2)."""
    _claim()
    _cancel()
    statuses = sorted(row["status"] for row in espn.overrides["mTransactions2"]["transactions"])
    assert statuses == ["CANCELED", "PENDING"]
    assert _claims() == []


def test_a_cancel_needs_no_acknowledgement_and_is_never_announced(
    espn: WritableEspn, isolated_home: Path
):
    told = isolated_home / "told.json"
    config = isolated_home / "config" / "fantasy-sports" / "config.toml"
    config.write_text(
        config.read_text("utf-8") + f"\n[notify]\nirreversible_command = \"cat > '{told}'\"\n",
        encoding="utf-8",
    )
    _claim()
    told.unlink()  # the claim itself was announced
    data = _cancel()
    assert data["notified"] is None
    assert not told.exists()


def test_cancel_takes_a_team_by_name(espn: WritableEspn):
    _claim()
    assert _cancel(team="Team Alpha")["team_id"] == "1"


def test_a_dry_run_checks_the_queue_and_sends_and_journals_nothing(
    espn: WritableEspn, isolated_home: Path
):
    _claim()
    _journal_file(isolated_home).unlink()
    data = _cancel(dry_run=True)
    assert data["dry_run"] is True
    assert data["provider_transaction_id"] is None and data["journal_entry_id"] is None
    assert data["cancelled_claim_id"] == TRANSACTION_ID
    assert set(data["credentials"]) == {"espn_s2_age_seconds", "warning"}
    assert "nothing was sent" in data["note"]
    assert len(espn.posts) == 1, "only the claim"
    assert not _journal_file(isolated_home).exists()
    assert [c["provider_id"] for c in _claims()] == [TRANSACTION_ID]


# --------------------------------------------------------------------------- #
# cancel-claim: refused before anything is sent
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("queued", "wanted"),
    [
        ([], TRANSACTION_ID),  # nothing queued: processed, already cancelled, or a typo
        ([_queued("theirs", 2)], "theirs"),  # another team's claim
        ([_queued("mine", 1)], "nonesuch"),
    ],
)
def test_an_id_not_in_this_teams_queue_is_refused_and_never_sent(
    espn: WritableEspn, isolated_home: Path, queued: list[dict[str, Any]], wanted: str
):
    espn.pending += queued
    espn._publish(espn.bootstrap)
    with pytest.raises(ConfigInvalidError) as caught:
        _cancel(id=wanted)
    error = caught.value
    assert error.details["kind"] == "argument"
    assert error.details["id"] == wanted and error.details["team_id"] == "1"
    assert error.details["pending_ids"] == [q["id"] for q in queued if q["teamId"] == 1]
    assert "fantasy-sports claims" in (error.remediation or "")
    assert exit_code_for(error) == 6
    assert espn.posts == []
    assert not _journal_file(isolated_home).exists()


def test_an_already_cancelled_claim_cannot_be_cancelled_twice(espn: WritableEspn):
    _claim()
    _cancel()
    with pytest.raises(ConfigInvalidError, match="nothing to cancel"):
        _cancel()
    assert [body["executionType"] for body in espn.posts] == ["EXECUTE", "CANCEL"]


def test_a_team_the_session_does_not_own_is_refused_before_the_queue_is_read(
    espn: WritableEspn, isolated_home: Path
):
    espn.pending.append(_queued("theirs", 2))
    espn._publish(espn.bootstrap)
    with pytest.raises(WriteRejectedError) as caught:
        _cancel(team="2", id="theirs")
    assert caught.value.kind == "not_owned"
    assert "mPendingTransactions" not in espn.views()
    assert espn.posts == []
    assert not _journal_file(isolated_home).exists()


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_id_is_refused_before_any_read(espn: WritableEspn, blank: str):
    with pytest.raises(ConfigInvalidError, match="--id is empty"):
        _cancel(id=blank)
    assert espn.calls == []


# --------------------------------------------------------------------------- #
# cancel-claim: what ESPN says back
# --------------------------------------------------------------------------- #


def test_a_rejected_cancel_is_journaled_rejected(monkeypatch: pytest.MonkeyPatch):
    espn = install_writable_espn(monkeypatch)
    _claim()
    espn.mode, espn.reject_type = "reject", "TRAN_LINEUP_LOCKED"
    with pytest.raises(WriteRejectedError):
        _cancel()
    cancel, _ = _entries()
    assert cancel.kind == "cancel" and cancel.status == "rejected"


def test_a_cancel_that_times_out_is_never_retried_and_stays_pending(
    espn: WritableEspn, monkeypatch: pytest.MonkeyPatch
):
    import requests

    _claim()
    attempts: list[Any] = []

    def timeout(*_: Any, **kwargs: Any) -> Any:
        attempts.append(kwargs.get("json"))
        raise requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setattr(requests, "post", timeout)
    with pytest.raises(ProviderUnavailableError) as caught:
        _cancel()
    assert len(attempts) == 1
    assert caught.value.details["transaction_state"] == "unknown"
    cancel, _ = _entries()
    assert cancel.status == "pending"


def test_a_cancel_applied_as_another_account_is_journaled_diverged(espn: WritableEspn):
    _claim()
    espn.echo_member = OTHER_MEMBER
    with pytest.raises(WriteDivergedError) as caught:
        _cancel()
    assert caught.value.details["transaction_state"] == "applied"
    assert "notified" not in caught.value.details
    cancel, _ = _entries()
    assert cancel.status == "diverged"


def test_a_claim_still_listed_after_the_cancel_is_unverified_with_a_warning(
    espn: WritableEspn,
):
    _claim()
    espn.mode = "ignore"  # 200 CANCELED, and the queue does not change
    data = _cancel()
    assert data["status"] == "CANCELED"
    assert data["read_back"] == {
        "verified": False,
        "diverged": [TRANSACTION_ID],
        "matched_by": ["provider_transaction_id"],
        "queue_size": 1,
    }
    assert "still lists claim" in data["warnings"][0]
    cancel, _ = _entries()
    assert cancel.status == "executed"
    assert cancel.applied_state is not None
    assert cancel.applied_state["read_back"]["verified"] is False


def test_a_queue_that_cannot_be_read_back_is_unverified_with_a_warning(
    espn: WritableEspn, monkeypatch: pytest.MonkeyPatch
):
    from fantasy_sports.providers.espn import EspnProvider

    _claim()
    original = EspnProvider.fetch_pending_claims
    calls: list[int] = []

    def flaky(self: EspnProvider, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) > 1:
            raise ProviderUnavailableError("down")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(EspnProvider, "fetch_pending_claims", flaky)
    data = _cancel()
    assert data["read_back"] == {
        "verified": False,
        "diverged": [],
        "error": "PROVIDER_UNAVAILABLE",
    }
    assert "could not be read back" in data["warnings"][0]


def test_a_journal_that_cannot_be_completed_is_a_warning(
    espn: WritableEspn, monkeypatch: pytest.MonkeyPatch
):
    _claim()

    def broken(self: Journal, *_: Any, **__: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(Journal, "complete", broken)
    data = _cancel()
    assert data["provider_transaction_id"] == CANCEL_TRANSACTION_ID
    assert "stays 'pending'" in data["warnings"][0]


def test_a_claim_journal_entry_points_at_cancel_claim(espn: WritableEspn):
    from fantasy_sports.core.errors import NotAvailableError

    entry = _claim()["journal_entry_id"]
    with pytest.raises(NotAvailableError) as caught:
        _run("journal reverse", id=entry)
    assert "fantasy-sports cancel-claim" in (caught.value.remediation or "")
