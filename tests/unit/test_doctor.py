"""``fantasy-sports doctor`` (ADR-0005 §11.4, jwulff/fantasy-sports#10).

Every check reports a finding; none of them makes ``doctor`` itself raise —
that is the property under test as much as any individual check's content.
``tests/conftest.py``'s ``_no_health_check_by_default`` autouse fixture keeps
the version/provider-status checks off the network by default here too; the
tests that mean to exercise them clear it explicitly, same as
``tests/unit/test_health_client.py``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _harness import FAKE_S2, FAKE_SWID, RecordedEspn, install_espn, isolate_home

from fantasy_sports.commands import REGISTRY
from fantasy_sports.commands.doctor import CheckStatus, doctor
from fantasy_sports.core.errors import ConfigInvalidError
from fantasy_sports.core.redaction import forget_secrets
from fantasy_sports.health import client as health_client

CHECK_NAMES = {
    "python",
    "config",
    "credentials",
    "cache",
    "journal",
    "notify",
    "version",
    "provider_status",
    "leagues_reachable",
}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield isolate_home(tmp_path, monkeypatch)
    forget_secrets()


@pytest.fixture
def espn(monkeypatch: pytest.MonkeyPatch) -> RecordedEspn:
    return install_espn(monkeypatch)


def _by_name(envelope: Any) -> dict[str, dict[str, Any]]:
    return {check["name"]: check for check in envelope.data["checks"]}


# --------------------------------------------------------------------------- #
# Shape and coverage
# --------------------------------------------------------------------------- #


def test_doctor_is_registered_takes_no_league_and_declares_live():
    spec = REGISTRY["doctor"]
    assert spec.takes_league is False
    assert [param.name for param in spec.params] == ["live"]
    assert spec.params[0].annotation is bool
    assert spec.params[0].default is False


def test_doctor_always_succeeds_and_runs_every_check():
    envelope = doctor()
    assert envelope.ok
    assert envelope.error is None
    assert isinstance(envelope.data, dict)
    assert set(_by_name(envelope)) == CHECK_NAMES


def test_doctor_data_carries_status_and_ok_derived_from_the_worst_check():
    envelope = doctor()
    assert envelope.data["status"] in {s.value for s in CheckStatus}
    assert isinstance(envelope.data["ok"], bool)


# --------------------------------------------------------------------------- #
# python
# --------------------------------------------------------------------------- #


def test_python_check_reports_the_five_budgeted_dependencies():
    checks = _by_name(doctor())
    python = checks["python"]
    assert python["status"] == "ok"
    deps = python["details"]["dependencies"]
    assert set(deps) == {"espn-api", "typer", "requests", "keyring", "tomli-w"}
    assert all(version is not None for version in deps.values())
    assert python["details"]["fantasy_sports_version"]
    assert python["details"]["python_version"]


def test_python_check_warns_when_dependency_metadata_is_missing(monkeypatch: pytest.MonkeyPatch):
    from importlib import metadata

    real_version = metadata.version

    def _flaky(name: str) -> str:
        if name == "keyring":
            raise metadata.PackageNotFoundError(name)
        return real_version(name)

    monkeypatch.setattr(metadata, "version", _flaky)
    checks = _by_name(doctor())
    assert checks["python"]["status"] == "warn"
    assert checks["python"]["details"]["dependencies"]["keyring"] is None
    assert "keyring" in checks["python"]["summary"]


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


def test_config_check_ok_when_leagues_are_configured():
    checks = _by_name(doctor())
    assert checks["config"]["status"] == "ok"
    assert set(checks["config"]["details"]["leagues"]) == {"synthetic", "broken"}


def test_config_check_warns_on_an_empty_but_valid_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    empty = tmp_path / "config" / "fantasy-sports" / "config.toml"
    empty.write_text("")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    checks = _by_name(doctor())
    assert checks["config"]["status"] == "warn"
    assert checks["config"]["details"]["leagues"] == []


def test_config_check_fails_on_invalid_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config_dir = tmp_path / "config" / "fantasy-sports"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text("this is not [ valid toml")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    checks = _by_name(doctor())
    assert checks["config"]["status"] == "fail"


def test_doctor_never_raises_when_config_toml_is_broken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A parse failure must be a *finding*, not a crash on the check after it.

    Regression: `_leagues_reachable_check` originally called `list_leagues()`
    uncaught, so a broken `config.toml` took down the whole `doctor` command
    instead of being reported once by the `config` check.
    """
    config_dir = tmp_path / "config" / "fantasy-sports"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text("this is not [ valid toml")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    checks = _by_name(doctor(live=True))
    assert checks["config"]["status"] == "fail"
    assert checks["leagues_reachable"]["status"] == "skipped"


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #


def test_credentials_check_ok_when_both_are_configured():
    checks = _by_name(doctor())
    assert checks["credentials"]["status"] == "ok"
    assert checks["credentials"]["details"]["complete"] is True


def test_credentials_check_warns_when_incomplete(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FANTASY_SPORTS_ESPN_S2", raising=False)
    checks = _by_name(doctor())
    assert checks["credentials"]["status"] == "warn"
    assert checks["credentials"]["details"]["complete"] is False


def test_credentials_check_warns_when_a_stored_credential_is_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Staleness is only knowable for a Keychain-sourced credential (auth/staleness.py)."""
    from datetime import UTC, datetime, timedelta

    from fantasy_sports.auth import chain as auth_chain
    from fantasy_sports.auth import staleness

    monkeypatch.delenv("FANTASY_SPORTS_ESPN_S2", raising=False)
    monkeypatch.delenv("FANTASY_SPORTS_SWID", raising=False)
    monkeypatch.setattr(
        auth_chain,
        "read_from_keychain",
        lambda spec: {
            "espn_s2": "a" * 20,
            "swid": "{1A2B3C4D-5E6F-4A8B-9C0D-1E2F3A4B5C6D}",
        }[spec.name],
    )
    old = datetime.now(UTC) - timedelta(days=90)
    staleness.record_stored(["espn_s2", "swid"], now=old, path=staleness.auth_state_path())

    checks = _by_name(doctor())
    assert checks["credentials"]["status"] == "warn"
    assert "stale" in checks["credentials"]["summary"]


def test_credentials_check_warns_when_the_two_cookies_come_from_different_links(
    monkeypatch: pytest.MonkeyPatch,
):
    """espn_s2 authenticates a write; swid decides ownership. Two links may mean
    two accounts (#107). Static, no network."""
    from fantasy_sports.auth import chain as auth_chain

    monkeypatch.delenv("FANTASY_SPORTS_SWID", raising=False)
    monkeypatch.setattr(
        auth_chain,
        "read_from_keychain",
        lambda spec: {"swid": "{1A2B3C4D-5E6F-4A8B-9C0D-1E2F3A4B5C6D}"}.get(spec.name),
    )
    checks = _by_name(doctor())
    assert checks["credentials"]["status"] == "warn"
    assert "different ESPN accounts" in checks["credentials"]["summary"]
    assert checks["credentials"]["details"]["sources_match"] is False


def test_credentials_from_one_link_report_sources_matching():
    checks = _by_name(doctor())
    assert checks["credentials"]["details"]["sources_match"] is True


def test_credentials_check_never_carries_a_credential_value():
    import json as _json

    checks = _by_name(doctor())
    rendered = _json.dumps(checks["credentials"])
    assert FAKE_S2 not in rendered
    assert FAKE_SWID not in rendered


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #


def test_cache_check_ok_on_a_fresh_store():
    checks = _by_name(doctor())
    cache = checks["cache"]
    assert cache["status"] == "ok"
    assert cache["details"]["entries"] == 0
    assert cache["details"]["size_bytes"] >= 0


def test_cache_check_fails_when_the_store_cannot_be_opened_at_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A file where the cache directory should be — mirrors test_cache.py's own fixture."""
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file where a directory was wanted")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocker))
    checks = _by_name(doctor())
    assert checks["cache"]["status"] == "fail"
    assert "could not be opened" in checks["cache"]["summary"]


def test_count_cache_entries_fails_open_to_none_on_a_corrupt_file(tmp_path: Path):
    from fantasy_sports.commands.doctor import _count_cache_entries

    corrupt = tmp_path / "not-a-database.sqlite3"
    corrupt.write_text("this is not a sqlite file")
    assert _count_cache_entries(corrupt) is None


def test_cache_check_counts_entries_after_a_read(espn: RecordedEspn):
    from fantasy_sports.commands.league import info

    info(league="synthetic")
    checks = _by_name(doctor())
    assert checks["cache"]["details"]["entries"] > 0


# --------------------------------------------------------------------------- #
# version / provider_status — opted out by default in this suite
# --------------------------------------------------------------------------- #


def test_version_and_provider_status_are_skipped_when_opted_out():
    checks = _by_name(doctor())
    assert checks["version"]["status"] == "skipped"
    assert checks["provider_status"]["status"] == "skipped"


class _FakeResponse:
    def __init__(self, body: Any, status_code: int = 200) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return self._body


def test_version_check_warns_on_an_available_upgrade(monkeypatch: pytest.MonkeyPatch):
    import requests

    from fantasy_sports import __version__

    monkeypatch.delenv(health_client.NO_CHECK_ENV, raising=False)
    manifest = {
        "latest_version": "999.0.0",
        "providers": {"espn": {"status": "healthy", "known_issues": []}},
    }
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(manifest))
    checks = _by_name(doctor())
    assert checks["version"]["status"] == "warn"
    assert checks["version"]["details"]["your_version"] == __version__
    assert checks["version"]["details"]["latest_version"] == "999.0.0"
    assert checks["provider_status"]["status"] == "ok"


def test_version_check_fails_when_running_a_yanked_release(monkeypatch: pytest.MonkeyPatch):
    import requests

    from fantasy_sports import __version__

    monkeypatch.delenv(health_client.NO_CHECK_ENV, raising=False)
    manifest = {
        "latest_version": "999.0.0",
        "yanked_versions": [__version__],
        "providers": {},
    }
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(manifest))
    checks = _by_name(doctor())
    assert checks["version"]["status"] == "fail"
    assert checks["version"]["details"]["yanked"] is True


