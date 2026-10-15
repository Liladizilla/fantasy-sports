# Command reference

Every command `fantasy-sports --help` lists, with what it returns in every
output mode and what every failure looks like — so a reader deciding whether
to install, or an agent deciding whether to call, can see the answer without
running anything. The README covers [install](../README.md#install),
[first run](../README.md#first-run), and [using it from an
agent](../README.md#using-it-from-an-agent); this document is the part you
look things up in.

**Nothing below is typed by hand.** `scripts/render_readme_samples.py` runs
each invocation, trims the result, writes it to `docs/samples/`, and splices
it into the block between the `<!-- sample: … -->` markers in this file and
the README. `tests/unit/test_readme_samples.py` regenerates every sample it
can without the network — the replayed and synthetic ones from their
fixtures, the live ones from the committed canary recording of the same
league — and fails if a committed sample no longer matches what the code
renders, or if the docs and the samples disagree. Six samples depend on the
machine or on a request the recording never captured (`--help`, the pipe,
`doctor`, the two `kona_player_info` reads, and the 2019 probe); the four
JSON ones are held to the envelope contract, `--help` is compared to what
the fast path renders from the registry, and the pipe sample must be the
head of the standings envelope. Regenerate with:

```bash
uv run python scripts/render_readme_samples.py
```

## How to read a sample

Each sample is the command as you would type it, then a comment line saying
where the output came from, then the output. The comment carries four
things: the league, the source, the exit status, and the stream.

- **`ESPN public league 1234, season 2018 (live)`** — fetched from ESPN when
  the samples were generated. This is the public test league `espn-api`'s
  own suite has hit for years; anyone can read it, and it is the only real
  league whose output may be committed to this repository
  (`docs/testing.md` §6). **No credentials are needed to reproduce these.**
  The CLI resolves the credential chain before it sends anything, so the
  generator sets two placeholder cookies in the environment; ESPN serves a
  public league without reading them.
- **`synthetic league 99, season 2026 (replayed)`** — an invented league
  that does not exist at ESPN, served offline from
  `tests/cassettes/espn/synthetic_2026.yaml` through the same transport
  stub the unit tests use. It stands in for `box-scores` and `free-agents`,
  which ESPN refuses before 2019, and for the `auth` commands, which run
  against an in-memory Keychain so nothing real is touched. Names in it are
  invented.
- **`(synthetic)`** — an error envelope built from the exception class in
  `core/errors.py` and rendered by the output layer, for the four codes
  that need a live failure nobody can safely provoke: an expired cookie, an
  outage, a throttle, and a schema change. The shape is exact; only the
  trigger is invented.

Output marked **trimmed** has had long lists and provider passthroughs cut
down; every cut is marked with `…` and a count, and the result is still
valid JSON. Every top-level envelope key is always present. Untrimmed
samples are the CLI's bytes exactly, which is why they escape non-ASCII as
`\uXXXX`; trimmed ones are re-serialised and show `—` and `…` directly.
Paths under the generator's throwaway config directory are rewritten to
`~/.config` and `~/.cache`, which is where they would be on your machine.

The two `config.toml` files the samples ran against:

```toml
# ESPN's public test league (used for every "live" sample)
default = "public"

[leagues.public]
provider  = "espn"
league_id = "1234"
season    = 2018
```

```toml
# The invented league (used for every "replayed" sample)
default = "synthetic"

[leagues.synthetic]
provider  = "espn"
league_id = "99"
season    = 2026
```

## Global options

Every read command accepts these, before or after the command name
(`fantasy-sports --league public standings` and `fantasy-sports standings
--league public` are the same call). `auth` and `doctor` take only
`--output` and `--no-raw`, since they read no league.

| Option | What it does |
|---|---|
| `--league`, `-l NAME` | Which `[leagues.NAME]` profile in `config.toml` to read. Defaults to `default`, or to the only league configured |
| `--season YEAR` | Override the profile's `season` for this call, for a historical read |
| `--output`, `-o FORMAT` | `json`, `table`, or `csv`. Defaults to `json` unless stdout is a terminal, then `table` |
| `--fresh` | Refetch from ESPN and update the cache entry |
| `--no-cache` | Neither read nor write the cache. Wins over `--fresh` if both are given |
| `--no-raw` | Strip `raw` from every normalized object, recursively, and set `raw_omitted: true`. `raw --view` ignores it |

### `--league`

<!-- sample: option-league -->
```bash
fantasy-sports standings --league public --no-raw
# ESPN public league 1234, season 2018 (live). Exit 0, stdout, trimmed with …:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:28Z",
  "data_as_of": "2026-09-12T19:31:25Z",
  "data_age_seconds": 2,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 2,
      "cached": true
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 2,
      "cached": true
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 2,
      "cached": true
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 1,
      "cached": true
    }
  ],
  "untrusted": {
    "[0].name": "Team 8",
    "…": "… 9 more entries"
  },
  "raw_omitted": true,
  "data": [
    {
      "provider": "espn",
      "provider_id": "8",
      "name": "Team 8",
      "owner_names": [],
      "wins": 9,
      "losses": 4,
      "ties": 0,
      "points_for": 1402.7200000000003,
      "points_against": 1191.54,
      "standing": 1
    },
    "… 9 more items"
  ],
  "error": null
}
```
<!-- /sample -->

### `--season`

The profile says 2018; the call asks for 2019, which this league never had,
so ESPN answers 404 and the CLI reports it under `LEAGUE_NOT_FOUND`:

<!-- sample: option-season -->
```bash
fantasy-sports league info --season 2019
# ESPN public league 1234, season 2018 (live). Exit 5, stderr. League 1234 exists only for 2018, so this is ESPN's real 404:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "LEAGUE_NOT_FOUND",
    "message": "League 1234 does not exist",
    "retryable": false,
    "agent_action": "Ask the human to confirm the league id and their access.",
    "remediation": "Confirm the league id, and that this account can see that season.",
    "details": {
      "view": "fetch_league",
      "status": 404
    },
    "health": null
  }
}
```
<!-- /sample -->

### `--fresh`

Compare `sources[].cached` with the `standings` sample under
[Commands](#standings), which ran a moment earlier and was served from the
cache. `--fresh` refetches every source and stores the result:

<!-- sample: option-fresh -->
```bash
fantasy-sports standings --fresh --no-raw
# ESPN public league 1234, season 2018 (live). Exit 0, stdout, trimmed with …:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:29Z",
  "data_as_of": "2026-09-12T19:31:28Z",
  "data_age_seconds": 0,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-12T19:31:28Z",
      "age_seconds": 0,
      "cached": false
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-12T19:31:28Z",
      "age_seconds": 0,
      "cached": false
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-12T19:31:28Z",
      "age_seconds": 0,
      "cached": false
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-12T19:31:29Z",
      "age_seconds": 0,
      "cached": false
    }
  ],
  "untrusted": {
    "[0].name": "Team 8",
    "…": "… 9 more entries"
  },
  "raw_omitted": true,
  "data": [
    {
      "provider": "espn",
      "provider_id": "8",
      "name": "Team 8",
      "owner_names": [],
      "wins": 9,
      "losses": 4,
      "ties": 0,
      "points_for": 1402.7200000000003,
      "points_against": 1191.54,
      "standing": 1
    },
    "… 9 more items"
  ],
  "error": null
}
```
<!-- /sample -->

### `--no-cache`

Same fetch, but the cache is neither read nor written: an entry that already
exists is left untouched, and the next call without the flag sees whatever
was there before. A cache store is still constructed — it is what scrubs
credential values out of a response body — so `--no-cache` never changes
what gets parsed:

<!-- sample: option-no-cache -->
```bash
fantasy-sports standings --no-cache --no-raw
# ESPN public league 1234, season 2018 (live). Exit 0, stdout, trimmed with …:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:29Z",
  "data_as_of": "2026-09-12T19:31:29Z",
  "data_age_seconds": 0,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-12T19:31:29Z",
      "age_seconds": 0,
      "cached": false
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-12T19:31:29Z",
      "age_seconds": 0,
      "cached": false
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-12T19:31:29Z",
      "age_seconds": 0,
      "cached": false
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-12T19:31:29Z",
      "age_seconds": 0,
      "cached": false
    }
  ],
  "untrusted": {
    "[0].name": "Team 8",
    "…": "… 9 more entries"
  },
  "raw_omitted": true,
  "data": [
    {
      "provider": "espn",
      "provider_id": "8",
      "name": "Team 8",
      "owner_names": [],
      "wins": 9,
      "losses": 4,
      "ties": 0,
      "points_for": 1402.7200000000003,
      "points_against": 1191.54,
      "standing": 1
    },
    "… 9 more items"
  ],
  "error": null
}
```
<!-- /sample -->

`--output` and `--no-raw` are shown under [Output modes](#output-modes).

## Output modes

The renderer is chosen by `--output`, or, when that is absent, by whether
stdout is a terminal: a table for a person, JSON for a pipe. A failure is
always JSON on stderr whatever `--output` asked for, and stdout stays
byte-empty (see [Errors](#errors)).

### JSON is the default when stdout is a pipe

No `--output` here; the command is piped into `head`, so the format
detector saw a pipe and chose JSON:

<!-- sample: pipe -->
```bash
fantasy-sports standings | head -4
# ESPN public league 1234, season 2018 (live). Exit 0, stdout. No --output given; stdout is a pipe, so the renderer chose JSON:
```
```text
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
```
<!-- /sample -->

### `--output table`

What a terminal gets by default. The header line restates the envelope's
provenance; `raw` is left out because one ESPN team payload alone would wrap
a hundred-column table over a dozen lines; text a league member controls
(team names) renders as literal characters, never as markup:

<!-- sample: standings.table -->
```bash
fantasy-sports standings --output table
# ESPN public league 1234, season 2018 (live). Exit 0, stdout:
```
```text
espn · league 1234 · season 2018 · generated 2026-09-12T19:31:26Z · data age 0s
provider   provider…   name        owner_n…   wins   losses   ties   points_f…   points_…   standing
────────────────────────────────────────────────────────────────────────────────────────────────────
espn       8           Team 8      []         9      4        0      1402.720…   1191.54    1       
espn       4           THE KING    []         8      5        0      1058.88     1075.06    2       
espn       7           Team 7      []         10     3        0      1344.800…   1071.9     3       
espn       1           Team 1      []         10     3        0      1276.88     1038.22    4       
espn       6           Team        []         5      8        0      1139.74     1252.62    5       
                       Viking                                                                       
                       Queen                                                                        
espn       2           Team 2      []         6      7        0      1019.599…   1028.58    6       
espn       3           FANTASY     []         2      11       0      884.1800…   1151.84    7       
                       GOD                                                                          
espn       9           Team        []         6      7        0      1070.94     1281.2     8       
                       Mizrachi                                                                     
espn       10          Team 10     []         5      8        0      1278.919…   1243.14    9       
espn       5           Team 5      []         4      9        0      1006.940…   1149.5     10      
`raw` omitted from this table; use --output json for the provider payload.
sources: mTeam+mRoster+mMatchup+mSettings+mStandings 0s (cached), players_wl 0s (cached), 
proTeamSchedules_wl 0s (cached), mDraftDetail 0s (cached)
```
<!-- /sample -->

A single-object command renders as field/value rows:

<!-- sample: league-info.table -->
```bash
fantasy-sports league info --output table
# ESPN public league 1234, season 2018 (live). Exit 0, stdout:
```
```text
espn · league 1234 · season 2018 · generated 2026-09-12T19:31:26Z · data age 0s
field          value                                                                          
──────────────────────────────────────────────────────────────────────────────────────────────
provider       espn                                                                           
provider_id    1234                                                                           
name           Arizona 1234                                                                   
season         2018                                                                           
sport          nfl                                                                            
team_count     10                                                                             
current_week   17                                                                             
roster_slots   {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "D/ST": 1, "K": 1, "BE": 7, "RB/WR/TE": 1}
`raw` omitted from this table; use --output json for the provider payload.
sources: mTeam+mRoster+mMatchup+mSettings+mStandings 0s (cached), players_wl 0s (cached), 
proTeamSchedules_wl 0s (cached), mDraftDetail 0s (cached)
```
<!-- /sample -->

### `--output csv`

CSV carries `data` and nothing else: no schema version, no provenance, no
error. Nested values become compact JSON in one cell. Any string cell that
begins with `=`, `+`, `-`, `@`, a tab, or a carriage return is prefixed with
`'` so a spreadsheet shows it as text instead of evaluating it; a machine
consumer strips that leading quote, or uses JSON. This sample adds
`--no-raw` because a `raw` cell is several hundred characters of ESPN
internals per row:

<!-- sample: standings.csv -->
```bash
fantasy-sports standings --output csv --no-raw
# ESPN public league 1234, season 2018 (live). Exit 0, stdout:
```
```text
provider,provider_id,name,owner_names,wins,losses,ties,points_for,points_against,standing
espn,8,Team 8,[],9,4,0,1402.7200000000003,1191.54,1
espn,4,THE KING,[],8,5,0,1058.88,1075.06,2
espn,7,Team 7,[],10,3,0,1344.8000000000002,1071.9,3
espn,1,Team 1,[],10,3,0,1276.88,1038.22,4
espn,6,Team Viking Queen,[],5,8,0,1139.74,1252.62,5
espn,2,Team 2,[],6,7,0,1019.5999999999999,1028.58,6
espn,3,FANTASY GOD,[],2,11,0,884.1800000000002,1151.84,7
espn,9,Team Mizrachi,[],6,7,0,1070.94,1281.2,8
espn,10,Team 10,[],5,8,0,1278.9199999999998,1243.14,9
espn,5,Team 5,[],4,9,0,1006.9400000000002,1149.5,10
```
<!-- /sample -->

### `--no-raw`, before and after

Every normalized object carries the provider's own sub-object under `raw`,
so nothing ESPN sent is lost. It is also most of the bytes. Before:

<!-- sample: teams -->
```bash
fantasy-sports teams
# ESPN public league 1234, season 2018 (live). Exit 0, stdout, trimmed with …:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:26Z",
  "data_as_of": "2026-09-12T19:31:25Z",
  "data_age_seconds": 0,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 0,
      "cached": true
    }
  ],
  "untrusted": {
    "[0].name": "Team 1",
    "[1].name": "Team 2",
    "…": "… 8 more entries"
  },
  "raw_omitted": false,
  "data": [
    {
      "provider": "espn",
      "provider_id": "1",
      "name": "Team 1",
      "owner_names": [],
      "wins": 10,
      "losses": 3,
      "ties": 0,
      "points_for": 1276.88,
      "points_against": 1038.22,
      "raw": {
        "abbrev": "TM1",
        "currentProjectedRank": 0,
        "divisionId": 0,
        "draftDayProjectedRank": 0,
        "…": "… 17 more keys"
      },
      "standing": 2
    },
    {
      "provider": "espn",
      "provider_id": "2",
      "name": "Team 2",
      "owner_names": [],
      "wins": 6,
      "losses": 7,
      "ties": 0,
      "points_for": 1019.5999999999999,
      "points_against": 1028.58,
      "raw": {
        "abbrev": "TM2",
        "currentProjectedRank": 0,
        "divisionId": 0,
        "draftDayProjectedRank": 0,
        "…": "… 17 more keys"
      },
      "standing": 6
    },
    "… 8 more items"
  ],
  "error": null
}
```
<!-- /sample -->

After — `raw` is gone from every object at every depth, and the envelope
says so with `raw_omitted: true`:

<!-- sample: teams.no-raw -->
```bash
fantasy-sports teams --no-raw
# ESPN public league 1234, season 2018 (live). Exit 0, stdout, trimmed with …:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:26Z",
  "data_as_of": "2026-09-12T19:31:25Z",
  "data_age_seconds": 0,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 0,
      "cached": true
    }
  ],
  "untrusted": {
    "[0].name": "Team 1",
    "[1].name": "Team 2",
    "…": "… 8 more entries"
  },
  "raw_omitted": true,
  "data": [
    {
      "provider": "espn",
      "provider_id": "1",
      "name": "Team 1",
      "owner_names": [],
      "wins": 10,
      "losses": 3,
      "ties": 0,
      "points_for": 1276.88,
      "points_against": 1038.22,
      "standing": 2
    },
    {
      "provider": "espn",
      "provider_id": "2",
      "name": "Team 2",
      "owner_names": [],
      "wins": 6,
      "losses": 7,
      "ties": 0,
      "points_for": 1019.5999999999999,
      "points_against": 1028.58,
      "standing": 6
    },
    "… 8 more items"
  ],
  "error": null
}
```
<!-- /sample -->

`raw_omitted: false` means "nothing was suppressed here", not "`raw` is
present": the `raw --view` command has no `raw` keys in its payload by
construction and still reports `false`.

## Commands

### `--help`

The help text is generated from the command registry, so the list below is
exactly what is registered — a command becomes visible by being registered
and in no other way. This is also the cold-start path that has to stay under
50 ms, which is why it imports no HTTP stack:

<!-- sample: help -->
```bash
fantasy-sports --help
# no league (live). Exit 0, stdout:
```
```text
Usage: fantasy-sports [OPTIONS] COMMAND [ARGS]...

  Agent-native CLI for fantasy sports leagues. Every payload is a versioned
  envelope; every failure is a machine-readable code on stderr.

Options (accepted before or after the command):
  --league, -l TEXT  Named league profile from config.toml. Defaults to the configured default.
  --season INTEGER   Four-digit year, overriding the profile's season for this call.
  --output, -o TEXT  json, table, or csv. Defaults to json unless stdout is a terminal.
  --fresh            Refetch from the provider and update the cache entry.
  --no-cache         Neither read nor write the cache. Wins over --fresh if both are given.
  --no-raw           Strip `raw` from every normalized object in the payload, recursively. Marks raw_omitted=true in the envelope. `raw --view` is passthrough by definition and ignores this flag.
  -V, --version      Show the version and exit.
  -h, --help         Show this message and exit.

Commands:
  add           Add a free agent to your own team, optionally dropping a player. Irreversible.
  auth          3 subcommands: login, logout, status
  box-scores    List both lineups for a week's matchups, player by player, with projections.
  claim         Submit a waiver claim, with a bid where the league uses FAAB; can drop a player. Irreversible.
  doctor        Run every health check: config, credentials, cache, journal, version, provider.
  drop          Release a player from your own team to waivers. Irreversible.
  free-agents   List unrostered players, optionally filtered to one position.
  journal       4 subcommands: digest, list, reverse, show
  league        1 subcommand: info
  lineup        1 subcommand: set
  matchups      List head-to-head pairings for a week, with both ESPN period identifiers.
  raw           Pass a view straight through to ESPN and return its payload unmodified.
  roster        List one team's roster slots: player, lineup slot, eligibility, kickoff, lock.
  standings     List teams in the provider's own rank order, with a 1-based standing.
  teams         List every team with its record, points, and owners. Unordered.
  transactions  List recent roster moves, newest first, walking scoring periods backward.
```
<!-- /sample -->

### `league info`

The league itself: name, season, sport, team count, current week, and
`roster_slots` — the slot-to-count map that makes a legal lineup
constructible from normalized output alone. `data` is one object. The
league `name` is commissioner-set free text, so `untrusted` points at it.

<!-- sample: league-info -->
```bash
fantasy-sports league info
# ESPN public league 1234, season 2018 (live). Exit 0, stdout, trimmed with …:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:26Z",
  "data_as_of": "2026-09-12T19:31:25Z",
  "data_age_seconds": 0,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 0,
      "cached": false
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 0,
      "cached": false
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 0,
      "cached": false
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 0,
      "cached": false
    }
  ],
  "untrusted": {
    "name": "Arizona 1234"
  },
  "raw_omitted": false,
  "data": {
    "provider": "espn",
    "provider_id": "1234",
    "name": "Arizona 1234",
    "season": 2018,
    "sport": "nfl",
    "team_count": 10,
    "current_week": 17,
    "raw": {
      "draftDetail": "… 2 keys",
      "gameId": 1,
      "id": 1234,
      "members": "… 10 items",
      "…": "… 5 more keys"
    },
    "roster_slots": {
      "QB": 1,
      "RB": 2,
      "WR": 2,
      "TE": 1,
      "D/ST": 1,
      "K": 1,
      "BE": 7,
      "RB/WR/TE": 1
    }
  },
  "error": null
}
```
<!-- /sample -->

### `teams`

Every team with its record, points, and owners, in ESPN's own order (by team
id, not by rank). `owner_names` is a list because a co-managed team has more
than one; the public league's members carry no display names, so it is empty
here. Team names and owner names are member-set free text and are listed in
`untrusted`. The `standing` field is the same 1-based rank `standings`
reports.

The `teams` sample is the "before" half of [`--no-raw`](#--no-raw-before-and-after)
above.

### `standings`

The same team objects in ESPN's rank order, tiebreakers included, each with
its 1-based `standing`. Do not re-sort it: the order is the provider's
multi-rule cascade, not "wins then points".

<!-- sample: standings -->
```bash
fantasy-sports standings
# ESPN public league 1234, season 2018 (live). Exit 0, stdout, trimmed with …:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:26Z",
  "data_as_of": "2026-09-12T19:31:25Z",
  "data_age_seconds": 0,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 0,
      "cached": true
    }
  ],
  "untrusted": {
    "[0].name": "Team 8",
    "[1].name": "THE KING",
    "…": "… 8 more entries"
  },
  "raw_omitted": false,
  "data": [
    {
      "provider": "espn",
      "provider_id": "8",
      "name": "Team 8",
      "owner_names": [],
      "wins": 9,
      "losses": 4,
      "ties": 0,
      "points_for": 1402.7200000000003,
      "points_against": 1191.54,
      "raw": {
        "abbrev": "TM8",
        "currentProjectedRank": 0,
        "divisionId": 1,
        "draftDayProjectedRank": 0,
        "…": "… 17 more keys"
      },
      "standing": 1
    },
    {
      "provider": "espn",
      "provider_id": "4",
      "name": "THE KING",
      "owner_names": [],
      "wins": 8,
      "losses": 5,
      "ties": 0,
      "points_for": 1058.88,
      "points_against": 1075.06,
      "raw": {
        "abbrev": "TM4",
        "currentProjectedRank": 0,
        "divisionId": 0,
        "draftDayProjectedRank": 0,
        "…": "… 17 more keys"
      },
      "standing": 2
    },
    "… 8 more items"
  ],
  "error": null
}
```
<!-- /sample -->

### `roster`

One team's roster slots. `--team` takes an ESPN team id or a name — exact,
then a unique case-insensitive prefix, then a unique substring; anything
that matches two teams is an error naming both. Each slot carries the
`player`, the lineup `slot` it occupies, `is_starter`, and `is_locked`. The
player carries `eligible_slots`, `pro_team`, `opponent`, `projected_points`,
and the UTC `kickoff` of their game.

| Option | |
|---|---|
| `--team TEXT` | **Required.** Team id, or its name (case-insensitive; a unique prefix is enough) |
| `--week INTEGER` | Scoring period to read the lineup for. Omitted: the current roster |

<!-- sample: roster -->
```bash
fantasy-sports roster --team 1
# ESPN public league 1234, season 2018 (live). Exit 0, stdout, trimmed with …:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:26Z",
  "data_as_of": "2026-09-12T19:31:25Z",
  "data_age_seconds": 0,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 0,
      "cached": true
    }
  ],
  "untrusted": {},
  "raw_omitted": false,
  "data": [
    {
      "provider": "espn",
      "provider_id": "15847",
      "player": {
        "provider": "espn",
        "provider_id": "15847",
        "name": "Travis Kelce",
        "position": "TE",
        "raw": {
          "acquisitionDate": 1524783436194,
          "acquisitionType": "DRAFT",
          "injuryStatus": "NORMAL",
          "…": "… 5 more keys"
        },
        "eligible_slots": [
          "WR/TE",
          "TE",
          "OP",
          "BE",
          "IR",
          "RB/WR/TE"
        ],
        "status": "active",
        "injury_status": "ACTIVE",
        "pro_team": "KC",
        "opponent": "LV",
        "projected_points": 13.85,
        "kickoff": "2018-12-30T21:25:00Z"
      },
      "slot": "TE",
      "is_starter": true,
      "raw": {
        "acquisitionDate": 1524783436194,
        "acquisitionType": "DRAFT",
        "injuryStatus": "NORMAL",
        "…": "… 5 more keys"
      },
      "is_locked": true
    },
    {
      "provider": "espn",
      "provider_id": "15835",
      "player": {
        "provider": "espn",
        "provider_id": "15835",
        "name": "Zach Ertz",
        "position": "TE",
        "raw": {
          "acquisitionDate": 1524783436194,
          "acquisitionType": "DRAFT",
          "injuryStatus": "NORMAL",
          "…": "… 5 more keys"
        },
        "eligible_slots": [
          "WR/TE",
          "TE",
          "OP",
          "BE",
          "IR",
          "RB/WR/TE"
        ],
        "status": "active",
        "injury_status": "ACTIVE",
        "pro_team": "PHI",
        "opponent": "WSH",
        "projected_points": 9.17,
        "kickoff": "2018-12-30T21:25:00Z"
      },
      "slot": "RB/WR/TE",
      "is_starter": true,
      "raw": {
        "acquisitionDate": 1524783436194,
        "acquisitionType": "DRAFT",
        "injuryStatus": "NORMAL",
        "…": "… 5 more keys"
      },
      "is_locked": true
    },
    "… 14 more items"
  ],
  "error": null
}
```
<!-- /sample -->

A past week's lineup, by team name, without `raw`:

<!-- sample: roster.week -->
```bash
fantasy-sports roster --team 'FANTASY GOD' --week 1 --no-raw
# ESPN public league 1234, season 2018 (live). Exit 0, stdout, trimmed with …:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:26Z",
  "data_as_of": "2026-09-12T19:31:25Z",
  "data_age_seconds": 1,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 1,
      "cached": true
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 1,
      "cached": true
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "mRoster",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 0,
      "cached": false
    }
  ],
  "untrusted": {},
  "raw_omitted": true,
  "data": [
    {
      "provider": "espn",
      "provider_id": "15825",
      "player": {
        "provider": "espn",
        "provider_id": "15825",
        "name": "Le'Veon Bell",
        "position": "RB",
        "eligible_slots": [
          "RB",
          "RB/WR",
          "OP",
          "BE",
          "IR",
          "RB/WR/TE"
        ],
        "status": "active",
        "injury_status": "OUT",
        "pro_team": "PIT",
        "opponent": "CLE",
        "projected_points": 0.0,
        "kickoff": "2018-09-09T17:00:00Z"
      },
      "slot": "RB",
      "is_starter": true,
      "is_locked": true
    },
    {
      "provider": "espn",
      "provider_id": "3115364",
      "player": {
        "provider": "espn",
        "provider_id": "3115364",
        "name": "Leonard Fournette",
        "position": "RB",
        "eligible_slots": [
          "RB",
          "RB/WR",
          "OP",
          "BE",
          "IR",
          "RB/WR/TE"
        ],
        "status": "active",
        "injury_status": "QUESTIONABLE",
        "pro_team": "JAX",
        "opponent": "NYG",
        "projected_points": 14.66,
        "kickoff": "2018-09-09T17:00:00Z"
      },
      "slot": "RB",
      "is_starter": true,
      "is_locked": true
    },
    "… 14 more items"
  ],
  "error": null
}
```
<!-- /sample -->

### `matchups`

Head-to-head pairings for one week. `--week` is a **scoring period** (the
NFL week a human means). ESPN's schedule is indexed by *matchup* period, and
the two diverge when a playoff round spans two NFL weeks; the adapter
resolves that and puts both identifiers on every matchup. Pairings are
symmetric (`team_a` / `team_b`, not home/away).

| Option | |
|---|---|
| `--week INTEGER` | NFL scoring period. Omitted: the league's current week |

<!-- sample: matchups -->
```bash
fantasy-sports matchups --week 1
# ESPN public league 1234, season 2018 (live). Exit 0, stdout, trimmed with …:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:27Z",
  "data_as_of": "2026-09-12T19:31:25Z",
  "data_age_seconds": 1,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 1,
      "cached": true
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 1,
      "cached": true
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 1,
      "cached": true
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "mMatchupScore",
      "fetched_at": "2026-09-12T19:31:27Z",
      "age_seconds": 0,
      "cached": false
    }
  ],
  "untrusted": {},
  "raw_omitted": false,
  "data": [
    {
      "provider": "espn",
      "provider_id": "0",
      "week": 1,
      "team_a_provider_id": "5",
      "team_a_score": 82.34,
      "team_b_provider_id": "9",
      "team_b_score": 120.12,
      "is_playoff": false,
      "raw": {
        "away": "… 6 keys",
        "home": "… 6 keys",
        "id": 0,
        "matchupPeriodId": 1,
        "…": "… 2 more keys"
      },
      "scoring_period_id": 1,
      "matchup_period_id": 1
    },
    {
      "provider": "espn",
      "provider_id": "1",
      "week": 1,
      "team_a_provider_id": "2",
      "team_a_score": 85.56,
      "team_b_provider_id": "3",
      "team_b_score": 76.24,
      "is_playoff": false,
      "raw": {
        "away": "… 6 keys",
        "home": "… 6 keys",
        "id": 1,
        "matchupPeriodId": 1,
        "…": "… 2 more keys"
      },
      "scoring_period_id": 1,
      "matchup_period_id": 1
    },
    "… 3 more items"
  ],
  "error": null
}
```
<!-- /sample -->

### `box-scores`

Both lineups for every matchup in a week, player by player, with the slot
each player occupied, their own position, the pro opponent, projected and
actual points, and whether the slot `started` (counted toward the score).
Bench points are ordinary arithmetic over `started` and `actual_points`;
the CLI does not compute them. ESPN serves box scores from 2019 onward, so
the public 2018 league cannot produce one — the real refusal is under
[`NOT_AVAILABLE`](#not_available--exit-10) — and this sample is replayed
from the synthetic cassette. That fixture's per-player stat rows are for a
different scoring period than the one requested, so every player reads
`0.0` here; a live read carries real projections and totals.

| Option | |
|---|---|
| `--week INTEGER` | NFL scoring period. Omitted: the league's current week |

<!-- sample: box-scores -->
```bash
fantasy-sports box-scores --week 1
# synthetic league 99, season 2026 (replayed). Exit 0, stdout, trimmed with …:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "99",
  "season": 2026,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": "2026-09-12T19:31:30Z",
  "data_age_seconds": 0,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-12T19:31:30Z",
      "age_seconds": 0,
      "cached": false
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-12T19:31:30Z",
      "age_seconds": 0,
      "cached": false
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-12T19:31:30Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-12T19:31:30Z",
      "age_seconds": 0,
      "cached": false
    },
    {
      "name": "mMatchupScore+mScoreboard",
      "fetched_at": "2026-09-12T19:31:30Z",
      "age_seconds": 0,
      "cached": false
    },
    {
      "name": "mPositionalRatings",
      "fetched_at": "2026-09-12T19:31:30Z",
      "age_seconds": 0,
      "cached": false
    }
  ],
  "untrusted": {},
  "raw_omitted": false,
  "data": [
    {
      "provider": "espn",
      "provider_id": "1-0",
      "raw": {
        "matchup_type": "NONE"
      },
      "week": 1,
      "team_a_provider_id": "1",
      "team_a_score": 151.8,
      "team_a_lineup": [
        {
          "provider": "espn",
          "provider_id": "2976316",
          "raw": {},
          "slot": "WR",
          "player_name": "Michael Thomas",
          "position": "WR",
          "pro_team": "NO",
          "pro_opponent": null,
          "projected_points": 0.0,
          "actual_points": 0.0,
          "started": true
        },
        {
          "provider": "espn",
          "provider_id": "3117251",
          "raw": {},
          "slot": "RB",
          "player_name": "Christian McCaffrey",
          "position": "RB",
          "pro_team": "CAR",
          "pro_opponent": null,
          "projected_points": 0.0,
          "actual_points": 0.0,
          "started": true
        },
        {
          "provider": "espn",
          "provider_id": "16799",
          "raw": {},
          "slot": "BE",
          "player_name": "Allen Robinson",
          "position": "WR",
          "pro_team": "CHI",
          "pro_opponent": null,
          "projected_points": 0.0,
          "actual_points": 0.0,
          "started": false
        },
        "… 12 more items"
      ],
      "team_b_provider_id": "2",
      "team_b_score": 128.0,
      "team_b_lineup": [
        {
          "provider": "espn",
          "provider_id": "2576434",
          "raw": {},
          "slot": "BE",
          "player_name": "Melvin Gordon",
          "position": "RB",
          "pro_team": "LAC",
          "pro_opponent": null,
          "projected_points": 0.0,
          "actual_points": 0.0,
          "started": false
        },
        {
          "provider": "espn",
          "provider_id": "13982",
          "raw": {},
          "slot": "WR",
          "player_name": "Julio Jones",
          "position": "WR",
          "pro_team": "ATL",
          "pro_opponent": null,
          "projected_points": 0.0,
          "actual_points": 0.0,
          "started": true
        },
        {
          "provider": "espn",
          "provider_id": "3116385",
          "raw": {},
          "slot": "RB",
          "player_name": "Joe Mixon",
          "position": "RB",
          "pro_team": "CIN",
          "pro_opponent": null,
          "projected_points": 0.0,
          "actual_points": 0.0,
          "started": true
        },
        "… 12 more items"
      ],
      "is_playoff": false,
      "scoring_period_id": 1,
      "matchup_period_id": 1
    }
  ],
  "error": null
}
```
<!-- /sample -->

### `free-agents`

Unrostered players, most-owned first as ESPN ranks them. `--pos` is checked
against ESPN's slot vocabulary *before* the request: an unknown position is
refused as `CONFIG_INVALID`, because ESPN would otherwise answer 200 with
its default player set and the wrong answer would look exactly like the
right one. `--limit` is passed upstream as the page size and applied again
to the result. Refused before 2019, like `box-scores`; replayed here.

| Option | |
|---|---|
| `--pos TEXT` | Position filter: `QB`, `RB`, `WR`, `TE`, `D/ST`, `K`, … Rejected if unknown |
| `--limit INTEGER` | Maximum players to return. Default 25 |
| `--week INTEGER` | NFL scoring period. Omitted: the league's current week |

<!-- sample: free-agents -->
```bash
fantasy-sports free-agents --pos WR --limit 5 --week 2
# synthetic league 99, season 2026 (replayed). Exit 0, stdout, trimmed with …:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "99",
  "season": 2026,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": "2026-09-12T19:31:30Z",
  "data_age_seconds": 0,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-12T19:31:30Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-12T19:31:30Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-12T19:31:30Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-12T19:31:30Z",
      "age_seconds": 0,
      "cached": true
    },
    {
      "name": "kona_player_info",
      "fetched_at": "2026-09-12T19:31:30Z",
      "age_seconds": 0,
      "cached": false
    },
    {
      "name": "mPositionalRatings",
      "fetched_at": "2026-09-12T19:31:30Z",
      "age_seconds": 0,
      "cached": false
    }
  ],
  "untrusted": {},
  "raw_omitted": false,
  "data": [
    {
      "provider": "espn",
      "provider_id": "3001",
      "player": {
        "provider": "espn",
        "provider_id": "3001",
        "name": "Barbara Liskov",
        "position": "WR",
        "raw": {
          "draftAuctionValue": 0,
          "id": 3001,
          "keeperValue": 0,
          "lineupLocked": false,
          "…": "… 6 more keys"
        },
        "eligible_slots": [
          "RB/WR",
          "WR",
          "WR/TE",
          "RB/WR/TE",
          "BE",
          "IR"
        ],
        "status": "active",
        "injury_status": "ACTIVE",
        "pro_team": "SEA",
        "opponent": "KC",
        "projected_points": 8.4,
        "kickoff": "2026-09-13T20:15:00Z"
      },
      "raw": {
        "draftAuctionValue": 0,
        "id": 3001,
        "keeperValue": 0,
        "lineupLocked": false,
        "…": "… 6 more keys"
      },
      "percent_owned": 61.5
    }
  ],
  "error": null
}
```
<!-- /sample -->

### `transactions`

Recent roster moves, newest first. ESPN's transaction view is scoped to one
scoring period with no date range, so the command walks scoring periods
backward from the current one, at most six, stopping as soon as `--limit`
is satisfied. Every period's fetch is its own entry in `sources`, and
`data_as_of` is the oldest of them. `type` is one of `add`, `drop`, `trade`,
`waiver_claim`; `players_in` and `players_out` are ESPN player ids, and the
names are in `raw.items`.

| Option | |
|---|---|
| `--limit INTEGER` | Most-recent moves to return. Default 25 |

<!-- sample: transactions -->
```bash
fantasy-sports transactions --limit 5
# ESPN public league 1234, season 2018 (live). Exit 0, stdout, trimmed with …:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:27Z",
  "data_as_of": "2026-09-12T19:31:25Z",
  "data_age_seconds": 1,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 1,
      "cached": true
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-12T19:31:25Z",
      "age_seconds": 1,
      "cached": true
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 1,
      "cached": true
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-12T19:31:26Z",
      "age_seconds": 1,
      "cached": true
    },
    {
      "name": "mTransactions2",
      "fetched_at": "2026-09-12T19:31:27Z",
      "age_seconds": 0,
      "cached": false
    }
  ],
  "untrusted": {},
  "raw_omitted": false,
  "data": [
    {
      "provider": "espn",
      "provider_id": "FREEAGENT-17",
      "type": "add",
      "raw": {
        "type": "FREEAGENT",
        "status": "EXECUTED",
        "scoringPeriodId": 17,
        "bidAmount": 0,
        "…": "… 2 more keys"
      },
      "team_provider_id": "8",
      "players_in": [
        "3059722"
      ],
      "players_out": [
        "3116721"
      ],
      "faab_spent": 0,
      "timestamp": "2018-12-27T14:11:34Z"
    },
    {
      "provider": "espn",
      "provider_id": "FREEAGENT-17",
      "type": "add",
      "raw": {
        "type": "FREEAGENT",
        "status": "EXECUTED",
        "scoringPeriodId": 17,
        "bidAmount": 0,
        "…": "… 2 more keys"
      },
      "team_provider_id": "8",
      "players_in": [
        "3068939"
      ],
      "players_out": [
        "2473037"
      ],
      "faab_spent": 0,
      "timestamp": "2018-12-27T14:11:04Z"
    },
    "… 3 more items"
  ],
  "error": null
}
```
<!-- /sample -->

### `raw`

The escape hatch: one ESPN view, passed straight through, payload
unmodified. Scoring settings, draft results, playoff formats, and anything
else the normalized model leaves alone are reachable here. Each `--view` is
fetched separately so its payload stays intact under its own key, and each
is its own `sources` entry. `--no-raw` is ignored — an unmodified payload is
the whole point — and `raw_omitted` is always `false`.

| Option | |
|---|---|
| `--view TEXT` | **Required.** ESPN view name; repeat for several |
| `--filter TEXT` | JSON for the `x-fantasy-filter` header ESPN scopes some views by |

<!-- sample: raw -->
```bash
fantasy-sports raw --view mSettings
# ESPN public league 1234, season 2018 (live). Exit 0, stdout, trimmed with …:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:27Z",
  "data_as_of": "2026-09-12T19:31:27Z",
  "data_age_seconds": 0,
  "sources": [
    {
      "name": "mSettings",
      "fetched_at": "2026-09-12T19:31:27Z",
      "age_seconds": 0,
      "cached": false
    }
  ],
  "untrusted": {},
  "raw_omitted": false,
  "data": {
    "mSettings": {
      "view": "mSettings",
      "filtered": false,
      "complete": true,
      "warning": null,
      "payload": {
        "draftDetail": {
          "drafted": true,
          "inProgress": false
        },
        "gameId": 1,
        "id": 1234,
        "scoringPeriodId": 18,
        "seasonId": 2018,
        "segmentId": 0,
        "settings": {
          "acquisitionSettings": "… 13 keys",
          "draftSettings": "… 12 keys",
          "financeSettings": "… 8 keys",
          "isAutoReactivate": false,
          "isCustomizable": false,
          "isPublic": true,
          "name": "Arizona 1234",
          "restrictionType": "NONE",
          "…": "… 5 more keys"
        },
        "status": {
          "activatedDate": 1524748380340,
          "createdAsLeagueType": 0,
          "currentLeagueType": 1,
          "currentMatchupPeriod": 15,
          "finalScoringPeriod": 17,
          "firstScoringPeriod": 1,
          "isActive": true,
          "isExpired": false,
          "…": "… 12 more keys"
        }
      }
    }
  },
  "error": null
}
```
<!-- /sample -->

Some views — `kona_player_info` is the known one — answer an unfiltered
request with a **default subset and a 200**. A partial player pool is
indistinguishable from a complete one by inspection, so the entry says so:
`complete: false` and a `warning` in words. Check `complete` before treating
a payload as the whole truth.

<!-- sample: raw.unfiltered -->
```bash
fantasy-sports raw --view kona_player_info
# ESPN public league 1234, season 2018 (live). Exit 0, stdout, trimmed with …:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:27Z",
  "data_as_of": "2026-09-12T19:31:27Z",
  "data_age_seconds": 0,
  "sources": [
    {
      "name": "kona_player_info",
      "fetched_at": "2026-09-12T19:31:27Z",
      "age_seconds": 0,
      "cached": false
    }
  ],
  "untrusted": {},
  "raw_omitted": false,
  "data": {
    "kona_player_info": {
      "view": "kona_player_info",
      "filtered": false,
      "complete": false,
      "warning": "ESPN answered 200 with its own default subset because no --filter was given. This is a partial result and must not be treated as the complete set for this view; supply --filter with an x-fantasy-filter JSON body to scope it.",
      "payload": {
        "players": "… 50 items"
      }
    }
  },
  "error": null
}
```
<!-- /sample -->

With a filter ESPN accepts, `filtered` and `complete` are both `true`:

<!-- sample: raw.filter -->
```bash
fantasy-sports raw --view kona_player_info --filter '{"players":{"limit":2,"sortPercOwned":{"sortPriority":1,"sortAsc":false}}}'
# ESPN public league 1234, season 2018 (live). Exit 0, stdout, trimmed with …:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:27Z",
  "data_as_of": "2026-09-12T19:31:27Z",
  "data_age_seconds": 0,
  "sources": [
    {
      "name": "kona_player_info",
      "fetched_at": "2026-09-12T19:31:27Z",
      "age_seconds": 0,
      "cached": false
    }
  ],
  "untrusted": {},
  "raw_omitted": false,
  "data": {
    "kona_player_info": {
      "view": "kona_player_info",
      "filtered": true,
      "complete": true,
      "warning": null,
      "payload": {
        "players": [
          "… 12 keys",
          "… 12 keys"
        ]
      }
    }
  },
  "error": null
}
```
<!-- /sample -->

### `doctor`

Every health check in one report, cheapest and most fixable first. `python`,
`config`, `credentials`, `cache`, `journal`, and `notify` never leave the machine. `notify`
reports whether `[notify] irreversible_command` is set; it never runs the command. `version` and
`provider_status` fetch the project's public health manifest (skipped with
`FANTASY_SPORTS_NO_HEALTH_CHECK=1`). `leagues_reachable` touches ESPN only
with `--live`. `data.status` is the worst individual check; `data.ok` is the
same thing as a boolean for a caller that only wants a gate. A bad finding is
data, not a failure: `doctor` exits 0 and never raises.

| Option | |
|---|---|
| `--live` | Also attempt one read against each configured league. Needs credentials |

<!-- sample: doctor -->
```bash
fantasy-sports doctor
# ESPN public league 1234, season 2018 (live). Exit 0, stdout:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-09-12T19:31:28Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": {
    "status": "warn",
    "ok": false,
    "checks": [
      {
        "name": "python",
        "status": "ok",
        "summary": "fantasy-sports 0.1.0.dev0 on Python 3.12.13.",
        "details": {
          "fantasy_sports_version": "0.1.0.dev0",
          "python_version": "3.12.13",
          "platform": "darwin",
          "dependencies": {
            "espn-api": "0.46.0",
            "typer": "0.27.2",
            "requests": "2.34.2",
            "keyring": "25.7.0",
            "tomli-w": "1.2.0"
          }
        }
      },
      {
        "name": "config",
        "status": "ok",
        "summary": "1 league(s) configured in ~/.config/fantasy-sports/config.toml.",
        "details": {
          "path": "~/.config/fantasy-sports/config.toml",
          "leagues": [
            "public"
          ],
          "default": "public"
        }
      },
      {
        "name": "credentials",
        "status": "ok",
        "summary": "ESPN credentials are configured.",
        "details": {
          "generated_at": "2026-09-12T19:31:27.898284Z",
          "complete": true,
          "credentials": [
            {
              "name": "espn_s2",
              "present": true,
              "source": "env",
              "age_days": null,
              "age_basis": "not-tracked:env",
              "stored_at": null,
              "last_success_at": null,
              "freshness": "unknown"
            },
            {
              "name": "swid",
              "present": true,
              "source": "env",
              "age_days": null,
              "age_basis": "not-tracked:env",
              "stored_at": null,
              "last_success_at": null,
              "freshness": "unknown"
            }
          ],
          "staleness_threshold": {
            "days": 30.0,
            "source": "default",
            "verified": false,
            "note": "Unverified heuristic, not a known ESPN cookie lifetime. ESPN publishes no expiry, and no library source or community report reviewed states a concrete one. Override with FANTASY_SPORTS_STALE_AFTER_DAYS."
          },
          "warnings": []
        }
      },
      {
        "name": "cache",
        "status": "ok",
        "summary": "Cache reachable: 10 entrie(s), 2,482,176 byte(s) at ~/.cache/fantasy-sports/http-cache.sqlite3.",
        "details": {
          "path": "~/.cache/fantasy-sports/http-cache.sqlite3",
          "entries": 10,
          "size_bytes": 2482176
        }
      },
      {
        "name": "journal",
        "status": "ok",
        "summary": "No journal yet at ~/.local/state/fantasy-sports/journal.sqlite; it is created by the first write.",
        "details": {
          "path": "~/.local/state/fantasy-sports/journal.sqlite",
          "exists": false,
          "entries": 0,
          "pending": 0,
          "pending_ids": [],
          "file_mode": null,
          "dir_mode": null,
          "permissions_ok": true
        }
      },
      {
        "name": "notify",
        "status": "ok",
        "summary": "Notifications are off; set [notify] irreversible_command to be told about adds, drops, and claims.",
        "details": {
          "enabled": false,
          "command_set": false
        }
      },
      {
        "name": "version",
        "status": "warn",
        "summary": "Could not reach the health manifest (offline, GitHub unreachable, or not yet published).",
        "details": {
          "your_version": "0.1.0.dev0"
        }
      },
      {
        "name": "provider_status",
        "status": "warn",
        "summary": "Could not reach the health manifest (offline, GitHub unreachable, or not yet published).",
        "details": {}
      },
      {
        "name": "leagues_reachable",
        "status": "skipped",
        "summary": "1 league(s) configured; pass --live to check reachability.",
        "details": {
          "leagues": [
            "public"
          ]
        }
      }
    ]
  },
  "error": null
}
```
<!-- /sample -->

### `auth status`

Which credentials are configured, from which link of the chain (`env`,
`keychain`, `config`), how old they are, and whether they are likely stale.
It never contacts ESPN and never prints a value. With both cookies supplied
through the environment, as a cron job would:

<!-- sample: auth-status.env -->
```bash
fantasy-sports auth status
# no league (live). Exit 0, stdout:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": null,
  "season": null,
  "generated_at": "2026-09-12T19:31:28Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": {
    "generated_at": "2026-09-12T19:31:28.138704Z",
    "complete": true,
    "credentials": [
      {
        "name": "espn_s2",
        "present": true,
        "source": "env",
        "age_days": null,
        "age_basis": "not-tracked:env",
        "stored_at": null,
        "last_success_at": null,
        "freshness": "unknown"
      },
      {
        "name": "swid",
        "present": true,
        "source": "env",
        "age_days": null,
        "age_basis": "not-tracked:env",
        "stored_at": null,
        "last_success_at": null,
        "freshness": "unknown"
      }
    ],
    "staleness_threshold": {
      "days": 30.0,
      "source": "default",
      "verified": false,
      "note": "Unverified heuristic, not a known ESPN cookie lifetime. ESPN publishes no expiry, and no library source or community report reviewed states a concrete one. Override with FANTASY_SPORTS_STALE_AFTER_DAYS."
    },
    "warnings": []
  },
  "error": null
}
```
<!-- /sample -->

With nothing configured anywhere — still exit 0, because an absent
credential is a fact to report, not a command failure:

<!-- sample: auth-status.missing -->
```bash
fantasy-sports auth status
# no league (replayed). Exit 0, stdout:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": null,
  "season": null,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": {
    "generated_at": "2026-09-12T19:31:30.553519Z",
    "complete": false,
    "credentials": [
      {
        "name": "espn_s2",
        "present": false,
        "source": null,
        "age_days": null,
        "age_basis": "absent",
        "stored_at": null,
        "last_success_at": null,
        "freshness": "missing"
      },
      {
        "name": "swid",
        "present": false,
        "source": null,
        "age_days": null,
        "age_basis": "absent",
        "stored_at": null,
        "last_success_at": null,
        "freshness": "missing"
      }
    ],
    "staleness_threshold": {
      "days": 30.0,
      "source": "default",
      "verified": false,
      "note": "Unverified heuristic, not a known ESPN cookie lifetime. ESPN publishes no expiry, and no library source or community report reviewed states a concrete one. Override with FANTASY_SPORTS_STALE_AFTER_DAYS."
    },
    "warnings": [
      "espn_s2: not configured. Run `fantasy-sports auth login`.",
      "swid: not configured. Run `fantasy-sports auth login`."
    ]
  },
  "error": null
}
```
<!-- /sample -->

### `auth login`

Prompts for `espn_s2` and `SWID` without echoing them and stores both in the
Keychain. The prompt guidance goes to stderr so stdout is still only the
envelope — which also means a *failed* login's stderr is guidance followed
by the error envelope, not one JSON document; this is the one command not
meant to be driven by a program. Nothing is written until both values
validate; a SWID pasted
without its braces is repaired and reported under `repaired`. No value ever
reaches the output. This sample answered the prompts itself and stored into
an in-memory Keychain:

<!-- sample: auth-login -->
```bash
fantasy-sports auth login
# no league (replayed). Exit 0, stdout. Prompts answered with placeholder cookies, the SWID pasted without braces; the prompt guidance went to stderr:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": null,
  "season": null,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": {
    "stored": [
      "espn_s2",
      "swid"
    ],
    "repaired": [
      "swid"
    ],
    "keychain_service": "fantasy-sports"
  },
  "error": null
}
```
<!-- /sample -->

`auth status` immediately afterwards reports the Keychain as the source and
an age of zero days:

<!-- sample: auth-status -->
```bash
fantasy-sports auth status
# no league (replayed). Exit 0, stdout:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": null,
  "season": null,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": {
    "generated_at": "2026-09-12T19:31:30.555946Z",
    "complete": true,
    "credentials": [
      {
        "name": "espn_s2",
        "present": true,
        "source": "keychain",
        "age_days": 0.0,
        "age_basis": "stored_at",
        "stored_at": "2026-09-12T19:31:30.554649Z",
        "last_success_at": null,
        "freshness": "fresh"
      },
      {
        "name": "swid",
        "present": true,
        "source": "keychain",
        "age_days": 0.0,
        "age_basis": "stored_at",
        "stored_at": "2026-09-12T19:31:30.554649Z",
        "last_success_at": null,
        "freshness": "fresh"
      }
    ],
    "staleness_threshold": {
      "days": 30.0,
      "source": "default",
      "verified": false,
      "note": "Unverified heuristic, not a known ESPN cookie lifetime. ESPN publishes no expiry, and no library source or community report reviewed states a concrete one. Override with FANTASY_SPORTS_STALE_AFTER_DAYS."
    },
    "warnings": []
  },
  "error": null
}
```
<!-- /sample -->

### `auth logout`

Removes both cookies from the Keychain and from `config.toml`'s
`[credentials]` table, and reports the outcome per link: `removed`,
`absent`, `still-set` (an environment variable it cannot unset, named in
`env_vars`), or `unavailable` (a locked Keychain or unreadable file — the
command then **fails** with `CONFIG_INVALID`, `details.kind:
"credential_store"`, because a value may still be on the machine). Nothing
stored anywhere is a success.

<!-- sample: auth-logout -->
```bash
fantasy-sports auth logout
# no league (replayed). Exit 0, stdout:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": null,
  "season": null,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": {
    "removed": [
      "espn_s2",
      "swid"
    ],
    "still_set": [],
    "unavailable": [],
    "credentials": [
      {
        "name": "espn_s2",
        "env": "absent",
        "env_vars": [],
        "keychain": "removed",
        "config": "absent"
      },
      {
        "name": "swid",
        "env": "absent",
        "env_vars": [],
        "keychain": "removed",
        "config": "absent"
      }
    ],
    "keychain_service": "fantasy-sports",
    "config_path": "~/.config/fantasy-sports/config.toml",
    "warnings": []
  },
  "error": null
}
```
<!-- /sample -->

### `lineup set`

Moves players on **your own team** to the slots you name, in one ESPN
transaction. The first command that writes to a league. `--set PLAYER=SLOT`
is repeatable and is an explicit **target**, never a relative move: it says
where each player should end up. `PLAYER` is a player id from `roster`, or a
case-insensitive name that matches exactly one player on that roster; `SLOT`
is a slot name as `roster` prints it (`QB`, `RB`, `WR`, `TE`, `RB/WR/TE`,
`D/ST`, `K`, `BE`, `IR`).

What happens, in order:

1. A **fresh** read of the team's roster and the league's slot counts,
   whatever `--fresh` says.
2. The diff: one move per named player whose slot differs, vacates (to `BE`
   or `IR`) listed first. A target that already matches is refused, because
   ESPN has no no-op transaction.
3. **Preflight**, each a `CONFIG_INVALID` with `details.kind` of `argument`
   that names the player, before anything is journaled or sent: a slot not in
   the player's `eligible_slots`; a player whose game has started (`is_locked`)
   and would have to move; a final lineup that holds more players in a slot
   than the league allows (ESPN checks the final lineup, not each move); a
   player named twice; a name matching no player or more than one.
4. **Ownership**: a `--team` the configured session does not own is
   `WRITE_REJECTED` with `details.kind` of `not_owned`, whatever the
   session's league-manager role. There is no override.
5. A journal entry with the prior slots, status `pending`, **before** the
   request. A journal that cannot be written stops the write.
6. One POST for the league's current scoring period. There is no `--week`:
   ESPN accepts a lineup write only for the current period. A rejection is
   `WRITE_REJECTED`, and any failure known not to have sent closes the entry
   `rejected`. A timeout is `PROVIDER_UNAVAILABLE` with
   `details.transaction_state` of `unknown`, and the entry stays `pending`,
   because nobody knows whether it applied.
7. **Read-back**: the roster is re-read live and compared slot for slot for
   every named player. A mismatch is `WRITE_DIVERGED`, with the differing
   players in `details.diverged` and the transaction and journal ids
   alongside; the entry closes `diverged`. A read-back that cannot be made
   at all is still a success, with `read_back.verified` false and a
   `warnings` line, because ESPN did execute the change.

`data` is one object: `team_id`, `kind` (`lineup`), `provider_transaction_id`
and `proposed_at` (ESPN's own, to join against `transactions`), `status` and
`is_pending`, `items` (`player_id`, `from_slot`, `to_slot` per moved
player), `journal_entry_id`, `reversible` (`true`), `acknowledged_irreversible`
(`null`; only the irreversible writes carry one), `cost`, `credentials`
(`espn_s2_age_seconds` and a `warning`, so an old cookie shows up on a
success rather than on the failure after it), `read_back` (`verified`,
`diverged`), and `dry_run`. `warnings` appears only when there is one.

| Option | |
|---|---|
| `--team` | Your team's id or name. Required |
| `--set` | `PLAYER=SLOT`, repeatable. Required |
| `--dry-run` | Report the `items` that would be sent; send and journal nothing |

<!-- sample: lineup-set -->
```bash
fantasy-sports lineup set --team 1 --set 2002=BE --set 'Katherine Johnson=WR'
# synthetic league 99, season 2026 (replayed). Exit 0, stdout. The clock is fixed before the week's kickoffs, so nothing is locked and the ages are large:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "99",
  "season": 2026,
  "generated_at": "2026-10-15T06:36:50Z",
  "data_as_of": "2026-09-10T12:00:00Z",
  "data_age_seconds": 3004610,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3004610,
      "cached": false
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3004610,
      "cached": false
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3004610,
      "cached": false
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3004610,
      "cached": false
    }
  ],
  "untrusted": {},
  "raw_omitted": false,
  "data": {
    "team_id": "1",
    "kind": "lineup",
    "provider_transaction_id": "5d0c6e2a-7f41-4b9e-a3c8-1e6f2b9d4a70",
    "status": "EXECUTED",
    "is_pending": false,
    "proposed_at": "2026-09-10T12:00:01Z",
    "items": [
      {
        "player_id": "2002",
        "from_slot": "RB",
        "to_slot": "BE"
      },
      {
        "player_id": "2003",
        "from_slot": "BE",
        "to_slot": "WR"
      }
    ],
    "journal_entry_id": "6e1f0b3a-2c4d-4e8f-9a1b-3c5d7e9f1a2b",
    "reversible": true,
    "acknowledged_irreversible": null,
    "cost": {
      "bid_amount": null,
      "displaced_player_id": null
    },
    "credentials": {
      "espn_s2_age_seconds": null,
      "warning": "credential age unknown"
    },
    "read_back": {
      "verified": true,
      "diverged": []
    },
    "dry_run": false
  },
  "error": null
}
```
<!-- /sample -->

`--dry-run` runs every preflight and the ownership check, then stops. The
ids are `null`, `read_back` is `null`, `credentials` is filled, and `note`
says plainly that ESPN was not asked: it has no validate-only mode, so a dry
run cannot promise ESPN would accept the change.

<!-- sample: lineup-set.dry-run -->
```bash
fantasy-sports lineup set --team 1 --set 2002=BE --set 'Katherine Johnson=WR' --dry-run
# synthetic league 99, season 2026 (replayed). Exit 0, stdout:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "99",
  "season": 2026,
  "generated_at": "2026-10-15T06:36:50Z",
  "data_as_of": "2026-09-10T12:00:00Z",
  "data_age_seconds": 3004610,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3004610,
      "cached": false
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3004610,
      "cached": false
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3004610,
      "cached": false
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3004610,
      "cached": false
    },
    {
      "name": "mNav",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3004610,
      "cached": false
    }
  ],
  "untrusted": {},
  "raw_omitted": false,
  "data": {
    "team_id": "1",
    "kind": "lineup",
    "provider_transaction_id": null,
    "status": null,
    "is_pending": null,
    "proposed_at": null,
    "items": [
      {
        "player_id": "2002",
        "from_slot": "RB",
        "to_slot": "BE"
      },
      {
        "player_id": "2003",
        "from_slot": "BE",
        "to_slot": "WR"
      }
    ],
    "journal_entry_id": null,
    "reversible": true,
    "acknowledged_irreversible": null,
    "cost": {
      "bid_amount": null,
      "displaced_player_id": null
    },
    "credentials": {
      "espn_s2_age_seconds": null,
      "warning": "credential age unknown"
    },
    "read_back": null,
    "dry_run": true,
    "note": "Dry run: nothing was sent and nothing was journaled. The items were computed from the roster just read; ESPN has no validate-only mode, so whether it would accept them was not checked."
  },
  "error": null
}
```
<!-- /sample -->

### `add`

Adds a free agent to **your own team**, optionally dropping a player in the
same ESPN transaction (a `FREEAGENT` transaction with an `ADD` item, and a
`DROP` item with `--drop`). **Irreversible**: undoing an add later is a drop,
and a dropped player goes to waivers where any team can claim the player first.

`--player` is a player id or a case-insensitive name that matches exactly one
player in this week's free-agent pool, the same pool `free-agents` lists. A
rostered player is not in the pool and is refused. `--drop` is a player id or
a name unique on your roster. `--slot` places the added player (`QB`, `RB`,
`WR`, `TE`, `RB/WR/TE`, `D/ST`, `K`, `BE`); omitted, ESPN places the player.

What happens, in order:

1. A **fresh** read of the team's roster and this week's free-agent pool.
2. **Preflight**, each a `CONFIG_INVALID` with `details.kind` of `argument`
   naming the player, before anything is journaled or sent: a player not in
   the pool, or a name matching more than one; a player on waivers (use
   `claim`); a `--slot` not in the player's `eligible_slots`; a `--drop` that
   is not on the roster.
3. **Ownership**, as for `lineup set`: a team the session does not own is
   `WRITE_REJECTED` with `details.kind` of `not_owned`. No override exists.
4. **The acknowledgement gate.** Without `--acknowledge-irreversible "<why>"`,
   or with a blank one, the command stops with `CONFIG_INVALID` whose message
   says exactly what this move cannot undo. The reason you pass is journaled
   verbatim and echoed as `data.acknowledged_irreversible`. The tool cannot
   tell whether a `/sanity-gate` ran; an autonomous agent clears one before
   it passes the flag (see the README's Writing section).
5. A journal entry, status `pending`, `reversible: false`, with what the move
   **costs** (`cost.bid_amount`, `cost.displaced_player_id`), the dropped
   player's slot, and the added player's pool status, **before** the request.
6. One POST for the league's current scoring period. A rejection is
   `WRITE_REJECTED` with ESPN's `TRAN_*` type verbatim in
   `details.provider_type`. Two kinds carry their own re-plan:
   `player_on_waivers` (claim instead) and `player_unavailable` (the player
   is on another roster; pick someone else). A type this tool has not mapped
   yet is
   `details.kind` `unknown`, never dropped. A timeout is
   `PROVIDER_UNAVAILABLE` with `details.transaction_state` `unknown`, is
   never retried, and leaves the entry `pending`: read `transactions` before
   doing anything else.
7. **Read-back**: the roster is re-read live. The added player must be on it
   (in `--slot`, if one was named) and the dropped player gone; anything else
   is `WRITE_DIVERGED` with both ids, and the entry closes `diverged`.
8. **The owner is told.** With a `[notify] irreversible_command` configured,
   the stored journal entry goes to it on stdin
   ([README](../README.md#being-told-about-irreversible-writes)). The result
   is `data.notified` (`enabled`, `attempted`, `ok`, `exit_status`, `error`);
   a failed notification never fails the write.

A success purges this league's cached rosters, matchups, transactions, and
free agents, so the next read of any of them is fresh.

`data` is one object with the same keys as `lineup set`, plus `notified`:
`team_id`, `kind` (`add`), `provider_transaction_id`, `status`,
`is_pending` (`false`), `proposed_at`, `items` (`player_id`, `type` of `add`
or `drop`, `from_slot`, `to_slot`), `journal_entry_id`, `reversible`
(`false`), `acknowledged_irreversible`, `cost`, `credentials`, `read_back`
(`verified`, `diverged`), `notified`, and `dry_run`. `warnings` appears only
when there is one.

| Option | |
|---|---|
| `--team` | Your team's id or name. Required |
| `--player` | The free agent to add. Required |
| `--drop` | A player on your roster to release in the same transaction |
| `--slot` | Lineup slot for the added player |
| `--acknowledge-irreversible` | Why. Required to send; not needed with `--dry-run` |
| `--dry-run` | Report items, cost, and what cannot be undone; send and journal nothing |

<!-- sample: add -->
```bash
fantasy-sports add --team 1 --player 'Barbara Liskov' --drop 'Katherine Johnson' --acknowledge-irreversible 'Johnson is out for the season; Liskov starts this week'
# synthetic league 99, season 2026 (replayed). Exit 0, stdout. The write host answers the community-attested FREEAGENT shape; no [notify] command is configured:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "99",
  "season": 2026,
  "generated_at": "2026-10-15T07:03:29Z",
  "data_as_of": "2026-09-10T12:00:00Z",
  "data_age_seconds": 3006209,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006209,
      "cached": false
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006209,
      "cached": false
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006209,
      "cached": false
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006209,
      "cached": false
    }
  ],
  "untrusted": {},
  "raw_omitted": false,
  "data": {
    "team_id": "1",
    "kind": "add",
    "provider_transaction_id": "5d0c6e2a-7f41-4b9e-a3c8-1e6f2b9d4a70",
    "status": "EXECUTED",
    "is_pending": false,
    "proposed_at": "2026-09-10T12:00:01Z",
    "items": [
      {
        "player_id": "3001",
        "type": "add",
        "from_slot": null,
        "to_slot": null
      },
      {
        "player_id": "2003",
        "type": "drop",
        "from_slot": "BE",
        "to_slot": null
      }
    ],
    "journal_entry_id": "ad5e4f70-6b8c-4d9e-bfa0-2b3c4d5e6f70",
    "reversible": false,
    "acknowledged_irreversible": "Johnson is out for the season; Liskov starts this week",
    "cost": {
      "bid_amount": null,
      "displaced_player_id": "2003"
    },
    "credentials": {
      "espn_s2_age_seconds": null,
      "warning": "credential age unknown"
    },
    "read_back": {
      "verified": true,
      "diverged": []
    },
    "notified": {
      "enabled": false,
      "attempted": false,
      "ok": null,
      "exit_status": null,
      "error": null
    },
    "dry_run": false
  },
  "error": null
}
```
<!-- /sample -->

`--dry-run` needs no acknowledgement. It runs every preflight and the
ownership check, then reports the items, the `cost`, the statement the gate
would refuse with under `irreversible_because`, and a `note` that ESPN was
not asked whether it would accept the move.

<!-- sample: add.dry-run -->
```bash
fantasy-sports add --team 1 --player 'Barbara Liskov' --drop 'Katherine Johnson' --dry-run
# synthetic league 99, season 2026 (replayed). Exit 0, stdout:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "99",
  "season": 2026,
  "generated_at": "2026-10-15T07:03:29Z",
  "data_as_of": "2026-09-10T12:00:00Z",
  "data_age_seconds": 3006209,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006209,
      "cached": false
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006209,
      "cached": false
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006209,
      "cached": false
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006209,
      "cached": false
    },
    {
      "name": "kona_player_info",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006209,
      "cached": false
    },
    {
      "name": "mPositionalRatings",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006209,
      "cached": false
    },
    {
      "name": "mNav",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006209,
      "cached": false
    }
  ],
  "untrusted": {},
  "raw_omitted": false,
  "data": {
    "team_id": "1",
    "kind": "add",
    "provider_transaction_id": null,
    "status": null,
    "is_pending": null,
    "proposed_at": null,
    "items": [
      {
        "player_id": "3001",
        "type": "add",
        "from_slot": null,
        "to_slot": null
      },
      {
        "player_id": "2003",
        "type": "drop",
        "from_slot": "BE",
        "to_slot": null
      }
    ],
    "journal_entry_id": null,
    "reversible": false,
    "acknowledged_irreversible": null,
    "cost": {
      "bid_amount": null,
      "displaced_player_id": "2003"
    },
    "credentials": {
      "espn_s2_age_seconds": null,
      "warning": "credential age unknown"
    },
    "read_back": null,
    "notified": null,
    "dry_run": true,
    "irreversible_because": "Barbara Liskov (3001) joins the roster, and undoing that later is a drop that sends the player to waivers. Katherine Johnson (2003) goes to waivers and can be claimed by any team; this cannot be reversed.",
    "note": "Dry run: nothing was sent and nothing was journaled. The items were computed from the roster and free-agent pool just read; ESPN has no validate-only mode, so whether it would accept them (roster size, position limits, budget, waiver order) was not checked. A real run needs --acknowledge-irreversible."
  },
  "error": null
}
```
<!-- /sample -->

### `drop`

Releases a player from **your own team** (a `ROSTER` transaction with one
`DROP` item; ESPN accepts `FREEAGENT` only with exactly one `ADD`). **Irreversible**: the player goes to waivers and can be claimed
by any team; re-adding is a separate add, and only if nobody claims the player first.
`--player` is a player id or a name unique on your roster.

The flow is `add`'s: fresh read, preflight, ownership, the acknowledgement
gate, journal, one POST, read-back (the player must be off the roster), and
the notify hook. `cost.displaced_player_id` is the dropped player.

| Option | |
|---|---|
| `--team` | Your team's id or name. Required |
| `--player` | The player to release. Required |
| `--acknowledge-irreversible` | Why. Required to send; not needed with `--dry-run` |
| `--dry-run` | Report the item, cost, and what cannot be undone; send and journal nothing |

<!-- sample: drop.dry-run -->
```bash
fantasy-sports drop --team 1 --player 2003 --dry-run
# synthetic league 99, season 2026 (replayed). Exit 0, stdout:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "99",
  "season": 2026,
  "generated_at": "2026-10-15T07:03:29Z",
  "data_as_of": "2026-09-10T12:00:00Z",
  "data_age_seconds": 3006209,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006209,
      "cached": false
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006209,
      "cached": false
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006209,
      "cached": false
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006209,
      "cached": false
    },
    {
      "name": "mNav",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006209,
      "cached": false
    }
  ],
  "untrusted": {},
  "raw_omitted": false,
  "data": {
    "team_id": "1",
    "kind": "drop",
    "provider_transaction_id": null,
    "status": null,
    "is_pending": null,
    "proposed_at": null,
    "items": [
      {
        "player_id": "2003",
        "type": "drop",
        "from_slot": "BE",
        "to_slot": null
      }
    ],
    "journal_entry_id": null,
    "reversible": false,
    "acknowledged_irreversible": null,
    "cost": {
      "bid_amount": null,
      "displaced_player_id": "2003"
    },
    "credentials": {
      "espn_s2_age_seconds": null,
      "warning": "credential age unknown"
    },
    "read_back": null,
    "notified": null,
    "dry_run": true,
    "irreversible_because": "Katherine Johnson (2003) goes to waivers and can be claimed by any team; this cannot be reversed.",
    "note": "Dry run: nothing was sent and nothing was journaled. The items were computed from the roster and free-agent pool just read; ESPN has no validate-only mode, so whether it would accept them (roster size, position limits, budget, waiver order) was not checked. A real run needs --acknowledge-irreversible."
  },
  "error": null
}
```
<!-- /sample -->

Without `--acknowledge-irreversible` it sends nothing; the refusal is in
[`CONFIG_INVALID`](#config_invalid--exit-6).

### `claim`

Submits a waiver claim for a player on waivers, optionally with a FAAB bid
and the player to drop if it processes (a `WAIVER` transaction, with
`bidAmount` only when `--bid` is given). **Irreversible once processed**: a processed claim consumes
waiver priority, and FAAB where the league uses it, and restoring the roster
later gives neither back.

`--player` must be in this week's pool with ESPN status on waivers; a free
agent is refused (use `add`). `--bid` is a whole number of 0 or more. Omit it
in a league without an acquisition budget: ESPN rejects any bid there, even
`1`, as `TRAN_ACQUISITION_BUDGET_NOT_SUPPORTED`. The command reads the
league's `acquisitionSettings` from the read it already made, and refuses a
`--bid` in such a league with `CONFIG_INVALID` before anything is sent. A
claim's `--dry-run` reports `acquisition_type` and `uses_budget`, `null` when
ESPN does not say, so an agent learns the rule without a failed attempt.
`--drop` is as for `add`.

The flow is `add`'s, with two differences. ESPN answers a claim
`is_pending: true`: nothing changes on the roster until waivers process, and
`provider_transaction_id` is the id `claim cancel` will take (#109). The
read-back therefore looks in this period's `transactions` for a pending
waiver claim by your team that matches everything you submitted: the player,
the `--drop`, and the bid, which ESPN records as `0` when none was sent.
Exactly one match is `verified: true` with `matched_by`. Several identical
pending claims are `verified: null`, because this one cannot be told apart
from an earlier one. No match is `verified: null` with a `note`, never
`true`: `transactions` does not carry ESPN's transaction ids, so a claim it
does not list is unverified, not missing. A match proves the claim was
queued and nothing later: a cancel is a new transaction, and the claim's own
row keeps reading `PENDING` after it. A timed-out claim is never re-sent,
because a second claim is a second bid.

`cost.bid_amount` is the bid, `null` without one; `cost.displaced_player_id`
is `--drop`.

| Option | |
|---|---|
| `--team` | Your team's id or name. Required |
| `--player` | The player on waivers to claim. Required |
| `--bid` | FAAB bid, 0 or more. Omit in a league without a budget |
| `--drop` | A player on your roster to release if the claim processes |
| `--acknowledge-irreversible` | Why. Required to send; not needed with `--dry-run` |
| `--dry-run` | Report items, cost, and what cannot be undone; send and journal nothing |

<!-- sample: claim.dry-run -->
```bash
fantasy-sports claim --team 1 --player 'Radia Perlman' --bid 7 --dry-run
# synthetic league 99, season 2026 (replayed). Exit 0, stdout:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "99",
  "season": 2026,
  "generated_at": "2026-10-15T07:13:19Z",
  "data_as_of": "2026-09-10T12:00:00Z",
  "data_age_seconds": 3006799,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006799,
      "cached": false
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006799,
      "cached": false
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006799,
      "cached": false
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006799,
      "cached": false
    },
    {
      "name": "kona_player_info",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006799,
      "cached": false
    },
    {
      "name": "mPositionalRatings",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006799,
      "cached": false
    },
    {
      "name": "mNav",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3006799,
      "cached": false
    }
  ],
  "untrusted": {},
  "raw_omitted": false,
  "data": {
    "team_id": "1",
    "kind": "claim",
    "provider_transaction_id": null,
    "status": null,
    "is_pending": null,
    "proposed_at": null,
    "items": [
      {
        "player_id": "3002",
        "type": "add",
        "from_slot": null,
        "to_slot": null
      }
    ],
    "journal_entry_id": null,
    "reversible": false,
    "acknowledged_irreversible": null,
    "cost": {
      "bid_amount": 7,
      "displaced_player_id": null
    },
    "credentials": {
      "espn_s2_age_seconds": null,
      "warning": "credential age unknown"
    },
    "read_back": null,
    "notified": null,
    "dry_run": true,
    "irreversible_because": "A processed claim for Radia Perlman (3002) consumes waiver priority (and FAAB where enabled), and restoring the roster later gives neither back.",
    "acquisition_type": null,
    "uses_budget": true,
    "note": "Dry run: nothing was sent and nothing was journaled. The items were computed from the roster and free-agent pool just read; ESPN has no validate-only mode, so whether it would accept them (roster size, position limits, budget, waiver order) was not checked. A real run needs --acknowledge-irreversible."
  },
  "error": null
}
```
<!-- /sample -->

### `journal list`

The mutation journal, newest first: every write this tool attempted, who ESPN
says made it, and how it ended. The journal is a SQLite file at
`$XDG_STATE_HOME/fantasy-sports/journal.sqlite` (default
`~/.local/state/fantasy-sports/`), created `0600` inside a `0700` directory.
It is written in two phases: an entry exists, with the prior state and the
request, **before** anything is sent, and is closed afterwards. An entry whose
`status` is still `pending` is a write that may have gone out without its
outcome being recorded, and `doctor` reports it.

The command reads a local file: no league is resolved, no credential is
needed, and the envelope's `provider`, `league_id`, and `season` are `null`.
A machine that has never written is an empty list, not an error; a journal
that exists but cannot be read is an error, because "no entries" would be a
lie about an audit trail. `lineup set` and `journal reverse` write to it; the
sample below is seeded so it reads the same on every regeneration.

| Option | |
|---|---|
| `--league`, `-l` | Only entries for this league: a profile name from `config.toml`, or a raw league id. A filter, not a target |
| `--limit` | Most-recent entries to return. Default 25; below 1 is `CONFIG_INVALID` |
| `--irreversible` | Only entries that cannot be reversed (`add`, `drop`, `claim`) |

<!-- sample: journal-list -->
```bash
fantasy-sports journal list
# synthetic league 99, season 2026 (replayed). Exit 0, stdout. A seeded journal, so the sample is the same on every regeneration:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-10-15T05:53:06Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": [
    {
      "id": "c47d19ab-52e0-4f3c-8a16-9e0b3d7a2c55",
      "recorded_at": "2026-09-12T18:33:40.215Z",
      "provider": "espn",
      "league_id": "99",
      "season": 2026,
      "scoring_period": 1,
      "command": [
        "fantasy-sports",
        "claim",
        "--team",
        "1",
        "--bid",
        "7"
      ],
      "member_id": null,
      "role": "creator",
      "team_id": "1",
      "kind": "claim",
      "reversible": false,
      "prior_state": {
        "9000012": 4
      },
      "applied_state": null,
      "request": {
        "teamId": 1,
        "type": "WAIVER",
        "bidAmount": 7
      },
      "provider_transaction_id": null,
      "proposed_at": null,
      "cost": {
        "bid_amount": 7,
        "displaced_player_id": "9000012"
      },
      "status": "pending",
      "reverses": null,
      "error": null
    },
    {
      "id": "0f8a9c2e-6b1d-4e57-9a3c-2d7b5e1f4a60",
      "recorded_at": "2026-09-12T18:29:24.880Z",
      "provider": "espn",
      "league_id": "99",
      "season": 2026,
      "scoring_period": 1,
      "command": [
        "fantasy-sports",
        "lineup",
        "set",
        "--team",
        "1"
      ],
      "member_id": "{00000000-0A47-F4CD-713D-FE2C76EB2524}",
      "role": "creator",
      "team_id": "1",
      "kind": "lineup",
      "reversible": true,
      "prior_state": {
        "9000013": 4,
        "9000012": 20
      },
      "applied_state": {
        "9000013": 20,
        "9000012": 4
      },
      "request": {
        "teamId": 1,
        "type": "ROSTER",
        "scoringPeriodId": 1,
        "executionType": "EXECUTE",
        "items": [
          {
            "playerId": 9000013,
            "type": "LINEUP",
            "fromLineupSlotId": 4,
            "toLineupSlotId": 20
          },
          {
            "playerId": 9000012,
            "type": "LINEUP",
            "fromLineupSlotId": 20,
            "toLineupSlotId": 4
          }
        ]
      },
      "provider_transaction_id": "3b1f6c52-8d0e-4a47-9b6a-5c2e7d9f0a11",
      "proposed_at": "2026-09-12T18:29:25.019Z",
      "cost": {
        "bid_amount": null,
        "displaced_player_id": null
      },
      "status": "executed",
      "reverses": null,
      "error": null
    }
  ],
  "error": null
}
```
<!-- /sample -->

### `journal digest`

The journal entries recorded since a point in time, newest first, in the
standard envelope. It is the pull half of being told about irreversible writes
(jwulff/fantasy-sports#113): a cron job or an agent runs it on a schedule and
hands the result to whatever delivers news to the owner. The push half is the
`[notify]` hook in the [README](../README.md#being-told-about-irreversible-writes).

Like `journal list` it reads a local file: no league is resolved, no credential
is needed, and the envelope's `provider`, `league_id`, and `season` are `null`.
An empty window is an empty list.

`--since` is required. It is either a duration counted back from now (`30m`,
`24h`, `7d`) or an ISO-8601 timestamp that carries its offset
(`2026-10-14T08:00:00Z`). A timestamp without an offset, or anything else, is
`CONFIG_INVALID` with `details.argument` of `since`. The window is on
`recorded_at`, when the entry was begun, so a write begun before the window and
closed inside it shows up only in the earlier window, with its final status.
Overlap the windows and de-duplicate on `id` when that matters.

| Option | |
|---|---|
| `--since` | Start of the window: a duration (`24h`, `7d`, `30m`) or an ISO-8601 UTC timestamp. Required |
| `--irreversible` | Only entries that cannot be reversed (`add`, `drop`, `claim`) |
| `--league`, `-l` | Only entries for this league: a profile name from `config.toml`, or a raw league id. A filter, not a target |
| `--limit` | Most-recent entries to return. Default 100; below 1 is `CONFIG_INVALID` |

<!-- sample: journal-digest -->
```bash
fantasy-sports journal digest --since 2026-09-12T00:00:00Z --irreversible
# synthetic league 99, season 2026 (replayed). Exit 0, stdout. A seeded journal; the claim was begun and never closed, so it is still pending:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-10-15T06:17:37Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": [
    {
      "id": "c47d19ab-52e0-4f3c-8a16-9e0b3d7a2c55",
      "recorded_at": "2026-09-12T18:33:40.215Z",
      "provider": "espn",
      "league_id": "99",
      "season": 2026,
      "scoring_period": 1,
      "command": [
        "fantasy-sports",
        "claim",
        "--team",
        "1",
        "--bid",
        "7"
      ],
      "member_id": null,
      "role": "creator",
      "team_id": "1",
      "kind": "claim",
      "reversible": false,
      "prior_state": {
        "9000012": 4
      },
      "applied_state": null,
      "request": {
        "teamId": 1,
        "type": "WAIVER",
        "bidAmount": 7
      },
      "provider_transaction_id": null,
      "proposed_at": null,
      "cost": {
        "bid_amount": 7,
        "displaced_player_id": "9000012"
      },
      "status": "pending",
      "reverses": null,
      "error": null
    }
  ],
  "error": null
}
```
<!-- /sample -->

### `journal show`

One entry in full. `data` is the entry, with these fields:

| Field | Meaning |
|---|---|
| `id`, `recorded_at` | A UUID, and when the intent was recorded (UTC) |
| `provider`, `league_id`, `season`, `scoring_period` | What was written to |
| `command` | The argv as invoked, redacted |
| `member_id` | The acting member as ESPN's **response** echoed it, stored as a pseudonym; `null` until the write completes |
| `role` | `creator`, `manager`, `member`, or `null`: the session's authority in that league |
| `team_id`, `kind` | The team written, and `lineup`, `add`, `drop`, `claim`, `cancel`, or `reverse` |
| `reversible` | Whether a reversal can be built from `prior_state` |
| `prior_state`, `applied_state` | The slots before the request, and as read back after it |
| `request` | The body sent |
| `provider_transaction_id`, `proposed_at` | ESPN's own `id` and `proposedDate`, to join against its transaction log |
| `cost` | `bid_amount` and `displaced_player_id` |
| `status` | `pending`, `executed`, `rejected`, or `diverged` |
| `reverses` | For a `reverse` entry, the id it undoes |
| `error` | The error payload for a `rejected` or `diverged` entry |

Credentials never reach the file: every string passes the redaction layer,
and SWID GUIDs, including `member_id`, are stored as per-journal
pseudonyms, stable across entries so "same member each time" can be read off
the list.

An unknown `--id` is `CONFIG_INVALID` with `details.kind` of `argument` and
the nearest ids in `details.nearest`.

<!-- sample: journal-show -->
```bash
fantasy-sports journal show --id 0f8a9c2e-6b1d-4e57-9a3c-2d7b5e1f4a60
# synthetic league 99, season 2026 (replayed). Exit 0, stdout. A seeded journal, so the sample is the same on every regeneration:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-10-15T05:53:06Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": {
    "id": "0f8a9c2e-6b1d-4e57-9a3c-2d7b5e1f4a60",
    "recorded_at": "2026-09-12T18:29:24.880Z",
    "provider": "espn",
    "league_id": "99",
    "season": 2026,
    "scoring_period": 1,
    "command": [
      "fantasy-sports",
      "lineup",
      "set",
      "--team",
      "1"
    ],
    "member_id": "{00000000-0A47-F4CD-713D-FE2C76EB2524}",
    "role": "creator",
    "team_id": "1",
    "kind": "lineup",
    "reversible": true,
    "prior_state": {
      "9000013": 4,
      "9000012": 20
    },
    "applied_state": {
      "9000013": 20,
      "9000012": 4
    },
    "request": {
      "teamId": 1,
      "type": "ROSTER",
      "scoringPeriodId": 1,
      "executionType": "EXECUTE",
      "items": [
        {
          "playerId": 9000013,
          "type": "LINEUP",
          "fromLineupSlotId": 4,
          "toLineupSlotId": 20
        },
        {
          "playerId": 9000012,
          "type": "LINEUP",
          "fromLineupSlotId": 20,
          "toLineupSlotId": 4
        }
      ]
    },
    "provider_transaction_id": "3b1f6c52-8d0e-4a47-9b6a-5c2e7d9f0a11",
    "proposed_at": "2026-09-12T18:29:25.019Z",
    "cost": {
      "bid_amount": null,
      "displaced_player_id": null
    },
    "status": "executed",
    "reverses": null,
    "error": null
  },
  "error": null
}
```
<!-- /sample -->

### `journal reverse`

Undoes one lineup change by sending its inverse: every player the entry
named goes back to the slot its `prior_state` recorded. It is the same flow
as `lineup set`, preflight, ownership, journal, one POST, and read-back,
against the league the entry names (a profile for that league must still be
in `config.toml`), journaled as a new entry of kind `reverse` whose
`reverses` is the original's id. A reverse can itself be reversed.

Only an `executed` or `diverged` lineup (or reverse) entry qualifies. A
`pending` or `rejected` entry is `CONFIG_INVALID`: nothing is known to have
applied. An `add`, `drop`, `claim`, or `cancel` is `NOT_AVAILABLE` and
**nothing is sent**; `remediation` says what a human would have to do
instead. A player who is now locked is refused in preflight, or by ESPN as
`WRITE_REJECTED` with `details.kind` of `roster_lock`; one transaction never
half-applies. A lineup that is already back where it was is an empty diff.

`data` is the `lineup set` object with `kind` of `reverse` and `reverses`.

| Option | |
|---|---|
| `--id` | The entry to undo, as `journal list` prints it. Required |
| `--dry-run` | Report the moves the reversal would send; send and journal nothing |

<!-- sample: journal-reverse -->
```bash
fantasy-sports journal reverse --id 6e1f0b3a-2c4d-4e8f-9a1b-3c5d7e9f1a2b
# synthetic league 99, season 2026 (replayed). Exit 0, stdout. Reverses the lineup-set sample above, against the same replayed league:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "99",
  "season": 2026,
  "generated_at": "2026-10-15T06:36:50Z",
  "data_as_of": "2026-09-10T12:00:00Z",
  "data_age_seconds": 3004610,
  "sources": [
    {
      "name": "mTeam+mRoster+mMatchup+mSettings+mStandings",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3004610,
      "cached": false
    },
    {
      "name": "players_wl",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3004610,
      "cached": false
    },
    {
      "name": "proTeamSchedules_wl",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3004610,
      "cached": false
    },
    {
      "name": "mDraftDetail",
      "fetched_at": "2026-09-10T12:00:00Z",
      "age_seconds": 3004610,
      "cached": false
    }
  ],
  "untrusted": {},
  "raw_omitted": false,
  "data": {
    "team_id": "1",
    "kind": "reverse",
    "provider_transaction_id": "5d0c6e2a-7f41-4b9e-a3c8-1e6f2b9d4a70",
    "status": "EXECUTED",
    "is_pending": false,
    "proposed_at": "2026-09-10T12:00:01Z",
    "items": [
      {
        "player_id": "2003",
        "from_slot": "WR",
        "to_slot": "BE"
      },
      {
        "player_id": "2002",
        "from_slot": "BE",
        "to_slot": "RB"
      }
    ],
    "journal_entry_id": "7a2b1c4d-3e5f-4a6b-8c7d-9e0f1a2b3c4d",
    "reversible": true,
    "acknowledged_irreversible": null,
    "cost": {
      "bid_amount": null,
      "displaced_player_id": null
    },
    "credentials": {
      "espn_s2_age_seconds": null,
      "warning": "credential age unknown"
    },
    "read_back": {
      "verified": true,
      "diverged": []
    },
    "dry_run": false,
    "reverses": "6e1f0b3a-2c4d-4e8f-9a1b-3c5d7e9f1a2b"
  },
  "error": null
}
```
<!-- /sample -->

## Errors

Every failure is one JSON document on stderr, with the same key set as a
success — `data` is `null`, `error` is not — and stdout is byte-empty, so a
parser reading stdout never sees half a payload. The process exits with the
code's own status, so a cron job or an agent can branch without parsing
anything. One qualification: when stderr is a **terminal**, and only then, a
`PROVIDER_UNAVAILABLE` or `SCHEMA_DRIFT` failure may be followed by a short
prose paragraph of upgrade or known-outage guidance for the person watching;
piped stderr is always exactly the one document.

| Code | Exit | Meaning | `retryable` | Agent response |
|---|---|---|---|---|
| `AUTH_MISSING` | 3 | No credentials configured | no | Ask the human to run `auth login` |
| `AUTH_EXPIRED` | 4 | ESPN rejected the cookies | no | Ask the human to re-extract them |
| `LEAGUE_NOT_FOUND` | 5 | Bad league id, no access, unknown `--league`, or unknown `--team` | no | Ask the human |
| `CONFIG_INVALID` | 6 | `config.toml` will not parse, or an argument only ESPN can validate was rejected | no | Ask the human to fix the named input |
| `PROVIDER_UNAVAILABLE` | 7 | ESPN 5xx, timeout, or an unclassifiable failure | yes | Retry with bounded backoff |
| `RATE_LIMITED` | 8 | ESPN throttled the request | yes | Retry after `details.retry_after` |
| `SCHEMA_DRIFT` | 9 | A response no longer has the shape the adapter reads | no | Stop; file an issue |
| `NOT_AVAILABLE` | 10 | ESPN positively refuses this request; it will never succeed as asked | no | Do not retry; read `remediation` |
| `WRITE_REJECTED` | 11 | A write was refused, by ESPN or by the tool before sending; nothing applied | no | Do not retry unchanged; read the roster and re-plan |
| `WRITE_DIVERGED` | 12 | ESPN accepted a write, but the read-back disagrees | no | Do not retry; read the roster and reconcile |

`0` is success, `1` an unclassified crash, `2` a usage error. Inside
`error`, every key is always present: `code`, `message`, `retryable`,
`agent_action` (the class-level instruction), `remediation` (this
instance's concrete next step, `null` when there is none), `details` (field
names, paths, and status codes — never a provider body, never a credential),
and `health`. `health` is `null` except on `PROVIDER_UNAVAILABLE` and
`SCHEMA_DRIFT`, where the client health check may fold in the project's
manifest: `your_version`, `latest_version`, `upgrade_available`,
`upgrade_command`, `provider_status`, and a `known_issue` when the manifest
lists one for that code. An unclassifiable failure lands on
`PROVIDER_UNAVAILABLE`, never on `RATE_LIMITED`: telling an agent to retry
is safe when we are wrong; telling it to back off and wait is not.

### `AUTH_MISSING` — exit 3

No cookie in the environment, the Keychain, or `config.toml`. Raised before
any request is made. `remediation` names the exact environment variables
that would satisfy it:

<!-- sample: error-auth-missing -->
```bash
fantasy-sports standings
# ESPN public league 1234, season 2018 (live). Exit 3, stderr. No cookie in the environment, the Keychain, or config.toml:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "AUTH_MISSING",
    "message": "No credentials configured for: espn_s2, swid.",
    "retryable": false,
    "agent_action": "Ask the human to run `fantasy-sports auth login`.",
    "remediation": "Run `fantasy-sports auth login`, or set FANTASY_SPORTS_ESPN_S2 and FANTASY_SPORTS_SWID in the environment.",
    "details": null,
    "health": null
  }
}
```
<!-- /sample -->

### `AUTH_EXPIRED` — exit 4

Defined for a response that positively proves the credential is dead. No
observed ESPN response does: probed one variable at a time, ESPN's 401 body
is byte-identical for no cookies, a bad `espn_s2` with a valid `SWID`, and
either cookie alone (`docs/memory/espn-401-tells-you-nothing.md`). So today a
rejected cookie surfaces as [`LEAGUE_NOT_FOUND`](#league_not_found--exit-5)
with `details.reason: "credentials_or_membership"`, and this code is never
emitted. The shape, should ESPN ever start distinguishing:

<!-- sample: error-auth-expired -->
```bash
fantasy-sports standings
# no league contacted (synthetic). Exit 4, stderr. No observed ESPN response proves a cookie is dead, so this code has never been emitted; a rejected cookie surfaces as LEAGUE_NOT_FOUND today:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "AUTH_EXPIRED",
    "message": "ESPN rejected the configured credentials.",
    "retryable": false,
    "agent_action": "Ask the human to re-extract their ESPN cookies.",
    "remediation": "Re-extract espn_s2 and SWID from a logged-in browser session and run `fantasy-sports auth login`.",
    "details": {
      "view": "mTeam",
      "status": 401
    },
    "health": null
  }
}
```
<!-- /sample -->

### `LEAGUE_NOT_FOUND` — exit 5

Anything that is genuinely not there: a `--league` name missing from
`config.toml`, a league id ESPN does not know (or a season it does not have,
as under [`--season`](#--season) above), a private league these cookies
cannot see, or a `--team` that matches no team or more than one.

An unknown profile name — the message lists what is configured:

<!-- sample: error-league-not-found -->
```bash
fantasy-sports standings --league nope
# ESPN public league 1234, season 2018 (live). Exit 5, stderr:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "LEAGUE_NOT_FOUND",
    "message": "no league named 'nope' (--league) in ~/.config/fantasy-sports/config.toml; configured: public",
    "retryable": false,
    "agent_action": "Ask the human to confirm the league id and their access.",
    "remediation": null,
    "details": null,
    "health": null
  }
}
```
<!-- /sample -->

No `config.toml` at all:

<!-- sample: error-no-config -->
```bash
fantasy-sports standings
# no league (live). Exit 5, stderr. No config.toml at all:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "LEAGUE_NOT_FOUND",
    "message": "no league given and no 'default' set in ~/.config/fantasy-sports/config.toml; no leagues are configured. Pass --league <name>.",
    "retryable": false,
    "agent_action": "Ask the human to confirm the league id and their access.",
    "remediation": null,
    "details": null,
    "health": null
  }
}
```
<!-- /sample -->

A `--team` nobody has — the message lists every id and name so the next call
can be right without a second command:

<!-- sample: error-league-not-found.team -->
```bash
fantasy-sports roster --team Nobody
# ESPN public league 1234, season 2018 (live). Exit 5, stderr:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "LEAGUE_NOT_FOUND",
    "message": "no team matches --team 'Nobody'. Teams in this league: 1='Team 1', 2='Team 2', 3='FANTASY GOD', 4='THE KING', 5='Team 5', 6='Team Viking Queen', 7='Team 7', 8='Team 8', 9='Team Mizrachi', 10='Team 10'.",
    "retryable": false,
    "agent_action": "Ask the human to confirm the league id and their access.",
    "remediation": "Run `fantasy-sports teams` to list the ids in this league.",
    "details": {
      "team": "Nobody"
    },
    "health": null
  }
}
```
<!-- /sample -->

A `--team` that matches several; the CLI names them all rather than picking
one, because a roster is the input to a lineup decision:

<!-- sample: error-league-not-found.ambiguous -->
```bash
fantasy-sports roster --team Team
# ESPN public league 1234, season 2018 (live). Exit 5, stderr:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "LEAGUE_NOT_FOUND",
    "message": "--team 'Team' matches more than one team: 'Team 1' (id 1), 'Team 10' (id 10), 'Team 2' (id 2), 'Team 5' (id 5), 'Team 7' (id 7), 'Team 8' (id 8), 'Team Mizrachi' (id 9), 'Team Viking Queen' (id 6).",
    "retryable": false,
    "agent_action": "Ask the human to confirm the league id and their access.",
    "remediation": "Pass the team id, or a name fragment that matches one team.",
    "details": {
      "team": "Team",
      "matches": 8
    },
    "health": null
  }
}
```
<!-- /sample -->

### `CONFIG_INVALID` — exit 6

Two causes, one code, because both tell an agent the identical thing — stop,
a human must change the input named in the message, retrying unchanged
cannot work. `details.kind` says which: `config` for a file that will not
parse, `argument` for a CLI argument only ESPN can validate (an unknown
`--pos`, a `--filter` that is not JSON). `auth logout` uses a third,
`credential_store`, when a link it needed could not be reached. A bad
argument the CLI *can* validate itself (`--output yaml`, a missing required
option) is a plain usage error, exit 2, with no envelope.

A `config.toml` missing a closing bracket:

<!-- sample: error-config-invalid -->
```bash
fantasy-sports standings
# ESPN public league 1234, season 2018 (live). Exit 6, stderr. config.toml is missing a closing bracket:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "CONFIG_INVALID",
    "message": "~/.config/fantasy-sports/config.toml is not valid TOML: Expected ']' at the end of a table declaration (at line 2, column 16)",
    "retryable": false,
    "agent_action": "Ask the human to fix the input named in the message \u2014 a config value or a command argument. Retrying unchanged cannot work.",
    "remediation": null,
    "details": {
      "kind": "config"
    },
    "health": null
  }
}
```
<!-- /sample -->

A `--pos` ESPN does not recognise, refused before the request is sent:

<!-- sample: error-config-invalid.pos -->
```bash
fantasy-sports free-agents --pos PUNTER --week 2
# synthetic league 99, season 2026 (replayed). Exit 6, stderr:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "CONFIG_INVALID",
    "message": "'PUNTER' is not an ESPN position. Valid values: QB, RB, WR, TE, D/ST, K, FLEX, DT, DE, LB, DL, CB, S, DB, DP, HC.",
    "retryable": false,
    "agent_action": "Ask the human to fix the input named in the message \u2014 a config value or a command argument. Retrying unchanged cannot work.",
    "remediation": "Pass a position ESPN recognises (see the valid values above), or omit --pos to get ESPN's default set.",
    "details": {
      "pos": "PUNTER",
      "kind": "argument"
    },
    "health": null
  }
}
```
<!-- /sample -->

An irreversible write (`add`, `drop`, `claim`) without
`--acknowledge-irreversible` is the same code with `details.kind` of
`argument`. The message states what that one invocation cannot undo, and
`details` carries the same statement and the move's `cost`. Nothing was
sent or journaled; pass the flag with a reason, or `--dry-run`.

<!-- sample: error-config-invalid.acknowledge -->
```bash
fantasy-sports drop --team 1 --player 'Katherine Johnson'
# synthetic league 99, season 2026 (replayed). Exit 6, stderr. An irreversible write without --acknowledge-irreversible; nothing is sent:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-10-15T07:03:29Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "CONFIG_INVALID",
    "message": "This drop cannot be undone, and --acknowledge-irreversible was not given. Katherine Johnson (2003) goes to waivers and can be claimed by any team; this cannot be reversed.",
    "retryable": false,
    "agent_action": "Ask the human to fix the input named in the message \u2014 a config value or a command argument. Retrying unchanged cannot work.",
    "remediation": "Pass --acknowledge-irreversible \"<why>\" with the reason for this move; it is journaled verbatim. An autonomous agent must clear /sanity-gate before passing it. Use --dry-run to preview the move without the flag.",
    "details": {
      "flag": "--acknowledge-irreversible",
      "move": "drop",
      "irreversible_because": "Katherine Johnson (2003) goes to waivers and can be claimed by any team; this cannot be reversed.",
      "cost": {
        "bid_amount": null,
        "displaced_player_id": "2003"
      },
      "kind": "argument"
    },
    "health": null
  }
}
```
<!-- /sample -->

A `--filter` that is not JSON:

<!-- sample: error-config-invalid.filter -->
```bash
fantasy-sports raw --view mSettings --filter not-json
# ESPN public league 1234, season 2018 (live). Exit 6, stderr:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "CONFIG_INVALID",
    "message": "--filter must be JSON for the x-fantasy-filter header: Expecting value: line 1 column 1 (char 0)",
    "retryable": false,
    "agent_action": "Ask the human to fix the input named in the message \u2014 a config value or a command argument. Retrying unchanged cannot work.",
    "remediation": "Pass something like --filter '{\"players\":{\"limit\":50}}'.",
    "details": {
      "filter": "not-json",
      "kind": "argument"
    },
    "health": null
  }
}
```
<!-- /sample -->

### `PROVIDER_UNAVAILABLE` — exit 7

ESPN is down, timed out, or failed in a way the adapter cannot classify.
`retryable: true`; bounded exponential backoff is the right response.
`details.cause` is the exception type, for a human with logs to grep.

<!-- sample: error-provider-unavailable -->
```bash
fantasy-sports standings
# no league contacted (synthetic). Exit 7, stderr:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "PROVIDER_UNAVAILABLE",
    "message": "ESPN request failed during fetch_league: HTTPSConnectionPool(host='lm-api-reads.fantasy.espn.com', port=443): Read timed out. (read timeout=30)",
    "retryable": true,
    "agent_action": "Retry with bounded exponential backoff.",
    "remediation": null,
    "details": {
      "view": "mTeam",
      "cause": "ReadTimeout"
    },
    "health": null
  }
}
```
<!-- /sample -->

### `RATE_LIMITED` — exit 8

Only on a positive throttle signal (HTTP 429), never as a guess.
`details.retry_after` is ESPN's `Retry-After` in seconds when the header was
present, and absent when it was not.

<!-- sample: error-rate-limited -->
```bash
fantasy-sports standings
# no league contacted (synthetic). Exit 8, stderr:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "RATE_LIMITED",
    "message": "ESPN throttled this request (HTTP 429).",
    "retryable": true,
    "agent_action": "Retry after `details.retry_after` seconds.",
    "remediation": "Wait for `details.retry_after` seconds, then retry.",
    "details": {
      "status": 429,
      "view": "mTeam",
      "retry_after": 30.0
    },
    "health": null
  }
}
```
<!-- /sample -->

### `SCHEMA_DRIFT` — exit 9

A response no longer has the shape the adapter reads. `details.path` says
where, by field name, never by value. This is the signal the health system
exists to produce: the daily canary files an issue on it, and the client
check folds any matching known issue into `health`. Do not retry; file an
issue with the payload.

<!-- sample: error-schema-drift -->
```bash
fantasy-sports standings
# no league contacted (synthetic). Exit 9, stderr, trimmed with …:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "1234",
  "season": 2018,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "SCHEMA_DRIFT",
    "message": "ESPN's mTeam response no longer has the shape espn-api reads (missing key 'teams', in League._fetch_teams).",
    "retryable": false,
    "agent_action": "Stop and file an issue; the provider's response shape changed.",
    "remediation": "File an issue with this error payload; the provider's response shape changed.",
    "details": {
      "view": "mTeam",
      "operation": "fetch_standings",
      "cause": "KeyError",
      "path": "… 3 items",
      "…": "… 1 more key"
    },
    "health": null
  }
}
```
<!-- /sample -->

### `NOT_AVAILABLE` — exit 10

The opposite of unavailable: a *positively classifiable* refusal, made
before any request goes out, for something that will never succeed as
asked. Box scores and free agents before 2019 are the two cases today.
`remediation` names the supported alternative.

<!-- sample: error-not-available -->
```bash
fantasy-sports box-scores --week 1
# ESPN public league 1234, season 2018 (live). Exit 10, stderr. ESPN does not serve box scores before 2019:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "NOT_AVAILABLE",
    "message": "ESPN does not serve box scores for the 2018 season.",
    "retryable": false,
    "agent_action": "Do not retry as sent; check `remediation` for a supported alternative.",
    "remediation": "Box scores exist from 2019 onward. Use `matchups` for an earlier season; it returns team-level scores.",
    "details": {
      "season": "2018",
      "week": "1"
    },
    "health": null
  }
}
```
<!-- /sample -->

<!-- sample: error-not-available.free-agents -->
```bash
fantasy-sports free-agents --pos RB --limit 5
# ESPN public league 1234, season 2018 (live). Exit 10, stderr:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-09-12T19:31:30Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "NOT_AVAILABLE",
    "message": "ESPN does not serve free agents for the 2018 season.",
    "retryable": false,
    "agent_action": "Do not retry as sent; check `remediation` for a supported alternative.",
    "remediation": "Free agents exist from 2019 onward. Use `transactions` for an earlier season; it shows roster moves that already happened.",
    "details": {
      "season": "2018",
      "week": "17"
    },
    "health": null
  }
}
```
<!-- /sample -->

### `WRITE_REJECTED` — exit 11

A write that did not apply. One code for every reason, with the reason in
`details.kind`: `roster_lock`, `not_owned`, `auth`, and the rest of ADR-0010's
closed vocabulary. `details.provider_type` is ESPN's own `TRAN_*` string,
`remediation` is ESPN's message, and `details.transaction_applied` is always
`false`: one ESPN transaction is validated and applied atomically, so a
rejection changed nothing. A `not_owned` refusal is the tool's, before any
request.

<!-- sample: error-write-rejected -->
```bash
fantasy-sports lineup set --team 1 --set 2002=BE --set 'Katherine Johnson=WR'
# synthetic league 99, season 2026 (replayed). Exit 11, stderr. The write host answers with the 409 captured in brief 05 (p8):
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-10-15T06:36:50Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "WRITE_REJECTED",
    "message": "ESPN rejected the transaction (TRAN_LINEUP_LOCKED).",
    "retryable": false,
    "agent_action": "Do not retry unchanged; read the roster and re-plan.",
    "remediation": "Lineup transaction could not be completed, Player 9000007 is locked",
    "details": {
      "kind": "roster_lock",
      "provider_type": "TRAN_LINEUP_LOCKED",
      "http_status": 409,
      "team_id": "1",
      "transaction_applied": false
    },
    "health": null
  }
}
```
<!-- /sample -->

### `WRITE_DIVERGED` — exit 12

The opposite failure: ESPN said yes and the roster does not match what was
asked, because something moved it between the read and the write. The
change may well have applied, so retrying could double-apply it.
`details.diverged` lists the player ids that differ, `expected` and
`observed` their slots, and `provider_transaction_id` and
`journal_entry_id` the records of what was sent.

<!-- sample: error-write-diverged -->
```bash
fantasy-sports lineup set --team 1 --set 2002=BE --set 'Katherine Johnson=WR'
# synthetic league 99, season 2026 (replayed). Exit 12, stderr. The write host answers 200 EXECUTED and the read-back still shows the old lineup:
```
```json
{
  "schema": "fantasy-sports/v1",
  "provider": null,
  "league_id": null,
  "season": null,
  "generated_at": "2026-10-15T06:36:50Z",
  "data_as_of": null,
  "data_age_seconds": null,
  "sources": [],
  "untrusted": {},
  "raw_omitted": false,
  "data": null,
  "error": {
    "code": "WRITE_DIVERGED",
    "message": "ESPN accepted the lineup change, but the roster read back differently for 2 player(s).",
    "retryable": false,
    "agent_action": "Do not retry; the write may have applied. Read the roster and reconcile.",
    "remediation": "Something changed the roster between the read and the write, or ESPN applied it differently. Read the roster and decide before writing again; do not resend the same change blind.",
    "details": {
      "diverged": [
        "2002",
        "2003"
      ],
      "team_id": "1",
      "provider_transaction_id": "5d0c6e2a-7f41-4b9e-a3c8-1e6f2b9d4a70",
      "journal_entry_id": "9c4d3e6f-5a7b-4c8d-ae9f-1a2b3c4d5e6f",
      "transaction_state": "applied",
      "expected": {
        "2002": "BE",
        "2003": "WR"
      },
      "observed": {
        "2002": "RB",
        "2003": "BE"
      }
    },
    "health": null
  }
}
```
<!-- /sample -->

### Usage errors — exit 2

Arguments the CLI can validate on its own are refused by the argument
parser, with prose on stderr and no envelope, exactly as every other
argument parser on the machine does it:

<!-- sample: error-usage -->
```bash
fantasy-sports standings --output yaml
# ESPN public league 1234, season 2018 (live). Exit 2, stderr:
```
```text
Usage: fantasy-sports standings [OPTIONS]
Try 'fantasy-sports standings --help' for help.

Error: Invalid value for --output: Unknown output format 'yaml'; expected one of json, table, csv
```
<!-- /sample -->

<!-- sample: error-usage.missing -->
```bash
fantasy-sports roster
# ESPN public league 1234, season 2018 (live). Exit 2, stderr:
```
```text
Usage: fantasy-sports roster [OPTIONS]
Try 'fantasy-sports roster --help' for help.

Error: Missing option '--team'.
```
<!-- /sample -->

### Exit 1

An unclassified crash: something raised that never reached the taxonomy.
There is no sample because there is no contract for it; if you see one,
file an issue with the traceback.
