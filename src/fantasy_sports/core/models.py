"""The normalized domain model — ADR-0002 as amended, origin R2/R3.

**Normalize shape, not semantics.** Teams, rosters, standings, matchups,
transactions, and free agents get a shared shape. Scoring settings, draft
logic, playoff formats, and cross-provider player identity explicitly do not —
they are reachable only through ``raw``. Every object here carries
``provider``, ``provider_id``, and ``raw`` so nothing an adapter saw is ever
lost.

ADR-0002's amendment matters for reading this file: normalization is selected
for **legibility per provider**, not for the cross-provider intersection. A
field may exist because ESPN exposes it usefully even where Sleeper would
leave it empty. Every such field is optional, and *absence is never drift*.

Three shapes here exist because of specific, documented traps
(``docs/research/02-provider-data-shapes.md`` §§4, 6):

* :attr:`Team.owner_names` is plural. ESPN's ``owners`` is a list from day one
  and Yahoo's ``managers`` carries an ``is_comanager`` flag; a single
  ``owner_name`` silently loses a manager in every co-managed league.
* :class:`Matchup` is symmetric (``team_a``/``team_b``), not home/away. Only
  ESPN has that distinction, and the other two would have to fake it.
* :class:`Matchup` surfaces *both* period identifiers. ESPN's
  ``matchupPeriodId`` and ``scoringPeriodId`` diverge during playoff weeks, and
  an adapter that keeps only one has already lost the information. The
  untouched provider values stay in ``raw`` as well.

Slot eligibility **is** modelled, on :class:`Player`. ARCHITECTURE §14's
finding 13 excluded it; origin R3 voids that finding, because an agent cannot
construct a legal lineup from position alone. Providers that do not publish
eligibility as player data (Sleeper computes it client-side; Yahoo reports
only ``is_flex`` for the current slot) leave the tuple empty rather than
synthesising conventions their API never states.

Nothing here imports anything beyond the standard library, and nothing here is
allowed to import ``typer``, ``click``, ``rich``, or ``espn_api``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import MISSING, dataclass, field, fields
from datetime import datetime
from typing import Any, ClassVar, Final, Literal, Self

from fantasy_sports.core.errors import SchemaDriftError

__all__ = [
    "NORMALIZED_MODELS",
    "SESSION_ROLES",
    "TRANSACTION_TYPES",
    "CredentialSpec",
    "FreeAgent",
    "collect_untrusted",
    "League",
    "BoxScore",
    "LineupEntry",
    "Matchup",
    "PendingClaim",
    "Player",
    "ProviderObject",
    "RosterMove",
    "RosterSlot",
    "SessionRole",
    "Team",
    "Transaction",
    "WriteResult",
]

#: The narrowed, lossy, honest transaction vocabulary (research §4). ESPN's
#: ``TRADE_VETO`` / ``TRADE_PROPOSAL`` / ``WAIVER_ERROR`` states have no
#: normalized equivalent and are readable only via ``raw``.
TRANSACTION_TYPES: Final[tuple[str, ...]] = ("add", "drop", "trade", "waiver_claim")


class ProviderObject:
    """Mixin giving every normalized object its construction contract.

    Subclasses are frozen dataclasses. A field with no default is *required*;
    a field with a default is optional and its absence is ordinary provider
    data, not drift.

    :meth:`from_payload` takes a mapping keyed by **our** field names — the
    adapter has already translated the provider's own key names — so this
    layer stays free of ESPN vocabulary while still refusing to let a
    ``KeyError`` escape when an expected value stops arriving.
    """

    #: field name -> nested model class, coerced from a mapping on construction
    _NESTED: ClassVar[Mapping[str, type[ProviderObject]]] = {}
    #: field names normalized to a tuple so a frozen object is really immutable
    _SEQUENCES: ClassVar[frozenset[str]] = frozenset()
    #: field names an ESPN league member can set the *value* of (R1a, #17)
    _UNTRUSTED: ClassVar[frozenset[str]] = frozenset()

    def __post_init__(self) -> None:
        for name in self._SEQUENCES:
            value = getattr(self, name)
            if not isinstance(value, tuple):
                object.__setattr__(self, name, tuple(value))

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        """Build a model from a normalized payload mapping.

        Raises :class:`~fantasy_sports.core.errors.SchemaDriftError` — never a
        bare ``KeyError`` — when a required field is absent. Unknown keys are
        ignored: an upstream *addition* is not drift, and failing on one would
        break the tool every time ESPN ships a new field.
        """
        if not isinstance(payload, Mapping):
            raise SchemaDriftError(
                f"{cls.__name__} payload is {type(payload).__name__}, not a mapping",
                path=[cls.__name__],
            )

        values: dict[str, Any] = {}
        missing: list[str] = []
        for f in fields(cls):  # type: ignore[arg-type]  # always a dataclass subclass
            if f.name in payload:
                values[f.name] = cls._coerce(f.name, payload[f.name])
            elif f.default is MISSING and f.default_factory is MISSING:
                missing.append(f.name)

        if missing:
            provider = payload.get("provider")
            raise SchemaDriftError(
                f"{cls.__name__} payload is missing required field(s): {', '.join(missing)}",
                path=[f"{cls.__name__}.{name}" for name in missing],
                provider=provider if isinstance(provider, str) else None,
            )
        return cls(**values)

    @classmethod
    def _coerce(cls, name: str, value: Any) -> Any:
        nested = cls._NESTED.get(name)
        if nested is not None and isinstance(value, Mapping):
            return nested.from_payload(value)
        return value

    def to_dict(self) -> dict[str, Any]:
        """A plain-Python view for the output layer.

        Nested models expand **at any depth**, including inside a tuple or a
        list — a box score's lineups are a tuple of models, and leaving them
        unexpanded meant a caller reading ``data`` in Python saw dataclasses
        where a caller reading the same payload as JSON saw mappings. Two views
        of one contract that disagree is worse than either.

        Tuples become lists. ``raw`` is passed through by reference: it is the
        provider's payload, and copying it would be both wasteful and a chance
        to alter it.
        """
        return {
            f.name: _plain(getattr(self, f.name))
            for f in fields(self)  # type: ignore[arg-type]  # always a dataclass subclass
        }

    def untrusted(self) -> dict[str, str]:
        """Attacker-influenceable string fields on this object, path -> value.

        Empty unless the concrete class declares field names in
        :attr:`_UNTRUSTED` (R1a): any league member can set a team or league
        name, and it reaches an agent that can write, so it must be labeled
        separately from fields the provider itself controls (ids, records,
        timestamps). A declared field is either a plain ``str`` or a tuple of
        ``str`` — a plural free-text field, e.g. a co-owned team's names —
        indexed as ``field[0]``, ``field[1]``, ... Declaring any other field
        type is a class-definition error this raises on rather than silently
        mislabels.

        :func:`collect_untrusted` is the entry point a command actually
        calls: it also handles a bare object versus the list a collection
        command's ``data`` is, which this method does not know about.
        """
        paths: dict[str, str] = {}
        for name in sorted(self._UNTRUSTED):
            value = getattr(self, name)
            if isinstance(value, str):
                paths[name] = value
            elif isinstance(value, tuple):
                for index, item in enumerate(value):
                    paths[f"{name}[{index}]"] = str(item)
            else:
                raise TypeError(
                    f"{type(self).__name__}._UNTRUSTED names {name!r}, whose value is a "
                    f"{type(value).__name__}; only str and tuple-of-str fields are supported."
                )
        return paths


def _plain(value: Any) -> Any:
    """One value as plain Python, expanding models wherever they are nested."""
    if isinstance(value, ProviderObject):
        return value.to_dict()
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


def collect_untrusted(data: Any) -> dict[str, str]:
    """The envelope's ``untrusted`` map for a command's ``data`` (R1a, ADR-0004).

    Call this on the object(s) a command is about to hand to
    ``fantasy_sports.output.envelope.Envelope.success`` — a single
    :class:`ProviderObject` for an object-shaped command, or the list a
    collection command returns. Paths mirror where the value sits inside the
    rendered ``data``: a bare field name for a single object (``"name"``), or
    ``"[i].field"`` for item ``i`` of a list — the same indexing a consumer
    walking the JSON ``data`` array would use to find it.

    Anything that is not a :class:`ProviderObject`, or a list/tuple of them,
    contributes nothing: a command with no untrusted fields need not call
    this at all, and ``raw`` passthrough is untouched by design — it is
    neither a normalized field nor labeled, it is the documented raw-
    passthrough exception (``CLAUDE.md`` rule 3).
    """
    if isinstance(data, ProviderObject):
        return data.untrusted()
    if isinstance(data, list | tuple):
        paths: dict[str, str] = {}
        for index, item in enumerate(data):
            if isinstance(item, ProviderObject):
                for path, value in item.untrusted().items():
                    paths[f"[{index}].{path}"] = value
        return paths
    return {}


@dataclass(frozen=True)
class League(ProviderObject):
    """A league as every provider describes it."""

    _UNTRUSTED: ClassVar[frozenset[str]] = frozenset({"name"})
    """The league name is commissioner-set free text (R1a)."""

    provider: str
    provider_id: str
    """The provider's native league id, always a string — Yahoo's own client
    stringifies it to survive leading zeros, which ESPN's integer ids never
    have."""
    name: str
    season: int
    sport: str
    team_count: int
    current_week: int
    raw: Mapping[str, Any]
    roster_slots: Mapping[str, int] = field(default_factory=dict)
    """Slot name -> count, e.g. ``{"QB": 1, "RB": 2, "RB/WR/TE": 1, "BE": 7}``.

    R3a: a legal target lineup must be constructible from normalized output
    alone. ESPN publishes slot counts, Sleeper a repeated ``roster_positions``
    array, Yahoo position/count pairs — all three flatten to this mapping.
    Empty when a provider does not expose it."""


@dataclass(frozen=True)
class Team(ProviderObject):
    """A team and its record."""

    _SEQUENCES: ClassVar[frozenset[str]] = frozenset({"owner_names"})
    _UNTRUSTED: ClassVar[frozenset[str]] = frozenset({"name", "owner_names"})
    """Team name and owner display names are both member-set free text
    (R1a): ESPN lets any team owner rename their team and their own display
    name, and either can carry attacker-influenceable content."""

    provider: str
    provider_id: str
    name: str
    owner_names: tuple[str, ...]
    """Plural, always. Stored as a tuple because the object is frozen — a list
    field would be mutable in place and would undercut the guarantee."""
    wins: int
    losses: int
    ties: int
    points_for: float
    points_against: float
    raw: Mapping[str, Any]
    standing: int | None = None
    """The provider's own rank where it has one. ESPN and Sleeper compute
    standings client-side through a multi-rule tiebreaker cascade; Yahoo
    returns one server-side. Each adapter owns its own tiebreaker source —
    there is deliberately no shared standings algorithm in ``core/``."""


@dataclass(frozen=True)
class Player(ProviderObject):
    """A player, with the context R3 requires to make a lineup decision.

    ``provider_id`` is the provider's own player id and is **not** portable
    across providers, or even across sports within ESPN. Cross-provider player
    identity is explicitly out of scope (ADR-0002); a crosswalk is deferred to
    a future ADR.
    """

    _SEQUENCES: ClassVar[frozenset[str]] = frozenset({"eligible_slots"})

    provider: str
    provider_id: str
    name: str
    position: str
    """The provider's own position string, not normalized further. ESPN's
    ``RB/WR``, Sleeper's ``FLEX`` and Yahoo's flex boolean genuinely disagree."""
    raw: Mapping[str, Any]
    eligible_slots: tuple[str, ...] = ()
    """Slots this player may fill, in the provider's own slot vocabulary (R3).
    Empty where the provider does not publish eligibility as player data."""
    status: str | None = None
    injury_status: str | None = None
    pro_team: str | None = None
    opponent: str | None = None
    projected_points: float | None = None
    kickoff: datetime | None = None
    """Timezone-aware, always. ``espn-api`` hands back naive host-local
    datetimes; an adapter must re-derive this from the raw epoch value."""


@dataclass(frozen=True)
class RosterSlot(ProviderObject):
    """A player *in a roster context* — occupancy, not a bare player."""

    _NESTED: ClassVar[Mapping[str, type[ProviderObject]]] = {"player": Player}

    provider: str
    provider_id: str
    """The player's provider id; a slot has no id of its own."""
    player: Player
    slot: str
    """The lineup slot this player currently occupies."""
    is_starter: bool
    """Every provider gives some way to derive this: ESPN via a lineup slot
    that is not ``BE``/``IR``, Sleeper via membership of ``starters``, Yahoo
    via a selected position that is not ``BN``."""
    raw: Mapping[str, Any]
    is_locked: bool | None = None
    """Whether the slot can still be changed (R3). ``None`` where the provider
    does not report lock state."""

    @property
    def player_provider_id(self) -> str:
        """Alias for :attr:`provider_id`, for adapters reading research §3."""
        return self.provider_id


