"""The offline harness the command and CLI tests share.

It lives beside ``conftest.py`` rather than inside it because the cassette
scrub hooks in that file are a *security control* with its own owner
(jwulff/fantasy-sports#12), and this is ordinary test scaffolding. Both
``tests/unit/test_commands.py`` and ``tests/integration/test_cli.py`` import
from here; ``tests/`` is on ``sys.path`` for the same reason ``from conftest
import ...`` works.

**Why a stub on ``requests.get`` rather than vcrpy.** ``transactions`` walks
scoring periods backward and asks ``mTransactions2`` about periods the fixture
never recorded. A cassette can only answer "miss", but the honest upstream
answer is a **200 with no transactions in it** — a quiet week is a successful
empty result, not a failure
(``docs/memory/espn-api-is-a-shape-reader-not-a-client.md``). The stub
synthesises exactly that, refuses everything else, and counts requests, which
is the only way the upstream-call cap can be asserted at all.

``pytest-socket`` is on, so anything this stub does not answer fails loudly
rather than quietly reaching ESPN.
"""

from __future__ import annotations

import json
import json as json_module
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

CASSETTE = "espn/synthetic_2026.yaml"
LEAGUE_ID = "99"
SEASON = 2026

#: Synthetic, and shaped like the real thing so the normalizer and the
#: credential scrubber behave as they would in production. Neither value is a
#: real credential, and neither may ever be replaced by one.
FAKE_S2 = "AEB" + "q" * 200
FAKE_SWID = "{1A2B3C4D-5E6F-7A8B-9C0D-1E2F3A4B5C6D}"

CONFIG_TOML = (
    'default = "synthetic"\n'
    "\n"
    "[leagues.synthetic]\n"
    'provider = "espn"\n'
    f'league_id = "{LEAGUE_ID}"\n'
    f"season = {SEASON}\n"
    "\n"
    "[leagues.broken]\n"
    'provider = "nonesuch"\n'
    'league_id = "1"\n'
    "season = 2026\n"
)


class FakeResponse:
    """The three attributes the adapter's transport reads off a response."""

    def __init__(self, body: Any, status_code: int = 200) -> None:
        self.status_code = status_code
        self._body = body
        self.headers: Mapping[str, str] = {}

    @property
    def content(self) -> bytes:
        if isinstance(self._body, bytes):
            return self._body
        if isinstance(self._body, str):
            return self._body.encode()
        return json.dumps(self._body).encode()

    def json(self) -> Any:
        return json.loads(self.content)


def query_key(url: str, extra: list[tuple[str, str]] | None = None) -> tuple[Any, ...]:
    """A request's identity: its path and its query pairs, order-insensitive."""
    parts = urlsplit(url)
    pairs = list(parse_qsl(parts.query)) + list(extra or [])
    return (parts.path, tuple(sorted(pairs)))


class RecordedEspn:
    """Serves a committed cassette, and answers a quiet scoring period honestly.

    ``overrides`` substitutes one view's payload for every request of it,
    whatever the scoring period. A *payload* variation on an otherwise healthy
    league -- a view ESPN shipped without a key it usually carries -- belongs
    next to the assertion that needs it, not in a second cassette to keep in
    step with the first.
    """

    def __init__(self, name: str = CASSETTE, overrides: Mapping[str, Any] | None = None) -> None:
        import yaml
        from conftest import CASSETTE_LIBRARY_DIR

        document = yaml.safe_load(Path(CASSETTE_LIBRARY_DIR / name).read_text())
        self.bodies: dict[tuple[Any, ...], str] = {}
        self.by_view: dict[str, str] = {}
        self.overrides = dict(overrides or {})
        for interaction in document["interactions"]:
            uri = interaction["request"]["uri"]
            body = interaction["response"]["body"]["string"]
            self.bodies[query_key(uri)] = body
            views = [value for key, value in parse_qsl(urlsplit(uri).query) if key == "view"]
            if len(views) == 1:
                self.by_view.setdefault(views[0], body)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url, params=None, headers=None, cookies=None) -> FakeResponse:
        pairs: list[tuple[str, str]] = []
        for name, value in (params or {}).items():
            values = value if isinstance(value, list | tuple) else [value]
            pairs.extend((str(name), str(item)) for item in values)
        flat = dict(pairs)
        self.calls.append({"url": url, "params": flat, "headers": dict(headers or {})})

        if flat.get("view") in self.overrides:
            return FakeResponse(self.overrides[flat["view"]])
        body = self.bodies.get(query_key(url, pairs))
        if body is None and len([v for k, v in pairs if k == "view"]) == 1:
            # `raw` asks for one view with no scoring period; the fixture
            # recorded some of the same views with one. Same payload.
            body = self.by_view.get(flat.get("view", ""))
        if body is not None:
            return FakeResponse(body)
        if flat.get("view") == "mTransactions2":
            # A period nobody traded in. `espn-api` turns the absent key into a
            # bare `Exception('No transactions found')`, which the adapter
            # reads as the successful empty result it is.
            return FakeResponse({"id": int(LEAGUE_ID), "seasonId": SEASON})
        raise AssertionError(f"no recorded response for {url} {flat}")

    def views(self) -> list[str]:
        """Every ``view=`` requested, in order, with its scoring period."""
        found = []
        for call in self.calls:
            view = call["params"].get("view", "?")
            period = call["params"].get("scoringPeriodId")
            found.append(view if period is None else f"{view}@{period}")
        return found


