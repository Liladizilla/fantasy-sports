"""Live add-with-drop, then a drop, through ``add`` and ``drop``. **Writes, irreversibly.**

    FANTASY_SPORTS_ESPN_LEAGUE=<test league id> \\
    FANTASY_SPORTS_ESPN_WRITE_LEAGUE=<the same id> \\
        uv run pytest -m live tests/live/test_moves_live.py

The first live use of the irreversible commands (jwulff/fantasy-sports#18) is
in the throwaway test league John joined for this (#108), never a real one: a
drop there puts a real player on waivers for real managers. So this needs the
same second, deliberate opt-in as ``test_lineup_set_live.py``:
``FANTASY_SPORTS_ESPN_WRITE_LEAGUE`` must be set **and equal**
``FANTASY_SPORTS_ESPN_LEAGUE``. The read-only live suite sets the first alone
and may point it at a real league; that never makes this test write.

What it does, on the session's own team only, mirroring probes F2-F7 in
``docs/research/06-espn-add-drop-surface.md`` §6:

1. Snapshots the roster and reads this week's free-agent pool.
2. Picks the two lowest-``percent_owned`` players whose ESPN pool status is
   ``FREEAGENT`` (outright adds, no waiver claim), and the bench player with
   the lowest projection who is not locked. Anything missing means a skip
   with the reason, never a guess: a pool that is all on waivers (true until
   the test league's waivers clear) has nothing ``add`` can take.
3. ``add --player FA1 --drop BENCH``: asserts ``EXECUTED``, a verified
   read-back, the journal entry ``executed`` with ``reversible: false`` and
   the displaced player as its cost, and a roster the same size as before.
4. ``drop --player FA1``: asserts the read-back verified and FA1 gone.
5. ``add --player FA2 --slot BE`` (F7): the roster ends at its starting size.

Steps 3 onward run inside one ``try``. Whatever fails, including the add
itself returning an error after ESPN may have applied it (``WRITE_DIVERGED``,
a timeout), the ``finally`` re-reads the roster and cleans up from what it
actually shows: FA1 still held is dropped, and a roster left short gets FA2
on the bench. It never trusts the failed command's own account.

Net effect on the throwaway team: the roster is back to its starting size
with FA2 in place of BENCH. BENCH is on 24-hour waivers and is **not**
restored, and FA1 is on waivers too. Neither can be undone, which is the
point of the test and why it runs only in the test league.

The journal and cache live in a temporary XDG tree; ``HOME`` is left alone so
the Keychain still supplies the cookies. Assertions are structural: no team,
player, or manager is named in output.
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
WHY = "live test of #18 in the throwaway test league; no real league is touched"


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


def _roster(cli: CliRunner, team: str) -> list[dict[str, Any]]:
    return _json(cli, ["roster", "--team", team, "--no-cache"])["data"]


def _outright_free_agents(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Players ESPN lists as free agents rather than on waivers, least owned first."""
    outright = [fa for fa in pool if (fa.get("raw") or {}).get("status") == "FREEAGENT"]
    return sorted(outright, key=lambda fa: fa.get("percent_owned") or 0.0)


def _cheapest_bench(roster: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The unlocked bench player with the lowest projection."""
    bench = [s for s in roster if s["slot"] == "BE" and s["is_locked"] is not True]
    return min(bench, key=lambda s: s["player"].get("projected_points") or 0.0, default=None)


def _invoke(cli: CliRunner, args: list[str]) -> Any:
    return cli.invoke(build_app(), [*args, "--output", "json"])


def test_an_add_with_a_drop_is_read_back_then_undone_down_to_roster_size(cli: CliRunner, target):
    team = _owned_team(target)
    before = _roster(cli, team)
    if not before:
        pytest.skip("the test league's roster is empty (draft not finished?)")
    pool = _outright_free_agents(
        _json(cli, ["free-agents", "--limit", "200", "--no-cache"])["data"]
    )
    if len(pool) < 2:
        pytest.skip("fewer than two players have ESPN status FREEAGENT; waivers have not cleared")
    bench = _cheapest_bench(before)
    if bench is None:
        pytest.skip("no unlocked bench player to drop")
    first, second = pool[0]["provider_id"], pool[1]["provider_id"]
    dropped = bench["provider_id"]

    try:
        result = _invoke(
            cli,
            [
                "add",
                "--team",
                team,
                "--player",
                first,
                "--drop",
                dropped,
                "--acknowledge-irreversible",
                WHY,
            ],
        )
        assert result.exit_code == 0, f"add -> {result.exit_code}: {result.stderr}"
        move = json.loads(result.stdout)["data"]
        assert move["status"] == "EXECUTED"
        assert move["is_pending"] is False
        assert move["provider_transaction_id"]
        assert move["reversible"] is False
        assert move["acknowledged_irreversible"] == WHY
        assert move["cost"] == {"bid_amount": None, "displaced_player_id": dropped}
        assert move["read_back"] == {"verified": True, "diverged": []}

        from fantasy_sports.core.journal import Journal

        with Journal.open() as journal:
            entry = journal.get(move["journal_entry_id"])
        assert entry is not None and entry.status == "executed"
        assert entry.reversible is False
        assert entry.provider_transaction_id == move["provider_transaction_id"]

        after_add = {slot["provider_id"] for slot in _roster(cli, team)}
        assert first in after_add and dropped not in after_add
        assert len(after_add) == len(before)

        release = _json(
            cli, ["drop", "--team", team, "--player", first, "--acknowledge-irreversible", WHY]
        )["data"]
        assert release["read_back"] == {"verified": True, "diverged": []}

        refill = _json(
            cli,
            [
                "add",
                "--team",
                team,
                "--player",
                second,
                "--slot",
                "BE",
                "--acknowledge-irreversible",
                f"{WHY} (F7: restore the roster size)",
            ],
        )["data"]
        assert refill["read_back"] == {"verified": True, "diverged": []}
        final = {slot["provider_id"]: slot["slot"] for slot in _roster(cli, team)}
        assert len(final) == len(before)
        assert final[second] == "BE"
        assert first not in final and dropped not in final
    finally:
        _restore(cli, team, before_size=len(before), first=first, second=second)
    print(
        f"\nlive add/drop: team {team}, 3 transactions (add with drop, drop, add to BE), "
        "read-back verified each time, roster size restored"
    )


def _restore(cli: CliRunner, team: str, *, before_size: int, first: str, second: str) -> None:
    """Clean up from what the roster shows now, not from what a command reported."""
    held = {slot["provider_id"] for slot in _roster(cli, team)}
    if first in held:
        _invoke(
            cli,
            [
                "drop",
                "--team",
                team,
                "--player",
                first,
                "--acknowledge-irreversible",
                f"{WHY} (cleanup)",
            ],
        )
        held = {slot["provider_id"] for slot in _roster(cli, team)}
    if len(held) < before_size and second not in held:
        _invoke(
            cli,
            [
                "add",
                "--team",
                team,
                "--player",
                second,
                "--slot",
                "BE",
                "--acknowledge-irreversible",
                f"{WHY} (cleanup: restore the roster size)",
            ],
        )