@dataclass(frozen=True)
class Matchup(ProviderObject):
    """One head-to-head pairing, symmetric by design."""

    provider: str
    provider_id: str
    """Synthesised by the adapter where the provider has no matchup id."""
    week: int
    """The **provider's matchup week**, not necessarily the NFL week."""
    team_a_provider_id: str
    team_a_score: float
    team_b_provider_id: str
    team_b_score: float
    is_playoff: bool
    raw: Mapping[str, Any]
    scoring_period_id: int | None = None
    """ESPN's scoring period. Diverges from :attr:`matchup_period_id` when a
    playoff matchup spans more than one scoring week."""
    matchup_period_id: int | None = None
    """ESPN's matchup period. Equal to :attr:`scoring_period_id` in the regular
    season, which is exactly why the split is easy to miss."""


@dataclass(frozen=True)
class LineupEntry(ProviderObject):
    """One player's line in one team's lineup for one week.

    The unit a fantasy box score is actually made of, and the thing a weekly
    recap is written from: who was started, who was benched, what each was
    projected for, and what each returned.
    """

    provider: str
    provider_id: str
    """The provider's player id."""
    raw: Mapping[str, Any]
    slot: str
    """Lineup slot as the provider names it, e.g. ``RB``, ``FLEX``, ``BE``.
    Slot vocabulary does not normalize across providers (ADR-0002)."""
    player_name: str
    position: str | None = None
    """The player's own position, which is not the slot they filled."""
    pro_team: str | None = None
    """The player's own NFL club that week, as the provider abbreviates it.
    Carried so a consumer can place the player in a game even when the
    opponent is unknown; ``None`` for a free agent."""
    pro_opponent: str | None = None
    """The club the player's team faces that week; ``None`` on a bye."""
    projected_points: float | None = None
    actual_points: float | None = None
    started: bool = False
    """Whether this slot counted toward the team's score. Derived from the
    slot, because 'is this a bench slot' is a provider question."""


