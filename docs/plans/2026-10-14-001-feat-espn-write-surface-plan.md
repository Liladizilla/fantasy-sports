---
title: "feat: ESPN write surface — lineups, adds, drops, waivers"
date: 2026-10-14
type: feat
origin: docs/brainstorms/2026-08-26-agent-managed-fantasy-leagues-requirements.md
research: docs/research/05-espn-write-surface.md
epic: https://github.com/jwulff/fantasy-sports/issues/105
---

# feat: ESPN write surface

## Summary

Build the write half of `fantasy-sports`: one write transport with a client-side
ownership guard, two new taxonomy codes, a mutation journal with attribution and
scoped reversal, an explicit-state `lineup set`, and the irreversible class —
`add`, `drop`, `claim`, `claim cancel`, `claims`. Trades and the MCP server are
filed and deferred.

The 2026-08-27 plan excluded writes because the surface was unresearched. The
2026-09-12 brief (#92) researched it. This plan turns the brief's §9 into units.

---

## Decisions that shape the plan

**Build the irreversible class now, lineups first.** #18 said to wait a season
on lineup writes. John decided on 2026-10-14 to build add/drop/waiver now. The
sequencing stays: lineup writes fire first because they are the only write that
can be verified live in a real league and reversed, and the transport they prove
is the transport everything else rides on.

**Ownership is our invariant, not ESPN's.** The Supper Club cookie is the
commissioner's and ESPN honoured a cross-team lineup write on it with
`isLeagueManager: false` (brief §6). So every write resolves "my teams" from the
league payload and refuses any other `teamId` before a request is built. There
is no override flag. Whether ESPN honours commissioner authority on `FREEAGENT`
or `WAIVER` is unknown and is **never probed in a real league**.

**One rejection code, a closed `kind` vocabulary.** `WRITE_REJECTED` (exit 11)
with `details.kind` and ESPN's `TRAN_*` string verbatim, the same shape ADR-0009
gave `CONFIG_INVALID`. `WRITE_DIVERGED` (exit 12) for a write that succeeded
while the world moved. A write-host 401 on a credential that just read the
league is `WRITE_REJECTED` / `kind: auth`, never `AUTH_EXPIRED` (brief §5.1).

**A timeout is not a failure.** A socket timeout after the request was sent
leaves the transaction in an unknown state. The transport reports
`PROVIDER_UNAVAILABLE` with `transaction_state: unknown` and never retries on
its own; the remediation says to read `transactions` first. Harmless for a
lineup (a replay hits `TRAN_ROSTER_SAME_SLOT`), a double-spend for a claim.

**The sanity gate is an acknowledgement flag with a journaled justification,
and the plan says plainly what that does and does not enforce.** ADR-0006's
fourth gate survives for the irreversible class, and a CLI cannot invoke
`/sanity-gate` or verify that a caller did. What the tool *can* do: refuse an
irreversible write unless `--acknowledge-irreversible "<why>"` is passed with a
non-empty justification, state exactly what cannot be undone in the refusal,
and record the justification verbatim in the journal entry so a write made
without a real gate is visible to John after the fact rather than invisible.
The gate itself is enforced where it can be: in the agent's standing
instructions (README "Writing" section, and the `/sanity-gate` rule in John's
global `CLAUDE.md`). `--dry-run` reports cost and needs no flag.

**Irreversible writes are surfaced before the irreversible commands ship.**
R7a says John learns about a drop or claim without going to look. The journal
records it; something has to push it. #113 moves into the epic as a dependency
of `add`/`drop`/`claim`: the minimal mechanism (a `journal digest --since`
command plus the delivery hook John already reads — a daily Project Events
entry or a Cotrugli task) lands before the first irreversible command can.

**Credential age rides on every write result.** R12a: an agent should learn
its cookies are old *before* a deadline-bound write fails, not from the 401.
Every write envelope's `data` carries `credentials: {espn_s2_age_seconds,
warning}` from the auth chain's staleness reporting, and `--dry-run` carries it
too, so a pre-flight shows it.

**Shapes are captured before they are trusted.** The add/drop/waiver item shapes
are community-attested only. John creates a throwaway ESPN league; #108
captures every shape and every provokable rejection there, scrubbed as #92's
captures were, before any real-league use of #18.

**Unverified ESPN behaviour is never encoded as a passing test.** No cassette
asserts that ESPN rejects a cross-team write. The guard's test is a unit test
of the guard (`docs/memory/espn-lineup-write-authority-is-broader-than-team-ownership.md`).

---

## Requirements traced

| Origin | Where it lands |
|---|---|
| R6 explicit target state | #15 `lineup set --set PLAYER=SLOT` computes a diff, one POST |
| R7 journal with prior state, identity, command | #16 two-phase entry, member id as ESPN echoes it, role from `mNav` |
| R7a surface irreversible writes | #113, in the epic, blocks #18: `journal digest --since` + a delivery hook |
| R8 reversibility recorded; irreversible identified before firing | #16 `reversible` per kind; #18 `--acknowledge-irreversible "<why>"` |
| R8a calibration check | acknowledgement + journaled justification + README rule; `/sanity-gate` is the agent's step and the tool cannot verify it |
| R9 `--dry-run`, opt-in, no provider validation | every write command |
| R9a provider rejection as a machine code | #106 `WRITE_REJECTED` + `kind` |
| R10 purge by league and scoring-period tag | #107, on every 200 |
| R12a credential staleness before a write | #107: every write result and dry-run carries `credentials.espn_s2_age_seconds` and a `warning` |

---

## Units

| Unit | Issue | Track | Size | Depends on |
|---|---|---|---|---|
| W1 Error codes `WRITE_REJECTED`, `WRITE_DIVERGED`, ADR-0010 | #106 | core | s | — |
| W2 Journal store, `list`, `show` (reversal deferred to W5) | #16 | core | m | — (takes any `FantasySportsError`; no W1 type needed) |
| W3 Write transport, `WritingProvider`, ownership guard, role, purge, credential age | #107 | espn | m | W1 |
| W4 `lineup set` | #15 | espn | m | W2, W3 |
| W5 `journal reverse` | #16 | core | s | W3, W4 |
| W6 Test-league capture brief 06 | #108 | espn | s | John's league; W3 |
| W7 Surface irreversible writes: `journal digest`, delivery hook | #113 | reports | s | W2 |
| W8 `add`, `drop`, `claim` | #18 | espn | m | W4 live-proven, W6, W7 |
| W9 `claims`, `claim cancel` | #109 | espn | s | W8 |
| W10 Docs pass | #110 | cli | s | all |

Waves, each gated on the previous wave's PRs merging: **1** = W1 ‖ W2.
**2** = W3 ‖ W7. **3** = W4. **4** = W5 ‖ W6 (W6 as soon as the league exists).
**5** = W8. **6** = W9. **7** = W10.

Every unit is one PR with its own `changes/` file, TDD against injected
responses and the synthetic cassette, budgets green (ADR-0008), no cassette of
a private league.

---

## Shapes the units share

**Request** (brief §2.1, `espn_s2` only on the wire):

```json
{"isLeagueManager": false, "teamId": 1, "type": "ROSTER|FREEAGENT|WAIVER",
 "scoringPeriodId": <current>, "executionType": "EXECUTE",
 "items": [{"playerId": 1, "type": "LINEUP|ADD|DROP", ...}],
 "bidAmount": <WAIVER only>}
