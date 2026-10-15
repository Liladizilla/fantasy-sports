"""The HTTP-layer cache (jwulff/fantasy-sports#8).

Three things in here are security controls rather than behaviour checks, and
they are written to fail closed:

* the stored row is read back **out of SQLite** rather than trusting the value
  a function returned — the bytes on disk are what leaks;
* the redaction fixture starts from a **gzipped** body, because a scrubber
  handed compressed bytes matches nothing and reports success
  (``docs/memory/cassette-scrubbing-blind-spots.md``);
* no test may touch the developer's real cache directory. ``HOME`` is
  redirected *and* every XDG variable is unset, per
  ``docs/memory/config-toml-is-a-shared-namespace.md`` §2.
"""

from __future__ import annotations

import ast
import gzip
import json
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from fantasy_sports.cache.store import (
    DEFAULT_CACHE_FILENAME,
    CacheMode,
    CacheStore,
    CachingFetcher,
    cache_key,
    canonical_url,
)
from fantasy_sports.cache.tags import (
    FOREVER,
    RequestContext,
    Resource,
    TagScope,
    league_tag,
    scope_of,
    scoring_period_tag,
    season_tag,
    tags_for,
    ttl_for,
)
from fantasy_sports.core.redaction import (
    CASSETTE_SWID_SALT,
    CREDENTIAL_PATTERNS,
    is_swid_pseudonym,
    swid_pseudonym,
)

XDG_VARS = ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")

# --- synthetic credentials, shaped like the real thing --------------------- #

FAKE_SWID = "{0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0}"
OTHER_SWID = "{99887766-5544-3322-1100-AABBCCDDEEFF}"
FAKE_ESPN_S2 = "AEBnotarealcookie0123456789abcdefABCDEF%2Bnotareal%3D%3D"

SEASON_ROOT = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026"
LEAGUE_URL = f"{SEASON_ROOT}/segments/0/leagues/123456"
PLAYERS_URL = f"{SEASON_ROOT}/players"
SCHEDULES_URL = f"{SEASON_ROOT}/proTeamSchedules"

ROSTER_BODY_WITH_SWID = (
    '{"teams": [{"id": 1, "owners": ["' + FAKE_SWID + '"]}],'
    ' "members": [{"id": "' + FAKE_SWID + '", "firstName": "Jo"}]}'
)


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No test may see the real home directory or the real XDG environment."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in XDG_VARS:
        monkeypatch.delenv(var, raising=False)
    return home


class Clock:
    """A hand-cranked ``time.time``. TTL tests must not sleep for five minutes."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingFetch:
    """A stand-in for the provider's HTTP transport that counts its calls."""

    def __init__(
        self,
        bodies: Mapping[str, bytes | str] | None = None,
        *,
        default: bytes | str = b'{"ok": true}',
        delay: float = 0.0,
    ) -> None:
        self.bodies = dict(bodies or {})
        self.default = default
        self.delay = delay
        self.calls: list[str] = []

    def __call__(self, url: str, params: Mapping[str, object] | None = None) -> bytes | str:
        self.calls.append(url)
        if self.delay:
            time.sleep(self.delay)
        return self.bodies.get(url, self.default)


def roster_context(week: int = 3, **kwargs: object) -> RequestContext:
    return RequestContext(
        provider="espn",
        resource=Resource.ROSTER,
        season=2026,
        league_id="123456",
        week=week,
        **kwargs,  # type: ignore[arg-type]
    )


def players_context() -> RequestContext:
    """``players_wl`` — fetched against a season endpoint carrying no league id.

    ``league_id`` is supplied on purpose: a season-scoped resource must ignore
    it, and a test that never passes one could not tell.
    """
    return RequestContext(
        provider="espn",
        resource=Resource.PLAYERS,
        season=2026,
        league_id="123456",
    )


def store_at(tmp_path: Path, clock: Clock | None = None) -> CacheStore:
    return CacheStore(tmp_path / "cache.sqlite3", now=clock or Clock())


def raw_rows(path: Path) -> list[tuple[object, ...]]:
    """Every stored body, read straight out of SQLite.

    Deliberately not a :class:`CacheStore` call: a redaction assertion that
    goes through the class it is testing can be satisfied by a getter that
    scrubs on read while the disk holds the credential.
    """
    connection = sqlite3.connect(path)
    try:
        return list(connection.execute("SELECT key, url, body FROM entries"))
    finally:
        connection.close()


def stored_text(path: Path) -> str:
    """Every byte the cache file holds, as text. Includes the SQLite page bytes."""
    return path.read_bytes().decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Keys: URL plus params, below the composite calls
# --------------------------------------------------------------------------- #


def test_key_is_stable_across_parameter_order():
    """Two spellings of one request must be one cache entry, or nothing hits."""
    first = cache_key(LEAGUE_URL, {"view": "mTeam", "scoringPeriodId": 3})
    second = cache_key(LEAGUE_URL, {"scoringPeriodId": 3, "view": "mTeam"})
    assert first == second


