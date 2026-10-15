---
date: 2026-08-26
topic: agent-managed-fantasy-leagues
---

# Agent-Managed Fantasy Leagues

## Summary

A CLI that gives John's agents a live, readable picture of his ESPN fantasy
leagues and the ability to act on them. Reads carry enough context for an agent
to reason; writes let it manage the team. The tool exposes capability and holds
no judgment. Built to a public standard, sequenced to serve John first.

---

## Problem Frame

The project was scoped from a research finding: no maintained CLI exists for
ESPN fantasy leagues, and every prior attempt died when ESPN silently changed
its unofficial API. That framing produced a plan optimized for a durable public
artifact — a canary, a health manifest, release automation, CI-enforced budgets.

The framing was incomplete. The actual need is not a tool John types into. It is
an interface his agents use to see and manage his leagues on his behalf, across
a season, without him in the loop for every action. Drafting is incidental.

Two consequences follow. Reads have to be rich enough that an agent can decide
from them rather than guess. And writes are not a later phase — an interface
that can only observe does not manage anything.

The prior plan also encoded safety designed for a human operator: dry-run
defaults and interactive confirmation. Those protect nobody here. An agent
passes `--yes`. What actually protects John is knowing what his agent did and
being able to undo it.

---

## Key Decisions

**The agent is the primary user; John is the auditor.** Command surface,
output shape, error text, and help output are designed for an agent to consume
without external documentation. John's direct use is real but secondary, and
his primary interaction with the tool is reviewing and reversing what his agents
did.

**Tools, not policy.** The CLI exposes capability and contains no decision
logic. Start/sit reasoning, waiver valuation, and trade evaluation live in the
agent. This removes an authorization model, a policy engine, and every "should
we" branch from scope.

**Writes are core scope, not a later phase.** This supersedes ADR-0006, which
deferred writes to v0.3. A read-only release would demonstrate the idea without
delivering it.

**Auditability replaces confirmation — for reversible writes only.** ADR-0006
specified four gates. Three are removed: dry-run-as-default, the printed diff,
and interactive confirmation. The reasoning is not that an agent bypasses them —
it would bypass `--dry-run` equally — but that a synchronous human prompt cannot
be satisfied by an unattended process, so it converts unattended operation into
failure rather than into safety.

**ADR-0006's fourth gate stands.** Autonomous writes in the irreversible class
(drops, processed waiver claims, trades) still route through a pre-action
calibration check, per the standing high-stakes/irreversible-actions rule.
Auditability cannot substitute for prevention where reversal does not exist.

Explicit-state operations, a mutation journal, and scoped reversibility replace
the removed three. `--dry-run` survives as an opt-in flag because an agent
previewing its own change is a genuine use.

**Normalization serves readability, not portability.** This revises ADR-0002,
which defined the normalized layer as the structural intersection across
providers. Cross-provider equivalence is not a goal. ESPN's normalized output
may carry fields no other provider exposes. Raw payloads ship alongside
normalized ones in every response, so nothing is ever unreachable.

**Which portability constraints survive.** Dropping cross-provider equivalence
does not void every finding in `docs/ARCHITECTURE.md` §14. Those grounded in
ESPN's own model remain binding: plural `owner_names` (ESPN supports co-managers),
ESPN's two distinct transaction surfaces, both period identifiers preserved in
raw, and per-adapter standings tiebreakers. Finding 13 — keeping slot eligibility
out of the normalized model — is **amended** by R3, which requires it. The
provider interface remains shaped for Yahoo and Sleeper structurally; what is no
longer promised is that their normalized output will be comparable to ESPN's.

**Lineup writes first; the irreversible class follows.** The write surface splits
cleanly along the reversibility line drawn in R8, and it ships in that order.

*Phase one* is lineup changes only — the weekly management loop, reversible while
slots are unlocked, and the case the journal can actually restore. It proves the
write path against real ESPN behaviour before anything spends a resource.

