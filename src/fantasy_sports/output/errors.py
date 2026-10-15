"""Exit statuses, and the rule that an unknown failure is never a throttle.

ADR-0004's consequences say cron jobs can branch on exit codes. That is only
true if the codes are *distinct*, so every taxonomy code gets its own status
rather than everything collapsing onto ``1``. The numbers are part of the
contract: changing one is an API change, exactly like renaming a code.

The range matters. ``0`` means success, ``1`` is the conventional catch-all,
``2`` is the usage error every argument parser emits, and ``126``, ``127`` and
``128+n`` are claimed by the shell for "not executable", "not found" and "killed
by signal N". Everything here sits between ``3`` and ``12``, which is inside the
portable range and leaves room to grow without colliding with any of that.

:func:`classify` implements R12's hard rule: **an unclassifiable failure maps to
availability, never to throttling.** ``PROVIDER_UNAVAILABLE`` tells an agent to
retry with bounded backoff, which is safe when we are wrong. ``RATE_LIMITED``
tells it to back off and wait, which is unsafe when we are wrong — it converts
"we do not know what happened" into "stop acting", and ESPN's throttle signal is
unconfirmed enough that we would be wrong often.
"""

from __future__ import annotations

from fantasy_sports.core.errors import ErrorCode, FantasySportsError, ProviderUnavailableError

__all__ = [
    "EXIT_CODES",
    "EXIT_OK",
    "EXIT_UNEXPECTED",
    "EXIT_USAGE",
    "classify",
    "exit_code_for",
]

EXIT_OK = 0
EXIT_UNEXPECTED = 1
"""A failure that never reached the taxonomy at all — a crash, not a diagnosis."""
EXIT_USAGE = 2
"""Bad arguments. Owned by the CLI layer (#9); reserved here so nothing takes it."""

EXIT_CODES: dict[ErrorCode, int] = {
    ErrorCode.AUTH_MISSING: 3,
    ErrorCode.AUTH_EXPIRED: 4,
    ErrorCode.LEAGUE_NOT_FOUND: 5,
    ErrorCode.CONFIG_INVALID: 6,
    ErrorCode.PROVIDER_UNAVAILABLE: 7,
    ErrorCode.RATE_LIMITED: 8,
    ErrorCode.SCHEMA_DRIFT: 9,
    ErrorCode.NOT_AVAILABLE: 10,
    ErrorCode.WRITE_REJECTED: 11,
    ErrorCode.WRITE_DIVERGED: 12,
}
"""Taxonomy code -> process exit status. One status per code, and never zero."""


def exit_code_for(error: FantasySportsError | ErrorCode) -> int:
    """The exit status for a failure."""
    code = error if isinstance(error, ErrorCode) else error.code
    return EXIT_CODES[code]


def classify(exc: BaseException) -> FantasySportsError:
    """Map any exception onto the taxonomy, defaulting to availability.

    An error that already carries a code is returned untouched — it was
    classified by whoever had the evidence. Everything else becomes
    :class:`~fantasy_sports.core.errors.ProviderUnavailableError`, whose
    docstring calls this the honest landing place for a failure we cannot
    positively classify.

    The exception's type name is recorded in ``details["cause"]`` so a human
    debugging a report has something to grep for. The message is included
    because it is usually the only diagnosis available; it is scrubbed by
    :class:`~fantasy_sports.core.errors.FantasySportsError` at construction, so
    a credential a caller interpolated into it does not survive.
    """
    if isinstance(exc, FantasySportsError):
        return exc
    name = type(exc).__name__
    return ProviderUnavailableError(
        f"Unclassified failure ({name}): {exc}",
        details={"cause": name},
    )
