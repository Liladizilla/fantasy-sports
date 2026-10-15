"""``fantasy-sports doctor`` — every health check, one verdict (ADR-0005 §11.4).

What a human runs when something feels wrong and what an agent runs *before*
it starts guessing: one call that answers "config parses, credentials are
present and not stale, the cache is reachable, and here is what upstream looks
like" without touching ESPN unless asked to.

**Nine checks, six of them free.** ``python``, ``config``, ``credentials``,
``cache``, ``journal``, and ``notify`` never leave the machine. ``version`` and
``provider_status`` make the one network call ARCHITECTURE §11.3 calls "forced,
ignores cache" — a 2-second, unauthenticated ``GET`` of the public health manifest
(:mod:`fantasy_sports.health.client`), skipped entirely when the user opted
out. ``leagues_reachable`` is the only check that can touch ESPN, and only
when ``--live`` is passed: an agent-native ``doctor`` that silently spent a
credentialed request every time it ran would be the opposite of what "before
guessing" means.

Every check reports its own :class:`CheckStatus`; nothing here raises on a bad
result; the doctor's own job is uniformly ``ok``. A configuration problem, a
stale cookie, an unreachable league — those are *findings*, not command
failures, exactly like ``auth status`` treats missing credentials as data
rather than an error.

Nothing here imports typer (ADR-0003). ``requests``, ``keyring``, and
``espn_api`` reach this module only through calls other layers already keep
lazy; nothing is imported at module scope beyond the standard library.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from fantasy_sports.commands.context import require_shape

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fantasy_sports.output.envelope import Envelope

__all__ = ["CheckStatus", "DoctorCheck", "doctor"]

_DEPENDENCIES: tuple[str, ...] = ("espn-api", "typer", "requests", "keyring", "tomli-w")
"""The ADR-0008 budget's five direct runtime dependencies, by PyPI name."""


