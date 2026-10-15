"""The error taxonomy — ``docs/ARCHITECTURE.md`` §5, origin R12.

Every failure this tool reports carries a stable machine code so an agent can
tell "your cookies died, ask the human" from "ESPN is down, retry later"
without parsing English. **Adding, renaming, or removing a code is an API
change**, not a refactor.

This module lives in ``core/`` rather than with the output layer on purpose:
the domain models raise :class:`SchemaDriftError` while validating a payload,
and ``output/`` depends on ``core/``, not the other way round. Putting the
taxonomy downstream would make the models unlandable.

Two rules the classes below encode:

* **Unclassifiable means unavailable, never throttled** (R12). ESPN's throttle
  signal is unconfirmed, so a failure we cannot positively classify maps to
  :class:`ProviderUnavailableError` with bounded retry. Guessing
  ``RATE_LIMITED`` would teach an agent to back off from an outage forever.
* **An error payload is not a data channel.** ``details`` is for field *names*,
  paths, and status codes — never provider bytes, response bodies, or anything
  that could carry an ``espn_s2`` value or a SWID GUID (CLAUDE.md rule 5).
* **``remediation`` is a first-class payload key, not a detail.** It is the part
  a caller acts on, and a key that rides in a general-purpose bag is a key some
  consumers will never read. ``agent_action`` is the class-level instruction
  ("ask the human to re-auth"); ``remediation`` is the *instance's* concrete
  next step, naming the environment variable or the file that would actually fix
  this failure. Promoted from ``details`` in the U5 output layer, on the
  decision recorded on jwulff/fantasy-sports#6; ADR-0004 amended to match.
* **The base class scrubs, so no raise site has to remember to.** Every
  message and every ``details`` value passes through
  :func:`~fantasy_sports.core.redaction.redact` at construction. The leak this
  defends against is written by a caller — ``raise ProviderUnavailableError(f"GET {url}")``
  where ``url`` carries ``?espn_s2=…`` — and raise sites are where scrubbing
  gets forgotten (``docs/memory/credential-leak-channels.md``).

Nothing here imports anything beyond the standard library, and nothing here is
allowed to import ``typer``, ``click``, ``rich``, or ``espn_api``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar, Final

from fantasy_sports.core.redaction import redact, scrub

__all__ = [
    "ERROR_TYPES",
    "WRITE_REJECTED_KINDS",
    "AuthExpiredError",
    "AuthMissingError",
    "ConfigInvalidError",
    "ErrorCode",
    "FantasySportsError",
    "LeagueNotFoundError",
    "NotAvailableError",
    "ProviderUnavailableError",
    "RateLimitedError",
    "SchemaDriftError",
    "WriteDivergedError",
    "WriteRejectedError",
    "error_type_for",
]


class ErrorCode(StrEnum):
    """The stable machine codes. This set is the API."""

    AUTH_MISSING = "AUTH_MISSING"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    LEAGUE_NOT_FOUND = "LEAGUE_NOT_FOUND"
    CONFIG_INVALID = "CONFIG_INVALID"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"
    WRITE_REJECTED = "WRITE_REJECTED"
    WRITE_DIVERGED = "WRITE_DIVERGED"


class FantasySportsError(Exception):
    """Base class for every failure this tool reports with a machine code.

    Subclasses set :attr:`code`, :attr:`retryable`, and :attr:`agent_action`.
    ``output/`` renders :meth:`to_dict` to stderr as JSON with a nonzero exit;
    the exit-status mapping itself belongs to the output layer, not here.
    """

    code: ClassVar[ErrorCode]
    retryable: ClassVar[bool] = False
    agent_action: ClassVar[str]

    def __init__(
        self,
        message: str,
        *,
        remediation: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        # Scrub at construction, not at render. Once the message is stored
        # redacted there is no path — args, str(), repr(), traceback, payload —
        # that can put the value back. This is the guarantee that used to live
        # on `auth.chain.AuthError`; it belongs here so that every error type
        # has it rather than only the auth ones.
        message = redact(message)
        super().__init__(message)
        self.message = message
        self.remediation = redact(remediation) if remediation else None
        # Copied, not aliased: a caller must not be able to mutate a rendered
        # payload after the fact, and we must not retain a reference into a
        # provider response.
        self.details: dict[str, Any] = {
            key: scrub(value) for key, value in dict(details or {}).items()
        }

    def _record_detail(self, key: str, value: Any) -> None:
        """Add one detail post-``super().__init__``, scrubbed like the rest.

        Subclasses that derive a detail from their own arguments go through
        here so nothing reaches :meth:`to_dict` unscrubbed.
        """
        self.details[key] = scrub(value)

    def to_dict(self) -> dict[str, Any]:
        """The stable error payload. Every key is always present.

        ``remediation`` and ``details`` are ``None`` rather than absent when
        empty. A key a consumer *may* have to look for is a key some consumers
        will not look for, and the whole point of the taxonomy is that a
        failure tells its caller what to do next.
        """
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "agent_action": self.agent_action,
            "remediation": self.remediation,
            "details": dict(self.details) if self.details else None,
        }


class AuthMissingError(FantasySportsError):
    """No credentials are configured for this provider."""

    code = ErrorCode.AUTH_MISSING
    retryable = False
    agent_action = "Ask the human to run `fantasy-sports auth login`."


class AuthExpiredError(FantasySportsError):
    """Credentials exist but the provider rejected them."""

    code = ErrorCode.AUTH_EXPIRED
    retryable = False
    agent_action = "Ask the human to re-extract their ESPN cookies."


class LeagueNotFoundError(FantasySportsError):
    """The league id is wrong, or these credentials cannot see it."""

    code = ErrorCode.LEAGUE_NOT_FOUND
    retryable = False
    agent_action = "Ask the human to confirm the league id and their access."


class ConfigInvalidError(FantasySportsError):
    """``config.toml`` will not parse, or an argument only the provider can
    validate came back invalid.

    Distinct from :class:`LeagueNotFoundError` on purpose, and the distinction
    is the whole reason the code was added (decision on
    jwulff/fantasy-sports#6). ``LEAGUE_NOT_FOUND`` tells an agent to retry with
    a different ``--league``, which cannot possibly work when the file itself
    will not parse. This is user-fixable and not retryable: a human changes
    what they gave us, and nothing the agent does on its own changes the
    outcome.

    **Two causes, one code (decided on jwulff/fantasy-sports#48, ADR-0004
    amended by ADR-0009).** A config file that will not parse and a CLI
    argument ESPN itself rejects — a ``--pos`` it does not recognise, a
    ``--filter`` that is not JSON, a ``raw`` with no ``--view`` — read
    identically to an agent: not retryable, a human must change the input
    named in the message. Adding a second code for the second cause would
    duplicate that instruction under a new name. ``kind`` records which one
    this instance is, for a consumer that wants to log or branch on the
    distinction without needing a second exit status to do it; it never
    changes ``retryable`` or the exit code.
    """

    code = ErrorCode.CONFIG_INVALID
    retryable = False
    agent_action = (
        "Ask the human to fix the input named in the message — a config value or a "
        "command argument. Retrying unchanged cannot work."
    )

    def __init__(
        self,
        message: str,
        *,
        kind: str = "config",
        remediation: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, remediation=remediation, details=details)
        self.kind = kind
        self._record_detail("kind", kind)


class NotAvailableError(FantasySportsError):
    """The provider positively refuses this request; it was never sent.

    This is the opposite case from :class:`ProviderUnavailableError`: not "we
    don't know what happened", but "we know exactly what happened, and it will
    never succeed as asked." ``espn-api`` refuses some reads outright, on the
    *year*, before any HTTP request is made — box scores, free agents, and the
    activity feed all predate 2019 for some leagues, and ESPN will not start
    serving them. Landing that on ``PROVIDER_UNAVAILABLE`` would hand an agent
    ``retryable: true`` and a bounded-backoff instruction for something that
    can never come back, which is the opposite of what R12 guards against: R12
    says an *unclassifiable* failure must not be guessed into a specific code,
    but this is a **positively classifiable** one, and guessing it onto
    availability is just as wrong in the other direction (decided on
    jwulff/fantasy-sports#45; ADR-0004 amended to match).
    """

    code = ErrorCode.NOT_AVAILABLE
    retryable = False
    agent_action = "Do not retry as sent; check `remediation` for a supported alternative."


class ProviderUnavailableError(FantasySportsError):
    """The provider is down, timed out, or failed in a way we cannot classify.

    This is the honest landing place for an unclassifiable failure (R12) —
    bounded retry is safe, and claiming ``RATE_LIMITED`` would not be.
    """

    code = ErrorCode.PROVIDER_UNAVAILABLE
    retryable = True
    agent_action = "Retry with bounded exponential backoff."


class RateLimitedError(FantasySportsError):
    """The provider explicitly throttled us.

    Only raise this on a positive throttle signal — never as a guess.
    """

    code = ErrorCode.RATE_LIMITED
    retryable = True
    agent_action = "Retry after `details.retry_after` seconds."

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        remediation: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, remediation=remediation, details=details)
        self.retry_after = retry_after
        if retry_after is not None:
            self._record_detail("retry_after", retry_after)


class SchemaDriftError(FantasySportsError):
    """A response no longer has the shape we know how to read.

    This is the trigger for the canary's issue-filing and the client-side
    health check, so it must fire on *genuine* shape changes only. An absent
    optional value — a transaction with no processed date, a provider that
    does not publish slot eligibility — is ordinary data, not drift. A false
    alarm on a routine payload is the fastest way to make the signal
    ignorable.

    ``path`` records *where* the shape broke, by field name. Never put a
    provider value in it.
    """

    code = ErrorCode.SCHEMA_DRIFT
    retryable = False
    agent_action = "Stop and file an issue; the provider's response shape changed."

    def __init__(
        self,
        message: str,
        *,
        path: str | Sequence[str] | None = None,
        provider: str | None = None,
        remediation: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, remediation=remediation, details=details)
        self.path: tuple[str, ...] = (path,) if isinstance(path, str) else tuple(path or ())
        self.provider = provider
        if self.path:
            self._record_detail("path", list(self.path))
        if provider is not None:
            self._record_detail("provider", provider)


WRITE_REJECTED_KINDS: Final[tuple[str, ...]] = (
    "roster_lock",
    "slot_limit",
    "same_slot",
    "not_current_period",
    "budget_exceeded",
    "position_limit",
    "already_dropped",
    "roster_full",
    "not_owned",
    "auth",
    "unknown",
)
"""The closed vocabulary for :attr:`WriteRejectedError.kind`. We own it.

A reason ESPN adds that we have not mapped yet lands on ``"unknown"`` with the
provider's own string in ``provider_type``, so it is reported rather than lost
and adding it later is not an API change. ``"not_owned"`` is ours, not ESPN's:
the client-side guard that refuses a write for a team the session does not
own, because ESPN does not (``docs/research/05-espn-write-surface.md`` §6).
"""


class WriteRejectedError(FantasySportsError):
    """The provider refused a write, or we refused it before sending.

    One code with a closed ``kind`` discriminator, not one code per reason
    (ADR-0010, the same move ADR-0009 made for ``CONFIG_INVALID``). Every reason
    tells an agent the same thing: this exact request will not succeed, so do
    not repeat it; read the roster and re-plan. ``kind`` is for the consumer
    that wants to *choose* the re-plan, and it never changes ``retryable`` or
    the exit status.

    **A write-host 401 with a credential sent is ``kind="auth"``, never
    :class:`AuthExpiredError`.** The write host's 401 body is a constant that
    does not distinguish expired from absent from refused, and every write is
    preceded by a read of the same league on the same cookie, so that read
    having just succeeded is evidence the session is valid. ``AUTH_EXPIRED``
    needs positive evidence and this is not it (brief §5.1 item 1;
    ``docs/memory/espn-401-tells-you-nothing.md``).

    ``provider_type`` carries ESPN's ``details[0].type`` verbatim and is
    ``None`` when ESPN sent none. ``transaction_applied`` is always ``False``,
    because a rejection never applies (a ``409`` does not, brief §3). It is not a
    parameter, so no raise site can claim otherwise; it is in ``details`` so a
    consumer never has to infer it.

    ``kind`` defaults to ``"unknown"``, the honest answer when a raise site has
    not classified the reason, as ``ConfigInvalidError`` defaults to ``"config"``.
    An unknown ``kind`` is a bug in the caller, so it raises ``ValueError``
    rather than becoming a taxonomy error.
    """

    code = ErrorCode.WRITE_REJECTED
    retryable = False
    agent_action = "Do not retry unchanged; read the roster and re-plan."

    def __init__(
        self,
        message: str,
        *,
        kind: str = "unknown",
        provider_type: str | None = None,
        http_status: int | None = None,
        team_id: str | int | None = None,
        remediation: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if kind not in WRITE_REJECTED_KINDS:
            raise ValueError(
                f"unknown WRITE_REJECTED kind {kind!r}; expected one of {WRITE_REJECTED_KINDS}"
            )
        super().__init__(message, remediation=remediation, details=details)
        self.kind = kind
        # Attributes hold the scrubbed value, not the original: `vars(exc)` and a
        # captured-locals traceback repr read them, not `details`
        # (docs/memory/credential-leak-channels.md, channel 1).
        self.provider_type = None if provider_type is None else redact(provider_type)
        self.http_status = http_status
        self.team_id = None if team_id is None else redact(str(team_id))
        # A rejection never applied (a 409 does not apply, brief §3), so this is
        # a constant, not an argument a raise site could get wrong.
        self.transaction_applied = False
        # All five, always, so the payload shape does not depend on the reason.
        self._record_detail("kind", kind)
        self._record_detail("provider_type", self.provider_type)
        self._record_detail("http_status", http_status)
        self._record_detail("team_id", self.team_id)
        self._record_detail("transaction_applied", False)


class WriteDivergedError(FantasySportsError):
    """A write was accepted, but the read-back does not match what was asked.

    The opposite failure from :class:`WriteRejectedError`: the provider said
    yes and the world is not as we asked, so the transaction may well have
    applied (or something else moved the roster in between). Retrying could
    double-apply, which is why it is not folded into a rejection and why it
    gets its own exit status.

    ``expected`` and ``observed`` are field-name-level maps (player id to
    slot id, or similar) of only the entries that disagree. Never put
    provider free text in them.
    """

    code = ErrorCode.WRITE_DIVERGED
    retryable = False
    agent_action = "Do not retry; the write may have applied. Read the roster and reconcile."

    def __init__(
        self,
        message: str,
        *,
        expected: Mapping[str, Any] | None = None,
        observed: Mapping[str, Any] | None = None,
        remediation: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, remediation=remediation, details=details)
        # `scrub` walks values but not mapping keys, and these maps are keyed by
        # caller-supplied ids, so the keys are redacted here.
        # The attributes hold the scrubbed maps for the same reason `details` does.
        self.expected: dict[str, Any] = {
            redact(str(k)): scrub(v) for k, v in dict(expected or {}).items()
        }
        self.observed: dict[str, Any] = {
            redact(str(k)): scrub(v) for k, v in dict(observed or {}).items()
        }
        self._record_detail("expected", self.expected)
        self._record_detail("observed", self.observed)


ERROR_TYPES: tuple[type[FantasySportsError], ...] = (
    AuthMissingError,
    AuthExpiredError,
    LeagueNotFoundError,
    ConfigInvalidError,
    NotAvailableError,
    ProviderUnavailableError,
    RateLimitedError,
    SchemaDriftError,
    WriteRejectedError,
    WriteDivergedError,
)

_BY_CODE: dict[str, type[FantasySportsError]] = {cls.code.value: cls for cls in ERROR_TYPES}


def error_type_for(code: ErrorCode | str) -> type[FantasySportsError]:
    """Look up the exception class for a taxonomy code.

    Raises ``KeyError`` for anything outside the taxonomy — an unknown code is
    a programming error here, not a provider failure to be reported.
    """
    return _BY_CODE[str(code)]
