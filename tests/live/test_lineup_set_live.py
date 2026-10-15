"""Live swap-and-reverse through ``lineup set`` and ``journal reverse``. **Writes.**

    FANTASY_SPORTS_ESPN_LEAGUE=<test league id> \\
    FANTASY_SPORTS_ESPN_WRITE_LEAGUE=<the same id> \\
        uv run pytest -m live tests/live/test_lineup_set_live.py

The one live test in this project that sends a transaction (jwulff/fantasy-sports#15).
It must only ever run against the throwaway test league John joined for this:
a real-league write leaves a transaction row other managers see. So it needs a
second, deliberate opt-in: ``FANTASY_SPORTS_ESPN_WRITE_LEAGUE`` must be set
**and equal** ``FANTASY_SPORTS_ESPN_LEAGUE``. The read-only live suite uses the
first variable alone and may point it at a real league; that setting alone
never makes this test write.

What it does, on the session's own team only:

1. Snapshots the roster.
2. Finds one starter and one bench player, both unlocked, each eligible for
   the other's slot. No pair means a skip with the reason, never a guess.
3. Swaps them with ``lineup set`` and asserts the read-back verified and the
   journal entry executed.
4. Reverses it with ``journal reverse`` and asserts the roster matches the
   snapshot slot for slot. The reversal runs in a ``finally`` once the swap
   has been journaled, so a failed assertion does not leave the swap behind.

The journal and cache live in a temporary XDG tree; ``HOME`` is left alone so
the Keychain still supplies the cookies. Assertions are structural: no team,
player, or manager is named.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fantasy_sports.auth.chain import ESPN_CREDENTIALS, resolve_credentials
from fantasy_sports.cli.app import build_app

pytestmark = pytest.mark.live

LEAGUE_ENV = "FANTASY_SPORTS_ESPN_LEAGUE"
WRITE_ENV = "FANTASY_SPORTS_ESPN_WRITE_LEAGUE"
SEASON_ENV = "FANTASY_SPORTS_ESPN_SEASON"
PROFILE = "live-writes"


@pytest.fixture(autouse=True)
def _reach_the_network() -> None:
    """Undo ``--disable-socket`` for this module. See ``test_espn_live.py``."""
    from pytest_socket import enable_socket

    enable_socket()


@pytest.fixture(scope="module")
def target() -> tuple[str, int]:
    league_id = os.environ.get(LEAGUE_ENV)
    if not league_id or not resolve_credentials(ESPN_CREDENTIALS).complete:
        pytest.skip(f"needs ESPN cookies and {LEAGUE_ENV}")
    if os.environ.get(WRITE_ENV) != league_id:
        pytest.skip(f"writes need {WRITE_ENV} set to the same throwaway league as {LEAGUE_ENV}")
    return league_id, int(os.environ.get(SEASON_ENV, "2026"))


@pytest.fixture
def cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: tuple[str, int]
) -> Iterator[CliRunner]:
    league_id, season = target
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    config = tmp_path / "xdg_config_home" / "fantasy-sports"
    config.mkdir(parents=True)
    (config / "config.toml").write_text(
        f'default = "{PROFILE}"\n\n'
        f"[leagues.{PROFILE}]\n"
        'provider = "espn"\n'
        f'league_id = "{league_id}"\n'
        f"season = {season}\n",
        encoding="utf-8",
    )
    yield CliRunner()


def _json(cli: CliRunner, args: list[str]) -> dict[str, Any]:
    result = cli.invoke(build_app(), [*args, "--output", "json"])
    assert result.exit_code == 0, f"{args[:3]} -> {result.exit_code}: {result.stderr}"
    return json.loads(result.stdout)


def _owned_team(target: tuple[str, int]) -> str:
    from fantasy_sports.providers.espn import EspnProvider

    def _no_writes(*_: object, **__: object) -> object:
        raise AssertionError("ownership lookup must never POST")

    provider = EspnProvider(resolve_credentials(ESPN_CREDENTIALS), http_post=_no_writes)
    owned = provider.session_role(*target).owned_team_ids
    if not owned:
        pytest.skip("the session owns no team in the test league")
    return owned[0]


def _snapshot(cli: CliRunner, team: str) -> dict[str, str]:
    roster = _json(cli, ["roster", "--team", team, "--no-cache"])["data"]
    return {slot["provider_id"]: slot["slot"] for slot in roster}


def _swappable_pair(roster: list[dict[str, Any]]) -> tuple[dict, dict] | None:
    """A starter and a bench player, both unlocked, each eligible for the other's slot."""
    starters = [
        s for s in roster if s["is_starter"] and s["is_locked"] is False and s["slot"] != "IR"
    ]
    bench = [s for s in roster if s["slot"] == "BE" and s["is_locked"] is False]
    for starter in starters:
        for reserve in bench:
            if (
                starter["slot"] in reserve["player"]["eligible_slots"]
                and "BE" in (starter["player"]["eligible_slots"])
            ):
                return starter, reserve
    return None


def test_a_swap_is_read_back_and_reversed_to_the_exact_prior_lineup(cli: CliRunner, target):
    team = _owned_team(target)
    roster = _json(cli, ["roster", "--team", team, "--no-cache"])["data"]
    if not roster:
        pytest.skip("the test league's roster is empty (draft not finished?)")
    before = {slot["provider_id"]: slot["slot"] for slot in roster}
    pair = _swappable_pair(roster)
    if pair is None:
        pytest.skip("no unlocked starter/bench pair with mutually eligible slots")
    starter, reserve = pair

    swap = _json(
        cli,
        [
            "lineup",
            "set",
            "--team",
            team,
            "--set",
            f"{starter['provider_id']}=BE",
            "--set",
            f"{reserve['provider_id']}={starter['slot']}",
        ],
    )["data"]
    reversed_ = False
    try:
        assert swap["status"] == "EXECUTED"
        assert swap["provider_transaction_id"]
        assert swap["read_back"] == {"verified": True, "diverged": []}
        assert len(swap["items"]) == 2

        from fantasy_sports.core.journal import Journal

        with Journal.open() as journal:
            entry = journal.get(swap["journal_entry_id"])
        assert entry is not None and entry.status == "executed"
        assert entry.provider_transaction_id == swap["provider_transaction_id"]
        assert entry.member_id is not None

        undo = _json(cli, ["journal", "reverse", "--id", swap["journal_entry_id"]])["data"]
        reversed_ = True
        assert undo["kind"] == "reverse"
        assert undo["reverses"] == swap["journal_entry_id"]
        assert undo["read_back"] == {"verified": True, "diverged": []}
        assert undo["provider_transaction_id"] != swap["provider_transaction_id"]
        assert _snapshot(cli, team) == before
    finally:
        if not reversed_:
            cli.invoke(
                build_app(),
                ["journal", "reverse", "--id", swap["journal_entry_id"], "--output", "json"],
            )
    print(
        f"\nlive lineup set: team {team}, 2 transactions "
        f"({swap['status']}, then reverse), read-back verified both times, "
        "roster restored slot for slot"
    )