def test_key_separates_different_parameters():
    assert cache_key(LEAGUE_URL, {"view": "mTeam"}) != cache_key(LEAGUE_URL, {"view": "mRoster"})


def test_key_folds_query_string_and_explicit_params_together():
    """``?view=mTeam`` and ``params={"view": "mTeam"}`` are the same request."""
    assert cache_key(f"{LEAGUE_URL}?view=mTeam") == cache_key(LEAGUE_URL, {"view": "mTeam"})


def test_key_ignores_credential_query_parameters():
    """A rotated cookie must not silently invalidate the whole cache."""
    with_credentials = cache_key(
        LEAGUE_URL, {"view": "mTeam", "espn_s2": FAKE_ESPN_S2, "SWID": FAKE_SWID}
    )
    assert with_credentials == cache_key(LEAGUE_URL, {"view": "mTeam"})


def test_canonical_url_strips_credentials_from_the_query():
    canonical = canonical_url(LEAGUE_URL, {"espn_s2": FAKE_ESPN_S2, "SWID": FAKE_SWID})
    assert FAKE_ESPN_S2 not in canonical
    assert FAKE_SWID not in canonical


def test_key_distinguishes_an_extra_dimension():
    """ESPN's free-agent filter travels in ``x-fantasy-filter``, not the URL."""
    plain = cache_key(LEAGUE_URL, {"view": "kona_player_info"})
    filtered = cache_key(
        LEAGUE_URL, {"view": "kona_player_info"}, extra={"x-fantasy-filter": '{"players":{}}'}
    )
    assert plain != filtered


# --------------------------------------------------------------------------- #
# Two tag classes
# --------------------------------------------------------------------------- #


def test_a_league_resource_carries_league_season_and_period_tags():
    tags = tags_for(roster_context(week=3))
    assert set(tags) == {
        league_tag("espn", "123456", 2026),
        season_tag("espn", 2026),
        scoring_period_tag("espn", "123456", 2026, 3),
    }


def test_a_season_resource_never_carries_a_league_tag():
    """The crux of the two-class split.

    ``players_wl`` is fetched from a season endpoint with no league id in it,
    and both free agents and box scores re-fetch it. A league tag here would
    either split the entry per league — losing the cross-league hit — or let a
    purge after a write to one league evict the whole-season player map.
    """
    tags = tags_for(players_context())
    assert tags == (season_tag("espn", 2026),)
    assert all(scope_of(tag) is TagScope.SEASON for tag in tags)


def test_a_league_resource_with_no_week_carries_no_period_tag():
    """League settings are not a weekly thing; inventing a week would split them."""
    tags = tags_for(
        RequestContext(
            provider="espn", resource=Resource.LEAGUE_SETTINGS, season=2026, league_id="123456"
        )
    )
    assert set(tags) == {league_tag("espn", "123456", 2026), season_tag("espn", 2026)}


def test_the_two_season_scoped_resources_share_one_tag():
    assert tags_for(players_context()) == tags_for(
        RequestContext(
            provider="espn", resource=Resource.PRO_TEAM_SCHEDULES, season=2026, league_id="123456"
        )
    )


def test_league_and_period_tags_are_league_scoped():
    assert scope_of(league_tag("espn", "123456", 2026)) is TagScope.LEAGUE
    assert scope_of(scoring_period_tag("espn", "123456", 2026, 3)) is TagScope.LEAGUE
    assert scope_of(season_tag("espn", 2026)) is TagScope.SEASON


def test_a_league_resource_without_a_league_id_is_a_programming_error():
    with pytest.raises(ValueError, match="league_id"):
        tags_for(RequestContext(provider="espn", resource=Resource.ROSTER, season=2026))


def test_scope_of_rejects_an_unknown_tag():
    with pytest.raises(ValueError):
        scope_of("nonsense:whatever")


# --------------------------------------------------------------------------- #
# TTL by resource type (ARCHITECTURE §8)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        (Resource.ROSTER, 300),
        (Resource.FREE_AGENTS, 300),
        (Resource.STANDINGS, 900),
        (Resource.MATCHUPS, 900),
        (Resource.LEAGUE_SETTINGS, 86_400),
    ],
)
def test_ttl_follows_the_resource_table(resource: Resource, expected: int):
    context = RequestContext(
        provider="espn", resource=resource, season=2026, league_id="123456", week=3
    )
    assert ttl_for(context) == expected


def test_a_completed_week_caches_forever():
    assert ttl_for(roster_context(week=1, completed=True)) is FOREVER


def test_a_historical_season_caches_forever():
    context = RequestContext(
        provider="espn",
        resource=Resource.STANDINGS,
        season=2018,
        league_id="1234",
        current_season=2026,
    )
    assert ttl_for(context) is FOREVER


