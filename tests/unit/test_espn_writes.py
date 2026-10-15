"""The ESPN write transport and its ownership guard, offline (jwulff/fantasy-sports#107).

Every POST here goes to an injected ``http_post`` that records what it was
handed and answers with a body shaped exactly like a capture in
``docs/research/05-espn-write-surface/`` or, for adds, drops, claims and
cancels, ``docs/research/06-espn-add-drop-surface/`` (league ``99``). The reads a write
depends on -- the bootstrap, ``mNav`` -- replay the committed synthetic
cassette through the read transport's ``http`` seam, with ``mNav`` supplied as
an override because no cassette records it.

Two things this file deliberately does **not** contain:

* a cassette or test asserting that ESPN rejects a cross-team write. It does
  not, for a commissioner's session (brief §6); the guard's test is a unit test
  of the guard (``docs/memory/espn-lineup-write-authority-is-broader-than-team-ownership.md``);
* any live request. ``pytest-socket`` is on.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from fantasy_sports.core.errors import (
    AuthMissingError,
    ConfigInvalidError,
    ProviderUnavailableError,
    RateLimitedError,
    SchemaDriftError,
    WriteRejectedError,
)
from fantasy_sports.core.models import RosterMove, SessionRole, WriteResult
from fantasy_sports.core.redaction import (
    CASSETTE_SWID_SALT,
    forget_secrets,
    is_swid_pseudonym,
    remember_secret,
    swid_pseudonym,
)
from fantasy_sports.providers.base import (
    PROVIDER_METHODS,
    WRITING_PROVIDER_METHODS,
    Provider,
    WritingProvider,
)
from fantasy_sports.providers.espn import (
    _TRAN_KINDS,
    WRITE_TIMEOUT_SECONDS,
    EspnProvider,
    _credentials_block,
    _guard,
    _Write,
    _write_outcome,
)

CAPTURES = Path(__file__).resolve().parents[2] / "docs" / "research" / "05-espn-write-surface"
LEAGUE = ("99", 2026)
WRITE_URL = (
    "https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/seasons/2026"
    "/segments/0/leagues/99/transactions/"
)

S2 = "AEB" + "s2secretvalue" * 16
SWID = "{1A2B3C4D-5E6F-7A8B-9C0D-1E2F3A4B5C6D}"
OTHER = "{9F8E7D6C-5B4A-3928-1706-F5E4D3C2B1A0}"
CREDENTIALS = {"espn_s2": S2, "swid": SWID}


def _capture(name: str) -> dict[str, Any]:
    return json.loads((CAPTURES / f"{name}.json").read_text())


def _captured_response(name: str, **replace: Any) -> FakeResponse:
    """A capture's response, with the redacted member id swapped for a real-shaped one."""
    response = _capture(name)["response"]
    body = json.loads(json.dumps(response["body"]).replace("{SWID-REDACTED}", SWID))
    body.update(replace)
    return FakeResponse(status_code=response["status"], body=body)


# --------------------------------------------------------------------------- #
# Transport doubles
# --------------------------------------------------------------------------- #


@dataclass
class FakeResponse:
    status_code: int = 200
    body: Any = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def content(self) -> bytes:
        if isinstance(self.body, bytes):
            return self.body
        if isinstance(self.body, str):
            return self.body.encode()
        return json.dumps(self.body).encode()

    def json(self) -> Any:
        return json.loads(self.content)


class ReplayHttp:
    """The synthetic cassette through the read seam, plus per-view overrides."""

    def __init__(self, overrides: Mapping[str, Any] | None = None) -> None:
        import yaml
        from conftest import CASSETTE_LIBRARY_DIR

        document = yaml.safe_load((CASSETTE_LIBRARY_DIR / "espn/synthetic_2026.yaml").read_text())
        self.bodies = {
            _key(interaction["request"]["uri"]): interaction["response"]["body"]["string"]
            for interaction in document["interactions"]
        }
        self.overrides = dict(overrides or {})
        # No cassette records a standalone current-period mRoster; the
        # bootstrap carries the same teams[].roster shape, so serve that.
        self.bootstrap = json.loads(
            next(body for key, body in self.bodies.items() if ("view", "mMatchupScore") in key[1])
        )
        self.overrides.setdefault("mRoster", self.bootstrap)
        self.views: list[str] = []

    def __call__(self, url: str, params=None, headers=None, cookies=None) -> FakeResponse:
        pairs: list[tuple[str, str]] = []
        for name, value in (params or {}).items():
            values = value if isinstance(value, list | tuple) else [value]
            pairs.extend((str(name), str(item)) for item in values)
        view = dict(pairs).get("view")
        self.views.append(str(view))
        if view in self.overrides:
            override = self.overrides[view]
            return FakeResponse(body=override(dict(pairs)) if callable(override) else override)
        body = self.bodies.get(_key(url, pairs))
        if body is None:
            raise AssertionError(f"no recorded response for {url} {pairs}")
        return FakeResponse(body=body)


def _key(url: str, extra: list[tuple[str, str]] | None = None) -> tuple[Any, ...]:
    from urllib.parse import parse_qsl, urlsplit

    parts = urlsplit(url)
    return (parts.path, tuple(sorted(list(parse_qsl(parts.query)) + list(extra or []))))


class FakePost:
    """Records every POST; answers from a queue, or raises ``raises``."""

    def __init__(self, *responses: Any, raises: BaseException | None = None) -> None:
        self.responses = list(responses)
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        if self.raises is not None:
            raise self.raises
        if not self.responses:
            raise AssertionError("unexpected extra POST")
        return self.responses.pop(0)

    @property
    def body(self) -> dict[str, Any]:
        assert len(self.calls) == 1, f"expected one POST, saw {len(self.calls)}"
        return self.calls[0]["json"]


def _nav(
    *,
    creator: bool = False,
    manager: bool = False,
    member: str = SWID,
    teams: Any = None,
) -> dict[str, Any]:
    """An ``mNav`` body shaped as brief §7 records it: two authority flags per member."""
    return {
        "members": [
            {"id": member, "isLeagueCreator": creator, "isLeagueManager": manager},
            {"id": OTHER, "isLeagueCreator": False, "isLeagueManager": False},
        ],
        "teams": [
            {"id": 1, "owners": [member]},
            {"id": 2, "owners": [OTHER]},
        ]
        if teams is None
        else teams,
    }


def _provider(
    post: FakePost | None = None,
    *,
    nav: Any = None,
    credentials: Any = CREDENTIALS,
    **options: Any,
) -> EspnProvider:
    return EspnProvider(
        credentials,
        http=ReplayHttp({"mNav": _nav() if nav is None else nav}),
        http_post=post if post is not None else FakePost(_captured_response("p3-swap-execute")),
        **options,
    )


@pytest.fixture(autouse=True)
def _clean_scrub_registry() -> Iterator[None]:
    yield
    forget_secrets()


# --------------------------------------------------------------------------- #
# The Protocol
# --------------------------------------------------------------------------- #


def test_the_writing_surface_is_pinned():
    assert WRITING_PROVIDER_METHODS == (
        "session_role",
        "set_lineup",
        "move_players",
        "cancel_transaction",
        "fetch_pending_claims",
    )
    for method in WRITING_PROVIDER_METHODS:
        assert callable(getattr(WritingProvider, method))
    assert not set(WRITING_PROVIDER_METHODS) & set(PROVIDER_METHODS)


