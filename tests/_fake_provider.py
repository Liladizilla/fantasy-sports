"""Providers that fail in specific, taxonomy-shaped ways.

The command layer's job on a failure is to *not get in the way*: a code the
adapter chose must reach stderr with its details intact, and nothing may reach
stdout. Proving that needs a provider that fails on demand, which no cassette
can supply — a recording of a 401 records the absence of a credential, which is
indistinguishable from a recording where the scrub ran
(``tests/unit/test_espn_provider.py``).

These also stand in for "some other provider", which is the only way to check
that :data:`fantasy_sports.commands.context.PROVIDERS` is a real indirection
rather than an ESPN import with a dict around it.
"""

from __future__ import annotations

from typing import Any

from fantasy_sports.auth.chain import ESPN_CREDENTIALS
from fantasy_sports.core.errors import AuthExpiredError, SchemaDriftError


class _Base:
    """Enough of the Provider Protocol for a command to call one method."""

    name = "fake"

    def __init__(self, credentials: Any = None, **_: Any) -> None:
        self.credentials = credentials
        self.last_fetch = None

    def credential_specs(self) -> list[Any]:
        return list(ESPN_CREDENTIALS)

    def _fail(self) -> Any:
        raise NotImplementedError

    fetch_league = fetch_teams = fetch_standings = _fail
    fetch_roster = fetch_matchups = fetch_transactions = _fail
    fetch_free_agents = fetch_raw = _fail


class AuthExpiredProvider(_Base):
    """Every read reports that the stored credentials were rejected."""

    def _fail(self, *_: Any, **__: Any) -> Any:
        raise AuthExpiredError(
            "ESPN rejected the stored cookies.",
            remediation="Re-extract the cookies and run `fantasy-sports auth login`.",
        )

    fetch_league = fetch_teams = fetch_standings = _fail
    fetch_roster = fetch_matchups = fetch_transactions = _fail
    fetch_free_agents = fetch_raw = _fail


class DriftingProvider(_Base):
    """Every read reports a shape change, carrying the offending field path."""

    def _fail(self, *_: Any, **__: Any) -> Any:
        raise SchemaDriftError(
            "ESPN's mTeam response no longer carries the field we read.",
            path=["teams", "record", "overall", "wins"],
            provider="espn",
            details={"view": "mTeam"},
        )

    fetch_league = fetch_teams = fetch_standings = _fail
    fetch_roster = fetch_matchups = fetch_transactions = _fail
    fetch_free_agents = fetch_raw = _fail


class CrashingProvider(_Base):
    """Every read raises something the taxonomy has never heard of."""

    def _fail(self, *_: Any, **__: Any) -> Any:
        raise RuntimeError("a bug nobody classified")

    fetch_league = fetch_teams = fetch_standings = _fail
    fetch_roster = fetch_matchups = fetch_transactions = _fail
    fetch_free_agents = fetch_raw = _fail


class WritingFake(_Base):
    """A provider that satisfies ``WritingProvider`` and changes nothing.

    For the write commands (#15, #18) and the journal (#16): every write call
    is recorded on :attr:`calls` and answered with a canned
    :class:`~fantasy_sports.core.models.WriteResult`. ``owned`` drives the
    same refuse-closed ownership guard the ESPN adapter applies, so a command
    test can prove it surfaces ``not_owned`` without a real provider.
    ``fail_with`` makes every write raise instead, for the error paths.

    The canned result's ``credentials`` block has the same keys the adapter
    emits (R12a) so an envelope built from it has the real shape.
    """

    name = "fake"

    def __init__(
        self,
        credentials: Any = None,
        *,
        owned: tuple[str, ...] = ("1",),
        role: str = "member",
        member_id: str | None = "{00000000-0000-0000-0000-000000000001}",
        result: Any = None,
        fail_with: BaseException | None = None,
        **options: Any,
    ) -> None:
        super().__init__(credentials, **options)
        self.owned = tuple(owned)
        self.role = role
        self.member_id = member_id
        self.result = result
        self.fail_with = fail_with
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def session_role(self, league_id: str, season: int) -> Any:
        from fantasy_sports.core.models import SessionRole

        self.calls.append(("session_role", {"league_id": league_id, "season": season}))
        return SessionRole(
            provider=self.name,
            member_id=self.member_id,
            role=self.role,  # type: ignore[arg-type]
            owned_team_ids=self.owned if self.member_id else (),
            raw={},
        )

    def set_lineup(self, league_id: str, season: int, team_id: str, target: Any) -> Any:
        return self._write(
            "set_lineup", league_id, season, team_id, target=dict(target), kind="ROSTER"
        )

    def move_players(
        self,
        league_id: str,
        season: int,
        team_id: str,
        *,
        adds: Any,
        drops: Any,
        bid: int | None = None,
        waiver: bool = False,
    ) -> Any:
        claim = waiver or bid is not None
        return self._write(
            "move_players",
            league_id,
            season,
            team_id,
            adds=list(adds),
            drops=list(drops),
            bid=bid,
            waiver=waiver,
            kind="WAIVER" if claim else "FREEAGENT" if adds else "ROSTER",
        )

    def cancel_transaction(
        self, league_id: str, season: int, team_id: str, transaction_id: str
    ) -> Any:
        return self._write(
            "cancel_transaction",
            league_id,
            season,
            team_id,
            transaction_id=transaction_id,
            kind="CANCEL",
        )

    def _write(
        self, method: str, league_id: str, season: int, team_id: str, *, kind: str, **args: Any
    ) -> Any:
        from fantasy_sports.core.errors import WriteRejectedError
        from fantasy_sports.core.models import WriteResult

        self.calls.append(
            (method, {"league_id": league_id, "season": season, "team_id": team_id, **args})
        )
        if self.member_id is None or str(team_id) not in self.owned:
            raise WriteRejectedError(
                f"Refusing to write to team {team_id}: the fake session does not own it.",
                kind="not_owned",
                team_id=str(team_id),
            )
        if self.fail_with is not None:
            raise self.fail_with
        if self.result is not None:
            return self.result
        return WriteResult(
            provider=self.name,
            provider_transaction_id=f"fake-{len(self.calls)}",
            status="PENDING" if kind == "WAIVER" else "EXECUTED",
            is_pending=kind == "WAIVER",
            team_id=str(team_id),
            items=(),
            member_id=self.member_id,
            credentials={"espn_s2_age_seconds": None, "warning": "credential age unknown"},
            raw={"type": kind},
        )