# --------------------------------------------------------------------------- #
# Hits, misses, and TTL
# --------------------------------------------------------------------------- #


def test_a_repeat_call_inside_ttl_hits_the_cache(tmp_path: Path):
    clock = Clock()
    fetch = RecordingFetch()
    fetcher = CachingFetcher(fetch, store_at(tmp_path, clock))
    context = roster_context()

    first = fetcher.fetch(LEAGUE_URL, {"view": "mRoster"}, context=context)
    clock.advance(60)
    second = fetcher.fetch(LEAGUE_URL, {"view": "mRoster"}, context=context)

    assert fetch.calls == [LEAGUE_URL]
    assert first.cached is False
    assert second.cached is True
    assert second.body == first.body


def test_a_call_outside_ttl_refetches(tmp_path: Path):
    clock = Clock()
    fetch = RecordingFetch()
    fetcher = CachingFetcher(fetch, store_at(tmp_path, clock))
    context = roster_context()

    fetcher.fetch(LEAGUE_URL, {"view": "mRoster"}, context=context)
    clock.advance(301)
    second = fetcher.fetch(LEAGUE_URL, {"view": "mRoster"}, context=context)

    assert len(fetch.calls) == 2
    assert second.cached is False


def test_a_forever_entry_survives_an_absurd_clock_jump(tmp_path: Path):
    clock = Clock()
    fetch = RecordingFetch()
    fetcher = CachingFetcher(fetch, store_at(tmp_path, clock))
    context = roster_context(week=1, completed=True)

    fetcher.fetch(LEAGUE_URL, {"view": "mRoster"}, context=context)
    clock.advance(86_400 * 400)
    second = fetcher.fetch(LEAGUE_URL, {"view": "mRoster"}, context=context)

    assert fetch.calls == [LEAGUE_URL]
    assert second.cached is True


def test_a_composite_call_hits_on_the_shared_sub_request(tmp_path: Path):
    """Why the cache sits below the composite calls (ARCHITECTURE §14.4).

    ``box_scores()`` and ``free_agents()`` each fan out into several sequential
    ESPN requests with no internal dedup, and they share ``players_wl``.
    Cached at the command layer, the second command still pays every round
    trip; keyed on URL plus params, it pays only for what it did not share.
    """
    clock = Clock()
    fetch = RecordingFetch()
    fetcher = CachingFetcher(fetch, store_at(tmp_path, clock))

    box_scores = [
        (LEAGUE_URL, {"view": "mMatchupScore"}, roster_context()),
        (LEAGUE_URL, {"view": "mScoreboard"}, roster_context()),
        (PLAYERS_URL, {"view": "players_wl"}, players_context()),
    ]
    free_agents = [
        (LEAGUE_URL, {"view": "kona_player_info"}, roster_context()),
        (PLAYERS_URL, {"view": "players_wl"}, players_context()),
    ]

    for url, params, context in box_scores:
        fetcher.fetch(url, params, context=context)
    calls_after_box_scores = len(fetch.calls)

    results = [fetcher.fetch(url, params, context=context) for url, params, context in free_agents]

    assert calls_after_box_scores == 3
    assert len(fetch.calls) == 4, "free agents re-fetched the shared players_wl sub-request"
    assert [r.cached for r in results] == [False, True]


def test_a_second_league_hits_the_shared_season_entry(tmp_path: Path):
    """The cross-league hit a league-scoped tag would have thrown away."""
    fetch = RecordingFetch()
    fetcher = CachingFetcher(fetch, store_at(tmp_path))

    fetcher.fetch(PLAYERS_URL, {"view": "players_wl"}, context=players_context())
    other_league = RequestContext(
        provider="espn", resource=Resource.PLAYERS, season=2026, league_id="999999"
    )
    second = fetcher.fetch(PLAYERS_URL, {"view": "players_wl"}, context=other_league)

    assert fetch.calls == [PLAYERS_URL]
    assert second.cached is True


# --------------------------------------------------------------------------- #
# --fresh and --no-cache
# --------------------------------------------------------------------------- #


def test_fresh_bypasses_the_read_and_updates_the_entry(tmp_path: Path):
    """AE1. ``--fresh`` is a refresh, not a bypass — the next call must hit."""
    clock = Clock()
    fetch = RecordingFetch(default=b'{"n": 1}')
    store = store_at(tmp_path, clock)
    context = roster_context()

    CachingFetcher(fetch, store).fetch(LEAGUE_URL, None, context=context)
    fetch.default = b'{"n": 2}'

    refreshed = CachingFetcher(fetch, store, mode=CacheMode.FRESH).fetch(
        LEAGUE_URL, None, context=context
    )
    assert refreshed.cached is False
    assert len(fetch.calls) == 2

    after = CachingFetcher(fetch, store).fetch(LEAGUE_URL, None, context=context)
    assert after.cached is True
    assert after.body == '{"n": 2}'
    assert len(fetch.calls) == 2


