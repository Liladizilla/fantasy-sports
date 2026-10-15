#!/usr/bin/env python
"""Probe ESPN's add/drop/waiver write surface in a throwaway league, one request at a time.

    uv run python scripts/probe_espn_add_drop.py --league test role
    uv run python scripts/probe_espn_add_drop.py --league test readback p0
    uv run python scripts/probe_espn_add_drop.py --league test move p1-fa-add --add 123 --note "..."
    uv run python scripts/probe_espn_add_drop.py --league test move p4-waiver --add 123 --waiver
    uv run python scripts/probe_espn_add_drop.py --league test pool       # FREEAGENT vs WAIVERS
    uv run python scripts/probe_espn_add_drop.py --league test cancel p5-cancel --related <uuid>
    uv run python scripts/probe_espn_add_drop.py --league test raw p7-x --body '{"type": ...}'

Written for jwulff/fantasy-sports#108. **This script sends real write requests
to ESPN**, so it is built to make the wrong league unreachable rather than
merely unlikely:

* ``--league`` must be the config alias ``test`` -- the throwaway public league
  John joined for this. Any other alias is refused before anything is read.
* Every POST passes through :func:`_guarded_post`, which refuses unless the URL
  names the test league's id and no other configured league's id, the body's
  ``teamId`` is one the session owns (re-derived from ``mNav`` on every run),
  and every ``ADD``/``DROP`` item that names a team names that one.
* At least :data:`MIN_GAP_SECONDS` between writes, enforced across
  invocations through a state file. One write per invocation; there is no
  loop.

``move`` and ``cancel`` go through :class:`EspnProvider`'s own
``move_players`` / ``cancel_transaction`` so the shape under test is the one
the code encodes. ``raw`` sends a hand-built body, for the shape variations the
provider cannot express (an omitted ``toTeamId``, an alternative ``type`` on a
cancel); ``teamId`` and ``scoringPeriodId`` are filled in by the script, never
by the caller.

Captures land in ``tests/cassettes/private/108/`` (gitignored): request and
response per probe, with the cookie line redacted and every credential pattern
scrubbed from the bodies. They still name the real league, its players and its
transaction ids, so they are **never** committed; ``redact`` rewrites them into
``docs/research/06-espn-add-drop-surface/`` the way the 05 captures were.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

ALIAS = "test"
CONFIG = Path.home() / ".config" / "fantasy-sports" / "config.toml"
PRIVATE = REPO / "tests" / "cassettes" / "private" / "108"
STATE = PRIVATE / "state.json"
MIN_GAP_SECONDS = 5.0
TRANSACTION_TYPES = [
    "FREEAGENT",
    "WAIVER",
    "WAIVER_ERROR",
    "ROSTER",
    "DRAFT",
    "TRADE_ACCEPT",
    "TRADE_UPHOLD",
    "TRADE_VETO",
    "TRADE_DECLINE",
    "TRADE_PROPOSAL",
    "FUTURE_ROSTER",
    "RETRO_ROSTER",
]


class Refused(SystemExit):
    def __init__(self, why: str) -> None:
        super().__init__(f"REFUSED: {why}")


# --------------------------------------------------------------------------- #
# The league, and only the league
# --------------------------------------------------------------------------- #


def league_ids(alias: str) -> tuple[str, int, frozenset[str]]:
    """The test league's id and season, plus every *other* configured league id."""
    if alias != ALIAS:
        raise Refused(f"--league must be {ALIAS!r}; this script writes to the test league only")
    config = tomllib.loads(CONFIG.read_text())
    leagues = config.get("leagues", {})
    entry = leagues.get(ALIAS)
    if not entry:
        raise Refused(f"no [leagues.{ALIAS}] in {CONFIG}")
    others = frozenset(
        str(value["league_id"])
        for name, value in leagues.items()
        if name != ALIAS and isinstance(value, dict) and "league_id" in value
    )
    league = str(entry["league_id"])
    if league in others:
        raise Refused("the test league's id is also configured as another league")
    return league, int(entry["season"]), others


def _check_url(url: str, league: str, others: frozenset[str]) -> None:
    if f"/leagues/{league}/" not in url:
        raise Refused("write URL does not name the test league")
    for other in others:
        if f"/leagues/{other}/" in url or other in url:
            raise Refused("write URL names a non-test league")


