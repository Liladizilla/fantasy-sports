# Document the write surface as one design (#110)

## Why

Eight PRs built the write surface (#105) one unit at a time, and each updated
the docs it touched. The result read like a list of commits. ARCHITECTURE §9
still said ADR-0006 and the requirements doc "both need amendment", ADR-0006
still described a v0.3 that never happened, AE2 still said partial application
was reported rather than prevented, the README's "Writing" section had grown
three overlapping parts, and §12 and §13 still called writes out of scope.
This pass makes the docs describe the one design that shipped.

## How

- **ARCHITECTURE §9 is rewritten as the shipped posture**, organised by
  control rather than by PR: the transport, the journal, `lineup set`, the
  irreversible commands, the waiver queue, and what the tests prove. §6 states
  that writes send `espn_s2` only and that the member id reported anywhere is
  the cache's pseudonym. §12 and §13 list the commands and move writes out of
  "out of scope". §14 gains a subsection of the nine facts from briefs 05 and
  06 that changed the design.
- **Single POST means no partial application.** AE2's claim that partial
  application is "reported, not prevented" came from before the research.
  Brief 05 §0 row 3 shows a lineup write is one atomic transaction, so AE2 now
  says a divergence means the world moved. The original sentence is kept in a
  dated amendment note below the examples rather than erased.
- **ADR-0006 is Superseded**, with a paragraph on what replaced each gate. The
  honest limit of the fourth gate is stated there and in §9: a CLI cannot run
  `/sanity-gate`, so the acknowledgement flag makes a skipped gate visible by
  journaling the justification. ADR-0010 already listed the thirteen kinds
  after #122, so only the ADR index changed.
- **The README keeps its voice and loses its overlap.** One "Writing" section
  now has rules common to every write, then lineups, irreversible writes, the
  claim lifecycle, the journal, notification, and the two write error codes
  with a table of what to re-plan for each `kind`. The command table gained
  the five commands #122 and #123 added. "Using it from an agent" gained a
  rule 7 that points at the loop.
- **`docs/commands.md` needed no regeneration.** Every write command, every
  `journal` sub-command, and both error samples were already present and the
  check was clean. Only the `WRITE_REJECTED` prose, which named three of the
  thirteen kinds, was extended by hand outside the sample markers.
- **CHANGELOG** folds the seven separate entries into one "Writes" block
  grouped by commands, transport and taxonomy, and research. Every fact is
  kept. The "Out of scope" paragraph no longer says writes are deferred.
- **Stale items fixed in passing**: `tests/live/README.md` listed two write
  tests and there are three; the memory index line for the transaction-id note
  described the pre-#109 match; `CLAUDE.md` still said "read-only in v0.1".
- **No new memory note.** The lessons from this epic are already recorded
  (pseudonyms, the two `mNav` flags, a cancelled claim reading `PENDING`, the
  missing transaction id), and the typer shadowing rule sits in a code comment
  next to the registration it governs.

## Review fixes (Codex on PR #125)

- **`cancel-claim` does not raise on a read-back mismatch.** The README had
  said every mismatch is `WRITE_DIVERGED`. A cancel ESPN accepted whose claim
  is still queued returns a success with `verified: false` and a warning, so
  the README now names it as the exception.
- **Availability refusals are `CONFIG_INVALID` in the normal case.** The
  preflight refuses an add of a waiver player and a claim of a free agent
  locally. The `player_on_waivers` and `player_unavailable` kinds reach an
  agent only when the pool moved after the read. The README and ARCHITECTURE
  §9 now say so.

## Validation

- `uv run pytest`: 2045 passed, 19 skipped, including the README and sample
  tests that hold the docs to the command registry. `ruff check` and `ruff
  format --check` are clean, `check_budgets.py` passes (wheel 240,248 bytes of
  262,144), and `render_readme_samples.py --check` is clean.
- Behaviour is untouched. Where a doc claim could contradict the code, the code
  was read first. One stale comment in `src/fantasy_sports/commands/moves.py`
  still says `claim cancel`; it is left alone because this PR changes no code.

Closes #110.
