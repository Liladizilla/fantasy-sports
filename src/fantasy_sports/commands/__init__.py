"""The command registry.

Commands are plain typed functions. The typer CLI and the future MCP server are
both thin projections over this registry (ADR-0003), so **nothing in this
package may import typer**. A test enforces that.

Each entry is a :class:`CommandSpec`. ``handler`` is a dotted path resolved
lazily at call time, so building the registry — and therefore rendering
``--help`` — never imports a provider, an HTTP stack, or a renderer.

Three things a spec declares that make a projection possible without reading
the handler's source:

* **Its parameters** (:class:`Param`). Both surfaces are generated from these:
  ``cli/app.py`` turns them into typer options, and an MCP server would turn
  the same list into a tool schema. A handler's own signature is *not* the
  source of truth, because introspecting it would mean importing it — which is
  the 50 ms cold start ``cli/fastpath.py`` exists to avoid.
* **Whether it reads a league** (:attr:`CommandSpec.takes_league`). Every read
  command accepts ``--league``, ``--season``, ``--fresh``, and ``--no-cache``;
  the ``auth`` commands accept none of them.
* **The shape of its ``data``** (:class:`DataShape`). The envelope's ``data``
  is a list for a collection and a mapping for a single object. That was a
  convention nothing enforced until jwulff/fantasy-sports#9 wrote it down here
  and made :func:`fantasy_sports.commands.context.success` check it on every
  call, which turns it into a contract the first consumer
  (``jwulff/league-gazette``) can rely on.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "COMMON_PARAMS",
    "GLOBAL_PARAMS",
    "NO_RAW_PARAM",
    "OUTPUT_PARAM",
    "REGISTRY",
    "CommandSpec",
    "DataShape",
    "Param",
    "groups",
    "in_group",
    "register",
    "top_level",
]


class DataShape(StrEnum):
    """What the envelope's ``data`` holds for one command.

    A consumer that has to sniff whether ``data`` is a list or a mapping is a
    consumer that will get it wrong once, on the command it tested least. The
    shape is declared here, enforced at construction, and asserted per command
    in ``tests/unit/test_commands.py``.
    """

    OBJECT = "object"
    """A mapping — one thing described. ``league info``, ``auth status``."""

    COLLECTION = "collection"
    """A list — many things of one kind. ``teams``, ``standings``, ``roster``."""


@dataclass(frozen=True)
class Param:
    """One argument, described once for every projection.

    ``annotation`` is a real Python type object, not a string: the CLI
    projection hands it to typer, which resolves it through ``get_type_hints``.
    """

    name: str
    """The handler's keyword-argument name, e.g. ``no_cache``."""

    help: str
    """Written for an agent reading ``--help`` as its only documentation."""

    annotation: Any = str
    default: Any = None
    flags: tuple[str, ...] = ()
    """Explicit CLI flags. Empty means ``--<name with dashes>``."""

    required: bool = False

    @property
    def cli_flags(self) -> tuple[str, ...]:
        return self.flags or (f"--{self.name.replace('_', '-')}",)

    @property
    def metavar(self) -> str | None:
        """What the value looks like in help, or ``None`` for a flag."""
        if self.annotation is bool:
            return None
        return "INTEGER" if self.annotation in (int, int | None) else "TEXT"

    @property
    def display(self) -> str:
        """How this option appears in the hand-rolled fast-path help."""
        flags = ", ".join(self.cli_flags)
        return flags if self.metavar is None else f"{flags} {self.metavar}"


COMMON_PARAMS: tuple[Param, ...] = (
    Param(
        name="league",
        help="Named league profile from config.toml. Defaults to the configured default.",
        annotation=str | None,
        flags=("--league", "-l"),
    ),
    Param(
        name="season",
        help="Four-digit year, overriding the profile's season for this call.",
        annotation=int | None,
    ),
    Param(
        name="fresh",
        help="Refetch from the provider and update the cache entry.",
        annotation=bool,
        default=False,
    ),
    Param(
        name="no_cache",
        help="Neither read nor write the cache. Wins over --fresh if both are given.",
        annotation=bool,
        default=False,
    ),
)
"""Accepted by every command that reads a league, and passed to the handler.

``--output`` is deliberately absent: how a payload is rendered is a property of
the *surface*, not of the command, and an MCP tool has no table to render.
"""