def test_the_espn_adapter_satisfies_both_protocols():
    provider = EspnProvider()
    assert isinstance(provider, Provider)
    assert isinstance(provider, WritingProvider)


def test_a_read_only_provider_still_conforms_to_the_read_protocol():
    """``Provider`` did not grow: an adapter that cannot write still satisfies it."""
    from _fake_provider import AuthExpiredProvider, WritingFake

    assert not isinstance(AuthExpiredProvider(), WritingProvider)
    assert isinstance(WritingFake(), WritingProvider)


def test_the_writing_fake_records_and_applies_the_same_guard():
    from _fake_provider import WritingFake

    fake = WritingFake(owned=("1",))
    result = fake.move_players("99", 2026, "1", adds=[RosterMove("5")], drops=[], bid=3)
    assert isinstance(result, WriteResult)
    assert result.is_pending and result.status == "PENDING"
    assert set(result.credentials) == {"espn_s2_age_seconds", "warning"}
    assert fake.calls[-1][0] == "move_players"
    assert fake.set_lineup("99", 2026, "1", {"5": "BE"}).status == "EXECUTED"
    assert fake.cancel_transaction("99", 2026, "1", "abc").raw == {"type": "CANCEL"}
    with pytest.raises(WriteRejectedError) as caught:
        fake.set_lineup("99", 2026, "2", {"5": "BE"})
    assert caught.value.kind == "not_owned"
    assert WritingFake(member_id=None).session_role("99", 2026).owned_team_ids == ()
    failing = WritingFake(fail_with=ProviderUnavailableError("down"))
    with pytest.raises(ProviderUnavailableError):
        failing.cancel_transaction("99", 2026, "1", "abc")
    canned = WritingFake(result="canned")
    assert canned.set_lineup("99", 2026, "1", {}) == "canned"


# --------------------------------------------------------------------------- #
# session_role
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("creator", "manager", "expected"),
    [
        (True, False, "creator"),
        (False, True, "manager"),
        (True, True, "creator"),
        (False, False, "member"),
    ],
)
def test_the_role_reads_both_mnav_flags(creator, manager, expected):
    """The founder carries ``isLeagueCreator`` and not ``isLeagueManager`` (brief §7)."""
    role = _provider(nav=_nav(creator=creator, manager=manager)).session_role(*LEAGUE)
    assert role.role == expected
    assert role.owned_team_ids == ("1",)


def test_the_role_comes_from_mnav_and_never_mleaguemanager():
    http = ReplayHttp({"mNav": _nav()})
    EspnProvider(CREDENTIALS, http=http).session_role(*LEAGUE)
    assert "mNav" in http.views
    assert "mLeagueManager" not in http.views


def test_the_member_id_is_the_swid_pseudonym_never_the_swid():
    role = _provider().session_role(*LEAGUE)
    assert role.member_id == swid_pseudonym(SWID, salt=CASSETTE_SWID_SALT)
    assert is_swid_pseudonym(role.member_id)
    assert SWID not in json.dumps(role.to_dict())
    assert role.raw["isLeagueCreator"] is False


def test_the_swid_matches_case_insensitively_and_without_braces():
    provider = _provider(credentials={"espn_s2": S2, "swid": SWID.lower().strip("{}")})
    assert provider.session_role(*LEAGUE).owned_team_ids == ("1",)


def test_a_co_managed_team_counts_as_owned():
    teams = [{"id": 1, "owners": [SWID]}, {"id": 3, "owners": [OTHER, SWID.lower()]}]
    assert _provider(nav=_nav(teams=teams)).session_role(*LEAGUE).owned_team_ids == ("1", "3")


def test_ownership_falls_back_to_the_bootstrap_teams_when_mnav_has_none():
    """The synthetic bootstrap's team 1 is owned by ``{OWNER-ALPHA}``."""
    nav = {"members": [{"id": "{OWNER-ALPHA}", "isLeagueCreator": True}]}
    provider = _provider(nav=nav, credentials={"espn_s2": S2, "swid": "{owner-alpha}"})
    role = provider.session_role(*LEAGUE)
    assert role.owned_team_ids == ("1",)
    assert role.role == "creator"


def test_without_a_swid_the_member_and_its_teams_are_unknown():
    role = _provider(credentials={"espn_s2": S2}).session_role(*LEAGUE)
    assert role.member_id is None
    assert role.owned_team_ids == ()
    assert role.role == "member"
    assert role.raw == {}


def test_without_a_swid_a_member_id_echoed_by_a_write_is_used():
    provider = _provider(credentials={"espn_s2": S2})
    echoed = swid_pseudonym(SWID, salt=CASSETTE_SWID_SALT)
    provider._echoed_members[LEAGUE] = echoed
    nav = _nav(member=echoed)
    role = provider._role_from(nav, {}, key=LEAGUE)
    assert role.member_id == echoed
    assert role.owned_team_ids == ("1",)


def test_a_swid_that_is_not_a_member_owns_nothing():
    role = _provider(nav=_nav(member=OTHER)).session_role(*LEAGUE)
    assert role.member_id is not None
    assert role.owned_team_ids == ()
    assert role.role == "member"


@pytest.mark.parametrize("nav", [["not", "a", "mapping"], {"teams": []}, {"members": "x"}])
def test_an_mnav_without_members_is_schema_drift(nav):
    with pytest.raises(SchemaDriftError) as caught:
        _provider(nav=nav).session_role(*LEAGUE)
    assert caught.value.details["path"] == ["mNav", "members"]


def test_ownership_survives_the_cache_pseudonymising_every_swid(tmp_path):
    """A command's reads go through the store, which rewrites every SWID in a
    body to a per-store pseudonym before the adapter parses it. Matching the
    raw SWID against that would own nothing and refuse every write."""
    from fantasy_sports.cache.store import CacheStore

    store = CacheStore(tmp_path / "cache.sqlite3")
    provider = _provider(cache=store, nav=_nav(creator=True))
    role = provider.session_role(*LEAGUE)
    assert role.owned_team_ids == ("1",)
    assert role.role == "creator"
    assert role.member_id == swid_pseudonym(SWID, salt=store.swid_salt)
    recorded = json.dumps(provider.last_fetch.payload("mNav"))
    assert SWID not in recorded and role.member_id in recorded


def test_session_role_is_a_read_and_records_its_sources():
    provider = _provider()
    provider.session_role(*LEAGUE)
    assert "mNav" in provider.last_fetch.responses


def test_a_session_role_rejects_an_unknown_role():
    with pytest.raises(ValueError, match="unknown session role"):
        SessionRole(provider="espn", member_id=None, role="owner", owned_team_ids=(), raw={})


# --------------------------------------------------------------------------- #
# The ownership guard
# --------------------------------------------------------------------------- #


def test_a_write_to_an_owned_team_is_sent():
    post = FakePost(_captured_response("p3-swap-execute"))
    _provider(post).set_lineup(*LEAGUE, "1", {"2002": "BE", "2003": "RB/WR/TE"})
    assert len(post.calls) == 1


