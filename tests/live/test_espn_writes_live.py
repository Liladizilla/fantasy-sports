"""Live check of the write surface's one read: ``session_role``. Never a write.

    uv run pytest -m live

Excluded from CI and skipped unless both cookies and
``FANTASY_SPORTS_ESPN_LEAGUE`` are configured. It sends **no** transaction:
``session_role`` reads ``view=mNav`` on the read host, and nothing else here
touches ``lm-api-writes``. A real-league write by accident is the one outcome
this project cannot undo, so the swap-and-reverse live test belongs to #15,
against the throwaway test league #108 stands up.

The assertions are structural. Who owns what, and which role the session holds,
are facts about a real private league and are not asserted.
"""

from __future__ import annotations

import os

import pytest

from fantasy_sports.auth.chain import ESPN_CREDENTIALS, resolve_credentials
from fantasy_sports.core.models import SESSION_ROLES
from fantasy_sports.core.redaction import is_swid_pseudonym
from fantasy_sports.providers.espn import EspnProvider

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _reach_the_network() -> None:
    """Undo ``--disable-socket`` for this module only (see ``test_espn_live.py``)."""
    from pytest_socket import enable_socket

    enable_socket()


@pytest.fixture(scope="module")
def private() -> tuple[EspnProvider, str, int]:
    credentials = resolve_credentials(ESPN_CREDENTIALS)
    league_id = os.environ.get("FANTASY_SPORTS_ESPN_LEAGUE")
    if not credentials.complete or not league_id:
        pytest.skip("needs both ESPN cookies and FANTASY_SPORTS_ESPN_LEAGUE")
    season = int(os.environ.get("FANTASY_SPORTS_ESPN_SEASON", "2026"))

    def _no_writes(*_: object, **__: object) -> object:
        raise AssertionError("the live session_role test must never POST")

    return EspnProvider(credentials, http_post=_no_writes), league_id, season


def test_the_session_owns_a_team_and_has_a_role(private):
    provider, league_id, season = private
    role = provider.session_role(league_id, season)
    assert role.role in SESSION_ROLES
    assert role.member_id is not None and is_swid_pseudonym(role.member_id)
    assert role.owned_team_ids, "the configured member owns no team in mNav's teams[].owners"
    teams = {team.provider_id for team in provider.fetch_teams(league_id, season)}
    assert set(role.owned_team_ids) <= teams