OUTPUT_PARAM = Param(
    name="output",
    help="json, table, or csv. Defaults to json unless stdout is a terminal.",
    annotation=str | None,
    flags=("--output", "-o"),
)
"""CLI-only. Never reaches a handler; :mod:`fantasy_sports.output` consumes it."""

NO_RAW_PARAM = Param(
    name="no_raw",
    help="Strip `raw` from every normalized object in the payload, recursively. "
    "Marks raw_omitted=true in the envelope. `raw --view` is passthrough by "
    "definition and ignores this flag.",
    annotation=bool,
    default=False,
)
"""CLI-only, like ``OUTPUT_PARAM``: never reaches a handler. Unlike ``--output``,
this does not just change how ``data`` renders — it changes ``data`` itself —
so it is applied once, at the dispatch layer, to the envelope a handler already
returned (:meth:`fantasy_sports.output.envelope.Envelope.without_raw`), rather
than threaded through every handler that might have a `raw` field somewhere in
its result.
"""

GLOBAL_PARAMS: tuple[Param, ...] = (
    COMMON_PARAMS[0],
    COMMON_PARAMS[1],
    OUTPUT_PARAM,
    COMMON_PARAMS[2],
    COMMON_PARAMS[3],
    NO_RAW_PARAM,
)
"""Every option the CLI accepts before *or* after the command name, in help order."""


@dataclass(frozen=True)
class CommandSpec:
    """One command, independent of how it is invoked."""

    name: str
    """The command's own name, e.g. ``standings``."""

    summary: str
    """One line, written for an agent reading ``--help`` as its documentation."""

    handler: str
    """Dotted path to the implementing function, e.g.
    ``fantasy_sports.commands.league:standings``. Resolved on demand."""

    group: str | None = None
    """Sub-command group, e.g. ``auth``. ``None`` means top level."""

    aliases: tuple[str, ...] = field(default_factory=tuple)

    params: tuple[Param, ...] = field(default_factory=tuple)
    """This command's own arguments, before the common ones are added."""

    shape: DataShape = DataShape.COLLECTION
    """What ``data`` holds on success. Enforced, not documented."""

    takes_league: bool = True
    """Whether ``--league``/``--season``/``--fresh``/``--no-cache`` apply."""

    honors_no_raw: bool = True
    """Whether ``--no-raw`` strips ``data`` before this command's envelope is
    emitted. ``False`` for exactly one command: ``raw``, whose whole point is
    an unmodified provider payload (jwulff/fantasy-sports#52)."""

    @property
    def path(self) -> tuple[str, ...]:
        return (self.group, self.name) if self.group else (self.name,)

    @property
    def invocation(self) -> str:
        return " ".join(self.path)

    @property
    def handler_params(self) -> tuple[Param, ...]:
        """Every argument the handler is called with."""
        return (*self.params, *COMMON_PARAMS) if self.takes_league else self.params

    @property
    def cli_params(self) -> tuple[Param, ...]:
        """Every option the typer projection declares, ``--output``/``--no-raw`` included."""
        return (*self.handler_params, OUTPUT_PARAM, NO_RAW_PARAM)

    def resolve(self) -> Callable[..., Any]:
        """Import and return the implementing function."""
        from importlib import import_module

        module_name, _, attr = self.handler.partition(":")
        if not attr:
            raise ValueError(f"handler must be 'module:function', got {self.handler!r}")
        return getattr(import_module(module_name), attr)


REGISTRY: dict[str, CommandSpec] = {}
"""Every command, keyed by its full invocation (``"auth status"``).

The CLI and its help output are generated from whatever is registered here, so
a command becomes visible in both surfaces by being registered and nowhere
else. Do not hand-write a command into either surface.
"""


def register(spec: CommandSpec) -> CommandSpec:
    """Add ``spec`` to the registry. Raises on a duplicate invocation."""
    if spec.invocation in REGISTRY:
        raise ValueError(f"command {spec.invocation!r} is already registered")
    REGISTRY[spec.invocation] = spec
    return spec


