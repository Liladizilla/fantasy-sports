# Changelog

All notable changes to `fantasy-sports` are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

The v0.1 read-only milestone: an agent-native CLI over the ESPN fantasy
football API, distributed via `uv tool install fantasy-sports`.

### Added

- **Captured ESPN add/drop/waiver shapes** — `docs/research/06-espn-add-drop-surface.md`
  and 13 redacted captures from a throwaway league's post-draft waiver window.
  `move_players` gains `waiver=True`, a claim with no `bidAmount`, because a
  league without an acquisition budget rejects every bid. A drops-only move is
  now a `ROSTER` transaction, since `FREEAGENT` must carry exactly one `ADD`.
  More than one add, or a claim with nothing to add, is refused before sending.
  `TRAN_ROSTER_LIMIT_EXCEEDED_ONE` maps to `roster_full` and
  `TRAN_PLAYER_NOT_ON_TEAM` to `already_dropped`. The executing `FREEAGENT`
  probes wait on the waiver pool and stay open on #108.
  `scripts/probe_espn_add_drop.py` runs them by name against `--league test`
  only (#108).
- **`lineup set` and `journal reverse`, the first writes** — `lineup set
  --team T --set PLAYER=SLOT ...` moves the session's own team to an explicit
  target lineup in one ESPN transaction. It reads fresh, preflights
  eligibility, locks, final slot counts, and duplicate or ambiguous players
  as named `CONFIG_INVALID` errors, refuses any team the session does not own
  before journaling, journals the prior slots before the POST, and reads the
  roster back slot for slot (`WRITE_DIVERGED` on a mismatch). `--dry-run`
  reports the moves and sends nothing. `journal reverse --id` sends a lineup
  entry's inverse through the same flow, journaled as kind `reverse`, and
  refuses add/drop/claim/cancel entries as `NOT_AVAILABLE` without sending.
  `WRITE_REJECTED` and `WRITE_DIVERGED` now have real command samples (#15,
  #16).
- **Irreversible writes are surfaced without a manual check** (#113, R7a).
  `journal digest --since <24h|7d|30m|ISO-8601> [--irreversible] [--league]
  [--limit]` lists journal entries recorded in a window, newest first, in the
  standard envelope, for a cron job or agent to render. A `[notify]` table in
  `config.toml` (`irreversible_command`) names a command that
  `reports.notify.notify_irreversible` runs once per irreversible write with
  the redacted journal entry as JSON on stdin; it never raises and never
  blocks the write. `doctor` gains a `notify` check that reports whether it is
  set without running it. `scripts/notify-irreversible-example.sh` is a sample
  hook. The package knows nothing of any vault or tracker; delivery is the
  owner's script.
- **The ESPN write transport and ownership guard** — a second Protocol,
  `WritingProvider` (`session_role`, `set_lineup`, `move_players`,
  `cancel_transaction`), with `SessionRole`, `RosterMove`, and `WriteResult`
  models. The ESPN adapter resolves the session's role from `mNav` and
  refuses, before building a request, any team the session does not own
  (`WRITE_REJECTED` `not_owned`, refuse-closed when ownership is unknown, no
  override). One POST to `lm-api-writes` with `espn_s2` only and no
  `memberId`; a 15 s timeout, never retried, reported as
  `transaction_state: "unknown"`; every status mapped to the taxonomy; a
  success purges the league and scoring-period cache tags. Every result
  carries `credentials.espn_s2_age_seconds`. No command writes yet; the
  add/drop/waiver/cancel shapes are community-attested until #108 (#107).
- **Mutation journal store** — `core/journal.py` (standard library only): a
  SQLite journal at `$XDG_STATE_HOME/fantasy-sports/journal.sqlite` (file
  `0600`, directory `0700`) with a two-phase `begin` / `complete` | `reject` |
  `diverge` API, so a crash between the intent and the outcome leaves a visible
  `pending` row. Every string and key is redacted on the way in, and SWID GUIDs
  (including the acting `member_id`) are stored as per-journal pseudonyms.
  Adds `journal list [--league] [--limit] [--irreversible]` and
  `journal show --id`, and a `journal` check in `doctor` that reports the path,
  entry count, pending rows, and permissions without ever raising. `journal
  reverse` waits for the write transport (#16, #107).
- **`WRITE_REJECTED` and `WRITE_DIVERGED` error codes** — exits 11 and 12,
  both non-retryable. `WRITE_REJECTED` is one code with a closed
  `details.kind` (`roster_lock`, `slot_limit`, `same_slot`,
  `not_current_period`, `budget_exceeded`, `position_limit`,
  `already_dropped`, `roster_full`, `not_owned`, `auth`, `unknown`) and
  ESPN's own type string in `details.provider_type`; a write-host 401 with a
  credential sent is `kind: "auth"`, never `AUTH_EXPIRED`. `WRITE_DIVERGED`
  reports a read-back that does not match an accepted write. No command
  raises either yet (#106, ADR-0010).
- **Command reference with generated samples** — `docs/commands.md` shows
  every command, output mode, global option, and error code with real
  output, generated by `scripts/render_readme_samples.py` from ESPN's public
  test league and a synthetic offline league (never a private one), spliced
  between markers, and held in sync by a unit test. The README links every
  command and gains a "Using it from an agent" section with a worked retry
  loop (#93).
- **ESPN write-surface research brief** — `docs/research/05-espn-write-surface.md`
  with scrubbed captures: the write host and lineup-transaction shape confirmed
  end to end against a real league, the `409 TRAN_*` rejection vocabulary,
  `espn_s2` as the sole write credential, writes pinned to the current scoring
  period, and the finding that a commissioner's cookie moves another team's
  lineup without being asked to act as LM — so team ownership is a client-side
  guard. Research only; nothing writes yet (#14).
- **`auth logout`** — removes `espn_s2` and `SWID` from the Keychain and from
  `config.toml`'s `[credentials]` table (every other key survives), reports
  each link as removed / absent / still-set / unavailable without ever
  printing a value, and names any environment variable it cannot unset. This
  is the leak remediation `SECURITY.md` could not describe before (#62).
- **Command surface** — `doctor`, `auth status` / `auth login`, `standings`,
  `roster`, `matchups`, `box-scores`, `free-agents`, `transactions`, and a
  `raw` escape hatch for any ESPN view not yet normalized. Every command is a
  plain typed function in a registry; the `typer` CLI is a thin projection
  over it (ADR-0003), which is what keeps a future MCP adapter a small wrapper
  instead of a rewrite.
- **ESPN provider adapter** with honest error classification — distinguishes
  an expired credential, a private league you're not a member of, and a
  genuine upstream outage, instead of collapsing all three into one 401.
- **Versioned output contract** (`"schema": "fantasy-sports/v1"`) with three
  renderers (JSON, table, CSV) and a stable machine-readable error taxonomy on
  stderr, so a cron job or an agent can branch on failure without parsing
  prose (ADR-0004).
- **Credential resolution chain** — environment variable, then macOS
  Keychain via `keyring`, then config file — with staleness reporting so
  `doctor` can tell you your session cookie is about to expire before ESPN
  does.
- **HTTP-layer cache** with two classes of cache tag, and a scrub-before-write
  hook plus a structural credential scanner so no recorded test cassette can
  ever carry a live `SWID` or `espn_s2` value.
- **Health system** — a scheduled canary against real ESPN that files an
  `auto-error` issue the day the unofficial API drifts, a health manifest, and
  a client-side check so `doctor` can tell you whether a failure is your
  credentials, a known outage, or something new.
- **The canary auto-files and dedupes an `auto-error` issue on confirmed
  `SCHEMA_DRIFT`, and publishes/updates the public `health.json`** the
  client-side health check reads — a separately-permissioned second CI job
  (`issues: write`/`contents: write`) that runs only on that classification,
  never on `BUILD_ERROR`/`CANARY_INFRA` (#64).
- **Performance and quality budgets, enforced in CI** (ADR-0008): sub-50ms
  cold start, five-dependency ceiling, sub-256KB wheel, 90%/85% line/branch
  coverage, 80% mutation score on `core/` and `providers/` — see
  `docs/adr/0008-performance-and-quality-budgets.md`.
- **Trusted publishing to PyPI** via GitHub Actions OIDC — no long-lived API
  token stored anywhere in this repo (#13).
- **`pro_team` on every lineup entry** of `box-scores`, so a consumer can
  place a player in a game without deriving his club from the opponent (#72).

### Fixed

- **`auth logout` exits nonzero when it could not reach a link.** A locked
  Keychain or an unwritable `config.toml` used to warn inside a success
  envelope; now it is `CONFIG_INVALID` (`kind: credential_store`) with the
  per-link report in `details.report`, because a leaked value may still be on
  the machine. Raised by u/kantorcodes1 on the launch thread (#89).

- **`box-scores` lost every `pro_opponent`** for a league whose
  `mPositionalRatings` view ESPN served without its `positionAgainstOpponent`
  key — two of three leagues on opening night 2026. `espn-api` gates the
  opponent on the ratings; the adapter now reads it from
  `proTeamSchedules_wl` directly, and the library's `"None"` sentinel no
  longer reaches the output as a club (#72).
- **`roster` and `free-agents` returned `opponent: null`** for every player
  while `box-scores` resolved it. The shared player path never read the
  schedule at all; it now takes its opponent from the same
  `proTeamSchedules_wl` map the box-score path uses, a bye is `null`, and a
  player with no club (`"None"`) has neither a team nor an opponent (#86).
- **`leagues.save()` dropped every table it did not own** — on a host using
  the plaintext `[credentials]` fallback, any rewrite of `config.toml` would
  have silently discarded the stored cookies. It now reads the document
  back, replaces only `default` and `[leagues]`, and writes atomically
  through the same mode-preserving helper `auth logout` uses; a file it
  cannot parse is refused rather than overwritten (#85).

### Out of scope for v0.1

Writes (lineup changes, trades, waiver claims) are gated behind a later
milestone per ADR-0006. Sleeper and Yahoo providers are designed for but not
implemented.

[Unreleased]: https://github.com/jwulff/fantasy-sports/compare/main...HEAD