def test_provider_status_check_warns_on_a_degraded_provider(monkeypatch: pytest.MonkeyPatch):
    import requests

    monkeypatch.delenv(health_client.NO_CHECK_ENV, raising=False)
    manifest = {
        "latest_version": "0.1.0.dev0",
        "providers": {"espn": {"status": "degraded", "known_issues": []}},
    }
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(manifest))
    checks = _by_name(doctor())
    assert checks["provider_status"]["status"] == "warn"
    assert "degraded" in checks["provider_status"]["summary"]


def test_doctor_never_raises_when_get_manifest_itself_blows_up(monkeypatch: pytest.MonkeyPatch):
    """Belt and braces over ``health.client.get_manifest``'s own fail-open promise."""

    def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("a bug inside get_manifest")

    monkeypatch.delenv(health_client.NO_CHECK_ENV, raising=False)
    monkeypatch.setattr(health_client, "get_manifest", _boom)
    checks = _by_name(doctor())
    assert checks["version"]["status"] == "warn"
    assert checks["provider_status"]["status"] == "warn"


def test_version_check_warns_when_the_manifest_is_unreachable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(health_client.NO_CHECK_ENV, raising=False)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise TimeoutError("offline")

    import requests

    monkeypatch.setattr(requests, "get", _boom)
    checks = _by_name(doctor())
    assert checks["version"]["status"] == "warn"
    assert checks["provider_status"]["status"] == "warn"