```

`CANCEL`: `executionType: "CANCEL"`, `relatedTransactionId`, no items
(community shape; #108 confirms).

**Success `data`** for every write command (OBJECT). `reversible` is per
kind, decided by the command, never a constant: `true` only for `lineup` while
the slots are unlocked; `false` for `add`, `drop`, `claim`; a `cancel`
withdraws a pending claim and is reported `reversible: false` too (you cannot
un-cancel; you resubmit). The example is a lineup set:

```json
{"team_id": "1", "kind": "lineup",
 "provider_transaction_id": "<uuid>", "status": "EXECUTED", "is_pending": false,
 "proposed_at": "2026-10-14T18:00:00Z", "items": [...],
 "journal_entry_id": "...", "reversible": true,
 "acknowledged_irreversible": null,
 "cost": {"bid_amount": null, "displaced_player_id": null},
 "credentials": {"espn_s2_age_seconds": 86400, "warning": null},
 "read_back": {"verified": true, "diverged": []},
 "dry_run": false}
```

A `drop` reports `"reversible": false` and `"acknowledged_irreversible":
"<the justification passed on the flag>"`. `--dry-run` returns the same object
with `dry_run: true`, no transaction id, no journal id, `credentials` filled,
and a `note` that provider acceptance was not validated.

**`WRITE_REJECTED` payload** (brief §5.3):

```json
{"code": "WRITE_REJECTED", "retryable": false,
 "agent_action": "Do not retry unchanged; read the roster and re-plan.",
 "remediation": "<ESPN's message, verbatim, redacted>",
 "details": {"kind": "roster_lock", "provider_type": "TRAN_LINEUP_LOCKED",
             "http_status": 409, "team_id": "1", "transaction_applied": false}}
```

---

## Out of scope

Trades (#112). MCP server (#111). Future-week lineups, IR rules, the non-LM
cross-team question (brief §8). Commissioner tooling of any kind. Retention and
cross-host consolidation of the journal.

## Risks

- **Shapes from community code are wrong.** Mitigated by W6 before W7; W3's
  `TRAN_*` map has an `unknown` kind so nothing is swallowed meanwhile.
- **A real-league write slips in during development.** Every write test uses an
  injected POST; live tests are `live`-marked and skip without
  `FANTASY_SPORTS_ESPN_LEAGUE` pointing at the test league; #18's first live use
  is in that league by acceptance criterion.
- **Budgets.** `requests.post` and the journal's `sqlite3` are imported inside
  the functions that need them; `--help` never pays for either.
