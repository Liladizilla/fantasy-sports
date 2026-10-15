"""The ESPN adapter — the one provider that ships in v0.1.

It wraps ``cwendt94/espn-api`` rather than re-deriving ESPN's undocumented
payload shapes: the library's ``PLAYER_STATS_MAP``, its per-week ``proTeamId``
resolution for traded players, and its standings tiebreaker cascade are years of
reverse engineering we would otherwise repeat badly. What the library does
**not** do is everything this module exists for.

The six things this module adds on top of ``espn-api``
-------------------------------------------------------

**1. A transport seam under the library** (:class:`_Transport`). ``espn-api``
calls ``requests.get`` directly and hands the status code to
``checkRequestStatus``, which special-cases 401 and 404 and folds *everything
else* — 429 included — into one ``ESPNUnknownError`` carrying the status as
English. So ``RATE_LIMITED`` and its ``retry_after`` are ours to write, and they
have to be written *before* the library sees the status, because ``Retry-After``
is a header and the header does not survive into the exception. The same seam is
where the HTTP cache attaches (ARCHITECTURE §14.4): ``box_scores()`` and
``free_agents()`` fan out into two or three sequential requests with no internal
dedup, so a cache above them pays every round trip on a miss.

**2. Schema-drift detection.** ``espn-api`` offers nothing here. Every model
constructor does unguarded dict access — ``data['record']['overall']['wins']`` —
so a renamed field surfaces as a bare ``KeyError`` from six frames down, and a
``KeyError`` is indistinguishable from a bug in our own call. :func:`_mapped`
catches those, walks the traceback for the deepest ``espn_api`` frame, and
raises :class:`~fantasy_sports.core.errors.SchemaDriftError` carrying the view,
that frame, and the missing key as ``details.path``. That path is what makes the
canary's auto-filed issue actionable instead of a stack trace.

**3. Honest 401 classification.** A bare 401 from ESPN is ambiguous between
expired cookies and the wrong current-vs-historical URL shape for the season
(ARCHITECTURE §14 item 1). The double-probe that resolves it is **library-owned**
— ``checkRequestStatus`` swaps ``/leagueHistory/`` for ``/seasons/`` and retries
before raising — so our code never observes a bare 401 and re-implementing the
probe would double the request cost against a provider whose throttle behaviour
is unconfirmed. It is also **mandatory rather than a fallback**: ESPN's 401 body
does carry a machine-readable ``details[].type``, and on this path that type is a
*constant* — ``AUTH_LEAGUE_NOT_VISIBLE`` comes back identically for no cookies,
a bad ``espn_s2``, and a league the account was never in. The type is read
anyway, before the retry runs, because it varies elsewhere and is cheap. What we
add is the classification above it, which never reports expiry from evidence
that proves nothing. See :data:`AUTH_REASONS`.

**4. Timezone correctness.** ``espn-api`` builds every datetime with
``datetime.fromtimestamp()`` and no ``tz=``, producing naive, host-local values:
the same league renders a different kickoff in Seattle and on a UTC CI runner.
Nothing in this module passes one of those through. Kickoff times are re-derived
from the raw epoch milliseconds in the ``proTeamSchedules_wl`` payload and
transaction timestamps from the raw ``processDate``/``proposedDate``, both via
:func:`~fantasy_sports.output.envelope.from_epoch_millis`. The output layer
refuses a naive datetime with ``NaiveDatetimeError``, so a regression here fails
loudly rather than silently shifting every timestamp.

**5. Transaction reconciliation across ESPN's two surfaces.** ``mTransactions2``
and the ``kona_league_communication`` activity feed carry non-overlapping
vocabularies, and the activity feed synthesises *two* rows per trade
(``TRADE_SENT`` and ``TRADE_RECEIVED``). Building ``transactions`` on one surface
silently drops whatever routes only through the other, and
``core.Transaction`` must never inherit that asymmetry (research §6 item 2).
:meth:`EspnProvider.fetch_transactions` reads both and merges them.

**6. Telling the cache which seasons are over.** ``cache/tags.py`` caches a
season forever once it knows that season is behind the current one, but "what
season is it" is not a clock's business — the NFL calendar spans a year
boundary, and the answer that matters is ESPN's own. ``espn-api`` offers
nothing here either, so :meth:`_Transport._note_season_status` reads it
straight from ESPN's ``status`` block (``latestScoringPeriod`` vs.
``finalScoringPeriod``) the moment a response reveals it, and
:meth:`_Transport._upgrade_ttl` corrects the one entry — the league
bootstrap — whose own write already happened before that block was readable
(jwulff/fantasy-sports#44).

What ``raw`` carries, and where the whole response went
------------------------------------------------------

Each normalized object's ``raw`` is **the provider's own sub-object** — the slice
of the response that describes that thing — not the whole payload. A league
bootstrap is 800 KB, of which 730 KB is rosters; putting all of it on every
``Team`` would make ``fantasy-sports teams`` unreadable and unusable.

Nothing is lost, because R1 is satisfied a level up: every upstream response that
contributed to a call is retained verbatim, keyed by the request that produced
it, on :attr:`EspnProvider.last_fetch`. That is also where the envelope's
``sources`` come from, and it is why composite reads report each contributing
fetch separately instead of collapsing to one.

Constraints this module lives under
-----------------------------------

``espn_api`` and ``requests`` are imported **inside functions**, never at module
scope: ADR-0008 budgets ``--help`` at 50 ms and ``tests/unit/test_imports.py``
asserts both are absent from ``sys.modules`` on the cheap paths. Nothing here
may import ``typer``, ``click``, or ``rich`` (an AST test enforces it), and
nothing here may log, format, or record a credential — library exception text is
run through :func:`~fantasy_sports.core.redaction.scrub_credential_patterns`
before it reaches an error message, because ``espn-api`` interpolated the raw
``espn_s2`` value into its own access-denied message until 2026-02.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from fantasy_sports.cache.tags import TTL_SECONDS, RequestContext, Resource, tags_for, ttl_for
from fantasy_sports.core.errors import (
    AuthExpiredError,
    AuthMissingError,
    ConfigInvalidError,
    FantasySportsError,
    LeagueNotFoundError,
    NotAvailableError,
    ProviderUnavailableError,
    RateLimitedError,
    SchemaDriftError,
    WriteDivergedError,
    WriteRejectedError,
)
from fantasy_sports.core.models import (
    BoxScore,
    CredentialSpec,
    FreeAgent,
    League,
    LineupEntry,
    Matchup,
    Player,
    RosterMove,
    RosterSlot,
    SessionRole,
    Team,
    Transaction,
    WriteResult,
)
from fantasy_sports.core.redaction import (
    CASSETTE_SWID_SALT,
    remember_secret,
    scrub_body,
    scrub_credential_patterns,
    swid_pseudonym,
)
from fantasy_sports.output.envelope import (
    DataSource,
    from_epoch_millis,
    from_epoch_seconds,
    utc_now,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from fantasy_sports.cache.store import CacheMode, CacheStore

__all__ = [
    "AUTH_REASONS",
    "PROVIDER",
    "STARTER_EXCLUDED_SLOTS",
    "WRITE_TIMEOUT_SECONDS",
    "WRITE_URL",
    "EspnProvider",
    "FetchRecord",
    "RawResponse",
]

PROVIDER: Final[str] = "espn"

STARTER_EXCLUDED_SLOTS: Final[frozenset[str]] = frozenset({"BE", "IR", ""})
"""Lineup slots that are not a starting spot.

ESPN has no ``is_starter`` field; every provider makes you derive it. ``BE`` is
the bench, ``IR`` is injured reserve, and the empty string is what
``POSITION_MAP`` yields for slot 22, which ESPN uses as a filler.
"""

_ALL_TRANSACTION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "FREEAGENT",
        "WAIVER",
        "WAIVER_ERROR",
        "TRADE_ACCEPT",
        "TRADE_UPHOLD",
        "ROSTER",
        "DRAFT",
    }
)
"""The ``mTransactions2`` filter we send.

Broader than ``espn-api``'s default of waivers and free agents, because trades
live under ``TRADE_ACCEPT`` and a transactions command that cannot show a trade
is not a transactions command. ``TRADE_PROPOSAL``, ``TRADE_DECLINE``,
``TRADE_VETO``, and ``TRADE_ERROR`` are deliberately absent: they are proposals
and failures, not roster moves, and ``core.Transaction``'s four-value vocabulary
has no honest home for them (they remain readable through :meth:`fetch_raw`).
"""

_TRANSACTION_TYPE_MAP: Final[dict[str, str]] = {
    "WAIVER": "waiver_claim",
    "TRADE_ACCEPT": "trade",
    "TRADE_UPHOLD": "trade",
}
"""ESPN's transaction ``type`` to our four-value vocabulary.

``FREEAGENT`` is absent on purpose: it is ``add`` or ``drop`` depending on what
its items say, and deciding that needs the items rather than the type.
"""

_ACTIVITY_TYPE_MAP: Final[dict[str, str]] = {
    "FA ADDED": "add",
    "WAIVER ADDED": "waiver_claim",
    "DROPPED": "drop",
    "TRADE_SENT": "trade",
    "TRADE_RECEIVED": "trade",
}
"""The activity feed's *separate* vocabulary. This is the asymmetry research §6
item 2 warns about: neither surface's strings appear in the other's."""


@dataclass(frozen=True)
class AuthReason:
    """What one ESPN 401 ``details[].type`` value actually proves."""

    explanation: str
    proves_credentials_bad: bool = False
    """Whether this reason is positive evidence that the *credential* failed.

    ``False`` everywhere so far, and that is the finding, not an oversight.
    """


AUTH_REASONS: Final[dict[str, AuthReason]] = {
    "AUTH_LEAGUE_NOT_VISIBLE": AuthReason(
        explanation=(
            "ESPN says this league is not visible to whatever was sent. Probed "
            "against a real private league on 2026-09-05, that response is "
            "byte-identical for no cookies, a valid SWID with an invalid espn_s2, "
            "and either cookie alone -- so it distinguishes nothing."
        ),
        proves_credentials_bad=False,
    ),
}
"""Observed ``details[].type`` values in ESPN's 401 body, and what each proves.

ESPN's 401 body is not bare -- it carries ``details[].type``, a machine-readable
discriminator -- and reading it costs nothing. But on the league-read path it is
a **constant**: probing a real private league on 2026-09-05, one variable at a
time, ``AUTH_LEAGUE_NOT_VISIBLE`` came back identically for no cookies at all, a
valid ``SWID`` with an invalid ``espn_s2``, and each cookie sent alone. ESPN does
not distinguish "your cookie is bad" from "this league is not yours" from "you
sent nothing".

So this table exists to be *read*, not to be trusted as evidence about the
credential, and no entry in it maps to ``AUTH_EXPIRED``. Telling a user to
re-extract cookies that were fine is precisely the misdiagnosis ARCHITECTURE
§14 item 1 exists to prevent, and it is the reason the alternate-URL-shape
double-probe stays mandatory rather than becoming a fallback.

The honest asymmetry, and what this module encodes: *nothing was sent* is
cheaply distinguishable and reports ``AUTH_MISSING``; *expired* versus
*malformed* versus *not your league* is not distinguishable from this response
and reports ``LEAGUE_NOT_FOUND``, whose ``agent_action`` -- confirm the id and
the access -- is right for all three. ``AUTH_EXPIRED`` is reachable only from a
reason type that positively proves the credential failed, and no such type has
been observed yet. Add one here the first time ESPN produces it.

See ``docs/memory/espn-401-tells-you-nothing.md``, which also records the
``fan.api.espn.com`` membership probe: it answers "is this league even mine?",
but it answers ``200`` with no cookies at all, so it is not a credential check.
"""

_VIEW_RESOURCES: Final[dict[str, Resource]] = {
    "mTeam": Resource.LEAGUE_SETTINGS,
    "mSettings": Resource.LEAGUE_SETTINGS,
    "mDraftDetail": Resource.LEAGUE_SETTINGS,
    # Who the session is and which teams it owns. Changes when the commissioner
    # grants LM powers or a team changes hands -- a settings-rate event.
    "mNav": Resource.LEAGUE_SETTINGS,
    "mRoster": Resource.ROSTER,
    "mMatchup": Resource.MATCHUPS,
    "mMatchupScore": Resource.MATCHUPS,
    "mScoreboard": Resource.MATCHUPS,
    "mStandings": Resource.STANDINGS,
    "mTransactions2": Resource.TRANSACTIONS,
    "kona_league_communication": Resource.TRANSACTIONS,
    "kona_player_info": Resource.FREE_AGENTS,
    "mPositionalRatings": Resource.FREE_AGENTS,
    "players_wl": Resource.PLAYERS,
    "proTeamSchedules_wl": Resource.PRO_TEAM_SCHEDULES,
}
"""ESPN view name to cache resource class.

The cache never inspects a URL to guess this — deriving it from a URL shape
would put ESPN's routing conventions inside a provider-agnostic layer
(``cache/tags.py``). ``players_wl`` and ``proTeamSchedules_wl`` map to the
season-scoped class: they come from a season endpoint carrying no league id and
are re-fetched by every league, so tagging them league-scoped would either evict
one league's copy on a write to another or lose the cross-league hit entirely.
"""

_SEASON_SCOPED_VIEWS: Final[frozenset[str]] = frozenset({"players_wl", "proTeamSchedules_wl"})

WRITE_URL: Final[str] = (
    "https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
    "/segments/0/leagues/{league_id}/transactions/"
)
"""The write host (brief §1.1). Same path family as reads, different host, and
the trailing slash the web client sends."""

WRITE_TIMEOUT_SECONDS: Final[float] = 15.0
"""How long one write may take before its outcome is declared unknown.

Every write probed answered in 70-110 ms server-side (brief §2.4), so 15 s is
over a hundred times the observed latency: long enough that a slow-but-healthy
response is not cut off, because a timeout is the expensive outcome here -- it
leaves the transaction in an unknown state that the caller must resolve by
reading. Short enough that an agent writing against a kickoff deadline is not
hung for a minute. Never retried (brief §8 item 1)."""

_WRITE_HEADERS: Final[Mapping[str, str]] = {
    "Content-Type": "application/json",
    "x-fantasy-source": "kona",
    "x-fantasy-platform": "kona-PROD",
}
"""What the web client sends. The two ``x-fantasy-*`` headers are optional
(brief §2.2, p4/p15r); they are kept so the request is indistinguishable from
the web client's, and their absence is never a debugging lead."""

_TRAN_KINDS: Final[Mapping[str, str]] = {
    "TRAN_LINEUP_LOCKED": "roster_lock",
    "TRAN_ROSTER_SLOT_LIMIT_EXCEEDED": "slot_limit",
    "TRAN_ROSTER_SAME_SLOT": "same_slot",
    "TRAN_INVALID_SCORINGPERIOD_NOT_CURRENT": "not_current_period",
    "TRAN_ROSTER_LIMIT_EXCEEDED_ONE": "roster_full",
    "TRAN_PLAYER_NOT_ON_TEAM": "already_dropped",
}
"""ESPN's observed ``TRAN_*`` rejection types to our closed ``WRITE_REJECTED``
vocabulary: the lineup reasons from brief §5.1, and the add/drop reasons from
``06-espn-add-drop-surface.md``. Anything else is ``"unknown"`` with ESPN's
string kept verbatim in ``provider_type``: reported, not lost, and mapping it
later is not an API change.

Observed and deliberately left ``"unknown"`` (06 §5): the availability pair
``TRAN_PLAYER_NOT_FREEAGENT`` (on waivers; claim instead) and
``TRAN_PLAYER_NOT_AVAILABLE`` (on another roster), which want a kind of their
own that the vocabulary does not have yet; ``TRAN_ACQUISITION_BUDGET_NOT_SUPPORTED``
(a bid in a league without a budget), which is not ``budget_exceeded``; and
the three item-shape reasons, which are our bug rather than a user's."""