@pytest.mark.parametrize("creator", [False, True])
def test_a_write_to_another_team_is_refused_before_any_request_whatever_the_role(creator):
    """A commissioner's cookie is honoured on another team (brief §6), so the
    refusal must be ours and must not depend on role."""
    post = FakePost()
    provider = _provider(post, nav=_nav(creator=creator))
    with pytest.raises(WriteRejectedError) as caught:
        provider.set_lineup(*LEAGUE, "2", {"2011": "BE"})
    assert post.calls == []
    error = caught.value
    assert error.kind == "not_owned"
    assert error.http_status is None
    assert error.transaction_applied is False
    assert error.details["team_id"] == "2"
    assert "1" in error.remediation
    assert error.details["owned_team_ids"] == ["1"]


@pytest.mark.parametrize(
    "call",
    [
        lambda p: p.set_lineup(*LEAGUE, "1", {"2002": "BE"}),
        lambda p: p.move_players(*LEAGUE, "1", adds=[RosterMove("3001")], drops=[]),
        lambda p: p.cancel_transaction(*LEAGUE, "1", "abc"),
    ],
)
def test_unknown_ownership_is_refused_closed_for_every_write(call):
    post = FakePost()
    with pytest.raises(WriteRejectedError) as caught:
        call(_provider(post, credentials={"espn_s2": S2}))
    assert post.calls == []
    assert caught.value.kind == "not_owned"
    assert "SWID" in caught.value.remediation


def test_a_non_numeric_team_is_simply_not_owned():
    role = SessionRole(provider="espn", member_id="m", role="member", owned_team_ids=("1",), raw={})
    with pytest.raises(WriteRejectedError) as caught:
        _guard(role, "one")
    assert caught.value.kind == "not_owned"
    assert _guard(role, " 1 ") == 1


def test_no_override_exists_on_any_write_method():
    import inspect

    for method in WRITING_PROVIDER_METHODS[1:]:
        params = set(inspect.signature(getattr(EspnProvider, method)).parameters)
        assert not params & {"force", "override", "allow_other_team", "as_league_manager"}


def test_a_write_without_espn_s2_is_auth_missing_with_nothing_sent():
    post = FakePost()
    http = ReplayHttp({"mNav": _nav()})
    provider = EspnProvider({"swid": SWID}, http=http, http_post=post)
    with pytest.raises(AuthMissingError):
        provider.set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert post.calls == []
    assert http.views == []


# --------------------------------------------------------------------------- #
# What goes on the wire
# --------------------------------------------------------------------------- #


def test_a_lineup_set_is_one_roster_transaction_of_only_the_changed_slots():
    post = FakePost(_captured_response("p3-swap-execute"))
    _provider(post).set_lineup(
        *LEAGUE,
        "1",
        # 2001 is already at QB: it must not become a from==to item, which ESPN
        # rejects as TRAN_ROSTER_SAME_SLOT and takes the transaction with it.
        {"2001": "QB", "2003": "RB/WR/TE", "2002": "BE"},
    )
    assert post.body == {
        "isLeagueManager": False,
        "teamId": 1,
        "type": "ROSTER",
        "scoringPeriodId": 2,
        "executionType": "EXECUTE",
        "items": [
            # The vacate is emitted first even though the caller named it second.
            {"playerId": 2002, "type": "LINEUP", "fromLineupSlotId": 2, "toLineupSlotId": 20},
            {"playerId": 2003, "type": "LINEUP", "fromLineupSlotId": 20, "toLineupSlotId": 23},
        ],
    }


def test_the_request_carries_espn_s2_only_and_the_web_clients_headers():
    post = FakePost(_captured_response("p3-swap-execute"))
    _provider(post).set_lineup(*LEAGUE, "1", {"2002": "BE"})
    call = post.calls[0]
    assert call["url"] == WRITE_URL
    assert call["cookies"] == {"espn_s2": S2}
    assert call["headers"] == {
        "Content-Type": "application/json",
        "x-fantasy-source": "kona",
        "x-fantasy-platform": "kona-PROD",
    }
    assert call["timeout"] == WRITE_TIMEOUT_SECONDS == 15.0
    wire = json.dumps(call)
    assert SWID not in wire
    assert SWID.strip("{}") not in wire
    assert "memberId" not in call["json"]
    assert call["json"]["isLeagueManager"] is False


def test_a_free_agent_add_and_drop_sends_the_captured_item_shape():
    post = FakePost(_captured_response("p3-swap-execute", type="FREEAGENT"))
    _provider(post).move_players(
        *LEAGUE, "1", adds=[RosterMove("3001", to_slot="BE")], drops=[RosterMove("2003")]
    )
    assert post.body == {
        "isLeagueManager": False,
        "teamId": 1,
        "type": "FREEAGENT",
        "scoringPeriodId": 2,
        "executionType": "EXECUTE",
        "items": [
            {"playerId": 3001, "type": "ADD", "toTeamId": 1, "toLineupSlotId": 20},
            {"playerId": 2003, "type": "DROP", "fromTeamId": 1},
        ],
    }


def test_a_bid_makes_it_a_waiver_claim_carrying_bid_amount():
    post = FakePost(
        _captured_response("p3-swap-execute", type="WAIVER", isPending=True, status="PENDING")
    )
    result = _provider(post).move_players(
        *LEAGUE, "1", adds=[RosterMove("3001")], drops=[RosterMove("2003", from_slot="BE")], bid=7
    )
    assert post.body["type"] == "WAIVER"
    assert post.body["bidAmount"] == 7
    assert post.body["items"][1] == {
        "playerId": 2003,
        "type": "DROP",
        "fromTeamId": 1,
        "fromLineupSlotId": 20,
    }
    assert result.is_pending is True


def test_a_free_agent_move_never_carries_a_bid_amount():
    post = FakePost(_captured_response("p3-swap-execute"))
    _provider(post).move_players(*LEAGUE, "1", adds=[], drops=[RosterMove("2003")])
    assert "bidAmount" not in post.body


def test_a_cancel_names_the_transaction_and_sends_no_items():
    post = FakePost(_captured_response("p3-swap-execute", executionType="CANCEL"))
    _provider(post).cancel_transaction(*LEAGUE, "1", "  0f1e2d3c-uuid  ")
    assert post.body == {
        "isLeagueManager": False,
        "teamId": 1,
        "type": "WAIVER",
        "scoringPeriodId": 2,
        "executionType": "CANCEL",
        "relatedTransactionId": "0f1e2d3c-uuid",
    }


def test_the_scoring_period_is_the_leagues_current_one_not_a_parameter():
    import inspect

    assert "week" not in inspect.signature(EspnProvider.set_lineup).parameters
    post = FakePost(_captured_response("p3-swap-execute"))
    _provider(post).set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert post.body["scoringPeriodId"] == 2  # the synthetic league's scoringPeriodId


@pytest.mark.parametrize(
    ("bootstrap", "expected"),
    [({"scoringPeriodId": 5}, 5), ({"status": {"latestScoringPeriod": 7}}, 7)],
)
def test_the_current_period_falls_back_to_the_status_block(bootstrap, expected):
    from fantasy_sports.providers.espn import _current_period

    assert _current_period(bootstrap) == expected


