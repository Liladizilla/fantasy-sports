# fantasy-sports — Architecture Design

**Status:** Draft for review (pre-repo)
**Date:** 2026-08-26
**Author:** John Wulff (with Claude)

An agent-native CLI for fantasy sports leagues. ESPN first, provider-agnostic by
construction. Read-only in v0.1.

---

## 0. Why this exists

Verified 2026-08-26: there is no maintained CLI for ESPN fantasy leagues.

| Project | Stars | Last push | State |
|---|---|---|---|
| `cwendt94/espn-api` | 948 | 2026-08-18 | Healthy — a **library**, not a CLI |
| `derekrbreese/fantasy-football-mcp-public` | 61 | 2026-08-26 | Healthy — **Yahoo only**, MCP not CLI |
| `KBThree13/mcp_espn_ff` | 41 | 2025-12-19 | **8 months stale** — most-starred ESPN MCP |
| `gtonic/nfl_mcp` | 15 | 2026-08-24 | NFL news, not league management |
| `jdguggs10/flaim` | 13 | 2026-08-26 | Multi-platform, hosted, **read-only** |
| `Avanderheyde/espn-fantasy-cli` | 0 | 2026-07-30 | **One commit**, abandoned, unpublished |

PyPI namespace is entirely open. Every prior attempt died the same death: ESPN
changed something, nobody noticed, the repo went quiet. **Decision 11 (the canary)
is the direct response to that failure mode.**

---

## 1. Naming

| Surface | Value |
|---|---|
| GitHub | `jwulff/fantasy-sports` |
| PyPI | `fantasy-sports` |
| Command (canonical) | `fantasy-sports` |
| Command (alias) | `fantasy` |

Both entry points declared in `pyproject.toml`; both verified free on PATH.
Descriptive over clever — the name should tell you what it is without insider
vocabulary.

---

## 2. Language: Python

**Decision: Python 3.12+.**

The entire fantasy-sports API ecosystem exists in exactly one language:

| Provider | Library | Version | Last release |
|---|---|---|---|
| ESPN | `espn-api` | 0.46.0 | 2026-03-23 |
| Yahoo | `yfpy` | 17.0.0 | 2025-09-14 |
| Sleeper | `sleeper-api-wrapper` | 1.2.1 | 2025-11-02 |

Go's single static binary is genuinely better for cron, and it loses anyway:
choosing Go means reimplementing `espn-api`, which is not a thin HTTP client but
948 stars of accumulated knowledge about an undocumented API — view parameters,
scoring-setting shapes, position eligibility maps, the 2024 base-URL migration.
That rewrite *is* the project, and you would then do it twice more.

Distribution gap is closed by `uv tool install fantasy-sports`.

**Floor is 3.12** (`tomllib` in stdlib since 3.11; 3.12 for typing ergonomics).

### Toolchain

| Concern | Choice | Rationale |
|---|---|---|
| Packaging | `uv` + `hatchling` | Already installed (0.11.3); fast, lockfile-native |
| CLI framework | `typer` | Type annotations do double duty as MCP schemas later |
| Rendering | `rich` | Tables for TTY, and typer already depends on it |
| Config read | TOML via `tomllib` | Stdlib, no dependency |
| Config **write** | `tomli-w` | `tomllib` is read-only; `auth login` and config edits need a writer |
| HTTP | `requests` | Matches `espn-api`'s own transport. Mixing two stacks under VCR is a known source of pain, and a synchronous CLI has no async need |
| Dev deps | PEP 735 `[dependency-groups]` | Current standard; `[project.optional-dependencies]` is the legacy shape for this purpose |
| Tests | `pytest` + `vcrpy` | `espn-api` is built on `requests`, which is vcrpy's best-supported target |
| Type check | `pyright` | Recommended for CI today; `ty` is worth watching but not yet the default |
| Secrets | `keyring` | Standard, with a documented locked-Keychain-under-cron footgun that env-first resolution already mitigates |

Validated against 2026 practice in `docs/research/04-python-cli-packaging.md`,
which confirmed every prior choice and resolved the open ones above.

**Note:** there is no `uv build --standalone`. `uv build` produces an sdist and
a wheel only. Standalone binaries would require PyInstaller or Nuitka, assessed
and deferred — see the research brief.

---

## 3. The core bet: normalize shape, not semantics

This is the decision everything hangs off, and where "multi-provider" tools
usually die.

The three providers diverge harder than they appear. **Player identity is
per-provider** — there is no universal NFL player ID shared across ESPN, Yahoo,
and Sleeper. Scoring settings have structurally incompatible shapes. Roster-slot
eligibility rules differ. Transaction vocabularies differ. Playoff formats differ.

Building one true unified `League` model means a year of impedance mismatch and
no shipped product.