def groups() -> list[str]:
    """Registered sub-command group names, sorted."""
    return sorted({spec.group for spec in REGISTRY.values() if spec.group})


def top_level() -> list[CommandSpec]:
    """Registered commands that sit at the top level, sorted by name."""
    return sorted((s for s in REGISTRY.values() if not s.group), key=lambda s: s.name)


def in_group(group: str) -> list[CommandSpec]:
    """Registered commands inside ``group``, sorted by name."""
    return sorted((s for s in REGISTRY.values() if s.group == group), key=lambda s: s.name)


# --------------------------------------------------------------------------- #
# The v0.1 read surface (ARCHITECTURE §12)
# --------------------------------------------------------------------------- #
#
# Declarations only. Nothing below imports a handler, a provider, or an HTTP
# stack, which is what keeps `--help` inside the 50 ms cold-start budget.

_WEEK_HELP = (
    "NFL scoring period (the week number a human means). Defaults to the league's current week."
)

register(
    CommandSpec(
        name="info",
        group="league",
        summary="Describe the league: name, season, team count, current week, roster slots.",
        handler="fantasy_sports.commands.league:info",
        shape=DataShape.OBJECT,
    )
)

register(
    CommandSpec(
        name="teams",
        summary="List every team with its record, points, and owners. Unordered.",
        handler="fantasy_sports.commands.league:teams",
        shape=DataShape.COLLECTION,
    )
)

register(
    CommandSpec(
        name="standings",
        summary="List teams in the provider's own rank order, with a 1-based standing.",
        handler="fantasy_sports.commands.league:standings",
        shape=DataShape.COLLECTION,
    )
)

register(
    CommandSpec(
        name="roster",
        summary="List one team's roster slots: player, lineup slot, eligibility, kickoff, lock.",
        handler="fantasy_sports.commands.roster:roster",
        shape=DataShape.COLLECTION,
        params=(
            Param(
                name="team",
                help="Team id, or its name (case-insensitive; a unique prefix is enough).",
                annotation=str,
                required=True,
            ),
            Param(
                name="week",
                help="Scoring period to read the lineup for. Defaults to the current roster.",
                annotation=int | None,
            ),
        ),
    )
)

register(
    CommandSpec(
        name="matchups",
        summary="List head-to-head pairings for a week, with both ESPN period identifiers.",
        handler="fantasy_sports.commands.matchups:matchups",
        shape=DataShape.COLLECTION,
        params=(Param(name="week", help=_WEEK_HELP, annotation=int | None),),
    )
)

register(
    CommandSpec(
        name="box-scores",
        summary="List both lineups for a week's matchups, player by player, with projections.",
        handler="fantasy_sports.commands.box_scores:box_scores",
        shape=DataShape.COLLECTION,
        params=(Param(name="week", help=_WEEK_HELP, annotation=int | None),),
    )
)

register(
    CommandSpec(
        name="transactions",
        summary="List recent roster moves, newest first, walking scoring periods backward.",
        handler="fantasy_sports.commands.transactions:transactions",
        shape=DataShape.COLLECTION,
        params=(
            Param(
                name="limit",
                help="Most-recent moves to return. Stops early at the upstream-call cap.",
                annotation=int,
                default=25,
            ),
        ),
    )
)

register(
    CommandSpec(
        name="free-agents",
        summary="List unrostered players, optionally filtered to one position.",
        handler="fantasy_sports.commands.free_agents:free_agents",
        shape=DataShape.COLLECTION,
        params=(
            Param(
                name="pos",
                help="Position filter, e.g. QB, RB, WR, TE, D/ST, K. Rejected if unknown.",
                annotation=str | None,
            ),
            Param(
                name="limit",
                help="Maximum players to return.",
                annotation=int,
                default=25,
            ),
            Param(name="week", help=_WEEK_HELP, annotation=int | None),
        ),
    )
)