class CheckStatus(StrEnum):
    """One check's verdict, ordered worst-to-best by :func:`_worst_of`."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIPPED = "skipped"
    """Not run — opted out, or a network check without ``--live``. Never counts
    toward an overall ``fail``/``warn``: a check that did not run found nothing wrong."""


_SEVERITY: dict[CheckStatus, int] = {
    CheckStatus.OK: 0,
    CheckStatus.SKIPPED: 0,
    CheckStatus.WARN: 1,
    CheckStatus.FAIL: 2,
}


@dataclass(frozen=True)
class DoctorCheck:
    """One line of the report."""

    name: str
    status: CheckStatus
    summary: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "summary": self.summary,
            "details": dict(self.details),
        }


def doctor(*, live: bool = False) -> Envelope:
    """Run every check and return one report.

    ``data.status`` is the worst individual check's status; ``data.ok`` is the
    same thing as a boolean, for a caller that only wants a gate. ``data
    .checks`` is every check, in the order a human would want to read them —
    cheapest and most fixable first.

    ``live`` also attempts one read against each configured league's provider.
    It is the one thing here that can fail on a correctly-configured setup
    (ESPN is down, a league was renamed) rather than on *this* machine, which
    is why it defaults off.
    """
    from fantasy_sports import __version__
    from fantasy_sports.output.envelope import Envelope

    checks = [
        _python_check(),
        _config_check(),
        _credentials_check(),
        _cache_check(),
        _journal_check(),
        _notify_check(),
        *_health_checks(current_version=__version__),
        _leagues_reachable_check(live=live),
    ]
    worst = _worst_of(checks)
    data: dict[str, Any] = {
        "status": worst.value,
        "ok": worst is not CheckStatus.FAIL and worst is not CheckStatus.WARN,
        "checks": [check.to_dict() for check in checks],
    }
    require_shape(data, command="doctor")
    return Envelope.success(data=data)


def _worst_of(checks: list[DoctorCheck]) -> CheckStatus:
    return max((check.status for check in checks), key=lambda status: _SEVERITY[status])


# --------------------------------------------------------------------------- #
# Local checks — no network, ever
# --------------------------------------------------------------------------- #


def _python_check() -> DoctorCheck:
    """Interpreter, our own version, and the ADR-0008 dependency set."""
    import sys
    from importlib import metadata

    from fantasy_sports import __version__

    versions: dict[str, str | None] = {}
    missing: list[str] = []
    for name in _DEPENDENCIES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
            missing.append(name)

    status = CheckStatus.WARN if missing else CheckStatus.OK
    summary = (
        f"fantasy-sports {__version__} on Python {sys.version.split()[0]}."
        if not missing
        else f"Installed but missing metadata for: {', '.join(missing)}."
    )
    return DoctorCheck(
        name="python",
        status=status,
        summary=summary,
        details={
            "fantasy_sports_version": __version__,
            "python_version": sys.version.split()[0],
            "platform": sys.platform,
            "dependencies": versions,
        },
    )


def _config_check() -> DoctorCheck:
    """Does ``config.toml`` parse, and is at least one league configured?"""
    from fantasy_sports.config import leagues as config_leagues
    from fantasy_sports.config import paths
    from fantasy_sports.core.errors import ConfigInvalidError

    path = paths.config_file()
    try:
        config = config_leagues.load()
    except ConfigInvalidError as exc:
        return DoctorCheck(
            name="config",
            status=CheckStatus.FAIL,
            summary=exc.message,
            details={"path": str(path)},
        )

    names = config.names()
    if not names:
        return DoctorCheck(
            name="config",
            status=CheckStatus.WARN,
            summary=f"{path} parses, but no leagues are configured.",
            details={"path": str(path), "leagues": [], "default": config.default},
        )
    return DoctorCheck(
        name="config",
        status=CheckStatus.OK,
        summary=f"{len(names)} league(s) configured in {path}.",
        details={"path": str(path), "leagues": names, "default": config.default},
    )


def _credentials_check() -> DoctorCheck:
    """Presence and staleness, via the same report ``auth status`` renders.

    Reads the chain rather than requiring it (``auth.chain.resolve_credentials``,
    not ``require_credentials``): a missing credential is a finding for doctor
    to report, not a reason for doctor itself to raise.
    """
    from fantasy_sports.auth.chain import ESPN_CREDENTIALS, resolve_credentials
    from fantasy_sports.auth.staleness import Freshness, build_auth_status

    resolved = resolve_credentials(ESPN_CREDENTIALS)
    report = build_auth_status(resolved)
    payload = report.to_payload()

    # The write path checks ownership against the SWID but authenticates as
    # espn_s2's session, and the chain resolves the two independently. Two
    # cookies from two different links (env, Keychain, config) are the cheap,
    # no-network signal that they may belong to two accounts (#107, PR #118).
    s2, swid = resolved.get("espn_s2"), resolved.get("swid")
    split = s2 is not None and swid is not None and s2.source is not swid.source
    payload["sources_match"] = None if s2 is None or swid is None else not split

    if not report.complete:
        status = CheckStatus.WARN
        summary = "ESPN credentials are not fully configured."
    elif split:
        assert s2 is not None and swid is not None  # narrowed by `split`
        status = CheckStatus.WARN
        summary = (
            f"espn_s2 resolves from {s2.source.value} but swid from {swid.source.value}; "
            "if they belong to different ESPN accounts, writes are refused after the "
            "first one. Run `fantasy-sports auth login` to store both together."
        )
    elif any(status.freshness is Freshness.STALE for status in report.credentials):
        status = CheckStatus.WARN
        summary = "ESPN credentials are configured, but at least one looks stale."
    else:
        status = CheckStatus.OK
        summary = "ESPN credentials are configured."
    return DoctorCheck(name="credentials", status=status, summary=summary, details=payload)


def _cache_check() -> DoctorCheck:
    """Can the SQLite response cache be opened, and what is in it.

    Counts rows through a fresh, read-only connection of its own rather than
    reaching into :class:`~fantasy_sports.cache.store.CacheStore`'s private
    connection — the store exposes no row-count API and doctor has no reason
    to add one to a module it does not otherwise touch.
    """
    from fantasy_sports.cache.store import CacheStore

    store = CacheStore()
    try:
        if not store.available:
            return DoctorCheck(
                name="cache",
                status=CheckStatus.FAIL,
                summary=f"Cache store at {store.path} could not be opened.",
                details={"path": str(store.path)},
            )
        size = store.path.stat().st_size if store.path.exists() else 0
        entries = _count_cache_entries(store.path)
        summary = (
            f"Cache reachable: {entries} entrie(s), {size:,} byte(s) at {store.path}."
            if entries is not None
            else f"Cache file at {store.path} could not be read back."
        )
        return DoctorCheck(
            name="cache",
            status=CheckStatus.OK if entries is not None else CheckStatus.WARN,
            summary=summary,
            details={"path": str(store.path), "entries": entries, "size_bytes": size},
        )
    finally:
        store.close()


def _count_cache_entries(path: Any) -> int | None:
    import sqlite3

    try:
        connection = sqlite3.connect(path)
        try:
            row = connection.execute("SELECT COUNT(*) FROM entries").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return int(row[0]) if row is not None else None


def _journal_check() -> DoctorCheck:
    """Where the mutation journal is, what is in it, and whether anything is unfinished.

    Read-only: it never creates the file or its directory. A missing journal is
    fine, because nothing has been written yet. ``pending`` rows are the point
    of the check: an entry begun and never closed means a write may have been
    sent without the outcome being recorded, which a person must reconcile
    against ESPN's transaction log. A corrupt or unreadable file is a finding,
    never an exception (``docs/memory/doctor-must-never-raise.md``).
    """
    from fantasy_sports.core.journal import Journal

    try:
        health = Journal.open().health()
    except Exception as exc:  # noqa: BLE001 - a check reports; it never crashes doctor
        return DoctorCheck(
            name="journal",
            status=CheckStatus.FAIL,
            summary=f"The journal could not be inspected ({type(exc).__name__}).",
        )

    details: dict[str, Any] = {
        "path": str(health.path),
        "exists": health.exists,
        "entries": health.entries,
        "pending": health.pending,
        "pending_ids": list(health.pending_ids),
        "file_mode": None if health.file_mode is None else format(health.file_mode, "04o"),
        "dir_mode": None if health.dir_mode is None else format(health.dir_mode, "04o"),
        "permissions_ok": health.permissions_ok,
    }
    if health.error is not None:
        details["error"] = health.error
        return DoctorCheck(
            name="journal",
            status=CheckStatus.FAIL,
            summary=(
                f"Journal at {health.path} exists but cannot be read; writes are refused "
                "until it is moved aside."
            ),
            details=details,
        )
    if not health.exists:
        return DoctorCheck(
            name="journal",
            status=CheckStatus.OK,
            summary=f"No journal yet at {health.path}; it is created by the first write.",
            details=details,
        )
    if health.pending:
        return DoctorCheck(
            name="journal",
            status=CheckStatus.WARN,
            summary=(
                f"{health.pending} journal entr{'y is' if health.pending == 1 else 'ies are'} "
                "pending: a write was begun and its outcome never recorded. Compare with "
                "ESPN's transaction log; see `journal show --id`."
            ),
            details=details,
        )
    if not health.permissions_ok:
        return DoctorCheck(
            name="journal",
            status=CheckStatus.WARN,
            summary=(
                f"Journal at {health.path} is not private: expected the file 0600 and its "
                f"directory 0700, found {details['file_mode']} and {details['dir_mode']}."
            ),
            details=details,
        )
    return DoctorCheck(
        name="journal",
        status=CheckStatus.OK,
        summary=f"Journal reachable: {health.entries} entr{'y' if health.entries == 1 else 'ies'}, "
        f"none pending, at {health.path}.",
        details=details,
    )


def _notify_check() -> DoctorCheck:
    """Is irreversible-write notification configured, and is the command non-blank?

    Reads ``[notify]`` from ``config.toml`` and nothing else. It does **not**
    run the command: a health check that fired the owner's delivery script would
    announce a write that never happened. Disabled is ``ok`` (the default); an
    unreadable table is ``warn``, because John may believe he will be told when
    he will not (jwulff/fantasy-sports#113).
    """
    from fantasy_sports.config import notify as config_notify

    try:
        config = config_notify.load()
    except Exception as exc:  # noqa: BLE001 - a check reports; it never crashes doctor
        return DoctorCheck(
            name="notify",
            status=CheckStatus.WARN,
            summary="[notify] in config.toml could not be read, so irreversible writes "
            "will not be announced: " + getattr(exc, "message", type(exc).__name__),
            details={"enabled": False},
        )
    if config.irreversible_command is None:
        return DoctorCheck(
            name="notify",
            status=CheckStatus.OK,
            summary="Notifications are off; set [notify] irreversible_command to be told "
            "about adds, drops, and claims.",
            details={"enabled": False, "command_set": False},
        )
    if not config.enabled:
        return DoctorCheck(
            name="notify",
            status=CheckStatus.WARN,
            summary="[notify] irreversible_command is blank, so nothing will be announced.",
            details={"enabled": False, "command_set": False},
        )
    return DoctorCheck(
        name="notify",
        status=CheckStatus.OK,
        summary="Irreversible writes are announced to the configured command (not run by doctor).",
        details={"enabled": True, "command_set": True},
    )


# --------------------------------------------------------------------------- #
# The forced, cache-ignoring manifest fetch (ARCHITECTURE §11.3's trigger table)
# --------------------------------------------------------------------------- #


def _health_checks(*, current_version: str) -> list[DoctorCheck]:
    """``version`` and ``provider_status``, both derived from one manifest fetch.

    ``doctor`` is the one caller allowed to ignore the 6-hour cache
    (``force=True``) — every other caller goes through the on-error path in
    :func:`fantasy_sports.health.client.evaluate_failure`, which respects it.
    The opt-out is honoured here too: a user who disabled the check does not
    expect ``doctor`` to quietly make the one network call they turned off.
    """
    from fantasy_sports.commands.context import PROVIDERS
    from fantasy_sports.health.client import get_manifest, is_opted_out, upgrade_available

    if is_opted_out():
        note = (
            "Health check disabled (FANTASY_SPORTS_NO_HEALTH_CHECK, or "
            "health_check = false in config.toml)."
        )
        return [
            DoctorCheck(name="version", status=CheckStatus.SKIPPED, summary=note),
            DoctorCheck(name="provider_status", status=CheckStatus.SKIPPED, summary=note),
        ]

    try:
        manifest = get_manifest(force=True)
    except Exception:  # noqa: BLE001 - belt and braces: get_manifest already fails open
        manifest = None
    if manifest is None:
        note = (
            "Could not reach the health manifest (offline, GitHub unreachable, "
            "or not yet published)."
        )
        return [
            DoctorCheck(
                name="version",
                status=CheckStatus.WARN,
                summary=note,
                details={"your_version": current_version},
            ),
            DoctorCheck(name="provider_status", status=CheckStatus.WARN, summary=note),
        ]

    upgraded = upgrade_available(current_version, manifest.latest_version)
    yanked = current_version in manifest.yanked_versions
    if yanked:
        version_status = CheckStatus.FAIL
        version_summary = (
            f"Running a yanked release ({current_version}); upgrade to {manifest.latest_version}."
        )
    elif upgraded:
        version_status = CheckStatus.WARN
        version_summary = f"You are on {current_version} — {manifest.latest_version} is available."
    else:
        version_status = CheckStatus.OK
        version_summary = f"You are on the latest version ({current_version})."
    version_check = DoctorCheck(
        name="version",
        status=version_status,
        summary=version_summary,
        details={
            "your_version": current_version,
            "latest_version": manifest.latest_version,
            "min_supported_version": manifest.min_supported_version,
            "yanked": yanked,
            "upgrade_available": upgraded,
        },
    )

    providers: dict[str, Any] = {}
    degraded: list[str] = []
    for name in sorted(PROVIDERS):
        entry = manifest.provider(name)
        status = entry.status if entry else None
        providers[name] = {
            "status": status,
            "checked_at": entry.checked_at if entry else None,
            "known_issues": (
                [issue.to_health_payload() for issue in entry.known_issues] if entry else []
            ),
        }
        if status and status != "healthy":
            degraded.append(f"{name}: {status}")
    provider_check = DoctorCheck(
        name="provider_status",
        status=CheckStatus.WARN if degraded else CheckStatus.OK,
        summary="; ".join(degraded) if degraded else "No known provider issues reported.",
        details=providers,
    )
    return [version_check, provider_check]


# --------------------------------------------------------------------------- #
# The one check that can touch ESPN, and only under --live
# --------------------------------------------------------------------------- #


def _leagues_reachable_check(*, live: bool) -> DoctorCheck:
    from fantasy_sports.config.leagues import list_leagues
    from fantasy_sports.core.errors import ConfigInvalidError

    try:
        profiles = list_leagues()
    except ConfigInvalidError:
        # The `config` check already reports this in detail; a second check
        # crashing on the same broken file would defeat the point of doctor
        # never raising. Nothing to report reachability *of*.
        return DoctorCheck(
            name="leagues_reachable",
            status=CheckStatus.SKIPPED,
            summary="config.toml did not parse; see the `config` check.",
        )
    if not profiles:
        return DoctorCheck(
            name="leagues_reachable",
            status=CheckStatus.SKIPPED,
            summary="No leagues configured.",
        )
    if not live:
        return DoctorCheck(
            name="leagues_reachable",
            status=CheckStatus.SKIPPED,
            summary=f"{len(profiles)} league(s) configured; pass --live to check reachability.",
            details={"leagues": [profile.name for profile in profiles]},
        )

    from fantasy_sports.commands.context import open_read
    from fantasy_sports.core.errors import FantasySportsError
    from fantasy_sports.output.errors import classify

    results: dict[str, Any] = {}
    for profile in profiles:
        try:
            ctx = open_read(profile.name)
            ctx.provider.fetch_league(*ctx.target)
        except FantasySportsError as exc:
            results[profile.name] = {
                "reachable": False,
                "code": exc.code.value,
                "message": exc.message,
            }
        except Exception as exc:  # noqa: BLE001 - a check reports; it never crashes doctor
            classified = classify(exc)
            results[profile.name] = {
                "reachable": False,
                "code": classified.code.value,
                "message": classified.message,
            }
        else:
            results[profile.name] = {"reachable": True}

    unreachable = sorted(name for name, result in results.items() if not result["reachable"])
    return DoctorCheck(
        name="leagues_reachable",
        status=CheckStatus.FAIL if unreachable else CheckStatus.OK,
        summary=(
            "All configured leagues are reachable."
            if not unreachable
            else f"Unreachable: {', '.join(unreachable)}."
        ),
        details={"leagues": results},
    )