**Normalize for legibility, per provider. Passthrough raw alongside it, always.**

*(Revised: normalization is no longer defined as the cross-provider intersection.
ESPN's normalized output may carry fields no other provider exposes. Which of the
portability constraints in §14 findings 11-15 remain binding on the shared model
is an open question — see the requirements doc.)*

- **Normalized:** teams, rosters, standings, matchups, transactions, free agents.
  Same *shape* across every provider.
- **Explicitly NOT normalized:** scoring settings, draft logic, playoff formats,
  cross-provider player identity. Reachable via `fantasy-sports raw --view mSettings`.
- Every normalized object carries `provider`, `provider_id`, and a `raw` dict so
  nothing is ever lost.

Cross-provider player-ID mapping is deferred to a future ADR. `nflverse` publishes
crosswalks if it ever becomes necessary.

---

## 4. Layering

```
cli/         typer adapters — argument parsing only, zero logic
mcp/         fastmcp adapter — v0.3+, same registry
commands/    typed functions: THE command registry
core/        domain models (League, Team, Player, Matchup, Transaction)
cache/       SQLite, TTL per resource type
providers/
  base.py    Protocol: fetch_league, fetch_rosters, fetch_matchups, ...
  espn.py    wraps espn-api          [v0.1]
  sleeper.py wraps sleeper-api-wrapper [deferred]
  yahoo.py   wraps yfpy               [deferred]
auth/        credential specs, resolution chain, staleness detection
output/      json | table | csv renderers
reports/     scheduled artifacts — MAY call an LLM; nothing below it may
```

### Two structural rules

**Rule 1 — Commands are typed functions in a registry; CLI and MCP are both thin
projections over it.** No logic in typer callbacks. Honor this and
`fantasy-sports mcp serve` is ~100 lines of `fastmcp` later instead of a rewrite.
This buys the "ask from my phone" option without paying for it now.

**Rule 2 — `core/` and `providers/` have zero LLM dependency.** Only `reports/`
may call a model. Keeps the primitive testable and keeps token spend in one
auditable place.

---

## 5. Output contract (this is the product)

The agent-native claim lives or dies here.

- **JSON** when stdout is not a TTY; **rich table** when it is; `--output csv` flattens.
  `--output` overrides the detection in both directions.
- Every payload wrapped and **versioned from day one**:

```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "123456",
  "season": 2026,
  "generated_at": "2026-08-26T18:04:11Z",
  "data_as_of": "2026-08-26T17:56:11Z",
  "data_age_seconds": 480,
  "sources": [
    {"name": "mTeam", "fetched_at": "2026-08-26T17:56:11Z", "age_seconds": 480, "cached": true}
  ],
  "untrusted": {},
  "raw_omitted": false,
  "data": { },
  "error": null
}
```

The moment a cron job or an agent parses this, it is an API. Retrofitting a schema
version later is miserable; adding it now is free.

