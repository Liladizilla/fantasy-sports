"""The mutation journal store (jwulff/fantasy-sports#16, store half).

Everything here runs against a temp directory: nothing touches the real
``~/.local/state``. The tests that matter most are the ones that read the
SQLite file's **bytes** back — a redaction test that only looks at what the API
returns proves the API redacts, not that the file is clean.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from _harness import FAKE_S2, FAKE_SWID, isolate_home

from fantasy_sports.core.errors import ConfigInvalidError, RateLimitedError
from fantasy_sports.core.journal import (
    DIR_MODE,
    FILE_MODE,
    Journal,
    JournalEntry,
    journal_path,
)
from fantasy_sports.core.redaction import forget_secrets, remember_secret

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")

#: A second GUID, distinct from the harness's, so "member" and "stray" differ.
STRAY_GUID = "{AAAABBBB-CCCC-DDDD-EEEE-FFFF00001111}"
MEMBER_GUID = "{5C6D7E8F-1A2B-4C3D-8E9F-0A1B2C3D4E5F}"


@pytest.fixture(autouse=True)
def _clean_secrets():
    yield
    forget_secrets()


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "fantasy-sports" / "journal.sqlite"


def make(**overrides: Any) -> JournalEntry:
    base: dict[str, Any] = {
        "provider": "espn",
        "league_id": "99",
        "season": 2026,
        "scoring_period": 1,
        "command": ["fantasy-sports", "lineup", "set", "--team", "1"],
        "role": "creator",
        "team_id": "1",
        "kind": "lineup",
        "reversible": True,
        "prior_state": {"3916387": 0, "4262921": 20},
        "request": {"type": "LINEUP", "items": [{"playerId": 3916387, "from": 0, "to": 20}]},
    }
    base.update(overrides)
    return JournalEntry(**base)


# --------------------------------------------------------------------------- #
# Where it lives, and who can read it
# --------------------------------------------------------------------------- #


def test_default_path_is_under_the_xdg_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    isolate_home(tmp_path, monkeypatch)
    assert journal_path() == tmp_path / "state" / "fantasy-sports" / "journal.sqlite"
    assert Journal.open().path == journal_path()


def test_default_path_falls_back_to_dot_local_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    isolate_home(tmp_path, monkeypatch)
    monkeypatch.delenv("XDG_STATE_HOME")
    assert journal_path() == tmp_path / ".local" / "state" / "fantasy-sports" / "journal.sqlite"


@posix_only
def test_file_is_0600_and_directory_is_0700(path: Path):
    with Journal.open(path) as journal:
        journal.begin(make())
    assert stat.S_IMODE(path.stat().st_mode) == FILE_MODE == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == DIR_MODE == 0o700


@posix_only
def test_modes_hold_under_a_permissive_umask(path: Path):
    previous = os.umask(0o000)
    try:
        with Journal.open(path) as journal:
            journal.begin(make())
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@posix_only
def test_an_existing_loose_directory_and_file_are_tightened(path: Path):
    path.parent.mkdir(parents=True)
    path.parent.chmod(0o755)
    path.touch()
    path.chmod(0o644)
    with Journal.open(path) as journal:
        journal.begin(make())
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_constructing_and_reading_creates_nothing(path: Path):
    journal = Journal.open(path)
    assert journal.list() == []
    assert journal.count() == 0
    assert journal.pending() == []
    assert journal.get("anything") is None
    assert journal.nearest_ids("anything") == []
    assert not path.parent.exists()


# --------------------------------------------------------------------------- #
# The entry
# --------------------------------------------------------------------------- #


def test_entry_defaults_mint_an_id_and_a_utc_timestamp():
    entry = make()
    assert len(entry.id) == 36
    assert entry.recorded_at.tzinfo is not None
    assert entry.status == "pending"
    assert entry.cost == {"bid_amount": None, "displaced_player_id": None}
    assert make().id != entry.id


def test_to_dict_is_json_plain_with_utc_timestamps_in_the_documented_order():
    plain = make(proposed_at=datetime(2026, 9, 12, 18, 31, 2, 123000, tzinfo=UTC)).to_dict()
    assert json.loads(json.dumps(plain)) == plain
    assert list(plain) == [
        "id",
        "recorded_at",
        "provider",
        "league_id",
        "season",
        "scoring_period",
        "command",
        "member_id",
        "role",
        "team_id",
        "kind",
        "reversible",
        "prior_state",
        "applied_state",
        "request",
        "provider_transaction_id",
        "proposed_at",
        "cost",
        "status",
        "reverses",
        "error",
    ]
    assert plain["proposed_at"] == "2026-09-12T18:31:02.123Z"
    assert plain["recorded_at"].endswith("Z")
    assert plain["command"] == ["fantasy-sports", "lineup", "set", "--team", "1"]


def test_a_timestamp_in_another_zone_is_stored_as_utc():
    pacific = timezone(timedelta(hours=-7))
    entry = make(recorded_at=datetime(2026, 9, 12, 11, 0, tzinfo=pacific))
    assert entry.to_dict()["recorded_at"] == "2026-09-12T18:00:00.000Z"


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "trade"},
        {"role": "owner"},
        {"status": "done"},
        {"command": "fantasy-sports lineup set"},
        {"recorded_at": datetime(2026, 9, 12, 18, 0)},
        {"proposed_at": datetime(2026, 9, 12, 18, 0)},
    ],
    ids=["kind", "role", "status", "command-string", "naive-recorded", "naive-proposed"],
)
def test_a_malformed_entry_is_a_programming_error(overrides: dict[str, Any]):
    with pytest.raises(ValueError):
        make(**overrides)


def test_prior_state_request_and_reversible_cannot_be_forgotten():
    with pytest.raises(TypeError):
        JournalEntry(provider="espn", league_id="99", season=2026, kind="add", command=["x"])


# --------------------------------------------------------------------------- #
# Two-phase write
# --------------------------------------------------------------------------- #


def test_begin_writes_a_pending_row_with_prior_state_before_anything_is_sent(path: Path):
    with Journal.open(path) as journal:
        stored = journal.begin(make())
    reopened = Journal.open(path)
    row = reopened.get(stored.id)
    assert row is not None
    assert row.status == "pending"
    assert row.prior_state == {"3916387": 0, "4262921": 20}
    assert row.request["type"] == "LINEUP"
    assert row.applied_state is None and row.provider_transaction_id is None
    assert row.to_dict() == stored.to_dict()


def test_a_begin_without_a_completion_is_visible_as_pending(path: Path):
    journal = Journal.open(path)
    crashed = journal.begin(make())
    done = journal.begin(make())
    journal.complete(
        done.id,
        member_id=MEMBER_GUID,
        provider_transaction_id="tx-1",
        proposed_at=datetime(2026, 9, 12, 18, 0, tzinfo=UTC),
        applied_state={"3916387": 20},
    )
    assert [e.id for e in journal.pending()] == [crashed.id]
    assert journal.count() == 2
    assert journal.count(status="pending") == 1
    assert journal.count(status="executed") == 1


def test_begin_discards_result_fields_it_was_handed(path: Path):
    journal = Journal.open(path)
    stored = journal.begin(
        make(
            status="executed",
            applied_state={"x": 1},
            provider_transaction_id="tx-early",
            proposed_at=datetime(2026, 9, 12, tzinfo=UTC),
            error={"code": "NOPE"},
        )
    )
    assert stored.status == "pending"
    assert stored.applied_state is None
    assert stored.provider_transaction_id is None
    assert stored.proposed_at is None
    assert stored.error is None


def test_complete_records_espns_ids_and_the_member_espn_echoed(path: Path):
    journal = Journal.open(path)
    entry = journal.begin(make(member_id=None))
    proposed = datetime.fromtimestamp(1789237862123 / 1000, tz=UTC)
    closed = journal.complete(
        entry.id,
        member_id=MEMBER_GUID,
        provider_transaction_id="b4e1c2d0-0000-4000-8000-000000000001",
        proposed_at=proposed,
        applied_state={"3916387": 20, "4262921": 0},
    )
    assert closed.status == "executed"
    assert closed.provider_transaction_id == "b4e1c2d0-0000-4000-8000-000000000001"
    assert closed.proposed_at == proposed
    assert closed.applied_state == {"3916387": 20, "4262921": 0}
    assert closed.member_id is not None and closed.member_id != MEMBER_GUID
    assert Journal.open(path).get(entry.id) == closed


def test_a_diverged_completion_keeps_espns_ids_and_the_divergence(path: Path):
    from fantasy_sports.core.errors import WriteDivergedError

    journal = Journal.open(path)
    entry = journal.begin(make())
    error = WriteDivergedError("read back differently", expected={"1": 0}, observed={"1": 20})
    closed = journal.complete(
        entry.id,
        member_id=MEMBER_GUID,
        provider_transaction_id="b4e1c2d0-0000-4000-8000-000000000002",
        proposed_at=None,
        applied_state={"1": 20},
        status="diverged",
        error=error,
    )
    assert closed.status == "diverged"
    assert closed.provider_transaction_id == "b4e1c2d0-0000-4000-8000-000000000002"
    assert closed.error == error.to_dict()


def test_an_executed_completion_cannot_carry_an_error(path: Path):
    from fantasy_sports.core.errors import WriteDivergedError

    journal = Journal.open(path)
    entry = journal.begin(make())
    with pytest.raises(ValueError, match="only a 'diverged' entry"):
        journal.complete(
            entry.id,
            member_id=None,
            provider_transaction_id=None,
            proposed_at=None,
            applied_state=None,
            error=WriteDivergedError("x"),
        )
    assert journal.get(entry.id).status == "pending"


def test_the_same_member_gets_the_same_attribution_every_time(path: Path):
    journal = Journal.open(path)
    ids = []
    for guid in (MEMBER_GUID, MEMBER_GUID.strip("{}"), STRAY_GUID):
        entry = journal.begin(make())
        ids.append(
            journal.complete(
                entry.id,
                member_id=guid,
                provider_transaction_id=None,
                proposed_at=None,
                applied_state=None,
            ).member_id
        )
    assert ids[0] == ids[1] != ids[2]


def test_a_seeded_salt_makes_pseudonyms_reproducible_and_an_existing_salt_wins(tmp_path: Path):
    def attribution(where: Path, salt: str | None) -> str | None:
        journal = Journal.open(where, swid_salt=salt)
        entry = journal.begin(make())
        return journal.complete(
            entry.id,
            member_id=MEMBER_GUID,
            provider_transaction_id=None,
            proposed_at=None,
            applied_state=None,
        ).member_id

    first = attribution(tmp_path / "a.sqlite", "fixture-salt")
    assert first == attribution(tmp_path / "b.sqlite", "fixture-salt")
    assert first != attribution(tmp_path / "c.sqlite", None)
    # A file that already has a salt keeps it, whatever a later opener passes.
    assert first == attribution(tmp_path / "a.sqlite", "a-different-salt")


def test_a_guid_in_a_request_agrees_with_the_member_pseudonym(path: Path):
    """A body that echoes ``memberId`` joins to the ``member_id`` column."""
    journal = Journal.open(path)
    entry = journal.begin(make(request={"memberId": MEMBER_GUID}))
    closed = journal.complete(
        entry.id,
        member_id=MEMBER_GUID,
        provider_transaction_id=None,
        proposed_at=None,
        applied_state=None,
    )
    assert closed.request["memberId"] == closed.member_id


def test_a_member_id_that_is_not_a_guid_is_kept_but_still_redacted(path: Path):
    remember_secret("opaque-member-token-0123456789")
    journal = Journal.open(path)
    entry = journal.begin(make())
    closed = journal.complete(
        entry.id,
        member_id="opaque-member-token-0123456789",
        provider_transaction_id=None,
        proposed_at=None,
        applied_state=None,
    )
    assert closed.member_id == "***redacted***"


def test_reject_records_the_error_and_nothing_applied(path: Path):
    journal = Journal.open(path)
    entry = journal.begin(make())
    error = RateLimitedError("Slow down.", retry_after=30)
    closed = journal.reject(entry.id, error)
    assert closed.status == "rejected"
    assert closed.applied_state is None
    assert closed.error is not None
    assert closed.error["code"] == "RATE_LIMITED"
    assert closed.error["message"] == "Slow down."


def test_diverge_records_what_was_observed_and_why(path: Path):
    journal = Journal.open(path)
    entry = journal.begin(make())
    closed = journal.diverge(entry.id, {"3916387": 0}, ConfigInvalidError("Read-back differs."))
    assert closed.status == "diverged"
    assert closed.applied_state == {"3916387": 0}
    assert closed.error is not None and closed.error["code"] == "CONFIG_INVALID"


def test_diverge_without_an_error_or_state(path: Path):
    journal = Journal.open(path)
    entry = journal.begin(make())
    closed = journal.diverge(entry.id, None, None)
    assert closed.status == "diverged" and closed.error is None and closed.applied_state is None


def test_complete_may_close_as_diverged(path: Path):
    journal = Journal.open(path)
    entry = journal.begin(make())
    closed = journal.complete(
        entry.id,
        member_id=None,
        provider_transaction_id="tx",
        proposed_at=None,
        applied_state={"a": 1},
        status="diverged",
    )
    assert closed.status == "diverged"


def test_complete_cannot_close_as_pending_or_rejected(path: Path):
    journal = Journal.open(path)
    entry = journal.begin(make())
    for status in ("pending", "rejected"):
        with pytest.raises(ValueError, match="executed"):
            journal.complete(
                entry.id,
                member_id=None,
                provider_transaction_id=None,
                proposed_at=None,
                applied_state=None,
                status=status,
            )


def test_an_entry_closes_once(path: Path):
    journal = Journal.open(path)
    entry = journal.begin(make())
    journal.reject(entry.id, ConfigInvalidError("no"))
    with pytest.raises(ValueError, match="'rejected', not 'pending'"):
        journal.reject(entry.id, ConfigInvalidError("again"))


def test_closing_an_unknown_entry_is_a_lookup_error(path: Path):
    journal = Journal.open(path)
    journal.begin(make())
    with pytest.raises(LookupError):
        journal.reject("no-such-id", ConfigInvalidError("no"))


def test_a_reversal_points_at_the_entry_it_undoes(path: Path):
    journal = Journal.open(path)
    original = journal.begin(make())
    reversal = journal.begin(make(kind="reverse", reverses=original.id))
    assert Journal.open(path).get(reversal.id).reverses == original.id  # type: ignore[union-attr]


def test_a_duplicate_id_is_refused_loudly(path: Path):
    journal = Journal.open(path)
    entry = make()
    journal.begin(entry)
    with pytest.raises(ConfigInvalidError):
        journal.begin(entry)
    assert journal.count() == 1


# --------------------------------------------------------------------------- #
# Redaction: read the file's bytes back
# --------------------------------------------------------------------------- #


def _file_bytes(path: Path) -> bytes:
    return b"".join(
        candidate.read_bytes()
        for candidate in sorted(path.parent.iterdir())
        if candidate.name.startswith(path.name)
    )


def test_credentials_and_guids_never_reach_the_file_bytes(path: Path):
    remember_secret(FAKE_S2)
    journal = Journal.open(path)
    entry = journal.begin(
        make(
            command=["fantasy-sports", "--espn-s2", FAKE_S2, f"--swid={STRAY_GUID}"],
            request={"cookie": f"espn_s2={FAKE_S2}", "memberId": STRAY_GUID, STRAY_GUID: "key"},
            prior_state={"owner": STRAY_GUID, "note": f"token {FAKE_S2}"},
            member_id=MEMBER_GUID,
            team_id=STRAY_GUID,
            cost={"bid_amount": 3, "displaced_player_id": STRAY_GUID},
        )
    )
    journal.reject(
        entry.id, ConfigInvalidError(f"Rejected for {STRAY_GUID} with espn_s2={FAKE_S2}")
    )
    on_disk = _file_bytes(path)
    for secret in (
        FAKE_S2,
        STRAY_GUID,
        STRAY_GUID.strip("{}"),
        MEMBER_GUID,
        MEMBER_GUID.strip("{}"),
    ):
        assert secret.encode() not in on_disk, secret
    assert FAKE_SWID.encode() not in on_disk

    shown = json.dumps(journal.get(entry.id).to_dict())  # type: ignore[union-attr]
    assert FAKE_S2 not in shown and STRAY_GUID not in shown


def test_guids_in_the_error_message_and_complete_fields_are_scrubbed_too(path: Path):
    journal = Journal.open(path)
    entry = journal.begin(make())
    journal.complete(
        entry.id,
        member_id=MEMBER_GUID,
        provider_transaction_id=f"tx-{STRAY_GUID}",
        proposed_at=None,
        applied_state={"seen": STRAY_GUID},
    )
    on_disk = _file_bytes(path)
    assert STRAY_GUID.encode() not in on_disk
    assert MEMBER_GUID.encode() not in on_disk


def test_a_value_the_chain_registered_is_redacted_in_a_non_pattern_position(path: Path):
    secret = "opaque-session-token-0123456789"
    remember_secret(secret)
    journal = Journal.open(path)
    journal.begin(make(command=["x", secret]))
    assert secret.encode() not in _file_bytes(path)


def test_ordinary_values_survive_redaction_untouched(path: Path):
    journal = Journal.open(path)
    stored = journal.begin(make())
    assert stored.prior_state == {"3916387": 0, "4262921": 20}
    assert stored.request["items"] == [{"playerId": 3916387, "from": 0, "to": 20}]


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def test_list_is_newest_first_and_limited(path: Path):
    journal = Journal.open(path)
    ids = [journal.begin(make(command=["n", str(i)])).id for i in range(5)]
    assert [e.id for e in journal.list()] == ids[::-1]
    assert [e.id for e in journal.list(limit=2)] == ids[:-3:-1]


def test_list_filters_by_league_and_by_irreversibility(path: Path):
    journal = Journal.open(path)
    a = journal.begin(make(league_id="1"))
    b = journal.begin(make(league_id="2", kind="add", reversible=False))
    c = journal.begin(make(league_id="1", kind="drop", reversible=False))
    assert {e.id for e in journal.list(league_id="1")} == {a.id, c.id}
    assert {e.id for e in journal.list(irreversible_only=True)} == {b.id, c.id}
    assert [e.id for e in journal.list(league_id="1", irreversible_only=True)] == [c.id]
    assert journal.list(league_id="404") == []


def test_list_rejects_a_limit_below_one(path: Path):
    with pytest.raises(ValueError):
        Journal.open(path).list(limit=0)


def test_nearest_ids_prefers_prefix_matches(path: Path):
    journal = Journal.open(path)
    ids = [journal.begin(make()).id for _ in range(4)]
    nearest = journal.nearest_ids(ids[2][:8])
    assert nearest[0] == ids[2]
    assert len(nearest) <= 3 and set(nearest) <= set(ids)


def test_a_zero_byte_file_is_an_empty_journal(path: Path):
    path.parent.mkdir(parents=True)
    path.touch()
    journal = Journal.open(path)
    assert journal.list() == [] and journal.count() == 0
    # ...and the first write still works against it.
    journal.begin(make())
    assert Journal.open(path).count() == 1


def test_entries_survive_reopening(path: Path):
    first = Journal.open(path)
    entry = first.begin(make())
    first.close()
    assert Journal.open(path).get(entry.id) == entry


# --------------------------------------------------------------------------- #
# Loud writing, loud corruption
# --------------------------------------------------------------------------- #


def test_a_begin_that_cannot_write_raises_and_names_the_path(tmp_path: Path):
    blocker = tmp_path / "state"
    blocker.write_text("a file where a directory is wanted")
    target = blocker / "fantasy-sports" / "journal.sqlite"
    with pytest.raises(ConfigInvalidError) as caught:
        Journal.open(target).begin(make())
    error = caught.value
    assert error.kind == "config"
    assert str(target) in error.message
    assert error.remediation is not None and str(target.parent) in error.remediation
    assert error.details["path"] == str(target)


@posix_only
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
def test_a_begin_against_a_read_only_file_raises_instead_of_dropping_the_entry(path: Path):
    with Journal.open(path) as journal:
        journal.begin(make())
    path.chmod(0o400)
    try:
        # chmod happens on open, so the owner can always tighten it again; make
        # the *directory* unwritable too, which is what stops a journal file.
        path.parent.chmod(0o500)
        with pytest.raises(ConfigInvalidError, match="not writable"):
            Journal.open(path).begin(make())
    finally:
        path.parent.chmod(0o700)
        path.chmod(0o600)
    assert Journal.open(path).count() == 1


def test_a_begin_into_a_corrupt_file_raises_rather_than_overwriting(path: Path):
    path.parent.mkdir(parents=True)
    path.write_text("this is not a sqlite file, and it is evidence")
    with pytest.raises(ConfigInvalidError, match="not writable"):
        Journal.open(path).begin(make())
    assert path.read_text() == "this is not a sqlite file, and it is evidence"


def test_reading_a_corrupt_file_is_an_error_not_an_empty_list(path: Path):
    path.parent.mkdir(parents=True)
    path.write_text("this is not a sqlite file")
    journal = Journal.open(path)
    for call in (journal.list, journal.count, journal.pending):
        with pytest.raises(ConfigInvalidError, match="Refusing to report it as empty"):
            call()
    with pytest.raises(ConfigInvalidError):
        journal.get("x")


# --------------------------------------------------------------------------- #
# health() — what doctor reports
# --------------------------------------------------------------------------- #


def test_health_of_a_journal_that_does_not_exist_yet(path: Path):
    health = Journal.open(path).health()
    assert not health.exists
    assert health.entries == 0 and health.pending == 0
    assert health.permissions_ok
    assert health.error is None
    assert not path.parent.exists()


def test_health_counts_entries_and_lists_pending_ids(path: Path):
    journal = Journal.open(path)
    stuck = journal.begin(make())
    finished = journal.begin(make())
    journal.reject(finished.id, ConfigInvalidError("no"))
    health = Journal.open(path).health()
    assert health.exists and health.entries == 2 and health.pending == 1
    assert health.pending_ids == (stuck.id,)
    assert health.permissions_ok


@posix_only
def test_health_flags_a_world_readable_file(path: Path):
    Journal.open(path).begin(make())
    path.chmod(0o644)
    health = Journal.open(path).health()
    assert health.file_mode == 0o644
    assert not health.permissions_ok


def test_health_reports_a_corrupt_file_without_raising(path: Path):
    path.parent.mkdir(parents=True)
    path.write_text("garbage")
    health = Journal.open(path).health()
    assert health.exists
    assert health.entries is None and health.pending is None
    assert health.error is not None and str(path) in health.error


# --------------------------------------------------------------------------- #
# Import budget (ADR-0008)
# --------------------------------------------------------------------------- #


def test_importing_the_journal_pulls_in_no_cli_stack_and_no_sqlite():
    probe = (
        "import sys, json; import fantasy_sports.core.journal; "
        "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))"
    )
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    loaded = set(json.loads(proc.stdout.strip().splitlines()[-1]))
    assert not loaded & {"typer", "click", "rich", "espn_api", "requests", "keyring", "sqlite3"}


# --------------------------------------------------------------------------- #
# Attribution comes from the response; a damaged file is never "empty"
# --------------------------------------------------------------------------- #


def test_begin_drops_a_member_id_and_only_complete_supplies_one(path: Path):
    journal = Journal.open(path)
    stored = journal.begin(make(member_id=MEMBER_GUID))
    assert stored.member_id is None
    assert Journal.open(path).get(stored.id).member_id is None  # type: ignore[union-attr]

    closed = journal.complete(
        stored.id,
        member_id=MEMBER_GUID,
        provider_transaction_id=None,
        proposed_at=None,
        applied_state=None,
    )
    assert closed.member_id is not None and closed.member_id != MEMBER_GUID

    rejected = journal.begin(make(member_id=MEMBER_GUID))
    assert journal.reject(rejected.id, ConfigInvalidError("no")).member_id is None


def _journal_without_its_table(path: Path) -> None:
    import sqlite3

    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE something_else (x INTEGER)")
    connection.commit()
    connection.close()


def test_a_sqlite_file_that_lost_its_journal_table_is_a_failure_not_an_empty_list(path: Path):
    _journal_without_its_table(path)
    journal = Journal.open(path)
    for call in (journal.list, journal.count, journal.pending):
        with pytest.raises(ConfigInvalidError, match="Refusing to report it as empty"):
            call()
    health = journal.health()
    assert health.error is not None and health.entries is None


def test_begin_refuses_to_recreate_the_schema_over_a_damaged_file(path: Path):
    import sqlite3

    _journal_without_its_table(path)
    with pytest.raises(ConfigInvalidError, match="not writable"):
        Journal.open(path).begin(make())
    connection = sqlite3.connect(path)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    connection.close()
    assert tables == {"something_else"}
