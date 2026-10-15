"""The mutation journal: what the agent did, acting as whom, and what it cost.

Why this exists
---------------

The journal is the control that replaced dry-run defaults and interactive
confirmation for reversible writes (jwulff/fantasy-sports#16, part of #105). It
only does that job if it can answer, after the fact, three questions the
provider's own ledger answers badly or not at all:

* **What changed?** ``prior_state`` is recorded *before* the request is sent,
  ``applied_state`` after, so a reversal can be built from the file alone.
* **Acting as whom?** ``member_id`` is what ESPN's *response* echoed, never the
  one we sent, and ``role`` is the per-league authority read from ``mNav`` at
  write time (``docs/research/05-espn-write-surface.md`` §7).
* **What did it cost?** ``cost`` carries ``bid_amount`` and
  ``displaced_player_id``, and ``provider_transaction_id`` plus ``proposed_at``
  are ESPN's own ``id`` and ``proposedDate`` so the two ledgers can be joined
  (research §6.2).

Two-phase, because a crash is a fact the journal must show
-----------------------------------------------------------

:meth:`Journal.begin` writes the entry, with its prior state and the request,
**before** the POST goes out. :meth:`Journal.complete`, :meth:`Journal.reject`,
or :meth:`Journal.diverge` close it afterwards. A process that dies in between
leaves a row whose status is still ``pending``; :meth:`Journal.pending` returns
it and ``doctor`` reports it. A row that says "I was about to do this and never
said how it ended" is exactly the evidence a reviewer needs, and it exists only
because the first write happened first.

Loud writing, loud corruption, soft absence
-------------------------------------------

* A journal that **does not exist yet** reads as empty. Nothing has been done.
* A journal that **cannot be written** makes :meth:`Journal.begin` raise
  :class:`~fantasy_sports.core.errors.ConfigInvalidError`. A write that cannot
  be journaled must not proceed; this is the opposite of the response cache,
  which degrades to a live fetch.
* A journal that **exists but cannot be read** raises the same error from
  every read. Answering "no entries" about a file that merely failed to open
  would turn an audit trail into a falsehood. ``doctor`` is the one caller
  that turns this into a finding instead (:meth:`Journal.health`).

What never reaches the file
---------------------------

Every string passes through the registered-secret redaction and the credential
*pattern* scrub, and every mapping is walked, keys included
(``docs/memory/credential-leak-channels.md``). Brace-wrapped SWID GUIDs become
per-GUID pseudonyms under a random salt that lives in this same file, as the
response cache does (``docs/memory/swid-pseudonyms.md``), so two entries by the
same member still agree with each other and a GUID pasted into a request body
does not survive. That includes ``member_id``: ESPN's ``memberId`` *is* the
acting account's SWID, so the attribution is stored as that pseudonym. It is
stable within one journal, which is what "was it the same member each time"
needs; ESPN's transaction ``id`` is the join to the provider's ledger, where
the member has a name.

The file is created ``0600`` inside a ``0700`` directory. Redaction removes
credential shapes, but the journal still holds other people's rosters.

Standard library only, and ``sqlite3`` is imported on first use so importing
this module costs nothing (ADR-0008).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from fantasy_sports.core.errors import ConfigInvalidError, FantasySportsError
from fantasy_sports.core.redaction import (
    redact,
    scrub_credential_patterns,
    swid_pseudonym,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import sqlite3

__all__ = [
    "DEFAULT_JOURNAL_FILENAME",
    "DIR_MODE",
    "FILE_MODE",
    "KINDS",
    "ROLES",
    "STATUSES",
    "Journal",
    "JournalEntry",
    "JournalHealth",
    "journal_path",
    "new_entry_id",
]

DEFAULT_JOURNAL_FILENAME: Final[str] = "journal.sqlite"

DIR_MODE: Final[int] = 0o700
FILE_MODE: Final[int] = 0o600

KINDS: Final[tuple[str, ...]] = ("lineup", "add", "drop", "claim", "cancel", "reverse")
ROLES: Final[tuple[str, ...]] = ("creator", "manager", "member")
STATUSES: Final[tuple[str, ...]] = ("pending", "executed", "rejected", "diverged")

_SCHEMA_VERSION: Final[int] = 1
_SALT_KEY: Final[str] = "swid_salt"

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS journal (
    id                      TEXT PRIMARY KEY,
    recorded_at             TEXT NOT NULL,
    provider                TEXT NOT NULL,
    league_id               TEXT NOT NULL,
    season                  INTEGER NOT NULL,
    scoring_period          INTEGER,
    command                 TEXT NOT NULL,
    member_id               TEXT,
    role                    TEXT,
    team_id                 TEXT,
    kind                    TEXT NOT NULL,
    reversible              INTEGER NOT NULL,
    prior_state             TEXT NOT NULL,
    applied_state           TEXT,
    request                 TEXT NOT NULL,
    provider_transaction_id TEXT,
    proposed_at             TEXT,
    cost                    TEXT NOT NULL,
    status                  TEXT NOT NULL,
    reverses                TEXT,
    error                   TEXT
);
CREATE INDEX IF NOT EXISTS journal_league ON journal (league_id);
CREATE INDEX IF NOT EXISTS journal_status ON journal (status);
CREATE TABLE IF NOT EXISTS journal_meta (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "recorded_at",
    "provider",
    "league_id",
    "season",
    "scoring_period",
    "command",
    "member_id",
    "role",
    "team_id",
    "kind",
    "reversible",
    "prior_state",
    "applied_state",
    "request",
    "provider_transaction_id",
    "proposed_at",
    "cost",
    "status",
    "reverses",
    "error",
)

_BARE_GUID_RE: Final = re.compile(
    r"\{?[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}?"
)


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    """UTC, millisecond precision, ``Z`` suffix. Naive datetimes are refused.

    The same rule as ``output.envelope``: a naive timestamp is silently wrong by
    the host's UTC offset, so it is a programming error rather than a value.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            "journal timestamps must be timezone-aware; got a naive datetime. "
            "Use fantasy_sports.output.envelope.from_epoch_millis for ESPN dates."
        )
    utc = value.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _ceil_to_millisecond(value: datetime) -> datetime:
    """``value`` rounded *up* to the millisecond the file stores.

    :func:`_iso` truncates, which is right for a stored timestamp but wrong for
    a ``>=`` cutoff: a cutoff of ``.000999Z`` would truncate to ``.000Z`` and
    admit an entry that predates it.
    """
    extra = value.microsecond % 1000
    return value if extra == 0 else value + timedelta(microseconds=1000 - extra)