def install_espn(
    monkeypatch: Any, name: str = CASSETTE, overrides: Mapping[str, Any] | None = None
) -> RecordedEspn:
    """Route the adapter's transport at the recorded fixture."""
    import requests

    http = RecordedEspn(name, overrides)
    monkeypatch.setattr(requests, "get", http)
    return http


def isolate_home(tmp_path: Path, monkeypatch: Any) -> Path:
    """A private XDG tree, a synthetic credential pair, and a config file.

    The environment variables matter twice over. They are the first link of the
    credential chain, so the Keychain — which on a developer's machine holds
    John's **real** ESPN cookies under this exact service name — is never
    reached by a test. And clearing the XDG variables is what stops a
    developer's own ``config.toml`` and response cache from leaking into the
    run (``docs/memory/config-toml-is-a-shared-namespace.md``).
    """
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("FANTASY_SPORTS_ESPN_S2", FAKE_S2)
    monkeypatch.setenv("FANTASY_SPORTS_SWID", FAKE_SWID)

    config = tmp_path / "config" / "fantasy-sports"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.toml").write_text(CONFIG_TOML, encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------- #
# Writes (jwulff/fantasy-sports#15)
# --------------------------------------------------------------------------- #

#: Before every week-2 kickoff in the synthetic league (the earliest is
#: 2026-09-13 17:00 UTC), so no synthetic player reads as locked.
UNLOCKED_AT = "2026-09-10T12:00:00+00:00"

WRITE_CAPTURES = Path(__file__).resolve().parents[1] / "docs" / "research" / "05-espn-write-surface"
ADD_DROP_CAPTURES = WRITE_CAPTURES.parent / "06-espn-add-drop-surface"

#: Fixed so a write's success payload is byte-stable across runs.
TRANSACTION_ID = "5d0c6e2a-7f41-4b9e-a3c8-1e6f2b9d4a70"
PROPOSED_DATE_MS = 1789041601000  # 2026-09-10T12:00:01Z, a second after UNLOCKED_AT


def writable_nav(swid: str = FAKE_SWID) -> dict[str, Any]:
    """An ``mNav`` in which ``swid`` founded the league and owns team 1.

    No cassette records ``mNav``; brief §7 is its shape.
    """
    return {
        "members": [
            {"id": swid, "isLeagueCreator": True, "isLeagueManager": False},
            {"id": "{OWNER-BRAVO}", "isLeagueCreator": False, "isLeagueManager": False},
        ],
        "teams": [{"id": 1, "owners": [swid]}, {"id": 2, "owners": ["{OWNER-BRAVO}"]}],
    }


