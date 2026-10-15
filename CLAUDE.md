# CLAUDE.md — fantasy-sports

## Project Overview

An agent-native CLI for fantasy sports leagues. ESPN first, provider-agnostic by
construction. Reads and writes (lineups, adds, drops, waiver claims) in v0.1. Python 3.12+, distributed via
`uv tool install fantasy-sports`.

The full design and its rationale live in **`docs/ARCHITECTURE.md`** — read it
before making structural changes. Individual decisions are recorded in
`docs/adr/`.

---

## Fresh Clone Setup

```bash
uv sync                  # install deps into .venv
uv run fantasy-sports doctor
uv run pytest            # offline; runs against recorded cassettes
```

Live tests against real ESPN are opt-in and never run in normal CI:

```bash
uv run pytest -m live    # requires ESPN_S2 / ESPN_SWID in env
```

---

## The five rules that matter most

These are the load-bearing constraints from `docs/ARCHITECTURE.md`. Violating
one is a design regression, not a style nit.

1. **Commands are typed functions in a registry. CLI and MCP are thin
   projections over it.** Put zero logic in typer callbacks. This is what keeps
   the future MCP adapter a ~100-line projection instead of a rewrite.

2. **`core/` and `providers/` have zero LLM dependency.** Only `reports/` may
   call a model, and it routes through `model-route` rather than defaulting to
   Claude.

3. **Normalize shape, not semantics.** Teams, rosters, standings, matchups,
   transactions, and free agents normalize. Scoring settings, draft logic,
   playoff formats, and player identity explicitly do NOT — they go through the
   `raw` passthrough. Every normalized object carries `provider`, `provider_id`,
   and `raw`.

4. **The output contract is the product.** Every payload is wrapped and
   versioned (`"schema": "fantasy-sports/v1"`). Every error goes to stderr as
   JSON with a stable machine code from the taxonomy in ARCHITECTURE §5. Adding
   a new error code is an API change.

5. **Never log, print, or record a credential.** `espn_s2` and `SWID` are
   secrets. They must be redacted in tracebacks, error payloads, telemetry, and
   VCR cassettes. Cassette scrubbing is enforced in `conftest.py` — if you add a
   new recorded fixture, verify the scrub before committing.

---

## Budgets (ADR-0008) — these fail CI, they are not aspirations

| Metric | Budget |
|---|---|
| `--version` / `--help` cold start | < 50 ms |
| Read command, cache hit | < 150 ms |
| Direct runtime dependencies | ≤ 5 |
| Our wheel size | < 150 KB |
| Line / branch coverage | ≥ 90% / ≥ 85% |
| Mutation score on `core/` and `providers/` | ≥ 80% |

**Lazy imports are mandatory.** Never import `espn_api`, `requests`,
`rich.table`, or `keyring` at module scope — import inside the function that
needs them. `--help` must not pay for an HTTP stack it will never use. A test
asserts these are absent from `sys.modules` after importing the entry point.

**Unit tests have no network.** `pytest-socket` blocks it. A cassette miss must
fail loudly, not silently call ESPN.

Adding a sixth direct dependency requires justification in the PR body.

---

## Work Tracking

Follows the GitHub-Issues workflow in `~/.claude/CLAUDE.md`. Outstanding work is
in `gh issue list`; every PR carries `Closes #N` or `Refs #N`.

**Tracks for this project:**

| Label | Area |
|---|---|
| `track:cli` | Command surface, output rendering, UX |
| `track:espn` | The ESPN provider adapter |
| `track:core` | Domain models, cache, config, auth |
| `track:health` | Canary, health manifest, telemetry, `doctor` |
| `track:reports` | The scheduled-artifact layer |
| `track:infra` | Packaging, CI, release, distribution |

`auto-error` is reserved for issues filed automatically by the canary or client
telemetry. `user-feedback` for issues originating from users.

---

## Project Memories

Persistent context lives at `docs/memory/`. Check `docs/memory/MEMORY.md` on
session start; load individual files when relevant.

**Never** save memories to `~/.claude/projects/<path>/memory/`.

---

## Development Rules

### Worktrees

Per the global doctrine: worktrees are nested at
`main/.claude/worktrees/<name>`, branched from `origin/main`. `main/` is
admin-only — never edit application code there.

Before editing in a worktree, verify `git rev-parse --show-toplevel` contains
`.claude/worktrees/`.

### Branch protection

Never commit directly to `main`. Branch, PR, merge.

### Test-driven

Write the failing test first. For anything touching ESPN, that means recording
or hand-writing a cassette — never a live call in a unit test.

### Testing against an API that breaks

**`docs/testing.md` is the fixture policy** — which cassettes are real
recordings, which are hand-authored, what each one cannot cover, and how to
re-record them. Read it before adding a fixture.

This project's whole thesis is that ESPN will break unpredictably. Therefore:

- Unit tests run offline against cassettes. Always. No exceptions.
- The live canary is a separate, scheduled suite. It is the only thing that
  touches real ESPN.
- When ESPN breaks, the canary opens an issue labelled `auto-error`. Treat those
  as real bugs until proven otherwise — a red canary is a genuine upstream change
  far more often than it is a flake.

### Secrets in tests

If a test needs credentials, it is a live test and must be marked `@pytest.mark.live`.
Unit tests never touch real credentials.
