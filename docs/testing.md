# Testing against an API that breaks

This project's whole thesis is that ESPN changes without notice
(`docs/memory/prior-art-graveyard.md`: every previous ESPN fantasy tool died the
same way). That makes the fixture policy a design decision rather than a test
detail, so it is written down here.

Three layers, and each one covers what the others cannot.

| Layer | Runs | Answers |
|---|---|---|
| Recorded cassettes | every CI run, offline | does the adapter read the shapes ESPN **actually sent**? |
| Hand-authored cassettes | every CI run, offline | does it read the shapes no public league can produce? |
| `live`-marked tests | on demand, never in CI | does ESPN **still** send those shapes? |

Unit tests never touch the network. `pytest-socket` is enabled through
`addopts`, so a cassette miss fails loudly instead of quietly reaching ESPN —
and §8 covers *which* recorded response a hit gets, which is a separate
question with its own failure mode.

Writes do not fit the table: nothing records them, and §9 says what stands in
for a cassette. Two more sections carry rules rather than descriptions: **§6 is
what may and may not be committed**, and **§7 is the re-record procedure**.
Read both before adding a fixture.

---

## 1. Recorded: `tests/cassettes/espn/canary_2018.yaml`

A real recording of ESPN's public test league — `league_id=1234, year=2018`, the
league `espn-api`'s own integration test has hit daily and unattended for years
(ARCHITECTURE §14 item 6). No credentials are needed and none are sent.

Re-record with:

```bash
uv run python scripts/record_espn_cassettes.py
```