@dataclass(frozen=True)
class BoxScore(ProviderObject):
    """One matchup with both lineups, player by player.

    Deliberately separate from :class:`Matchup` rather than an optional field
    on it. A box score costs extra upstream requests, is unavailable for some
    seasons entirely, and carries a different shape — folding it in would make
    every caller of ``matchups`` pay for detail most of them never read.
    """

    provider: str
    provider_id: str
    raw: Mapping[str, Any]
    week: int
    team_a_provider_id: str
    team_a_score: float
    team_a_lineup: tuple[LineupEntry, ...]
    team_b_provider_id: str
    team_b_score: float
    team_b_lineup: tuple[LineupEntry, ...]
    is_playoff: bool = False
    scoring_period_id: int | None = None
    matchup_period_id: int | None = None

    @property
    def team_a_bench_points(self) -> float:
        """Points left on team A's bench. Ordinary arithmetic, not a projection."""
        return round(sum(e.actual_points or 0.0 for e in self.team_a_lineup if not e.started), 2)

    @property
    def team_b_bench_points(self) -> float:
        return round(sum(e.actual_points or 0.0 for e in self.team_b_lineup if not e.started), 2)


@dataclass(frozen=True)
class Transaction(ProviderObject):
    """A roster move, narrowed to a four-value vocabulary.

    The narrowing is lossy and honest: filtering on ``type == "trade"`` will
    not show a proposed-then-declined trade, because ESPN's ``TRADE_PROPOSAL``
    and ``TRADE_DECLINE`` have no normalized equivalent. Read ``raw`` for
    those. An ESPN adapter must reconcile ``mTransactions2`` *and*
    ``kona_league_communication`` before building these.
    """

    _SEQUENCES: ClassVar[frozenset[str]] = frozenset({"players_in", "players_out"})

    provider: str
    provider_id: str
    type: str
    raw: Mapping[str, Any]
    team_provider_id: str | None = None
    players_in: tuple[str, ...] = ()
    """Player provider ids gained by :attr:`team_provider_id`."""
    players_out: tuple[str, ...] = ()
    faab_spent: int | None = None
    timestamp: datetime | None = None
    """When the provider processed the move. ``None`` when the payload carries
    neither a processed nor a proposed date — that is ordinary ESPN data and
    must not be reported as drift."""

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.type not in TRANSACTION_TYPES:
            raise SchemaDriftError(
                f"Transaction type {self.type!r} is outside the normalized vocabulary "
                f"{TRANSACTION_TYPES}; provider-specific states belong in `raw`",
                path=["Transaction.type"],
                provider=self.provider,
            )


