# ADR 0004: Versioned output contract and machine-readable error taxonomy

**Status:** Accepted
**Date:** 2026-08-26
**Amended:** 2026-09-05 — `CONFIG_INVALID` added to the taxonomy
(jwulff/fantasy-sports#35, decided on #6); the envelope's full key set, the
`remediation` key, and the exit-status table fixed by the output layer
(jwulff/fantasy-sports#6). **Amended:** 2026-09-08 by
[ADR-0009](0009-config-invalid-covers-a-bad-argument-too.md) —
`CONFIG_INVALID` also covers an argument only the provider can validate (no
eighth code); its `agent_action` no longer names a config file specifically,
and it gains a `details.kind` (`"config"` or `"argument"`) discriminator
(jwulff/fantasy-sports#48). **Amended:** 2026-09-08 — clarified what
`fetched_at` means on a cache hit, after it shipped decorative
(jwulff/fantasy-sports#51). **Amended:** 2026-09-09 — `raw_omitted` added to
the envelope and `--no-raw` added as a global option
(jwulff/fantasy-sports#52). **Amended:** 2026-10-14 by
[ADR-0010](0010-write-codes-one-code-with-a-kind-discriminator.md) —
`WRITE_REJECTED` (exit 11) and `WRITE_DIVERGED` (exit 12) added for the write
surface (jwulff/fantasy-sports#106).

(jwulff/fantasy-sports#6). 2026-09-08 — `NOT_AVAILABLE` added to the taxonomy
(jwulff/fantasy-sports#45).

## Context

The moment a cron job or an AI agent parses this tool's output, that output is an
API. Most CLIs discover this late and cannot change shape without breaking
consumers.

Separately: agents handle failure badly when errors are prose. An agent that
cannot distinguish "your credentials expired" from "ESPN is down" will retry a
permanent failure until it exhausts its budget.

## Decision

**Every successful payload is wrapped and versioned:**

```json
{
  "schema": "fantasy-sports/v1",
  "provider": "espn",
  "league_id": "123456",
  "season": 2026,
  "generated_at": "2026-08-26T18:04:11Z",
  "data": {}
}
```

Output format is JSON when stdout is not a TTY, a rich table when it is, and CSV
on request.

**Every error goes to stderr as JSON with a nonzero exit and a stable machine
code:**

| Code | Meaning | Correct agent response |
|---|---|---|
| `AUTH_MISSING` | No credentials configured | Ask the human |
| `AUTH_EXPIRED` | Credentials rejected | Ask the human to re-auth |
| `LEAGUE_NOT_FOUND` | Bad ID or no access | Ask the human |
| `CONFIG_INVALID` | `config.toml` will not parse | Ask the human to fix the file |
| `NOT_AVAILABLE` | Provider positively refuses this request; never sent | Don't retry; check `remediation` |
| `PROVIDER_UNAVAILABLE` | Upstream 5xx / timeout | Retry with backoff |
| `RATE_LIMITED` | Throttled | Retry after `retry_after` |
| `SCHEMA_DRIFT` | Response shape unrecognized | Stop; file an issue |

Adding a code is an API change and requires a version consideration.

### Amendment, 2026-09-05: `CONFIG_INVALID`

A malformed config file had no code, and the config layer shipped with
`ConfigError.code = None` as a deliberate seam. Uncoded is the wrong resting
place: the taxonomy exists so an agent knows what to do next, and an error
rendered generically invites the retry loop the taxonomy was built to prevent.

`LEAGUE_NOT_FOUND` is the near-miss and is actively wrong here. It tells an
agent to retry with a different `--league`, which cannot possibly succeed when
the file itself will not parse. `CONFIG_INVALID` is the one instruction no
existing code gives: *user-fixable, not retryable, and no amount of agent
retrying changes it.*

Adding a code is an API change, which is an argument for doing it now rather
than later — the package is `0.1.0.dev0`, nothing is published, and no consumer
parses the taxonomy yet. This is the cheapest moment the change will ever have.

### Amendment, 2026-09-05: the full envelope, `remediation`, and exit statuses

Three things the original decision left to the implementing unit, fixed here by
jwulff/fantasy-sports#6 because a consumer now exists that parses them.

**1. The envelope's complete key set.** The shape above was illustrative; this
is the contract. Success and failure carry the *identical* keys, and `data` and
`error` are the discriminator — exactly one is non-null. A consumer that has to
branch on which keys *exist* before it can branch on what happened is a consumer
that will get it wrong once.

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
  "data": [],
  "error": null
}
```

`data_as_of` / `data_age_seconds` report the **oldest** contributing upstream
fetch and `sources` itemizes each one, which is origin R4. `untrusted` is a
path-to-string map reserved **now, empty**, for attacker-influenceable text;
jwulff/fantasy-sports#17 populates it. Reserving it here rather than adding it
there is the difference between #17 being a provider change and #17 being a
schema-version bump on the one contract every consumer parses.

Timestamps are UTC with a literal `Z`, always. `espn-api` builds its datetimes
with `datetime.fromtimestamp()` and no `tz=`, so they are naive and host-local;
the envelope **refuses** a naive datetime rather than guessing, and adapters
re-derive from raw epoch milliseconds.

**2. `remediation` is a first-class error key, not an entry in `details`.**
#36 put it in `details` deliberately, on the grounds that promoting it is a
contract change and a refactor is the wrong place to make one quietly. It is
promoted here. Remediation is the part a caller *acts on*; leaving it in a
general-purpose bag makes reading it optional, which means some consumers will
not, and the whole point of the taxonomy is that a failure tells its caller what
to do next. `agent_action` is the class-level instruction ("ask the human to
re-auth"); `remediation` is this instance's concrete next step, naming the
environment variable or the file that would actually fix *this* failure.

Every error key is now always present, `null` when empty — `details` included.
A key that disappears when it is empty is a key some consumers will never look
for, which is the same failure mode one level down.

```json
{
  "code": "AUTH_EXPIRED",
  "message": "ESPN rejected the stored cookies.",
  "retryable": false,
  "agent_action": "Ask the human to re-extract their ESPN cookies.",
  "remediation": "Re-extract espn_s2 and SWID from DevTools, then run `auth login`.",
  "details": {"status": 401}
}
```

**3. Exit statuses, one per code.** "Cron jobs can branch on exit codes" is only
true if the codes are distinct, so nothing collapses onto `1`.

| Code | Exit |
|---|---|
| *success* | `0` |
| *unclassified crash* | `1` |
| *usage error* | `2` |
| `AUTH_MISSING` | `3` |
| `AUTH_EXPIRED` | `4` |
| `LEAGUE_NOT_FOUND` | `5` |
| `CONFIG_INVALID` | `6` |
| `PROVIDER_UNAVAILABLE` | `7` |
| `RATE_LIMITED` | `8` |
| `SCHEMA_DRIFT` | `9` |
| `NOT_AVAILABLE` | `10` |

All inside the portable range: `126`, `127` and `128+n` are claimed by the shell
for "not executable", "not found" and "killed by signal N". Changing a number is
an API change, exactly like renaming a code.

**Two rules the renderers enforce**, both about a program reading stdout:
**stdout stays byte-empty on failure** — a consumer piping it into a parser must
never receive half a payload followed by an error — and **a failure is always
JSON**, whatever `--output` asked for, because a table-formatted error is prose
again.

### Amendment, 2026-09-08: what `fetched_at` means on a cache hit

The original decision never said, and the freshness contract shipped inert
because of it: every `sources[].fetched_at` was stamped with the *current*
call's clock, cached or not, so `age_seconds` was always `0` and `cached: true`
was the only honest field in the envelope
(jwulff/fantasy-sports#51). A downstream consumer,
`jwulff/league-gazette`'s `gazette snapshot`, refuses to build an issue from
data older than an hour — a gate that cannot do its job against a field that
never moves.

**`fetched_at` is when the bytes left the provider, not when this process read
them.** For a live request that is the same instant either way, so the
ambiguity was invisible until a second read hit the cache. Made explicit here:

- A cache **miss** — live, `--fresh`, or `--no-cache` — reports `fetched_at` as
  now and `age_seconds` as `0`. The bytes and the read are the same moment by
  construction.
- A cache **hit** reports `fetched_at` as the moment the entry was *written*,
  carried on the cache store's own clock
  (`fantasy_sports.cache.store.CacheStore._now`, not the reader's). `age_seconds`
  is `now - fetched_at` and grows on every subsequent hit until the entry
  expires or is refreshed.
- `data_as_of` / `data_age_seconds` are unchanged by this amendment — they were
  already specified as the oldest contributing `fetched_at`
  (origin R4) — but they were exercising a value that never varied. They now
  do.

The fix lives at the one seam that decides it:
`fantasy_sports.cache.store.FetchResult.fetched_at` carries the entry's
`stored_at` on a hit and `None` on anything else, and
`fantasy_sports.providers.espn._Transport._body` is the only place that reads
it. Nothing about the envelope's own arithmetic changed —
`fantasy_sports.output.envelope.Envelope.to_dict` was already computing
`age_seconds` correctly from whatever `fetched_at` it was handed; the bug was
that every `fetched_at` reaching it said "now."

### Amendment, 2026-09-08: `untrusted` populated (R1a, jwulff/fantasy-sports#17)

The container reserved above is no longer always empty. Any league member can
set a team or league name, and that text reaches an agent that reads the
envelope to reason and can write back to ESPN — a crafted name is a prompt-
injection path, and `untrusted` is the documented seam for treating it as
data rather than instructions.

**What is labeled, today.** `League.name` and `Team.name`/`Team.owner_names`
are the only normalized fields any league member controls; nothing else
normalized carries free text yet (`docs/brainstorms/2026-08-26-agent-managed-
fantasy-leagues-requirements.md` R1a additionally names trade notes and
waiver/offer comments, which are not modeled as their own fields — they are
reachable only through `raw`, and `raw` is not labeled, per the existing raw-
passthrough exception in `CLAUDE.md` rule 3). Adding a new normalized field
that any member can set means adding its name to that model's `_UNTRUSTED`
classvar in `core/models.py` in the same change — not a follow-up.

**The seam.** `fantasy_sports.core.models.ProviderObject._UNTRUSTED` is a
per-class set of field names; `ProviderObject.untrusted()` reads it off one
instance, and `fantasy_sports.core.models.collect_untrusted(data)` is the
entry point a command calls with the model object(s) it is about to return —
before `.to_dict()`, so the type information `.to_dict()` throws away is
still there to consult. This is provider-agnostic: a Yahoo or Sleeper adapter
that returns the same `League`/`Team` shape inherits the labeling for free
the moment it populates those fields, and any future model gets it by
declaring its own `_UNTRUSTED`.

**Path syntax.** A bare field name for an object-shaped command (`"name"`
under `league info`); `"[i].field"` for item `i` of a collection command's
list (`"[1].name"` is the second team's `teams` response); a plural free-text
field indexes twice (`"[0].owner_names[0]"` is the first owner of the first
team). This mirrors how a consumer would already be walking the JSON `data`
array — no separate addressing scheme to learn.

**The value is labeled, not hidden.** `data` still carries `name` normally;
`untrusted` is a sidecar pointing at the same value, not a redaction. An
agent that never reads `untrusted` sees exactly the output it saw before this
amendment.

**Where the label stops mattering: rendering.** JSON, CSV and the table all
already carry arbitrary strings safely through a real format library, not a
convention — `json.dumps`, `csv.writer`, and (after this change)
`rich.text.Text` for table cells, which stopped `rich` from parsing a team
name as its own `[markup]` syntax (a name as ordinary as `Team [/bold]`
previously crashed the table renderer with `MarkupError`; see
`src/fantasy_sports/output/table.py::_cell`). The one surface with no such
library on this project's dependency budget is markdown — a GitHub issue
body, the client error reporter ADR-0007 describes, any future report.
`fantasy_sports.output.untrusted.render_untrusted_block` renders untrusted
text as a markdown *indented* code block, never fenced: an indented block's
boundary is the absence of indentation on a following line, not a token like
three backticks that the content itself could contain and close early.

### Amendment, 2026-09-09: `--no-raw` and `raw_omitted`

Every normalized object carries `raw` (this ADR's own decision, and `CLAUDE.md`
rule 3), and for a roster that means a complete ESPN player record per slot —
`seasonOutlook` prose, ranking arrays, and five `stats` splits with roughly
fifty keys each. One real 15-slot roster runs ~530 KB, and the normalized
fields the tool actually promises are well under 1% of it
(jwulff/fantasy-sports#52). That is fine for a caller reading one response and
ruinous for `jwulff/league-gazette`'s `snapshot` command, which commits the
envelope byte for byte as its archive: a season of weekly snapshots is roughly
110 MB of git history for a league whose actual weekly facts are a few hundred
rows.

`--no-raw` is a global option, on both sides of the command name like
`--output`, that strips the `raw` key from every normalized object in `data`,
recursively. It is deliberately **not** a handler parameter: it does not change
what a command computes, only what the envelope built from that computation
keeps, so the dispatch layer applies it once, to the envelope a handler already
returned, the same way `--output` picks a renderer without either being visible
to `commands/*.py`. That also means it needs no case in
`ARCHITECTURE.md`'s handler-parameter conventions, and `raw` stays fully
reachable to anything that calls a handler directly.

**`raw_omitted` is a new envelope key, added below `untrusted` and above
`data`, always present.** Additive rather than a schema bump, following this
ADR's own precedent for `untrusted`: `raw_omitted: false` on every envelope
that predates this amendment is exactly the value it would have reported
anyway. It answers one question — was suppression *applied* here — not
whether `raw` is present: a passthrough `raw --view` payload has no `raw` key
under any circumstance and still reports `raw_omitted: false`, because nothing
was suppressed. A stored payload needs that distinction to tell "this provider
response never had it" from "this was stripped before it was written," which
is exactly the ambiguity an archival consumer cannot resolve by inspection
alone.

**`raw --view` ignores the flag.** Its whole reason to exist is an unmodified
provider payload (this ADR's own decision); silently trimming part of it on a
flag that every other command interprets as "smaller, still the truth" would
make one command's `--no-raw` semantics differ from every other's without
saying so on the payload. It is a no-op rather than a usage error because a
global option landing on a command it does not apply to already has a
precedent that is not an error — `--league` on `auth status` — and because
`--no-raw` is frequently set once, globally, by a caller that also wants
`raw` output occasionally.

### Amendment, 2026-09-08: `NOT_AVAILABLE`

`espn-api` refuses some reads outright, on the *year*, before any HTTP request
is made — `League(1234, 2018).box_scores(1)` and `.free_agents()` both raise a
bare `Exception` naming the season, and ESPN will not start serving 2018 box
scores no matter how many times it is asked. The ESPN adapter mapped these
through its catch-all to `PROVIDER_UNAVAILABLE`, per this ADR's original
instruction to send unrecognised library exceptions there. That is wrong in one
specific, actionable way: `PROVIDER_UNAVAILABLE` carries `retryable: true` and
"retry with bounded exponential backoff" — an agent that believes it will back
off and retry forever against something that can never succeed
(jwulff/fantasy-sports#45).

This is the *opposite* of the R12 failure this ADR's error taxonomy guards
against. R12 says an unclassifiable failure must land on `PROVIDER_UNAVAILABLE`
rather than being guessed into `RATE_LIMITED`; this is a **positively
classifiable** condition — the adapter knows exactly why the request will never
succeed — landing there anyway and inheriting a retry instruction that is
false.

`NOT_AVAILABLE` is added: `retryable: false`, agent action "do not retry as
sent; check `remediation` for a supported alternative." The cost of an eighth
code is weighed and accepted here rather than deferred, for the same reason
`CONFIG_INVALID` was added above — the package has no published consumers yet,
so this is the cheapest moment the change will ever have, and a misleading
`retryable` flag is a worse contract than one more code every consumer parses.

The catch-all in `providers/espn.py` still lands genuinely unrecognised
exceptions on `PROVIDER_UNAVAILABLE`; this narrows what reaches it rather than
replacing it. `espn-api` keeps no structured attribute for these refusals — a
bare `Exception` with a sentence, nothing else — so the adapter keys off the
known message text, matched against the specific known refusals rather than a
loose substring.

## Consequences

**Easier:** Agents can act correctly on failure without parsing English. Cron
jobs can branch on exit codes. Schema evolution has a defined path.

**Harder:** Every command must produce the envelope. Every error path must map to
a code. Slightly more ceremony per command.

**Accepted:** `SCHEMA_DRIFT` requires us to actually validate response shapes
rather than letting `KeyError` propagate. That is real work, and it is what makes
ADR-0005's canary actionable.

## Alternatives considered

**Unversioned JSON** — rejected; retrofitting a version after consumers exist is
miserable and this costs nothing now.

**Prose errors only** — rejected; defeats the agent-native premise.
