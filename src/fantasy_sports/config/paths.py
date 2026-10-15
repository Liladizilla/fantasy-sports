"""XDG base directories, forced on every platform (KTD6, ARCHITECTURE §7).

``platformdirs`` returns ``~/Library/Application Support/fantasy-sports`` on
macOS. The architecture commits to ``~/.config/fantasy-sports`` everywhere —
which is what ripgrep, gh, docker, and uv all do — so the dependency was
dropped in favour of these few lines.

Two rules from the XDG Base Directory spec are honoured here and are the
reason this is not a one-line ``os.environ.get``:

* an unset **or empty** variable falls back to the default;
* a **relative** path in one of these variables is invalid and is ignored.

Nothing in this module creates a directory. Reading configuration must never
write to the filesystem; the one place that does is :func:`leagues.save`.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "fantasy-sports"
"""The directory name appended to every base directory."""


def _base(var: str, *default: str) -> Path:
    """The app directory under ``$var``, or under ``~/<default>`` if unusable."""
    value = os.environ.get(var, "").strip()
    root = Path(value).expanduser() if value else Path.home().joinpath(*default)
    if not root.is_absolute():
        root = Path.home().joinpath(*default)
    return root / APP_NAME


def config_home() -> Path:
    """Where ``config.toml`` lives — ``$XDG_CONFIG_HOME`` or ``~/.config``."""
    return _base("XDG_CONFIG_HOME", ".config")


def cache_home() -> Path:
    """Where the SQLite response cache lives (ARCHITECTURE §8)."""
    return _base("XDG_CACHE_HOME", ".cache")


def data_home() -> Path:
    """``$XDG_DATA_HOME`` — reserved; nothing is stored here today."""
    return _base("XDG_DATA_HOME", ".local", "share")


def state_home() -> Path:
    """Where the mutation journal lives — ``$XDG_STATE_HOME`` or ``~/.local/state``.

    State, not data: the journal is a host-local record of what this machine
    did, which the XDG spec files under ``STATE`` (actions history), and it is
    what jwulff/fantasy-sports#16 specifies.
    """
    return _base("XDG_STATE_HOME", ".local", "state")


def config_file() -> Path:
    """The multi-league config file."""
    return config_home() / "config.toml"