def test_no_cache_bypasses_without_writing(tmp_path: Path):
    fetch = RecordingFetch()
    store = store_at(tmp_path)

    result = CachingFetcher(fetch, store, mode=CacheMode.BYPASS).fetch(
        LEAGUE_URL, None, context=roster_context()
    )

    assert result.cached is False
    assert result.stored is False
    assert store.get(cache_key(LEAGUE_URL)) is None


def test_no_cache_neither_reads_nor_overwrites_an_existing_entry(tmp_path: Path):
    fetch = RecordingFetch(default=b'{"n": 1}')
    store = store_at(tmp_path)
    context = roster_context()

    CachingFetcher(fetch, store).fetch(LEAGUE_URL, None, context=context)
    fetch.default = b'{"n": 2}'
    bypassed = CachingFetcher(fetch, store, mode=CacheMode.BYPASS).fetch(
        LEAGUE_URL, None, context=context
    )

    assert bypassed.body == '{"n": 2}'
    entry = store.get(cache_key(LEAGUE_URL))
    assert entry is not None
    assert entry.body == '{"n": 1}', "--no-cache wrote to the store"


# --------------------------------------------------------------------------- #
# fetched_at (jwulff/fantasy-sports#51)
# --------------------------------------------------------------------------- #


def test_a_hit_reports_the_entrys_stored_at_not_the_call_time(tmp_path: Path):
    """The whole bug: a hit must carry *when the bytes were written*, not now."""
    clock = Clock()
    fetch = RecordingFetch(default=b'{"n": 1}')
    store = store_at(tmp_path, clock)
    context = roster_context()

    written = CachingFetcher(fetch, store).fetch(LEAGUE_URL, None, context=context)
    assert written.cached is False
    assert written.fetched_at is None, "a live fetch is 'now'; the caller already knows that"

    clock.advance(90)
    hit = CachingFetcher(fetch, store).fetch(LEAGUE_URL, None, context=context)
    assert hit.cached is True
    assert hit.fetched_at == clock.now - 90, "must be the write time, not this call's time"


def test_a_miss_that_writes_also_leaves_fetched_at_none(tmp_path: Path):
    """A miss is a live fetch too — the caller's own clock already knows 'now'."""
    fetch = RecordingFetch(default=b'{"n": 1}')
    store = store_at(tmp_path)

    result = CachingFetcher(fetch, store).fetch(LEAGUE_URL, None, context=roster_context())

    assert result.cached is False
    assert result.stored is True
    assert result.fetched_at is None


def test_fresh_and_bypass_both_leave_fetched_at_none(tmp_path: Path):
    """Neither is a hit, so neither may report the old entry's write time."""
    clock = Clock()
    fetch = RecordingFetch(default=b'{"n": 1}')
    store = store_at(tmp_path, clock)
    context = roster_context()

    CachingFetcher(fetch, store).fetch(LEAGUE_URL, None, context=context)
    clock.advance(300)

    refreshed = CachingFetcher(fetch, store, mode=CacheMode.FRESH).fetch(
        LEAGUE_URL, None, context=context
    )
    bypassed = CachingFetcher(fetch, store, mode=CacheMode.BYPASS).fetch(
        LEAGUE_URL, None, context=context
    )

    assert refreshed.cached is False
    assert refreshed.fetched_at is None
    assert bypassed.cached is False
    assert bypassed.fetched_at is None


# --------------------------------------------------------------------------- #
# Purge by tag
# --------------------------------------------------------------------------- #


def test_purge_by_tag_removes_matching_entries_and_leaves_others(tmp_path: Path):
    store = store_at(tmp_path)
    store.put("a", "one", tags=(league_tag("espn", "111", 2026),), ttl=300)
    store.put("b", "two", tags=(league_tag("espn", "222", 2026),), ttl=300)

    removed = store.purge_by_tag(league_tag("espn", "111", 2026))

    assert removed == 1
    assert store.get("a") is None
    assert store.get("b") is not None


def test_purge_by_league_tag_leaves_the_season_scoped_entry_intact(tmp_path: Path):
    """The failure the second tag class exists to prevent.

    A write to one league must not evict the whole-season player map that every
    league shares.
    """
    fetch = RecordingFetch()
    store = store_at(tmp_path)
    fetcher = CachingFetcher(fetch, store)
    fetcher.fetch(LEAGUE_URL, {"view": "mRoster"}, context=roster_context())
    fetcher.fetch(PLAYERS_URL, {"view": "players_wl"}, context=players_context())
    fetcher.fetch(SCHEDULES_URL, {"view": "proTeamSchedules_wl"}, context=players_context())

    removed = store.purge_by_league_tag(league_tag("espn", "123456", 2026))

    assert removed == 1
    assert store.get(cache_key(LEAGUE_URL, {"view": "mRoster"})) is None
    assert store.get(cache_key(PLAYERS_URL, {"view": "players_wl"})) is not None
    assert store.get(cache_key(SCHEDULES_URL, {"view": "proTeamSchedules_wl"})) is not None