It drives the real `EspnProvider`, so what it records is exactly the request set
the adapter makes — including the *combinations* of `view=` parameters, which
matters because ESPN's server-side view composition is not the union of the
individual responses (research §7.7, issue #596). See §7 for what the script
checks before it leaves a file on disk.

**What it covers:** `fetch_league`, `fetch_teams`, `fetch_standings`,
`fetch_roster` (current and a past week), `fetch_matchups`,
`fetch_transactions` (including the full-season `since` sweep), and
`fetch_raw`.

**What it cannot cover, and why.** All three are library refusals keyed on the
*year*, so no request is made and there is nothing to record:

- **Box scores.** `League(1234, 2018).box_scores(1)` raises `Cant use box score
  before 2019`, and league 1234 does not exist for 2019, 2021, 2023 or 2025.
  The canary can never supply one.
- **Free agents.** Same refusal, same reason.
- **The activity feed.** Same refusal, plus ESPN stopped serving
  `kona_league_communication` for historical seasons entirely (issue #546, open
  since 2024).

Two further gaps that are about the league's *data* rather than the library:

- **The playoff week split.** Every one of this league's matchup periods maps
  1:1 to a scoring period, so `scoringPeriodId` vs `matchupPeriodId` — the one
  thing an in-season ESPN-only test never exercises — is not observable here.
- **Owner display names.** The canary's `members[]` carry ids and no names at
  all. Team-to-person mapping must not be built on ESPN member names.

The `teams[].owners` → `members[].id` join *is* exercised by the recording,
because #38's per-GUID pseudonyms keep the key distinct through redaction. If a
future scrubber change flattens it back to one shared placeholder,
`test_the_owner_join_survives_redaction_in_the_recording` goes red rather than
`owner_names` quietly emptying.

---

## 2. Hand-authored: `tests/cassettes/espn/synthetic_2026.yaml`

Covers exactly the five gaps above (minus box scores, see below). Built by:

```bash
uv run python scripts/build_synthetic_cassette.py
```

**Be honest about what a synthetic fixture proves.** It shows the adapter reads
the shape it was told about. It does not show ESPN still sends that shape — only
the recording and the live tests do that. A green run against a hand-built
payload is weaker evidence than it looks.

**The corpus cannot cover the compressed-body path, and it is not that a
recording happens not to be gzipped — no recording ever can be.**
`decode_compressed_response=True` is what makes the body scrubber work at all
(§8, and `docs/memory/cassette-scrubbing-blind-spots.md`), and it decompresses
*before* anything is written, so every cassette lands as plain text by
construction. A green corpus run is therefore not evidence that the scrubber
survives a gzipped body. The compressed path is covered by direct unit tests
instead — `test_a_compressed_body_is_decoded_before_the_scrubber_sees_it`
(gzip and deflate) and
`test_an_encoding_vcrpy_cannot_decode_is_refused_rather_than_recorded` in
`tests/unit/test_cassette_harness.py`, plus the gzip-to-SQLite test in
`tests/unit/test_cache.py`. `test_no_committed_cassette_carries_a_compressed_body`
holds the claim itself: if a compressed or `!!binary` body ever does reach a
committed cassette, that test goes red and this paragraph is wrong.

Three deliberate choices in the data, all load-bearing:

- **Owner ids are brace-wrapped but not GUIDs** (`{OWNER-ALPHA}`). A real SWID
  would be rewritten to a single `{SWID-REDACTED}` placeholder by the scrub
  hook, collapsing every member to one identity and silently destroying the
  `teams[].owners` → `members[].id` join the fixture exists to exercise.
- **Every timestamp is a round epoch-millisecond value**, so a test can assert
  the exact UTC instant a kickoff renders as and catch a naive, host-local
  datetime leaking through from `espn-api`.
- **Every interaction carries the `x-fantasy-filter` header `espn-api` actually
  sends**, and `kona_player_info` appears *twice* — once unfiltered and once
  with `filterSlotIds: [4]`, returning only the wide receiver. Those two
  interactions have an identical method, scheme, host, port, path and query, so
  they are the corpus-level proof of the matcher in §8: delete the matcher and
  `test_a_position_filtered_read_gets_its_own_recording` returns Radia Perlman
  for a wide-receiver query. A hand-authored interaction that *omits* a header
  ESPN would have received is a fixture the adapter can never match.

---

## 3. The box-score interaction, derived from an MIT corpus

`synthetic_2026.yaml` carries one `mMatchupScore` + `mScoreboard` interaction
that serves `box-scores --week 1`: one matchup, 15 lineup entries per side,
with per-player projected and actual points.

It could not be recorded. `espn-api` refuses box scores before 2019 and the
canary league (`1234`) exists only for 2018, so the league this project
designates for recording can never produce one (ARCHITECTURE §14 item 6, as
amended).

**Provenance.** The payload is derived from
`tests/football/unit/data/league_boxscore_2018.json` in
[`cwendt94/espn-api`](https://github.com/cwendt94/espn-api), **MIT licensed,
Copyright (c) 2019 Christian Wendt**. It is a real ESPN response, unused
upstream because their own suite cannot exercise it either.

**MIT attribution is required, not optional.** The copyright notice travels
with the derived file, which is why it is recorded here and in
`tests/unit/test_box_scores.py`.

**How it was derived**, so it can be rebuilt:

1. Take `schedule[0]` from the source and both its sides.
2. On each side, keep only stat rows for the requested scoring period with
   `statSourceId` 0 (actual) and 1 (projected). Drop `draftRanksByRankType`,
   `rankings`, `outlooks`, `seasonOutlook`, `ownership`.
3. Rewrite `seasonId` to a supported season and the team ids to the fixture's
   own (`1`, `2`). The pre-2019 refusal is enforced on the *season requested*,
   not on the payload.
4. Keep `teams` to the two teams the matchup references.

43,593,721 bytes becomes roughly 79 KB. One matchup and one scoring period is
the whole trim; the size is almost entirely full-season per-player stat arrays.

**The request carries two views in one call.** `espn-api` asks for
`view=mMatchupScore&view=mScoreboard` together, so the interaction's URI must
list both. A URI naming only `mScoreboard` looks right, matches nothing, and
fails as though the fixture were missing — see §8.

**What it proves and what it does not.** It proves the adapter reads the shape
it was told about, and that bench and starter slots are separated correctly.
It does not prove ESPN still sends that shape, and it never exercises the
compressed-body path (§2).

## 4. What is *not* a cassette, on purpose

HTTP failures — 401, 404, 429, a body that will not parse — are tested by
injecting a transport (`EspnProvider(http=...)`), not by recording. Recording a
429 would mean provoking one from ESPN, and a cassette of a 401 records the
*absence* of a credential, which is indistinguishable from a recording where the
scrub ran. Neither is worth a fixture nobody can trust.

The one library behaviour that cannot be reached through that seam is the
alternate-URL-shape retry inside `checkRequestStatus`, which issues its own
`requests.get`. `tests/unit/test_espn_provider.py` stubs the module's `requests`
for those two tests specifically, and asserts the probe *ran* — the probe being
mandatory rather than optional is the whole of ARCHITECTURE §14 item 1.

---

## 5. Live tests

```bash
uv run pytest -m live                     # canary only; no credentials needed
ESPN_S2=... ESPN_SWID='{...}' FANTASY_SPORTS_ESPN_LEAGUE=... uv run pytest -m live
```

`tests/live/test_espn_live.py` splits into a canary group that needs nothing and
a private-league group that skips without credentials. When the canary group
goes red while the unit tests stay green, ESPN changed something — that is the
signal the whole health system exists to produce, and a red canary is a genuine
upstream change far more often than it is a flake.

Live tests are excluded from the default selection two ways, deliberately
overlapping: CI runs `pytest -m "not live"`, and `tests/conftest.py`'s
`pytest_collection_modifyitems` skips them when no `-m` was given at all, so a
bare `uv run pytest` on a developer machine does not silently start calling
ESPN either.

---

## 6. What may and may not be committed

The credential scan (`tests/unit/test_scrubbing.py`) proves a committed cassette
holds no **credential**. It says nothing about whose league the payload came
from, and those are different questions. #12's acceptance criteria name PII
alongside credentials; this section is what that means in practice.

**Committable:**

- `1234, 2018` — ESPN's public test league. Already public, hit daily and
  unattended by `espn-api`'s own CI.
- `99, 2026` — invented. Does not exist at ESPN.

**Not committable, scrubbed or not:** any real private league. A recording of
one carries nine other people's team names, display names, roster choices, and
— since #38 — **stable per-GUID SWID pseudonyms under a public, deterministic
salt**. That last one is the part that is easy to wave through: the GUID itself
never reaches disk and there is no inverse, but anyone holding a real SWID can
hash it under the known cassette salt and confirm whether that member appears in
a committed fixture. That is a confirmable mapping, accepted for the canary
because the canary is already public, and *not* something to extend to a league
whose members did not choose to be in this repository.
(`docs/memory/swid-pseudonyms.md` argues the trade in full.)

Scrubbing does not change any of that, so the enforcement is on **provenance**
rather than on content — a name is not machine-recognisable, but a league id is:

- `test_every_committed_cassette_comes_from_a_public_league` fails on a
  committed cassette whose request URIs name any league outside the two above.
- `scripts/record_espn_cassettes.py` writes every non-canary league to
  `tests/cassettes/private/`, and refuses `--out` pointed at the committed
  directory.
- `tests/cassettes/private/` is gitignored, asserted by
  `test_private_recordings_are_gitignored`.
- `test_every_committed_fixture_names_only_public_leagues` extends the first
  check to the *text* of every committed `.json` and `.yaml` anywhere in the
  tree for a league id in any shape a capture can carry one — the
  `/leagues/<id>` and `/leagueHistory/<id>` paths, a `leagueId=` query
  string, and the `groupId` / `leagueId` keys of a `fan.api` membership body —
  because a research capture under `docs/` is a recording too. The
  write-surface captures in `docs/research/05-espn-write-surface/` (#14) are
  the worked example of what "rewrite it until it is not a private-league
  recording" means: league id replaced with `99`, member id replaced with the
  non-confirmable placeholder, another manager's player ids and snapshots
  withheld, the owner's own player ids replaced by a stable synthetic mapping
  (and the names ESPN interpolates into messages with it), transaction ids
  redacted, and a `redactions` list in each file saying so.

None of that can stop a determined contributor, and it is not meant to. It
means the committable path is the default and the uncommittable one takes a
deliberate act.

---

## 7. Re-recording safely

```bash
uv run python scripts/record_espn_cassettes.py                       # the canary
uv run python scripts/record_espn_cassettes.py --credentialed        # + real cookies
uv run python scripts/record_espn_cassettes.py --league 55501 --season 2026
```

`--credentialed` resolves `espn_s2` and `SWID` through the auth chain, which
means the **macOS Keychain** first (service `fantasy-sports`, one entry per
credential name — `fantasy-sports auth login` writes them), then the
environment, then config. Nothing is printed; the script reports how many
credentials resolved and no more.

Against a private league it is required. Against the canary it is optional and
worth running anyway, because it is the cheapest end-to-end proof that the scrub
covers a real credential: ESPN answers 200 either way, and the recorded cassette
must come back with `cookie: [REDACTED]` — the header *present*, its value
replaced. A cassette with no `Cookie` line at all is indistinguishable from one
recorded without credentials, which is why the filters use vcrpy's
`(name, replacement)` tuple form rather than a bare name.

**Nothing is trusted until it has been read back off disk.** After writing, the
script:

1. runs the repo-wide credential scan (`scan_file`) against the finished bytes,
   including its structural pass over base64 `!!binary` scalars;
2. greps those bytes for the **literal values this run actually sent**, in every
   form they could have been serialised in — brace-wrapped, bare, and
   percent-encoded (`%7B...%7D`, the shape a SWID takes in a URL path, which
   matches neither the scrub patterns nor the scan);
3. deletes the recording and exits non-zero if either finds anything, naming the
   problem without echoing the value.

Step 2 is the one that does not depend on a pattern being right. Step 3 is why
a failed recording cannot be committed by accident.

If a recording session ever hits `UnscrubbableResponseError`, the answer is to
find out what encoding arrived — not to catch the exception. See §8.

---

## 8. How a request is matched, and why the default is not enough

vcrpy's default `match_on` is `method, scheme, host, port, path, query`. It
**ignores headers entirely**. ESPN scopes free agents, transactions, the
activity feed and box scores by a JSON `x-fantasy-filter` *header* against an
otherwise identical URL, so under the default matcher two such recordings
collide, and the second read silently replays the first one's body. Nothing
errors. The filter-gated test passes against the wrong payload — and for
`fetch_free_agents` in particular that is the worst available failure mode,
because ESPN's default player set looks exactly like a plausible answer to any
filter. It is the cassette twin of the cache-key bug that put `x-fantasy-filter`
into `cache_key`'s `extra`.

`tests/conftest.py` registers `match_fantasy_filter` and appends it to
`match_on`. Two things about it are not obvious:

- **`build_vcr()` is the only supported way to get a `VCR`.** `match_on` entries
  are resolved by *name* against `VCR.matchers`, so `vcr.VCR(**build_vcr_config())`
  raises `KeyError` at `use_cassette` time rather than quietly matching on less.
  `pytest-recording` builds its own `VCR`, so the `pytest_recording_configure`
  hook registers the matcher there too.
- **It compares canonicalised JSON, not the raw string.** `espn-api` builds the
  transactions filter as `{"filterType": {"value": list(types)}}` over a Python
  **set**, and `str` hashing is randomised per interpreter, so the identical
  query serialises its `value` array in a different order on every run. This is
  observable: re-recording the canary today rewrites those header lines and
  nothing else. A literal comparison would make the committed `mTransactions2`
  interactions replay or miss depending on `PYTHONHASHSEED` — a flaky matcher,
  which is worse than the bug it fixes. Object keys are sorted and arrays are
  sorted by their elements' canonical form; every filter ESPN accepts is a *set*
  of values, so order carries no meaning. A value that is not JSON falls back to
  a literal comparison.

The committed `canary_2018.yaml` still holds the *stale* order from whichever
process recorded it, so the suite is green only because the comparison
canonicalises. That is deliberate — it keeps the guarantee visible rather than
letting a fresh recording paper over it.

**A miss raises.** `record_mode="none"` makes the cassette write-protected, so
an unrecorded request raises vcrpy's `CannotOverwriteExistingCassetteException`
*before* a socket is opened — not `pytest-socket`'s `SocketBlockedError` after
the fact. Both fail the run; only one fails before anything leaves the process.
`test_a_cassette_miss_raises_rather_than_calling_espn` drives real `requests`
through the whole stack to assert which one it is.

**Two invariants in `build_vcr_config()` that must not be undone:**

- `decode_compressed_response=True` is a *security* setting. vcrpy composes its
  `decode_response` filter ahead of `before_record_response`, so the body
  scrubber sees text. Without it the scrubber runs a regex over a gzip stream,
  matches nothing, reports success, and writes a live credential to disk.
- `filter_headers` uses `(name, replacement)` tuples. A bare name **deletes**
  the header, and a deleted header is indistinguishable from one that was never
  sent.

Both are held by tests in `tests/unit/test_scrubbing.py` and
`tests/unit/test_cassette_harness.py`; the reasoning is in
`docs/memory/cassette-scrubbing-blind-spots.md`.

---

## 9. Write tests

A write never reaches ESPN from a unit test, and there is no cassette of one.
Reads replay recordings. A POST goes to something else, for three reasons: a
cassette would record a transaction that changed a real league, a write's
answer depends on the request body, and the interesting write behaviour is
what the tool does with each answer rather than the answer itself.

- **An injected `http_post`.** `EspnProvider(..., http_post=...)` is the seam
  for the write host, the way `http=` is for reads. It resolves at call time,
  so the default, `requests.post`, is imported inside the method and
  `--help` never pays for it (`docs/memory/default-args-defeat-monkeypatch.md`
  is why the default cannot be bound in the signature). Every write test
  hands it a fake that records the body it was sent, so a test can assert the
  exact request and that nothing was sent when a guard refused.
- **`WritableEspn`** (`tests/_harness.py`) is the synthetic league plus a write
  host that changes what later reads return. Its `mode` is `apply` (the body
  takes effect), `ignore` (the same 200 with nothing changed, so the
  read-back diverges), or `reject` (a 409). It plays `ROSTER`, `FREEAGENT`,
  `WAIVER`, and `CANCEL` bodies, keeps the pending queue that
  `mPendingTransactions` serves, and can echo another member to play a write
  applied by a different account. Commands are tested through the real
  adapter on top of it, not through a fake provider, so the guard, the
  journal, and the read-back are all exercised together.
- **The bodies are capture-derived, against league `99`.** Rejections and
  lineup successes replay the redacted captures in
  `docs/research/05-espn-write-surface/` and
  `docs/research/06-espn-add-drop-surface/` verbatim, with the league id
  already rewritten to the invented one (§6). The success answers for
  `FREEAGENT` adds and standalone drops are the exception: the test league's
  pool sat on waivers when it was captured, so those are hand-built in the
  captured shape, and the docs and samples say so. #108 stays open for the
  executing probes.
- **The live double opt-in.** A `live` write test runs only when
  `FANTASY_SPORTS_ESPN_WRITE_LEAGUE` is set **and equals**
  `FANTASY_SPORTS_ESPN_LEAGUE`, because the read-only live suite points the
  second variable at whatever league is handy, possibly a real one. Both must
  name the throwaway test league. Each live write test uses a temporary XDG
  tree for its journal and cache and cleans up from a fresh roster read, never
  from the failed command's report. See `tests/live/README.md`.
- **No committed fixture asserts that ESPN refuses a cross-team write.** ESPN
  honoured one on the commissioner's cookie, whether it refuses it for an
  ordinary member is unverified, and a cassette saying "rejected" would turn
  a guess into a passing test
  (`docs/memory/espn-lineup-write-authority-is-broader-than-team-ownership.md`).
  The ownership guard is tested as what it is, a unit test of the guard: an
  unowned team is `WRITE_REJECTED` `not_owned` and no POST was made.