def _check_body(body: dict[str, Any], owned: tuple[str, ...]) -> None:
    team = str(body.get("teamId"))
    if team not in owned:
        raise Refused(f"body teamId {team} is not a team this session owns ({owned})")
    for item in body.get("items") or []:
        for key in ("toTeamId", "fromTeamId"):
            if key in item and str(item[key]) not in owned:
                raise Refused(f"item {key}={item[key]} is not this session's team")


# --------------------------------------------------------------------------- #
# State across invocations
# --------------------------------------------------------------------------- #


def _state() -> dict[str, Any]:
    return json.loads(STATE.read_text()) if STATE.exists() else {"writes": []}


def _save_state(state: dict[str, Any]) -> None:
    PRIVATE.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def _wait_for_gap(state: dict[str, Any]) -> None:
    if not state["writes"]:
        return
    last = datetime.fromisoformat(state["writes"][-1]["at"])
    gap = (datetime.now(UTC) - last).total_seconds()
    if gap < MIN_GAP_SECONDS:
        time.sleep(MIN_GAP_SECONDS - gap)


# --------------------------------------------------------------------------- #
# Credentials and the provider
# --------------------------------------------------------------------------- #


def _credentials() -> Any:
    from fantasy_sports.auth.chain import ESPN_CREDENTIALS, resolve_credentials

    resolved = resolve_credentials(ESPN_CREDENTIALS)
    if not resolved.complete:
        raise Refused(f"credentials missing: {', '.join(resolved.missing)}")
    return resolved


def _scrubbed(text: str | bytes) -> Any:
    from fantasy_sports.core.redaction import CASSETTE_SWID_SALT, scrub_body

    try:
        return json.loads(scrub_body(text, swid_salt=CASSETTE_SWID_SALT))
    except (ValueError, TypeError):
        return {"unreadable": True}


