"""``journal list`` and ``journal show`` (jwulff/fantasy-sports#16, store half).

Both read a local file: no league, no credential, no provider, no network. The
isolated XDG tree guarantees nothing here can touch the real journal.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _harness import isolate_home

from fantasy_sports.cli.app import run
from fantasy_sports.commands import REGISTRY, DataShape
from fantasy_sports.core.errors import ConfigInvalidError
from fantasy_sports.core.journal import Journal, JournalEntry, journal_path
from fantasy_sports.core.redaction import forget_secrets


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield isolate_home(tmp_path, monkeypatch)
    forget_secrets()


def _call(name: str, **kwargs: Any) -> Any:
    return REGISTRY[name].resolve()(**kwargs)


def _entry(**overrides: Any) -> JournalEntry:
    base: dict[str, Any] = {
        "provider": "espn",
        "league_id": "99",
        "season": 2026,
        "scoring_period": 1,
        "command": ["fantasy-sports", "lineup", "set"],
        "role": "creator",
        "team_id": "1",
        "kind": "lineup",
        "reversible": True,
        "prior_state": {"1": 0},
        "request": {"type": "LINEUP"},
    }
    base.update(overrides)
    return JournalEntry(**base)


def _seed(*entries: JournalEntry) -> list[JournalEntry]:
    with Journal.open() as journal:
        return [journal.begin(entry) for entry in entries]


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_the_journal_commands_declare_their_shapes_and_take_no_league():
    listing, showing = REGISTRY["journal list"], REGISTRY["journal show"]
    assert listing.shape is DataShape.COLLECTION and showing.shape is DataShape.OBJECT
    assert not listing.takes_league and not showing.takes_league
    assert listing.group == showing.group == "journal"
    assert [p.name for p in listing.params] == ["league", "limit", "irreversible"]
    assert [p.name for p in showing.params] == ["id"]
    assert showing.params[0].required


# --------------------------------------------------------------------------- #
# journal list
# --------------------------------------------------------------------------- #


def test_list_on_a_machine_with_no_journal_is_an_empty_collection_and_creates_nothing():
    envelope = _call("journal list")
    assert envelope.ok and envelope.data == []
    assert not journal_path().parent.exists()


def test_list_envelope_names_no_provider_league_or_season():
    _seed(_entry())
    payload = _call("journal list").to_dict()
    assert payload["schema"] == "fantasy-sports/v1"
    assert payload["provider"] is None
    assert payload["league_id"] is None
    assert payload["season"] is None
    assert payload["sources"] == []
    assert payload["error"] is None
    assert isinstance(payload["data"], list)


def test_list_returns_entry_dicts_newest_first():
    first, second = _seed(_entry(), _entry(kind="drop", reversible=False))
    data = _call("journal list").data
    assert [row["id"] for row in data] == [second.id, first.id]
    assert data[0] == second.to_dict()


def test_list_limit_and_irreversible_filters():
    keep, _, drop = _seed(
        _entry(kind="add", reversible=False), _entry(), _entry(kind="drop", reversible=False)
    )
    assert [r["id"] for r in _call("journal list", limit=1).data] == [drop.id]
    assert {r["id"] for r in _call("journal list", irreversible=True).data} == {keep.id, drop.id}


def test_list_league_accepts_a_profile_name_or_a_raw_id():
    mine, other = _seed(_entry(league_id="99"), _entry(league_id="12345"))
    # The harness config maps the profile "synthetic" to league 99.
    assert [r["id"] for r in _call("journal list", league="synthetic").data] == [mine.id]
    assert [r["id"] for r in _call("journal list", league="12345").data] == [other.id]
    assert _call("journal list", league="no-such-league").data == []


def test_list_league_still_works_when_config_toml_is_broken(isolated_home: Path):
    (isolated_home / "config" / "fantasy-sports" / "config.toml").write_text("not = [valid")
    (entry,) = _seed(_entry(league_id="99"))
    assert [r["id"] for r in _call("journal list", league="99").data] == [entry.id]


# --------------------------------------------------------------------------- #
# journal digest
# --------------------------------------------------------------------------- #


def _seed_at(*moments: str, **overrides: Any) -> list[JournalEntry]:
    return _seed(*(_entry(recorded_at=datetime.fromisoformat(m), **overrides) for m in moments))


def test_digest_is_registered_as_a_leagueless_collection():
    spec = REGISTRY["journal digest"]
    assert spec.shape is DataShape.COLLECTION and not spec.takes_league
    assert spec.group == "journal"
    assert [p.name for p in spec.params] == ["since", "irreversible", "league", "limit"]
    assert spec.params[0].required


def test_digest_keeps_entries_at_or_after_a_timestamp_newest_first():
    old, edge, new = _seed_at(
        "2026-10-14T07:59:59.999Z", "2026-10-14T08:00:00Z", "2026-10-14T09:30:00Z"
    )
    data = _call("journal digest", since="2026-10-14T08:00:00Z").data
    assert [row["id"] for row in data] == [new.id, edge.id]
    assert old.id not in {row["id"] for row in data}
    assert data[0] == new.to_dict()


def test_digest_accepts_an_offset_timestamp_and_normalizes_it_to_utc():
    early, late = _seed_at("2026-10-14T12:59:00Z", "2026-10-14T13:01:00Z")
    data = _call("journal digest", since="2026-10-14T06:00:00-07:00").data
    assert [row["id"] for row in data] == [late.id]
    assert early.id not in {row["id"] for row in data}


def test_digest_cutoff_rounds_up_to_the_stored_millisecond():
    """The file keeps milliseconds; a cutoff between two must not admit the earlier one."""
    (entry,) = _seed_at("2026-10-14T08:00:00.000Z")
    ids = lambda since: [r["id"] for r in _call("journal digest", since=since).data]  # noqa: E731
    assert ids("2026-10-14T08:00:00.000999Z") == []
    assert ids("2026-10-14T08:00:00.000001Z") == []
    assert ids("2026-10-14T08:00:00.000Z") == [entry.id]
    assert ids("2026-10-14T07:59:59.999001Z") == [entry.id]


def test_digest_a_duration_counts_back_from_now():
    now = datetime.now(UTC)
    stale, fresh = _seed(
        _entry(recorded_at=now - timedelta(hours=30)), _entry(recorded_at=now - timedelta(hours=2))
    )
    assert [r["id"] for r in _call("journal digest", since="24h").data] == [fresh.id]
    assert {r["id"] for r in _call("journal digest", since="2d").data} == {stale.id, fresh.id}
    assert _call("journal digest", since="30m").data == []


def test_digest_irreversible_league_and_limit_filters():
    mine, reversible, other_league, newest = _seed(
        _entry(kind="drop", reversible=False),
        _entry(),
        _entry(kind="add", reversible=False, league_id="12345"),
        _entry(kind="claim", reversible=False),
    )
    ids = lambda **kw: [r["id"] for r in _call("journal digest", since="1d", **kw).data]  # noqa: E731
    assert reversible.id not in ids(irreversible=True)
    assert ids(irreversible=True, league="synthetic") == [newest.id, mine.id]
    assert ids(league="12345") == [other_league.id]
    assert ids(limit=1) == [newest.id]


def test_digest_on_a_machine_with_no_journal_is_empty_and_creates_nothing():
    envelope = _call("journal digest", since="7d")
    assert envelope.ok and envelope.data == []
    assert not journal_path().parent.exists()


def test_digest_envelope_names_no_provider_league_or_season():
    envelope = _call("journal digest", since="7d")
    assert (envelope.provider, envelope.league_id, envelope.season) == (None, None, None)


@pytest.mark.parametrize(
    "since", ["", "yesterday", "24", "h", "-1d", "1.5h", "2026-10-14T08:00:00", "2026-13-45", "1w"]
)
def test_digest_refuses_anything_that_is_not_a_duration_or_an_aware_timestamp(since: str):
    with pytest.raises(ConfigInvalidError) as caught:
        _call("journal digest", since=since)
    assert caught.value.kind == "argument"
    assert caught.value.details["argument"] == "since"
    assert caught.value.details["value"] == since


def test_digest_rejects_an_absurd_duration_as_a_bad_argument():
    with pytest.raises(ConfigInvalidError) as caught:
        _call("journal digest", since="99999999999d")
    assert caught.value.details["argument"] == "since"


@pytest.mark.parametrize("limit", [0, -3])
def test_digest_rejects_a_non_positive_limit(limit: int):
    with pytest.raises(ConfigInvalidError) as caught:
        _call("journal digest", since="1d", limit=limit)
    assert caught.value.details["argument"] == "limit"


def test_the_cli_projects_digest(capsys: pytest.CaptureFixture[str]):
    (entry,) = _seed(_entry(kind="drop", reversible=False))
    assert run(["journal", "digest", "--since", "1h", "--irreversible"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in payload["data"]] == [entry.id]


def test_the_cli_reports_a_bad_since_on_stderr(capsys: pytest.CaptureFixture[str]):
    assert run(["journal", "digest", "--since", "soon"]) != 0
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "CONFIG_INVALID"


@pytest.mark.parametrize("limit", [0, -3])
def test_list_rejects_a_non_positive_limit_as_a_bad_argument(limit: int):
    with pytest.raises(ConfigInvalidError) as caught:
        _call("journal list", limit=limit)
    assert caught.value.kind == "argument"
    assert caught.value.details["argument"] == "limit"


def test_list_on_a_corrupt_journal_fails_instead_of_reporting_empty():
    path = journal_path()
    path.parent.mkdir(parents=True)
    path.write_text("not sqlite")
    with pytest.raises(ConfigInvalidError, match="cannot be read"):
        _call("journal list")


# --------------------------------------------------------------------------- #
# journal show
# --------------------------------------------------------------------------- #


def test_show_returns_the_whole_entry_as_an_object():
    (entry,) = _seed(_entry())
    payload = _call("journal show", id=entry.id).to_dict()
    assert payload["provider"] is None and payload["error"] is None
    assert payload["data"] == entry.to_dict()
    assert payload["data"]["prior_state"] == {"1": 0}


def test_show_an_unknown_id_names_the_nearest_and_the_remedy():
    entries = _seed(_entry(), _entry())
    typo = entries[0].id[:-2] + "zz"
    with pytest.raises(ConfigInvalidError) as caught:
        _call("journal show", id=typo)
    error = caught.value
    assert error.kind == "argument"
    assert entries[0].id in error.message
    assert error.details["nearest"][0] == entries[0].id
    assert "journal list" in (error.remediation or "")


def test_show_on_an_empty_journal_says_so():
    with pytest.raises(ConfigInvalidError, match="The journal is empty"):
        _call("journal show", id="nope")


# --------------------------------------------------------------------------- #
# Through the CLI
# --------------------------------------------------------------------------- #


def test_the_cli_projects_list_and_show(capsys: pytest.CaptureFixture[str]):
    (entry,) = _seed(_entry())

    assert run(["journal", "list", "--limit", "5"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in listed["data"]] == [entry.id]

    assert run(["journal", "show", "--id", entry.id]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["data"]["id"] == entry.id


def test_a_root_level_league_flag_filters_too(capsys: pytest.CaptureFixture[str]):
    _seed(_entry(league_id="99"), _entry(league_id="7"))
    assert run(["--league", "7", "journal", "list"]) == 0
    assert [r["league_id"] for r in json.loads(capsys.readouterr().out)["data"]] == ["7"]


def test_the_cli_reports_an_unknown_id_as_config_invalid_on_stderr(
    capsys: pytest.CaptureFixture[str],
):
    status = run(["journal", "show", "--id", "missing"])
    captured = capsys.readouterr()
    assert status != 0 and captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["code"] == "CONFIG_INVALID"
    assert error["details"]["kind"] == "argument"
