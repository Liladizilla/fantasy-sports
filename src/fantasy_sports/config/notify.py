"""The ``[notify]`` table of ``config.toml``: where an irreversible write is announced.

::

    [notify]
    irreversible_command = "~/bin/tell-john-about-a-drop"

One key. The command is a shell string; the notify hook
(:mod:`fantasy_sports.reports.notify`) runs it once per irreversible write and
feeds the journal entry to its standard input as JSON. The package does not
know what the command does: delivery to a vault, a task tracker, or a phone is
the owner's script, outside this repo (jwulff/fantasy-sports#113).

An absent table or an absent key means notifications are off, which is the
default. Unknown keys inside ``[notify]`` are tolerated, for the same reason
``config.toml`` tolerates unknown top-level tables: it is a shared namespace
(``docs/memory/config-toml-is-a-shared-namespace.md``), and a newer version's
knob must not break an older one. :func:`fantasy_sports.config.leagues.save`
owns only ``default`` and ``[leagues]``, so this table survives a save.

This module only *reads*. It lives in ``config/`` so ``doctor`` can report on it
without importing ``reports/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fantasy_sports.config import paths
from fantasy_sports.config.document import read_document
from fantasy_sports.core.errors import ConfigInvalidError

__all__ = ["NotifyConfig", "load"]


@dataclass(frozen=True)
class NotifyConfig:
    """The parsed ``[notify]`` table."""

    irreversible_command: str | None = None

    @property
    def enabled(self) -> bool:
        """Whether a non-blank command is configured."""
        return bool(self.irreversible_command and self.irreversible_command.strip())


def load(path: Path | None = None) -> NotifyConfig:
    """Parse ``[notify]`` from ``path`` (default: the XDG config file).

    A missing file, table, or key is the disabled default. A ``[notify]`` that
    is not a table, or an ``irreversible_command`` that is not a string, is
    ``CONFIG_INVALID``: silently ignoring it would leave John believing he
    would be told.
    """
    path = path or paths.config_file()
    table = read_document(path).get("notify")
    if table is None:
        return NotifyConfig()
    if not isinstance(table, dict):
        raise ConfigInvalidError(f"{path}: 'notify' must be a table")
    command = table.get("irreversible_command")
    if command is None:
        return NotifyConfig()
    if not isinstance(command, str):
        raise ConfigInvalidError(
            f"{path}: [notify] 'irreversible_command' must be a string, got {command!r}"
        )
    return NotifyConfig(irreversible_command=command)