class Recorder:
    """``http_post`` for the provider: guards, sends once, and records."""

    def __init__(self, name: str, note: str, league: str, others: frozenset[str], owned):
        self.name, self.note = name, note
        self.league, self.others, self.owned = league, others, owned
        self.capture: dict[str, Any] | None = None

    def __call__(self, url: str, *, json: dict[str, Any], headers, cookies, timeout):  # noqa: A002
        import requests

        _check_url(url, self.league, self.others)
        _check_body(json, self.owned)
        if set(cookies) != {"espn_s2"}:
            raise Refused("only espn_s2 may be sent")
        import fcntl

        PRIVATE.mkdir(parents=True, exist_ok=True)
        # One writer at a time across processes: the gap check, the
        # reservation, and the send happen under one exclusive lock.
        with (PRIVATE / "write.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = _state()
            _wait_for_gap(state)
            # Reserve the name and record the attempt BEFORE sending. A write that
            # times out may still have applied, so its name must never be reusable
            # and the gap must count from it; reconcile by read-back, not re-send.
            reservation = PRIVATE / f"{self.name}.attempt"
            try:
                with reservation.open("x") as handle:
                    handle.write(datetime.now(UTC).isoformat())
            except FileExistsError as exc:
                raise Refused(
                    f"{self.name} was already attempted; read back, do not re-send"
                ) from exc
            sent_at = datetime.now(UTC)
            state["writes"].append({"at": sent_at.isoformat(), "name": self.name})
            _save_state(state)
            started = time.monotonic()
            response = requests.post(
                url, json=json, headers=headers, cookies=cookies, timeout=timeout
            )
            elapsed = int((time.monotonic() - started) * 1000)
        prepared = dict(response.request.headers)
        prepared["Cookie"] = "<redacted>"
        self.capture = {
            "name": self.name,
            "note": self.note,
            "captured_at": sent_at.replace(microsecond=0).isoformat(),
            "request": {
                "method": "POST",
                "url": url,
                "headers": prepared,
                "cookies_sent": sorted(cookies),
                "body": json,
            },
            "response": {
                "status": response.status_code,
                "headers": {
                    k: v
                    for k, v in response.headers.items()
                    if k.lower() not in {"x-amz-cf-id", "x-amz-cf-pop", "via", "set-cookie"}
                },
                "body": _scrubbed(response.content),
                "elapsed_ms": elapsed,
            },
        }
        PRIVATE.mkdir(parents=True, exist_ok=True)
        (PRIVATE / f"{self.name}.json").write_text(__import__("json").dumps(self.capture, indent=2))
        return response


def _provider(http_post=None):
    from fantasy_sports.providers.espn import EspnProvider

    return EspnProvider(_credentials(), http_post=http_post)


def _role(provider, league: str, season: int):
    role = provider.session_role(league, season)
    if len(role.owned_team_ids) != 1:
        raise Refused(f"expected exactly one owned team, got {role.owned_team_ids}")
    return role


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_role(args, league, season, others) -> int:
    provider = _provider()
    role = _role(provider, league, season)
    boot = provider._bootstrap_payload(league, season)  # noqa: SLF001 - research script
    settings = provider.fetch_raw(league, season, view="mSettings")
    s = settings.get("settings", {})
    acq = s.get("acquisitionSettings", {})
    roster = s.get("rosterSettings", {})
    print(
        json.dumps(
            {
                "member_id": role.member_id,
                "role": role.role,
                "owned_team_ids": role.owned_team_ids,
                "raw_flags": {k: role.raw.get(k) for k in ("isLeagueCreator", "isLeagueManager")},
                "scoringPeriodId": boot.get("scoringPeriodId"),
                "currentMatchupPeriod": boot.get("status", {}).get("currentMatchupPeriod"),
                "acquisition": {
                    k: acq.get(k)
                    for k in (
                        "acquisitionType",
                        "isUsingAcquisitionBudget",
                        "acquisitionBudget",
                        "minimumBid",
                        "waiverHours",
                        "waiverProcessHour",
                        "waiverProcessDays",
                        "waiverOrderReset",
                        "acquisitionLimit",
                    )
                },
                "lineupSlotCounts": roster.get("lineupSlotCounts"),
                "positionLimits": roster.get("positionLimits"),
            },
            indent=1,
        )
    )
    return 0


def _own_roster(provider, league, season, team: int, period: int) -> list[dict[str, Any]]:
    data = provider.fetch_raw(league, season, view="mRoster", scoringPeriodId=period)
    for t in data.get("teams", []):
        if t.get("id") == team:
            out = []
            for e in t.get("roster", {}).get("entries", []):
                p = e.get("playerPoolEntry", {}).get("player", {})
                out.append(
                    {
                        "playerId": e.get("playerId"),
                        "lineupSlotId": e.get("lineupSlotId"),
                        "name": p.get("fullName"),
                        "defaultPositionId": p.get("defaultPositionId"),
                        "eligibleSlots": p.get("eligibleSlots"),
                        "lineupLocked": e.get("playerPoolEntry", {}).get("lineupLocked"),
                        "acquisitionType": e.get("acquisitionType"),
                        "status": e.get("status"),
                    }
                )
            return out
    raise Refused(f"team {team} not in mRoster")


def cmd_readback(args, league, season, others) -> int:
    provider = _provider()
    role = _role(provider, league, season)
    team = int(role.owned_team_ids[0])
    period = int(provider._bootstrap_payload(league, season)["scoringPeriodId"])  # noqa: SLF001
    roster = _own_roster(provider, league, season, team, period)
    tx = provider.fetch_raw(
        league,
        season,
        view="mTransactions2",
        scoringPeriodId=period,
        x_fantasy_filter={"transactions": {"filterType": {"value": TRANSACTION_TYPES}}},
    )
    transactions = _scrubbed(json.dumps(tx.get("transactions", [])))
    snapshot = {
        "label": args.label,
        "taken_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "team": team,
        "scoringPeriodId": period,
        "roster": roster,
        "transactions": transactions,
    }
    out = PRIVATE / "readbacks"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{args.label}.json").write_text(json.dumps(snapshot, indent=2))
    print(f"team {team} period {period} roster size {len(roster)}")
    for row in sorted(roster, key=lambda r: r["lineupSlotId"]):
        print(
            f"  {row['playerId']:>9} slot {row['lineupSlotId']:>2} pos {row['defaultPositionId']}"
            f" {row['name']} locked={row['lineupLocked']}"
        )
    own = [t for t in transactions if t.get("teamId") == team]
    print(f"mTransactions2 period {period}: {len(transactions)} rows, {len(own)} for team {team}")
    for t in own:
        print(
            f"  {t.get('type'):<10} {t.get('status'):<10} pending={t.get('isPending')}"
            f" id={t.get('id')} bid={t.get('bidAmount')} related={t.get('relatedTransactionId')}"
            f" items={[(i.get('type'), i.get('playerId')) for i in t.get('items', [])]}"
        )
    return 0


def cmd_pool(args, league, season, others) -> int:
    """Read-only: how many unrostered players are FREEAGENT vs WAIVERS."""
    provider = _provider()
    period = json.loads((PRIVATE / "readbacks" / "p0.json").read_text())["scoringPeriodId"]
    counts = {}
    for status in ("FREEAGENT", "WAIVERS"):
        data = provider.fetch_raw(
            league,
            season,
            view="kona_player_info",
            scoringPeriodId=period,
            x_fantasy_filter={
                "players": {
                    "filterStatus": {"value": [status]},
                    "limit": 3000,
                    "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
                }
            },
        )
        counts[status] = len(data.get("players", []))
    print(json.dumps(counts))
    return 0


def _send(name: str, note: str, league, season, others, call) -> int:
    from fantasy_sports.core.errors import FantasySportsError

    PRIVATE.mkdir(parents=True, exist_ok=True)
    if (PRIVATE / f"{name}.json").exists() or (PRIVATE / f"{name}.attempt").exists():
        raise Refused(f"{name} was already sent or attempted; probes are not re-run")
    reader = _provider()
    role = _role(reader, league, season)
    recorder = Recorder(name, note, league, others, role.owned_team_ids)
    provider = _provider(http_post=recorder)
    try:
        result = call(provider, int(role.owned_team_ids[0]))
        outcome = {
            "ok": True,
            "status": result.status,
            "is_pending": result.is_pending,
            "transaction_id": result.provider_transaction_id,
        }
    except FantasySportsError as exc:
        outcome = {"ok": False, "error": exc.to_dict()}
    if recorder.capture is None:
        print(json.dumps({"sent": False, "outcome": outcome}, indent=1, default=str))
        return 1
    print(
        json.dumps(
            {
                "sent": True,
                "status": recorder.capture["response"]["status"],
                "body": recorder.capture["response"]["body"],
                "outcome": outcome,
            },
            indent=1,
            default=str,
        )
    )
    return 0


def cmd_move(args, league, season, others) -> int:
    from fantasy_sports.core.models import RosterMove

    def call(provider, team):
        return provider.move_players(
            league,
            season,
            str(team),
            adds=[RosterMove(p, to_slot=args.add_slot) for p in args.add],
            drops=[RosterMove(p) for p in args.drop],
            bid=args.bid,
            waiver=args.waiver,
        )

    return _send(args.name, args.note, league, season, others, call)


def cmd_cancel(args, league, season, others) -> int:
    def call(provider, team):
        return provider.cancel_transaction(league, season, str(team), args.related)

    return _send(args.name, args.note, league, season, others, call)


def cmd_raw(args, league, season, others) -> int:
    """A hand-built body through the provider's own ``_post``.

    The provider still runs ``_begin_write`` (credentials, ownership guard,
    current period) and maps the response; only the envelope is the caller's.
    """
    envelope = json.loads(args.body)
    if "teamId" in envelope or "scoringPeriodId" in envelope:
        raise Refused("teamId and scoringPeriodId are filled in by the script")

    def call(provider, team):
        write = provider._begin_write(league, season, str(team))  # noqa: SLF001
        return provider._post(write, envelope)  # noqa: SLF001

    return _send(args.name, args.note, league, season, others, call)


# --------------------------------------------------------------------------- #
# Redaction into docs/research/06-espn-add-drop-surface/
# --------------------------------------------------------------------------- #

OUT = REPO / "docs" / "research" / "06-espn-add-drop-surface"
SYNTHETIC_FIRST = 9000015
"""05 used 9000001..9000014 and -9000001 for a different league's roster; this
map continues after it so no synthetic id means two players across briefs."""
SYNTHETIC_DST_FIRST = -9000002

REDACTIONS = [
    "league id in every URL replaced with 99, the repository's invented synthetic league "
    "(docs/testing.md section 6); the real league is a throwaway public league joined for #108",
    "the probing member's SWID (already a salted pseudonym in the private capture) replaced "
    "with {SWID-REDACTED}, so it is not a confirmable mapping",
    "ESPN transaction ids replaced with <uuid-N>, numbered per distinct id across every file "
    "so a cancel's relatedTransactionId still joins to the claim it withdrew",
    "CloudFront request ids dropped",
    "team-7 player ids, and the free agents the probing member added or claimed, replaced "
    "with a stable synthetic mapping continuing 05's (9000015 upward, D/ST -9000002), and "
    "player names inside ESPN's messages replaced with the same synthetic label; slot ids "
    "are untouched",
    "another team's player id replaced with 0, its name with <another team's player>, and "
    "its team number with <other>; other managers' names and ids never appear",
]


def _synthetic_map(state: dict[str, Any]) -> dict[int, int]:
    """Stable: team 7's p0 roster in (slot, id) order, then touched free agents
    in the order they were first named."""
    p0 = json.loads((PRIVATE / "readbacks" / "p0.json").read_text())
    mapping: dict[int, int] = {}
    nxt, dst = SYNTHETIC_FIRST, SYNTHETIC_DST_FIRST
    for row in sorted(p0["roster"], key=lambda r: (r["lineupSlotId"], r["playerId"])):
        if row["playerId"] < 0:
            mapping[row["playerId"]] = dst
            dst -= 1
        else:
            mapping[row["playerId"]] = nxt
            nxt += 1
    withheld = set(state.get("withheld_player_ids", []))
    for path in sorted(
        PRIVATE.glob("p*.json"), key=lambda q: json.loads(q.read_text())["captured_at"]
    ):
        for item in json.loads(path.read_text())["request"]["body"].get("items", []):
            pid = item["playerId"]
            if pid not in mapping and pid not in withheld:
                mapping[pid] = nxt
                nxt += 1
    return mapping


def _names(ids: list[int], league: str, season: int) -> dict[int, str]:
    """Every name ESPN could interpolate for ``ids``: rostered players from
    ``mRoster`` (all teams), unrostered ones from the free-agent/waiver pool."""
    wanted = set(ids)
    period = json.loads((PRIVATE / "readbacks" / "p0.json").read_text())["scoringPeriodId"]
    provider = _provider()
    found: dict[int, str] = {}
    rosters = provider.fetch_raw(league, season, view="mRoster", scoringPeriodId=period)
    for team in rosters.get("teams", []):
        for entry in team.get("roster", {}).get("entries", []):
            if entry.get("playerId") in wanted:
                found[entry["playerId"]] = entry["playerPoolEntry"]["player"]["fullName"]
    pool = provider.fetch_raw(
        league,
        season,
        view="kona_player_info",
        scoringPeriodId=period,
        x_fantasy_filter={
            "players": {
                "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
                "limit": 3000,
                "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
            }
        },
    )
    for entry in pool.get("players", []):
        if entry.get("id") in wanted:
            found[int(entry["id"])] = entry["player"]["fullName"]
    missing = wanted - set(found)
    if missing:
        raise Refused(
            f"no name found for {len(missing)} player(s); cannot prove messages are clean"
        )
    return found


def cmd_redact(args, league, season, others) -> int:
    import re

    state = _state()
    mapping = _synthetic_map(state)
    withheld = set(state.get("withheld_player_ids", []))
    member = state.get("member_pseudonym")
    if not member:
        raise Refused("state.json has no member_pseudonym; run `role` and record it")
    names = _names(sorted(set(mapping) | withheld), league, season)
    uuids: dict[str, str] = {}
    uuid_re = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

    def text(value: str) -> str:
        value = value.replace(f"/leagues/{league}/", "/leagues/99/")
        if league in value:
            raise Refused("the real league id survived in a string")
        value = value.replace(member, "{SWID-REDACTED}")
        value = uuid_re.sub(
            lambda m: uuids.setdefault(m.group(0), f"<uuid-{len(uuids) + 1}>"), value
        )
        for pid, name in names.items():
            if pid in withheld:
                value = value.replace(name, "<another team's player>")
            elif pid in mapping:
                value = value.replace(name, f"Player {mapping[pid]}")
        value = re.sub(r"currently on team \d+", "currently on team <other>", value)
        return value

    def walk(node: Any, key: str | None = None) -> Any:
        if isinstance(node, dict):
            return {k: walk(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, key) for v in node]
        if isinstance(node, str):
            return text(node)
        if key == "playerId" and isinstance(node, int):
            if node in withheld:
                return 0
            return mapping.get(node, node)
        return node

    OUT.mkdir(parents=True, exist_ok=True)
    captures = sorted(
        PRIVATE.glob("p*.json"), key=lambda q: json.loads(q.read_text())["captured_at"]
    )
    for path in captures:
        capture = json.loads(path.read_text())
        capture["response"]["headers"] = {
            k: v
            for k, v in capture["response"]["headers"].items()
            if not k.lower().startswith("x-amz")
        }
        redacted = walk(capture)
        redacted = {
            "name": redacted["name"],
            "note": redacted["note"],
            "redactions": REDACTIONS,
            **{k: v for k, v in redacted.items() if k not in {"name", "note"}},
        }
        (OUT / path.name).write_text(json.dumps(redacted, indent=2, ensure_ascii=False) + "\n")

    snapshots = {}
    for path in sorted((PRIVATE / "readbacks").glob("*.json"), key=lambda q: q.stat().st_mtime):
        snap = json.loads(path.read_text())
        if "roster" not in snap:
            continue
        snapshots[snap["label"]] = {
            "taken_at": snap["taken_at"],
            "roster_size": len(snap["roster"]),
            "slots": {
                str(mapping.get(r["playerId"], r["playerId"])): r["lineupSlotId"]
                for r in snap["roster"]
            },
        }
        unknown = [r["playerId"] for r in snap["roster"] if r["playerId"] not in mapping]
        if unknown:
            raise Refused(f"roster player without a synthetic id: {unknown}")
    (OUT / "roster-readbacks.json").write_text(
        json.dumps(
            {
                "note": "team 7 mRoster {synthetic playerId: lineupSlotId} snapshots, before the "
                "first probe and after the probes that could change the roster; keys are probe "
                "labels in the order taken",
                "redactions": REDACTIONS,
                "snapshots": snapshots,
            },
            indent=1,
        )
        + "\n"
    )

    final = sorted((PRIVATE / "readbacks").glob("*.json"), key=lambda q: q.stat().st_mtime)
    last = next(
        json.loads(q.read_text())
        for q in reversed(final)
        if "transactions" in json.loads(q.read_text())
    )
    own = [t for t in last["transactions"] if t.get("teamId") == last["team"]]
    own.sort(key=lambda t: t.get("proposedDate") or 0)
    (OUT / "audit-trail-mTransactions2.json").write_text(
        json.dumps(
            {
                "note": (
                    "Every team-7 row in mTransactions2 "
                    f"(scoringPeriodId={last['scoringPeriodId']}) at {last['taken_at']}, "
                    "oldest first. Rejected probes never appear; claims, cancels, and "
                    "executed moves do. Other teams' rows are not committed."
                ),
                "redactions": REDACTIONS,
                "transactions": walk(own),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    pending = {}
    for path in sorted((PRIVATE / "readbacks").glob("*mPendingTransactions.json")):
        d = json.loads(path.read_text())
        rows = d.get("pendingTransactions") or []
        pending[path.stem] = {
            "pending_rows_total": len(rows),
            "team_7_rows": walk([t for t in rows if t.get("teamId") == 7]),
        }
    (OUT / "pending-transactions.json").write_text(
        json.dumps(
            {
                "note": "view=mPendingTransactions read while a claim was pending and after it was "
                "cancelled; only team 7's rows are kept",
                "redactions": REDACTIONS,
                "snapshots": pending,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    print(f"wrote {len(captures)} captures + readbacks to {OUT.relative_to(REPO)}")
    print("synthetic map size", len(mapping), "withheld", len(withheld))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--league", required=True, help=f"must be {ALIAS!r}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("role")
    rb = sub.add_parser("readback")
    rb.add_argument("label")
    mv = sub.add_parser("move")
    mv.add_argument("name")
    mv.add_argument("--add", action="append", default=[])
    mv.add_argument("--drop", action="append", default=[])
    mv.add_argument("--bid", type=int, default=None)
    mv.add_argument("--waiver", action="store_true", help="a claim; bidless unless --bid")
    mv.add_argument("--add-slot", default=None, help="slot name for the add, e.g. BE")
    mv.add_argument("--note", required=True)
    cn = sub.add_parser("cancel")
    cn.add_argument("name")
    cn.add_argument("--related", required=True)
    cn.add_argument("--note", required=True)
    rw = sub.add_parser("raw")
    rw.add_argument("name")
    rw.add_argument("--body", required=True, help="envelope JSON: type, executionType, items...")
    rw.add_argument("--note", required=True)
    sub.add_parser("redact")
    sub.add_parser("pool")
    args = parser.parse_args(argv)

    league, season, others = league_ids(args.league)
    handler = {
        "role": cmd_role,
        "readback": cmd_readback,
        "move": cmd_move,
        "cancel": cmd_cancel,
        "raw": cmd_raw,
        "redact": cmd_redact,
        "pool": cmd_pool,
    }[args.command]
    return handler(args, league, season, others)


if __name__ == "__main__":
    raise SystemExit(main())