@pytest.mark.parametrize("bootstrap", [{}, {"status": "x"}, {"scoringPeriodId": True}])
def test_a_league_with_no_current_period_is_schema_drift(bootstrap):
    from fantasy_sports.providers.espn import _current_period

    with pytest.raises(SchemaDriftError):
        _current_period(bootstrap)


# --------------------------------------------------------------------------- #
# Refused as a bad argument, before any request
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("target", "fragment"),
    [
        ({"2001": "QB"}, "nothing to send"),
        ({}, "names no players"),
        ({"2001": "NOT-A-SLOT"}, "not an ESPN lineup slot"),
        ({"2001": 4}, "not an ESPN lineup slot"),
        ({"9999": "BE"}, "not on team"),
        ({"abc": "BE"}, "not on team"),
        # 2003's eligibleSlots are [3, 4, 5, 23, 20, 21]: no RB (2).
        ({"2003": "RB"}, "not eligible"),
    ],
)
def test_a_lineup_that_cannot_be_sent_is_refused_as_a_bad_argument(target, fragment):
    post = FakePost()
    with pytest.raises(ConfigInvalidError) as caught:
        _provider(post).set_lineup(*LEAGUE, "1", target)
    assert caught.value.kind == "argument"
    assert fragment in caught.value.message
    assert post.calls == []


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"adds": [], "drops": []}, "names no players"),
        ({"adds": [RosterMove("1")], "drops": [], "bid": -1}, "non-negative"),
        ({"adds": [RosterMove("1")], "drops": [], "bid": "lots"}, "non-negative"),
        ({"adds": [RosterMove("x")], "drops": []}, "numeric"),
        ({"adds": [RosterMove("1", to_slot="SIDELINE")], "drops": []}, "lineup slot"),
    ],
)
def test_an_add_or_drop_that_cannot_be_sent_is_refused(kwargs, fragment):
    post = FakePost()
    with pytest.raises(ConfigInvalidError) as caught:
        _provider(post).move_players(*LEAGUE, "1", **kwargs)
    assert caught.value.kind == "argument"
    assert fragment in caught.value.message
    assert post.calls == []


@pytest.mark.parametrize("transaction_id", ["", "   ", None])
def test_a_cancel_without_a_transaction_id_is_refused(transaction_id):
    post = FakePost()
    with pytest.raises(ConfigInvalidError):
        _provider(post).cancel_transaction(*LEAGUE, "1", transaction_id)
    assert post.calls == []


def test_a_roster_entry_without_a_slot_is_schema_drift():
    http = ReplayHttp({"mNav": _nav()})
    roster = json.loads(json.dumps(http.bootstrap))
    for team in roster["teams"]:
        for entry in team["roster"]["entries"]:
            entry.pop("lineupSlotId", None)
    http.overrides["mRoster"] = roster
    post = FakePost()
    provider = EspnProvider(CREDENTIALS, http=http, http_post=post)
    with pytest.raises(SchemaDriftError):
        provider.set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert post.calls == []


# --------------------------------------------------------------------------- #
# Status mapping -- one test per branch
# --------------------------------------------------------------------------- #


def test_a_200_becomes_a_write_result_parsed_from_the_capture():
    post = FakePost(_captured_response("p3-swap-execute"))
    result = _provider(post).set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert isinstance(result, WriteResult)
    assert result.provider == "espn"
    assert result.provider_transaction_id == "<uuid>"
    assert result.status == "EXECUTED"
    assert result.is_pending is False
    assert result.proposed_at == datetime.fromtimestamp(1789237765019 / 1000, tz=UTC)
    assert result.proposed_at.tzinfo is not None
    assert result.bid_amount == 0
    assert result.team_id == "1"
    assert [item["playerId"] for item in result.items] == [9000013, 9000012]
    assert result.member_id == swid_pseudonym(SWID, salt=CASSETTE_SWID_SALT)
    assert set(result.credentials) == {"espn_s2_age_seconds", "warning"}


def test_a_write_response_member_id_is_the_same_pseudonym_session_role_reports():
    provider = _provider()
    role = provider.session_role(*LEAGUE)
    result = provider.set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert result.member_id == role.member_id
    assert provider._echoed_members[LEAGUE] == role.member_id


def test_a_401_with_a_credential_sent_is_write_rejected_auth_never_expired():
    post = FakePost(_captured_response("p13-invalid-espn_s2"))
    with pytest.raises(WriteRejectedError) as caught:
        _provider(post).set_lineup(*LEAGUE, "1", {"2002": "BE"})
    error = caught.value
    assert error.kind == "auth"
    assert error.provider_type == "AUTH_MISSING_CREDENTIALS"
    assert error.http_status == 401
    assert error.retryable is False
    assert "auth status" in error.remediation


def _write() -> _Write:
    return _Write(
        league_id="99",
        season=2026,
        team_id=1,
        scoring_period=2,
        credentials={"espn_s2_age_seconds": None, "warning": "credential age unknown"},
    )


def test_a_401_with_nothing_sent_is_auth_missing():
    """Unreachable through the public methods, which refuse earlier; the
    mapping is still total."""
    with pytest.raises(AuthMissingError):
        _write_outcome(401, {}, headers={}, cookies_sent=False, write=_write())


def test_a_400_without_details_is_schema_drift():
    post = FakePost(_captured_response("p2-executionType-VALIDATE"))
    with pytest.raises(SchemaDriftError) as caught:
        _provider(post).set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert caught.value.details["path"] == ["transactions"]
    assert caught.value.details["http_status"] == 400


def test_a_typed_400_is_a_rejection_not_drift():
    body = {"messages": ["nope"], "details": [{"type": "TRAN_SOMETHING_NEW"}]}
    with pytest.raises(WriteRejectedError) as caught:
        _write_outcome(400, body, headers={}, cookies_sent=True, write=_write())
    assert caught.value.kind == "unknown"
    assert caught.value.http_status == 400


@pytest.mark.parametrize(("provider_type", "kind"), sorted(_TRAN_KINDS.items()))
def test_every_known_tran_type_maps_to_its_kind(provider_type, kind):
    body = {"messages": ["ESPN says no"], "details": [{"type": provider_type}]}
    with pytest.raises(WriteRejectedError) as caught:
        _write_outcome(409, body, headers={}, cookies_sent=True, write=_write())
    assert caught.value.kind == kind
    assert caught.value.provider_type == provider_type
    assert caught.value.remediation == "ESPN says no"


def test_a_409_lock_from_the_capture_reports_espns_message():
    post = FakePost(_captured_response("p7-mixed-legal-and-locked"))
    with pytest.raises(WriteRejectedError) as caught:
        _provider(post).set_lineup(*LEAGUE, "1", {"2002": "BE"})
    error = caught.value
    assert error.kind == "roster_lock"
    assert error.provider_type == "TRAN_LINEUP_LOCKED"
    assert error.http_status == 409
    assert error.details["team_id"] == "1"
    assert error.details["transaction_applied"] is False
    assert error.remediation.startswith("Lineup transaction could not be completed")