def test_purge_by_league_tag_refuses_a_season_tag(tmp_path: Path):
    """Called with a season tag it would evict every league's shared entries."""
    store = store_at(tmp_path)
    with pytest.raises(ValueError, match="league-scoped"):
        store.purge_by_league_tag(season_tag("espn", 2026))


def test_purging_one_tag_leaves_the_other_tags_of_a_surviving_entry(tmp_path: Path):
    store = store_at(tmp_path)
    tags = tags_for(roster_context(week=3))
    store.put("a", "one", tags=tags, ttl=300)

    assert store.purge_by_tag(scoring_period_tag("espn", "123456", 2026, 9)) == 0
    entry = store.get("a")
    assert entry is not None
    assert set(entry.tags) == set(tags)


def test_clear_empties_the_store(tmp_path: Path):
    store = store_at(tmp_path)
    store.put("a", "one", tags=(season_tag("espn", 2026),), ttl=300)
    store.put("b", "two", tags=(season_tag("espn", 2026),), ttl=300)

    assert store.clear() == 2
    assert store.get("a") is None
    assert raw_rows(store.path) == []


# --------------------------------------------------------------------------- #
# Redaction — the gzip trap
# --------------------------------------------------------------------------- #


def test_the_gzip_trap_is_real():
    """A control. Without it the redaction test below proves nothing.

    Scrubbing before decoding is a no-op that reports success: the regex finds
    nothing in a gzip stream, so the body "passes" with the credential intact.
    """
    compressed = gzip.compress(ROSTER_BODY_WITH_SWID.encode())
    as_bytes_text = compressed.decode("utf-8", errors="replace")

    assert FAKE_SWID not in as_bytes_text, "fixture is not actually compressed"
    assert not CREDENTIAL_PATTERNS["SWID GUID"].search(as_bytes_text)
    assert FAKE_SWID in gzip.decompress(compressed).decode()


def test_a_gzipped_body_is_decoded_before_it_is_scrubbed(tmp_path: Path):
    """The stored row is read out of SQLite, not taken from the return value."""
    fetch = RecordingFetch(default=gzip.compress(ROSTER_BODY_WITH_SWID.encode()))
    store = store_at(tmp_path)
    CachingFetcher(fetch, store).fetch(LEAGUE_URL, None, context=roster_context())

    rows = raw_rows(store.path)
    assert len(rows) == 1
    body = rows[0][2]
    body_text = body.decode() if isinstance(body, bytes) else body

    assert FAKE_SWID not in body_text
    assert is_swid_pseudonym(json.loads(body_text)["teams"][0]["owners"][0]), (
        "the body was stored still compressed"
    )
    assert '"firstName": "Jo"' in body_text, "the body was never decompressed"
    assert FAKE_SWID not in stored_text(store.path)


# --------------------------------------------------------------------------- #
# Redaction — the owner-to-member join (jwulff/fantasy-sports#38)
# --------------------------------------------------------------------------- #


def _stored_payload(store: CacheStore) -> dict:
    rows = raw_rows(store.path)
    assert len(rows) == 1
    body = rows[0][2]
    return json.loads(body.decode() if isinstance(body, bytes) else body)


def test_the_owner_to_member_join_survives_a_round_trip_through_sqlite(tmp_path: Path):
    """One shared placeholder collapses the join; a per-GUID pseudonym does not.

    Read back out of SQLite rather than off the return value: the bytes on
    disk are what the next process joins against.
    """
    body = (
        '{"teams": [{"id": 1, "owners": ["' + FAKE_SWID + '"]},'
        ' {"id": 2, "owners": ["' + OTHER_SWID + '"]}],'
        ' "members": [{"id": "' + FAKE_SWID + '"}, {"id": "' + OTHER_SWID + '"}]}'
    )
    store = store_at(tmp_path)
    CachingFetcher(RecordingFetch(default=body.encode()), store).fetch(
        LEAGUE_URL, None, context=roster_context()
    )

    payload = _stored_payload(store)
    owners = [team["owners"][0] for team in payload["teams"]]
    members = [member["id"] for member in payload["members"]]

    assert FAKE_SWID not in stored_text(store.path)
    assert OTHER_SWID not in stored_text(store.path)
    assert owners[0] != owners[1], "two distinct members collapsed onto one token"
    assert owners == members, "the join key no longer joins"
    assert all(is_swid_pseudonym(value) for value in owners + members)


