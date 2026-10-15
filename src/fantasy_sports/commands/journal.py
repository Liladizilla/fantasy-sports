"""``journal list``, ``journal show``, and ``journal reverse``.

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

``journal reverse`` is the one that writes. For a lineup entry it sends the
inverse target — every player back to the slot ``prior_state`` recorded —
through the same flow as ``lineup set`` (preflight, journal, send, read back),
journaled as a new ``reverse`` entry whose ``reverses`` names the original. An
add, drop, claim, or cancel cannot be undone by the tool, so reversing one is
``NOT_AVAILABLE`` and nothing is sent.

Nothing here imports typer (ADR-0003).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from fantasy_sports.commands.context import require_shape
from fantasy_sports.core.errors import ConfigInvalidError, NotAvailableError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.output.envelope import Envelope

__all__ = ["journal_digest", "journal_list", "journal_reverse", "journal_show", "parse_since"]


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


#: What a human would have to do instead, for each kind the tool cannot undo.
_IRREVERSIBLE_REMEDIATION: dict[str, str] = {
    "add": "An add cannot be undone by the tool. To reverse it, drop the player yourself "
    "in the ESPN app (or with `drop` once it ships); they may then sit on waivers.",
    "drop": "A drop cannot be undone by the tool. The player is on waivers or a free agent; "
    "re-acquiring them is a new add or claim, decided by a human.",
    "claim": "A claim cannot be undone by the tool. If it is still pending, withdraw it with "
    "`fantasy-sports cancel-claim --team ... --id <provider_transaction_id>`; if it "
    "processed, dropping the player is a new, irreversible decision.",
    "cancel": "A cancelled claim cannot be restored. Resubmitting it is a new claim, decided "
    "by a human.",
}

_REVERSIBLE_KINDS = ("lineup", "reverse")
_REVERSIBLE_STATUSES = ("executed", "diverged")


def journal_reverse(*, id: str, dry_run: bool = False) -> Envelope:  # noqa: A002 - `--id`
    """Undo one lineup entry by moving every player back to their recorded slot.

    Only ``lineup`` and ``reverse`` entries that ESPN executed (or executed and
    read back differently) can be reversed. The inverse target is the entry's
    ``prior_state``; it goes through the same preflight, journal, send, and
    read-back as ``lineup set``, against the league the entry names, and is
    journaled as kind ``reverse`` with ``reverses`` set. A player now locked
    is refused before sending, or by ESPN as ``WRITE_REJECTED`` kind
    ``roster_lock``; one transaction never half-applies.

    An add, drop, claim, or cancel is ``NOT_AVAILABLE`` without sending
    anything, with what a human would have to do instead. A ``pending`` or
    ``rejected`` entry is ``CONFIG_INVALID``: nothing is known to have applied.

    ``data`` is the ``lineup set`` mapping with ``kind: "reverse"`` and
    ``reverses``.
    """
    from fantasy_sports.core.journal import Journal

    with Journal.open() as journal:
        entry = journal.get(id)
        nearest = [] if entry is not None else journal.nearest_ids(id)
    if entry is None:
        raise ConfigInvalidError(
            f"No journal entry with id {id!r}."
            + (f" Nearest ids: {', '.join(nearest)}." if nearest else " The journal is empty."),
            kind="argument",
            remediation="Run `fantasy-sports journal list` to see the ids on record.",
            details={"argument": "id", "value": id, "nearest": nearest},
        )
    if entry.kind in _IRREVERSIBLE_REMEDIATION or not entry.reversible:
        raise NotAvailableError(
            f"Journal entry {id} is a {entry.kind!r}, which this tool cannot reverse. "
            "Nothing was sent.",
            remediation=_IRREVERSIBLE_REMEDIATION.get(
                entry.kind, "Undo it by hand in the ESPN app."
            ),
            details={"id": id, "kind": entry.kind, "reversible": entry.reversible},
        )
    if entry.kind not in _REVERSIBLE_KINDS or entry.status not in _REVERSIBLE_STATUSES:
        raise ConfigInvalidError(
            f"Journal entry {id} is {entry.status!r}; only an executed or diverged lineup "
            "change can be reversed.",
            kind="argument",
            remediation=(
                "A rejected entry changed nothing. A pending one has an unknown outcome: read "
                "`fantasy-sports roster` and `transactions` first, then set the lineup "
                "explicitly with `lineup set`."
            ),
            details={"id": id, "kind": entry.kind, "status": entry.status},
        )
    target = {str(player): str(slot) for player, slot in dict(entry.prior_state).items()}
    if not target or entry.team_id is None:
        raise ConfigInvalidError(
            f"Journal entry {id} records no prior lineup to restore.",
            kind="argument",
            remediation="Set the lineup explicitly with `fantasy-sports lineup set`.",
            details={"id": id},
        )

    from fantasy_sports.commands.context import open_read, success
    from fantasy_sports.commands.lineup import execute_lineup

    ctx = open_read(_profile_for(entry.provider, entry.league_id), entry.season, fresh=True)
    roster = ctx.provider.fetch_roster(*ctx.target, entry.team_id)
    command = ["fantasy-sports", "journal", "reverse", "--id", id]
    if dry_run:
        command.append("--dry-run")
    data = execute_lineup(
        ctx,
        entry.team_id,
        target,
        roster=roster,
        command=command,
        dry_run=dry_run,
        kind="reverse",
        reverses=entry.id,
    )
    return success(ctx, data, command="journal reverse")


def _profile_for(provider: str, league_id: str) -> str:
    """The configured profile name for a journaled league.

    The journal records the league id; ``open_read`` takes a profile name,
    because credentials and the provider come from it. A league no longer in
    ``config.toml`` cannot be written to, and says so.
    """
    from fantasy_sports.config import leagues as config_leagues

    for name, profile in sorted(config_leagues.load().profiles_by_name.items()):
        if profile.league_id == league_id and profile.provider.lower() == provider.lower():
            return name
    raise ConfigInvalidError(
        f"No league profile in config.toml points at {provider} league {league_id}, which "
        "the journal entry names.",
        kind="config",
        remediation=f'Add a [leagues.<name>] profile with league_id = "{league_id}" and retry.',
        details={"provider": provider, "league_id": league_id},
    )


_DURATION = re.compile(r"(\d+)([mhd])")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_since(text: str, *, now: datetime | None = None) -> datetime:
    """A UTC instant from ``text``: a duration (``30m``, ``24h``, ``7d``) or an ISO-8601 timestamp.

    A duration counts back from ``now`` (default: the present). A timestamp
    must carry its offset (``2026-10-14T08:00:00Z``): a naive one would be
    wrong by the host's UTC offset, so it is refused rather than guessed.
    Anything else is ``CONFIG_INVALID`` with ``kind: "argument"``.
    """
    value = text.strip() if isinstance(text, str) else ""
    match = _DURATION.fullmatch(value)
    if match is not None:
        amount = int(match.group(1))
        try:
            return (now or datetime.now(UTC)) - timedelta(**{_UNITS[match.group(2)]: amount})
        except OverflowError:
            pass  # a duration reaching before year 1 is not a duration: refused below
    else:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(UTC)
    raise ConfigInvalidError(
        f"--since must be a duration like 24h, 7d, or 30m, or a UTC timestamp like "
        f"2026-10-14T08:00:00Z; got {text!r}.",
        kind="argument",
        remediation="Pass --since 24h, or --since with an ISO-8601 timestamp that ends in Z.",
        details={"argument": "since", "value": text},
    )


def journal_digest(
    *,
    since: str,
    irreversible: bool = False,
    league: str | None = None,
    limit: int = 100,
) -> Envelope:
    """Entries recorded at or after ``since``, newest first, for a consumer to render.

    Built for a cron job or an agent that reports on the journal: run it on a
    schedule with a window at least as long as the schedule, hand the output to
    whatever tells John. ``--since`` is a duration (``24h``, ``7d``, ``30m``)
    or an ISO-8601 timestamp with an offset (``2026-10-14T08:00:00Z``).
    ``--irreversible`` keeps adds, drops, and claims. ``--league`` filters
    exactly as it does for ``journal list``. ``data`` is a list of entry
    mappings (see ``journal show``); an empty window is an empty list.

    The window is on ``recorded_at``, when the entry was begun, so a write
    begun before the window and closed inside it appears only in the earlier
    window, with its final status. Overlap the windows and de-duplicate on
    ``id`` if that matters. The envelope needs no provider, credential, or
    league, exactly like ``journal list``.
    """
    from fantasy_sports.core.journal import Journal
    from fantasy_sports.output.envelope import Envelope

    cutoff = parse_since(since)
    if limit < 1:
        raise ConfigInvalidError(
            f"--limit must be at least 1; got {limit}.",
            kind="argument",
            remediation="Pass --limit with a positive integer, e.g. --limit 100.",
            details={"argument": "limit", "value": limit},
        )
    with Journal.open() as journal:
        entries = journal.list(
            league_id=_league_filter(league),
            limit=limit,
            irreversible_only=irreversible,
            since=cutoff,
        )
    data: list[dict[str, Any]] = [entry.to_dict() for entry in entries]
    require_shape(data, command="journal digest")
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