@dataclass(frozen=True)
class FreeAgent(ProviderObject):
    """An unrostered player, plus ownership where the provider tracks it."""

    _NESTED: ClassVar[Mapping[str, type[ProviderObject]]] = {"player": Player}

    provider: str
    provider_id: str
    player: Player
    raw: Mapping[str, Any]
    percent_owned: float | None = None
    """Present on ESPN and Yahoo; ``None`` on Sleeper, which does not track
    per-player ownership the same way."""


@dataclass(frozen=True)
class CredentialSpec:
    """What a provider needs to authenticate, where it comes from, and how
    staleness is detected.

    **One shape, two callers.** ``Provider.credential_specs()`` describes what
    a provider *requires*; ``auth/chain.py`` resolves that description against
    the environment, the Keychain, and ``config.toml``. Those were briefly two
    dataclasses of the same name, built in parallel off the same commit
    (jwulff/fantasy-sports#35). They are one description of one thing, so they
    are one class: a provider that declares a credential and a chain that
    resolves it must not be able to disagree about what it is called.

    Only :attr:`name` and :attr:`label` are required. A provider describing its
    needs abstractly can stop there; the resolution fields carry defaults so
    declaring a credential never obliges an author to invent an environment
    variable for it. A provider needing no credentials at all — Sleeper —
    returns an empty list, which is itself a useful signal about that
    provider's operational risk profile.

    :attr:`staleness` describes *how expiry becomes visible*, not a predicted
    lifetime. No ESPN documentation, library source, or community post states
    a concrete cookie lifetime, so a fabricated one would either cry wolf on a
    live cookie or stay silent past a dead one.
    """

    name: str
    """Canonical name — the Keychain account, the config key, e.g. ``espn_s2``."""

    label: str
    """Human-facing name, e.g. ``ESPN_S2 cookie``."""

    env_vars: tuple[str, ...] = ()
    """Environment variables to check, in order. The namespaced one first.

    Empty means this credential has no environment fallback, which the chain
    handles by simply moving to the next link.
    """

    guidance: str = ""
    """One line telling a human where to find this value in DevTools."""

    secret: bool = True
    required: bool = True
    staleness: str | None = None