def test_the_store_salt_is_random_per_store(tmp_path: Path):
    """A cache is never committed, so it gets unlinkability the cassette cannot.

    If this ever reverts to the public cassette salt the pseudonyms in one
    developer's store become confirmable against a real SWID by anyone.
    """
    one = CacheStore(tmp_path / "one.sqlite3", now=Clock())
    two = CacheStore(tmp_path / "two.sqlite3", now=Clock())

    assert one.swid_salt != two.swid_salt
    assert one.swid_salt != CASSETTE_SWID_SALT
    assert swid_pseudonym(FAKE_SWID, salt=one.swid_salt) != swid_pseudonym(
        FAKE_SWID, salt=CASSETTE_SWID_SALT
    )


def test_two_entries_written_in_different_sessions_agree_about_one_member(tmp_path: Path):
    """Why the salt lives in the file with the rows it salted.

    A salt held only in memory is a fresh salt on the next run, and then two
    entries naming the same member carry two different pseudonyms — the exact
    disagreement #38 exists to prevent, and one no single-session test sees.
    """
    path = tmp_path / "cache.sqlite3"
    body = '{"id": "' + FAKE_SWID + '"}'

    first = CacheStore(path, now=Clock())
    salt = first.swid_salt
    first.put("monday", body, tags=(), ttl=300)
    first.close()

    second = CacheStore(path, now=Clock())
    second.put("tuesday", body, tags=(), ttl=300)

    bodies = {row[0]: json.loads(row[2])["id"] for row in raw_rows(path)}
    assert bodies["monday"] == bodies["tuesday"], (
        "the same member got two pseudonyms across two sessions"
    )
    assert second.swid_salt == salt


def test_discarding_the_store_loses_the_salt_with_the_entries(tmp_path: Path):
    """The two must go together. A rotated salt would make old and new entries
    disagree about the same member — the precise failure #38 exists to prevent."""
    path = tmp_path / "cache.sqlite3"
    store = CacheStore(path, now=Clock())
    salt = store.swid_salt
    store.close()

    path.write_bytes(b"this is not a database")
    rebuilt = CacheStore(path, now=Clock())
    rebuilt.put("a", '{"id": "' + FAKE_SWID + '"}', tags=(), ttl=300)

    assert rebuilt.swid_salt != salt
    assert _stored_payload(rebuilt)["id"] == swid_pseudonym(FAKE_SWID, salt=rebuilt.swid_salt)


def test_a_hit_and_a_miss_agree_on_every_pseudonym(tmp_path: Path):
    """The miss is scrubbed with the store's salt, not the public default.

    Otherwise the adapter's owner-to-member join would resolve differently on
    the first run than on the second, which is the worst place for it to show.
    """
    body = (
        '{"teams": [{"owners": ["' + FAKE_SWID + '"]}], "members": [{"id": "' + FAKE_SWID + '"}]}'
    )
    store = store_at(tmp_path)
    fetcher = CachingFetcher(RecordingFetch(default=body.encode()), store)

    miss = fetcher.fetch(LEAGUE_URL, None, context=roster_context())
    hit = fetcher.fetch(LEAGUE_URL, None, context=roster_context())

    assert hit.cached is True
    assert miss.body == hit.body
    assert swid_pseudonym(FAKE_SWID, salt=store.swid_salt) in miss.body


def test_re_storing_a_body_does_not_re_pseudonymise_it(tmp_path: Path):
    """``--fresh`` rewrites the row. A second scrub must be a no-op, or the
    refreshed entry would disagree with a sibling entry naming the same member."""
    body = '{"id": "' + FAKE_SWID + '"}'
    store = store_at(tmp_path)
    fetcher = CachingFetcher(RecordingFetch(default=body.encode()), store)

    fetcher.fetch(LEAGUE_URL, None, context=roster_context())
    once = _stored_payload(store)["id"]
    CachingFetcher(RecordingFetch(default=body.encode()), store, mode=CacheMode.FRESH).fetch(
        LEAGUE_URL, None, context=roster_context()
    )

    assert _stored_payload(store)["id"] == once


def test_a_plain_body_containing_a_swid_is_scrubbed_before_the_write(tmp_path: Path):
    fetch = RecordingFetch(default=ROSTER_BODY_WITH_SWID)
    store = store_at(tmp_path)
    CachingFetcher(fetch, store).fetch(LEAGUE_URL, None, context=roster_context())

    assert FAKE_SWID not in stored_text(store.path)
    for name, pattern in CREDENTIAL_PATTERNS.items():
        assert not pattern.search(stored_text(store.path)), name


def test_espn_s2_and_swid_are_never_persisted(tmp_path: Path):
    """Both credential channels at once: the request URL and the response body."""
    body = f'{{"cookie": "espn_s2={FAKE_ESPN_S2}; SWID={FAKE_SWID}"}}'
    fetch = RecordingFetch(default=body.encode())
    store = store_at(tmp_path)
    CachingFetcher(fetch, store).fetch(
        LEAGUE_URL,
        {"view": "mTeam", "espn_s2": FAKE_ESPN_S2, "SWID": FAKE_SWID},
        context=roster_context(),
    )

    on_disk = stored_text(store.path)
    assert FAKE_ESPN_S2 not in on_disk
    assert FAKE_SWID not in on_disk
    for name, pattern in CREDENTIAL_PATTERNS.items():
        assert not pattern.search(on_disk), name


