"""The irreversible-write notify hook (jwulff/fantasy-sports#113, requirement R7a).

The hook runs a command the owner configured, with the journal entry as JSON on
stdin. Everything but one test uses an injected runner, so no shell is spawned;
the one that does runs ``cat > <file>`` to prove the plumbing end to end.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _harness import FAKE_S2, isolate_home

from fantasy_sports.config import leagues
from fantasy_sports.config import notify as config_notify
from fantasy_sports.core.errors import ConfigInvalidError
from fantasy_sports.core.journal import Journal, JournalEntry
from fantasy_sports.core.redaction import forget_secrets, remember_secret
from fantasy_sports.reports.notify import (
    NOTIFY_TIMEOUT_SECONDS,
    NotifyResult,
    notify_irreversible,
    should_notify,
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield isolate_home(tmp_path, monkeypatch)
    forget_secrets()


def _entry(**overrides: Any) -> JournalEntry:
    base: dict[str, Any] = {
        "provider": "espn",
        "league_id": "99",
        "season": 2026,
        "command": ["fantasy-sports", "drop", "--team", "1"],
        "team_id": "1",
        "kind": "drop",
        "reversible": False,
        "prior_state": {"9000012": 4},
        "request": {"type": "DROP"},
        "status": "executed",
    }
    base.update(overrides)
    return JournalEntry(**base)


class FakeRunner:
    """Records every call and answers like ``subprocess.run``."""

    def __init__(self, *, returncode: int = 0, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self._returncode = returncode
        self._raises = raises

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(returncode=self._returncode, stdout=b"", stderr=b"")


def _config(command: str | None = "tell-john") -> config_notify.NotifyConfig:
    return config_notify.NotifyConfig(irreversible_command=command)


def _write_config(isolated_home: Path, text: str) -> Path:
    path = isolated_home / "config" / "fantasy-sports" / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# [notify] config
# --------------------------------------------------------------------------- #


def test_notify_is_disabled_when_the_file_table_or_key_is_absent(
    isolated_home: Path, tmp_path: Path
):
    assert config_notify.load(tmp_path / "missing.toml") == config_notify.NotifyConfig()
    assert not config_notify.load().enabled  # isolate_home's config has no [notify]
    _write_config(isolated_home, "[notify]\nsomething_else = 1\n")
    assert not config_notify.load().enabled


def test_notify_reads_the_command_and_tolerates_unknown_keys(isolated_home: Path):
    _write_config(isolated_home, '[notify]\nirreversible_command = "x"\nfuture = true\n')
    config = config_notify.load()
    assert config.enabled and config.irreversible_command == "x"


def test_a_blank_command_is_not_enabled(isolated_home: Path):
    _write_config(isolated_home, '[notify]\nirreversible_command = "  "\n')
    assert not config_notify.load().enabled


@pytest.mark.parametrize("body", ['notify = "x"', "[notify]\nirreversible_command = 3"])
def test_a_malformed_notify_table_is_config_invalid(isolated_home: Path, body: str):
    _write_config(isolated_home, body)
    with pytest.raises(ConfigInvalidError):
        config_notify.load()


def test_leagues_save_keeps_the_notify_table(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[notify]\nirreversible_command = "tell-john"\nfuture = 1\n')
    leagues.save(leagues.LeagueConfig(path=path, default=None, profiles_by_name={}))
    assert tomllib.loads(path.read_text())["notify"] == {
        "irreversible_command": "tell-john",
        "future": 1,
    }
    assert config_notify.load(path).irreversible_command == "tell-john"


# --------------------------------------------------------------------------- #
# should_notify
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("reversible", "status", "expected"),
    [
        (False, "executed", True),
        (False, "diverged", True),
        (False, "rejected", False),
        (False, "pending", False),
        (True, "executed", False),
        (True, "diverged", False),
    ],
)
def test_should_notify_only_for_irreversible_writes_that_took_effect(
    reversible: bool, status: str, expected: bool
):
    kind = "lineup" if reversible else "drop"
    assert should_notify(_entry(reversible=reversible, status=status, kind=kind)) is expected


# --------------------------------------------------------------------------- #
# notify_irreversible
# --------------------------------------------------------------------------- #


def test_disabled_runs_nothing():
    runner = FakeRunner()
    result = notify_irreversible(_entry(), config=_config(None), runner=runner)
    assert result == NotifyResult(enabled=False, attempted=False)
    assert result.ok is None and runner.calls == []


def test_it_runs_the_command_with_the_entry_as_json_on_stdin():
    runner = FakeRunner()
    entry = _entry()
    result = notify_irreversible(entry, config=_config("tell-john --now"), runner=runner)

    assert result == NotifyResult(enabled=True, attempted=True, ok=True, exit_status=0)
    ((args, kwargs),) = runner.calls
    assert args == ("tell-john --now",)
    assert kwargs["shell"] is True
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == NOTIFY_TIMEOUT_SECONDS == 20
    assert "env" not in kwargs, "the environment is passed through unchanged"
    payload = kwargs["input"]
    assert payload.endswith(b"\n") and payload.count(b"\n") == 1
    assert json.loads(payload) == entry.to_dict()


def test_it_reads_the_command_from_config_toml_by_default(isolated_home: Path):
    _write_config(isolated_home, '[notify]\nirreversible_command = "from-file"\n')
    runner = FakeRunner()
    assert notify_irreversible(_entry(), runner=runner).ok is True
    assert runner.calls[0][0] == ("from-file",)


def test_a_nonzero_exit_is_reported_not_raised():
    result = notify_irreversible(_entry(), config=_config(), runner=FakeRunner(returncode=3))
    assert (result.enabled, result.attempted, result.ok, result.exit_status) == (
        True,
        True,
        False,
        3,
    )
    assert result.error == "notify command exited with status 3"


def test_a_timeout_is_reported_not_raised():
    boom = subprocess.TimeoutExpired(cmd="tell-john", timeout=20)
    result = notify_irreversible(_entry(), config=_config(), runner=FakeRunner(raises=boom))
    assert result.ok is False and result.exit_status is None
    assert result.error == "notify command timed out after 20s"


def test_an_oserror_is_reported_without_echoing_its_message():
    boom = OSError("secret-ish detail /etc/passwd")
    result = notify_irreversible(_entry(), config=_config(), runner=FakeRunner(raises=boom))
    assert result.ok is False and result.attempted is True
    assert result.error == "notify command could not run (OSError)"
    assert "passwd" not in str(result)


def test_an_unreadable_config_is_reported_not_raised(isolated_home: Path):
    _write_config(isolated_home, "[notify\n")
    runner = FakeRunner()
    result = notify_irreversible(_entry(), runner=runner)
    assert result.enabled is False and result.attempted is False
    assert result.error == "config unreadable (ConfigInvalidError)"
    assert runner.calls == []


def test_the_result_serializes_to_plain_json():
    result = notify_irreversible(_entry(), config=_config(), runner=FakeRunner(returncode=1))
    assert json.loads(json.dumps(result.to_dict())) == {
        "enabled": True,
        "attempted": True,
        "ok": False,
        "exit_status": 1,
        "error": "notify command exited with status 1",
    }


def test_a_registered_secret_in_an_entry_never_reaches_the_subprocess_input():
    """Journal through a real store, then notify from what it stored.

    The secret is registered, placed in several fields, and the entry read back
    from disk is what the hook serializes: the journal's redaction is the
    control, and this proves the hook adds no second path around it.
    """
    secret = "s3cr3t-cookie-value-0123456789abcdef"
    remember_secret(secret)
    remember_secret(FAKE_S2)
    with Journal.open() as journal:
        begun = journal.begin(
            _entry(
                status="pending",
                command=["fantasy-sports", "drop", f"--espn-s2={secret}"],
                prior_state={"note": f"s2={secret}", FAKE_S2: 1},
                request={"cookie": secret, "type": "DROP"},
            )
        )
        journal.complete(
            begun.id,
            member_id=None,
            provider_transaction_id="tx-1",
            proposed_at=datetime(2026, 10, 14, tzinfo=UTC),
            applied_state={"echo": FAKE_S2},
        )
        stored = journal.get(begun.id)
    assert stored is not None and should_notify(stored)

    runner = FakeRunner()
    notify_irreversible(stored, config=_config(), runner=runner)
    sent = runner.calls[0][1]["input"].decode()
    assert secret not in sent and FAKE_S2 not in sent
    assert "redacted" in sent, "the field was scrubbed, not dropped"


def test_the_default_runner_really_pipes_the_entry_to_a_shell_command(tmp_path: Path):
    out = tmp_path / "received.json"
    entry = _entry()
    result = notify_irreversible(entry, config=_config(f"cat > '{out}'"))
    assert result == NotifyResult(enabled=True, attempted=True, ok=True, exit_status=0)
    assert json.loads(out.read_text()) == entry.to_dict()


def test_a_missing_command_is_a_shell_127_reported_in_the_result():
    result = notify_irreversible(_entry(), config=_config("definitely-not-a-command-113"))
    assert result.attempted and result.ok is False and result.exit_status == 127


def test_the_example_script_appends_one_summary_line_per_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The shipped sample hook works when driven through the real hook."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "notify-irreversible-example.sh"
    assert script.stat().st_mode & 0o111, "the example must be executable"
    state = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    entry = _entry(cost={"bid_amount": 7, "displaced_player_id": "9000012"}, kind="claim")

    for _ in range(2):
        assert notify_irreversible(entry, config=_config(str(script))).ok is True

    lines = (state / "fantasy-sports" / "irreversible.log").read_text().splitlines()
    cost = '{"bid_amount":7,"displaced_player_id":"9000012"}'
    expected = f"{entry.to_dict()['recorded_at']} claim 1 99 {cost}"
    assert lines == [expected, expected]


def test_a_timeout_kills_the_whole_process_group(tmp_path: Path):
    """``subprocess.run`` would kill only the shell; the child must die too."""
    import os
    import time

    from fantasy_sports.reports.notify import _run_grouped

    marker = tmp_path / "fired-late"
    killed: list[int] = []

    def recording_killpg(pgid: int, sig: int) -> None:
        killed.append(pgid)
        os.killpg(pgid, sig)

    with pytest.raises(subprocess.TimeoutExpired):
        _run_grouped(
            f"(sleep 1; touch '{marker}') & sleep 30",
            shell=True,
            input=b"{}\n",
            capture_output=True,
            timeout=0.3,
            killpg=recording_killpg,
        )
    assert len(killed) == 1
    time.sleep(1.5)
    assert not marker.exists(), "the grandchild survived the timeout"


def test_notify_reports_a_real_timeout_from_the_default_runner(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("fantasy_sports.reports.notify.NOTIFY_TIMEOUT_SECONDS", 0.3)
    result = notify_irreversible(_entry(), config=_config("sleep 30"))
    assert result.ok is False and result.attempted is True
    assert result.error == "notify command timed out after 0.3s"


def test_the_example_script_keeps_its_log_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script = Path(__file__).resolve().parents[2] / "scripts" / "notify-irreversible-example.sh"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert notify_irreversible(_entry(), config=_config(str(script))).ok is True
    log = tmp_path / "state" / "fantasy-sports" / "irreversible.log"
    assert log.stat().st_mode & 0o077 == 0
    assert log.parent.stat().st_mode & 0o077 == 0