# --------------------------------------------------------------------------- #
# The write surface (jwulff/fantasy-sports#107)
# --------------------------------------------------------------------------- #

SESSION_ROLES: Final[tuple[str, ...]] = ("creator", "manager", "member")
"""What the session's member is in one league, most privileged first.

``creator`` is ESPN's ``isLeagueCreator``, the founding commissioner;
``manager`` is ``isLeagueManager``, a *granted* league manager. ESPN does not
mirror the first into the second, so a check reading only ``isLeagueManager``
reports the real commissioner as a plain member
(``docs/memory/commissioner-authority-is-two-flags-in-mnav.md``).
"""


@dataclass(frozen=True)
class SessionRole(ProviderObject):
    """Who the configured credentials act as in one league, and which teams
    they own.

    The role is recorded, never used to widen what a write may touch: the
    ownership guard refuses any team outside :attr:`owned_team_ids`
    regardless of role, because a commissioner's cookie is honoured by ESPN on
    another manager's team and a ``--team`` typo would be applied
    (``docs/memory/espn-lineup-write-authority-is-broader-than-team-ownership.md``).
    """

    _SEQUENCES: ClassVar[frozenset[str]] = frozenset({"owned_team_ids"})

    provider: str
    member_id: str | None
    """The session's member id **as this tool reports member ids** -- for
    ESPN, the SWID's pseudonym, never the SWID itself. ``None`` when it could
    not be determined, in which case :attr:`owned_team_ids` is empty and every
    write is refused."""
    role: Literal["creator", "manager", "member"]
    owned_team_ids: tuple[str, ...]
    """Team ids whose owners include :attr:`member_id`. Plural: ESPN records a
    co-manager as a second entry in ``owners``."""
    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.role not in SESSION_ROLES:
            raise ValueError(f"unknown session role {self.role!r}; expected one of {SESSION_ROLES}")


