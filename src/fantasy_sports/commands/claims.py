"""``claims`` and ``cancel-claim`` — the waiver queue (jwulff/fantasy-sports#109).

Managing waivers means seeing what is queued and withdrawing a claim before
it processes, not only submitting one. ``claims`` lists a team's pending
claims by ESPN's transaction id; ``cancel-claim`` withdraws one of them.

What an agent can rely on
-------------------------

* **The queue is ESPN's own.** Both commands read ``view=mPendingTransactions``
  through :meth:`~fantasy_sports.providers.base.WritingProvider.fetch_pending_claims`.
  ``transactions`` is the wrong place to look: a cancelled claim's own
  ``mTransactions2`` row keeps reading ``PENDING``
  (``docs/memory/a-cancelled-claim-still-reads-pending.md``).
* **Only a claim this team has queued is ever sent.** ``cancel-claim`` checks
  ownership of ``--team`` first (``WRITE_REJECTED``, ``not_owned``), then
  that ``--id`` is in that team's queue right now. An id that is not --
  another team's claim, one that already processed, one already cancelled, a
  typo -- is ``CONFIG_INVALID`` (``kind: "argument"``) pointing at
  ``claims``, and nothing is sent or journaled.
* **No acknowledgement flag.** A cancel withdraws a claim; it spends nothing
  (no FAAB, no waiver priority) and changes no roster. The flag exists to
  make a caller state why it is giving something up, and a cancel gives up
  nothing a resubmitted claim cannot restore. It is still ``reversible:
  false``: the tool cannot un-cancel, only submit a new claim, which is a new
  irreversible decision behind its own flag.
* **Journaled before it is sent**, as kind ``cancel`` with the claim as
  ``prior_state``. ESPN answers a cancel with a **new** transaction id, which
  becomes the entry's ``provider_transaction_id``; the claim's id is kept in
  ``applied_state.cancelled_claim_id``, so both ends of the withdrawal join.
* **Read back.** The claim must be gone from the queue: ``read_back.verified``
  is ``true`` when it is, ``false`` (with a warning) when ESPN still lists it
  or the queue could not be read.
* **Not announced.** The notify hook (R7a) exists so the owner hears about a
  write that spent or released something. A cancel does neither, so it is
  not called; ``data.notified`` is ``null``.

Nothing here imports typer (ADR-0003); the journal and the provider are
imported inside the functions that use them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from fantasy_sports.commands.context import open_read, success
from fantasy_sports.commands.writes import close_entry, credential_block, require_owned, settle
from fantasy_sports.core.errors import ConfigInvalidError, FantasySportsError, WriteDivergedError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.commands.context import ReadContext
    from fantasy_sports.core.models import PendingClaim
    from fantasy_sports.output.envelope import Envelope

__all__ = ["cancel_claim", "claim_state", "list_claims", "pending_read_back"]

CANCEL_DRY_RUN_NOTE = (
    "Dry run: nothing was sent and nothing was journaled. The claim is in this team's "
    "pending queue as just read; whether ESPN would still accept the cancel (the claim "
    "may process first) was not checked."
)


# --------------------------------------------------------------------------- #
# claims
# --------------------------------------------------------------------------- #


def list_claims(
    *,
    team: str | None = None,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """One team's pending waiver claims, newest first.

    ``data`` is a list of claim mappings (:class:`~fantasy_sports.core.models.PendingClaim`):
    ``provider_id`` is the id ``cancel-claim --id`` takes. ``--team`` defaults
    to the session's own team; reading another team's queue is allowed, as it
    is in the ESPN app.
    """
    ctx = open_read(league, season, fresh=fresh, no_cache=no_cache)
    team_id = _team_or_own(ctx, team)
    claims = ctx.provider.fetch_pending_claims(*ctx.target, team_id)
    return success(ctx, [claim.to_dict() for claim in newest_first(claims)], command="claims")


def newest_first(claims: Sequence[PendingClaim]) -> list[PendingClaim]:
    """Newest submission first; a claim with no submitted time sorts last."""
    dated = sorted(
        (c for c in claims if c.proposed_at is not None),
        key=lambda c: c.proposed_at,  # type: ignore[arg-type, return-value]
        reverse=True,
    )
    return dated + [c for c in claims if c.proposed_at is None]


def _team_or_own(ctx: ReadContext, team: str | None) -> str:
    """``--team`` resolved like every other command, or the one team the session owns."""
    provider = ctx.provider
    if team is not None:
        from fantasy_sports.commands.roster import resolve_team_id

        return resolve_team_id(provider.fetch_teams(*ctx.target), team)
    owned = tuple(str(t) for t in (provider.session_role(*ctx.target).owned_team_ids or ()))
    if len(owned) == 1:
        return owned[0]
    if not owned:
        raise _argument(
            "--team was not given, and the configured session owns no team in this league.",
            "Pass --team with the team whose claims to list (`fantasy-sports teams` lists "
            "them). Without a SWID the session's own team cannot be determined.",
            details={"owned_team_ids": []},
        )
    raise _argument(
        f"--team was not given, and the configured session owns {len(owned)} teams in this league.",
        f"Pass --team with one of: {', '.join(owned)}.",
        details={"owned_team_ids": list(owned)},
    )


# --------------------------------------------------------------------------- #
# cancel-claim
# --------------------------------------------------------------------------- #


def cancel_claim(
    *,
    team: str,
    id: str,  # noqa: A002 - the CLI flag is --id, and the registry names params by flag
    dry_run: bool = False,
    league: str | None = None,
    season: int | None = None,
    fresh: bool = False,
    no_cache: bool = False,
) -> Envelope:
    """Withdraw one of the session's own pending waiver claims.

    ``data`` is one mapping with the keys every write returns -- ``team_id``,
    ``kind`` (``"cancel"``), ``provider_transaction_id`` (the **cancel's** new
    id), ``status``, ``is_pending``, ``proposed_at``, ``items``,
    ``journal_entry_id``, ``reversible`` (false), ``acknowledged_irreversible``
    (null: no flag is needed), ``cost``, ``credentials``, ``read_back``,
    ``notified`` (null), ``dry_run`` -- plus ``cancelled_claim_id``, the id
    that was withdrawn.
    """
    del fresh  # a write always reads fresh; accepted only as a common option
    command = ["fantasy-sports", "cancel-claim", "--team", str(team), "--id", str(id)]
    if dry_run:
        command.append("--dry-run")
    wanted = str(id or "").strip()
    if not wanted:
        raise _argument(
            "--id is empty; a cancel needs the transaction id of the claim it withdraws.",
            "Run `fantasy-sports claims --team ...` and pass a `provider_id` from it.",
            details={"id": str(id)},
        )

    ctx = open_read(league, season, fresh=True, no_cache=no_cache)
    from fantasy_sports.commands.roster import resolve_team_id

    provider = ctx.provider
    team_id = resolve_team_id(provider.fetch_teams(*ctx.target), team)
    role = provider.session_role(*ctx.target)
    # Before the queue is read, so a team the session does not own is refused
    # as not_owned whatever --id says.
    require_owned(role, team_id)
    queue = provider.fetch_pending_claims(*ctx.target, team_id)
    claim = next((c for c in queue if c.provider_id == wanted), None)
    if claim is None:
        raise _argument(
            f"No pending claim {wanted!r} is queued for team {team_id}, so there is nothing "
            "to cancel. Nothing was sent.",
            "Run `fantasy-sports claims --team ...` for this team's pending claim ids. A claim "
            "that already processed or was already cancelled is no longer pending, and "
            "another team's claim cannot be cancelled from this one.",
            details={
                "id": wanted,
                "team_id": str(team_id),
                "pending_ids": [c.provider_id for c in queue],
            },
        )
    period = getattr(provider.fetch_league(*ctx.target), "current_week", None)
    data = _execute_cancel(
        ctx, str(team_id), claim, role=role, command=command, dry_run=dry_run, period=period
    )
    return success(ctx, data, command="cancel-claim")


def claim_state(claim: PendingClaim) -> dict[str, Any]:
    """The claim as the journal stores it: JSON-plain, without ``raw``."""
    return {
        "provider_transaction_id": claim.provider_id,
        "team_id": claim.team_id,
        "players_in": list(claim.players_in),
        "players_out": list(claim.players_out),
        "bid_amount": claim.bid_amount,
        "proposed_at": None if claim.proposed_at is None else claim.proposed_at.isoformat(),
        "processes_at": None if claim.processes_at is None else claim.processes_at.isoformat(),
        "status": claim.status,
        "sub_order": claim.sub_order,
    }


def _items(claim: PendingClaim) -> list[dict[str, Any]]:
    """The withdrawn claim's players, in the same vocabulary ``claim`` reports."""
    items = [
        {"player_id": pid, "type": "add", "from_slot": None, "to_slot": None}
        for pid in claim.players_in
    ]
    items += [
        {"player_id": pid, "type": "drop", "from_slot": None, "to_slot": None}
        for pid in claim.players_out
    ]
    return items