def test_an_unmapped_tran_type_is_unknown_and_kept_verbatim():
    body = {"messages": ["Roster is full"], "details": [{"type": "TRAN_ROSTER_FULL_MAYBE"}]}
    with pytest.raises(WriteRejectedError) as caught:
        _write_outcome(409, body, headers={}, cookies_sent=True, write=_write())
    assert caught.value.kind == "unknown"
    assert caught.value.provider_type == "TRAN_ROSTER_FULL_MAYBE"


def test_a_409_with_an_unreadable_body_is_still_a_rejection():
    post = FakePost(FakeResponse(status_code=409, body=b"\xff\xfe not json"))
    with pytest.raises(WriteRejectedError) as caught:
        _provider(post).set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert caught.value.kind == "unknown"
    assert caught.value.provider_type is None


def test_a_429_is_rate_limited_with_retry_after():
    post = FakePost(FakeResponse(status_code=429, headers={"Retry-After": "12"}))
    with pytest.raises(RateLimitedError) as caught:
        _provider(post).set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert caught.value.retry_after == 12.0
    assert len(post.calls) == 1


def test_a_lower_case_retry_after_header_is_still_read():
    with pytest.raises(RateLimitedError) as caught:
        _write_outcome(429, None, headers={"retry-after": "3"}, cookies_sent=True, write=_write())
    assert caught.value.retry_after == 3.0


@pytest.mark.parametrize("status", [500, 502, 503, 504, 403, 404, 405, None])
def test_a_5xx_or_anything_else_is_unavailable_with_unknown_state(status):
    with pytest.raises(ProviderUnavailableError) as caught:
        _write_outcome(status, None, headers={}, cookies_sent=True, write=_write())
    assert caught.value.details["transaction_state"] == "unknown"
    assert caught.value.details["http_status"] == status
    assert "transactions" in caught.value.remediation


def test_a_503_from_the_transport_is_sent_once():
    post = FakePost(FakeResponse(status_code=503, body={}))
    with pytest.raises(ProviderUnavailableError):
        _provider(post).set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert len(post.calls) == 1


@pytest.mark.parametrize("exc_name", ["Timeout", "ReadTimeout", "ConnectionError"])
def test_a_timeout_after_sending_is_unknown_state_and_never_retried(exc_name):
    import requests

    post = FakePost(raises=getattr(requests.exceptions, exc_name)("the socket went quiet"))
    with pytest.raises(ProviderUnavailableError) as caught:
        _provider(post).set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert len(post.calls) == 1, "a write whose outcome is unknown must not be re-sent"
    error = caught.value
    assert error.details["transaction_state"] == "unknown"
    assert error.details["cause"] == exc_name
    assert "may have applied" in error.remediation
    assert "`transactions`" in error.remediation and "`claims`" in error.remediation


def test_a_builtin_timeout_from_an_injected_transport_is_classified_the_same_way():
    post = FakePost(raises=TimeoutError("timed out"))
    with pytest.raises(ProviderUnavailableError) as caught:
        _provider(post).move_players(*LEAGUE, "1", adds=[RosterMove("3001")], drops=[], bid=1)
    assert caught.value.details["transaction_state"] == "unknown"
    assert len(post.calls) == 1


def test_the_default_transport_is_requests_post_resolved_at_call_time(monkeypatch):
    """No ``http_post`` -> ``requests.post``, looked up when the write runs."""
    import requests

    post = FakePost(_captured_response("p3-swap-execute"))
    monkeypatch.setattr(requests, "post", post)
    provider = EspnProvider(CREDENTIALS, http=ReplayHttp({"mNav": _nav()}))
    provider.set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert post.calls[0]["url"] == WRITE_URL


@pytest.mark.parametrize(
    "body",
    [b"<html>accepted</html>", ["a", "list"], {"status": "EXECUTED"}, {"id": "x"}],
)
def test_an_unreadable_200_is_drift_and_still_purges(body, tmp_path):
    from fantasy_sports.cache.store import CacheStore

    store = CacheStore(tmp_path / "cache.sqlite3")
    post = FakePost(FakeResponse(status_code=200, body=body))
    provider = _provider(post, cache=store)
    with pytest.raises(SchemaDriftError) as caught:
        provider.set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert caught.value.details["transaction_state"] == "unknown"
    assert provider._leagues == {}, "the write may have applied; the memo must not survive"


def test_a_minimal_200_parses_with_absent_optionals():
    result = _write_outcome(
        200,
        {"id": "t", "status": "EXECUTED", "proposedDate": True, "items": "nope"},
        headers={},
        cookies_sent=True,
        write=_write(),
    )
    assert result.proposed_at is None
    assert result.items == ()
    assert result.member_id is None
    assert result.team_id == "1"
    assert result.bid_amount is None


# --------------------------------------------------------------------------- #
# Cache coherence after a success (R10)
# --------------------------------------------------------------------------- #


def test_a_success_purges_the_league_and_period_tags_and_spares_the_season(tmp_path):
    from fantasy_sports.cache.store import CacheStore
    from fantasy_sports.cache.tags import TagScope, league_tag, scope_of, scoring_period_tag

    store = CacheStore(tmp_path / "cache.sqlite3")
    purged: list[str] = []
    original = store.purge_by_league_tag

    def _spy(tag: str) -> int:
        purged.append(tag)
        return original(tag)

    store.purge_by_league_tag = _spy  # type: ignore[method-assign]
    provider = _provider(cache=store)
    provider.session_role(*LEAGUE)

    def _tags() -> list[tuple[str, ...]]:
        keys = [row[0] for row in store._connect().execute("SELECT key FROM entries")]
        return [store.get(key).tags for key in keys]

    before = _tags()
    assert any(scope_of(tags[0]) is TagScope.SEASON for tags in before)
    assert any(league_tag("espn", "99", 2026) in tags for tags in before)

    provider.set_lineup(*LEAGUE, "1", {"2002": "BE"})

    assert purged == [league_tag("espn", "99", 2026), scoring_period_tag("espn", "99", 2026, 2)]
    after = _tags()
    assert after, "the season-scoped entries must survive a league purge"
    assert all(league_tag("espn", "99", 2026) not in tags for tags in after)
    assert all(all(scope_of(tag) is TagScope.SEASON for tag in tags) for tags in after)
    assert provider._leagues == {}


def test_a_rejection_purges_nothing(tmp_path):
    from fantasy_sports.cache.store import CacheStore

    store = CacheStore(tmp_path / "cache.sqlite3")
    provider = _provider(FakePost(_captured_response("p7-mixed-legal-and-locked")), cache=store)
    with pytest.raises(WriteRejectedError):
        provider.set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert LEAGUE in provider._leagues


def test_a_success_without_a_store_still_drops_the_memoised_league():
    provider = _provider()
    provider.set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert provider._leagues == {}


def test_the_write_response_is_recorded_as_an_uncached_source():
    provider = _provider()
    provider.set_lineup(*LEAGUE, "1", {"2002": "BE"})
    record = provider.last_fetch
    assert record.responses["transactions"].cached is False
    assert record.payload("transactions")["status"] == "EXECUTED"
    assert any(source.name == "transactions" for source in record.sources)
    assert "mNav" in record.responses


# --------------------------------------------------------------------------- #
# Credential age on every result (R12a)
# --------------------------------------------------------------------------- #