register(
    CommandSpec(
        name="raw",
        summary="Pass a view straight through to ESPN and return its payload unmodified.",
        handler="fantasy_sports.commands.raw:raw",
        shape=DataShape.OBJECT,
        honors_no_raw=False,
        params=(
            Param(
                name="view",
                help="ESPN view name; repeat for several. Each is fetched and reported "
                "separately under its own key.",
                annotation=list[str] | None,
                required=True,
            ),
            Param(
                name="filter",
                help="JSON for the x-fantasy-filter header. Some views return a partial "
                "default set with a 200 without one.",
                annotation=str | None,
            ),
        ),
    )
)

register(
    CommandSpec(
        name="status",
        group="auth",
        summary="Report which credentials are configured, where from, and how old they are.",
        handler="fantasy_sports.commands.auth:status",
        shape=DataShape.OBJECT,
        takes_league=False,
    )
)

register(
    CommandSpec(
        name="doctor",
        summary="Run every health check: config, credentials, cache, journal, version, provider.",
        handler="fantasy_sports.commands.doctor:doctor",
        shape=DataShape.OBJECT,
        takes_league=False,
        params=(
            Param(
                name="live",
                help="Also attempt to reach each configured league's provider. "
                "Touches the network and requires credentials.",
                annotation=bool,
                default=False,
            ),
        ),
    )
)

register(
    CommandSpec(
        name="login",
        group="auth",
        summary="Store ESPN cookies in the Keychain. Prompts without echoing; never prints them.",
        handler="fantasy_sports.commands.auth:login",
        shape=DataShape.OBJECT,
        takes_league=False,
    )
)

register(
    CommandSpec(
        name="logout",
        group="auth",
        summary=(
            "Remove ESPN cookies from the Keychain and config.toml. "
            "Env vars are reported, not unset."
        ),
        handler="fantasy_sports.commands.auth:logout",
        shape=DataShape.OBJECT,
        takes_league=False,
    )
)

register(
    CommandSpec(
        name="list",
        group="journal",
        summary="List the mutation journal, newest first: each write, who made it, how it ended.",
        handler="fantasy_sports.commands.journal:journal_list",
        shape=DataShape.COLLECTION,
        takes_league=False,
        params=(
            Param(
                name="league",
                help="Only entries for this league: a profile name from config.toml, or a "
                "raw league id. Unlike read commands this filters; it does not select a target.",
                annotation=str | None,
                flags=("--league", "-l"),
            ),
            Param(
                name="limit",
                help="Most-recent entries to return.",
                annotation=int,
                default=25,
            ),
            Param(
                name="irreversible",
                help="Only entries that cannot be reversed (add, drop, claim): the ones a "
                "human would have to undo by hand.",
                annotation=bool,
                default=False,
            ),
        ),
    )
)

register(
    CommandSpec(
        name="digest",
        group="journal",
        summary="Journal entries since a time, newest first, for a cron job or agent to report.",
        handler="fantasy_sports.commands.journal:journal_digest",
        shape=DataShape.COLLECTION,
        takes_league=False,
        params=(
            Param(
                name="since",
                help="Start of the window: a duration (24h, 7d, 30m) or an ISO-8601 UTC "
                "timestamp (2026-10-14T08:00:00Z). Entries recorded at or after it.",
                annotation=str,
                required=True,
            ),
            Param(
                name="irreversible",
                help="Only entries that cannot be reversed (add, drop, claim): the ones a "
                "human would have to undo by hand.",
                annotation=bool,
                default=False,
            ),
            Param(
                name="league",
                help="Only entries for this league: a profile name from config.toml, or a "
                "raw league id. Filters; it does not select a target.",
                annotation=str | None,
                flags=("--league", "-l"),
            ),
            Param(
                name="limit",
                help="Most-recent entries to return.",
                annotation=int,
                default=100,
            ),
        ),
    )
)

register(
    CommandSpec(
        name="show",
        group="journal",
        summary="Show one journal entry in full: prior and applied state, acting member, cost.",
        handler="fantasy_sports.commands.journal:journal_show",
        shape=DataShape.OBJECT,
        takes_league=False,
        params=(
            Param(
                name="id",
                help="Entry id, as printed by `journal list`. An unknown id fails and "
                "names the nearest ones.",
                annotation=str,
                required=True,
            ),
        ),
    )
)