Success and failure carry the **same key set**; `data` and `error` are the
discriminator and exactly one is non-null. `data_as_of` / `data_age_seconds`
report the oldest contributing upstream fetch and `sources` itemizes each one
(R4). `untrusted` labels ESPN-sourced free text a league member controls —
team and league names today — distinctly from normalized structured fields
(R1a, ADR-0004's 2026-09-08 amendment, jwulff/fantasy-sports#17).

**`--no-raw` strips `raw` from every normalized object in `data`, recursively,
and sets `raw_omitted: true`** (jwulff/fantasy-sports#52). It is a global
option, accepted on either side of the command name like `--output`, and it
never reaches a command handler — the dispatch layer applies it to the
envelope a handler already returned, after the handler ran. `raw_omitted:
false` means "not suppressed here," not "`raw` is present": a stored payload
needs that distinction to tell a genuinely raw-less response from one that had
`raw` stripped, which matters for an archival consumer that commits envelopes
byte for byte (`jwulff/league-gazette`'s `snapshot` command) and wants to know
which it is looking at later. `fantasy-sports raw --view` ignores the flag —
its whole point is an unmodified provider payload — and always reports
`raw_omitted: false`.

**Timestamps are UTC or they are refused.** `espn-api` builds datetimes with
`datetime.fromtimestamp()` and no `tz=`, so they are naive and host-local — the
same league would render a different kickoff on a laptop and on a CI runner,
with nothing reporting an error. The envelope raises on a naive datetime rather
than guessing; adapters re-derive from raw epoch milliseconds.

### Error taxonomy — the part agents actually need

Errors go to **stderr as JSON** with a nonzero exit and a stable machine code. An
agent must be able to distinguish "your cookies died" (ask the human) from "ESPN is
down" (retry later) without parsing English:

| Code | Meaning | Correct agent response |
|---|---|---|
| `AUTH_MISSING` | No credentials configured | Ask human to run `auth login` |
| `AUTH_EXPIRED` | ESPN cookies rejected | Ask human to re-extract cookies |
| `LEAGUE_NOT_FOUND` | Bad league ID or no access | Ask human |
| `CONFIG_INVALID` | `config.toml` will not parse, or a command argument only the provider can validate came back invalid | Ask human to fix the input named in the message |
| `NOT_AVAILABLE` | ESPN positively refuses this request; never sent | Don't retry; check `remediation` |
| `PROVIDER_UNAVAILABLE` | ESPN 5xx / timeout | Retry with backoff |
| `RATE_LIMITED` | Throttled | Retry after `retry_after` |
| `SCHEMA_DRIFT` | Response shape unrecognized | Stop; file an issue |
| `WRITE_REJECTED` | A write was refused by ESPN, or by our own pre-flight; nothing applied | Do not retry unchanged; read the roster and re-plan; branch on `details.kind` |
| `WRITE_DIVERGED` | ESPN accepted a write but the read-back differs from what was asked | Do not retry; the write may have applied; read the roster and reconcile |

Every error payload carries `code`, `message`, `retryable`, `agent_action`,
`remediation` and `details`, all of them always present and `null` when empty.
`agent_action` is the class-level instruction; **`remediation` is the instance's
concrete next step** — the environment variable or the file that would fix *this*
failure. It is a first-class key rather than an entry in `details` because it is
the part a caller acts on, and a key in a general-purpose bag is a key some
consumers never read (ADR-0004 as amended, decided on #6).

Each code carries its own **exit status** so a cron job can branch without
parsing anything: `AUTH_MISSING` 3, `AUTH_EXPIRED` 4, `LEAGUE_NOT_FOUND` 5,
`CONFIG_INVALID` 6, `PROVIDER_UNAVAILABLE` 7, `RATE_LIMITED` 8, `SCHEMA_DRIFT` 9,
`NOT_AVAILABLE` 10, `WRITE_REJECTED` 11, `WRITE_DIVERGED` 12; `0` success, `1` an unclassified crash, `2` a usage error.

**`CONFIG_INVALID` covers two causes, not one (ADR-0004 amended by ADR-0009).**
A `config.toml` that will not parse and a CLI argument ESPN itself rejects — a
`--pos` it does not recognise, a `--filter` that is not JSON, a `raw` with no
`--view` — get the identical code, exit status, and `retryable: false`, because
they tell an agent the identical thing: stop, ask a human, retrying unchanged
cannot work. There is no eighth code for the second cause; `details.kind`
(`"config"` or `"argument"`) is the discriminator for a consumer that wants to
tell them apart without a second exit status to branch on.

**`WRITE_REJECTED` is one code with a closed `details.kind`, not one code per
reason (ADR-0010).** `kind` is one of `roster_lock`, `slot_limit`, `same_slot`,
`not_current_period`, `budget_exceeded`, `position_limit`, `already_dropped`,
`roster_full`, `not_owned`, `auth`, `unknown`. The payload also carries
`provider_type` (ESPN's `TRAN_*` string verbatim, so a reason we have not
mapped is reported as `unknown` rather than lost), `http_status`, `team_id`,
and `transaction_applied`. `not_owned` is ours, not ESPN's: the client-side
guard, because ESPN does not reject a commissioner's cross-team write.
`WRITE_DIVERGED` is the opposite failure: ESPN said yes and the roster read
back differently, so the transaction may have applied and a retry could
double-apply. It carries `details.expected` and `details.observed`, field
names and ids only.

**A write-host 401 never becomes `AUTH_EXPIRED`.** The write host's 401 body is
one constant for no cookie, an invalid cookie, and a missing SWID, and every
write is preceded by a read of the same league on the same cookie, so that
read having just succeeded is evidence the session is valid. With nothing sent
the code is `AUTH_MISSING`. With a credential sent it is `WRITE_REJECTED`
with `kind: "auth"` (research brief `docs/research/05-espn-write-surface.md`
§5.1 item 1).

**stdout stays byte-empty on failure**, and a failure is always JSON whatever
`--output` asked for. A consumer piping stdout into a parser must never receive
half a payload followed by an error, and a table-formatted error is prose again.

**An unclassifiable failure maps to `PROVIDER_UNAVAILABLE`, never to
`RATE_LIMITED`.** Availability tells an agent to retry with bounded backoff,
which is safe when we are wrong; a false throttle tells it to back off and wait,
which is not.

**`NOT_AVAILABLE` is the opposite case: a *positively classifiable* refusal, not
an unclassifiable one.** `espn-api` refuses some reads outright, on the year,
before any request is made — box scores and free agents before 2019, for
instance. That is not "we don't know what happened"; it is "we know exactly
what happened, and it will never succeed as asked", so it must not inherit
`PROVIDER_UNAVAILABLE`'s bounded-retry instruction (ADR-0004 amendment,
jwulff/fantasy-sports#45).

`SCHEMA_DRIFT` is what makes the health system (§11) actionable — it is the
trigger for both the canary's issue-filing and the client-side health check.

---

## 6. Auth

Providers diverge most here, and ESPN's failure mode is **silent**.

| Provider | Mechanism | Refresh | Expiry |
|---|---|---|---|
| ESPN | `espn_s2` + `SWID` cookies, manual DevTools extraction | None | Weeks–months, silently |
| Yahoo | OAuth2 | Refresh token | Standard |
| Sleeper | None for reads (public API by username) | — | — |

Interface: `Provider.credential_specs() -> list[CredentialSpec]`.

**Resolution chain: env → macOS Keychain → config file.** Env first is
non-negotiable — cron and CI cannot reach the Keychain.

**Differentiator:** `fantasy-sports auth status` reports **cookie age and likely
staleness**, not just present/absent. Silent ESPN cookie expiry is the #1
operational failure in this space and no existing tool surfaces it.

Secrets are never logged, never printed, and redacted in all error paths
including tracebacks.

**Writes send `espn_s2` only, never the SWID.** `espn_s2` is the session;
the write host accepts a transaction with it alone, and the SWID cookie and a
body `memberId` are both optional (`docs/research/05-espn-write-surface.md`
§2.2, §6.3). So the write path puts exactly one secret on the wire, sends no
`memberId`, and the SWID is used only locally — to find the session's own
member in `mNav`, compared against the cache's pseudonym of it, so it never
reaches a request body or a result either (#107).

---

## 7. Multi-league configuration

A first-class requirement — John runs several leagues; a tool that assumes one is
useless by week two.

`~/.config/fantasy-sports/config.toml`:

```toml
default = "dynasty"

[leagues.dynasty]
provider  = "espn"
league_id = "123456"
season    = 2026
sport     = "football"

[leagues.redraft]
provider  = "espn"
league_id = "789012"
season    = 2026
sport     = "football"
```

`fantasy-sports --league dynasty standings`. Season is per-league and overridable
with `--season` for historical queries.

**macOS path caveat.** `platformdirs` returns
`~/Library/Application Support/fantasy-sports` on macOS, which conflicts with the
XDG-style paths specified here and in §8. We **force XDG-style paths
unconditionally** on every platform, honouring `XDG_CONFIG_HOME`/`XDG_CACHE_HOME`
when set and falling back to `~/.config` and `~/.cache` when not. This is what
technical-audience CLIs do — ripgrep, gh, docker, and uv all behave this way on
macOS — and it is what this document already committed to. Do not rely on
`platformdirs`' macOS branch.

---

## 8. Caching

Decided now because it is a layer, and layers are hard to retrofit.

ESPN is multi-second and rate-limits. An agent exploring one question may make
eight calls. Cache sits **between provider and core** as a decorator.

SQLite at `~/.cache/fantasy-sports/`, TTL by resource type:

| Resource | TTL |
|---|---|
| Rosters, free agents | 5 min (in season) |
| Standings, matchups (current week) | 15 min |
| League settings | 1 day |
| Completed weeks, historical seasons | Forever |

`--no-cache` and `fantasy-sports cache clear` escape hatches.

---

## 9. Read/write posture

**Superseded by `docs/brainstorms/2026-08-26-agent-managed-fantasy-leagues-requirements.md`.**
Writes are core scope, not a deferred phase — an interface that can only observe
does not manage anything. `--dry-run` is an opt-in flag rather than the default,
because a synchronous confirmation prompt cannot be satisfied by an unattended
process and so converts unattended operation into failure rather than safety.

What replaces the removed gates: explicit-state operations, a mutation journal
recording prior state, and reversal where the provider permits it.

**Open, and load-bearing:** reversal is NOT available for every write. Drops,
processed waiver claims, and trades cannot be undone, and even a lineup change
locks at kickoff. ADR-0006's fourth gate — routing autonomous writes through
`/sanity-gate` — was never superseded and still stands for that irreversible
class. See ADR-0006 and the requirements doc; both need amendment.

**The write transport (#107).** Every write goes through one method on the
adapter that refuses before it builds, sends once, and maps everything that
comes back:

- **Ownership is refused client-side, before a body exists.** `session_role`
  reads `mNav` (`isLeagueCreator` *and* `isLeagueManager`) and the session's
  `teams[].owners`; a `team_id` outside that set is `WRITE_REJECTED`
  `kind: "not_owned"`, regardless of role, and ownership that cannot be
  determined is refused the same way. There is no override parameter. A
  commissioner's cookie is honoured by ESPN on another manager's team
  (brief §6), so this guard is the only thing that keeps `--team` meaning
  "which of *my* teams".
- **The writing session must be the owning one.** Ownership comes from the
  SWID; ESPN authenticates the write as `espn_s2`. The `memberId` a write
  echoes is checked against the guard's member: a mismatch is
  `WRITE_DIVERGED` for that write and refuses every later one. A hand-split
  pair is therefore caught after its first write, not before; `auth login`
  stores both together and `doctor` warns when they come from different
  links.
- **A timeout is not a failure.** The POST has a 15 s timeout (over a
  hundred times the observed latency) and is sent exactly once. A timeout or
  connection error after sending is `PROVIDER_UNAVAILABLE` with
  `details.transaction_state: "unknown"` and a remediation to read
  `transactions` (and `claims`) before retrying: a blind retry of a lineup is
  harmless, a blind retry of a waiver claim is a double spend (brief §8 item
  1). There is no retry loop on the write path.
- **The cache is coherent after a success.** A `200` purges every entry
  tagged with this league and this scoring period through
  `CacheStore.purge_by_league_tag` — before the response is parsed, since the
  write applied whether or not its body is readable — and drops the memoised
  league so the next read re-bootstraps. Season-scoped entries are untouched.
- **Every result carries credential age** (`credentials:
  {espn_s2_age_seconds, warning}`), computed before the request from the same
  staleness report `auth status` renders.

**`lineup set` and `journal reverse` (#15, #16).** The first commands on that
transport, and the template the irreversible ones (#18) reuse. The command
layer owns the flow; the adapter owns the wire:

1. Read fresh: teams, the roster, the league's slot counts.
2. Diff the explicit target (`--set PLAYER=SLOT`) against that roster and
   preflight it from the same read: eligibility, lock state, final slot
   counts, duplicate or ambiguous players, and an empty diff, each a named
   `CONFIG_INVALID` (`kind: "argument"`). The diff lives in the command, not
   the adapter, because `--dry-run` and the preflight need it; the adapter
   re-reads live and re-diffs before sending, so a world that moved in
   between surfaces there or in the read-back.
3. Check ownership against `session_role` again, so a refused team leaves no
   journal row and `--dry-run` never plans for a team the tool will not write.
4. `Journal.begin` with the prior slots, then one `set_lineup` call. An
   identity `WRITE_DIVERGED` closes the entry `diverged`. A failure the
   adapter marks `transaction_state: "unknown"` (a timeout, a 5xx, an
   unreadable 200), or one outside the taxonomy, leaves it `pending`, because
   nothing says whether it applied. Any other taxonomy error, such as ESPN's
   409 or the adapter's live re-read finding the roster moved, is a known
   no-send and closes it `rejected`. A journal that fails to close never
   replaces the write's own error; it rides along in `details.journal`.
5. Read back live and compare slot for slot. A match closes the entry
   `executed` with ESPN's transaction id, `proposedDate`, and the echoed
   member. A mismatch closes it `diverged` with what was observed, and raises
   `WRITE_DIVERGED` carrying both ids. A read-back that fails outright is an
   unverified success, not an error: ESPN did execute it.

`journal reverse` is the same flow with the entry's `prior_state` as the
target, journaled as kind `reverse` with `reverses` set, against the league
profile whose id the entry names. It refuses add, drop, claim, and cancel
entries as `NOT_AVAILABLE` before resolving a provider at all.

---

## 10. Reports — the layer that makes it worth owning

Everyone else stops at read-only data access. The interesting product is the
**scheduled artifact**:

```
fantasy-sports report start-sit --week 1 --output md
```

A Sunday-morning cron pulls the matchup, runs it through a model, and drops a
start/sit memo into the vault with the reasoning preserved. That has value at zero
stars and no external users.

Per the frugal-routing doctrine: `reports/` consults `model-route` rather than
defaulting to Claude. Data summarization and formatting are local-model work;
only genuine judgment escalates.

---

## 11. Health system — drift detection and self-diagnosis

**This is the highest-leverage part of the design.** It has a server side (the
canary) and a client side (the health check). They are one system.

ESPN's API is unofficial and breaks without notice. Every dead project in §0 died
identically: ESPN changed something -> maintainer did not notice for months ->
users hit raw stack traces -> repo looked abandoned -> people left.

### 11.1 Server side — the canary

A scheduled GitHub Action runs a live smoke suite against a real *public* ESPN
league on a weekly cadence (daily in season).

- Unit tests run against recorded `vcrpy` cassettes: fast, offline, deterministic.
- The canary is the **only** thing that touches live ESPN.
- On drift it **auto-files an issue** and **publishes `health.json`**.

That converts "silently broken" into "there is an open issue," which the existing
Foreman/shepherd fleet can pick up automatically.

### 11.2 The health manifest

The canary publishes a single static file to the repo, served free and
CDN-cached from `raw.githubusercontent.com`:

```json
{
  "schema": "fantasy-sports-health/v1",
  "latest_version": "0.1.4",
  "min_supported_version": "0.1.2",
  "yanked_versions": ["0.1.3"],
  "providers": {
    "espn": {
      "status": "degraded",
      "checked_at": "2026-09-03T06:00:00Z",
      "known_issues": [
        {
          "code": "SCHEMA_DRIFT",
          "endpoint": "mRoster",
          "affects": "<0.1.4",
          "fixed_in": "0.1.4",
          "issue": 42,
          "url": "https://github.com/jwulff/fantasy-sports/issues/42",
          "summary": "ESPN changed mRoster player-entry shape on 2026-09-03"
        }
      ]
    }
  },
  "updated_at": "2026-09-03T06:00:00Z"
}
```

No hosting to run, no Pages setup, no server. The canary commits it; the CLI
reads it.

### 11.3 Client side — cheap, lazy, fail-open

**Triggers.** Never on the happy path. The check fires only when it can change
what the user does next:

| Trigger | Check? |
|---|---|
| `SCHEMA_DRIFT`, `PROVIDER_UNAVAILABLE`, unexpected exception | Yes, inline |
| `AUTH_EXPIRED`, `RATE_LIMITED` | No (cause is known and local) |
| Successful command | No |
| `doctor` | Yes, forced, ignores cache |

**Rules, all of them non-negotiable:**

1. **Fail open, always.** If the check errors, times out, or the user is offline,
   say nothing extra and surface the original error unchanged. The health check
   must never be able to produce an error of its own.
2. **2-second timeout.** We are already on the error path; a short wait is
   acceptable, a hang is not.
3. **Cached 6 hours** in `~/.cache/fantasy-sports/health.json`. Repeated failures
   in a loop hit the network once, not a hundred times. Well inside GitHub's
   60 req/hr unauthenticated limit.
4. **No telemetry.** It is an unauthenticated GET of a public static file. Nothing
   about the user, their leagues, or their query is transmitted. **Say this
   explicitly in the README** — "checks for updates" reads as tracking to many
   people, and here it genuinely is not.
5. **Opt out** via `FANTASY_SPORTS_NO_HEALTH_CHECK=1` or `health_check = false`
   in config.
6. Version comparison uses `packaging.version` (PEP 440), never string compare.

**Human output** — appended to stderr, after the real error:

```
Error: ESPN returned an unrecognized response shape (SCHEMA_DRIFT).

  You are on 0.1.2 — 0.1.4 is available.
  This looks like a known issue, fixed in 0.1.4:
    #42  ESPN changed mRoster player-entry shape on 2026-09-03

  Fix:  uv tool upgrade fantasy-sports
```

And when already current:

```
  You are on the latest version (0.1.4).
  ESPN status: degraded — this is a known outage, tracked in #47.
  Nothing to do but wait. Details: https://github.com/jwulff/fantasy-sports/issues/47
```

**Machine output** — folded into the error envelope so agents get it too:

```json
{
  "schema": "fantasy-sports/v1",
  "error": {
    "code": "SCHEMA_DRIFT",
    "message": "ESPN returned an unrecognized response shape",
    "health": {
      "your_version": "0.1.2",
      "latest_version": "0.1.4",
      "upgrade_available": true,
      "upgrade_command": "uv tool upgrade fantasy-sports",
      "provider_status": "degraded",
      "known_issue": {
        "issue": 42,
        "url": "https://github.com/jwulff/fantasy-sports/issues/42",
        "fixed_in": "0.1.4",
        "summary": "ESPN changed mRoster player-entry shape on 2026-09-03"
      }
    }
  }
}
```

This is the agent-native payoff: Claude Code hits the error, reads
`upgrade_available: true` and `upgrade_command`, and can resolve it without
involving the human at all. When `upgrade_available` is false it knows to stop
retrying and report a known outage instead of burning turns.

### 11.4 `doctor`

One command that runs every check proactively and returns a single structured
verdict — the same shape agents already expect from tooling of this kind:

```
fantasy-sports doctor [--json]
```

Checks: installed vs latest version · ESPN provider status and known issues ·
credential presence **and cookie age/staleness** · configured leagues reachable ·
cache size and health · Python and dependency versions.

Gives both a human a single "what is wrong" answer and an agent a single call to
diagnose everything before it starts guessing.

### 11.5 The closed loop

```
ESPN changes something
  -> canary detects drift within 24h
  -> auto-files issue + updates health.json
  -> user's next command errors
  -> CLI reads health.json (cached, 2s, fail-open)
  -> human sees "known issue #42, fixed in 0.1.4, run this"
     agent sees upgrade_available:true and just fixes it
```

No human in the loop on the detection side. This is what none of the dead
projects in §0 had.

## 12. v0.1 scope

```
fantasy-sports doctor              # full health check, --json for agents
fantasy-sports auth status
fantasy-sports auth login          # guided cookie extraction
fantasy-sports auth logout         # clear Keychain + config.toml; env is reported, not unset
fantasy-sports league info
fantasy-sports teams
fantasy-sports standings
fantasy-sports roster    --team <id|name>
fantasy-sports matchups  --week N
fantasy-sports transactions --limit N
fantasy-sports free-agents --pos WR --limit N
fantasy-sports raw --view mSettings [--view ...]
```

ESPN only · read-only · JSON/table/CSV · multi-league config · SQLite cache ·
cassette tests · live canary in CI · health manifest + client-side check.

**Out of scope for v0.1:** writes, Yahoo, Sleeper, MCP server, cross-provider
player IDs, draft tooling, projections.

---

## 13. Roadmap

| Version | Contents |
|---|---|
| **v0.1** | ESPN reads, cache, config, drift detection, `doctor` — plus writes, pending the write-surface spike below |
| **v0.2** | `reports/` — start-sit memo, weekly recap, vault output |
| **v0.3** | `mcp serve` adapter over the same registry |
| **v0.4+** | Second provider, only when a real need appears |

**Blocking prerequisite:** `espn-api` is read-only — every live call is a `GET`
against `lm-api-reads.fantasy.espn.com`. The ESPN write surface (host, transaction
payload shape, headers, per-operation error vocabulary) is unresearched and must
be reverse-engineered before writes can be treated as schedulable work.

---

## 13.5 Performance and quality budgets

Measured, not asserted. Full rationale and the benchmark table in ADR-0008.

| Metric | Budget | Enforcement |
|---|---|---|
| `--version` / `--help` cold start | < 50 ms | `hyperfine` in CI vs committed baseline |
| Read command, cache hit | < 150 ms | `hyperfine` in CI |
| Direct runtime dependencies | ≤ 5 | dependency count check |
| Our wheel size | < 256 KB | build-artifact check |
| Line / branch coverage | ≥ 90% / ≥ 85% | hard CI fail |
| Mutation score, `core/` + `providers/` | ≥ 80% | scheduled + pre-release |

Three structural rules follow:

1. **Lazy imports are mandatory.** Naive module-scope imports of
   `typer + rich + requests + espn_api` measured 75 ms cold; `typer` alone
   measured 39 ms. A `--help` must not pay for an HTTP stack. Enforced by a test
   asserting the heavy modules are absent from `sys.modules`.
2. **`platformdirs` is dropped** — §7 forces XDG paths unconditionally anyway, so
   it buys an import and nothing else. ~30 lines of our own replaces it.
3. **Unit tests have no network at all.** `pytest-socket` blocks it, so a
   cassette miss fails loudly instead of silently calling ESPN and making the
   suite quietly dependent on ESPN being up.

## 14. Research findings that change the design

Four parallel research agents ran on 2026-08-26 before any code was written.
Their briefs are in `docs/research/`. These findings materially alter decisions
above and are binding.

### From the ESPN API brief (`03`)

1. **`AUTH_EXPIRED` cannot be a naive 401 mapping.** A bare 401 from ESPN is
   ambiguous between expired cookies and using the wrong current-vs-historical
   URL shape for the season. Classifying naively sends the user off to
   re-extract cookies that were never the problem. The adapter must double-probe
   before deciding.

   **Reinforced 2026-09-05 (U7, #4).** ESPN's 401 body is *not* bare — it
   carries a machine-readable `details[].type` — but on this path that type is a
   constant. Probed against a real private league one variable at a time, no
   cookies, a valid `SWID` with an invalid `espn_s2`, and either cookie alone
   all return `AUTH_LEAGUE_NOT_VISIBLE`. The body settles nothing, so **the
   double-probe is mandatory rather than a fallback**, and it stays
   library-owned: `espn-api`'s `checkRequestStatus` already swaps the URL shape
   and retries before raising. What we add is the classification above it, which
   never reports `AUTH_EXPIRED` from a reason that proves nothing about the
   credential. See `docs/memory/espn-401-tells-you-nothing.md`.

2. **`SCHEMA_DRIFT` is entirely ours to build — `espn-api` offers nothing.**
   Shape problems surface as raw `KeyError`/`TypeError` from inside object
   constructors doing unguarded dict access. This is a real wrapping layer with
   context capture, not an `except X: raise Y` shim. Budget for it.

3. **`RATE_LIMITED` needs code written from scratch.** `espn-api` does not
   special-case 429 and never reads `Retry-After`; it folds into a generic
   error. The `retry_after` field promised by §5 requires intercepting before
   the library's status check.

4. **Cache below the composite calls.** `box_scores()` and `free_agents()` each
   fan out into 2–3 sequential ESPN calls with no internal dedup. Caching at the
   command layer still pays 3 round-trips per miss. Cache at the HTTP layer,
   keyed on URL+params, so calls sharing a sub-fetch actually hit.

5. **The canary must distinguish ESPN drift from our own build breaking.**
   `espn-api`'s own live CI canary was red for 12 straight days (2026-08-07→18)
   from a transitive dependency incompatibility, not an ESPN change. Pin the
   canary's dependencies as tightly as the runtime, and classify by *where* the
   exception occurred — before or after a real HTTP response came back. A canary
   that cries wolf trains everyone to ignore it.

6. **A proven canary league already exists:** `league_id=1234, year=2018`, the
   league `espn-api`'s own integration test has hit daily and unattended for
   years. Publicly accessible and structurally stable. Reuse it rather than
   standing one up.

   **Amended 2026-09-05 (U7, #4): valid for league, team, standings, roster,
   draft, settings, matchup and transaction reads only — never for box scores
   or free agents.** Verified against real ESPN with `espn-api` 0.46.0:
   `League(1234, 2018).box_scores(1)` raises `Cant use box score before 2019`,
   `free_agents()` raises the same for its own view, and league 1234 does not
   exist for 2019, 2021, 2023 or 2025 (`ESPNInvalidLeague`). The library's
   refusal is on the *year*, so no request is ever made and no cassette can be
   recorded. Those two payloads are covered by a hand-authored fixture instead
   (`scripts/build_synthetic_cassette.py`), and the box score the League Gazette
   depends on (#29) has a proven MIT-licensed derivation recipe on #4. Two
   further consequences: the canary's matchup periods are 1:1 with its scoring
   periods, so the playoff-week split is not observable against it either, and
   its `members[]` carry ids with **no display names at all**, so team-to-person
   mapping must not be built on ESPN member names. See `docs/testing.md`.

7. **`mBoxscore` and `mPendingTransactions` are not real views.** Box scores are
   `mMatchupScore` + `mScoreboard` stitched client-side with two side-calls.
   Pending waivers come through `mTransactions2` with status filtering.

8. **Never pass `espn-api`'s `datetime` objects through.** They are built with
   `datetime.fromtimestamp()` and no `tz=` — naive and host-local. Our versioned
   envelope would silently emit wrong timestamps depending on where the CLI runs.
   Re-derive from the raw epoch-ms.

9. **`auth login` must validate and repair SWID's curly braces.** Confirmed as
   the single most-repeated manual-extraction mistake across every community
   source reviewed.

10. **There is no programmatic auth to design toward, ever.** ESPN closed the
    username/password path. v0.3 writes will still require a human-extracted
    cookie.

### From the provider data-shapes brief (`02`)

These are the ways an ESPN-only v0.1 produces an abstraction that breaks when a
second provider arrives.

11. **`Team` must carry `owner_names: list[str]`, never a single string.** ESPN's
    `owners` is a list and Yahoo's `managers` is explicitly plural with a
    co-manager flag. Both platforms support co-management natively; a `str`
    silently drops a manager.

12. **ESPN has *two* transaction surfaces** — `mTransactions2` and
    `Activity`/`kona_league_communication` — with non-overlapping vocabularies.
    Reconcile them inside the ESPN adapter so `core.Transaction` never inherits
    the asymmetry.

13. **Do not model roster-slot eligibility on the player.** ESPN's
    `eligibleSlots` makes `Player.eligible_slots` tempting; Sleeper has no
    equivalent and would have to synthesize it from hardcoded FLEX conventions
    its API never states. Model `slot` (current occupancy) only; eligibility
    lives in `raw` until a second provider proves what is shareable.

14. **`week` is not a portable integer.** Keep both `scoringPeriodId`- and
    `matchupPeriodId`-equivalents in `raw` for every provider, even where they
    are currently identical, so the shape does not change later. The split only
    bites during playoff weeks — exactly the path an in-season ESPN-only test
    never exercises.

15. **Standings are computed client-side for ESPN and Sleeper, server-side for
    Yahoo.** ESPN's API returns no sorted standings at all; `espn-api` implements
    a full tiebreaker cascade locally. Copying that into `core/` as "the"
    standings algorithm builds logic Yahoo does not need and will disagree with.
    Keep it in the ESPN adapter.

## 15. Open questions

1. **Public or private repo?** Public assumed — nothing sensitive, and the gap is real.
2. **Do reports live here or in a separate repo?** Assumed same repo, separate
   subcommand namespace, with the hard "no LLM below `reports/`" boundary.
3. **PEP 541 request for the bare `fantasy` PyPI name?** It is a fileless squat
   (v0.1, zero uploads, dead repo). Worth filing eventually; not worth blocking on.