class WritableEspn(RecordedEspn):
    """The synthetic league, plus a write host that changes what reads return.

    Install with :func:`install_writable_espn`. Reads replay the cassette
    exactly as :class:`RecordedEspn` does, except that the bootstrap's rosters
    (and the adapter's standalone ``mRoster``), the free-agent pool
    (``kona_player_info``), and the pending claims in ``mTransactions2``
    reflect every POST answered ``"apply"``. ``mode`` picks the answer to the
    next POST:

    * ``"apply"`` — 200, shaped like ``p3-swap-execute``, and the body takes
      effect. A ``ROSTER`` body moves slots; a ``FREEAGENT`` body moves its
      ``ADD`` players from the pool onto the roster and its ``DROP`` players
      onto waivers, ``EXECUTED``; a ``WAIVER`` body is answered ``PENDING``
      with ``isPending: true`` and queued as a pending transaction, and the
      roster does not change; a ``CANCEL`` withdraws the queued claim it names.
    * ``"ignore"`` — the same 200, but nothing changes, so a read-back diverges;
    * ``"reject"`` — a 409. By default ``p8-locked-player-move``'s
      ``TRAN_LINEUP_LOCKED``; ``reject_capture`` names a capture in
      ``06-espn-add-drop-surface/`` to replay instead (#108), and
      ``reject_type`` substitutes another ``TRAN_*`` type in the p8 body.

    A drops-only body arrives as ``ROSTER`` with ``DROP`` items and is applied
    by item type, as ESPN does (brief 06). The success answers follow brief
    06's captures in shape: a claim is ``PENDING`` with ``bidAmount`` echoed
    (``0`` when none was sent). ``posts`` records every request body sent. ``echo_member`` replaces
    the ``memberId`` ESPN echoes, to play a write applied as another account.
    ``show_pending`` off hides queued claims from ``mTransactions2``, as an
    ESPN that did not list them would.
    """

    #: The free-agent pool the move tests resolve against: one free agent per
    #: position the synthetic roster can use, and one player on waivers.
    POOL: tuple[tuple[int, str, str, str, int, float], ...] = (
        # id, first, last, pool status, defaultPositionId, percentOwned
        (3001, "Barbara", "Liskov", "FREEAGENT", 4, 61.5),
        (3002, "Radia", "Perlman", "WAIVERS", 6, 40.0),
        (3003, "Frances", "Allen", "FREEAGENT", 2, 12.0),
        (3004, "Frances", "Spence", "FREEAGENT", 4, 3.5),
    )

    def __init__(
        self,
        *,
        swid: str = FAKE_SWID,
        mode: str = "apply",
        echo_member: str | None = None,
        reject_type: str | None = None,
        reject_capture: str | None = None,
        show_pending: bool = True,
    ) -> None:
        super().__init__()
        self.mode = mode
        self.echo_member = echo_member
        self.reject_type = reject_type
        self.reject_capture = reject_capture
        self.show_pending = show_pending
        self.posts: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self._bootstrap_key = next(
            key
            for key in self.bodies
            if ("view", "mTeam") in key[1] and ("view", "mRoster") in key[1]
        )
        self.overrides["mNav"] = writable_nav(swid)
        self.pool = self._initial_pool()
        self.overrides["kona_player_info"] = {"players": self.pool}
        self._publish(json.loads(self.bodies[self._bootstrap_key]))

    def _initial_pool(self) -> list[dict[str, Any]]:
        recorded = next(
            json.loads(body)["players"][0]
            for key, body in self.bodies.items()
            if ("view", "kona_player_info") in key[1]
        )
        pool = []
        for player_id, first, last, status, position, owned in self.POOL:
            entry = _json_copy(recorded)
            entry["id"] = player_id
            entry["status"] = status
            player = entry["player"]
            player.update(
                id=player_id,
                firstName=first,
                lastName=last,
                fullName=f"{first} {last}",
                defaultPositionId=position,
            )
            player["ownership"]["percentOwned"] = owned
            if position == 2:  # RB
                player["eligibleSlots"] = [2, 3, 23, 20, 21]
            elif position == 6:  # TE
                player["eligibleSlots"] = [5, 6, 23, 20, 21]
            pool.append(entry)
        return pool

    def _publish(self, bootstrap: dict[str, Any]) -> None:
        self.bootstrap = bootstrap
        self.bodies[self._bootstrap_key] = json.dumps(bootstrap)
        # The adapter diffs against a standalone current-period `mRoster`; the
        # bootstrap carries the same `teams[].roster` shape.
        self.overrides["mRoster"] = bootstrap
        self.overrides["kona_player_info"] = {"players": self.pool}
        if self.show_pending and self.pending:
            self.overrides["mTransactions2"] = {"transactions": list(self.pending)}
        else:
            self.overrides.pop("mTransactions2", None)

    def slots(self, team_id: int = 1) -> dict[str, int]:
        """``{player_id: lineupSlotId}`` for one team, as the next read will see it."""
        team = next(t for t in self.bootstrap["teams"] if t["id"] == team_id)
        return {str(e["playerId"]): e["lineupSlotId"] for e in team["roster"]["entries"]}

    def pool_ids(self) -> dict[str, str]:
        """``{player_id: pool status}`` as the next free-agent read will see it."""
        return {str(entry["id"]): entry["status"] for entry in self.pool}

    def post(self, url: str, json: Any = None, **_: Any) -> FakeResponse:  # noqa: A002
        body = json if json is not None else {}
        self.posts.append(body)
        if self.mode == "reject" and self.reject_capture is not None:
            capture = json_module.loads(
                (ADD_DROP_CAPTURES / f"{self.reject_capture}.json").read_text(encoding="utf-8")
            )
            return FakeResponse(capture["response"]["body"], status_code=409)
        if self.mode == "reject":
            capture = _load_capture("p8-locked-player-move")
            answer = _json_copy(capture["response"]["body"])
            if self.reject_type is not None:
                for detail in answer.get("details", []):
                    detail["type"] = self.reject_type
            return FakeResponse(answer, status_code=409)
        kind = body.get("type", "ROSTER")
        execution = body.get("executionType", "EXECUTE")
        status, pending = "EXECUTED", False
        if kind == "WAIVER" and execution == "EXECUTE":
            status, pending = "PENDING", True
        if execution == "CANCEL":
            status = "CANCELED"
        if self.mode == "apply":
            self._apply(body, kind, execution)
        return FakeResponse(
            {
                "bidAmount": body.get("bidAmount", 0),
                "executionType": execution,
                "id": TRANSACTION_ID,
                "isActingAsTeamOwner": False,
                "isLeagueManager": False,
                "isPending": pending,
                "items": [self._echo(item) for item in body.get("items", [])],
                "memberId": self.echo_member or self.overrides["mNav"]["members"][0]["id"],
                "proposedDate": PROPOSED_DATE_MS,
                "rating": 0,
                "scoringPeriodId": body.get("scoringPeriodId"),
                "skipTransactionCounters": False,
                "status": status,
                "subOrder": 0,
                "teamId": body.get("teamId"),
                "type": kind,
            }
        )

    @staticmethod
    def _echo(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "fromLineupSlotId": item.get("fromLineupSlotId", -1),
            "fromTeamId": item.get("fromTeamId", 0),
            "isKeeper": False,
            "overallPickNumber": 0,
            "playerId": item["playerId"],
            "toLineupSlotId": item.get("toLineupSlotId", -1),
            "toTeamId": item.get("toTeamId", 0),
            "type": item.get("type", "LINEUP"),
        }

    def _apply(self, body: Mapping[str, Any], kind: str, execution: str) -> None:
        if execution == "CANCEL":
            related = body.get("relatedTransactionId")
            self.pending = [t for t in self.pending if t.get("id") != related]
            self._publish(self.bootstrap)
            return
        if kind == "WAIVER":
            self.pending.append(
                {
                    "id": TRANSACTION_ID,
                    "teamId": body.get("teamId"),
                    "type": "WAIVER",
                    "status": "PENDING",
                    "isPending": True,
                    "scoringPeriodId": body.get("scoringPeriodId"),
                    "proposedDate": PROPOSED_DATE_MS,
                    "bidAmount": body.get("bidAmount", 0),
                    "items": [
                        {"playerId": i["playerId"], "type": i.get("type")}
                        for i in body.get("items", [])
                    ],
                }
            )
            self._publish(self.bootstrap)
            return
        updated = _json_copy(self.bootstrap)
        team = next(t for t in updated["teams"] if t["id"] == body.get("teamId"))
        entries = team["roster"]["entries"]
        for item in body.get("items", []):
            player_id = item["playerId"]
            if item.get("type") == "ADD":
                pooled = next(e for e in self.pool if e["id"] == player_id)
                self.pool = [e for e in self.pool if e["id"] != player_id]
                entries.append(_roster_entry(pooled, team["id"], item.get("toLineupSlotId", 20)))
            elif item.get("type") == "DROP":
                held = next(e for e in entries if e["playerId"] == player_id)
                entries.remove(held)
                released = _json_copy(held["playerPoolEntry"])
                released.update(status="WAIVERS", onTeamId=0)
                self.pool.append(released)
            else:
                next(e for e in entries if e["playerId"] == player_id)["lineupSlotId"] = item[
                    "toLineupSlotId"
                ]
        self._publish(updated)


