"""The ``Provider`` Protocol, validated on paper against three providers.

Issue #3 asks for the Protocol to be checked against Sleeper and Yahoo shapes
*without implementing them*, so v0.1's ESPN-only reality does not quietly bake
ESPN assumptions into the contract. The three stubs below are that check made
executable: each returns normalized objects built from hand-written payloads in
that provider's own shape, taken from
``docs/research/02-provider-data-shapes.md`` §§3-6.

They are deliberately not adapters. Nothing here calls a network, and nothing
here is the ESPN provider — that is U7 (#8).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fantasy_sports.core.models import (
    BoxScore,
    CredentialSpec,
    FreeAgent,
    League,
    LineupEntry,
    Matchup,
    Player,
    RosterSlot,
    Team,
    Transaction,
)
from fantasy_sports.providers.base import (
    PROVIDER_METHODS,
    WRITING_PROVIDER_METHODS,
    Provider,
    WritingProvider,
)


class _StubProvider:
    """Shared plumbing so each stub below shows only what differs by provider."""

    name = "stub"
    league_id = "1"
    player_id = "1"
    owner_names: tuple[str, ...] = ("Owner",)
    eligible_slots: tuple[str, ...] = ()
    percent_owned: float | None = None
    specs: tuple[CredentialSpec, ...] = ()

    def credential_specs(self) -> list[CredentialSpec]:
        return list(self.specs)

    def fetch_league(self, league_id: str, season: int) -> League:
        return League(
            provider=self.name,
            provider_id=league_id,
            name="Test League",
            season=season,
            sport="nfl",
            team_count=12,
            current_week=3,
            raw={"league_id": league_id},
            roster_slots={"QB": 1, "RB": 2, "FLEX": 1, "BN": 6},
        )

    def fetch_teams(self, league_id: str, season: int) -> list[Team]:
        return [
            Team(
                provider=self.name,
                provider_id="7",
                name="Regression to the Mean",
                owner_names=self.owner_names,
                wins=2,
                losses=1,
                ties=0,
                points_for=331.4,
                points_against=298.2,
                raw={"team": 7},
                standing=4,
            )
        ]

    def _player(self) -> Player:
        return Player(
            provider=self.name,
            provider_id=self.player_id,
            name="Patrick Mahomes",
            position="QB",
            raw={"player_id": self.player_id},
            eligible_slots=self.eligible_slots,
            status="ACTIVE",
            opponent="DEN",
            projected_points=21.7,
            kickoff=datetime(2026, 9, 20, 17, 0, tzinfo=UTC),
        )

    def fetch_roster(
        self, league_id: str, season: int, team_id: str, week: int | None = None
    ) -> list[RosterSlot]:
        return [
            RosterSlot(
                provider=self.name,
                provider_id=self.player_id,
                player=self._player(),
                slot="QB",
                is_starter=True,
                raw={"slot": "QB", "week": week},
                is_locked=False,
            )
        ]

    def fetch_standings(self, league_id: str, season: int) -> list[Team]:
        return self.fetch_teams(league_id, season)

    def fetch_matchups(self, league_id: str, season: int, week: int) -> list[Matchup]:
        return [
            Matchup(
                provider=self.name,
                provider_id=f"{season}-w{week}-1",
                week=week,
                team_a_provider_id="7",
                team_a_score=118.4,
                team_b_provider_id="2",
                team_b_score=101.9,
                is_playoff=False,
                raw={"week": week},
                scoring_period_id=week,
                matchup_period_id=week,
            )
        ]

    def fetch_box_scores(self, league_id: str, season: int, week: int) -> list[BoxScore]:
        return [
            BoxScore(
                provider=self.name,
                provider_id=f"{season}-w{week}-1",
                week=week,
                team_a_provider_id="7",
                team_a_score=118.4,
                team_a_lineup=(
                    LineupEntry(
                        provider=self.name,
                        provider_id=self.player_id,
                        slot="QB",
                        player_name="A Quarterback",
                        position="QB",
                        pro_opponent="OPP",
                        projected_points=18.2,
                        actual_points=21.0,
                        started=True,
                        raw={},
                    ),
                ),
                team_b_provider_id="2",
                team_b_score=101.9,
                team_b_lineup=(),
                is_playoff=False,
                raw={"week": week},
                scoring_period_id=week,
                matchup_period_id=week,
            )
        ]

    def fetch_transactions(
        self, league_id: str, season: int, since: datetime | None = None
    ) -> list[Transaction]:
        return [
            Transaction(
                provider=self.name,
                provider_id="txn-1",
                type="waiver_claim",
                raw={"since": since},
                team_provider_id="7",
                players_in=(self.player_id,),
            )
        ]

    def fetch_free_agents(
        self, league_id: str, season: int, week: int, position: str | None = None
    ) -> list[FreeAgent]:
        return [
            FreeAgent(
                provider=self.name,
                provider_id=self.player_id,
                player=self._player(),
                raw={"position": position},
                percent_owned=self.percent_owned,
            )
        ]

    def fetch_raw(self, league_id: str, season: int, **provider_params: object) -> dict:
        return {"league_id": league_id, "season": season, "params": provider_params}


class EspnShapedProvider(_StubProvider):
    """Cookie-pair auth, integer-ish ids, eligibility enumerated on the player."""

    name = "espn"
    league_id = "123456"
    player_id = "3139477"
    owner_names = ("John Wulff", "Courtney Wulff")  # co-managers, native to ESPN
    eligible_slots = ("QB", "OP", "BE", "IR")
    percent_owned = 41.2
    specs = (
        CredentialSpec(name="espn_s2", label="ESPN espn_s2 cookie", staleness="age-only"),
        CredentialSpec(name="SWID", label="ESPN SWID cookie", staleness="age-only"),
    )


class SleeperShapedProvider(_StubProvider):
    """No auth at all; a "team" is roster ⋈ user; eligibility is not player data."""

    name = "sleeper"
    league_id = "784462448236174336"
    player_id = "4034"
    owner_names = ("sleeperuser",)
    eligible_slots = ()  # computed client-side from roster_positions, never returned
    percent_owned = None  # Sleeper does not track per-player ownership
    specs = ()


class YahooShapedProvider(_StubProvider):
    """OAuth2 refresh token; string league keys with leading zeros; co-managers."""

    name = "yahoo"
    league_id = "000123"  # yfpy stringifies precisely because of leading zeros
    player_id = "449.p.12345"
    owner_names = ("John", "Co-Manager")
    eligible_slots = ()  # Yahoo reports is_flex for the current slot only
    percent_owned = 38.0
    specs = (
        CredentialSpec(
            name="refresh_token",
            label="Yahoo OAuth2 refresh token",
            staleness="server-signalled on refresh",
        ),
    )


STUBS = [EspnShapedProvider, SleeperShapedProvider, YahooShapedProvider]


@pytest.mark.parametrize("cls", STUBS, ids=lambda c: c.__name__)
def test_a_conformance_stub_satisfies_isinstance(cls):
    assert isinstance(cls(), Provider)


@pytest.mark.parametrize("cls", STUBS, ids=lambda c: c.__name__)
def test_every_read_returns_normalized_objects_for_every_provider_shape(cls):
    provider = cls()
    league = provider.fetch_league(provider.league_id, 2026)
    assert isinstance(league, League)
    assert league.provider_id == provider.league_id
    assert all(isinstance(t, Team) for t in provider.fetch_teams(provider.league_id, 2026))
    assert all(isinstance(t, Team) for t in provider.fetch_standings(provider.league_id, 2026))
    assert all(
        isinstance(s, RosterSlot) for s in provider.fetch_roster(provider.league_id, 2026, "7")
    )
    assert all(isinstance(m, Matchup) for m in provider.fetch_matchups(provider.league_id, 2026, 3))
    assert all(
        isinstance(t, Transaction) for t in provider.fetch_transactions(provider.league_id, 2026)
    )
    assert all(
        isinstance(f, FreeAgent) for f in provider.fetch_free_agents(provider.league_id, 2026, 3)
    )
    assert isinstance(provider.fetch_raw(provider.league_id, 2026, view="mTeam"), dict)


def test_a_provider_requiring_no_credentials_is_expressible():
    """Sleeper needs no auth; the Protocol must not force a credential to exist."""
    assert SleeperShapedProvider().credential_specs() == []
    assert len(EspnShapedProvider().credential_specs()) == 2


def test_a_leading_zero_league_id_survives_because_ids_are_strings():
    league = YahooShapedProvider().fetch_league("000123", 2026)
    assert league.provider_id == "000123"


def test_the_protocol_does_not_require_eligibility_to_be_player_data():
    """Sleeper computes FLEX eligibility client-side; an empty tuple is legal."""
    slots = SleeperShapedProvider().fetch_roster("784462448236174336", 2026, "1")
    assert slots[0].player.eligible_slots == ()
    assert slots[0].slot == "QB"


def test_the_protocol_does_not_require_ownership_data():
    agents = SleeperShapedProvider().fetch_free_agents("784462448236174336", 2026, 3)
    assert agents[0].percent_owned is None


def test_roster_reads_accept_a_historical_week_and_default_to_current():
    provider = EspnShapedProvider()
    assert provider.fetch_roster("123456", 2026, "7")[0].raw["week"] is None
    assert provider.fetch_roster("123456", 2026, "7", week=2)[0].raw["week"] == 2


def test_transactions_accept_an_optional_since_filter():
    provider = EspnShapedProvider()
    since = datetime(2026, 9, 1, tzinfo=UTC)
    assert provider.fetch_transactions("123456", 2026, since=since)[0].raw["since"] == since


def test_raw_params_are_provider_specific_by_design():
    """`fetch_raw` deliberately does not invent a unified query language."""
    espn = EspnShapedProvider().fetch_raw("123456", 2026, view="mRoster")
    sleeper = SleeperShapedProvider().fetch_raw("784462448236174336", 2026, path="rosters")
    assert espn["params"] == {"view": "mRoster"}
    assert sleeper["params"] == {"path": "rosters"}


def test_a_type_missing_a_method_is_not_a_provider():
    class MissingFetchRaw(EspnShapedProvider):
        fetch_raw = None  # present as an attribute, but not callable

    assert not isinstance(MissingFetchRaw(), Provider)


def test_an_unrelated_object_is_not_a_provider():
    assert not isinstance(object(), Provider)


def test_the_protocol_surface_is_exactly_what_issue_3_requires():
    """#3's surface, plus `fetch_box_scores` added by #29.

    Required of every provider rather than optional: all three surveyed expose
    per-player weekly scoring, and an optional method would push a `hasattr`
    check into every consumer. A provider that cannot serve a *particular*
    week raises; the capability is not in doubt, the week is.
    """
    assert PROVIDER_METHODS == (
        "credential_specs",
        "fetch_league",
        "fetch_teams",
        "fetch_standings",
        "fetch_roster",
        "fetch_matchups",
        "fetch_box_scores",
        "fetch_transactions",
        "fetch_free_agents",
        "fetch_raw",
    )
    for method in PROVIDER_METHODS:
        assert callable(getattr(Provider, method))


def test_the_writing_protocol_surface_is_exactly_what_issue_107_requires():
    """A second Protocol, so `Provider` is unchanged and a read-only adapter
    still conforms. Pinned like `PROVIDER_METHODS`."""
    assert WRITING_PROVIDER_METHODS == (
        "session_role",
        "set_lineup",
        "move_players",
        "cancel_transaction",
        "fetch_pending_claims",
    )
    for method in WRITING_PROVIDER_METHODS:
        assert callable(getattr(WritingProvider, method))
        assert not hasattr(Provider, method)


@pytest.mark.parametrize("cls", STUBS, ids=lambda c: c.__name__)
def test_a_read_only_adapter_is_a_provider_and_not_a_writing_provider(cls):
    assert isinstance(cls(), Provider)
    assert not isinstance(cls(), WritingProvider)


def test_issubclass_is_unavailable_because_the_protocol_has_a_data_member():
    """``name`` is a data member; only ``isinstance`` works. Documented, not a bug."""
    with pytest.raises(TypeError):
        issubclass(EspnShapedProvider, Provider)