def test_a_hit_and_a_miss_return_the_same_bytes(tmp_path: Path):
    """A cache must not change what the adapter parses.

    The stored body is redacted, so the miss returns the redacted body too.
    Returning the live body on a miss and the redacted one on a hit would make
    normalized output depend on cache state, which is worse than either.
    """
    fetch = RecordingFetch(default=ROSTER_BODY_WITH_SWID.encode())
    fetcher = CachingFetcher(fetch, store_at(tmp_path))
    miss = fetcher.fetch(LEAGUE_URL, None, context=roster_context())
    hit = fetcher.fetch(LEAGUE_URL, None, context=roster_context())

    assert hit.cached is True
    assert miss.body == hit.body
    assert FAKE_SWID not in miss.body


def test_a_body_that_cannot_be_decoded_is_not_stored(tmp_path: Path):
    """Bytes we cannot read cannot be proven clean, so they do not land on disk."""
    fetch = RecordingFetch(default=b"\xff\xfe\x00 not utf-8 and not gzip")
    store = store_at(tmp_path)
    result = CachingFetcher(fetch, store).fetch(LEAGUE_URL, None, context=roster_context())

    assert result.stored is False
    assert raw_rows(store.path) == []
    assert "not utf-8 and not gzip" in result.body, "the read failed because of the cache"


def test_an_undecodable_body_is_still_scrubbed_on_its_way_back(tmp_path: Path):
    """It is not stored, but it is still not allowed to carry a credential out."""
    fetch = RecordingFetch(default=b"\xff\xfe" + ROSTER_BODY_WITH_SWID.encode())
    result = CachingFetcher(fetch, store_at(tmp_path)).fetch(
        LEAGUE_URL, None, context=roster_context()
    )

    assert result.stored is False
    assert FAKE_SWID not in result.body


def test_no_cache_also_returns_an_undecodable_body(tmp_path: Path):
    fetch = RecordingFetch(default=b"\xff\xfe\x00 not utf-8")
    result = CachingFetcher(fetch, store_at(tmp_path), mode=CacheMode.BYPASS).fetch(
        LEAGUE_URL, None, context=roster_context()
    )

    assert result.cached is False
    assert result.stored is False
    assert "not utf-8" in result.body


def test_the_url_column_never_holds_a_credential(tmp_path: Path):
    fetch = RecordingFetch()
    store = store_at(tmp_path)
    CachingFetcher(fetch, store).fetch(
        LEAGUE_URL, {"espn_s2": FAKE_ESPN_S2, "SWID": OTHER_SWID}, context=roster_context()
    )

    stored_url = raw_rows(store.path)[0][1]
    assert FAKE_ESPN_S2 not in stored_url
    assert OTHER_SWID not in stored_url


# --------------------------------------------------------------------------- #
# The file itself
# --------------------------------------------------------------------------- #


def test_the_cache_file_is_created_private(tmp_path: Path):
    store = store_at(tmp_path)
    store.put("a", "one", tags=(), ttl=300)

    assert store.path.stat().st_mode & 0o777 == 0o600


def test_the_default_path_follows_xdg_cache_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    xdg = tmp_path / "xdg-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))

    store = CacheStore()

    assert store.path == xdg / "fantasy-sports" / DEFAULT_CACHE_FILENAME


def test_the_default_path_falls_back_to_home_cache(isolated_home: Path):
    assert CacheStore().path == isolated_home / ".cache" / "fantasy-sports" / DEFAULT_CACHE_FILENAME


def test_a_corrupt_cache_file_degrades_to_a_live_fetch(tmp_path: Path):
    path = tmp_path / "cache.sqlite3"
    path.write_bytes(b"this is emphatically not a SQLite database\n" * 64)

    fetch = RecordingFetch(default=b'{"live": true}')
    result = CachingFetcher(fetch, CacheStore(path)).fetch(
        LEAGUE_URL, None, context=roster_context()
    )

    assert result.body == '{"live": true}'
    assert fetch.calls == [LEAGUE_URL]


def test_a_corrupt_cache_file_is_rebuilt_so_later_calls_hit(tmp_path: Path):
    path = tmp_path / "cache.sqlite3"
    path.write_bytes(b"garbage" * 512)

    fetch = RecordingFetch()
    fetcher = CachingFetcher(fetch, CacheStore(path))
    fetcher.fetch(LEAGUE_URL, None, context=roster_context())
    second = fetcher.fetch(LEAGUE_URL, None, context=roster_context())

    assert second.cached is True
    assert fetch.calls == [LEAGUE_URL]