def _roster_entry(pooled: Mapping[str, Any], team_id: int, slot: int) -> dict[str, Any]:
    """A pool entry as ``mRoster`` carries it once the player is on a team."""
    entry = _json_copy(pooled)
    entry.update(status="ONTEAM", onTeamId=team_id)
    return {
        "acquisitionDate": PROPOSED_DATE_MS,
        "acquisitionType": "ADD",
        "injuryStatus": entry["player"].get("injuryStatus", "ACTIVE"),
        "lineupSlotId": slot,
        "pendingTransactionIds": None,
        "playerId": entry["id"],
        "playerPoolEntry": entry,
        "status": "NORMAL",
    }


def _load_capture(name: str) -> dict[str, Any]:
    return json.loads((WRITE_CAPTURES / f"{name}.json").read_text(encoding="utf-8"))


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def install_writable_espn(monkeypatch: Any, **options: Any) -> WritableEspn:
    """Route reads and writes at a :class:`WritableEspn`, with the clock fixed
    before kickoff so nothing reads as locked."""
    from datetime import datetime

    import requests

    http = WritableEspn(**options)
    monkeypatch.setattr(requests, "get", http)
    monkeypatch.setattr(requests, "post", http.post)
    monkeypatch.setattr(
        "fantasy_sports.providers.espn.utc_now", lambda: datetime.fromisoformat(UNLOCKED_AT)
    )
    return http