# --------------------------------------------------------------------------- #
# leagues_reachable — only under --live
# --------------------------------------------------------------------------- #


def test_leagues_reachable_is_skipped_without_live():
    checks = _by_name(doctor(live=False))
    assert checks["leagues_reachable"]["status"] == "skipped"
    assert set(checks["leagues_reachable"]["details"]["leagues"]) == {"synthetic", "broken"}


def test_leagues_reachable_reports_a_working_league_and_a_broken_one(espn: RecordedEspn):
    checks = _by_name(doctor(live=True))
    result = checks["leagues_reachable"]
    assert result["status"] == "fail"  # `broken` names an unknown provider
    leagues = result["details"]["leagues"]
    assert leagues["synthetic"] == {"reachable": True}
    assert leagues["broken"]["reachable"] is False
    assert leagues["broken"]["code"] == "CONFIG_INVALID"


def test_leagues_reachable_reports_an_unclassified_exception_as_provider_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    """A bare exception from the provider is still a *finding*, classified like

    any other command's error path (``output.errors.classify``), never a crash
    of ``doctor`` itself.
    """
    from fantasy_sports.providers.espn import EspnProvider

    def _boom(self: EspnProvider, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("something ESPN never documented")

    monkeypatch.setattr(EspnProvider, "fetch_league", _boom)
    checks = _by_name(doctor(live=True))
    result = checks["leagues_reachable"]["details"]["leagues"]["synthetic"]
    assert result == {
        "reachable": False,
        "code": "PROVIDER_UNAVAILABLE",
        "message": "Unclassified failure (RuntimeError): something ESPN never documented",
    }


def test_leagues_reachable_is_skipped_with_no_leagues_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    empty = tmp_path / "config" / "fantasy-sports"
    empty.mkdir(parents=True, exist_ok=True)
    (empty / "config.toml").write_text("")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    checks = _by_name(doctor(live=True))
    assert checks["leagues_reachable"]["status"] == "skipped"
    assert checks["leagues_reachable"]["summary"] == "No leagues configured."


# --------------------------------------------------------------------------- #
# journal
# --------------------------------------------------------------------------- #


def _journal_entry(**overrides: Any):
    from fantasy_sports.core.journal import JournalEntry

    base: dict[str, Any] = {
        "provider": "espn",
        "league_id": "99",
        "season": 2026,
        "command": ["fantasy-sports", "lineup", "set"],
        "kind": "lineup",
        "reversible": True,
        "prior_state": {"1": 0},
        "request": {"type": "LINEUP"},
    }
    base.update(overrides)
    return JournalEntry(**base)


def test_journal_check_is_ok_and_creates_nothing_when_no_journal_exists_yet():
    from fantasy_sports.core.journal import journal_path

    journal = _by_name(doctor())["journal"]
    assert journal["status"] == "ok"
    assert journal["details"]["exists"] is False
    assert journal["details"]["entries"] == 0
    assert journal["details"]["path"] == str(journal_path())
    assert not journal_path().parent.exists()


def test_journal_check_counts_a_healthy_journal():
    from fantasy_sports.core.journal import Journal

    with Journal.open() as store:
        done = store.begin(_journal_entry())
        store.reject(done.id, ConfigInvalidError("no"))
    journal = _by_name(doctor())["journal"]
    assert journal["status"] == "ok"
    assert journal["details"]["entries"] == 1
    assert journal["details"]["pending"] == 0
    assert journal["details"]["permissions_ok"] is True
    assert "1 entry" in journal["summary"]


def test_journal_check_warns_about_pending_rows_and_names_them():
    from fantasy_sports.core.journal import Journal

    with Journal.open() as store:
        stuck = store.begin(_journal_entry())
        store.begin(_journal_entry())
    journal = _by_name(doctor())["journal"]
    assert journal["status"] == "warn"
    assert journal["details"]["pending"] == 2
    assert stuck.id in journal["details"]["pending_ids"]
    assert "2 journal entries are pending" in journal["summary"]


def test_journal_check_says_entry_in_the_singular_for_one_pending_row():
    from fantasy_sports.core.journal import Journal

    with Journal.open() as store:
        store.begin(_journal_entry())
    assert "1 journal entry is pending" in _by_name(doctor())["journal"]["summary"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_journal_check_warns_when_the_file_is_not_private():
    from fantasy_sports.core.journal import Journal, journal_path

    with Journal.open() as store:
        done = store.begin(_journal_entry())
        store.reject(done.id, ConfigInvalidError("no"))
    journal_path().chmod(0o644)
    journal = _by_name(doctor())["journal"]
    assert journal["status"] == "warn"
    assert journal["details"]["file_mode"] == "0644"
    assert journal["details"]["permissions_ok"] is False
    assert "not private" in journal["summary"]


def test_journal_check_reports_a_corrupt_file_as_a_finding_not_a_crash():
    from fantasy_sports.core.journal import journal_path

    path = journal_path()
    path.parent.mkdir(parents=True)
    path.write_text("this is not a sqlite file")
    envelope = doctor()
    assert envelope.ok
    journal = _by_name(envelope)["journal"]
    assert journal["status"] == "fail"
    assert journal["details"]["entries"] is None
    assert "cannot be read" in journal["summary"]
    assert path.read_text() == "this is not a sqlite file"
    assert envelope.data["status"] == "fail"


def test_journal_check_survives_the_inspection_itself_raising(monkeypatch: pytest.MonkeyPatch):
    from fantasy_sports.core import journal as journal_module

    def explode(self: Any) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(journal_module.Journal, "health", explode)
    envelope = doctor()
    assert envelope.ok
    journal = _by_name(envelope)["journal"]
    assert journal["status"] == "fail"
    assert "RuntimeError" in journal["summary"]


# --------------------------------------------------------------------------- #
# notify
# --------------------------------------------------------------------------- #


def _write_config(isolated_home: Path, extra: str) -> None:
    path = isolated_home / "config" / "fantasy-sports" / "config.toml"
    existing = path.read_text(encoding="utf-8")
    # A bare key must come first or it would land inside the last table.
    joined = existing + "\n" + extra if extra.startswith("[") else extra + "\n" + existing
    path.write_text(joined, encoding="utf-8")


def test_notify_check_is_ok_and_disabled_by_default():
    check = _by_name(doctor())["notify"]
    assert check["status"] == "ok"
    assert check["details"] == {"enabled": False, "command_set": False}


def test_notify_check_reports_a_configured_command_without_running_it(
    isolated_home: Path, tmp_path: Path
):
    marker = tmp_path / "ran"
    _write_config(isolated_home, f'[notify]\nirreversible_command = "touch {marker}"\n')
    check = _by_name(doctor())["notify"]
    assert check["status"] == "ok"
    assert check["details"] == {"enabled": True, "command_set": True}
    assert not marker.exists(), "doctor must never run the delivery command"
    assert str(marker) not in json.dumps(check), "the command line stays out of the report"


def test_notify_check_warns_on_a_blank_command(isolated_home: Path):
    _write_config(isolated_home, '[notify]\nirreversible_command = "   "\n')
    check = _by_name(doctor())["notify"]
    assert check["status"] == "warn"
    assert check["details"]["enabled"] is False


@pytest.mark.parametrize("body", ['notify = "nope"', "[notify]\nirreversible_command = 3"])
def test_notify_check_warns_instead_of_raising_on_a_malformed_table(isolated_home: Path, body: str):
    _write_config(isolated_home, body)
    envelope = doctor()
    assert envelope.ok
    assert _by_name(envelope)["notify"]["status"] == "warn"