def _credential_set(source: str = "keychain"):
    from fantasy_sports.auth.chain import (
        CredentialSet,
        CredentialSource,
        ResolvedCredential,
        Secret,
    )

    return CredentialSet(
        resolved={
            name: ResolvedCredential(
                name=name, source=CredentialSource(source), secret=Secret(value)
            )
            for name, value in CREDENTIALS.items()
        }
    )


def test_a_plain_mapping_reports_age_unknown():
    assert _credentials_block(None) == {
        "espn_s2_age_seconds": None,
        "warning": "credential age unknown",
    }


def test_an_absent_state_file_reports_age_unknown(tmp_path):
    block = _credentials_block(_credential_set(), state_path=tmp_path / "absent.json")
    assert block == {"espn_s2_age_seconds": None, "warning": "credential age unknown"}


def test_a_keychain_cookie_reports_its_age_in_seconds(tmp_path):
    from fantasy_sports.auth.staleness import record_stored

    state = tmp_path / "auth-state.json"
    stored = datetime(2026, 10, 1, tzinfo=UTC)
    record_stored(["espn_s2", "swid"], now=stored, path=state)
    block = _credentials_block(_credential_set(), state_path=state, now=stored + timedelta(hours=2))
    assert block == {"espn_s2_age_seconds": 7200, "warning": None}


def test_a_stale_cookie_carries_the_staleness_warning(tmp_path):
    from fantasy_sports.auth.staleness import record_stored

    state = tmp_path / "auth-state.json"
    stored = datetime(2026, 1, 1, tzinfo=UTC)
    record_stored(["espn_s2", "swid"], now=stored, path=state)
    block = _credentials_block(_credential_set(), state_path=state, now=stored + timedelta(days=90))
    assert block["espn_s2_age_seconds"] == 90 * 86400
    assert "espn_s2" in block["warning"] and "staleness" in block["warning"]


def test_an_environment_cookie_has_no_knowable_age(tmp_path):
    block = _credentials_block(_credential_set("env"), state_path=tmp_path / "s.json")
    assert block["espn_s2_age_seconds"] is None
    assert block["warning"] == "credential age unknown"


def test_a_failing_staleness_report_never_blocks_a_write():
    class _Broken:
        resolved = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        missing = ()

    assert _credentials_block(_Broken())["warning"] == "credential age unknown"


def test_every_result_carries_the_block_computed_before_the_request(tmp_path):
    from fantasy_sports.auth.staleness import record_stored

    state = tmp_path / "auth-state.json"
    stored = datetime(2026, 10, 1, tzinfo=UTC)
    record_stored(["espn_s2", "swid"], now=stored, path=state)
    provider = EspnProvider(
        _credential_set(),
        http=ReplayHttp({"mNav": _nav()}),
        http_post=FakePost(_captured_response("p3-swap-execute")),
        auth_state_path=state,
        now=lambda: stored + timedelta(days=1),
    )
    assert provider.credential_report()["espn_s2_age_seconds"] == 86400
    result = provider.set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert result.credentials == {"espn_s2_age_seconds": 86400, "warning": None}


# --------------------------------------------------------------------------- #
# Nothing a write produces carries a credential
# --------------------------------------------------------------------------- #


def test_no_credential_survives_into_a_result_even_if_espn_echoes_one():
    remember_secret(S2)
    remember_secret(SWID)
    echo = _captured_response("p3-swap-execute")
    echo.body["debug"] = {"cookie": f"espn_s2={S2}", "swid": SWID, "note": f"session {S2}"}
    provider = _provider(FakePost(echo))
    result = provider.set_lineup(*LEAGUE, "1", {"2002": "BE"})
    dumped = json.dumps(result.to_dict(), default=str)
    recorded = json.dumps(provider.last_fetch.payload("transactions"))
    for text in (dumped, recorded):
        assert S2 not in text
        assert SWID not in text and SWID.strip("{}") not in text


def test_no_credential_survives_into_a_rejection():
    remember_secret(S2)
    body = {"messages": [f"bad cookie {S2}"], "details": [{"type": f"TRAN_{S2}"}]}
    with pytest.raises(WriteRejectedError) as caught:
        _write_outcome(409, body, headers={}, cookies_sent=True, write=_write())
    assert S2 not in json.dumps(caught.value.to_dict())
    assert S2 not in repr(vars(caught.value))


def test_no_credential_survives_into_a_transport_failure():
    remember_secret(S2)
    post = FakePost(raises=ConnectionError(f"reset while sending espn_s2={S2}"))
    with pytest.raises(ProviderUnavailableError) as caught:
        _provider(post).set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert S2 not in json.dumps(caught.value.to_dict())


def test_a_write_result_round_trips_through_to_dict():
    result = _provider().set_lineup(*LEAGUE, "1", {"2002": "BE"})
    payload = result.to_dict()
    assert set(payload) == {
        "provider",
        "provider_transaction_id",
        "status",
        "is_pending",
        "team_id",
        "items",
        "credentials",
        "raw",
        "proposed_at",
        "bid_amount",
        "member_id",
    }
    assert isinstance(payload["items"], list)


# --------------------------------------------------------------------------- #
# Edges of the helpers
# --------------------------------------------------------------------------- #


def test_malformed_teams_in_mnav_are_skipped_not_fatal():
    teams = [
        "junk",
        {"id": "x", "owners": [SWID]},
        {"id": 4, "owners": "nope"},
        {"id": 1, "owners": [SWID]},
    ]
    assert _provider(nav=_nav(teams=teams)).session_role(*LEAGUE).owned_team_ids == ("1",)


@pytest.mark.parametrize("value", ["{}", "", "   ", 5, None])
def test_an_empty_or_non_string_member_id_never_matches(value):
    from fantasy_sports.providers.espn import _guid_key

    assert _guid_key(value) is None


@pytest.mark.parametrize("name", ["", "  ", None, 20])
def test_a_blank_or_non_string_slot_name_has_no_id(name):
    from fantasy_sports.providers.espn import _slot_id

    assert _slot_id(name) is None
    assert _slot_id(" be ") == 20


def test_a_rejection_body_with_no_typed_detail_has_no_provider_type():
    from fantasy_sports.providers.espn import _first_detail

    assert _first_detail({"details": [{"type": 3}, "x"], "messages": [7]}) == (None, None)
    assert _first_detail("not a mapping") == (None, None)


def test_a_player_who_publishes_no_eligibility_is_not_refused_on_it():
    from fantasy_sports.providers.espn import _eligible_slot_ids

    assert _eligible_slot_ids({"lineupSlotId": 2}) is None
    assert _eligible_slot_ids({"playerPoolEntry": {"player": {"eligibleSlots": [2, "x"]}}}) == {2}


def test_a_success_without_an_echoed_member_records_none():
    post = FakePost(FakeResponse(body={"id": "t", "status": "EXECUTED", "teamId": 1}))
    provider = _provider(post)
    assert provider.set_lineup(*LEAGUE, "1", {"2002": "BE"}).member_id is None
    assert LEAGUE not in provider._echoed_members


# --------------------------------------------------------------------------- #
# PR #118 review: the diff is against the current roster
# --------------------------------------------------------------------------- #


