"""``add``, ``drop``, and ``claim`` — the irreversible writes (jwulff/fantasy-sports#18).

A dropped player can be claimed by another team within seconds; a processed
claim spends waiver priority and FAAB that restoring the roster does not give
back; an add that needs a drop inherits the drop's irreversibility. None of
these can be undone by this tool, so each one is **identified as irreversible
before it fires** (R8) and refused unless the caller says why it is doing it.

What an agent can rely on
-------------------------

* **The acknowledgement gate.** Without ``--acknowledge-irreversible "<why>"``
  (or with a blank one) the command is ``CONFIG_INVALID`` (``kind:
  "argument"``) and its message states exactly what this invocation cannot
  undo. The justification is journaled verbatim and echoed in
  ``data.acknowledged_irreversible``. The CLI cannot run ``/sanity-gate`` or
  verify that a caller did (ADR-0006's fourth gate); what it can do is make a
  bypass visible, and the README tells an autonomous agent to clear the gate
  before passing the flag. ``--dry-run`` needs no flag and reports the same
  statement under ``irreversible_because``.
* **Everything checkable from the read is checked first.** ``--player`` for an
  add or a claim must be in the current free-agent pool (an id, or a name that
  matches one player); ``--drop`` and ``drop --player`` must be on the team's
  roster. An add of a player on waivers, a claim of a free agent, and a
  ``--slot`` the player is not eligible for are each refused by name.
* **Only the session's own team**, exactly as ``lineup set``: a team the
  configured member does not own is ``WRITE_REJECTED`` (``not_owned``) before
  anything is journaled, in ``--dry-run`` too.
* **Journaled before it is sent, with what it costs.** The ``pending`` entry
  carries ``reversible: false``, ``cost`` (``bid_amount``,
  ``displaced_player_id``), the justification, the dropped player's slot and
  the added player's pool status. ESPN's answer closes it; an unknown outcome
  (a timeout) leaves it ``pending`` and is never retried.
* **Read back.** An executed add or drop re-reads the roster: the added player
  must be on it and the dropped one gone, or it is ``WRITE_DIVERGED``. A claim
  is pending until waivers process, so its read-back looks for the queued row
  in ``transactions`` and reports ``verified: null`` when it cannot see one --
  never a verification it did not make.
* **The owner is told.** After the entry closes, :func:`notify_irreversible`
  runs with the stored entry (R7a, #113). Its result is ``data.notified``; it
  never fails the write.

The flow mirrors :func:`fantasy_sports.commands.lineup.execute_lineup`; the
shared pieces live in :mod:`fantasy_sports.commands.writes`. Nothing here
imports typer (ADR-0003), and the journal, the notifier, and the provider are
imported inside the functions that use them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from fantasy_sports.commands.context import open_read, success
from fantasy_sports.commands.writes import close_entry, credential_block, require_owned, settle
from fantasy_sports.core.errors import (
    ConfigInvalidError,
    FantasySportsError,
    WriteDivergedError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.commands.context import ReadContext
    from fantasy_sports.core.models import RosterMove
    from fantasy_sports.output.envelope import Envelope

__all__ = ["MovePlan", "add_player", "claim_player", "drop_player", "execute_move", "plan_move"]

POOL_LIMIT = 500
"""How many free agents a name or id is resolved against: deep enough that any
player an agent found through ``free-agents`` is in it."""

ACKNOWLEDGE_FLAG = "--acknowledge-irreversible"

DRY_RUN_NOTE = (
    "Dry run: nothing was sent and nothing was journaled. The items were computed from "
    "the roster and free-agent pool just read; ESPN has no validate-only mode, so whether "
    "it would accept them (roster size, position limits, budget, waiver order) was not "
    f"checked. A real run needs {ACKNOWLEDGE_FLAG}."
)

PENDING_NOTE = (
    "ESPN queued the claim; it processes when waivers clear, and the roster does not "
    "change until then. No pending row for it was visible in `transactions`, which does "
    "not expose ESPN's transaction ids, so the claim is unverified rather than missing. "
    "`claims` (jwulff/fantasy-sports#109) lists pending claims by id."
)

_POOL_FREE_AGENT = "FREEAGENT"
_POOL_WAIVERS = "WAIVERS"


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


def add_player(
    *,
    team: str,
    player: str,
    acknowledge_irreversible: str | None = None,
    drop: str | None = None,
    slot: str | None = None,
    dry_run: bool = False,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """Add a free agent to one of the session's own teams, optionally dropping a player.

    ``data`` is one mapping: ``team_id``, ``kind`` (``"add"``),
    ``provider_transaction_id``, ``status``, ``is_pending``, ``proposed_at``,
    ``items``, ``journal_entry_id``, ``reversible`` (false),
    ``acknowledged_irreversible``, ``cost``, ``credentials``, ``read_back``,
    ``notified``, and ``dry_run``. See :func:`execute_move`.
    """
    del fresh  # a write always reads fresh; accepted only as a common option
    command = _argv("add", team=team, player=player, drop=drop, slot=slot)
    return _run(
        "add",
        command,
        team=team,
        player=player,
        drop=drop,
        slot=slot,
        bid=None,
        acknowledged=acknowledge_irreversible,
        dry_run=dry_run,
        league=league,
        season=season,
        no_cache=no_cache,
    )


def drop_player(
    *,
    team: str,
    player: str,
    acknowledge_irreversible: str | None = None,
    dry_run: bool = False,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """Release a player from one of the session's own teams to waivers.

    ``data`` has the same keys as :func:`add_player`, with ``kind`` ``"drop"``.
    """
    del fresh
    command = _argv("drop", team=team, player=player)
    return _run(
        "drop",
        command,
        team=team,
        player=None,
        drop=player,
        slot=None,
        bid=None,
        acknowledged=acknowledge_irreversible,
        dry_run=dry_run,
        league=league,
        season=season,
        no_cache=no_cache,
    )


def claim_player(
    *,
    team: str,
    player: str,
    bid: int | None = None,
    acknowledge_irreversible: str | None = None,
    drop: str | None = None,
    dry_run: bool = False,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """Submit a waiver claim, with a FAAB bid where the league has a budget,
    optionally naming the player to drop.

    ``bid`` is omitted for a league without an acquisition budget: ESPN
    rejects every bid there, even ``1``, as
    ``TRAN_ACQUISITION_BUDGET_NOT_SUPPORTED``, so no ``bidAmount`` is sent.
    ``data`` has the same keys as :func:`add_player`, with ``kind``
    ``"claim"``; ``is_pending`` is true and ``provider_transaction_id`` is the
    id a later ``claim cancel`` takes.
    """
    del fresh
    command = _argv("claim", team=team, player=player, drop=drop, bid=bid)
    return _run(
        "claim",
        command,
        team=team,
        player=player,
        drop=drop,
        slot=None,
        bid=bid,
        acknowledged=acknowledge_irreversible,
        dry_run=dry_run,
        league=league,
        season=season,
        no_cache=no_cache,
    )


def _argv(name: str, **options: Any) -> list[str]:
    """The invocation as journaled, minus the flags :func:`_run` appends."""
    command = ["fantasy-sports", name]
    for option, value in options.items():
        if value is not None:
            command += [f"--{option}", str(value)]
    return command


def _run(
    kind: str,
    command: list[str],
    *,
    team: str,
    player: str | None,
    drop: str | None,
    slot: str | None,
    bid: int | None,
    acknowledged: str | None,
    dry_run: bool,
    league: str | None,
    season: int | None,
    no_cache: bool,
) -> Envelope:
    if bid is not None and (isinstance(bid, bool) or int(bid) < 0):
        raise _argument(
            f"A waiver bid must be a whole number of 0 or more; got {bid!r}.",
            "Pass --bid 0 for a no-cost claim, or the FAAB amount to spend.",
            details={"bid": str(bid)},
        )
    if acknowledged is not None:
        command += [ACKNOWLEDGE_FLAG, acknowledged]
    if dry_run:
        command.append("--dry-run")

    ctx = open_read(league, season, fresh=True, no_cache=no_cache, free_agent_limit=POOL_LIMIT)
    from fantasy_sports.commands.roster import resolve_team_id

    provider = ctx.provider
    team_id = resolve_team_id(provider.fetch_teams(*ctx.target), team)
    league_info = provider.fetch_league(*ctx.target)
    period = getattr(league_info, "current_week", None)
    roster = provider.fetch_roster(*ctx.target, team_id)
    pool = provider.fetch_free_agents(*ctx.target, period, None) if player is not None else []
    data = execute_move(
        ctx,
        team_id,
        kind=kind,
        roster=roster,
        pool=pool,
        player=player,
        drop=drop,
        slot=slot,
        bid=None if bid is None else int(bid),
        acknowledged=acknowledged,
        command=command,
        dry_run=dry_run,
        scoring_period=period,
        acquisition=acquisition_settings(league_info),
    )
    return success(ctx, data, command=kind)


def acquisition_settings(league: Any) -> dict[str, Any]:
    """``{"acquisition_type", "uses_budget"}`` from the league the read already holds.

    ESPN's ``settings.acquisitionSettings`` rides in the bootstrap, so this
    costs no request. Either value is ``None`` when the provider does not say,
    and ``None`` refuses nothing.
    """
    raw = getattr(league, "raw", None)
    settings = raw.get("settings") if isinstance(raw, Mapping) else None
    found = settings.get("acquisitionSettings") if isinstance(settings, Mapping) else None
    found = found if isinstance(found, Mapping) else {}
    uses = found.get("isUsingAcquisitionBudget")
    kind = found.get("acquisitionType")
    return {
        "acquisition_type": kind if isinstance(kind, str) else None,
        "uses_budget": uses if isinstance(uses, bool) else None,
    }


# --------------------------------------------------------------------------- #
# Resolving players
# --------------------------------------------------------------------------- #


def resolve_free_agent(pool: Sequence[Any], wanted: str) -> Any:
    """The free agent ``wanted`` names: an id in the pool, an exact
    case-insensitive name, or a name fragment that matches exactly one player.

    :raises ConfigInvalidError: (``kind: "argument"``) for a player not in the
        pool, which includes every rostered player, and for a name matching
        more than one.
    """
    needle = str(wanted).strip()
    for agent in pool:
        if str(agent.provider_id) == needle:
            return agent
    folded = needle.casefold()
    for match in (
        [a for a in pool if str(a.player.name).casefold() == folded],
        [a for a in pool if folded and folded in str(a.player.name).casefold()],
    ):
        if len(match) == 1:
            return match[0]
        if len(match) > 1:
            ids = sorted(str(a.provider_id) for a in match)
            raise _argument(
                f"{wanted!r} matches {len(match)} free agents.",
                "Pass the player id, or more of the name. `fantasy-sports free-agents` lists "
                "the ids.",
                details={"player": str(wanted), "matches": ids},
            )
    raise _argument(
        f"No free agent in the current pool matches {wanted!r}. A rostered player cannot "
        "be added or claimed.",
        "Run `fantasy-sports free-agents` (with --pos to narrow it) for the ids and names "
        "that can be added this week.",
        details={"player": str(wanted), "pool_size": len(pool)},
    )


def resolve_rostered(roster: Sequence[Any], wanted: str) -> Any:
    """The roster slot ``wanted`` names: an id on this roster, an exact
    case-insensitive name, or a fragment unique on this roster.

    :raises ConfigInvalidError: (``kind: "argument"``) for a player not on the
        roster or a name matching more than one.
    """
    needle = str(wanted).strip()
    for slot in roster:
        if str(slot.provider_id) == needle:
            return slot
    folded = needle.casefold()
    for match in (
        [s for s in roster if str(s.player.name).casefold() == folded],
        [s for s in roster if folded and folded in str(s.player.name).casefold()],
    ):
        if len(match) == 1:
            return match[0]
        if len(match) > 1:
            ids = sorted(str(s.provider_id) for s in match)
            raise _argument(
                f"{wanted!r} matches {len(match)} players on this roster.",
                "Pass the player id, or more of the name. Run `fantasy-sports roster` for the ids.",
                details={"player": str(wanted), "matches": ids},
            )
    raise _argument(
        f"No player on this roster matches {wanted!r}; only a player the team holds can "
        "be dropped.",
        "Run `fantasy-sports roster --team ...` for this team's player ids and names.",
        details={"player": str(wanted)},
    )


def _label(player: Any) -> str:
    return f"{player.name} ({player.provider_id})"


def _pool_status(agent: Any) -> str | None:
    """ESPN's ``FREEAGENT``/``WAIVERS``, read from the pool entry the adapter kept.

    Not a normalized field: it is ESPN's acquisition state, and the command
    uses it only to refuse a move ESPN would. ``None`` when it is absent, in
    which case nothing is refused on its account.
    """
    raw = getattr(agent, "raw", None)
    status = raw.get("status") if isinstance(raw, Mapping) else None
    return str(status).upper() if isinstance(status, str) and status else None


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MovePlan:
    """What an add, drop, or claim would do, computed from one read."""

    kind: str
    adds: tuple[RosterMove, ...]
    drops: tuple[RosterMove, ...]
    bid: int | None
    items: tuple[dict[str, Any], ...]
    """``{player_id, type, from_slot, to_slot}`` per player, in this tool's vocabulary."""
    prior_state: dict[str, Any]
    """``{"roster": {dropped id: slot}, "pool": {added id: pool status}}``."""
    cost: dict[str, Any]
    """``{"bid_amount", "displaced_player_id"}`` — what the write spends."""
    irreversible_because: str
    """Exactly what this move cannot undo, in one sentence or two."""