def _parse(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def new_entry_id() -> str:
    """A fresh entry id: a random UUID4 in its canonical string form."""
    return str(uuid.uuid4())


def _default_cost() -> dict[str, Any]:
    return {"bid_amount": None, "displaced_player_id": None}


# --------------------------------------------------------------------------- #
# The entry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class JournalEntry:
    """One mutation, from the intent to write it to how it ended.

    Fields are the ones jwulff/fantasy-sports#16 lists, in that order.
    ``prior_state``, ``request``, and ``reversible`` have no default on purpose:
    an entry that forgot any of the three is a bug, and a ``TypeError`` at
    construction is cheaper than discovering it when a reversal is needed.
    """

    id: str = field(default_factory=new_entry_id)
    recorded_at: datetime = field(default_factory=_now)
    provider: str
    league_id: str
    season: int
    scoring_period: int | None = None
    command: Sequence[str]
    """The argv as invoked, redacted. Stored as a tuple."""
    member_id: str | None = None
    """The acting member as ESPN **echoed** it, stored as its journal pseudonym.
    ``None`` until the write completes."""
    role: str | None = None
    """``creator``, ``manager``, ``member``, or ``None`` when ``mNav`` was unread."""
    team_id: str | None = None
    kind: str
    reversible: bool
    prior_state: Mapping[str, Any]
    applied_state: Mapping[str, Any] | None = None
    request: Mapping[str, Any]
    provider_transaction_id: str | None = None
    """ESPN's transaction ``id``. The join key to the provider's own ledger."""
    proposed_at: datetime | None = None
    """ESPN's ``proposedDate`` (epoch ms), as a UTC datetime."""
    cost: Mapping[str, Any] = field(default_factory=_default_cost)
    status: str = "pending"
    reverses: str | None = None
    """For a ``reverse`` entry, the id of the entry it undoes."""
    error: Mapping[str, Any] | None = None
    """``FantasySportsError.to_dict()`` for a rejected or diverged write."""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}; got {self.kind!r}")
        if self.role is not None and self.role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)} or None; got {self.role!r}")
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}; got {self.status!r}")
        if isinstance(self.command, str):
            raise ValueError("command is an argv list, not a string")
        # Normalised through object.__setattr__ because the dataclass is frozen:
        # an immutable argv, and a cost that always carries both keys.
        object.__setattr__(self, "command", tuple(str(part) for part in self.command))
        cost = {**_default_cost(), **dict(self.cost)}
        object.__setattr__(self, "cost", cost)
        # Truncated to the millisecond the file stores (and ESPN's epoch-ms
        # dates carry), so an entry equals its own round trip; a naive datetime
        # is refused here, at construction.
        object.__setattr__(self, "recorded_at", _parse(_iso(self.recorded_at)))
        if self.proposed_at is not None:
            object.__setattr__(self, "proposed_at", _parse(_iso(self.proposed_at)))

    def to_dict(self) -> dict[str, Any]:
        """JSON-plain values, ISO-8601 UTC timestamps, key order as in the issue."""
        return {
            "id": self.id,
            "recorded_at": _iso(self.recorded_at),
            "provider": self.provider,
            "league_id": self.league_id,
            "season": self.season,
            "scoring_period": self.scoring_period,
            "command": list(self.command),
            "member_id": self.member_id,
            "role": self.role,
            "team_id": self.team_id,
            "kind": self.kind,
            "reversible": self.reversible,
            "prior_state": _plain(self.prior_state),
            "applied_state": None if self.applied_state is None else _plain(self.applied_state),
            "request": _plain(self.request),
            "provider_transaction_id": self.provider_transaction_id,
            "proposed_at": None if self.proposed_at is None else _iso(self.proposed_at),
            "cost": _plain(self.cost),
            "status": self.status,
            "reverses": self.reverses,
            "error": None if self.error is None else _plain(self.error),
        }


