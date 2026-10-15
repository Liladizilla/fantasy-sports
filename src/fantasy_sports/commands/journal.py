"""``journal list`` and ``journal show`` — read-only views of the mutation journal.

The journal (:mod:`fantasy_sports.core.journal`) records every write this tool
makes: what it was about to change, who ESPN says made the change, and how it
ended. These two commands are how a person, or an agent auditing another agent,
reads it back (jwulff/fantasy-sports#16).

Both are ``takes_league=False``. They read a local file, so there is no league
to resolve, no credential to require, and no provider to name: the envelope
carries ``provider``, ``league_id``, and ``season`` as ``null``, exactly as
``doctor`` does. ``--league`` here is a **filter**, not a target.

A journal that does not exist yet is an empty collection, not an error: nothing
has been written. A journal that exists but cannot be read is an error, because
reporting it empty would be a lie about an audit trail
(``core/journal.py``).

``journal reverse`` is not here. It needs the write transport (#107).

Nothing here imports typer (ADR-0003).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fantasy_sports.commands.context import require_shape
from fantasy_sports.core.errors import ConfigInvalidError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.output.envelope import Envelope

__all__ = ["journal_list", "journal_show"]


def journal_list(
    *,
    league: str | None = None,
    limit: int = 25,
    irreversible: bool = False,
) -> Envelope:
    """List journal entries, newest first.

    ``data`` is a list of entry mappings (see ``journal show`` for every field).
    ``--league`` accepts a configured profile name, resolved to its league id,
    or a raw league id; a name that is not a profile is taken as an id, so a
    league that has since been removed from ``config.toml`` can still be
    audited. ``--irreversible`` keeps only entries whose ``reversible`` is
    false: adds, drops, and claims, which a human would have to undo by hand.
    """
    from fantasy_sports.core.journal import Journal
    from fantasy_sports.output.envelope import Envelope

    if limit < 1:
        raise ConfigInvalidError(
            f"--limit must be at least 1; got {limit}.",
            kind="argument",
            remediation="Pass --limit with a positive integer, e.g. --limit 25.",
            details={"argument": "limit", "value": limit},
        )
    with Journal.open() as journal:
        entries = journal.list(
            league_id=_league_filter(league),
            limit=limit,
            irreversible_only=irreversible,
        )
    data: list[dict[str, Any]] = [entry.to_dict() for entry in entries]
    require_shape(data, command="journal list")
    return Envelope.success(data=data)


def journal_show(*, id: str) -> Envelope:  # noqa: A002 - `--id` is the documented flag
    """Show one journal entry in full.

    ``data`` is a mapping: ``id``, ``recorded_at``, ``provider``, ``league_id``,
    ``season``, ``scoring_period``, ``command`` (argv, redacted), ``member_id``
    (the acting member as ESPN's response echoed it, stored as a pseudonym),
    ``role`` (``creator``, ``manager``, ``member``, or null), ``team_id``,
    ``kind``, ``reversible``, ``prior_state``, ``applied_state``, ``request``,
    ``provider_transaction_id`` and ``proposed_at`` (ESPN's own ids, to join
    against its transaction log), ``cost`` (``bid_amount``,
    ``displaced_player_id``), ``status`` (``pending``, ``executed``,
    ``rejected``, ``diverged``), ``reverses``, and ``error``.

    An unknown id is ``CONFIG_INVALID`` with ``kind: "argument"``; the nearest
    ids are in ``details.nearest``.
    """
    from fantasy_sports.core.journal import Journal
    from fantasy_sports.output.envelope import Envelope

    with Journal.open() as journal:
        entry = journal.get(id)
        if entry is None:
            nearest = journal.nearest_ids(id)
            raise ConfigInvalidError(
                f"No journal entry with id {id!r}."
                + (f" Nearest ids: {', '.join(nearest)}." if nearest else " The journal is empty."),
                kind="argument",
                remediation="Run `fantasy-sports journal list` to see the ids on record.",
                details={"argument": "id", "value": id, "nearest": nearest},
            )
    data = entry.to_dict()
    require_shape(data, command="journal show")
    return Envelope.success(data=data)


def _league_filter(league: str | None) -> str | None:
    """A profile name's league id, or ``league`` itself taken as an id.

    A config file that will not parse does not block the lookup: the journal is
    read-only evidence and ``config`` is ``doctor``'s to report.
    """
    if league is None:
        return None
    from fantasy_sports.config import leagues as config_leagues

    try:
        profile = config_leagues.load().profiles_by_name.get(league)
    except ConfigInvalidError:
        return league
    return league if profile is None else profile.league_id