def test_a_lineup_diff_uses_the_current_roster_not_a_previously_loaded_week():
    """After ``fetch_roster(week=1)`` the instance holds week 1's ``mRoster``.
    In week 1, 2002 sat on the bench; now he is at RB. Diffing against the
    retained week would see no change and refuse, or send a wrong
    ``fromLineupSlotId``."""
    http = ReplayHttp()
    current = http.bootstrap
    week_one = json.loads(json.dumps(current))
    for team in week_one["teams"]:
        for entry in team["roster"]["entries"]:
            if entry["playerId"] == 2002:
                entry["lineupSlotId"] = 20
    asked: list[str | None] = []

    def _roster(params: Mapping[str, str]) -> Any:
        asked.append(params.get("scoringPeriodId"))
        return week_one if params.get("scoringPeriodId") == "1" else current

    http.overrides["mRoster"] = _roster
    http.overrides["mNav"] = _nav()
    post = FakePost(_captured_response("p3-swap-execute"))
    provider = EspnProvider(CREDENTIALS, http=http, http_post=post)
    provider.fetch_roster(*LEAGUE, "1", week=1)
    provider.set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert asked[-1] == "2", "the write's roster read must name the current period"
    assert post.body["items"] == [
        {"playerId": 2002, "type": "LINEUP", "fromLineupSlotId": 2, "toLineupSlotId": 20}
    ]


def test_a_current_roster_without_the_team_is_a_player_not_on_it():
    http = ReplayHttp({"mNav": _nav(), "mRoster": {"teams": []}})
    post = FakePost()
    provider = EspnProvider(CREDENTIALS, http=http, http_post=post)
    with pytest.raises(ConfigInvalidError, match="not on team"):
        provider.set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert post.calls == []


# --------------------------------------------------------------------------- #
# PR #118 review: the SWID and espn_s2 must be the same account
# --------------------------------------------------------------------------- #


def test_a_write_echoing_a_different_member_diverges_and_blocks_later_writes():
    from fantasy_sports.core.errors import WriteDivergedError

    other = _captured_response("p3-swap-execute", memberId=OTHER)
    post = FakePost(other)
    provider = _provider(post)
    with pytest.raises(WriteDivergedError) as caught:
        provider.set_lineup(*LEAGUE, "1", {"2002": "BE"})
    details = caught.value.details
    expected = swid_pseudonym(SWID, salt=CASSETTE_SWID_SALT)
    observed = swid_pseudonym(OTHER, salt=CASSETTE_SWID_SALT)
    assert details["expected_member"] == expected
    assert details["observed_member"] == observed
    assert details["provider_transaction_id"] == "<uuid>"
    assert details["team_id"] == "1"
    assert details["transaction_state"] == "applied"
    assert details["proposed_at"].endswith("+00:00")
    assert len(details["items"]) == 2
    dumped = json.dumps(caught.value.to_dict())
    assert SWID not in dumped and OTHER not in dumped
    assert provider._leagues == {}, "the write applied; the cache must still be purged"
    assert LEAGUE not in provider._echoed_members

    with pytest.raises(WriteRejectedError) as refused:
        provider.cancel_transaction(*LEAGUE, "1", "abc")
    assert refused.value.kind == "not_owned"
    assert "different accounts" in refused.value.remediation
    assert "auth login" in refused.value.remediation
    assert len(post.calls) == 1, "nothing is sent once the pair is known to be split"


def test_a_matching_echo_in_another_case_is_not_a_mismatch():
    echo = _captured_response("p3-swap-execute", memberId=SWID.lower())
    provider = _provider(FakePost(echo))
    assert provider.set_lineup(*LEAGUE, "1", {"2002": "BE"}).status == "EXECUTED"
    assert provider._identity_mismatches == {}


def test_a_mismatch_in_one_league_does_not_block_another():
    provider = _provider(FakePost(_captured_response("p3-swap-execute")))
    provider._identity_mismatches[("100", 2026)] = ("a", "b")
    assert provider.set_lineup(*LEAGUE, "1", {"2002": "BE"}).status == "EXECUTED"


def test_the_lineup_diff_reads_the_roster_live_even_when_it_is_cached(tmp_path):
    """A current-period mRoster under its 5-minute TTL must not be the diff's
    source: the slot could have moved since, on another device."""
    from fantasy_sports.cache.store import CacheStore

    store = CacheStore(tmp_path / "cache.sqlite3")
    http = ReplayHttp({"mNav": _nav()})
    live = http.bootstrap  # 2002 is at RB (slot 2) now
    stale = json.loads(json.dumps(live))
    for team in stale["teams"]:
        for entry in team["roster"]["entries"]:
            if entry["playerId"] == 2002:
                entry["lineupSlotId"] = 20  # on the bench, as of the earlier read
    served = {"body": stale}
    http.overrides["mRoster"] = lambda params: served["body"]

    def _read_roster(provider: EspnProvider) -> Any:
        league = provider._league(*LEAGUE)
        league.espn_request.league_get(params={"view": "mRoster", "scoringPeriodId": 2})
        return provider._transport(*LEAGUE).responses["mRoster@2"]

    _read_roster(EspnProvider(CREDENTIALS, http=http, cache=store))  # caches the stale roster
    served["body"] = live

    # An ordinary read is still served the stale entry from the cache...
    hit = _read_roster(EspnProvider(CREDENTIALS, http=http, cache=store))
    assert hit.cached is True
    assert _team_slot(hit.payload, 2002) == 20

    # ...but the write diffs against the live roster.
    post = FakePost(_captured_response("p3-swap-execute"))
    writer = EspnProvider(CREDENTIALS, http=http, cache=store, http_post=post)
    writer.set_lineup(*LEAGUE, "1", {"2002": "BE"})
    assert post.body["items"] == [
        {"playerId": 2002, "type": "LINEUP", "fromLineupSlotId": 2, "toLineupSlotId": 20}
    ]
    assert writer._cache_mode is None, "the provider's own cache mode is untouched"


def _team_slot(payload: Any, player_id: int) -> int | None:
    for team in payload["teams"]:
        for entry in team["roster"]["entries"]:
            if entry["playerId"] == player_id:
                return entry["lineupSlotId"]
    return None


def test_a_per_call_mode_is_restored_even_when_the_call_fails():
    from fantasy_sports.cache.store import CacheMode

    provider = _provider()
    provider._league(*LEAGUE)
    transport = provider._transport(*LEAGUE)
    with pytest.raises(RuntimeError), transport.mode_for_call(CacheMode.BYPASS):
        raise RuntimeError("boom")
    assert transport._call_mode is None


# --------------------------------------------------------------------------- #
# Add / drop / waiver shapes, captured in a test league (#108)
# --------------------------------------------------------------------------- #

CAPTURES_06 = CAPTURES.parent / "06-espn-add-drop-surface"


def _capture_06(name: str) -> dict[str, Any]:
    return json.loads((CAPTURES_06 / f"{name}.json").read_text())


def _captured_06_response(name: str) -> FakeResponse:
    response = _capture_06(name)["response"]
    body = json.loads(json.dumps(response["body"]).replace("{SWID-REDACTED}", SWID))
    # The captures are team 7 in period 6; the synthetic league is team 1 in period 2.
    if isinstance(body, dict) and body.get("teamId") == 7:
        body["teamId"] = 1
    return FakeResponse(status_code=response["status"], body=body)


