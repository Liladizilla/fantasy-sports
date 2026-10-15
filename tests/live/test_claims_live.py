"""Live claim, ``claims``, ``cancel-claim``, ``claims``. **Writes**, but leaves nothing queued.

    FANTASY_SPORTS_ESPN_LEAGUE=<test league id> \\
    FANTASY_SPORTS_ESPN_WRITE_LEAGUE=<the same id> \\
        uv run pytest -m live tests/live/test_claims_live.py

The claim lifecycle (jwulff/fantasy-sports#109) in the throwaway test league
John joined for #108, never a real one. It needs the same second, deliberate
opt-in as ``test_moves_live.py``: ``FANTASY_SPORTS_ESPN_WRITE_LEAGUE`` must be
set **and equal** ``FANTASY_SPORTS_ESPN_LEAGUE``.

What it does, on the session's own team only:

1. Reads the team's queue and skips if anything is already queued there, so
   the test never cancels a claim it did not make.
2. Reads ``view=mPendingTransactions`` raw once, to record whether ESPN sends
   the ``pendingTransactions`` key for an empty queue (brief 06 could not
   tell).
3. Picks the lowest-``percent_owned`` player whose ESPN status is
   ``WAIVERS``. With room on the roster the claim drops nobody; on a full
   roster it names the unlocked bench player with the lowest projection.
4. ``claim`` (bidless: this league has no FAAB): asserts ``PENDING`` and a
   read-back verified by id.
5. ``claims``: the claim is listed by that id.
6. ``cancel-claim --id``: asserts ``CANCELED``, the cancel's own new id, the
   claim's id as ``cancelled_claim_id``, and a verified read-back.
7. ``claims``: the claim is gone.

A pending claim changes nothing until waivers process, so no roster moves.
The ``finally`` re-reads the queue, cancels any claim this test made that is
still there, and asserts the team's queue is empty: nothing is left pending,
whatever failed. No add and no drop is ever sent.

The journal and cache live in a temporary XDG tree; ``HOME`` is left alone so
the Keychain still supplies the cookies. Output is counts and flags only: no
team, player, manager, or transaction id is printed.
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
PROFILE = "live-claims"
WHY = "live test of #109 in the throwaway test league; cancelled before waivers process"
IR_SLOT = "21"


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


def _invoke(cli: CliRunner, args: list[str]) -> Any:
    return cli.invoke(build_app(), [*args, "--output", "json"])


def _json(cli: CliRunner, args: list[str]) -> Any:
    result = _invoke(cli, args)
    assert result.exit_code == 0, f"{args[:2]} -> {result.exit_code}: {result.stderr}"
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


def _queue(cli: CliRunner, team: str) -> list[dict[str, Any]]:
    return _json(cli, ["claims", "--team", team, "--no-cache"])["data"]


def _raw(cli: CliRunner, view: str) -> Any:
    return _json(cli, ["raw", "--view", view, "--no-cache"])["data"][view]["payload"]


def _roster_limit(cli: CliRunner) -> int | None:
    """Starters plus bench: ``lineupSlotCounts`` without IR (brief 06 §7)."""
    payload = _raw(cli, "mSettings")
    counts = ((payload.get("settings") or {}).get("rosterSettings") or {}).get("lineupSlotCounts")
    if not isinstance(counts, dict):
        return None
    return sum(int(n) for slot, n in counts.items() if str(slot) != IR_SLOT)


def _cheapest_bench(roster: list[dict[str, Any]]) -> dict[str, Any] | None:
    bench = [s for s in roster if s["slot"] == "BE" and s["is_locked"] is not True]
    return min(bench, key=lambda s: s["player"].get("projected_points") or 0.0, default=None)


def test_a_claim_is_listed_cancelled_and_gone(cli: CliRunner, target):
    team = _owned_team(target)
    if _queue(cli, team):
        pytest.skip("the team already has pending claims; this test cancels only its own")
    empty = _raw(cli, "mPendingTransactions")
    key_sent_when_empty = isinstance(empty, dict) and "pendingTransactions" in empty

    pool = _json(cli, ["free-agents", "--limit", "200", "--no-cache"])["data"]
    on_waivers = sorted(
        (fa for fa in pool if (fa.get("raw") or {}).get("status") == "WAIVERS"),
        key=lambda fa: fa.get("percent_owned") or 0.0,
    )
    if not on_waivers:
        pytest.skip("no player is on waivers, so there is nothing to claim")
    player = on_waivers[0]["provider_id"]
    roster = _json(cli, ["roster", "--team", team, "--no-cache"])["data"]
    limit = _roster_limit(cli)
    claim = ["claim", "--team", team, "--player", player, "--acknowledge-irreversible", WHY]
    with_drop = limit is None or len(roster) >= limit
    if with_drop:
        bench = _cheapest_bench(roster)
        if bench is None:
            pytest.skip("the roster is full and has no unlocked bench player to name as a drop")
        claim += ["--drop", bench["provider_id"]]

    made: list[str] = []
    try:
        result = _invoke(cli, claim)
        assert result.exit_code == 0, f"claim -> {result.exit_code}: {result.stderr}"
        claimed = json.loads(result.stdout)["data"]
        claim_id = claimed["provider_transaction_id"]
        made.append(claim_id)
        assert claimed["status"] == "PENDING" and claimed["is_pending"] is True
        assert claimed["cost"]["bid_amount"] is None, "a bidless claim sends no bid"
        assert claimed["read_back"]["verified"] is True
        assert claimed["read_back"]["matched_by"] == ["provider_transaction_id"]

        listed = _queue(cli, team)
        assert [c["provider_id"] for c in listed] == [claim_id]
        assert listed[0]["status"] == "PENDING"
        assert listed[0]["players_in"] == [player]

        cancelled = _json(cli, ["cancel-claim", "--team", team, "--id", claim_id])["data"]
        assert cancelled["kind"] == "cancel"
        assert cancelled["status"] == "CANCELED"
        assert cancelled["cancelled_claim_id"] == claim_id
        assert cancelled["provider_transaction_id"] not in (None, claim_id)
        assert cancelled["read_back"]["verified"] is True
        assert cancelled["notified"] is None

        assert _queue(cli, team) == []
    finally:
        leftover = [c for c in _queue(cli, team) if c["provider_id"] in made]
        for claim_row in leftover:
            _invoke(cli, ["cancel-claim", "--team", team, "--id", claim_row["provider_id"]])
        remaining = _queue(cli, team)
        assert remaining == [], f"{len(remaining)} claim(s) left pending on the test team"
    print(
        f"\nlive claims: 1 claim ({'with' if with_drop else 'without'} a drop), "
        f"PENDING, read-back verified={claimed['read_back']['verified']}; "
        f"claims listed {len(listed)}; cancel {cancelled['status']}, "
        f"read-back verified={cancelled['read_back']['verified']}; queue after: 0; "
        f"pendingTransactions key sent for an empty queue: {key_sent_when_empty}"
    )
