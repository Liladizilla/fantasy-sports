# A plan for the write surface (#105)

The read path has been in production since 2026-09-12 and the write-surface
research (#92) landed the same day. Nothing writes. On 2026-10-14 John asked for
"full add/drop capabilities so you can manage waivers and adds", which is #18 —
an issue whose dependency line said to wait a season on lineup writes first.
That wait was a risk-sequencing choice, not a value judgement, and John
overrode it: build the irreversible class now.

## What changed

- **`docs/plans/2026-10-14-001-feat-espn-write-surface-plan.md`** turns the
  brief's §9 into nine units across one epic (#105): the two taxonomy codes
  (#106), the write transport with the ownership guard (#107), the journal
  (#16), `lineup set` (#15), the test-league capture (#108), `add`/`drop`/
  `claim` (#18), `claims`/`claim cancel` (#109), and a docs pass (#110).
  Trades (#112), the MCP server (#111), and surfacing irreversible writes to
  John (#113) are filed and deferred.

## Decisions worth recording

- **Lineups still fire first, but nothing waits a season.** The transport is
  shared; a lineup write is the only write that can be verified live in a real
  league and reversed, so it proves the transport. Adds and drops ride it in
  the next wave, not the next year.
- **The sanity gate becomes a flag.** ADR-0006's fourth gate survives for the
  irreversible class, and a CLI cannot invoke `/sanity-gate`. The enforcement
  point is `--acknowledge-irreversible`; the README will say an autonomous
  agent clears the gate before passing it. `--dry-run` reports cost.
- **No real-league add/drop until a throwaway league has produced the shapes.**
  The `FREEAGENT`/`WAIVER` item shapes are community code, not captures. John
  creates a test league (Cotrugli task filed); #108 captures there; #18's first
  live use is there by acceptance criterion.
- **CLI only, for now.** John confirmed he reaches the tool through Bash; the
  MCP projection stays a p3 follow-on, generated from the registry when wanted.

## Validation

Docs only. The plan was checked against the brief's §9 recommendations and
against every acceptance criterion on #15, #16, #18 as rewritten today; each
criterion maps to a unit in the table.