def test_an_unusable_cache_directory_still_serves_reads(tmp_path: Path):
    """A store that cannot be opened at all is not an error the user sees."""
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file where a directory was wanted")
    store = CacheStore(blocker / "nested" / "cache.sqlite3")

    fetch = RecordingFetch(default=b'{"live": true}')
    result = CachingFetcher(fetch, store).fetch(LEAGUE_URL, None, context=roster_context())

    assert store.available is False
    assert result.body == '{"live": true}'
    assert result.stored is False
    assert store.get("anything") is None
    assert store.purge_by_tag(league_tag("espn", "1", 2026)) == 0
    assert store.clear() == 0


def broken_store(tmp_path: Path) -> CacheStore:
    """A store whose connection dies after the file opened cleanly.

    A disk filling up, a database locked by another process, a file truncated
    under us. Reaching into ``_connection`` is the only way to produce one of
    those deterministically; what the tests below assert is the public
    contract, which is that every entry point degrades to "empty cache".
    """
    store = CacheStore(tmp_path / f"cache-{tmp_path.stat().st_ino}.sqlite3", now=Clock())
    store.put("a", "one", tags=(league_tag("espn", "111", 2026),), ttl=300)
    store._connection.close()  # every later statement now raises ProgrammingError
    return store


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda store: store.get("a"), None),
        (lambda store: store.put("b", "two", tags=(), ttl=300), False),
        (lambda store: store.purge_by_tag(league_tag("espn", "111", 2026)), 0),
        (lambda store: store.clear(), 0),
    ],
    ids=["get", "put", "purge_by_tag", "clear"],
)
def test_an_error_part_way_through_gives_up_without_raising(tmp_path, call, expected):
    store = broken_store(tmp_path)

    assert call(store) == expected
    assert store.available is False


def test_put_refuses_a_body_it_cannot_decode(tmp_path: Path):
    """The guarantee at the store's own front door, not only through the fetcher."""
    store = store_at(tmp_path)
    assert store.available is True

    assert store.put("a", b"\xff\xfe\x00 not utf-8", tags=(), ttl=300) is False
    assert raw_rows(store.path) == []


def test_a_store_that_gave_up_still_serves_a_live_fetch(tmp_path: Path):
    store = broken_store(tmp_path)
    store.get("a")  # trips the failure

    fetch = RecordingFetch(default=b'{"live": true}')
    result = CachingFetcher(fetch, store).fetch(LEAGUE_URL, None, context=roster_context())

    assert result.body == '{"live": true}'
    assert result.stored is False


def test_the_store_closes_as_a_context_manager(tmp_path: Path):
    with store_at(tmp_path) as store:
        store.put("a", "one", tags=(), ttl=300)
    assert store._connection is None

    # Closing is not the same as failing: reopening happens on the next call.
    entry = store.get("a")
    assert entry is not None
    assert entry.body == "one"


def test_closing_a_store_that_was_never_opened_is_harmless(tmp_path: Path):
    store = store_at(tmp_path)
    store.close()
    assert store.get("a") is None


def test_a_truncated_database_degrades_on_read(tmp_path: Path):
    """Corruption that appears *after* the schema was verified."""
    store = store_at(tmp_path)
    store.put("a", "one", tags=(), ttl=300)
    store.close()
    store.path.write_bytes(b"\x00" * 4096)

    reopened = CacheStore(store.path)
    fetch = RecordingFetch(default=b'{"live": true}')
    result = CachingFetcher(fetch, reopened).fetch(LEAGUE_URL, None, context=roster_context())

    assert result.body == '{"live": true}'


# --------------------------------------------------------------------------- #
# Verification: a hit is measurably faster
# --------------------------------------------------------------------------- #


def test_a_cache_hit_is_measurably_faster_than_a_miss(tmp_path: Path):
    fetch = RecordingFetch(delay=0.05)
    fetcher = CachingFetcher(fetch, store_at(tmp_path))
    context = roster_context()

    start = time.perf_counter()
    fetcher.fetch(LEAGUE_URL, None, context=context)
    miss_seconds = time.perf_counter() - start

    start = time.perf_counter()
    hit = fetcher.fetch(LEAGUE_URL, None, context=context)
    hit_seconds = time.perf_counter() - start

    assert hit.cached is True
    assert len(fetch.calls) == 1
    assert hit_seconds < miss_seconds / 2


# --------------------------------------------------------------------------- #
# Layering
# --------------------------------------------------------------------------- #

FORBIDDEN_IMPORTS = frozenset({"typer", "click", "rich", "espn_api"})


@pytest.mark.parametrize("path", sorted(Path("src/fantasy_sports/cache").rglob("*.py")), ids=str)
def test_the_cache_layer_imports_no_cli_or_http_stack(path: Path):
    """``cache/`` sits between provider and core; it owns neither end."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    assert not (roots & FORBIDDEN_IMPORTS), f"{path} imports {sorted(roots & FORBIDDEN_IMPORTS)}"