def _plain(value: Any) -> Any:
    """A JSON round trip: mappings and sequences become dicts and lists."""
    return json.loads(json.dumps(value))


# --------------------------------------------------------------------------- #
# Where it lives
# --------------------------------------------------------------------------- #


def journal_path() -> Path:
    """``$XDG_STATE_HOME/fantasy-sports/journal.sqlite``.

    Resolved on every call, never at import, so a test that points the XDG
    variables at a temp directory is honoured (``docs/memory/
    default-args-defeat-monkeypatch.md``).
    """
    from fantasy_sports.config.paths import state_home

    return state_home() / DEFAULT_JOURNAL_FILENAME


@dataclass(frozen=True)
class JournalHealth:
    """What ``doctor`` reports. Built without raising, whatever state the file is in."""

    path: Path
    exists: bool
    entries: int | None
    """``None`` when the file exists but could not be read."""
    pending: int | None
    pending_ids: tuple[str, ...]
    file_mode: int | None
    dir_mode: int | None
    permissions_ok: bool
    """``True`` when the file is ``0600`` and its directory ``0700`` — or when
    there is no file yet, or the platform has no such modes."""
    error: str | None = None
    """Why the file could not be read, or ``None``."""


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #


class Journal:
    """The SQLite-backed journal. Construct with :meth:`open`.

    Connections are opened on first use, so constructing a ``Journal`` neither
    creates the directory nor touches the disk. Reads of a file that does not
    exist return empty; the first :meth:`begin` creates it.
    """

    def __init__(self, path: Path | str | None = None, *, swid_salt: str | None = None) -> None:
        self.path = Path(path) if path is not None else journal_path()
        self._rw: sqlite3.Connection | None = None
        self._ro: sqlite3.Connection | None = None
        self._salt: str | None = None
        self._seed_salt = swid_salt

    @classmethod
    def open(cls, path: Path | str | None = None, *, swid_salt: str | None = None) -> Journal:
        """A journal at ``path``, or at the XDG state directory by default.

        ``swid_salt`` is for reproducible fixtures only (the doc samples): it
        becomes the file's pseudonym salt when the file has none yet, and is
        ignored when it already has one. Production callers never pass it, and
        the file's own random salt is what keeps pseudonyms unconfirmable.
        """
        return cls(path, swid_salt=swid_salt)

    def close(self) -> None:
        for connection in (self._rw, self._ro):
            if connection is not None:
                connection.close()
        self._rw = self._ro = None

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- writing ---------------------------------------------------------- #

    def begin(self, entry: JournalEntry) -> JournalEntry:
        """Record the intent, with prior state and request, before anything is sent.

        The entry is stored with status ``pending`` whatever it arrived with, and
        any result fields it carried (member id, applied state, transaction id,
        error) are dropped: a row that has not been sent has no result, and the
        acting member comes only from the response, via :meth:`complete`. Returns the entry as
        stored — redacted — so the caller holds what the file holds.

        :raises ConfigInvalidError: if the journal cannot be written. The caller
            must not proceed with the write.
        """
        connection = self._writer()
        fresh = replace(
            entry,
            status="pending",
            member_id=None,
            applied_state=None,
            provider_transaction_id=None,
            proposed_at=None,
            error=None,
        )
        stored = self._cleaned(fresh)
        row = self._to_row(stored)
        try:
            with connection:
                connection.execute(
                    f"INSERT INTO journal ({', '.join(_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                    row,
                )
        except Exception as exc:  # noqa: BLE001 - sqlite3 is imported lazily
            raise self._write_failure("record the intended change", exc) from exc
        return stored

    def complete(
        self,
        entry_id: str,
        *,
        member_id: str | None,
        provider_transaction_id: str | None,
        proposed_at: datetime | None,
        applied_state: Mapping[str, Any] | None,
        status: str = "executed",
        error: FantasySportsError | None = None,
    ) -> JournalEntry:
        """Close a ``pending`` entry with what ESPN's response carried.

        ``member_id`` is the response's ``memberId``, not the one sent.
        ``proposed_at`` is ESPN's ``proposedDate``. ``error`` is the
        ``WRITE_DIVERGED`` a read-back raised, stored with a ``diverged``
        entry so the row (and the owner's notification) says what disagreed;
        an ``executed`` entry has none. Raises if the entry is not
        ``pending``: a journal row closes once.
        """
        if status not in ("executed", "diverged"):
            raise ValueError("complete() closes an entry as 'executed' or 'diverged'")
        if error is not None and status != "diverged":
            raise ValueError("only a 'diverged' entry carries an error")
        return self._close(
            entry_id,
            status=status,
            member_id=member_id,
            provider_transaction_id=provider_transaction_id,
            proposed_at=proposed_at,
            applied_state=applied_state,
            error=None if error is None else error.to_dict(),
        )

    def reject(self, entry_id: str, error: FantasySportsError) -> JournalEntry:
        """Close a ``pending`` entry as ``rejected``: ESPN refused, nothing applied."""
        return self._close(entry_id, status="rejected", error=error.to_dict())

    def diverge(
        self,
        entry_id: str,
        applied_state: Mapping[str, Any] | None,
        error: FantasySportsError | None,
    ) -> JournalEntry:
        """Close a ``pending`` entry as ``diverged``.

        For a write whose read-back disagrees with what was requested — ESPN said
        yes and the roster says otherwise, or a timeout left the outcome unknown
        (research §8.1). ``applied_state`` is what was actually observed.
        """
        return self._close(
            entry_id,
            status="diverged",
            applied_state=applied_state,
            error=None if error is None else error.to_dict(),
        )

    def _close(self, entry_id: str, *, status: str, **fields: Any) -> JournalEntry:
        connection = self._writer()
        assignments: dict[str, Any] = {"status": status}
        if "member_id" in fields:
            assignments["member_id"] = self._clean_member(fields["member_id"])
        if "provider_transaction_id" in fields:
            assignments["provider_transaction_id"] = self._clean(fields["provider_transaction_id"])
        if "proposed_at" in fields:
            moment = fields["proposed_at"]
            assignments["proposed_at"] = None if moment is None else _iso(moment)
        for name in ("applied_state", "error"):
            if name in fields:
                value = fields[name]
                assignments[name] = None if value is None else json.dumps(self._clean(value))
        names = list(assignments)
        try:
            with connection:
                cursor = connection.execute(
                    f"UPDATE journal SET {', '.join(f'{n} = ?' for n in names)} "
                    "WHERE id = ? AND status = 'pending'",
                    [assignments[n] for n in names] + [entry_id],
                )
                closed = cursor.rowcount
        except Exception as exc:  # noqa: BLE001 - sqlite3 is imported lazily
            raise self._write_failure("close the journal entry", exc) from exc
        if closed != 1:
            existing = self.get(entry_id)
            if existing is None:
                raise LookupError(f"no journal entry {entry_id!r}")
            raise ValueError(
                f"journal entry {entry_id!r} is {existing.status!r}, not 'pending'; "
                "an entry closes once."
            )
        closed_entry = self.get(entry_id)
        assert closed_entry is not None
        return closed_entry

    # --- reading ---------------------------------------------------------- #

    def get(self, entry_id: str) -> JournalEntry | None:
        """One entry by exact id, or ``None``."""
        rows = self._select("WHERE id = ?", [entry_id])
        return rows[0] if rows else None

    def list(
        self,
        *,
        league_id: str | None = None,
        limit: int = 25,
        irreversible_only: bool = False,
        since: datetime | None = None,
    ) -> list[JournalEntry]:
        """Newest first. ``irreversible_only`` keeps entries with ``reversible`` false.

        ``since`` keeps entries recorded at or after that moment. It must be
        timezone-aware (a naive datetime is a ``ValueError``, as everywhere in
        this module); the comparison is on the stored UTC millisecond text.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        clauses: list[str] = []
        params: list[Any] = []
        if league_id is not None:
            clauses.append("league_id = ?")
            params.append(league_id)
        if irreversible_only:
            clauses.append("reversible = 0")
        if since is not None:
            clauses.append("recorded_at >= ?")
            params.append(_iso(_ceil_to_millisecond(since)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._select(where, params, limit=limit)

    def count(self, *, status: str | None = None) -> int:
        """How many entries there are, optionally in one status."""
        sql, params = "SELECT COUNT(*) FROM journal", []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        rows = self._query(sql, params)
        return int(rows[0][0]) if rows else 0

    def pending(self) -> list[JournalEntry]:
        """Entries begun and never closed, newest first: crashes and in-flight writes."""
        return self._select("WHERE status = 'pending'", [])

    def nearest_ids(self, target: str, *, limit: int = 3) -> list[str]:
        """Ids closest to ``target``: prefix matches first, then by edit similarity."""
        import difflib

        ids = [row[0] for row in self._query("SELECT id FROM journal ORDER BY rowid DESC", [])]
        prefixed = [i for i in ids if target and i.startswith(target)]
        close = difflib.get_close_matches(target, ids, n=limit, cutoff=0.0)
        ordered = list(dict.fromkeys([*prefixed, *close]))
        return ordered[:limit]

    def health(self) -> JournalHealth:
        """A report for ``doctor``. Never raises, whatever the file's state."""
        path = self.path
        exists = path.exists()
        file_mode = _mode(path) if exists else None
        dir_mode = _mode(path.parent) if path.parent.exists() else None
        permissions_ok = _permissions_ok(file_mode, dir_mode, exists=exists)
        entries: int | None = 0
        pending: int | None = 0
        pending_ids: tuple[str, ...] = ()
        error: str | None = None
        if exists:
            try:
                entries = self.count()
                waiting = self.pending()
                pending = len(waiting)
                pending_ids = tuple(item.id for item in waiting[:10])
            except ConfigInvalidError as exc:
                entries = pending = None
                error = exc.message
        return JournalHealth(
            path=path,
            exists=exists,
            entries=entries,
            pending=pending,
            pending_ids=pending_ids,
            file_mode=file_mode,
            dir_mode=dir_mode,
            permissions_ok=permissions_ok,
            error=error,
        )

    # --- plumbing --------------------------------------------------------- #

    def _writer(self) -> sqlite3.Connection:
        """The read-write connection, creating directory, file, and schema."""
        if self._rw is not None:
            return self._rw
        import sqlite3

        try:
            _ensure_private_directory(self.path.parent)
            existed = self.path.exists()
            descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, FILE_MODE)
            os.close(descriptor)
            _chmod(self.path, FILE_MODE)
            connection = sqlite3.connect(self.path, timeout=5.0)
            try:
                if existed and not _is_uninitialised(self.path):
                    # A populated file that has lost its table is an audit trail
                    # someone damaged. Recreating the schema would turn that into
                    # a quiet, empty, healthy-looking journal.
                    found = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'journal'"
                    ).fetchone()
                    if found is None:
                        raise sqlite3.DatabaseError("the file has no journal table")
                connection.executescript(_SCHEMA)
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                self._salt = self._load_or_mint_salt(connection)
            except BaseException:
                connection.close()
                raise
        except (OSError, sqlite3.Error) as exc:
            raise self._write_failure("open the journal", exc) from exc
        self._rw = connection
        return connection

    def _reader(self) -> sqlite3.Connection | None:
        """A connection to read through, or ``None`` when there is no file yet."""
        if self._rw is not None:
            return self._rw
        if self._ro is not None:
            return self._ro
        if not self.path.exists():
            return None
        import sqlite3

        try:
            self._ro = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=5.0)
        except sqlite3.Error as exc:
            raise self._read_failure(exc) from exc
        return self._ro

    def _query(self, sql: str, params: Sequence[Any]) -> list[Any]:
        import sqlite3

        connection = self._reader()
        if connection is None:
            return []
        try:
            return connection.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc) and _is_uninitialised(self.path):
                return []  # a zero-byte file holds no entries and nothing to lose
            raise self._read_failure(exc) from exc
        except sqlite3.Error as exc:
            raise self._read_failure(exc) from exc

    def _select(self, where: str, params: Sequence[Any], *, limit: int | None = None) -> list[Any]:
        sql = f"SELECT {', '.join(_COLUMNS)} FROM journal {where} ORDER BY rowid DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [self._from_row(row) for row in self._query(sql, params)]

    def _load_or_mint_salt(self, connection: sqlite3.Connection) -> str:
        from fantasy_sports.core.redaction import new_swid_salt

        row = connection.execute(
            "SELECT value FROM journal_meta WHERE name = ?", (_SALT_KEY,)
        ).fetchone()
        if row is not None:
            return str(row[0])
        with connection:
            connection.execute(
                "INSERT INTO journal_meta (name, value) VALUES (?, ?) ON CONFLICT(name) DO NOTHING",
                (_SALT_KEY, self._seed_salt or new_swid_salt()),
            )
        row = connection.execute(
            "SELECT value FROM journal_meta WHERE name = ?", (_SALT_KEY,)
        ).fetchone()
        return str(row[0])

    def _write_failure(self, doing: str, exc: BaseException) -> ConfigInvalidError:
        return ConfigInvalidError(
            f"Cannot {doing}: the mutation journal at {self.path} is not writable "
            f"({type(exc).__name__}). Nothing was sent to the provider.",
            kind="config",
            remediation=(
                f"Make {self.path.parent} writable by you (mode 0700) and {self.path} "
                "a SQLite file you own, or move a corrupt file aside, then retry. "
                "A write that cannot be journaled is not attempted."
            ),
            details={"path": str(self.path)},
        )

    def _read_failure(self, exc: BaseException) -> ConfigInvalidError:
        return ConfigInvalidError(
            f"The mutation journal at {self.path} exists but cannot be read "
            f"({type(exc).__name__}). Refusing to report it as empty.",
            kind="config",
            remediation=(
                f"Check the permissions on {self.path}, or move a corrupt file aside "
                "(keep it: it is the audit trail) and run `fantasy-sports doctor`."
            ),
            details={"path": str(self.path)},
        )

    # --- redaction -------------------------------------------------------- #

    def _clean(self, value: Any) -> Any:
        """Redact every string and key in ``value``, recursively.

        Pattern scrub first (GUIDs become pseudonyms, ``espn_s2=`` values vanish),
        then the registered-secret redaction, the order ``scrub_body`` uses.
        """
        salt = self._salt
        assert salt is not None, "_clean called before the journal was opened for writing"
        if isinstance(value, str):
            return redact(scrub_credential_patterns(value, swid_salt=salt))
        if isinstance(value, Mapping):
            return {self._clean(str(key)): self._clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._clean(item) for item in value]
        return value

    def _clean_member(self, member_id: str | None) -> str | None:
        """The acting member, as the journal's pseudonym for that GUID."""
        if member_id is None:
            return None
        if _BARE_GUID_RE.fullmatch(member_id.strip()):
            salt = self._salt
            assert salt is not None
            return swid_pseudonym(member_id, salt=salt)
        return self._clean(member_id)

    def _cleaned(self, entry: JournalEntry) -> JournalEntry:
        return replace(
            entry,
            provider=self._clean(entry.provider),
            league_id=self._clean(entry.league_id),
            command=tuple(self._clean(part) for part in entry.command),
            member_id=self._clean_member(entry.member_id),
            team_id=self._clean(entry.team_id),
            prior_state=self._clean(entry.prior_state),
            applied_state=self._clean(entry.applied_state),
            request=self._clean(entry.request),
            provider_transaction_id=self._clean(entry.provider_transaction_id),
            cost=self._clean(entry.cost),
            reverses=self._clean(entry.reverses),
            error=self._clean(entry.error),
        )

    # --- rows ------------------------------------------------------------- #

    @staticmethod
    def _to_row(entry: JournalEntry) -> list[Any]:
        data = entry.to_dict()
        row: list[Any] = []
        for column in _COLUMNS:
            value = data[column]
            if column in ("command", "prior_state", "request", "cost") or (
                column in ("applied_state", "error") and value is not None
            ):
                value = json.dumps(value)
            elif column == "reversible":
                value = int(value)
            row.append(value)
        return row

    @staticmethod
    def _from_row(row: Iterable[Any]) -> JournalEntry:
        data = dict(zip(_COLUMNS, row, strict=True))
        for column in ("command", "prior_state", "request", "cost", "applied_state", "error"):
            if data[column] is not None:
                data[column] = json.loads(data[column])
        recorded = _parse(data["recorded_at"])
        assert recorded is not None
        return JournalEntry(
            id=data["id"],
            recorded_at=recorded,
            provider=data["provider"],
            league_id=data["league_id"],
            season=data["season"],
            scoring_period=data["scoring_period"],
            command=data["command"],
            member_id=data["member_id"],
            role=data["role"],
            team_id=data["team_id"],
            kind=data["kind"],
            reversible=bool(data["reversible"]),
            prior_state=data["prior_state"],
            applied_state=data["applied_state"],
            request=data["request"],
            provider_transaction_id=data["provider_transaction_id"],
            proposed_at=_parse(data["proposed_at"]),
            cost=data["cost"],
            status=data["status"],
            reverses=data["reverses"],
            error=data["error"],
        )


# --------------------------------------------------------------------------- #
# Filesystem helpers
# --------------------------------------------------------------------------- #


def _ensure_private_directory(directory: Path) -> None:
    """Create ``directory`` (and parents) and make the leaf ``0700``.

    Only the leaf is tightened: ``~/.local/state`` belongs to the user and to
    other programs, ``fantasy-sports/`` belongs to us.
    """
    directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    _chmod(directory, DIR_MODE)


def _chmod(path: Path, mode: int) -> None:
    if os.name == "posix":
        os.chmod(path, mode)


def _is_uninitialised(path: Path) -> bool:
    """A zero-byte file: created, never given a schema. The only empty file."""
    try:
        return path.stat().st_size == 0
    except OSError:
        return False


def _mode(path: Path) -> int | None:
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return None


def _permissions_ok(file_mode: int | None, dir_mode: int | None, *, exists: bool) -> bool:
    if os.name != "posix" or not exists:
        return True
    return file_mode == FILE_MODE and dir_mode == DIR_MODE