def plan_move(
    kind: str,
    *,
    roster: Sequence[Any],
    pool: Sequence[Any],
    player: str | None,
    drop: str | None,
    slot: str | None,
    bid: int | None,
) -> MovePlan:
    """Resolve the players and refuse, by name, what ESPN would refuse.

    ``kind`` is ``add``, ``drop``, or ``claim``. For ``drop`` the player to
    release is ``drop`` and ``player`` is ``None``.
    """
    from fantasy_sports.core.models import RosterMove

    adds: list[RosterMove] = []
    drops: list[RosterMove] = []
    items: list[dict[str, Any]] = []
    prior: dict[str, Any] = {"roster": {}, "pool": {}}
    added_label = dropped_label = None
    displaced = None

    if player is not None:
        agent = resolve_free_agent(pool, player)
        added_id, added_label = str(agent.provider_id), _label(agent.player)
        status = _pool_status(agent)
        if kind == "add" and status == _POOL_WAIVERS:
            raise _argument(
                f"{added_label} is on waivers, so it cannot be added outright.",
                "Submit a waiver claim instead: `fantasy-sports claim --team ... --player "
                f"{added_id} --bid N`.",
                details={"player_id": added_id, "pool_status": status},
            )
        if kind == "claim" and status == _POOL_FREE_AGENT:
            raise _argument(
                f"{added_label} is a free agent, not on waivers, so there is nothing to claim.",
                f"Add the player outright: `fantasy-sports add --team ... --player {added_id}`.",
                details={"player_id": added_id, "pool_status": status},
            )
        to_slot = None if slot is None else str(slot).strip().upper()
        eligible = tuple(agent.player.eligible_slots or ())
        if to_slot is not None and eligible and to_slot not in eligible:
            raise _argument(
                f"{added_label} is not eligible for the {to_slot} slot.",
                f"Choose one of the player's eligible slots: {', '.join(eligible)}, or omit "
                "--slot to let ESPN place the player.",
                details={"player_id": added_id, "slot": to_slot, "eligible_slots": eligible},
            )
        adds.append(RosterMove(player_id=added_id, to_slot=to_slot))
        items.append({"player_id": added_id, "type": "add", "from_slot": None, "to_slot": to_slot})
        prior["pool"][added_id] = status

    if drop is not None:
        held = resolve_rostered(roster, drop)
        displaced, dropped_label = str(held.provider_id), _label(held.player)
        drops.append(RosterMove(player_id=displaced))
        items.append(
            {"player_id": displaced, "type": "drop", "from_slot": held.slot, "to_slot": None}
        )
        prior["roster"][displaced] = held.slot

    return MovePlan(
        kind=kind,
        adds=tuple(adds),
        drops=tuple(drops),
        bid=bid,
        items=tuple(items),
        prior_state=prior,
        cost={"bid_amount": bid, "displaced_player_id": displaced},
        irreversible_because=irreversible_because(kind, added_label, dropped_label),
    )