_UNKNOWN_STATE_REMEDIATION: Final[str] = (
    "The transaction may have applied. Read `transactions` (and `claims` for a "
    "waiver) before retrying; a blind retry of a claim can spend the bid twice."
)

_BENCH_SLOT_IDS: Final[frozenset[int]] = frozenset({20, 21})
"""``BE`` and ``IR``: moves *into* these are emitted first, so a transaction
reads vacate-then-fill in the journal (brief §3; ESPN itself validates the
final state and does not care)."""


# --------------------------------------------------------------------------- #
# What one call fetched
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RawResponse:
    """One upstream ESPN response, keyed by the request that produced it."""

    view: str
    """The ``view=`` value, or ``+``-joined values for a combined request. This
    is the request *descriptor*, never a URL: a URL can carry an ``espn_s2``
    query parameter and this name reaches stdout through the envelope."""

    payload: Any
    fetched_at: datetime
    cached: bool = False

    def as_source(self) -> DataSource:
        return DataSource(name=self.view, fetched_at=self.fetched_at, cached=self.cached)


@dataclass(frozen=True)
class FetchRecord:
    """Every upstream response that contributed to one read (origin R1).

    A composite read fans out — a box score is three requests, free agents is
    three — and a single payload would drop data. Keyed by request descriptor so
    a caller can tell which view a field came from, and so the envelope can
    report each contributing fetch's own age rather than one blended number.
    """

    responses: Mapping[str, RawResponse]

    @property
    def sources(self) -> tuple[DataSource, ...]:
        """The envelope's ``sources``, oldest first."""
        return tuple(
            response.as_source()
            for response in sorted(self.responses.values(), key=lambda r: r.fetched_at)
        )

    def payload(self, view: str) -> Any:
        """The raw payload recorded for ``view``, or ``None``.

        Accepts a bare view name as well as a full key, so a caller that does
        not care which scoring period a repeated view was fetched for does not
        have to know the key shape. The most recent fetch wins.
        """
        found = self.responses.get(view) or _latest(self.responses, view)
        return None if found is None else found.payload


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def _clean(text: object) -> str:
    """Library text with credential *shapes* removed.

    Two mechanisms, both needed. ``FantasySportsError`` scrubs values this
    process was handed, which covers our own cookies; this covers the shapes we
    were never handed. ``espn-api`` interpolated ``self.cookies['espn_s2']``
    into its access-denied message until commit ``78c239a`` (2026-02-15), so
    passing library text through verbatim is a known leak path, not a
    hypothetical one.
    """
    return scrub_credential_patterns(str(text))


def _espn_frame(exc: BaseException) -> str | None:
    """The deepest ``espn_api`` frame in ``exc``'s traceback, as ``module.func``.

    This is the context capture that makes ``SCHEMA_DRIFT`` actionable. A bare
    ``KeyError('record')`` says a key is missing; ``espn_api.football.team.__init__``
    plus that key says *ESPN changed the team payload*, which is a filable
    issue rather than a stack trace.
    """
    frame: str | None = None
    traceback = exc.__traceback__
    while traceback is not None:
        module = traceback.tb_frame.f_globals.get("__name__", "")
        if module.startswith("espn_api"):
            frame = f"{module}.{traceback.tb_frame.f_code.co_name}"
        traceback = traceback.tb_next
    return frame


def _missing_key(exc: BaseException) -> str | None:
    """The key a ``KeyError`` names, when it is safe to report.

    A ``KeyError``'s argument is normally a field name, which is exactly what
    ``details.path`` is for. It can also be an *id* — ``player_map[playerId]``
    raises with an integer — and an id is provider data rather than a field
    name, so a non-string key is reported by type only. Nothing that could
    carry a credential ever reaches the payload.
    """
    if not isinstance(exc, KeyError) or not exc.args:
        return None
    key = exc.args[0]
    if isinstance(key, str):
        return key
    return f"<{type(key).__name__}>"


def _retry_after_seconds(value: str | None) -> float | None:
    """``Retry-After`` as seconds. Accepts the delta form and the HTTP-date form.

    Returns ``None`` for anything unparseable rather than guessing: an invented
    backoff is worse than none, because a caller will trust it.
    """
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - utc_now()).total_seconds())


# --------------------------------------------------------------------------- #
# The transport seam
# --------------------------------------------------------------------------- #


class _Transport:
    """``espn-api``'s HTTP calls, routed through us.

    Installed by replacing ``league_get`` and ``get`` on one
    ``EspnFantasyRequests`` *instance* — not by patching the class or the
    ``requests`` module, both of which would leak into every other league object
    in the process and into any other library sharing ``requests``.

    Three things happen here that cannot happen anywhere else:

    * **429 is intercepted before the library's status check.** ``espn-api``
      folds every non-200/401/404 into ``ESPNUnknownError("ESPN returned an
      HTTP 429")`` and never reads ``Retry-After``. The header exists only on
      the response object, which the exception does not carry, so this is the
      only place ``retry_after`` can be obtained.
    * **A 401 body is read before the library retries.** ``checkRequestStatus``
      swaps the URL shape and retries — correctly, and we let it — but it does
      not surface ESPN's ``details[].type``. It is captured here first.
    * **The cache attaches below the composite calls**, keyed on URL plus params
      plus the ``x-fantasy-filter`` header, which is a dimension that changes
      the response and does not appear in the URL.
    """

    def __init__(
        self,
        espn_request: Any,
        *,
        season: int,
        league_id: str,
        store: CacheStore | None = None,
        mode: CacheMode | None = None,
        http: Callable[..., Any] | None = None,
        now: Callable[[], datetime] = utc_now,
        on_auth_reason: Callable[[str | None], None] | None = None,
    ) -> None:
        self._request = espn_request
        self._season = season
        self._league_id = league_id
        self._store = store
        self._mode = mode
        self._call_mode: CacheMode | None = None
        """A mode for the requests inside :meth:`mode_for_call`, overriding
        :attr:`_mode` without changing what the provider was configured with."""
        self._http = http
        self._now = now
        self._on_auth_reason = on_auth_reason
        self.responses: dict[str, RawResponse] = {}
        self.completed_through: int | None = None
        """Scoring periods at or below this are finished and cache forever."""
        self.current_season: int | None = None
        """The season in progress, once known -- see :meth:`_note_season_status`.

        Unlike ``completed_through``, this is not something we can compute up
        front from ``current_week``. It has to come from ESPN's own ``status``
        block (``latestScoringPeriod`` vs. ``finalScoringPeriod``), which lives
        inside a response body -- and the first request of a cold league is the
        one that would benefit most, whose ``RequestContext`` is necessarily
        built *before* that body exists. So this starts ``None`` and is filled
        in the moment a response reveals it, then reused by :meth:`_context`
        for every later request in this league.
        """

        # Instance-level replacement, so nothing outside this league is touched.
        espn_request.league_get = self.league_get
        espn_request.get = self.get

    # --- the two methods espn-api calls ----------------------------------- #

    def league_get(
        self,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        extend: str = "",
    ) -> Any:
        payload = self._fetch(
            self._request.LEAGUE_ENDPOINT + extend,
            params,
            headers,
            extend=extend,
            league_scoped=True,
        )
        # The pre-2018 ``/leagueHistory/`` shape answers with a JSON *list*.
        # ``espn-api`` unwraps it here and every caller downstream assumes the
        # unwrap has happened.
        return payload[0] if isinstance(payload, list) else payload

    def get(
        self,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        extend: str = "",
    ) -> Any:
        # The season endpoint. Not unwrapped: ``players_wl`` legitimately
        # answers with a list and ``_fetch_players`` iterates it.
        return self._fetch(
            self._request.ENDPOINT + extend,
            params,
            headers,
            extend="",
            league_scoped=False,
        )

    # --- the seam --------------------------------------------------------- #

    def _fetch(
        self,
        url: str,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        *,
        extend: str,
        league_scoped: bool,
    ) -> Any:
        view = _view_of(params)
        body, cached, stored, fetched_at = self._body(
            url, params, headers, extend=extend, view=view
        )
        try:
            payload = json.loads(body)
        except (ValueError, TypeError) as exc:
            raise SchemaDriftError(
                f"ESPN's {view} response is not JSON.",
                path=[view],
                provider=PROVIDER,
                details={"view": view},
            ) from exc
        self._note_season_status(
            payload,
            body=body,
            url=url,
            params=params,
            headers=headers,
            view=view,
            stored=stored,
        )
        self.responses[_record_key(view, params)] = RawResponse(
            view=view,
            payload=payload,
            # A cache hit is timestamped when the entry was *written*, never
            # when it was *read* — otherwise every hit reports itself as
            # brand new (jwulff/fantasy-sports#51). ``_body`` only returns a
            # ``fetched_at`` for a hit; every other path (live, ``--fresh``,
            # ``--no-cache``) is genuinely "now".
            fetched_at=fetched_at if fetched_at is not None else self._now(),
            cached=cached,
        )
        return payload

    def _note_season_status(
        self,
        payload: Any,
        *,
        body: str,
        url: str,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        view: str,
        stored: bool,
    ) -> None:
        """Learn ``current_season`` from ``payload``, the first time it appears.

        ESPN's own ``status`` block -- ``latestScoringPeriod`` vs.
        ``finalScoringPeriod`` -- says whether *this* season has played out,
        which is the signal ``ttl_for`` needs (jwulff/fantasy-sports#44). Not
        every view carries it (only the league bootstrap and anything
        including ``mMatchupScore`` do), so this is a no-op until one does, and
        a no-op again once ``current_season`` is known.

        A season that has not finished is the current one: ESPN has no notion
        of "an old season still being played," so ``current_season`` is set to
        this request's own season in that case, which makes
        ``context.season < context.current_season`` false -- ordinary TTLs,
        correctly.
        """
        if self.current_season is not None:
            return
        finished = _season_finished(payload)
        if finished is None:
            return
        self.current_season = self._season + 1 if finished else self._season
        if finished and stored and self._store is not None:
            self._upgrade_ttl(url, params, headers, view=view, body=body)

    def _body(
        self,
        url: str,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        *,
        extend: str,
        view: str,
    ) -> tuple[str, bool, bool, datetime | None]:
        """The response body, from the cache where one is configured.

        The third element is whether *this call* wrote the entry -- distinct
        from a cache hit, and what :meth:`_note_season_status` needs to decide
        whether there is anything of its own left to correct. The fourth is
        the entry's true fetch time on a cache hit, and ``None`` on anything
        else -- a live fetch, a ``--fresh`` refresh, or a ``--no-cache`` bypass
        all happen "now", which the caller already knows without our help.
        """
        if self._store is None:
            body = _as_text(self._live(url, params, headers, extend=extend, view=view))
            return body, False, False, None

        from fantasy_sports.cache.store import CachingFetcher

        mode = self._call_mode if self._call_mode is not None else self._mode
        fetcher = CachingFetcher(
            lambda fetch_url, fetch_params: self._live(
                fetch_url, fetch_params, headers, extend=extend, view=view
            ),
            self._store,
            **({} if mode is None else {"mode": mode}),
        )
        result = fetcher.fetch(
            url,
            params,
            context=self._context(view, params),
            extra=_filter_dimension(headers),
        )
        fetched_at = (
            from_epoch_seconds(result.fetched_at)
            if result.cached and result.fetched_at is not None
            else None
        )
        return result.body, result.cached, result.stored, fetched_at

    @contextmanager
    def mode_for_call(self, mode: CacheMode) -> Iterator[None]:
        """Use ``mode`` for the requests made inside this block, then restore.

        Per call, not per provider: the write path needs one guaranteed-live
        read (the roster it diffs against) without turning every other read
        on the instance into a bypass. The store still scrubs the body.
        """
        previous = self._call_mode
        self._call_mode = mode
        try:
            yield
        finally:
            self._call_mode = previous

    def _context(self, view: str, params: Mapping[str, Any] | None) -> RequestContext:
        """What the cache needs to know about this request.

        A combined request takes the **shortest** TTL of its views. The league
        bootstrap sends ``mSettings`` (a day) alongside ``mRoster`` (five
        minutes) in one call, and serving a day-old roster because the request
        also happened to carry settings is the expensive direction to be wrong
        in.
        """
        views = view.split("+")
        resource = min(
            (_VIEW_RESOURCES.get(name, Resource.UNKNOWN) for name in views),
            key=lambda item: TTL_SECONDS[item],
            default=Resource.UNKNOWN,
        )
        week = _as_int(None if params is None else params.get("scoringPeriodId"))
        season_scoped = resource in (Resource.PLAYERS, Resource.PRO_TEAM_SCHEDULES)
        return RequestContext(
            provider=PROVIDER,
            resource=resource,
            season=self._season,
            league_id=None if season_scoped else self._league_id,
            week=week,
            completed=(
                week is not None
                and self.completed_through is not None
                and week <= self.completed_through
            ),
            current_season=self.current_season,
        )

    def _upgrade_ttl(
        self,
        url: str,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        *,
        view: str,
        body: str,
    ) -> None:
        """Re-write the entry :meth:`_body` just wrote, now with the TTL it
        should have had.

        :meth:`_context` had no way to know this season was historical when it
        built the context this call's write used -- the signal lives inside
        the very body that write is storing. The entry already exists with an
        ordinary TTL; this corrects it in place with the same key, same body,
        and the context :meth:`_context` would build now that
        ``current_season`` is set. Cheap: one SQLite upsert on the key that was
        just written, not a purge or a second fetch.
        """
        from fantasy_sports.cache.store import cache_key, canonical_url

        assert self._store is not None  # only called when it is (see caller)
        key = cache_key(url, params, extra=_filter_dimension(headers))
        context = self._context(view, params)
        self._store.put(
            key,
            body,
            tags=tags_for(context),
            ttl=ttl_for(context),
            url=canonical_url(url, params),
        )

    def _live(
        self,
        url: str,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        *,
        extend: str,
        view: str,
    ) -> Any:
        """One real HTTP request, with the two things ``espn-api`` does not do."""
        response = self._send(url, params, headers)
        status = response.status_code

        if status == 429:
            # Before ``checkRequestStatus``, which would collapse this into a
            # generic ``ESPNUnknownError`` and discard the header.
            raise RateLimitedError(
                "ESPN throttled this request (HTTP 429).",
                retry_after=_retry_after_seconds(response.headers.get("Retry-After")),
                remediation="Wait for `details.retry_after` seconds, then retry.",
                details={"status": status, "view": view},
            )

        if status == 401:
            # Captured before the library's alternate-shape retry, which does
            # not surface the body. If the retry succeeds this is discarded.
            self._report_auth_reason(_auth_reason(response))

        alternate = self._request.checkRequestStatus(
            status, extend=extend, params=params, headers=headers
        )
        if alternate is not None:
            # The alternate URL shape worked: this was never an auth failure,
            # it was the current-vs-historical season shape. This is the whole
            # reason the double-probe is mandatory rather than a fallback --
            # the 401 body says the same thing either way.
            self._report_auth_reason(None)
            return json.dumps(alternate)
        return response.content

    def _report_auth_reason(self, reason: str | None) -> None:
        if self._on_auth_reason is not None:
            self._on_auth_reason(reason)

    def _send(self, url: str, params: Mapping[str, Any] | None, headers: Mapping[str, str] | None):
        if self._http is not None:
            return self._http(url, params=params, headers=headers, cookies=self._request.cookies)
        import requests

        return requests.get(url, params=params, headers=headers, cookies=self._request.cookies)


def _as_text(body: Any) -> str:
    if isinstance(body, bytes):
        return body.decode("utf-8")
    return str(body)


