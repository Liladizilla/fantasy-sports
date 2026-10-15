"""Tell the owner about an irreversible write without being asked.

Why this exists (jwulff/fantasy-sports#113, requirement R7a): the journal
records every write, but a drop nobody reads about is still a drop nobody
noticed. This hook pushes each irreversible write at a command John configures
in ``config.toml`` (``[notify] irreversible_command``), so the audit does not
depend on him remembering to run ``journal list``.

The package stays ignorant of where the news goes. The command receives the
journal entry as JSON on stdin and does whatever the owner wants: append to a
log, file a task, write a vault note. That script lives outside this repo.

Contract
--------

* :func:`notify_irreversible` **never raises** and never blocks the write that
  called it. A missing command, a non-zero exit, a timeout, an unreadable
  config: each is reported in the returned :class:`NotifyResult`, and the
  caller decides whether to log it. A notification failure must not undo or
  veto a write that already happened.
* The payload is ``JournalEntry.to_dict()``, which the journal has already
  redacted. Nothing is added to it, so no credential can reach the subprocess
  through this path. The environment is passed through unchanged.
* The command's own stdout and stderr are captured and discarded, never
  surfaced: a script we do not control could echo anything.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.config.notify import NotifyConfig
    from fantasy_sports.core.journal import JournalEntry

__all__ = ["NOTIFY_TIMEOUT_SECONDS", "NotifyResult", "notify_irreversible", "should_notify"]

NOTIFY_TIMEOUT_SECONDS = 20
"""How long the command gets before it is abandoned and reported as timed out."""

_NOTIFY_STATUSES = frozenset({"executed", "diverged"})

Runner = Callable[..., Any]
"""A callable with :func:`subprocess.run`'s signature; tests inject a fake."""


@dataclass(frozen=True)
class NotifyResult:
    """What happened when we tried to tell the owner.

    ``enabled`` is whether a command is configured. ``attempted`` is whether
    it was run. ``ok`` is ``None`` when nothing was attempted, else whether the
    command exited 0. ``exit_status`` is the command's exit code when it ran to
    completion. ``error`` is a short, credential-free reason when ``ok`` is
    false, or when the config could not be read.
    """

    enabled: bool
    attempted: bool
    ok: bool | None = None
    exit_status: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "attempted": self.attempted,
            "ok": self.ok,
            "exit_status": self.exit_status,
            "error": self.error,
        }


def should_notify(entry: JournalEntry) -> bool:
    """Whether ``entry`` is an irreversible write that took (or may have taken) effect.

    True for an irreversible entry that ended ``executed`` or ``diverged``. A
    ``rejected`` irreversible attempt applied nothing, so there is nothing to
    be told about; a ``pending`` one has not ended yet and will be reported
    when it closes (``doctor`` already flags one that never does).
    """
    return not entry.reversible and entry.status in _NOTIFY_STATUSES


def notify_irreversible(
    entry: JournalEntry,
    *,
    config: NotifyConfig | None = None,
    runner: Runner | None = None,
) -> NotifyResult:
    """Run the configured command with ``entry`` as JSON on stdin. Never raises.

    ``config`` defaults to the ``[notify]`` table of the XDG ``config.toml``.
    ``runner`` defaults to :func:`subprocess.run`; it is resolved inside the
    call, so patching the module does not depend on when a default was bound.
    This function does not check :func:`should_notify`: the caller decides
    which entries to announce, and this one announces what it is given.
    """
    try:
        if config is None:
            from fantasy_sports.config import notify as config_notify

            config = config_notify.load()
    except Exception as exc:  # noqa: BLE001 - a notifier reports; it never raises
        return NotifyResult(
            enabled=False, attempted=False, error=f"config unreadable ({type(exc).__name__})"
        )
    if not config.enabled:
        return NotifyResult(enabled=False, attempted=False)
    command = str(config.irreversible_command)
    try:
        payload = (json.dumps(entry.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")
        if runner is None:
            runner = _run_grouped
        completed = runner(
            command,
            shell=True,  # noqa: S602 - the owner's own configured command line
            input=payload,
            capture_output=True,
            timeout=NOTIFY_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - timeouts, OSError, a bad entry: all reported
        return NotifyResult(enabled=True, attempted=True, ok=False, error=_describe(exc))
    status = int(completed.returncode)
    if status == 0:
        return NotifyResult(enabled=True, attempted=True, ok=True, exit_status=0)
    return NotifyResult(
        enabled=True,
        attempted=True,
        ok=False,
        exit_status=status,
        error=f"notify command exited with status {status}",
    )


def _run_grouped(
    command: str,
    *,
    shell: bool,
    input: bytes,  # noqa: A002 - mirrors subprocess.run
    capture_output: bool,
    timeout: float,
    killpg: Callable[[int, int], None] | None = None,
) -> Any:
    """:func:`subprocess.run`, except a timeout kills the whole process group.

    ``subprocess.run`` kills only the shell it started, so a delivery script
    that spawned a child could fire after we reported a timeout. The command
    gets its own session, and on timeout the group is sent SIGKILL and reaped
    before :class:`subprocess.TimeoutExpired` is re-raised. Where there are no
    process groups (not POSIX) it falls back to plain ``subprocess.run``.
    """
    import contextlib
    import os
    import signal
    import subprocess

    if not hasattr(os, "killpg"):  # pragma: no cover - non-POSIX
        return subprocess.run(
            command, shell=shell, input=input, capture_output=capture_output, timeout=timeout
        )
    kill = killpg or os.killpg
    with subprocess.Popen(
        command,
        shell=shell,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        start_new_session=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):  # it may have just exited
                kill(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def _describe(exc: BaseException) -> str:
    """A reason that names the failure class and never echoes the command or its output."""
    import subprocess

    if isinstance(exc, subprocess.TimeoutExpired):
        return f"notify command timed out after {NOTIFY_TIMEOUT_SECONDS}s"
    return f"notify command could not run ({type(exc).__name__})"
