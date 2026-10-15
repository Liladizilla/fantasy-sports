"""The pieces every write command shares: ownership, closing the journal entry.

``lineup set`` (#15) wrote these first; ``add``, ``drop``, and ``claim`` (#18)
reuse them unchanged, so they live here rather than as private names one
command imports from another. Nothing here imports typer (ADR-0003), the
journal, or a provider.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fantasy_sports.core.errors import FantasySportsError, WriteRejectedError

__all__ = ["UNKNOWN_CREDENTIALS", "close_entry", "credential_block", "require_owned", "settle"]

UNKNOWN_CREDENTIALS: Mapping[str, Any] = {
    "espn_s2_age_seconds": None,
    "warning": "credential age unknown",
}
"""The R12a block's shape for a provider that cannot report cookie age."""


def credential_block(provider: Any) -> dict[str, Any]:
    """The provider's credential-age report, for a ``--dry-run`` that sends nothing."""
    report = getattr(provider, "credential_report", None)
    return dict(report() if callable(report) else UNKNOWN_CREDENTIALS)


def require_owned(role: Any, team_id: str) -> None:
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


def settle(outcome: FantasySportsError, close: Any, entry_id: str, *args: Any) -> None:
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


def close_entry(
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
