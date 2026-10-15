"""``lineup set`` — move players to an explicit target lineup, journaled and read back.

The first command that changes a real league (jwulff/fantasy-sports#15). It is
the only write that can be verified live and reversed, so it is also where the
shared write path — the transport and ownership guard (#107), the journal
(#16), and the two write codes (#106) — gets its first end-to-end use.

What an agent can rely on
-------------------------

* **Explicit target state, never a relative move** (R6). ``--set PLAYER=SLOT``
  names where each player should *end up*. The command reads the roster,
  computes the diff, and sends one atomic transaction for every player whose
  slot differs. A target that already matches is refused, because ESPN has no
  no-op transaction.
* **Everything checkable from the read is checked before a request.** Slot
  eligibility, a locked player that would have to move, a slot the final
  lineup overfills, a player named twice, a name that matches two players:
  each is ``CONFIG_INVALID`` (``kind: "argument"``) naming the player. ESPN
  validates the *final* lineup (brief §3), so the count check runs over the
  final state, not move by move.
* **Only the session's own team.** A ``--team`` the configured member does not
  own is refused before anything is journaled or sent, in ``--dry-run`` too:
  a plan for a team the tool will never write is not a plan.
* **Journaled before it is sent.** A ``pending`` entry with the prior slots
  exists before the POST; ESPN's answer closes it. A crash in between leaves
  the ``pending`` row, which ``doctor`` reports.
* **Read back after it is accepted.** The roster is re-read live and compared
  slot for slot; a mismatch is ``WRITE_DIVERGED`` with the differing players.

The diff is computed here, from the roster the command already read, rather
than through the adapter's private item builder: the command needs it for the
preflight and for ``--dry-run`` anyway, and the adapter re-reads the roster
live and re-diffs before it sends, so a world that moved in between is caught
there (an empty or ineligible diff) or by the read-back. ``items`` are reported
in this tool's vocabulary — player id and slot *names* — not ESPN's slot ids.

:func:`execute_lineup` is shared with ``journal reverse``, which is the same
flow with the inverse target. Nothing here imports typer (ADR-0003), and the
journal and the provider are imported inside the functions that use them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fantasy_sports.commands.context import open_read, success
from fantasy_sports.core.errors import (
    ConfigInvalidError,
    FantasySportsError,
    WriteDivergedError,
    WriteRejectedError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.commands.context import ReadContext
    from fantasy_sports.output.envelope import Envelope

__all__ = ["LineupPlan", "execute_lineup", "parse_assignments", "plan_lineup", "set_lineup"]

DRY_RUN_NOTE = (
    "Dry run: nothing was sent and nothing was journaled. The items were computed from "
    "the roster just read; ESPN has no validate-only mode, so whether it would accept "
    "them was not checked."
)

_UNKNOWN_CREDENTIALS: Mapping[str, Any] = {
    "espn_s2_age_seconds": None,
    "warning": "credential age unknown",
}


def set_lineup(
    *,
    team: str,
    set: Sequence[str],  # noqa: A002 - `--set` is the documented flag
    dry_run: bool = False,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """Move players on one of the session's own teams to the slots named.

    ``--set PLAYER=SLOT`` is repeatable. ``PLAYER`` is a provider player id or
    a case-insensitive name that matches exactly one player on that roster;
    ``SLOT`` is a slot name as ``roster`` prints it (``QB``, ``RB``, ``WR``,
    ``TE``, ``RB/WR/TE``, ``D/ST``, ``K``, ``BE``, ``IR``). The write always
    starts from a fresh read, whatever ``--fresh`` says; ``--week`` does not
    exist, because ESPN accepts a lineup write only for the current period.

    ``data`` is one mapping: ``team_id``, ``kind`` (``"lineup"``),
    ``provider_transaction_id``, ``status``, ``is_pending``, ``proposed_at``,
    ``items`` (``player_id``, ``from_slot``, ``to_slot`` per moved player),
    ``journal_entry_id``, ``reversible`` (true), ``acknowledged_irreversible``
    (null), ``cost``, ``credentials`` (cookie age), ``read_back``
    (``verified``, ``diverged``), and ``dry_run``. ``--dry-run`` returns the
    same mapping with the ids null, ``read_back`` null, and a ``note``.
    """
    del fresh  # a write always reads fresh; accepted only as a common option
    command = ["fantasy-sports", "lineup", "set", "--team", team]
    for assignment in set:
        command += ["--set", assignment]
    if dry_run:
        command.append("--dry-run")

    ctx = open_read(league, season, fresh=True, no_cache=no_cache)
    from fantasy_sports.commands.roster import resolve_team_id

    team_id = resolve_team_id(ctx.provider.fetch_teams(*ctx.target), team)
    roster = ctx.provider.fetch_roster(*ctx.target, team_id)
    target = parse_assignments(set, roster)
    data = execute_lineup(
        ctx, team_id, target, roster=roster, command=command, dry_run=dry_run, kind="lineup"
    )
    return success(ctx, data, command="lineup set")


# --------------------------------------------------------------------------- #
# Parsing --set
# --------------------------------------------------------------------------- #


def parse_assignments(assignments: Sequence[str], roster: Sequence[Any]) -> dict[str, str]:
    """``["PLAYER=SLOT", ...]`` as ``{player_id: SLOT}``, resolved against ``roster``.

    :raises ConfigInvalidError: (``kind: "argument"``) for an assignment that
        does not parse, a player that is not on the roster or matches more
        than one, and a player named twice.
    """
    if not assignments:
        raise _argument(
            "A lineup change names no players.",
            "Pass --set PLAYER=SLOT at least once, e.g. --set 2003=RB/WR/TE.",
        )
    target: dict[str, str] = {}
    for assignment in assignments:
        player, sep, slot = str(assignment).rpartition("=")
        player, slot = player.strip(), slot.strip().upper()
        if not sep or not player or not slot:
            raise _argument(
                f"--set {assignment!r} is not PLAYER=SLOT.",
                "Write each assignment as PLAYER=SLOT, e.g. --set 2003=BE or "
                "--set 'Katherine Johnson=RB/WR/TE'.",
                details={"value": str(assignment)},
            )
        player_id = _resolve_player(roster, player)
        if player_id in target:
            raise _argument(
                f"Player {player_id} is named more than once.",
                "Name each player once, with the one slot they should end up in.",
                details={"player_id": player_id},
            )
        target[player_id] = slot
    return target


def _resolve_player(roster: Sequence[Any], wanted: str) -> str:
    """A roster player's id from an id, an exact name, or a unique name fragment.

    Narrower than nothing and wider than an id only: the match is limited to
    this one roster, so a fragment can only ever pick a player the team holds,
    and anything matching two is refused rather than guessed.
    """
    for slot in roster:
        if str(slot.provider_id) == wanted:
            return str(slot.provider_id)
    folded = wanted.casefold()
    for match in (
        [s for s in roster if str(s.player.name).casefold() == folded],
        [s for s in roster if folded in str(s.player.name).casefold()],
    ):
        if len(match) == 1:
            return str(match[0].provider_id)
        if len(match) > 1:
            ids = sorted(str(s.provider_id) for s in match)
            raise _argument(
                f"{wanted!r} matches {len(match)} players on this roster.",
                "Pass the player id, or more of the name. Run `fantasy-sports roster` for the ids.",
                details={"player": wanted, "matches": ids},
            )
    raise _argument(
        f"No player on this roster matches {wanted!r}.",
        "Run `fantasy-sports roster --team ...` for this team's player ids and names.",
        details={"player": wanted},
    )


# --------------------------------------------------------------------------- #
# The plan: diff and preflight
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LineupPlan:
    """What a lineup change would do, computed from one roster read."""

    items: tuple[dict[str, str], ...]
    """One ``{player_id, from_slot, to_slot}`` per player whose slot changes,
    vacates (moves to ``BE``/``IR``) first."""
    prior_state: dict[str, str]
    """``{player_id: slot}`` before the change, for every player named."""


_RESERVE_SLOTS = frozenset({"BE", "IR"})


def plan_lineup(
    roster: Sequence[Any], target: Mapping[str, str], slot_counts: Mapping[str, int]
) -> LineupPlan:
    """Diff ``target`` against ``roster`` and refuse what ESPN would.

    Each refusal is ``CONFIG_INVALID`` with ``kind: "argument"`` and the player
    or slot in ``details``. ``slot_counts`` is the league's ``roster_slots``;
    when it is empty (a provider that does not publish it) the count check is
    skipped and left to the provider.
    """
    by_id = {str(slot.provider_id): slot for slot in roster}
    prior: dict[str, str] = {}
    items: list[dict[str, str]] = []
    for player_id, to_slot in target.items():
        current = by_id.get(player_id)
        if current is None:
            raise _argument(
                f"Player {player_id} is not on this team's roster.",
                "Run `fantasy-sports roster` for this team's player ids.",
                details={"player_id": player_id},
            )
        prior[player_id] = current.slot
        eligible = tuple(current.player.eligible_slots or ())
        if eligible and to_slot not in eligible:
            raise _argument(
                f"Player {player_id} is not eligible for the {to_slot} slot.",
                f"Choose one of the player's eligible slots: {', '.join(eligible)}.",
                details={"player_id": player_id, "slot": to_slot, "eligible_slots": eligible},
            )
        if current.slot == to_slot:
            continue
        if current.is_locked is True:
            raise _argument(
                f"Player {player_id} is locked: their game has started, so they cannot "
                f"move from {current.slot}.",
                "Leave locked players where they are; they unlock after the scoring period.",
                details={"player_id": player_id, "slot": current.slot},
            )
        items.append({"player_id": player_id, "from_slot": current.slot, "to_slot": to_slot})

    if not items:
        raise _argument(
            "The lineup already matches: every player named is already in that slot.",
            "ESPN has no no-op transaction. Name at least one player whose slot changes.",
            details={"target": dict(target)},
        )

    if slot_counts:
        final = {pid: slot.slot for pid, slot in by_id.items()}
        final.update(target)
        for slot_name in sorted({item["to_slot"] for item in items}):
            occupants = sorted(pid for pid, slot in final.items() if slot == slot_name)
            allowed = int(slot_counts.get(slot_name, 0))
            if len(occupants) > allowed:
                staying = [pid for pid in occupants if pid not in target]
                locked = [pid for pid in staying if by_id[pid].is_locked is True]
                hint = f" Locked in that slot already: {', '.join(locked)}." if locked else ""
                raise _argument(
                    f"The {slot_name} slot would hold {len(occupants)} players; the league "
                    f"allows {allowed}.{hint}",
                    "Also move a current occupant out, e.g. --set PLAYER=BE, so the final "
                    "lineup fits the league's roster slots.",
                    details={
                        "slot": slot_name,
                        "allowed": allowed,
                        "player_ids": occupants,
                        "not_named": staying,
                        "locked": locked,
                    },
                )

    items.sort(key=lambda item: item["to_slot"] not in _RESERVE_SLOTS)
    return LineupPlan(items=tuple(items), prior_state=prior)


# --------------------------------------------------------------------------- #
# The shared write flow
# --------------------------------------------------------------------------- #


def execute_lineup(
    ctx: ReadContext,
    team_id: str,
    target: Mapping[str, str],
    *,
    roster: Sequence[Any],
    command: Sequence[str],
    dry_run: bool,
    kind: str,
    reverses: str | None = None,
) -> dict[str, Any]:
    """Preflight, journal, send, and read back one lineup change.

    ``kind`` is ``"lineup"`` for ``lineup set`` and ``"reverse"`` for
    ``journal reverse``, which passes the entry it undoes as ``reverses``.
    Returns the success ``data`` mapping; every failure raises.
    """
    provider = ctx.provider
    league = provider.fetch_league(*ctx.target)
    plan = plan_lineup(roster, target, dict(league.roster_slots or {}))
    role = provider.session_role(*ctx.target)
    _require_owned(role, team_id)

    data: dict[str, Any] = {
        "team_id": str(team_id),
        "kind": kind,
        "provider_transaction_id": None,
        "status": None,
        "is_pending": None,
        "proposed_at": None,
        "items": [dict(item) for item in plan.items],
        "journal_entry_id": None,
        "reversible": True,
        "acknowledged_irreversible": None,
        "cost": {"bid_amount": None, "displaced_player_id": None},
        "credentials": None,
        "read_back": None,
        "dry_run": dry_run,
    }
    if reverses is not None:
        data["reverses"] = reverses

    if dry_run:
        report = getattr(provider, "credential_report", None)
        data["credentials"] = dict(report() if callable(report) else _UNKNOWN_CREDENTIALS)
        data["note"] = DRY_RUN_NOTE
        return data

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
                scoring_period=getattr(league, "current_week", None),
                command=list(command),
                role=role.role,
                team_id=str(team_id),
                kind=kind,
                reversible=True,
                prior_state=plan.prior_state,
                request={"team_id": str(team_id), "target": dict(target), "items": data["items"]},
                reverses=reverses,
            )
        )
        data["journal_entry_id"] = entry.id
        try:
            result = provider.set_lineup(*ctx.target, str(team_id), dict(target))
        except WriteDivergedError as exc:
            # Applied, perhaps as another account: record what ESPN echoed.
            items = list(exc.details.get("items") or [])
            _settle(exc, journal.diverge, entry.id, {"items": items}, exc)
            raise
        except FantasySportsError as exc:
            # The adapter marks every failure that may have applied with
            # `transaction_state: "unknown"` (a timeout, a 5xx, an unreadable
            # 200). Anything else is a known no-send or a refusal -- ESPN's 409,
            # or the adapter's own live re-read finding the roster moved -- so
            # nothing applied and the entry closes `rejected`.
            if exc.details.get("transaction_state") != "unknown":
                _settle(exc, journal.reject, entry.id, exc)
            raise
        # An unknown outcome, or an exception outside the taxonomy, leaves the
        # entry `pending`: nothing says whether it applied, and a pending row
        # is exactly that statement. `doctor` reports it.

        data.update(
            provider_transaction_id=result.provider_transaction_id,
            status=result.status,
            is_pending=result.is_pending,
            proposed_at=result.proposed_at,
            credentials=dict(result.credentials),
        )
        observed, read_error = _read_back(ctx, team_id, target)
        diverged = (
            []
            if observed is None
            else sorted(pid for pid in target if observed.get(pid) != target[pid])
        )
        closing = {
            "member_id": result.member_id,
            "provider_transaction_id": result.provider_transaction_id,
            "proposed_at": result.proposed_at,
        }
        if diverged:
            assert observed is not None
            _close(
                journal,
                entry.id,
                warnings,
                status="diverged",
                applied_state={pid: observed.get(pid) for pid in target},
                **closing,
            )
            raise WriteDivergedError(
                f"ESPN accepted the lineup change, but the roster read back differently "
                f"for {len(diverged)} player(s).",
                expected={pid: target[pid] for pid in diverged},
                observed={pid: observed.get(pid) for pid in diverged},
                remediation=(
                    "Something changed the roster between the read and the write, or ESPN "
                    "applied it differently. Read the roster and decide before writing again; "
                    "do not resend the same change blind."
                ),
                details={
                    "diverged": diverged,
                    "team_id": str(team_id),
                    "provider_transaction_id": result.provider_transaction_id,
                    "journal_entry_id": entry.id,
                    "transaction_state": "applied",
                },
            )
        _close(journal, entry.id, warnings, applied_state=dict(target), **closing)

    if read_error is None:
        data["read_back"] = {"verified": True, "diverged": []}
    else:
        data["read_back"] = {"verified": False, "diverged": [], "error": read_error}
        warnings.append(
            "ESPN executed the change but the roster could not be re-read to verify it "
            f"({read_error}). Run `fantasy-sports roster` before relying on it."
        )
    if warnings:
        data["warnings"] = warnings
    return data


def _require_owned(role: Any, team_id: str) -> None:
    """The ownership rule, applied before a journal row or a dry-run plan exists.

    The adapter's guard refuses the same write before building a body; this
    copy stops a refused write from leaving a journal row, and stops
    ``--dry-run`` from planning a change the tool would never send.
    """
    owned = tuple(str(t) for t in (getattr(role, "owned_team_ids", None) or ()))
    if getattr(role, "member_id", None) is not None and str(team_id) in owned:
        return
    listed = ", ".join(owned) or "none"
    raise WriteRejectedError(
        f"Refusing to change team {team_id}: the configured session does not own it.",
        kind="not_owned",
        team_id=str(team_id),
        remediation=(
            f"Teams this session owns in this league: {listed}. Writes to any other team "
            "are refused regardless of league-manager role."
        ),
        details={"owned_team_ids": list(owned)},
    )


def _read_back(
    ctx: ReadContext, team_id: str, target: Mapping[str, str]
) -> tuple[dict[str, str | None] | None, str | None]:
    """The target players' slots as ESPN now reports them, or the error code.

    The adapter purged this league from the cache and dropped its memoised
    league on success, and the provider was opened ``fresh``, so this is a
    live read. A failure here does not undo a write ESPN already executed; it
    is reported as an unverified read-back rather than raised.
    """
    try:
        roster = ctx.provider.fetch_roster(*ctx.target, str(team_id))
    except FantasySportsError as exc:
        return None, exc.code.value
    slots = {str(slot.provider_id): slot.slot for slot in roster}
    return {pid: slots.get(pid) for pid in target}, None


def _settle(outcome: FantasySportsError, close: Any, entry_id: str, *args: Any) -> None:
    """Close the entry for a failed write without losing the write's own error.

    A journal that fails here (a full disk, a lock) must not replace
    ``outcome``: for a ``WRITE_DIVERGED`` that would hide a write that applied.
    The journal's failure rides along in ``outcome.details.journal`` and the
    entry stays ``pending``.
    """
    try:
        close(entry_id, *args)
    except Exception as exc:  # noqa: BLE001 - the write's outcome is the error to raise
        outcome._record_detail(
            "journal",
            f"entry {entry_id} could not be closed ({type(exc).__name__}); it stays pending",
        )


def _close(
    journal: Any, entry_id: str, warnings: list[str], *, status: str = "executed", **fields: Any
) -> None:
    """Close the journal entry; a failure to do so is a warning, not a lost write."""
    try:
        journal.complete(entry_id, status=status, **fields)
    except Exception as exc:  # noqa: BLE001 - the write happened; say so either way
        warnings.append(
            f"The journal entry {entry_id} could not be completed ({type(exc).__name__}); "
            "it stays 'pending'. ESPN's transaction id above is the record of what applied."
        )


def _argument(
    message: str, remediation: str, *, details: Mapping[str, Any] | None = None
) -> ConfigInvalidError:
    return ConfigInvalidError(message, kind="argument", remediation=remediation, details=details)