def _execute_cancel(
    ctx: ReadContext,
    team_id: str,
    claim: PendingClaim,
    *,
    role: Any,
    command: Sequence[str],
    dry_run: bool,
    period: int | None,
) -> dict[str, Any]:
    provider = ctx.provider
    data: dict[str, Any] = {
        "team_id": team_id,
        "kind": "cancel",
        "provider_transaction_id": None,
        "cancelled_claim_id": claim.provider_id,
        "status": None,
        "is_pending": None,
        "proposed_at": None,
        "items": _items(claim),
        "journal_entry_id": None,
        "reversible": False,
        # No --acknowledge-irreversible: a cancel spends nothing (module docstring).
        "acknowledged_irreversible": None,
        "cost": {"bid_amount": None, "displaced_player_id": None},
        "credentials": None,
        "read_back": None,
        # The notify hook is for writes that spend or release something; a
        # cancel does neither, so it is never called.
        "notified": None,
        "dry_run": dry_run,
    }
    if dry_run:
        data["credentials"] = credential_block(provider)
        data["note"] = CANCEL_DRY_RUN_NOTE
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
                scoring_period=period,
                command=list(command),
                role=role.role,
                team_id=team_id,
                kind="cancel",
                reversible=False,
                prior_state={"claim": claim_state(claim)},
                request={"team_id": team_id, "cancelled_claim_id": claim.provider_id},
            )
        )
        data["journal_entry_id"] = entry.id
        try:
            result = provider.cancel_transaction(*ctx.target, team_id, claim.provider_id)
        except WriteDivergedError as exc:
            # Applied as another account (the adapter's identity check).
            items = list(exc.details.get("items") or [])
            settle(exc, journal.diverge, entry.id, {"items": items}, exc)
            raise
        except FantasySportsError as exc:
            # Only an outcome the adapter marks unknown (a timeout, a 5xx, an
            # unreadable 200) leaves the entry pending; it is never retried.
            if exc.details.get("transaction_state") != "unknown":
                settle(exc, journal.reject, entry.id, exc)
            raise

        related = result.raw.get("relatedTransactionId") if result.raw else None
        data.update(
            provider_transaction_id=result.provider_transaction_id,
            status=result.status,
            is_pending=result.is_pending,
            proposed_at=result.proposed_at,
            credentials=dict(result.credentials),
        )
        read_back = pending_read_back(ctx, team_id, claim.provider_id, gone=True)
        close_entry(
            journal,
            entry.id,
            warnings,
            applied_state={
                "cancelled_claim_id": claim.provider_id,
                "related_transaction_id": related if isinstance(related, str) else None,
                "items": [dict(item) for item in result.items],
                "read_back": read_back,
            },
            member_id=result.member_id,
            provider_transaction_id=result.provider_transaction_id,
            proposed_at=result.proposed_at,
        )
        data["read_back"] = read_back

    if read_back.get("error") is not None:
        warnings.append(
            f"ESPN accepted the cancel but the waiver queue could not be read back "
            f"({read_back['error']}). Run `fantasy-sports claims` before submitting again."
        )
    elif read_back["verified"] is False:
        warnings.append(
            f"ESPN answered {result.status} but still lists claim {claim.provider_id} as "
            "pending. Run `fantasy-sports claims` before acting on it; do not resubmit or "
            "re-cancel blind."
        )
    if warnings:
        data["warnings"] = warnings
    return data


def pending_read_back(
    ctx: ReadContext, team_id: str, transaction_id: str, *, gone: bool
) -> dict[str, Any]:
    """Re-read the team's queue for ``transaction_id``.

    ``gone=True`` (a cancel) verifies when it is absent; ``gone=False`` (a
    claim) verifies when it is listed. The result carries ``queue_size``, how
    many claims the team has queued, so a caller can tell "not listed in a
    non-empty queue" from "the queue was empty". A read that fails is
    ``verified: false`` with the error code, not raised: ESPN already
    answered the write.
    """
    try:
        queue = ctx.provider.fetch_pending_claims(*ctx.target, str(team_id))
    except FantasySportsError as exc:
        return {"verified": False, "diverged": [], "error": exc.code.value}
    listed = any(c.provider_id == transaction_id for c in queue)
    verified = listed is not gone
    return {
        "verified": verified,
        "diverged": [] if verified else [transaction_id],
        "matched_by": ["provider_transaction_id"],
        "queue_size": len(queue),
    }


def _argument(message: str, remediation: str, *, details: dict[str, Any]) -> ConfigInvalidError:
    return ConfigInvalidError(message, kind="argument", remediation=remediation, details=details)