@dataclass(frozen=True)
class RosterMove:
    """One player entering or leaving a roster, as a caller asks for it.

    An *input* to a write, not something a provider returned, so it carries no
    ``provider`` or ``raw`` and is not a :class:`ProviderObject`. Slots are
    slot **names** in the provider's own vocabulary (``"BE"``, ``"RB/WR/TE"``);
    the adapter maps them to whatever its API wants. ``None`` means "let the
    provider choose", which for an add is the bench.
    """

    player_id: str
    from_slot: str | None = None
    to_slot: str | None = None


@dataclass(frozen=True)
class WriteResult(ProviderObject):
    """What the provider said when it accepted a write.

    Not in :data:`NORMALIZED_MODELS`: it describes a transaction this tool
    *sent*, not a league object it read, and it has no ``provider_id`` of a
    league entity -- :attr:`provider_transaction_id` is the join key into the
    provider's own transaction log.
    """

    _SEQUENCES: ClassVar[frozenset[str]] = frozenset({"items"})

    provider: str
    provider_transaction_id: str
    """ESPN's transaction UUID, the key ``mTransactions2`` files it under."""
    status: str
    """The provider's own status string, verbatim (``"EXECUTED"``, ...)."""
    is_pending: bool
    """``True`` for a queued claim that has not processed yet."""
    team_id: str
    items: tuple[Mapping[str, Any], ...]
    """The provider's own item list, as it echoed it."""
    credentials: Mapping[str, Any]
    """``{"espn_s2_age_seconds": int | None, "warning": str | None}``, computed
    before the request was sent (R12a), so an agent learns its cookie is old
    from a success rather than from the 401 that eventually follows."""
    raw: Mapping[str, Any]
    proposed_at: datetime | None = None
    """UTC, re-derived from the provider's epoch value; never naive."""
    bid_amount: int | None = None
    member_id: str | None = None
    """The member the provider attributed the write to, as it echoed it --
    the pseudonym, never the SWID. The journal attributes to this, not to an
    id we sent (we send none)."""


@dataclass(frozen=True, kw_only=True)
class PendingClaim(ProviderObject):
    """A waiver claim the provider has queued and not yet processed.

    Read from the provider's own queue (ESPN's ``view=mPendingTransactions``),
    which carries the transaction id that ``cancel-claim`` takes and which a
    claim leaves the moment it is withdrawn. Not :class:`Transaction`: that
    model narrows to completed moves, and ESPN's transaction log keeps a
    cancelled claim's own row reading ``PENDING``
    (``docs/memory/a-cancelled-claim-still-reads-pending.md``).

    Not in :data:`NORMALIZED_MODELS`, like :class:`SessionRole`: it is served
    by the writing protocol, because the only reason to list the queue by id is
    to act on it.
    """

    _SEQUENCES: ClassVar[frozenset[str]] = frozenset({"players_in", "players_out"})

    provider: str
    provider_id: str
    """The provider's transaction id: what ``cancel-claim --id`` takes, and the
    ``provider_transaction_id`` the ``claim`` that queued it returned."""
    team_id: str
    """The team that submitted the claim."""
    players_in: tuple[str, ...] = ()
    """Player ids the claim would add."""
    players_out: tuple[str, ...] = ()
    """Player ids the claim would drop if it processes."""
    bid_amount: int | None = None
    """The FAAB bid as the provider recorded it. ESPN records a bidless claim
    as ``0``."""
    proposed_at: datetime | None = None
    """When the claim was submitted, UTC."""
    processes_at: datetime | None = None
    """When the provider says waivers will process it, UTC, where it says."""
    status: str
    """The provider's own status, verbatim (``"PENDING"``)."""
    sub_order: int | None = None
    """The provider's ordering among one team's claims, where it reports one.
    ESPN's queue view has been seen without it."""
    raw: Mapping[str, Any]


#: Every normalized provider object. ``CredentialSpec`` is deliberately absent:
#: it describes a provider, not a thing a provider returned.
NORMALIZED_MODELS: Final[tuple[type[ProviderObject], ...]] = (
    League,
    Team,
    Player,
    RosterSlot,
    Matchup,
    Transaction,
    FreeAgent,
)