def _as_int(value: Any) -> int | None:
    """``value`` as an ``int``, refusing ``bool``.

    ``isinstance(True, int)`` is ``True``, and a ``week=True`` that becomes
    ``1`` is a wrong answer that looks like a right one.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _season_finished(payload: Any) -> bool | None:
    """Whether the season ``payload`` describes has played out, or ``None`` if
    ``payload`` carries no signal at all.

    ESPN's ``status`` block gives two scoring-period numbers for the league
    endpoint: ``latestScoringPeriod``, how far ESPN's own data currently
    extends, and ``finalScoringPeriod``, the season's last possible one. The
    season is over once the former has reached the latter -- the same
    comparison ``espn-api``'s own ``current_week`` derivation makes
    (``base_league.py``: ``scoringPeriodId if scoringPeriodId <=
    finalScoringPeriod else finalScoringPeriod``). Not every view's response
    carries ``status`` -- ``players_wl``, ``proTeamSchedules_wl``,
    ``mDraftDetail`` and several others do not -- so ``None`` here just means
    this particular payload has nothing to say, not that the season is
    unfinished.
    """
    if not isinstance(payload, Mapping):
        return None
    status = payload.get("status")
    if not isinstance(status, Mapping):
        return None
    latest = _as_int(status.get("latestScoringPeriod"))
    final = _as_int(status.get("finalScoringPeriod"))
    if latest is None or final is None:
        return None
    return latest >= final


def _view_of(params: Mapping[str, Any] | None) -> str:
    """The request descriptor: the ``view=`` value, or ``+``-joined values."""
    if not params or "view" not in params:
        return "unknown"
    view = params["view"]
    if isinstance(view, str):
        return view
    return "+".join(str(item) for item in view)


def _record_key(view: str, params: Mapping[str, Any] | None) -> str:
    """Where one response is filed on :class:`FetchRecord`.

    The view name alone is not enough. ``fetch_transactions(since=...)`` asks
    ``mTransactions2`` about every scoring period in the season, and filing
    seventeen distinct responses under one key keeps the last one and silently
    drops sixteen -- which is exactly the data loss R1 exists to prevent. The
    scoring period is the only parameter that varies for a repeated view, so it
    is the only one in the key; ``RawResponse.view`` keeps the bare name for
    lookups that do not care.
    """
    period = None if params is None else _as_int(params.get("scoringPeriodId"))
    return view if period is None else f"{view}@{period}"


def _latest(responses: Mapping[str, RawResponse], view: str) -> RawResponse | None:
    """The most recently fetched response for ``view``, whatever its key.

    Later wins, because a repeated view is a repeated *fetch*: after
    ``load_roster_week(3)`` the week-3 ``mRoster`` is the one the object model
    is actually holding.
    """
    found = [item for item in responses.values() if item.view == view]
    return max(found, key=lambda item: item.fetched_at) if found else None


def _filter_dimension(headers: Mapping[str, str] | None) -> dict[str, str] | None:
    """The ``x-fantasy-filter`` header, as a cache-key dimension.

    Free agents, transactions, the activity feed and the message board are all
    scoped by this header rather than by the URL. Two filters against one URL
    are two different payloads, so omitting it would serve the wrong body; the
    rest of the header set is excluded because it carries the ``Cookie``.
    """
    if not headers:
        return None
    for name, value in headers.items():
        if name.lower() == "x-fantasy-filter":
            return {"x-fantasy-filter": value}
    return None


def _auth_reason(response: Any) -> str | None:
    """ESPN's machine-readable 401 reason, from ``details[].type``.

    Read before the library's alternate-shape probe, which does not surface the
    body. Returns ``None`` when the body is not the documented shape — an
    unreadable body is not evidence of anything, and guessing from a 401 status
    alone is the misdiagnosis the double-probe exists to avoid.
    """
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - an unparseable 401 body is simply no evidence
        return None
    if not isinstance(body, Mapping):
        return None
    details = body.get("details")
    if not isinstance(details, Sequence) or isinstance(details, str | bytes):
        return None
    for detail in details:
        if isinstance(detail, Mapping) and isinstance(detail.get("type"), str):
            return detail["type"]
    return None


# --------------------------------------------------------------------------- #
# The provider
# --------------------------------------------------------------------------- #


class EspnProvider:
    """The ESPN adapter. Satisfies :class:`~fantasy_sports.providers.base.Provider`.

    ``isinstance(EspnProvider(), Provider)`` passes, and proves only that the
    methods *exist* — a ``runtime_checkable`` Protocol never checks a signature
    or a return type (``docs/memory/runtime-checkable-proves-less-than-it-looks.md``).
    The conformance test that matters calls every method against cassettes and
    asserts on what comes back.

    One instance memoises one ``espn-api`` ``League`` per ``(league_id,
    season)``. Constructing one costs four ESPN requests — the five-view
    bootstrap, the season player map, the pro schedule, and the draft — so a
    command that reads teams and then standings pays for them once. Pass a
    :class:`~fantasy_sports.cache.store.CacheStore` to make that hold across
    processes as well.
    """

    name = PROVIDER

    #: Position strings ESPN's free-agent filter understands. Anything else is
    #: silently ignored by ESPN — it answers with its *default* player set
    #: rather than an error — so an unknown value is refused here instead.
    POSITIONS: Final[tuple[str, ...]] = (
        "QB",
        "RB",
        "WR",
        "TE",
        "D/ST",
        "K",
        "FLEX",
        "DT",
        "DE",
        "LB",
        "DL",
        "CB",
        "S",
        "DB",
        "DP",
        "HC",
    )

    def __init__(
        self,
        credentials: Mapping[str, Any] | None = None,
        *,
        cache: CacheStore | None = None,
        cache_mode: CacheMode | None = None,
        http: Callable[..., Any] | None = None,
        http_post: Callable[..., Any] | None = None,
        now: Callable[[], datetime] | None = None,
        free_agent_limit: int = 50,
        auth_state_path: Path | None = None,
    ) -> None:
        # Kept unrevealed, only so the write path can report credential age
        # (R12a); `build_auth_status` needs the chain's own record of where each
        # value came from. A plain mapping has no such record.
        self._credential_set = credentials if _is_credential_set(credentials) else None
        self._credentials = _revealed(credentials)
        for value in self._credentials.values():
            # Register with the scrubber so that if `espn-api` interpolates a
            # cookie into an exception message, `FantasySportsError` blanks it
            # at construction. Values arriving through the auth chain are
            # already registered; a caller passing plain strings is not.
            remember_secret(value)
        self._cache = cache
        self._cache_mode = cache_mode
        self._http = http
        # Resolved at call time, never bound as a default: a module-level
        # default captured here would defeat a monkeypatch of `requests.post`
        # (docs/memory/default-args-defeat-monkeypatch.md).
        self._http_post = http_post
        self._auth_state_path = auth_state_path
        # Same rule for the clock: `utc_now` is looked up when no clock is
        # given, so a test or the doc-sample generator that patches the
        # module's `utc_now` reaches a provider `open_read` built.
        self._now = now if now is not None else (lambda: utc_now())
        self._free_agent_limit = free_agent_limit
        self._echoed_members: dict[tuple[str, int], str] = {}
        self._identity_mismatches: dict[tuple[str, int], tuple[str, str]] = {}
        """(league, season) -> (expected, observed) member pseudonyms, once a
        write proved the SWID and espn_s2 are different accounts."""
        self._leagues: dict[tuple[str, int], tuple[Any, _Transport]] = {}
        self._auth_reason: str | None = None
        self.last_fetch: FetchRecord | None = None
        """Every upstream response the most recent read used (origin R1)."""

    # --- auth -------------------------------------------------------------- #

    def credential_specs(self) -> list[CredentialSpec]:
        """ESPN's two manually-extracted cookies.

        ``staleness`` describes how expiry becomes *visible*, not a predicted
        lifetime. No ESPN documentation, library source, or community post
        states a concrete cookie lifetime, so a fabricated one would either cry
        wolf on a live cookie or stay silent past a dead one.
        """
        from fantasy_sports.auth.chain import ESPN_CREDENTIALS

        staleness = (
            "Invisible until a read fails. ESPN answers 401 identically for an "
            "expired cookie, a malformed one, and a league the account was never "
            "in, and publishes no cookie lifetime -- so age is reported and "
            "expiry is never predicted."
        )
        return [replace(spec, staleness=staleness) for spec in ESPN_CREDENTIALS]

    @property
    def has_credentials(self) -> bool:
        """Whether a *complete* cookie pair was configured.

        Both or neither: ``espn-api`` sends cookies only when it has both, so
        one cookie on its own reaches ESPN as an unauthenticated request and
        must be reported as missing rather than as rejected.
        """
        return bool(self._credentials.get("espn_s2") and self._credentials.get("swid"))

    @property
    def missing_credentials(self) -> tuple[str, ...]:
        """Which of the two cookies is absent. Names only, never values."""
        return tuple(name for name in ("espn_s2", "swid") if not self._credentials.get(name))

    def _note_auth_reason(self, reason: str | None) -> None:
        """Record ESPN's typed 401 reason for the classifier. Never a value."""
        self._auth_reason = reason

    # --- reads ------------------------------------------------------------- #

    def fetch_league(self, league_id: str, season: int) -> League:
        """The league itself, including its roster-slot configuration (R3a)."""
        with self._read(league_id, season, "fetch_league") as league:
            data = self._bootstrap_payload(league_id, season)
            return League(
                provider=PROVIDER,
                provider_id=str(league_id),
                name=league.settings.name,
                season=int(season),
                sport="nfl",
                team_count=len(league.teams),
                current_week=int(league.current_week),
                roster_slots=_roster_slots(data),
                raw=_league_raw(data),
            )

    def fetch_teams(self, league_id: str, season: int) -> list[Team]:
        """Every team in the league, unordered (ESPN returns them by team id)."""
        with self._read(league_id, season, "fetch_teams") as league:
            data = self._bootstrap_payload(league_id, season)
            by_id = _teams_by_id(data)
            return [self._team(team, by_id.get(team.team_id, {})) for team in league.teams]

    def fetch_standings(self, league_id: str, season: int) -> list[Team]:
        """Teams in ESPN's own rank order.

        The tiebreaker source lives **here**, not in ``core/`` (ARCHITECTURE
        §14 item 15). ESPN's API returns no sorted standings; ``espn-api``
        computes the order from ``playoffSeed`` and ``rankCalculatedFinal``,
        which are the values ESPN's own site displays, and it also ships a full
        local tiebreaker cascade in ``standings_weekly``. Copying either into
        ``core/`` as "the" standings algorithm would build logic Yahoo — whose
        ranking *is* server-side — would then disagree with.

        ``standing`` on each returned team is this adapter's own 1-based
        position in that order, not ESPN's raw seed field, so it is meaningful
        even in a league where the seed is unset.
        """
        with self._read(league_id, season, "fetch_standings") as league:
            data = self._bootstrap_payload(league_id, season)
            by_id = _teams_by_id(data)
            ordered = league.standings()
            return [
                self._team(team, by_id.get(team.team_id, {}), standing=rank)
                for rank, team in enumerate(ordered, start=1)
            ]

    def fetch_roster(
        self, league_id: str, season: int, team_id: str, week: int | None = None
    ) -> list[RosterSlot]:
        """A team's roster. ``week=None`` is the current roster (R3).

        Every slot carries its lineup slot, the player's eligible slots, the
        kickoff and opponent of that player's game, and whether the slot is
        still changeable. Kickoff and opponent are both read from the raw
        ``proTeamSchedules_wl`` payload — never from ``espn-api``'s
        ``Player.schedule``, whose datetimes are naive and host-local, and
        never from its ``pro_opponent``, which is gated on positional ratings
        ESPN may not have published (#72, #86).
        """
        with self._read(league_id, season, "fetch_roster") as league:
            wanted = _as_int(team_id)
            if wanted is None:
                raise LeagueNotFoundError(
                    f"ESPN team ids are numeric; got {team_id!r}.",
                    remediation="Run `fantasy-sports teams` to list the ids in this league.",
                    details={"team_id": str(team_id)},
                )
            scoring_period = _as_int(week)
            if scoring_period is not None:
                league.load_roster_week(scoring_period)
            else:
                scoring_period = _as_int(league.current_week)

            team = next((item for item in league.teams if item.team_id == wanted), None)
            if team is None:
                raise LeagueNotFoundError(
                    f"No team {wanted} in ESPN league {league_id} for {season}.",
                    remediation="Run `fantasy-sports teams` to list the ids in this league.",
                    details={"team_id": str(wanted), "league_id": str(league_id)},
                )

            transport = self._transport(league_id, season)
            entries = _roster_entries(transport, wanted, scoring_period)
            kickoffs = _kickoff_map(transport.responses)
            opponents = _opponent_map(transport.responses)
            now = self._now()
            return [
                _roster_slot(
                    player,
                    entries.get(player.playerId, {}),
                    kickoffs,
                    opponents,
                    scoring_period,
                    now,
                )
                for player in team.roster
            ]

    def fetch_matchups(self, league_id: str, season: int, week: int) -> list[Matchup]:
        """Head-to-head pairings for the scoring period ``week``.

        ``week`` is a **scoring period** — the NFL week, the number a human
        means. ESPN's schedule is indexed by *matchup* period, and the two
        diverge exactly when a playoff round spans two NFL weeks, which is
        league-configurable rather than a calendar fact. ``espn-api``'s
        ``scoreboard(week)`` filters on ``matchupPeriodId`` and would silently
        return the wrong round, so the scoring period is resolved through
        ``settings.matchup_periods`` first. Both identifiers survive onto the
        returned object, and the untouched provider values stay in ``raw``.
        """
        with self._read(league_id, season, "fetch_matchups") as league:
            scoring_period = _as_int(week)
            if scoring_period is None:
                raise LeagueNotFoundError(
                    f"A week must be an integer scoring period; got {week!r}.",
                    details={"week": str(week)},
                )
            matchup_period = _matchup_period_for(league, scoring_period)
            transport = self._transport(league_id, season)
            transport.completed_through = max(_as_int(league.current_week) or 1, 1) - 1
            found = league.scoreboard(matchup_period)
            raw_entries = _schedule_entries(transport.responses, matchup_period)
            return [
                _matchup(
                    item,
                    raw_entries,
                    scoring_period=scoring_period,
                    matchup_period=matchup_period,
                    index=index,
                )
                for index, item in enumerate(found)
            ]

    def fetch_box_scores(self, league_id: str, season: int, week: int) -> list[BoxScore]:
        """One scoring period's matchups with both lineups, player by player.

        ``week`` is a scoring period, resolved to a matchup period the same way
        :meth:`fetch_matchups` does it — ``espn-api``'s ``box_scores()`` filters
        on ``matchupPeriodId``, so passing an NFL week straight through returns
        the wrong playoff round without complaining.

        ESPN has no ``mBoxscore`` view. ``espn-api`` stitches
        ``mMatchupScore`` + ``mScoreboard`` client-side with two side-calls,
        which is why this costs more than :meth:`fetch_matchups` and is a
        separate method rather than a flag.

        Each lineup entry's ``pro_opponent`` is read from the raw
        ``proTeamSchedules_wl`` payload, not from ``espn-api``'s
        ``BoxPlayer.pro_opponent``. The library only names an opponent when
        the player's position appears under ``mPositionalRatings``, and ESPN
        serves that view without its ``positionAgainstOpponent`` key for some
        leagues until the ratings exist -- on opening night 2026 two of three
        leagues lost every opponent that way (jwulff/fantasy-sports#72). The
        schedule is a fact about the NFL week; the ratings are a fact about
        how the matchup is rated, and the first must not wait on the second.

        The library refuses box scores before 2019 outright. That refusal
        surfaces as ``NOT_AVAILABLE`` naming the season, not as an empty
        list: "no box score exists for this season" and "nobody scored" are
        different answers and a consumer cannot tell them apart from ``[]``.
        ``NOT_AVAILABLE`` rather than ``PROVIDER_UNAVAILABLE`` because this is a
        refusal we can positively classify, not one we cannot
        (jwulff/fantasy-sports#45) — ``retryable`` must be ``False``, since ESPN
        will never start serving 2018 box scores.
        """
        with self._read(league_id, season, "fetch_box_scores") as league:
            scoring_period = _as_int(week)
            if scoring_period is None:
                raise LeagueNotFoundError(
                    f"A week must be an integer scoring period; got {week!r}.",
                    details={"week": str(week)},
                )
            matchup_period = _matchup_period_for(league, scoring_period)
            try:
                found = league.box_scores(matchup_period)
            except Exception as exc:  # noqa: BLE001 - library raises bare Exception
                if _is_library_refusal(exc):
                    raise NotAvailableError(
                        f"ESPN does not serve box scores for the {season} season.",
                        remediation=(
                            "Box scores exist from 2019 onward. Use `matchups` for an "
                            "earlier season; it returns team-level scores."
                        ),
                        details={"season": str(season), "week": str(scoring_period)},
                    ) from exc
                raise
            opponents = _opponent_map(self._transport(league_id, season).responses)
            return [
                _box_score(
                    item,
                    scoring_period=scoring_period,
                    matchup_period=matchup_period,
                    index=index,
                    opponents=opponents,
                )
                for index, item in enumerate(found)
            ]

    def fetch_transactions(
        self,
        league_id: str,
        season: int,
        since: datetime | None = None,
        *,
        scoring_period: int | None = None,
    ) -> list[Transaction]:
        """Roster moves from **both** of ESPN's transaction surfaces.

        ``mTransactions2`` and the ``kona_league_communication`` activity feed
        carry non-overlapping vocabularies, and the feed synthesises two rows
        per trade. Reading one surface silently drops whatever routes only
        through the other, so both are read and merged here -- inside the
        adapter, so ``core.Transaction`` never inherits ESPN's asymmetry
        (research §6 item 2).

        The two surfaces are also shaped differently, and each is used for what
        it is good at. ``mTransactions2`` is **scoped to one scoring period**;
        the activity feed is a rolling recent list across periods. So the
        default read is one period plus the feed -- fast, and complete for
        "what just happened". Asking for ``since`` widens the ``mTransactions2``
        half to sweep every scoring period in the season, because ESPN offers no
        way to ask a date range and a sweep is the only honest answer. Completed
        weeks cache forever, so the sweep is paid once.

        The activity feed is best-effort: ``espn-api`` refuses it before 2019
        and ESPN stopped serving it for historical seasons (issue #546, open
        since 2024). A feed that will not load degrades to the
        ``mTransactions2`` result rather than failing the read -- the
        alternative is a transactions command that cannot read a 2018 league at
        all.
        """
        with self._read(league_id, season, "fetch_transactions") as league:
            found: list[Transaction] = []
            seen: set[tuple[Any, ...]] = set()

            for period in _scoring_periods(league, since, scoring_period):
                for item in self._espn_transactions(league, period):
                    normalized = _transaction(item)
                    if normalized is not None and _remember(seen, normalized):
                        found.append(normalized)

            for activity in self._activity(league):
                for normalized in _activity_transactions(activity):
                    if _remember(seen, normalized):
                        found.append(normalized)

            if since is not None:
                found = [
                    item for item in found if item.timestamp is None or item.timestamp >= since
                ]
            found.sort(key=lambda item: (item.timestamp is None, item.timestamp or _EPOCH))
            return found

    def fetch_free_agents(
        self, league_id: str, season: int, week: int, position: str | None = None
    ) -> list[FreeAgent]:
        """Unrostered players, optionally filtered to one position.

        ESPN scopes this by the ``x-fantasy-filter`` header rather than the URL,
        and an unrecognised position is not an error there — ESPN answers with
        its own default player set, which looks like free agents and is not
        filtered the way you asked. An unknown position is therefore refused
        here rather than sent.

        The library refuses free agents before 2019 outright, the same way it
        refuses box scores. That refusal surfaces as ``NOT_AVAILABLE`` naming
        the season rather than ``PROVIDER_UNAVAILABLE``'s bounded-retry
        instruction — this is a refusal we can positively classify, not one we
        cannot, and ESPN will never start serving free agents for a 2018 league
        (jwulff/fantasy-sports#45).
        """
        slot = None if position is None else _position_key(position)
        if position is not None and slot is None:
            raise ValueError(
                f"{position!r} is not an ESPN position. Valid values: {', '.join(self.POSITIONS)}."
            )
        with self._read(league_id, season, "fetch_free_agents") as league:
            scoring_period = _as_int(week) or _as_int(league.current_week)
            try:
                players = league.free_agents(
                    week=scoring_period, size=self._free_agent_limit, position=slot
                )
            except Exception as exc:  # noqa: BLE001 - library raises bare Exception
                if _is_library_refusal(exc):
                    raise NotAvailableError(
                        f"ESPN does not serve free agents for the {season} season.",
                        remediation=(
                            "Free agents exist from 2019 onward. Use `transactions` for an "
                            "earlier season; it shows roster moves that already happened."
                        ),
                        details={"season": str(season), "week": str(scoring_period)},
                    ) from exc
                raise
            transport = self._transport(league_id, season)
            entries = _free_agent_entries(transport.responses)
            kickoffs = _kickoff_map(transport.responses)
            opponents = _opponent_map(transport.responses)
            now = self._now()
            return [
                _free_agent(
                    player,
                    entries.get(player.playerId, {}),
                    kickoffs,
                    opponents,
                    scoring_period,
                    now,
                )
                for player in players
            ]

    # --- the escape hatch -------------------------------------------------- #

    def fetch_raw(self, league_id: str, season: int, **provider_params: Any) -> dict:
        """Direct passthrough to ESPN's own API (ADR-0002).

        Takes ``view=`` — a string or a list of them — plus any other query
        parameter ESPN accepts, and an optional ``x_fantasy_filter`` mapping
        that becomes the header several views need to return anything useful.

        Does **not** bootstrap the league: a passthrough that costs four
        requests before answering is not a passthrough.
        """
        view = provider_params.pop("view", None)
        if not view:
            raise ValueError("fetch_raw needs a `view=` — ESPN has no default view.")
        fantasy_filter = provider_params.pop("x_fantasy_filter", None)
        headers = None
        if fantasy_filter is not None:
            headers = {
                "x-fantasy-filter": (
                    fantasy_filter
                    if isinstance(fantasy_filter, str)
                    else json.dumps(fantasy_filter)
                )
            }

        params: dict[str, Any] = {"view": view}
        params.update({key: value for key, value in provider_params.items() if value is not None})

        with self._raw_league(league_id, season) as (request, transport):
            with _mapped(view=_view_of(params), operation="fetch_raw", provider=self):
                payload = request.league_get(params=params, headers=headers)
            self.last_fetch = FetchRecord(responses=dict(transport.responses))
            return payload if isinstance(payload, dict) else {"data": payload}

    # --- writes (jwulff/fantasy-sports#107) -------------------------------- #

    def session_role(self, league_id: str, season: int) -> SessionRole:
        """Who these credentials act as in this league, from ``view=mNav``.

        ``mNav``, not ``mLeagueManager``: the founding commissioner carries
        ``isLeagueCreator`` and **not** ``isLeagueManager``, and
        ``mLeagueManager`` shows only the second
        (``docs/memory/commissioner-authority-is-two-flags-in-mnav.md``).

        The session's member is identified by the SWID credential, compared
        case-insensitively as a GUID *and* as this store's pseudonym of it --
        a command's reads go through the cache, which pseudonymises every SWID
        in a body before the adapter parses it, so ``members[].id`` and
        ``teams[].owners`` are pseudonyms there and raw GUIDs only on a
        store-less path. ``member_id`` is reported as the pseudonym either way,
        so the SWID never reaches output.

        Without a SWID, the member id is the one a write response in this
        process echoed, if any; otherwise it is ``None``, ownership is empty,
        and the guard refuses every write. In practice that means a SWID is
        required to write at all: refuse-closed has no first write to learn
        from.
        """
        with self._read(league_id, season, "session_role") as league:
            nav = league.espn_request.league_get(params={"view": "mNav"})
            bootstrap = self._bootstrap_payload(league_id, season)
            return self._role_from(nav, bootstrap, key=(str(league_id), int(season)))

    def set_lineup(
        self, league_id: str, season: int, team_id: str, target: Mapping[str, str]
    ) -> WriteResult:
        """One ``ROSTER`` transaction moving players to the slots ``target`` names.

        ``target`` maps player id to slot *name* (``"BE"``, ``"RB/WR/TE"``).
        The current roster is read, only the players whose slot changes become
        ``LINEUP`` items, and they go in **one** POST, which ESPN validates and
        applies atomically (brief §3) -- per-slot requests would create the
        half-applied lineup a single transaction cannot.

        Refused before any request, as ``CONFIG_INVALID`` (``kind:
        "argument"``): a slot name ESPN has no id for, a player not on this
        roster, a slot not in the player's ``eligibleSlots``, and an empty diff
        (there is no no-op transaction; ESPN rejects a from==to item as
        ``TRAN_ROSTER_SAME_SLOT`` and takes the whole transaction with it).
        Lock state and slot counts are left to the ``lineup set`` preflight
        (#15) and to ESPN: lock state read from a cached roster can be stale,
        and ESPN's own answer to both is typed.
        """
        write = self._begin_write(league_id, season, team_id)
        items = self._lineup_items(write, target)
        return self._post(write, {"type": "ROSTER", "executionType": "EXECUTE", "items": items})

    def move_players(
        self,
        league_id: str,
        season: int,
        team_id: str,
        *,
        adds: Sequence[RosterMove],
        drops: Sequence[RosterMove],
        bid: int | None = None,
        waiver: bool = False,
    ) -> WriteResult:
        """An add, an add/drop, a standalone drop, or a waiver claim.

        The envelope follows from the arguments, as captured in a test league
        (``docs/research/06-espn-add-drop-surface.md``):

        * ``waiver=True`` or a ``bid`` -> ``WAIVER``, which returns ``200``
          with ``status: "PENDING"`` and changes nothing until waivers
          process. ``bidAmount`` is sent **only** when ``bid`` is given: a
          league without an acquisition budget rejects every bid, even ``1``,
          as ``TRAN_ACQUISITION_BUDGET_NOT_SUPPORTED``, so a claim there is
          ``waiver=True`` with no bid.
        * an add (with or without drops) -> ``FREEAGENT``.
        * drops only -> ``ROSTER``. A ``FREEAGENT`` envelope must carry
          exactly one ``ADD`` (``TRAN_INVALID_ITEM_TYPE_COUNT``).

        ``ADD`` items carry ``toTeamId`` and ``DROP`` items ``fromTeamId``;
        each is required and its absence is a typed ``409``
        (``TRAN_ITEM_TO_TEAM_ID_MISSING`` / ``TRAN_ITEM_FROM_TEAM_ID_MISSING``).

        Refused before any request, as ``CONFIG_INVALID`` (``kind:
        "argument"``): no players at all, more than one add (ESPN refuses it
        on ``FREEAGENT``; on ``WAIVER`` it is unverified and refused the same
        way), a claim with nothing to add, and a negative or non-integer bid.
        """
        if not adds and not drops:
            raise ConfigInvalidError(
                "An add/drop names no players.",
                kind="argument",
                remediation="Name at least one player to add or drop.",
            )
        if len(adds) > 1:
            raise ConfigInvalidError(
                f"One transaction can add one player; {len(adds)} were named.",
                kind="argument",
                remediation="Send one add (with its drops) per transaction.",
            )
        claim = bool(waiver) or bid is not None
        if claim and not adds:
            raise ConfigInvalidError(
                "A waiver claim must name the player it claims.",
                kind="argument",
                remediation="Name one player to add; drops alone are not a claim.",
            )
        if bid is not None and (_as_int(bid) is None or int(bid) < 0):
            raise ConfigInvalidError(
                f"A waiver bid must be a non-negative integer; got {bid!r}.",
                kind="argument",
                remediation="Pass a whole-dollar bid of 0 or more.",
            )
        write = self._begin_write(league_id, season, team_id)
        items = [_move_item(move, "ADD", "toTeamId", write.team_id) for move in adds]
        items += [_move_item(move, "DROP", "fromTeamId", write.team_id) for move in drops]
        kind = "WAIVER" if claim else "FREEAGENT" if adds else "ROSTER"
        body: dict[str, Any] = {"type": kind, "executionType": "EXECUTE", "items": items}
        if bid is not None:
            body["bidAmount"] = int(bid)
        return self._post(write, body)

    def cancel_transaction(
        self, league_id: str, season: int, team_id: str, transaction_id: str
    ) -> WriteResult:
        """Withdraw a pending claim: ``type: "WAIVER"``, ``executionType:
        "CANCEL"``, ``relatedTransactionId``, and no items.

        Captured in a test league (``06-espn-add-drop-surface.md``): ESPN
        answers ``200`` with a **new** transaction id, ``status: "CANCELED"``,
        the claim's items echoed, and ``relatedTransactionId`` naming the
        claim. The claim leaves ``view=mPendingTransactions`` at once, but its
        own ``mTransactions2`` row keeps reading ``PENDING``; the cancel row
        pointing at it is what says it was withdrawn.
        """
        related = str(transaction_id or "").strip()
        if not related:
            raise ConfigInvalidError(
                "A cancel needs the transaction id it withdraws.",
                kind="argument",
                remediation="Pass the provider_transaction_id the claim returned.",
            )
        write = self._begin_write(league_id, season, team_id)
        return self._post(
            write,
            {"type": "WAIVER", "executionType": "CANCEL", "relatedTransactionId": related},
        )

    def credential_report(self) -> dict[str, Any]:
        """``{"espn_s2_age_seconds", "warning"}`` -- the R12a block every write
        result carries. Public so a ``--dry-run`` can show it without writing."""
        return _credentials_block(
            self._credential_set, state_path=self._auth_state_path, now=self._now()
        )

    # --- write plumbing ---------------------------------------------------- #

    def _begin_write(self, league_id: str, season: int, team_id: str) -> _Write:
        """Everything a write needs, in the order that keeps it safe.

        1. ``espn_s2`` present, or ``AUTH_MISSING`` -- nothing to send.
        2. The credential-age block, computed now so it never depends on the
           request having been sent.
        3. The ownership guard, before any body exists.
        4. The league's current scoring period, the only one ESPN accepts for
           a write (brief §4); never a parameter.
        """
        if not self._credentials.get("espn_s2"):
            raise AuthMissingError(
                "Writing to ESPN needs the espn_s2 cookie, and none is configured.",
                remediation=(
                    "Run `fantasy-sports auth login`, or set FANTASY_SPORTS_ESPN_S2 in the "
                    "environment."
                ),
                details={"missing": ["espn_s2"]},
            )
        mismatch = self._identity_mismatches.get((str(league_id), int(season)))
        if mismatch is not None:
            raise WriteRejectedError(
                "Refusing to write: an earlier write in this session showed that the "
                "configured SWID and espn_s2 belong to different ESPN accounts, so team "
                "ownership cannot be trusted.",
                kind="not_owned",
                team_id=str(team_id).strip(),
                remediation=(
                    "The SWID and espn_s2 appear to belong to different accounts. Re-run "
                    "`fantasy-sports auth login` and store both cookies from the same "
                    "logged-in browser session."
                ),
                details={"expected_member": mismatch[0], "observed_member": mismatch[1]},
            )
        credentials = self.credential_report()
        role = self.session_role(league_id, season)
        team = _guard(role, team_id)
        period = _current_period(self._bootstrap_payload(league_id, season))
        return _Write(
            league_id=str(league_id),
            season=int(season),
            team_id=team,
            scoring_period=period,
            credentials=credentials,
            member_id=role.member_id,
        )

    def _lineup_items(self, write: _Write, target: Mapping[str, str]) -> list[dict[str, Any]]:
        """The diff between ``target`` and the **current** roster.

        The roster is fetched here, explicitly and bypassing the cache, for the
        period the write will be sent for. Reusing whatever ``mRoster`` this instance last recorded
        would diff against a historical week after a ``fetch_roster(week=N)``
        on the same provider (PR #118 review), and every item would carry the
        wrong ``fromLineupSlotId``.
        """
        team_id = write.team_id
        if not isinstance(target, Mapping) or not target:
            raise ConfigInvalidError(
                "A lineup target names no players.",
                kind="argument",
                remediation="Map at least one player id to the slot it should occupy.",
            )
        from fantasy_sports.cache.store import CacheMode

        with _mapped(view="mRoster", operation="set_lineup", provider=self):
            league = self._league(write.league_id, write.season)
            # Live, whatever the provider's cache mode: a roster up to five
            # minutes old is exactly the stale `fromLineupSlotId` this read
            # exists to avoid. BYPASS neither reads nor writes the store, and
            # the store still scrubs the body.
            with self._transport(write.league_id, write.season).mode_for_call(CacheMode.BYPASS):
                current = league.espn_request.league_get(
                    params={"view": "mRoster", "scoringPeriodId": write.scoring_period}
                )
        entries = _team_entries(current, team_id) or {}
        items: list[dict[str, Any]] = []
        for player, slot in target.items():
            player_id = _as_int(player)
            entry = entries.get(player_id) if player_id is not None else None
            if entry is None:
                raise ConfigInvalidError(
                    f"Player {player!r} is not on team {team_id}'s roster.",
                    kind="argument",
                    remediation="Run `fantasy-sports roster` for this team's player ids.",
                    details={"player_id": str(player)},
                )
            to_slot = _slot_id(slot)
            if to_slot is None:
                raise ConfigInvalidError(
                    f"{slot!r} is not an ESPN lineup slot.",
                    kind="argument",
                    remediation="Use a slot name from `fantasy-sports league` roster_slots.",
                    details={"player_id": str(player), "slot": str(slot)},
                )
            eligible = _eligible_slot_ids(entry)
            if eligible is not None and to_slot not in eligible:
                raise ConfigInvalidError(
                    f"Player {player_id} is not eligible for the {slot} slot.",
                    kind="argument",
                    remediation="Choose a slot from the player's eligible_slots.",
                    details={"player_id": str(player_id), "slot": slot},
                )
            from_slot = _as_int(entry.get("lineupSlotId"))
            if from_slot is None:
                raise SchemaDriftError(
                    "A roster entry has no lineupSlotId.",
                    path=["mRoster", "entries", "lineupSlotId"],
                    provider=PROVIDER,
                )
            if from_slot != to_slot:
                items.append(
                    {
                        "playerId": player_id,
                        "type": "LINEUP",
                        "fromLineupSlotId": from_slot,
                        "toLineupSlotId": to_slot,
                    }
                )
        if not items:
            raise ConfigInvalidError(
                "Every player named is already in that slot; there is nothing to send.",
                kind="argument",
                remediation=(
                    "ESPN has no no-op transaction. Name at least one player whose slot changes."
                ),
            )
        # Vacates before fills, stable otherwise (brief §3).
        items.sort(key=lambda item: item["toLineupSlotId"] not in _BENCH_SLOT_IDS)
        return items

    def _role_from(
        self, nav: Any, bootstrap: Mapping[str, Any], *, key: tuple[str, int]
    ) -> SessionRole:
        if not isinstance(nav, Mapping) or not isinstance(nav.get("members"), list):
            raise SchemaDriftError(
                "ESPN's mNav response has no members list.",
                path=["mNav", "members"],
                provider=PROVIDER,
            )
        salt = self._swid_salt()
        swid = self._credentials.get("swid")
        echoed = self._echoed_members.get(key)
        if swid:
            member_id: str | None = swid_pseudonym(swid, salt=salt)
            candidates = {_guid_key(swid), _guid_key(member_id)}
        elif echoed:
            member_id = echoed
            candidates = {_guid_key(echoed)}
        else:
            member_id = None
            candidates = set()
        candidates.discard(None)

        member = next(
            (
                row
                for row in nav["members"]
                if isinstance(row, Mapping) and _guid_key(row.get("id")) in candidates
            ),
            None,
        )
        if member is not None and member.get("isLeagueCreator") is True:
            role = "creator"
        elif member is not None and member.get("isLeagueManager") is True:
            role = "manager"
        else:
            role = "member"

        teams = nav.get("teams")
        if not isinstance(teams, list):
            teams = bootstrap.get("teams") if isinstance(bootstrap, Mapping) else None
        owned: list[str] = []
        for team in teams if isinstance(teams, list) else []:
            if not isinstance(team, Mapping) or _as_int(team.get("id")) is None:
                continue
            owners = team.get("owners")
            if isinstance(owners, list) and any(_guid_key(o) in candidates for o in owners):
                owned.append(str(_as_int(team["id"])))

        raw = (
            {}
            if member is None
            else json.loads(scrub_body(json.dumps(dict(member)), swid_salt=salt))
        )
        return SessionRole(
            provider=PROVIDER,
            member_id=member_id,
            role=role,  # type: ignore[arg-type]  # one of the three literals above
            owned_team_ids=tuple(sorted(set(owned), key=int)),
            raw=raw,
        )

    def _swid_salt(self) -> str:
        """The salt this provider's reads were pseudonymised under.

        The store's own when there is one -- that is what the cache scrubbed
        the bodies with -- and the public cassette salt otherwise, so a
        store-less provider still never reports a raw SWID.
        """
        return self._cache.swid_salt if self._cache is not None else CASSETTE_SWID_SALT

    def _post(self, write: _Write, envelope: Mapping[str, Any]) -> WriteResult:
        """Send one transaction, once, and map whatever comes back.

        The body is assembled here so that the fields every write shares --
        ``isLeagueManager: false``, ``teamId``, ``scoringPeriodId`` -- cannot
        be got wrong by a caller. ``isLeagueManager`` is never ``true``
        (brief §7), and no ``memberId`` is sent: ESPN fills it from the
        session (brief §2.2), and sending one would put the SWID in a body.

        Only ``espn_s2`` goes on the wire. The SWID is not needed for a write
        (brief §6.3) and is not sent, which removes it from every leak channel
        on the mutation path.
        """
        body: dict[str, Any] = {
            "isLeagueManager": False,
            "teamId": write.team_id,
            "type": envelope["type"],
            "scoringPeriodId": write.scoring_period,
            "executionType": envelope["executionType"],
        }
        for name in ("items", "bidAmount", "relatedTransactionId"):
            if name in envelope:
                body[name] = envelope[name]

        url = WRITE_URL.format(season=write.season, league_id=write.league_id)
        cookies = {"espn_s2": self._credentials["espn_s2"]}
        post = self._http_post
        if post is None:
            import requests

            post = requests.post
        try:
            # Exactly one attempt. There is no retry loop anywhere on this
            # path: a write whose outcome is unknown must be read back, not
            # re-sent (brief §8 item 1).
            response = post(
                url,
                json=body,
                headers=dict(_WRITE_HEADERS),
                cookies=cookies,
                timeout=WRITE_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - every transport failure is "unknown"
            # A timeout, a reset, a DNS failure mid-retry inside `requests` --
            # none of them says whether ESPN received and applied the body.
            # Classifying any of them as "not applied" is the double-spend.
            raise ProviderUnavailableError(
                f"The write to ESPN did not complete ({type(exc).__name__}: {_clean(exc)}).",
                remediation=_UNKNOWN_STATE_REMEDIATION,
                details={
                    "transaction_state": "unknown",
                    "cause": type(exc).__name__,
                    "team_id": str(write.team_id),
                },
            ) from exc

        fetched_at = self._now()
        payload = _write_payload(response, swid_salt=self._swid_salt())
        status = _as_int(getattr(response, "status_code", None))
        if status == 200:
            # Purge before parsing: the write applied whether or not the body
            # turns out to be readable, and a stale roster served after a
            # successful write is the incoherence R10 exists to prevent.
            self._after_success(write)
        result = _write_outcome(
            status,
            payload,
            headers=getattr(response, "headers", None) or {},
            cookies_sent=bool(cookies.get("espn_s2")),
            write=write,
        )
        self.last_fetch = FetchRecord(
            responses={
                **(self.last_fetch.responses if self.last_fetch is not None else {}),
                "transactions": RawResponse(
                    view="transactions", payload=payload, fetched_at=fetched_at, cached=False
                ),
            }
        )
        self._check_identity(write, result)
        if result.member_id:
            self._echoed_members[(write.league_id, write.season)] = result.member_id
        return result

    def _check_identity(self, write: _Write, result: WriteResult) -> None:
        """Prove, after the fact, that the session that wrote is the one the
        guard checked ownership for.

        Ownership is derived from the SWID, but ESPN authenticates the write
        with ``espn_s2``, and the credential chain resolves the two
        independently -- so a hand-configured pair from two accounts would
        pass the guard for the SWID's teams while writing as someone else.
        No pre-write identity endpoint is trusted, so the check is the
        ``memberId`` ESPN echoes on the write itself. A mismatch is recorded
        for this league, refusing every later write in the process, and the
        write that just happened is reported as ``WRITE_DIVERGED`` with
        everything a caller needs to journal it.
        """
        expected, observed = write.member_id, result.member_id
        if not expected or not observed or _guid_key(expected) == _guid_key(observed):
            return
        key = (write.league_id, write.season)
        self._identity_mismatches[key] = (expected, observed)
        raise WriteDivergedError(
            "ESPN applied this write as a different member than the configured SWID: "
            "the SWID and espn_s2 appear to belong to different ESPN accounts.",
            expected={"member_id": expected},
            observed={"member_id": observed},
            remediation=(
                "The write may have changed a team this session was not verified to own. "
                "Read `transactions` for this league, then re-run `fantasy-sports auth "
                "login` and store both cookies from the same logged-in browser session. "
                "Further writes are refused until then."
            ),
            details={
                "expected_member": expected,
                "observed_member": observed,
                "provider_transaction_id": result.provider_transaction_id,
                "team_id": result.team_id,
                "status": result.status,
                "is_pending": result.is_pending,
                "proposed_at": None
                if result.proposed_at is None
                else result.proposed_at.isoformat(),
                "items": [dict(item) for item in result.items],
                "transaction_state": "applied",
            },
        )

    def _after_success(self, write: _Write) -> None:
        """Make the cache and the memoised league forget what the write changed.

        Everything tagged with this league and this scoring period (R10) --
        rosters, matchups, standings, transactions all moved within 1.5 s of
        each probed write (brief §9). Season-scoped entries (the player map,
        the NFL schedule) carry no league tag and are untouched; the store
        refuses to league-purge them by construction.
        """
        from fantasy_sports.cache.tags import league_tag, scoring_period_tag

        if self._cache is not None:
            self._cache.purge_by_league_tag(league_tag(PROVIDER, write.league_id, write.season))
            self._cache.purge_by_league_tag(
                scoring_period_tag(PROVIDER, write.league_id, write.season, write.scoring_period)
            )
        # The next read re-bootstraps rather than reusing rosters this write
        # just made stale.
        self._leagues.pop((write.league_id, write.season), None)

    # --- plumbing ---------------------------------------------------------- #

    @contextmanager
    def _read(self, league_id: str, season: int, operation: str) -> Iterator[Any]:
        """Build (or reuse) the league, map every failure, and record sources."""
        with _mapped(view=operation, operation=operation, provider=self):
            league = self._league(league_id, season)
            yield league
        self.last_fetch = FetchRecord(responses=dict(self._transport(league_id, season).responses))

    def _league(self, league_id: str, season: int) -> Any:
        key = (str(league_id), int(season))
        found = self._leagues.get(key)
        if found is not None:
            return found[0]

        from espn_api.football import League as EspnLeague

        numeric = _as_int(league_id)
        if numeric is None:
            raise LeagueNotFoundError(
                f"ESPN league ids are numeric; got {league_id!r}.",
                remediation="Copy the leagueId from the URL of your ESPN league page.",
                details={"league_id": str(league_id)},
            )

        league = EspnLeague(
            league_id=numeric,
            year=int(season),
            espn_s2=self._credentials.get("espn_s2"),
            swid=self._credentials.get("swid"),
            fetch_league=False,
        )
        transport = self._install(league, league_id=str(league_id), season=int(season))
        self._leagues[key] = (league, transport)
        try:
            league.fetch_league()
        except BaseException:
            # A half-built league must not be memoised: the next call would
            # reuse an object whose `teams` never loaded and fail as a
            # `SCHEMA_DRIFT` three layers from the real cause.
            self._leagues.pop(key, None)
            raise
        transport.completed_through = max(_as_int(league.current_week) or 1, 1) - 1
        return league

    @contextmanager
    def _raw_league(self, league_id: str, season: int) -> Iterator[tuple[Any, _Transport]]:
        """An ``EspnFantasyRequests`` with our transport, and no bootstrap."""
        from espn_api.requests.espn_requests import EspnFantasyRequests

        numeric = _as_int(league_id)
        if numeric is None:
            raise LeagueNotFoundError(
                f"ESPN league ids are numeric; got {league_id!r}.",
                details={"league_id": str(league_id)},
            )
        cookies = None
        if self.has_credentials:
            cookies = {
                "espn_s2": self._credentials["espn_s2"],
                "SWID": self._credentials["swid"],
            }
        request = EspnFantasyRequests(
            sport="nfl", year=int(season), league_id=numeric, cookies=cookies
        )
        transport = _Transport(
            request,
            season=int(season),
            league_id=str(league_id),
            store=self._cache,
            mode=self._cache_mode,
            http=self._http,
            now=self._now,
            on_auth_reason=self._note_auth_reason,
        )
        yield request, transport

    def _install(self, league: Any, *, league_id: str, season: int) -> _Transport:
        return _Transport(
            league.espn_request,
            season=season,
            league_id=league_id,
            store=self._cache,
            mode=self._cache_mode,
            http=self._http,
            now=self._now,
            on_auth_reason=self._note_auth_reason,
        )

    def _transport(self, league_id: str, season: int) -> _Transport:
        return self._leagues[(str(league_id), int(season))][1]

    def _bootstrap_payload(self, league_id: str, season: int) -> Mapping[str, Any]:
        responses = self._transport(league_id, season).responses
        for response in responses.values():
            if "mTeam" in response.view.split("+") and isinstance(response.payload, Mapping):
                return response.payload
        return {}

    def _team(self, team: Any, raw: Mapping[str, Any], *, standing: int | None = None) -> Team:
        return Team(
            provider=PROVIDER,
            provider_id=str(team.team_id),
            name=team.team_name,
            owner_names=_owner_names(team),
            wins=int(team.wins),
            losses=int(team.losses),
            ties=int(team.ties),
            points_for=float(team.points_for),
            points_against=float(team.points_against),
            standing=standing if standing is not None else _as_int(team.standing),
            raw=_team_raw(raw),
        )

    def _espn_transactions(self, league: Any, scoring_period: int | None) -> list[Any]:
        """``mTransactions2``, treating "none this period" as an empty result.

        ``espn-api`` raises a bare ``Exception('No transactions found')`` when
        the key is absent, which is what a quiet scoring period looks like. It
        is a successful empty result, not a failure, and reporting it as
        ``PROVIDER_UNAVAILABLE`` would tell an agent to retry an answer that is
        already correct.
        """
        try:
            return list(
                league.transactions(
                    scoring_period=scoring_period, types=set(_ALL_TRANSACTION_TYPES)
                )
            )
        except FantasySportsError:
            raise
        except Exception as exc:  # noqa: BLE001 - see docstring; classified below
            if "no transactions found" in str(exc).lower():
                return []
            raise

    def _activity(self, league: Any) -> list[Any]:
        """The activity feed, best-effort. See :meth:`fetch_transactions`."""
        try:
            return list(league.recent_activity())
        except FantasySportsError:
            raise
        except Exception:  # noqa: BLE001 - a missing second surface is not a failure
            return []


_EPOCH: Final[datetime] = from_epoch_millis(0)


def _revealed(credentials: Mapping[str, Any] | None) -> dict[str, str]:
    """Credential values as plain strings, accepting a ``CredentialSet``.

    ``reveal()`` is called at exactly one place — here, on the way to the
    transport — which is the discipline ``auth.chain.Secret`` exists to enforce.
    """
    if credentials is None:
        return {}
    if hasattr(credentials, "as_mapping"):
        return dict(credentials.as_mapping())
    revealed: dict[str, str] = {}
    for name, value in credentials.items():
        if value is None:
            continue
        revealed[name] = value.reveal() if hasattr(value, "reveal") else str(value)
    return revealed


# --------------------------------------------------------------------------- #
# Failure mapping
# --------------------------------------------------------------------------- #

_LIBRARY_REFUSALS: Final[tuple[str, ...]] = (
    "cant use box score before",
    "cant use free agents before",
    "cant use recent activity before",
)
"""The three refusals ``espn-api`` raises as a bare ``Exception``, on the year,
before any HTTP request is made — see
``docs/memory/espn-api-is-a-shape-reader-not-a-client.md``. Matched against this
known set rather than a loose substring, so an unrelated library message is not
misclassified as a permanent refusal."""


def _is_library_refusal(exc: BaseException) -> bool:
    """True for a known pre-2019 refusal.

    ``espn-api`` keeps no structured attribute for these — the exception is the
    base ``Exception`` class with a sentence in it — so message-text matching
    is what the adapter has to work with. This narrows the catch-all in
    :func:`_mapped`: a positively classifiable refusal (jwulff/fantasy-sports#45)
    is caught by the caller *before* it reaches the catch-all, so it never
    inherits ``PROVIDER_UNAVAILABLE``'s ``retryable: true``.
    """
    text = str(exc).lower()
    return any(needle in text for needle in _LIBRARY_REFUSALS)


@contextmanager
def _mapped(*, view: str, operation: str, provider: EspnProvider) -> Iterator[None]:
    """Turn anything ``espn-api`` raises into a taxonomy error.

    The order of the ``except`` clauses is the whole design:

    * our own errors pass through untouched — they were classified by whoever
      had the evidence;
    * ``KeyError``/``TypeError``/``IndexError``/``AttributeError`` are shape
      problems from inside a constructor doing unguarded dict access, and
      become ``SCHEMA_DRIFT`` with the offending path;
    * the library's three HTTP exceptions map per ARCHITECTURE §5, with the 401
      classified from ESPN's typed reason where there is one;
    * **everything else** becomes ``PROVIDER_UNAVAILABLE``. ``espn-api`` raises
      bare ``Exception`` for ordinary conditions, so this catch-all is not
      defensive padding — it is the common path for anything the library did
      not think to type.
    """
    from espn_api.requests.espn_requests import (
        ESPNAccessDenied,
        ESPNInvalidLeague,
        ESPNUnknownError,
    )

    try:
        yield
    except FantasySportsError:
        raise
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise _drift(exc, view=view, operation=operation) from exc
    except ESPNAccessDenied as exc:
        raise _access_denied(exc, view=view, provider=provider) from exc
    except ESPNInvalidLeague as exc:
        raise _not_found(exc, view=view, provider=provider) from exc
    except ESPNUnknownError as exc:
        raise _unknown_status(exc, view=view) from exc
    except Exception as exc:  # noqa: BLE001 - the honest landing place (R12)
        raise ProviderUnavailableError(
            f"ESPN request failed during {operation}: {_clean(exc)}",
            details={"view": view, "cause": type(exc).__name__},
        ) from exc


def _drift(exc: BaseException, *, view: str, operation: str) -> SchemaDriftError:
    """Build the ``SCHEMA_DRIFT`` error, with enough context to file an issue."""
    frame = _espn_frame(exc)
    key = _missing_key(exc)
    path = [part for part in (view, frame, key) if part]
    what = f"missing key {key!r}" if key else f"{type(exc).__name__}: {_clean(exc)}"
    return SchemaDriftError(
        f"ESPN's {view} response no longer has the shape espn-api reads "
        f"({what}" + (f", in {frame}" if frame else "") + ").",
        path=path,
        provider=PROVIDER,
        remediation=(
            "File an issue with this error payload; the provider's response shape changed."
        ),
        details={"view": view, "operation": operation, "cause": type(exc).__name__},
    )


def _access_denied(exc: BaseException, *, view: str, provider: EspnProvider) -> FantasySportsError:
    """Classify a 401 that survived ``espn-api``'s alternate-shape retry.

    The retry matters and is why this is not a status-code mapping: by the time
    this runs the library has already tried the other current-vs-historical URL
    shape and failed on both, so the season-boundary ambiguity ARCHITECTURE §14
    item 1 warns about is genuinely resolved. **That probe is mandatory, not a
    fallback** -- see :data:`AUTH_REASONS` for the measurement that settles it.

    What is left is a question this response cannot answer. Probed against a
    real private league on 2026-09-05, one variable at a time, the 401 body is
    byte-identical for no cookies, a valid ``SWID`` with a bad ``espn_s2``, and
    either cookie alone. So:

    * nothing (or half of the pair) was configured -> ``AUTH_MISSING``, which is
      a fact about us and needs no evidence from ESPN;
    * a reason type that positively proves the credential failed ->
      ``AUTH_EXPIRED``. None has ever been observed; the branch exists so that
      adding one to :data:`AUTH_REASONS` is the whole change;
    * otherwise -> ``LEAGUE_NOT_FOUND``, whose ``agent_action`` -- confirm the
      league id and this account's access -- is correct for *all* of the
      remaining possibilities. Reporting ``AUTH_EXPIRED`` here would send a
      user to re-extract cookies that may have been fine, which is exactly the
      failure the double-probe exists to prevent.
    """
    reason = provider._auth_reason
    details: dict[str, Any] = {"view": view, "status": 401}
    if reason:
        details["espn_reason"] = reason

    if not provider.has_credentials:
        missing = provider.missing_credentials
        details["missing"] = list(missing)
        return AuthMissingError(
            "ESPN refused this league, and "
            + (
                "no credentials were configured"
                if len(missing) == 2
                else f"only half the cookie pair was configured (missing {missing[0]})"
            )
            + ". ESPN sends cookies only when it has both, so this request "
            "reached ESPN unauthenticated. Private leagues and pre-2018 history "
            "both require them.",
            remediation=(
                "Run `fantasy-sports auth login`, or set FANTASY_SPORTS_ESPN_S2 "
                "and FANTASY_SPORTS_SWID in the environment."
            ),
            details=details,
        )

    known = AUTH_REASONS.get(reason or "")
    if known is not None and known.proves_credentials_bad:
        return AuthExpiredError(
            f"ESPN rejected the configured credentials. {known.explanation}",
            remediation=(
                "Re-extract espn_s2 and SWID from a logged-in browser session and "
                "run `fantasy-sports auth login`."
            ),
            details=details,
        )

    explanation = (
        known.explanation
        if known is not None
        else (
            f"ESPN gave the reason {reason!r}, which this tool has not seen before."
            if reason
            else "ESPN's 401 carried no machine-readable reason."
        )
    )
    details["reason"] = "credentials_or_membership"
    return LeagueNotFoundError(
        "ESPN refused this league for the configured credentials. "
        f"{explanation} That leaves two possibilities this response cannot tell "
        "apart: this account is not a member of the league, or the cookies no "
        "longer work. This tool will not guess between them.",
        remediation=(
            "Confirm the league id, and that the ESPN account whose cookies are "
            "configured is a member of it. If it is, re-extract the cookies and "
            "run `fantasy-sports auth login`."
        ),
        details=details,
    )


def _not_found(exc: BaseException, *, view: str, provider: EspnProvider) -> LeagueNotFoundError:
    """Classify a 404, which is not only "you typed the id wrong".

    ESPN moved pre-2018 league history behind authentication some time in 2025
    and the gating surfaces as a **404**, not a 401. Reporting a bare
    not-found there sends the user off to check an id that was correct, which
    is the same class of misdiagnosis the 401 double-probe exists to avoid — so
    a 404 with no credentials attached says so.
    """
    details: dict[str, Any] = {"view": view, "status": 404}
    if provider.has_credentials:
        return LeagueNotFoundError(
            _clean(exc),
            remediation="Confirm the league id, and that this account can see that season.",
            details=details,
        )
    details["reason"] = "credentials_may_be_required"
    return LeagueNotFoundError(
        f"{_clean(exc)} No credentials were sent, and ESPN answers 404 — not 401 — "
        "for a league or season that requires them. Historical seasons in "
        "particular moved behind authentication in 2025.",
        remediation=(
            "Check the league id first; if it is right, run `fantasy-sports auth login` "
            "and try again."
        ),
        details=details,
    )


def _unknown_status(exc: BaseException, *, view: str) -> FantasySportsError:
    """Recover the HTTP status ``espn-api`` folded into an English message.

    Belt and braces for 429: the transport intercepts it before the library
    sees it, but the library's own alternate-shape retry issues a request our
    transport never touches, so a 429 can still arrive by this path. It has no
    ``Retry-After`` — the header did not survive — and reporting one we do not
    have would be worse than reporting none.
    """
    text = _clean(exc)
    status = _as_int(text.rsplit(" ", 1)[-1]) if text else None
    if status == 429:
        return RateLimitedError(
            "ESPN throttled this request (HTTP 429).",
            remediation="Retry with bounded exponential backoff.",
            details={"view": view, "status": status},
        )
    details: dict[str, Any] = {"view": view}
    if status is not None:
        details["status"] = status
    return ProviderUnavailableError(text, details=details)


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Write:
    """What one write has established before its body is built."""

    league_id: str
    season: int
    team_id: int
    scoring_period: int
    credentials: Mapping[str, Any]
    member_id: str | None = None
    """The member the guard checked ownership for, as a pseudonym."""


def _is_credential_set(credentials: Any) -> bool:
    """Whether ``credentials`` is the auth chain's ``CredentialSet``.

    Duck-typed so this module does not import ``auth/`` at module scope.
    """
    return hasattr(credentials, "resolved") and hasattr(credentials, "missing")


def _credentials_block(
    credential_set: Any, *, state_path: Path | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """R12a: how old ``espn_s2`` is, and what the user should be told about it.

    Read from the auth chain's own staleness report, so this and ``auth
    status`` can never disagree. Age is only knowable for a cookie this tool
    stored in the Keychain; anything else -- an environment variable, a plain
    mapping handed to the constructor, a missing state file -- is reported as
    unknown rather than guessed (``docs/memory/credential-leak-channels.md``,
    "an age you cannot attribute is not an age"). Never raises: this is a
    diagnostic, and a write must not fail because its bookkeeping did.
    """
    unknown = "credential age unknown"
    if credential_set is None:
        return {"espn_s2_age_seconds": None, "warning": unknown}
    try:
        from fantasy_sports.auth.staleness import build_auth_status

        report = build_auth_status(credential_set, state_path=state_path, now=now)
        row = next((item for item in report.credentials if item.name == "espn_s2"), None)
        warnings = list(report.warnings)
    except Exception:  # noqa: BLE001 - fail-soft; see docstring
        return {"espn_s2_age_seconds": None, "warning": unknown}
    age = None if row is None or row.age is None else int(row.age.total_seconds())
    if age is None:
        warnings.insert(0, unknown)
    return {"espn_s2_age_seconds": age, "warning": "; ".join(warnings) or None}


def _guid_key(value: Any) -> str | None:
    """A member id in one comparable spelling: brace-wrapped, upper-case."""
    if not isinstance(value, str) or not value.strip().strip("{}"):
        return None
    return "{" + value.strip().strip("{}").upper() + "}"


def _guard(role: SessionRole, team_id: Any) -> int:
    """The ownership guard. Refuse-closed, no override, before any body exists.

    ESPN honoured a commissioner's write on another manager's team without
    being asked to act as LM (brief §6), so "only my team" is ours to enforce
    and ESPN will not do it for us. Ownership that cannot be determined is a
    refusal, never a pass.
    """
    wanted = str(team_id).strip()
    if role.member_id is None:
        raise WriteRejectedError(
            "Refusing to write: the session's ESPN member could not be identified, so "
            "team ownership cannot be verified.",
            kind="not_owned",
            team_id=wanted,
            remediation=(
                "Configure the SWID so ownership can be verified (`fantasy-sports auth "
                "login`, or FANTASY_SPORTS_SWID). Writes are refused until it is."
            ),
        )
    if wanted not in role.owned_team_ids:
        owned = ", ".join(role.owned_team_ids) or "none"
        raise WriteRejectedError(
            f"Refusing to write to team {wanted}: the configured session does not own it.",
            kind="not_owned",
            team_id=wanted,
            remediation=(
                f"Teams this session owns in this league: {owned}. Writes to any other "
                "team are refused regardless of league-manager role."
            ),
            details={"owned_team_ids": list(role.owned_team_ids)},
        )
    return int(wanted)


def _current_period(bootstrap: Mapping[str, Any]) -> int:
    """The league's current scoring period -- the only one ESPN accepts (brief §4)."""
    period = _as_int(bootstrap.get("scoringPeriodId")) if isinstance(bootstrap, Mapping) else None
    if period is None:
        status = bootstrap.get("status") if isinstance(bootstrap, Mapping) else None
        period = _as_int(status.get("latestScoringPeriod")) if isinstance(status, Mapping) else None
    if period is None:
        raise SchemaDriftError(
            "The league payload carries no current scoring period.",
            path=["scoringPeriodId"],
            provider=PROVIDER,
        )
    return period


def _slot_id(name: Any) -> int | None:
    """A lineup slot *name* as ESPN's ``lineupSlotId``, or ``None``.

    Built from ``POSITION_MAP``'s integer keys. Its string keys are the
    free-agent *filter* vocabulary and do not include ``BE``, ``IR`` or
    ``RB/WR/TE`` -- reading them would refuse the three slots a lineup change
    most often targets.
    """
    from espn_api.football.constant import POSITION_MAP

    if not isinstance(name, str) or not name.strip():
        return None
    wanted = name.strip().upper()
    for slot_id, slot_name in POSITION_MAP.items():
        if isinstance(slot_id, int) and isinstance(slot_name, str) and slot_name.upper() == wanted:
            return slot_id
    return None


def _eligible_slot_ids(entry: Mapping[str, Any]) -> frozenset[int] | None:
    """A roster entry's ``eligibleSlots``, or ``None`` when it publishes none."""
    pool = entry.get("playerPoolEntry")
    player = pool.get("player") if isinstance(pool, Mapping) else None
    slots = player.get("eligibleSlots") if isinstance(player, Mapping) else None
    if not isinstance(slots, list):
        return None
    return frozenset(slot for slot in (_as_int(item) for item in slots) if slot is not None)


def _move_item(move: RosterMove, kind: str, team_key: str, team_id: int) -> dict[str, Any]:
    """One ``ADD``/``DROP`` item: ``toTeamId`` on an add, ``fromTeamId`` on a
    drop, both required by ESPN (``06-espn-add-drop-surface.md``). A slot is
    optional: ESPN accepted ``toLineupSlotId`` on an add and fills a drop's
    ``fromLineupSlotId`` itself."""
    player_id = _as_int(getattr(move, "player_id", None))
    if player_id is None:
        raise ConfigInvalidError(
            f"ESPN player ids are numeric; got {getattr(move, 'player_id', None)!r}.",
            kind="argument",
            remediation="Use the provider_id `free-agents` or `roster` reports.",
        )
    item: dict[str, Any] = {"playerId": player_id, "type": kind, team_key: team_id}
    for attribute, key in (("from_slot", "fromLineupSlotId"), ("to_slot", "toLineupSlotId")):
        name = getattr(move, attribute, None)
        if name is None:
            continue
        slot = _slot_id(name)
        if slot is None:
            raise ConfigInvalidError(
                f"{name!r} is not an ESPN lineup slot.",
                kind="argument",
                remediation="Use a slot name from `fantasy-sports league` roster_slots.",
                details={"player_id": str(player_id), "slot": str(name)},
            )
        item[key] = slot
    return item


def _write_payload(response: Any, *, swid_salt: str) -> Any:
    """The write response body, scrubbed, as JSON -- or ``None``.

    Scrubbed with the same salt the reads were, so the ``memberId`` ESPN
    echoes becomes the same pseudonym ``session_role`` reports and the SWID
    never reaches ``raw``. An unreadable body is ``None``: it is not evidence
    of anything, and the status code still decides the outcome.
    """
    try:
        return json.loads(scrub_body(getattr(response, "content", b"") or b"", swid_salt=swid_salt))
    except Exception:  # noqa: BLE001 - see docstring
        return None


def _first_detail(payload: Any) -> tuple[str | None, str | None]:
    """``(details[0].type, messages[0])`` from an ESPN rejection body."""
    if not isinstance(payload, Mapping):
        return None, None
    provider_type = None
    details = payload.get("details")
    if isinstance(details, list):
        for detail in details:
            if isinstance(detail, Mapping) and isinstance(detail.get("type"), str):
                provider_type = detail["type"]
                break
    message = None
    messages = payload.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[0], str):
        message = messages[0]
    return provider_type, message


def _write_outcome(
    status: int | None,
    payload: Any,
    *,
    headers: Mapping[str, str],
    cookies_sent: bool,
    write: _Write,
) -> WriteResult:
    """Map one write-host response to a result or a taxonomy error (brief §5.3).

    * ``200`` -> :class:`WriteResult`.
    * ``401`` with a credential sent -> ``WRITE_REJECTED`` ``kind="auth"``,
      **never** ``AUTH_EXPIRED``: the write host's 401 is the constant
      ``AUTH_MISSING_CREDENTIALS`` for no cookie, a bad cookie and a SWID-only
      request alike, and the read that preceded this write just succeeded on
      the same cookie (brief §5.1 item 1). With nothing sent -> ``AUTH_MISSING``.
      That branch is unreachable through the public methods, which refuse
      before sending without ``espn_s2``; it is kept so the mapping is total.
    * ``400`` without ``details[]`` -> ``SCHEMA_DRIFT``: a body ESPN could not
      read is our bug or ESPN's shape moving, never a user's (brief §5.1 item
      2). A typed 400 is a rejection like any other.
    * ``409`` -> ``WRITE_REJECTED`` with ``kind`` from :data:`_TRAN_KINDS`.
    * ``429`` -> ``RATE_LIMITED`` with ``Retry-After``.
    * ``5xx`` and anything else -> ``PROVIDER_UNAVAILABLE`` with
      ``transaction_state: "unknown"``: we cannot say it did not apply.
    """
    team = str(write.team_id)
    if status == 200:
        return _write_result(payload, write=write)

    provider_type, message = _first_detail(payload)
    if status == 401:
        if not cookies_sent:
            raise AuthMissingError(
                "ESPN refused the write and no espn_s2 cookie was sent.",
                remediation="Run `fantasy-sports auth login`.",
                details={"http_status": 401},
            )
        raise WriteRejectedError(
            "ESPN's write host refused this session (HTTP 401). Its 401 is the same "
            "constant for a missing, invalid, or refused cookie, so this does not say "
            "the cookie expired.",
            kind="auth",
            provider_type=provider_type,
            http_status=401,
            team_id=team,
            remediation=(
                "The same session read this league a moment ago; ESPN refused the "
                "write. Check `fantasy-sports auth status`, then whether write access "
                "to this league changed."
            ),
        )
    if status == 400 and provider_type is None:
        raise SchemaDriftError(
            "ESPN could not read the write request (HTTP 400, no typed reason).",
            path=["transactions"],
            provider=PROVIDER,
            remediation="Stop and file an issue: the transaction body or ESPN's shape changed.",
            details={"http_status": 400},
        )
    if status in (400, 409):
        kind = _TRAN_KINDS.get(provider_type or "", "unknown")
        raise WriteRejectedError(
            f"ESPN rejected the transaction ({provider_type or 'no reason given'}).",
            kind=kind,
            provider_type=provider_type,
            http_status=status,
            team_id=team,
            remediation=message,
        )
    if status == 429:
        raise RateLimitedError(
            "ESPN throttled the write (HTTP 429).",
            retry_after=_retry_after_seconds(_header(headers, "Retry-After")),
            remediation="Wait for `details.retry_after` seconds, then retry.",
            details={"http_status": 429},
        )
    raise ProviderUnavailableError(
        f"ESPN's write host answered HTTP {status}.",
        remediation=_UNKNOWN_STATE_REMEDIATION,
        details={"transaction_state": "unknown", "http_status": status, "team_id": team},
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """One header, case-insensitively whether or not ``headers`` is ``requests``'
    own case-insensitive mapping."""
    found = headers.get(name)
    if found is None:
        found = next(
            (value for key, value in headers.items() if str(key).lower() == name.lower()), None
        )
    return found


def _write_result(payload: Any, *, write: _Write) -> WriteResult:
    """A ``200`` body as a :class:`WriteResult`. Shape changes are drift."""
    if not isinstance(payload, Mapping):
        raise SchemaDriftError(
            "ESPN accepted the write but its response is not a JSON object.",
            path=["transactions"],
            provider=PROVIDER,
            remediation=_UNKNOWN_STATE_REMEDIATION,
            details={"transaction_state": "unknown"},
        )
    transaction_id = payload.get("id")
    status = payload.get("status")
    if not isinstance(transaction_id, str) or not isinstance(status, str):
        missing = [name for name in ("id", "status") if not isinstance(payload.get(name), str)]
        raise SchemaDriftError(
            "ESPN accepted the write but its response lacks " + ", ".join(missing) + ".",
            path=[f"transactions.{name}" for name in missing],
            provider=PROVIDER,
            remediation=_UNKNOWN_STATE_REMEDIATION,
            details={"transaction_state": "unknown"},
        )
    proposed = payload.get("proposedDate")
    proposed_ms = None if isinstance(proposed, bool) else _as_int(proposed)
    items = payload.get("items")
    member = payload.get("memberId")
    team = _as_int(payload.get("teamId"))
    return WriteResult(
        provider=PROVIDER,
        provider_transaction_id=transaction_id,
        status=status,
        is_pending=payload.get("isPending") is True,
        proposed_at=None if proposed_ms is None else from_epoch_millis(proposed_ms),
        bid_amount=_as_int(payload.get("bidAmount")),
        team_id=str(write.team_id if team is None else team),
        items=tuple(dict(item) for item in items if isinstance(item, Mapping))
        if isinstance(items, list)
        else (),
        member_id=member if isinstance(member, str) and member else None,
        credentials=dict(write.credentials),
        raw=dict(payload),
    )


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #


def _teams_by_id(data: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    teams = data.get("teams") if isinstance(data, Mapping) else None
    if not isinstance(teams, list):
        return {}
    return {team["id"]: team for team in teams if isinstance(team, Mapping) and "id" in team}


def _team_raw(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A team's own payload, without its roster.

    The roster is 74 KB of the 75 KB ESPN sends per team, it is a different
    normalized concept with its own read, and carrying it here would make
    ``teams`` output three quarters of a megabyte of data nobody asked for.
    It is reachable through ``fetch_roster`` and through the recorded response
    on :attr:`EspnProvider.last_fetch`.
    """
    return {key: value for key, value in raw.items() if key != "roster"}


def _league_raw(data: Mapping[str, Any]) -> dict[str, Any]:
    """The league-level slice of the bootstrap: everything but teams and schedule."""
    return {key: value for key, value in data.items() if key not in {"teams", "schedule"}}


def _owner_names(team: Any) -> tuple[str, ...]:
    """Display names for a team's owners, plural from day one.

    ESPN's ``owners`` is a list and both ESPN and Yahoo support co-management
    natively, so a single ``owner_name`` silently loses a manager (research §6
    item 1). ``espn-api`` resolves the team's owner SWIDs against the league's
    ``members``, and the *member* objects are where a display name would live —
    but many leagues carry none at all, in which case this is empty rather than
    invented. An empty tuple is ordinary data, not drift.
    """
    names: list[str] = []
    for owner in getattr(team, "owners", []) or []:
        if isinstance(owner, str):
            name = owner.strip()
        elif isinstance(owner, Mapping):
            name = _member_name(owner)
        else:
            name = ""
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _member_name(member: Mapping[str, Any]) -> str:
    """A member's most human name, real name first.

    Order matters and is not the obvious one. ``displayName`` is always
    present, so preferring it looks safer — but in a real private league half
    of them are account handles that name nobody: ``espn19919360``,
    ``ESPNFAN4690433888``. The same members carry ``firstName`` and
    ``lastName``. A consumer printing "ESPNFAN4690433888 lost to johnwulff" is
    not publishable, so the real name wins and the handle is the fallback
    (jwulff/fantasy-sports#29).

    A public league may carry no names at all, in which case this is empty.
    That is ordinary data, not drift.
    """
    parts = [
        str(member.get(key, "")).strip()
        for key in ("firstName", "lastName")
        if str(member.get(key, "")).strip()
    ]
    if parts:
        return " ".join(parts)
    display = member.get("displayName")
    if isinstance(display, str) and display.strip():
        return display.strip()
    return ""


def _roster_slots(data: Mapping[str, Any]) -> dict[str, int]:
    """Slot name to count, from ``rosterSettings.lineupSlotCounts`` (R3a).

    Derived here rather than read off ``espn-api``'s ``position_slot_counts``,
    which zips slot counts against ``list(POSITION_MAP.values())[:n]`` and so
    depends on the literal source ordering of a dict that holds *both*
    directions of the id/name mapping (research §7.4). Mapping each slot id
    through ``POSITION_MAP`` directly is the same information without the
    ordering dependency.
    """
    from espn_api.football.constant import POSITION_MAP

    settings = data.get("settings") if isinstance(data, Mapping) else None
    if not isinstance(settings, Mapping):
        return {}
    counts = settings.get("rosterSettings", {}).get("lineupSlotCounts", {})
    if not isinstance(counts, Mapping):
        return {}
    slots: dict[str, int] = {}
    for slot_id, count in counts.items():
        number = _as_int(slot_id)
        amount = _as_int(count)
        if number is None or not amount:
            continue
        name = POSITION_MAP.get(number)
        if isinstance(name, str) and name:
            slots[name] = slots.get(name, 0) + amount
    return slots


def _kickoff_map(responses: Mapping[str, RawResponse]) -> dict[tuple[int, int], int]:
    """``(proTeamId, scoringPeriod)`` to kickoff in **epoch milliseconds**.

    Built from the raw ``proTeamSchedules_wl`` payload rather than from
    ``espn-api``'s ``Player.schedule``, whose values are
    ``datetime.fromtimestamp(ms / 1000)`` with no ``tz=`` — naive, host-local,
    and different on every machine. This is the re-derivation ARCHITECTURE §14
    item 8 requires; the output layer refuses a naive datetime outright, so
    reaching for the library's value fails loudly rather than silently.
    """
    kickoffs: dict[tuple[int, int], int] = {}
    for team_id, week, game in _pro_games(responses):
        when = _as_int(game.get("date"))
        if when is not None:
            kickoffs[(team_id, week)] = when
    return kickoffs


def _opponent_map(responses: Mapping[str, RawResponse]) -> dict[tuple[int, int], int]:
    """``(proTeamId, scoringPeriod)`` to the opposing ``proTeamId``.

    From the same ``proTeamSchedules_wl`` payload as :func:`_kickoff_map`, and
    for the same reason: it is the one source that answers without a condition
    attached. A team absent for a period is on a bye.
    """
    opponents: dict[tuple[int, int], int] = {}
    for team_id, week, game in _pro_games(responses):
        home = _as_int(game.get("homeProTeamId"))
        away = _as_int(game.get("awayProTeamId"))
        other = away if team_id == home else home
        if other is not None and other != team_id:
            opponents[(team_id, week)] = other
    return opponents


def _pro_games(
    responses: Mapping[str, RawResponse],
) -> Iterator[tuple[int, int, Mapping[str, Any]]]:
    """Each pro team's first game per scoring period, from ``proTeamSchedules_wl``."""
    response = _latest(responses, "proTeamSchedules_wl")
    if response is None or not isinstance(response.payload, Mapping):
        return
    pro_teams = response.payload.get("settings", {}).get("proTeams", [])
    if not isinstance(pro_teams, list):
        return
    for pro_team in pro_teams:
        if not isinstance(pro_team, Mapping):
            continue
        team_id = _as_int(pro_team.get("id"))
        games = pro_team.get("proGamesByScoringPeriod", {})
        if team_id is None or not isinstance(games, Mapping):
            continue
        for period, entries in games.items():
            week = _as_int(period)
            if week is None or not isinstance(entries, list) or not entries:
                continue
            first = entries[0]
            if isinstance(first, Mapping):
                yield team_id, week, first


def _roster_entries(
    transport: _Transport, team_id: int, week: int | None
) -> dict[int, Mapping[str, Any]]:
    """Raw roster entries for one team, keyed by player id.

    Prefers the standalone ``mRoster`` payload when a specific week was loaded,
    because that request replaced the bootstrap's rosters in the object model
    and the bootstrap's copy is now the wrong week.
    """
    for view in ("mRoster", "mTeam+mRoster+mMatchup+mSettings+mStandings"):
        response = _latest(transport.responses, view)
        if response is None or not isinstance(response.payload, Mapping):
            continue
        found = _team_entries(response.payload, team_id)
        if found is not None:
            return found
    return {}


def _team_entries(payload: Any, team_id: int) -> dict[int, Mapping[str, Any]] | None:
    """One team's raw roster entries from a payload, or ``None`` if it has no
    such team."""
    teams = payload.get("teams") if isinstance(payload, Mapping) else None
    for team in teams if isinstance(teams, list) else []:
        if not isinstance(team, Mapping) or team.get("id") != team_id:
            continue
        roster = team.get("roster")
        entries = roster.get("entries", []) if isinstance(roster, Mapping) else []
        return {
            entry["playerId"]: entry
            for entry in entries or []
            if isinstance(entry, Mapping) and "playerId" in entry
        }
    return None


def _free_agent_entries(responses: Mapping[str, RawResponse]) -> dict[int, Mapping[str, Any]]:
    response = _latest(responses, "kona_player_info")
    if response is None or not isinstance(response.payload, Mapping):
        return {}
    players = response.payload.get("players", [])
    if not isinstance(players, list):
        return {}
    return {entry["id"]: entry for entry in players if isinstance(entry, Mapping) and "id" in entry}


def _player(
    player: Any,
    raw: Mapping[str, Any],
    kickoffs: Mapping[tuple[int, int], int],
    opponents: Mapping[tuple[int, int], int],
    week: int | None,
) -> Player:
    """One normalized player, with the context R3 requires for a lineup call.

    Kickoff and opponent come from the schedule maps, keyed by the player's
    pro team and the scoring period, exactly as :func:`_lineup_entry` does for
    a box score. The club is the one the player was on *that week*, read from
    the raw entry's stat row for the period the way ``BoxPlayer`` does;
    ``Player.proTeam`` is today's club, and keying a past week by it would
    show a traded player facing his new club's opponent. A club with no game
    that period is on a bye: no kickoff, no opponent. ``"None"`` is
    ``espn-api``'s free-agent club and is never a team.
    """
    from espn_api.football.constant import PRO_TEAM_MAP

    pro_team_id = _week_pro_team_id(raw, week)
    if pro_team_id is None:
        current = _library_str(player.proTeam)
        pro_team_id = _pro_team_ids().get(current) if current else None
    pro_team = _library_str(PRO_TEAM_MAP.get(pro_team_id)) if pro_team_id is not None else None
    keyed = pro_team_id is not None and week is not None
    kickoff_ms = kickoffs.get((pro_team_id, week)) if keyed else None
    opponent = opponents.get((pro_team_id, week)) if keyed else None
    projected = player.stats.get(week, {}).get("projected_points") if week is not None else None
    return Player(
        provider=PROVIDER,
        provider_id=str(player.playerId),
        name=player.name,
        position=player.position,
        eligible_slots=tuple(player.eligibleSlots),
        status=getattr(player, "active_status", None),
        injury_status=player.injuryStatus,
        pro_team=pro_team,
        opponent=None if opponent is None else _library_str(PRO_TEAM_MAP.get(opponent)),
        projected_points=None if projected is None else float(projected),
        kickoff=None if kickoff_ms is None else from_epoch_millis(kickoff_ms),
        raw=dict(raw),
    )


def _week_pro_team_id(raw: Mapping[str, Any], week: int | None) -> int | None:
    """The club a player was on in ``week``, from the raw entry's stat rows.

    A roster entry nests the player under ``playerPoolEntry``; a free-agent
    entry does not. Either row for the period -- actual (``statSourceId`` 0)
    or projection (1) -- names the club. ``None`` when nothing does, and the
    caller falls back to the player's current club.

    A ``proTeamId`` of ``0`` is **not** a witness that the player was unsigned
    that week: before a game is played the only row for the period is the
    projection, and it carries ``0`` for every player. Treating it as an
    answer blanked most of a live roster (PR #87 review); it is a gap.
    """
    if week is None:
        return None
    pool = raw.get("playerPoolEntry", raw)
    stats = pool.get("player", {}).get("stats", []) if isinstance(pool, Mapping) else []
    if not isinstance(stats, list):
        return None
    for row in stats:
        if isinstance(row, Mapping) and _as_int(row.get("scoringPeriodId")) == week:
            team_id = _as_int(row.get("proTeamId"))
            if team_id:
                return team_id
    return None


def _roster_slot(
    player: Any,
    raw: Mapping[str, Any],
    kickoffs: Mapping[tuple[int, int], int],
    opponents: Mapping[tuple[int, int], int],
    week: int | None,
    now: datetime,
) -> RosterSlot:
    normalized = _player(player, raw, kickoffs, opponents, week)
    slot = player.lineupSlot or ""
    return RosterSlot(
        provider=PROVIDER,
        provider_id=str(player.playerId),
        player=normalized,
        slot=slot,
        is_starter=slot not in STARTER_EXCLUDED_SLOTS,
        is_locked=None if normalized.kickoff is None else normalized.kickoff <= now,
        raw=dict(raw),
    )


def _free_agent(
    player: Any,
    raw: Mapping[str, Any],
    kickoffs: Mapping[tuple[int, int], int],
    opponents: Mapping[tuple[int, int], int],
    week: int | None,
    now: datetime,
) -> FreeAgent:
    owned = getattr(player, "percent_owned", None)
    return FreeAgent(
        provider=PROVIDER,
        provider_id=str(player.playerId),
        player=_player(player, raw, kickoffs, opponents, week),
        percent_owned=None if owned is None or owned < 0 else float(owned),
        raw=dict(raw),
    )


def _scoring_periods(league: Any, since: datetime | None, scoring_period: int | None) -> list[int]:
    """Which scoring periods to ask ``mTransactions2`` about.

    ESPN's transaction view takes one period and offers no date range, so
    ``since`` can only be served by sweeping. The sweep is bounded by the
    league's own ``firstScoringPeriod`` and its current week -- never by a
    guess about how long a season is.
    """
    explicit = _as_int(scoring_period)
    if explicit is not None:
        return [explicit]
    current = _as_int(league.current_week) or _as_int(league.scoringPeriodId) or 1
    if since is None:
        return [current]
    first = _as_int(getattr(league, "firstScoringPeriod", None)) or 1
    return list(range(min(first, current), current + 1))


def _matchup_period_for(league: Any, scoring_period: int) -> int:
    """Which matchup period contains ``scoring_period``.

    ``settings.matchup_periods`` is ESPN's own mapping, shaped
    ``{matchupPeriodId: [scoringPeriodId, ...]}``. They are 1:1 for a normal
    week and diverge when a playoff round spans two NFL weeks, which is exactly
    the path an in-season ESPN-only test never exercises. Falling back to the
    scoring period when the mapping has no entry is right: that is what the
    identity case looks like.
    """
    periods = getattr(getattr(league, "settings", None), "matchup_periods", None) or {}
    for matchup_id, scoring_periods in periods.items():
        if scoring_period in (scoring_periods or []):
            resolved = _as_int(matchup_id)
            if resolved is not None:
                return resolved
    return scoring_period


def _schedule_entries(
    responses: Mapping[str, RawResponse], matchup_period: int
) -> dict[tuple[int, int], Mapping[str, Any]]:
    """Raw ``schedule[]`` entries for one matchup period, keyed by team-id pair."""
    for view in ("mMatchupScore", "mTeam+mRoster+mMatchup+mSettings+mStandings"):
        response = _latest(responses, view)
        if response is None or not isinstance(response.payload, Mapping):
            continue
        schedule = response.payload.get("schedule", [])
        if not isinstance(schedule, list):
            continue
        found: dict[tuple[int, int], Mapping[str, Any]] = {}
        for entry in schedule:
            if not isinstance(entry, Mapping) or entry.get("matchupPeriodId") != matchup_period:
                continue
            home = _as_int(entry.get("home", {}).get("teamId")) or 0
            away = _as_int(entry.get("away", {}).get("teamId")) or 0
            found[(home, away)] = entry
        if found:
            return found
    return {}


def _matchup(
    matchup: Any,
    raw_entries: Mapping[tuple[int, int], Mapping[str, Any]],
    *,
    scoring_period: int,
    matchup_period: int,
    index: int,
) -> Matchup:
    home_id = _team_id_of(matchup.home_team)
    away_id = _team_id_of(matchup.away_team)
    raw = raw_entries.get((home_id, away_id), {})
    provider_id = str(raw.get("id")) if raw.get("id") is not None else f"{matchup_period}-{index}"
    return Matchup(
        provider=PROVIDER,
        provider_id=provider_id,
        week=matchup_period,
        team_a_provider_id=str(home_id),
        team_a_score=float(matchup.home_score or 0.0),
        team_b_provider_id=str(away_id),
        team_b_score=float(matchup.away_score or 0.0),
        is_playoff=bool(matchup.is_playoff),
        scoring_period_id=scoring_period,
        matchup_period_id=matchup_period,
        raw=dict(raw),
    )


BENCH_SLOTS: frozenset[str] = frozenset({"BE", "IR"})
"""Slots that do not count toward a team's score.

ESPN's own vocabulary, kept here rather than in ``core/`` because slot
semantics explicitly do not normalize (ADR-0002). ``IR`` counts as bench for
scoring even though it is not a bench slot for roster-legality purposes.
"""


def _box_score(
    box: Any,
    *,
    scoring_period: int,
    matchup_period: int,
    index: int,
    opponents: Mapping[tuple[int, int], int],
) -> BoxScore:
    home_id = _team_id_of(box.home_team)
    away_id = _team_id_of(box.away_team)
    return BoxScore(
        provider=PROVIDER,
        provider_id=f"{matchup_period}-{index}",
        week=matchup_period,
        team_a_provider_id=str(home_id),
        team_a_score=float(getattr(box, "home_score", 0.0) or 0.0),
        team_a_lineup=_lineup(getattr(box, "home_lineup", None), opponents, scoring_period),
        team_b_provider_id=str(away_id),
        team_b_score=float(getattr(box, "away_score", 0.0) or 0.0),
        team_b_lineup=_lineup(getattr(box, "away_lineup", None), opponents, scoring_period),
        is_playoff=bool(getattr(box, "is_playoff", False)),
        scoring_period_id=scoring_period,
        matchup_period_id=matchup_period,
        raw={"matchup_type": getattr(box, "matchup_type", None)},
    )


def _lineup(
    players: Any, opponents: Mapping[tuple[int, int], int], scoring_period: int
) -> tuple[LineupEntry, ...]:
    return tuple(_lineup_entry(player, opponents, scoring_period) for player in (players or []))


def _lineup_entry(
    player: Any, opponents: Mapping[tuple[int, int], int], scoring_period: int
) -> LineupEntry:
    from espn_api.football.constant import PRO_TEAM_MAP

    slot = str(getattr(player, "slot_position", "") or "")
    # `BoxPlayer.proTeam` is already the club for *this* scoring period: the
    # library prefers the week's own stat row over the player's current team,
    # which is what makes a mid-season trade read correctly.
    pro_team = _library_str(getattr(player, "proTeam", None))
    team_id = _pro_team_ids().get(pro_team) if pro_team else None
    opponent = opponents.get((team_id, scoring_period)) if team_id is not None else None
    pro_opponent = (
        _library_str(PRO_TEAM_MAP.get(opponent))
        if opponent is not None
        else _library_str(getattr(player, "pro_opponent", None))
    )
    return LineupEntry(
        provider=PROVIDER,
        provider_id=str(getattr(player, "playerId", "") or ""),
        slot=slot,
        player_name=str(getattr(player, "name", "") or ""),
        position=_optional_str(getattr(player, "position", None)),
        pro_team=pro_team,
        pro_opponent=pro_opponent,
        projected_points=_optional_float(getattr(player, "projected_points", None)),
        actual_points=_optional_float(getattr(player, "points", None)),
        started=slot not in BENCH_SLOTS and bool(slot),
        raw={},
    )


def _pro_team_ids() -> dict[str, int]:
    """Abbreviation to ESPN pro team id: ``PRO_TEAM_MAP`` the other way round."""
    from espn_api.football.constant import PRO_TEAM_MAP

    return {abbrev: team_id for team_id, abbrev in PRO_TEAM_MAP.items() if team_id}


def _library_str(value: Any) -> str | None:
    """:func:`_optional_str`, minus ``espn-api``'s ``"None"`` sentinel.

    ``PRO_TEAM_MAP[0]`` is the string ``"None"`` -- the free-agent "team" -- and
    ``BoxPlayer.pro_opponent`` defaults to the same string, so it reaches the
    output as a club abbreviation unless it is stopped here.
    """
    text = _optional_str(value)
    return None if text == "None" else text


def _optional_str(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _team_id_of(team: Any) -> int:
    """``espn-api`` leaves an unresolved side as a bare team id integer."""
    if isinstance(team, int):
        return team
    return _as_int(getattr(team, "team_id", None)) or 0


def _remember(seen: set[tuple[Any, ...]], transaction: Transaction) -> bool:
    """Record a transaction's identity; ``False`` if the other surface had it.

    Identity is deliberately not the provider id: ``mTransactions2`` and the
    activity feed number the same move differently, and matching on the id
    would make every merged trade a duplicate.
    """
    key = (
        transaction.type,
        transaction.team_provider_id,
        tuple(sorted(transaction.players_in)),
        tuple(sorted(transaction.players_out)),
        None if transaction.timestamp is None else int(transaction.timestamp.timestamp()),
    )
    if key in seen:
        return False
    seen.add(key)
    return True


def _transaction(item: Any) -> Transaction | None:
    """One ``mTransactions2`` row, or ``None`` where we have no honest type.

    ``TRADE_PROPOSAL``, ``TRADE_DECLINE``, ``TRADE_VETO`` and ``WAIVER_ERROR``
    are provider-specific *states*, not roster moves, and ``core.Transaction``'s
    four-value vocabulary has no equivalent for them (ADR-0002). They are
    dropped from normalized output and remain readable through
    :meth:`EspnProvider.fetch_raw` — the narrowing is lossy on purpose, and
    documented on the model.
    """
    players_in = [str(entry.playerId) for entry in item.items if entry.type == "ADD"]
    players_out = [str(entry.playerId) for entry in item.items if entry.type == "DROP"]

    kind = _TRANSACTION_TYPE_MAP.get(item.type)
    if kind is None and item.type == "FREEAGENT":
        kind = "add" if players_in else "drop"
    if kind is None:
        return None

    return Transaction(
        provider=PROVIDER,
        provider_id=str(getattr(item, "id", "") or f"{item.type}-{item.scoring_period}"),
        type=kind,
        team_provider_id=_team_provider_id(item.team),
        players_in=tuple(players_in),
        players_out=tuple(players_out),
        faab_spent=_as_int(item.bid_amount),
        timestamp=None if item.date is None else from_epoch_millis(item.date),
        raw={
            "type": item.type,
            "status": item.status,
            "scoringPeriodId": item.scoring_period,
            "bidAmount": item.bid_amount,
            "date": item.date,
            "items": [
                {"type": entry.type, "playerId": entry.playerId, "player": entry.player}
                for entry in item.items
            ],
        },
    )


def _activity_transactions(activity: Any) -> list[Transaction]:
    """The activity feed's rows, normalized into the same vocabulary.

    A trade arrives as **two** rows — ``TRADE_SENT`` from one team and
    ``TRADE_RECEIVED`` by the other — so each becomes one ``Transaction`` from
    that team's point of view, with the player moving out of the sender and
    into the receiver. That is the reconciliation research §6 item 2 asks for:
    ``core.Transaction`` sees one vocabulary, and the caller never learns that
    ESPN has two.
    """
    when = _as_int(getattr(activity, "date", None))
    timestamp = None if when is None else from_epoch_millis(when)
    found: list[Transaction] = []
    for index, action in enumerate(getattr(activity, "actions", []) or []):
        team, verb, player, bid = _unpack_action(action)
        kind = _ACTIVITY_TYPE_MAP.get(verb)
        if kind is None:
            continue
        player_id = _player_id_of(player)
        outgoing = verb in {"DROPPED", "TRADE_SENT"}
        found.append(
            Transaction(
                provider=PROVIDER,
                provider_id=f"activity-{when}-{index}",
                type=kind,
                team_provider_id=_team_provider_id(team),
                players_in=() if outgoing else (player_id,),
                players_out=(player_id,) if outgoing else (),
                faab_spent=_as_int(bid),
                timestamp=timestamp,
                raw={"source": "kona_league_communication", "action": verb, "date": when},
            )
        )
    return found


def _unpack_action(action: Any) -> tuple[Any, str, Any, Any]:
    items = list(action)
    while len(items) < 4:
        items.append(None)
    return items[0], str(items[1] or ""), items[2], items[3]


def _team_provider_id(team: Any) -> str | None:
    team_id = _as_int(getattr(team, "team_id", None))
    return None if team_id is None else str(team_id)


def _player_id_of(player: Any) -> str:
    player_id = _as_int(getattr(player, "playerId", None))
    if player_id is None:
        player_id = _as_int(player)
    return "" if player_id is None else str(player_id)


def _position_key(position: str) -> str | None:
    """``position`` as ESPN spells it, or ``None`` if ESPN has no such slot."""
    from espn_api.football.constant import POSITION_MAP

    # `POSITION_MAP` holds both directions in one dict, so a *string* key with
    # an `int` value is a name ESPN's filter understands; an `int` key with a
    # string value is the reverse lookup and is not what we want here.
    for candidate in (position, position.upper()):
        if candidate in POSITION_MAP and isinstance(POSITION_MAP[candidate], int):
            return candidate
    return None
