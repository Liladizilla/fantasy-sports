# ADR 0006: Read-only in v0.1; writes gated behind dry-run and confirmation

**Status:** Superseded 2026-10-15 by [ADR-0010](0010-write-codes-one-code-with-a-kind-discriminator.md), the write-surface plan (`docs/plans/2026-10-14-001-feat-espn-write-surface-plan.md`), and `docs/brainstorms/2026-08-26-agent-managed-fantasy-leagues-requirements.md`; the shipped posture is `docs/ARCHITECTURE.md` §9
**Date:** 2026-08-26
**Superseded:** 2026-10-15 (was *Partially superseded 2026-08-27*)

> **What shipped in place of the four gates (2026-10-15).** All four gates
> below are gone as written, and the decision they served is settled: writes
> exist, and nothing waited a season. Dry-run as the default, the printed diff,
> and interactive confirmation were replaced by explicit target state, a
> mutation journal written before the request and read back after it, and
> reversal for lineups. The fourth gate, `/sanity-gate` for the irreversible
> class, became `--acknowledge-irreversible "<why>"` on `add`, `drop`, and
> `claim`: a CLI cannot invoke the gate or verify that a caller did, so the
> flag refuses the write without a non-blank justification, states what cannot
> be undone, and journals the justification verbatim, which makes a skipped
> gate visible instead of preventing it. The prevention lives in the agent's
> standing instructions (the README's "Writing" section). An ownership guard
> with no override, an owner notification for every irreversible write that
> took effect, and a credential-age warning on every result were added. Trades
> remain unbuilt (#112).

> **What was superseded.** Read-only v0.1 and the deferral of writes to v0.3 are
> reversed: writes are core scope, because an interface that can only observe does
> not manage anything. Three of the four gates below are removed — dry-run as the
> default, the printed diff, and interactive confirmation. The reason is not that
> an agent will pass `--yes`; that argument would invalidate `--dry-run` equally.
> It is that a synchronous human prompt cannot be satisfied by an unattended
> process, so it converts unattended operation into failure rather than safety.
>
> **What still stands, and is not negotiable.** The fourth gate — routing
> autonomous writes through `/sanity-gate` — survives for the irreversible write
> class: drops, processed waiver claims, and trades. The 2026-08-27 document
> review established that reversibility, the control offered in exchange for the
> other three gates, does not exist for those operations. A dropped player can be
> claimed within seconds, a processed waiver claim consumes FAAB and priority, a
> trade needs the counterparty's consent, and any lineup change locks at kickoff.
> Auditability cannot substitute for prevention where reversal is unavailable.
>
> **What this ADR got right.** Its stated rationale — "a miscalculated automated
> waiver claim is a real, un-undoable cost" — was correct, and the reframe
> initially failed to engage with it.

## Context

The obvious value of this tool includes writes: setting a lineup, submitting a
waiver claim, dropping a player. Those are also irreversible actions with real
cost — a miscalculated waiver claim cannot be undone, and an automated one can
fire while the user is asleep.

Separately, the read path is running against an API that we do not yet know
empirically. Building writes on top of a read layer that has not survived contact
with a real season compounds risk.

## Decision

**v0.1 is read-only.**

Writes land in v0.3, behind all of:

- `--dry-run` as the **default**; mutation requires explicit `--commit`
- a printed diff of before/after state
- interactive confirmation unless `--yes`
- any autonomous/scheduled write routes through `/sanity-gate`

## Consequences

**Easier:** v0.1 ships before the season starts. The read path gets weeks of real
use before anything can mutate state. No possibility of the tool costing the user
a roster spot during its least-tested phase.

**Harder:** The tool is not fully useful for a season until v0.3.

**Accepted:** This follows the standing high-stakes/irreversible-actions rule in
`~/.claude/CLAUDE.md`. The gate is not negotiable later for convenience.

## Alternatives considered

**Writes in v0.1 behind a flag** — tempting for immediate season usefulness.
Rejected: it front-loads the riskiest surface onto the least-proven code.

**Full write automation from the start** — rejected outright for v0.1.