*Phase two* is waiver claims, drops, adds, and trades. These are the irreversible
class and carry ADR-0006's surviving sanity gate. Adds are grouped here rather
than with lineup changes because an add that requires a corresponding drop
inherits the drop's irreversibility — the displaced player hits waivers and can be
claimed by anyone.

> **Amendment, 2026-10-15.** The order held and the wait did not. Lineup writes
> shipped first, because their transport and journal are what everything else
> rides on, and `add`, `drop`, `claim`, `claims`, and `cancel-claim` followed
> within the same epic (#105) at John's request rather than a season later.
> Trades are still unbuilt (#112). The surviving gate shipped as
> `--acknowledge-irreversible`, described in `docs/ARCHITECTURE.md` §9.

Both phases are wanted. The ordering is about which one absorbs the risk of an
unproven write layer, not about which is more valuable.

**Freshness is a correctness property.** Cache TTLs were tuned for latency. An
agent deciding a lineup against a kickoff deadline can be wrong because its data
was stale, which makes staleness a correctness concern and requires both a
stated data age and a way to demand a fresh read.

**Drift detection is core, not public-release machinery.** Noticing that ESPN
changed shape before a decision is made against broken data is John's exposure,
not an external user's — his agents run unattended against a live season on an
API mid-overhaul. Without it, R12's schema-drift code fires with nothing behind
it: the agent learns the shape is wrong but not whether it is known or worth
retrying. The public-facing health manifest and auto-filed issues stay deferred;
the detection does not.

**Public release is a close second, not a byproduct.** The quality bar in
ADR-0008 holds from the first commit. Sequencing puts John's working tool first;
standards do not relax to get there.

---

## Actors

- A1. **John** — commissioner of several ESPN leagues. Sets what his agents are
  allowed to do, outside this tool. Audits and reverses their actions.
- A2. **John's agents** — the primary consumer. Run in environments with shell
  access, read to reason, write to act, operate unattended.
- A3. **ESPN** — unofficial, undocumented, changes without notice, offers no
  programmatic authentication.

---

## Requirements

**Reads for agent reasoning**

- R1. Every read command emits a normalized rendering and every upstream response
  that contributed to the result, keyed by the request that produced it. Several
  commands stitch two or three ESPN calls, so a single "raw payload" would drop
  data this requirement promises is reachable.
- R1a. ESPN-sourced free text — team and league names, trade notes, waiver and
  offer comments, message-board content — is labeled as untrusted data, distinctly
  from normalized structured fields. Any league member can set these values, and
  they reach an agent that can write.
- R2. Normalized output is optimized for legibility; it is not constrained to
  fields other providers can match.
- R3. Roster, matchup, and free-agent output carries enough context to support a
  lineup decision without a follow-up call, including at minimum player status,
  position, opponent, any projection the provider exposes, slot eligibility, the
  player's current lineup slot, kickoff time, and slot lock state. Position alone
  does not tell an agent whether a player may fill FLEX in a given league.
- R3a. The league's roster-slot configuration is reachable from a read, so a valid
  target lineup can be constructed from normalized output alone.
- R4. Every response states the age of the oldest upstream fetch it draws on, and
  itemizes per-component ages when assembled from more than one provider call.
- R5. A caller can demand a guaranteed-fresh read that bypasses cached data.

**Writes and auditability**

- R6. Write commands accept explicit target state rather than relative
  instructions.
- R7. Every write records prior state to a journal before mutating, along with the
  acting identity, timestamp, league, and the command as invoked.
- R7a. Irreversible writes are surfaced to John without him initiating the check.
- R8. Every journaled write records whether it is reversible. Lineup changes are
  reversible while the affected roster slots are unlocked. Drops, processed waiver
  claims, and trades are **not** reversible, and must be identified as irreversible
  before the tool applies them.
- R8a. Irreversible writes pass a pre-action calibration check before firing.
- R9. `--dry-run` is available on every write command and reports the *intended*
  change without applying it. It does not validate provider acceptance — ESPN
  offers no preview endpoint. It is not the default.
- R9a. Write failures surface the provider's own rejection reason — roster lock,
  budget exceeded, position limit, already dropped — as a distinct machine code,
  so an agent can tell a rejected write from a transport failure.
- R10. Every cache entry carries a league, season, and scoring-period tag
  alongside its URL key, and a write purges every entry tagged with the league and
  scoring period it affects.

**Agent interface contract**

- R11. Every response is a versioned envelope.
- R12. Every failure carries a stable machine code distinguishing credential,
  availability, throttling, and schema-drift causes. A failure that cannot be
  positively classified maps to availability with bounded retry, never to
  throttling.
- R12a. Credential age and likely staleness are reportable independent of a
  failing command, and a write warns when credentials are near or past their
  expected lifetime — so an agent escalates before a deadline, not after it.
- R13. Help output is complete enough for an agent to discover and correctly
  invoke any command without external documentation.
- R13a. An agent can enumerate every configured league and target any of them
  explicitly on any read or write command, without editing configuration between
  calls.

**Quality and sequencing**

- R14. The tool meets its published performance and test budgets from first
  release, independent of whether anyone outside John has installed it.
- R15. No public-facing release ships before John's agents can both read and
  write his leagues.

---

## Key Flows

- F1. **Agent answers a question about the team**
  - **Trigger:** John asks his agent something about a league.
  - **Actors:** A2, A3
  - **Steps:** Agent invokes read commands; receives normalized and raw output
    with a stated data age; reasons over it; answers.
  - **Covered by:** R1, R2, R3, R4, R11, R13

- F2. **Agent acts on the league**
  - **Trigger:** Agent determines a roster change is warranted.
  - **Actors:** A2, A3
  - **Steps:** Agent optionally previews with `--dry-run`; issues an
    explicit-state write; the tool journals prior state, applies the change, and
    invalidates affected cache.
  - **Outcome:** League state changed, prior state recoverable.
  - **Covered by:** R6, R7, R9, R10, R11

- F3. **John audits or reverses agent action**
  - **Trigger:** John wants to know what his agents did, or disagrees with it.
  - **Actors:** A1
  - **Steps:** John reads the journal; optionally reverses a recorded write.
  - **Covered by:** R7, R8

---

## Acceptance Examples

- AE1. **Covers R4, R5.** Given a cached roster fetched eight minutes ago, when
  an agent requests the roster, then the response includes the data's age; when
  the agent instead demands a fresh read, then cached data is bypassed and the
  response reflects a new fetch.
- AE2. **Covers R6.** Given a roster whose state has changed since the agent last
  read it, when the agent submits an explicit target lineup, then the tool re-reads
  the roster afterward and either confirms it matches the target exactly or fails
  with a distinct machine code reporting the observed divergence — and the journal
  records both the intended and the observed state. One transaction is applied
  atomically, so a divergence means the world moved, not that part of the write
  landed. *(Amended 2026-10-15; see the note below.)*
- AE3. **Covers R7, R8.** Given an agent has set a lineup, when John inspects
  the journal, then he sees the prior lineup and the applied one; when he
  reverses it, then the prior lineup is restored.
- AE4. **Covers R9.** Given `--dry-run` on a write command, when it runs, then
  it reports the change it would make and league state is untouched.
- AE5. **Covers R12.** Given expired ESPN credentials, when any command runs,
  then the failure is distinguishable by machine code from an ESPN outage and
  from a throttling response, so an agent can tell "ask the human" from "retry
  later."
- AE6. **Covers R1.** Given any read command, when it returns successfully, then
  both the normalized rendering and the provider's raw payload are present.

> **Amendment, 2026-10-15 (AE2).** This example originally ended "ESPN offers
> no transaction boundary, so partial application is reported, not prevented."
> That was an assumption made before the write surface was researched, and it
> is wrong. A lineup write is one POST holding every changed slot, validated
> against the final lineup and applied all or nothing (research brief 05 §0
> row 3), so partial application does not occur. `WRITE_DIVERGED` still exists
> and still matters, but it reports that the world moved between the read and
> the write, or that the response's member was not the one the tool checked,
> never a half-applied write. The rest of AE2 stands as shipped in `lineup
> set` (#15).

---

## Scope Boundaries

**Deferred for later**

- Yahoo and Sleeper adapters — the provider interface is shaped for them,
  neither is built.
- An MCP server — the agents that matter have shell access; MCP becomes relevant
  only for surfaces that do not.
- Draft-specific tooling.
- Public release automation and package publishing. Sequenced after the core per
  R15, not cut. **Drift detection is not in this bucket** — see Key Decisions.

**Outside this product's identity**

- All decision logic: start/sit reasoning, waiver valuation, trade evaluation,
  projection modeling. The agent owns judgment.
- An authorization or policy model governing what an agent may do. That belongs
  to whoever operates the agent.
- Cross-provider comparability. Normalized output is per-provider by design.

---

## Dependencies and Assumptions

- Depends on `espn-api` for ESPN access — **for reads only.** The library has no
  write capability: every live call is a `GET` against `lm-api-reads.fantasy.espn.com`.
  R6 through R10 therefore require a hand-built mutation layer against an ESPN
  write surface that no research brief covers. One primary maintainer; the
  provider interface exists partly to contain that risk.
- Writes make `requests` a direct runtime dependency rather than a transitive one,
  raising the count to six against ADR-0008's ceiling of five. That ADR requires
  documented justification for a sixth; promoting writes to core scope is it.
- ESPN's throttling signal is unconfirmed. No 429 response body was observed in
  research, and `espn-api` folds every non-200/401/404 into a generic error
  carrying the status only inside a message string.
- Agent-side policy — what an agent may do unattended — lives with whoever operates
  the agent. The tool assumes such a policy exists and enforces none of it. Today
  the operator, the auditor, and the party bearing the loss are all John.

**Timing.** The league draft is being scheduled for early September 2026 and the
season starts 2026-09-09. Under deadline pressure the variable is **scope**,
not the standards in R14 — cutting the guardrails is the failure mode this note
exists to prevent.
- ESPN authentication is manually extracted browser cookies with no refresh
  path and no programmatic alternative. This constrains unattended operation:
  agents run unattended only until credentials expire, and expiry is silent.
- ESPN has an unofficial API undergoing a platform overhaul, with its API
  documentation currently offline. A breaking change that cannot be adapted to
  is a live risk to the whole product.
- Assumes agents run where a shell is available. If that stops being true, the
  deferred MCP surface becomes load-bearing rather than optional.

---

## Outstanding Questions

**Resolve before planning**

- What is the journal's retention policy, and is it per-league or global?
- Is scheduling and triggering of agent runs out of this product's scope, the way
  the authorization model is, or is it an unfilled gap? Nothing currently states
  what invokes an agent before a weekly lock.
- Does a guaranteed-fresh read refresh the cache as a side effect, or purely
  bypass it? This determines whether an agent can fetch fresh once and then batch
  subsequent queries.

**Deferred to planning**

- Journal storage shape and location.
- How cache invalidation on write is scoped — the mutated resource only, or the
  league's derived views as well.

---

## Sources and Research

- `docs/research/01-telemetry-auto-issues.md` — client error reporting; the
  basis for using the operator's own credential rather than shipping one.
- `docs/research/02-provider-data-shapes.md` — provider comparison. Its
  warnings about protecting a portable abstraction are largely moot now that
  cross-provider equivalence is out of scope; its findings on co-managed teams
  and ambiguous week semantics still hold.
- `docs/research/03-espn-api-surface.md` — ESPN endpoint surface, authentication
  mechanics, and breakage history.
- `docs/research/04-python-cli-packaging.md` — packaging, testing, and
  distribution practice.
- `docs/adr/` — the eight decision records this brainstorm revises in part.
  ADR-0002 and ADR-0006 both require amendment; see Key Decisions.
