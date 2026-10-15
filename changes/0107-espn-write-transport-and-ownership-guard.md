# The ESPN write transport and its ownership guard (#107)

Every write the epic (#105) adds goes through what this change builds, and
the cookie it holds is a commissioner's. ESPN honoured that cookie on another
manager's team without being asked to act as league manager
(`docs/research/05-espn-write-surface.md` §6), so ESPN will not stop a
`--team` typo. The refusal has to be ours. The same unit has to make three
other promises before any command can safely write. Only one secret goes on
the wire. A write whose outcome is unknown is never retried blind. The cache
is coherent after a success.

## What changed

- **`core/models.py`** gains three frozen dataclasses. `SessionRole` holds
  `provider`, `member_id`, `role` (`creator`|`manager`|`member`),
  `owned_team_ids`, and `raw`. `RosterMove` holds `player_id`, `from_slot`,
  and `to_slot`, as slot names. `WriteResult` holds `provider`,
  `provider_transaction_id`, `status`, `is_pending`, `team_id`, `items`,
  `credentials`, `raw`, `proposed_at`, `bid_amount`, and `member_id`.
  `SESSION_ROLES` is exported too. None of them joins `NORMALIZED_MODELS`.
  A role and a write receipt are not league objects read from a provider, and
  they have no `provider_id`. `RosterMove` is an input.
- **`providers/base.py`** gains `WritingProvider`, a second
  `runtime_checkable` Protocol, and `WRITING_PROVIDER_METHODS`. Its methods
  are `session_role`, `set_lineup`, `move_players(*, adds, drops, bid=None)`,
  and `cancel_transaction`. `Provider` is unchanged, so a read-only adapter
  still conforms.
- **`providers/espn.py`** implements it:
  - `session_role` reads `view=mNav` through the existing read transport.
    The view is cached under the league-settings TTL. The role is `creator`
    if `isLeagueCreator`, else `manager` if `isLeagueManager`, else `member`.
    Owned teams are the `teams[].owners` entries that contain the session's
    member. The adapter falls back to the bootstrap's teams when `mNav` has
    none, and it never reads `mLeagueManager`.
  - The ownership guard runs in every write method before a body exists. An
    unowned team is `WRITE_REJECTED` with `kind: "not_owned"`, and the
    remediation names the owned ids. Unknown ownership, which means no SWID
    and no echoed member, is refused the same way. No override parameter
    exists.
  - Each write is one POST to `lm-api-writes` with only
    `cookies={"espn_s2": …}`. It sends the web client's three headers,
    `isLeagueManager: false`, `teamId` as an int, and the league's current
    `scoringPeriodId` from the bootstrap. It sends no `memberId`. A
    `bidAmount` appears only on `WAIVER`, and a cancel sends `CANCEL` with
    `relatedTransactionId` and no items. The timeout is 15 s.
  - `set_lineup` reads the roster and diffs it against the target. Only
    changed slots become `LINEUP` items, with vacates first. An empty diff, an
    unknown slot name, a player not on the roster, or an ineligible slot is
    `CONFIG_INVALID` with `kind: "argument"`.
  - Every status maps to the taxonomy, as the next section lists.
  - A 200 purges the league tag and the scoring-period tag through
    `purge_by_league_tag` before the body is parsed. It also drops the
    memoised league. The response is recorded on `last_fetch` as an uncached
    `transactions` source.
  - `credential_report()` computes the R12a block from `build_auth_status`
    before the request. Every result carries it, and a dry-run can call it
    directly.
  - `http_post` is a new constructor seam. It is resolved at call time, so
    the default is `requests.post`, imported inside the method.
- **`tests/unit/test_espn_writes.py`** adds 128 offline tests. Every write
  response is a capture body from `docs/research/05-espn-write-surface/`
  against league `99`.
- **`tests/_fake_provider.py`** gains `WritingFake` for #15, #16, and #18.
- **`tests/live/test_espn_writes_live.py`** checks `session_role` only. Its
  `http_post` fails the test if called.
- ARCHITECTURE §6 and §9 are updated, along with the CHANGELOG and a memory
  note.

## Status mapping

| Wire | Raised |
|---|---|
| 200 | `WriteResult` (after the purge) |
| 401, cookie sent | `WRITE_REJECTED` `kind: "auth"`, `provider_type` verbatim — never `AUTH_EXPIRED` |
| 401, nothing sent | `AUTH_MISSING` (unreachable through the public methods, which refuse earlier) |
| 400 without `details[]` | `SCHEMA_DRIFT`, `path: ["transactions"]` |
| 400 typed, 409 | `WRITE_REJECTED`, `kind` from the `TRAN_*` map, else `unknown`; `remediation` is ESPN's `messages[0]` |
| 429 | `RATE_LIMITED` with `Retry-After` |
| 5xx, anything else | `PROVIDER_UNAVAILABLE`, `transaction_state: "unknown"` |
| any exception from the POST | `PROVIDER_UNAVAILABLE`, `transaction_state: "unknown"`, sent once |

## Decisions worth recording

- **Reads see pseudonyms, not the SWID.** A command's reads always go
  through a `CacheStore`, which rewrites every SWID in a body to a per-store
  pseudonym before the adapter parses it. Matching the raw SWID against
  `mNav` would own nothing. The adapter matches both spellings and reports the
  pseudonym. It scrubs the write response with the same salt, so the echoed
  `memberId` joins to `session_role`'s, and the SWID never reaches output.
  Store-less tests could not have caught this. One test runs through a real
  store, and a memory note records the trap.
- **Every transport exception means unknown state.** `requests` can retry a
  connection internally, and a reset can arrive after the body went out. No
  exception from the POST proves that nothing applied. Calling any of them
  "not applied" would be the double spend brief §8 warns about.
- **The purge runs before parsing.** A 200 whose body cannot be read still
  applied. That case raises `SCHEMA_DRIFT` with `transaction_state:
  "unknown"`, and the cache has already forgotten the stale roster.
- **Lineup preflight in the adapter is limited.** It refuses an empty diff,
  an unknown slot name, a player not on the roster, and an ineligible slot.
  Each of these reads data the diff already holds. Lock state and slot counts
  are left to #15's preflight and to ESPN, because lock state in a cached
  roster can be stale and ESPN's answer is typed.
- **`POSITION_MAP`'s string keys are the wrong map.** They omit `BE`, `IR`,
  and `RB/WR/TE`, so slot names map through its integer keys instead.
- **The add, drop, waiver, and cancel shapes are unconfirmed.** `ADD`
  carries `toTeamId`, `DROP` carries `fromTeamId`, and a cancel's `type` is
  `WAIVER`. All of this is community-attested. #108 captures the real shapes
  in a throwaway league and may amend `move_players` and
  `cancel_transaction`.
- **The README samples are unchanged.** `WRITE_REJECTED` and
  `WRITE_DIVERGED` stay in `AWAITING_A_COMMAND`, because no command raises
  them yet and a real sample needs one.

## Review fixes (Codex on PR #118)

- **Ownership is now tied to the session that writes.** Ownership comes from
  the SWID, but ESPN authenticates the write with `espn_s2`. The credential
  chain resolves the two cookies independently, so they could belong to two
  accounts. No pre-write identity endpoint is trusted, so there is no probe.
  Instead, after a 200 the echoed `memberId` pseudonym is compared with the
  member the guard checked. On a mismatch the write is reported as
  `WRITE_DIVERGED`. Its details carry `expected_member`, `observed_member`,
  the transaction id, the team, the status, `proposed_at`, the items, and
  `transaction_state: "applied"`, so a command can still journal it. The pair
  is also recorded for that league, and every later write in the process is
  refused as `not_owned` with an instruction to re-run `auth login`.
  `doctor` now warns, without any network call, when `espn_s2` and `swid`
  resolve from different links of the chain.
- **Residual risk.** A mismatched pair configured by hand is caught after
  its first write, not before. That write may already have touched a team
  this session was not verified to own. `auth login` prevents the case by
  storing both cookies together, and `doctor` flags the configuration most
  likely to produce it.
- **The lineup diff uses the current roster.** The diff previously read
  whatever `mRoster` the instance had retained, so it could use a historical
  week after a `fetch_roster(week=N)`. It now fetches `view=mRoster` for the
  write's current `scoringPeriodId` and diffs against that response. A test
  loads week 1 and then sets the lineup to prove it.
- **That roster read bypasses the cache.** It is still a cache-mode
  question even when the fetch is explicit, because a current-period roster
  under its 5-minute TTL would come back from the store. The read runs under
  a per-call `CacheMode.BYPASS` override on the transport. That override
  neither reads nor writes the store, and the store still scrubs the body.
  The provider's own mode is untouched. A test caches a stale roster, shows
  an ordinary read still serves it, and proves the write diffs against the
  live one.

## Validation

- `uv run pytest -m "not live" --cov`: 1687 passed, 16 deselected, after
  merging `origin/main`. Coverage is 99.06% total, `providers/espn.py` is 97%, and `base.py` and
  `models.py` are 100%.
- `ruff check` and `ruff format --check` pass.
- `check_budgets.py` passes with 5/5 dependencies and the wheel within
  budget. `check_startup.py` passes, with `--help` well under 50 ms.
- `test_imports.py` still passes, so `requests` is not imported at module
  scope.
- Disabling the guard, and separately the 200 purge, turned 12 tests red.
  Both were then restored.
- No live ESPN request was made. The live test was collected and skipped,
  never run. No mutation tool runs in this repo or in CI, so the ≥80%
  mutation-score criterion was not measured.