def _captured_06_request(name: str) -> dict[str, Any]:
    """A captured request body, re-addressed to the synthetic league's team 1, period 2."""
    body = _capture_06(name)["request"]["body"]
    text = json.dumps(body).replace('"teamId": 7', '"teamId": 1')
    text = text.replace('"toTeamId": 7', '"toTeamId": 1').replace(
        '"fromTeamId": 7', '"fromTeamId": 1'
    )
    body = json.loads(text)
    body["scoringPeriodId"] = 2
    return body


def test_a_bidless_waiver_claim_is_the_captured_body_and_comes_back_pending():
    """FAAB-off leagues reject every bid, so a claim there is ``waiver=True``
    with no ``bidAmount`` -- the body p4 sent and ESPN accepted as PENDING."""
    post = FakePost(_captured_06_response("p4-waiver-claim-with-drop"))
    result = _provider(post).move_players(
        *LEAGUE, "1", adds=[RosterMove("9000030")], drops=[RosterMove("9000023")], waiver=True
    )
    assert post.body == _captured_06_request("p4-waiver-claim-with-drop")
    assert "bidAmount" not in post.body
    assert result.status == "PENDING"
    assert result.is_pending is True
    assert result.provider_transaction_id == "<uuid-1>"
    assert result.bid_amount == 0


def test_a_bid_still_rides_on_the_claim_when_given():
    post = FakePost(_captured_06_response("p4-waiver-claim-with-drop"))
    _provider(post).move_players(
        *LEAGUE, "1", adds=[RosterMove("9000030")], drops=[], waiver=True, bid=0
    )
    assert post.body["type"] == "WAIVER"
    assert post.body["bidAmount"] == 0


def test_an_add_slot_is_sent_as_to_lineup_slot_id_as_captured():
    post = FakePost(_captured_06_response("p7c-claim-add-with-toLineupSlotId-20"))
    _provider(post).move_players(
        *LEAGUE,
        "1",
        adds=[RosterMove("9000030", to_slot="BE")],
        drops=[RosterMove("9000023")],
        waiver=True,
    )
    assert post.body == _captured_06_request("p7c-claim-add-with-toLineupSlotId-20")


def test_a_standalone_drop_is_a_roster_transaction_not_a_free_agent_one():
    """``FREEAGENT`` must carry exactly one ``ADD`` (p6b); a drop alone goes in
    a ``ROSTER`` envelope, which ESPN validated down to the player (p6c)."""
    post = FakePost(_captured_06_response("p6c-roster-drop-player-not-on-team"))
    with pytest.raises(WriteRejectedError) as caught:
        _provider(post).move_players(*LEAGUE, "1", adds=[], drops=[RosterMove("9000030")])
    assert post.body == _captured_06_request("p6c-roster-drop-player-not-on-team")
    assert caught.value.kind == "already_dropped"
    assert caught.value.provider_type == "TRAN_PLAYER_NOT_ON_TEAM"
    assert caught.value.remediation == "Player 9000030 is not on team 7"


def test_an_add_with_a_drop_is_one_free_agent_transaction():
    post = FakePost(_captured_06_response("p4-waiver-claim-with-drop"))
    _provider(post).move_players(
        *LEAGUE, "1", adds=[RosterMove("9000030")], drops=[RosterMove("9000023")]
    )
    assert post.body["type"] == "FREEAGENT"
    assert post.body["items"] == _captured_06_request("p4-waiver-claim-with-drop")["items"]


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"adds": [RosterMove("1"), RosterMove("2")], "drops": []}, "one player"),
        ({"adds": [RosterMove("1"), RosterMove("2")], "drops": [], "waiver": True}, "one player"),
        ({"adds": [], "drops": [RosterMove("1")], "waiver": True}, "must name the player"),
        ({"adds": [], "drops": [RosterMove("1")], "bid": 1}, "must name the player"),
    ],
)
def test_a_transaction_espn_would_refuse_for_its_shape_is_refused_first(kwargs, fragment):
    post = FakePost()
    with pytest.raises(ConfigInvalidError) as caught:
        _provider(post).move_players(*LEAGUE, "1", **kwargs)
    assert caught.value.kind == "argument"
    assert fragment in caught.value.message
    assert post.calls == []


def test_a_cancel_is_the_captured_body_and_returns_a_new_canceled_transaction():
    post = FakePost(_captured_06_response("p5-cancel-waiver-claim"))
    result = _provider(post).cancel_transaction(*LEAGUE, "1", "<uuid-1>")
    assert post.body == _captured_06_request("p5-cancel-waiver-claim")
    assert result.status == "CANCELED"
    assert result.is_pending is False
    assert result.provider_transaction_id == "<uuid-2>"
    assert result.raw["relatedTransactionId"] == "<uuid-1>"


@pytest.mark.parametrize(
    ("capture", "provider_type", "kind"),
    [
        ("p3-waiver-claim-no-bid-no-drop", "TRAN_ROSTER_LIMIT_EXCEEDED_ONE", "roster_full"),
        ("p6c-roster-drop-player-not-on-team", "TRAN_PLAYER_NOT_ON_TEAM", "already_dropped"),
        ("p1-freeagent-add-no-drop", "TRAN_PLAYER_NOT_FREEAGENT", "player_on_waivers"),
        ("p6d-freeagent-add-rostered-elsewhere", "TRAN_PLAYER_NOT_AVAILABLE", "player_unavailable"),
        ("p2-waiver-claim-no-drop-full-roster", "TRAN_ACQUISITION_BUDGET_NOT_SUPPORTED", "unknown"),
        ("p6a-waiver-bid-1000", "TRAN_ACQUISITION_BUDGET_NOT_SUPPORTED", "unknown"),
        ("p6b-drop-player-not-on-team", "TRAN_INVALID_ITEM_TYPE_COUNT", "unknown"),
        ("p7a-claim-add-without-toTeamId", "TRAN_ITEM_TO_TEAM_ID_MISSING", "unknown"),
        ("p7b-claim-drop-without-fromTeamId", "TRAN_ITEM_FROM_TEAM_ID_MISSING", "unknown"),
    ],
)
def test_every_captured_add_drop_rejection_maps_to_its_kind(capture, provider_type, kind):
    response = _captured_06_response(capture)
    with pytest.raises(WriteRejectedError) as caught:
        _write_outcome(
            response.status_code,
            response.body,
            headers={},
            cookies_sent=True,
            write=_write(),
        )
    assert caught.value.http_status == 409
    assert caught.value.provider_type == provider_type
    assert caught.value.kind == kind
    assert caught.value.remediation == response.body["messages"][0]


def test_the_writing_fake_records_the_waiver_flag():
    """A command that forgets ``waiver=True`` must fail a forwarding assertion."""
    from _fake_provider import WritingFake

    fake = WritingFake()
    fake.move_players("99", 2026, "1", adds=[RosterMove("5")], drops=[], waiver=True)
    fake.move_players("99", 2026, "1", adds=[RosterMove("5")], drops=[])
    assert fake.calls[-2][1]["waiver"] is True
    assert fake.calls[-1][1]["waiver"] is False