def irreversible_because(kind: str, added: str | None, dropped: str | None) -> str:
    """What this one invocation cannot undo, stated for the person or agent about to run it."""
    clauses: list[str] = []
    if kind == "claim":
        clauses.append(
            f"a processed claim for {added} consumes waiver priority (and FAAB where "
            "enabled), and restoring the roster later gives neither back"
        )
    elif kind == "add":
        clauses.append(
            f"{added} joins the roster, and undoing that later is a drop that sends the "
            "player to waivers"
        )
    if dropped is not None:
        clauses.append(
            f"{dropped} goes to waivers and can be claimed by any team; this cannot be reversed"
        )
    sentence = ". ".join(clause[0].upper() + clause[1:] for clause in clauses)
    return sentence + "."


# --------------------------------------------------------------------------- #
# The write flow
# --------------------------------------------------------------------------- #


def execute_move(
    ctx: ReadContext,
    team_id: str,
    *,
    kind: str,
    roster: Sequence[Any],
    pool: Sequence[Any],
    player: str | None,
    drop: str | None,
    slot: str | None,
    bid: int | None,
    acknowledged: str | None,
    command: Sequence[str],
    dry_run: bool,
    scoring_period: int | None = None,
    acquisition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Preflight, gate, journal, send, read back, and announce one irreversible move.

    Returns the success ``data`` mapping; every failure raises.
    """
    provider = ctx.provider
    role = provider.session_role(*ctx.target)
    require_owned(role, team_id)
    plan = plan_move(kind, roster=roster, pool=pool, player=player, drop=drop, slot=slot, bid=bid)
    acquisition = dict(acquisition or {"acquisition_type": None, "uses_budget": None})
    if kind == "claim" and plan.bid is not None and acquisition.get("uses_budget") is False:
        # ESPN rejects every bid in such a league, even 1, as
        # TRAN_ACQUISITION_BUDGET_NOT_SUPPORTED (brief 06). Refused here, so
        # the agent learns it without a rejected transaction on the record.
        raise _argument(
            "This league does not use an acquisition budget; omit --bid.",
            "Run the claim again without --bid. The claim is decided by waiver order "
            "alone in this league.",
            details={"bid": plan.bid, **acquisition},
        )

    data: dict[str, Any] = {
        "team_id": str(team_id),
        "kind": kind,
        "provider_transaction_id": None,
        "status": None,
        "is_pending": None,
        "proposed_at": None,
        "items": [dict(item) for item in plan.items],
        "journal_entry_id": None,
        "reversible": False,
        "acknowledged_irreversible": acknowledged,
        "cost": dict(plan.cost),
        "credentials": None,
        "read_back": None,
        "notified": None,
        "dry_run": dry_run,
    }
    if dry_run:
        data["credentials"] = credential_block(provider)
        data["irreversible_because"] = plan.irreversible_because
        if kind == "claim":
            data.update(acquisition)
        data["note"] = DRY_RUN_NOTE
        return data

    if acknowledged is None or not str(acknowledged).strip():
        raise _argument(
            f"This {kind} cannot be undone, and {ACKNOWLEDGE_FLAG} was not given. "
            f"{plan.irreversible_because}",
            f'Pass {ACKNOWLEDGE_FLAG} "<why>" with the reason for this move; it is journaled '
            "verbatim. An autonomous agent must clear /sanity-gate before passing it. Use "
            "--dry-run to preview the move without the flag.",
            details={
                "flag": ACKNOWLEDGE_FLAG,
                "move": kind,
                "irreversible_because": plan.irreversible_because,
                "cost": dict(plan.cost),
            },
        )

    from fantasy_sports.core.journal import Journal, JournalEntry, new_entry_id

    warnings: list[str] = []
    with Journal.open() as journal:
        # Raises CONFIG_INVALID if the journal cannot be written; nothing is
        # sent without its record.
        entry = journal.begin(
            JournalEntry(
                id=new_entry_id(),
                provider=provider.name,
                league_id=ctx.league_id,
                season=ctx.season,
                scoring_period=scoring_period,
                command=list(command),
                role=role.role,
                team_id=str(team_id),
                kind=kind,
                reversible=False,
                prior_state=plan.prior_state,
                request={
                    "team_id": str(team_id),
                    "items": data["items"],
                    "bid": plan.bid,
                    "acknowledged_irreversible": acknowledged,
                },
                cost=plan.cost,
            )
        )
        data["journal_entry_id"] = entry.id
        try:
            result = provider.move_players(
                *ctx.target,
                str(team_id),
                adds=plan.adds,
                drops=plan.drops,
                bid=plan.bid,
                waiver=kind == "claim",
            )
        except WriteDivergedError as exc:
            # Applied, perhaps as another account: record what ESPN echoed,
            # and tell the owner -- an irreversible write took effect.
            items = list(exc.details.get("items") or [])
            settle(exc, journal.diverge, entry.id, {"items": items}, exc)
            exc._record_detail("notified", _announce(journal, entry.id))
            raise
        except FantasySportsError as exc:
            # Same rule as `lineup set`: only an outcome the adapter marks
            # unknown (a timeout, a 5xx, an unreadable 200) leaves the entry
            # pending. It is never retried: a second claim is a second bid.
            if exc.details.get("transaction_state") != "unknown":
                settle(exc, journal.reject, entry.id, exc)
            raise

        data.update(
            provider_transaction_id=result.provider_transaction_id,
            status=result.status,
            is_pending=result.is_pending,
            proposed_at=result.proposed_at,
            credentials=dict(result.credentials),
        )
        closing = {
            "member_id": result.member_id,
            "provider_transaction_id": result.provider_transaction_id,
            "proposed_at": result.proposed_at,
        }
        echoed = [dict(item) for item in result.items]

        if result.is_pending:
            read_back = _pending_read_back(ctx, team_id, plan, scoring_period)
            applied: dict[str, Any] = {"items": echoed, "is_pending": True}
        else:
            read_back, observed, expected = _roster_read_back(ctx, team_id, plan)
            applied = {"items": echoed, "is_pending": False, "roster": observed}
            if read_back["diverged"]:
                diverged = read_back["diverged"]
                error = WriteDivergedError(
                    f"ESPN accepted the {kind}, but the roster read back differently for "
                    f"{len(diverged)} player(s).",
                    expected={pid: expected[pid] for pid in diverged},
                    observed={pid: (observed or {}).get(pid) for pid in diverged},
                    remediation=(
                        "Something changed the roster between the read and the write, or ESPN "
                        "applied it differently. Read the roster and `transactions` before "
                        "doing anything else; do not resend the same move."
                    ),
                    details={
                        "diverged": diverged,
                        "team_id": str(team_id),
                        "provider_transaction_id": result.provider_transaction_id,
                        "journal_entry_id": entry.id,
                        "transaction_state": "applied",
                    },
                )
                # Stored before the owner is told, so the row and the
                # notification both say what disagreed.
                settle(
                    error,
                    partial(
                        journal.complete,
                        status="diverged",
                        applied_state=applied,
                        error=error,
                        **closing,
                    ),
                    entry.id,
                )
                error._record_detail("notified", _announce(journal, entry.id))
                raise error
        close_entry(journal, entry.id, warnings, applied_state=applied, **closing)
        data["read_back"] = read_back
        data["notified"] = _announce(journal, entry.id)

    if read_back.get("error") is not None:
        warnings.append(
            f"ESPN accepted the {kind} but it could not be read back ({read_back['error']}). "
            "Run `fantasy-sports roster` and `transactions` before relying on it."
        )
    if warnings:
        data["warnings"] = warnings
    return data


def _roster_read_back(
    ctx: ReadContext, team_id: str, plan: MovePlan
) -> tuple[dict[str, Any], dict[str, str | None] | None, dict[str, str | None]]:
    """Re-read the roster live: every add on it (in ``--slot`` if one was named),
    every drop gone.

    Returns ``(read_back, observed, expected)``. ``observed`` and ``expected``
    map each moved player id to a slot name, ``"rostered"`` for an add with no
    slot named, or ``None`` for off the roster. A read that fails is reported,
    not raised: ESPN already executed the write.
    """
    expected: dict[str, str | None] = {}
    for move in plan.adds:
        expected[move.player_id] = move.to_slot or "rostered"
    for move in plan.drops:
        expected[move.player_id] = None
    try:
        roster = ctx.provider.fetch_roster(*ctx.target, str(team_id))
    except FantasySportsError as exc:
        return {"verified": False, "diverged": [], "error": exc.code.value}, None, expected
    slots = {str(slot.provider_id): slot.slot for slot in roster}
    observed = {pid: slots.get(pid) for pid in expected}
    diverged = sorted(
        pid
        for pid, want in expected.items()
        if (want is None and observed[pid] is not None)
        or (want == "rostered" and observed[pid] is None)
        or (want not in (None, "rostered") and observed[pid] != want)
    )
    return {"verified": not diverged, "diverged": diverged}, observed, expected


def _pending_read_back(
    ctx: ReadContext, team_id: str, plan: MovePlan, scoring_period: int | None
) -> dict[str, Any]:
    """Look for the queued claim in this period's ``transactions``.

    The normalized transaction carries no ESPN transaction id (``espn-api``
    drops it), so the match is on everything this claim submitted: a pending
    waiver claim by this team, for exactly the claimed player, displacing
    exactly the ``--drop`` player, at exactly this bid (ESPN records a
    bidless claim as ``0``). Exactly one such row is ``verified: true``.

    None is ``verified: null`` with a note: a pending row ESPN does not show
    and a claim that is missing look the same from here. More than one is
    ``null`` too, because an earlier identical claim would satisfy the same
    match and this one cannot be told apart from it. A match proves the claim
    was queued, nothing later: a cancel is a new transaction, and the claim's
    own ``mTransactions2`` row keeps reading ``PENDING`` after it.
    """
    try:
        rows = ctx.provider.fetch_transactions(*ctx.target, scoring_period=scoring_period)
    except FantasySportsError as exc:
        return {"verified": False, "diverged": [], "error": exc.code.value}
    adds = {move.player_id for move in plan.adds}
    drops = {move.player_id for move in plan.drops}
    bid = plan.bid or 0
    matches = 0
    for row in rows:
        raw = row.raw if isinstance(row.raw, Mapping) else {}
        recorded_bid = raw.get("bidAmount")
        if (
            row.type == "waiver_claim"
            and str(row.team_provider_id) == str(team_id)
            and set(row.players_in) == adds
            and set(row.players_out) == drops
            and (recorded_bid if isinstance(recorded_bid, int) else 0) == bid
            and str(raw.get("status") or "").upper() == "PENDING"
        ):
            matches += 1
    if matches == 1:
        return {
            "verified": True,
            "diverged": [],
            "matched_by": ["type", "team_id", "adds", "drops", "bid", "status"],
        }
    if matches > 1:
        return {
            "verified": None,
            "diverged": [],
            "note": f"{matches} identical pending claims are queued for this team, so this "
            "one cannot be told apart from an earlier one. `claims` "
            "(jwulff/fantasy-sports#109) lists them by id.",
        }
    return {"verified": None, "diverged": [], "note": PENDING_NOTE}


def _announce(journal: Any, entry_id: str) -> dict[str, Any]:
    """Run the owner's notify hook for the entry as stored; never raise.

    The hook gets the stored, redacted row, not what this command holds. An
    entry that did not close (its journal write failed) is still ``pending``,
    so it is not announced; ``doctor`` reports it instead.
    """
    try:
        from fantasy_sports.reports.notify import (
            NotifyResult,
            notify_irreversible,
            should_notify,
        )

        stored = journal.get(entry_id)
        if stored is None or not should_notify(stored):
            return NotifyResult(
                enabled=_notify_enabled(),
                attempted=False,
                error="the journal entry did not close, so nothing was announced; "
                "`doctor` reports it as pending",
            ).to_dict()
        return notify_irreversible(stored).to_dict()
    except Exception as exc:  # noqa: BLE001 - telling the owner never fails the write
        return {
            "enabled": None,
            "attempted": False,
            "ok": None,
            "exit_status": None,
            "error": f"notify skipped ({type(exc).__name__})",
        }


def _notify_enabled() -> bool:
    from fantasy_sports.config import notify as config_notify

    try:
        return bool(config_notify.load().enabled)
    except Exception:  # noqa: BLE001 - an unreadable config is "not enabled" here
        return False


def _argument(
    message: str, remediation: str, *, details: Mapping[str, Any] | None = None
) -> ConfigInvalidError:
    return ConfigInvalidError(message, kind="argument", remediation=remediation, details=details)
