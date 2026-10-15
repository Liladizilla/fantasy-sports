# ADR 0010: Write failures get two codes, and rejection reasons ride in `details.kind`

**Status:** Accepted
**Date:** 2026-10-14
**Amended:** 2026-10-15 (#18: `player_on_waivers`, `player_unavailable`)
**Amends:** ADR-0004 (the taxonomy gains `WRITE_REJECTED` at exit 11 and
`WRITE_DIVERGED` at exit 12)

## Context

The write surface (epic #105) needs a distinct machine answer for each way a
write can fail. The research brief (`docs/research/05-espn-write-surface.md`
§5) captured the rejection vocabulary against a real league: every roster-rule
refusal is a `409` carrying a typed `TRAN_*` string (`TRAN_LINEUP_LOCKED`,
`TRAN_ROSTER_SLOT_LIMIT_EXCEEDED`, `TRAN_ROSTER_SAME_SLOT`,
`TRAN_INVALID_SCORINGPERIOD_NOT_CURRENT`), and four more reasons the product
needs (budget, position limit, already dropped, roster full) are inferred but
not yet observed (§5.2). The set is unlikely to be closed on ESPN's side.

Two further facts shape the answer. A write-host `401` is one constant for no
cookie, an invalid cookie, and a missing SWID (§5.1 item 1), and every write is
preceded by a read of the same league on the same cookie. And a `200` is not
the end of a write: the plan reads the roster back, and a mismatch means the
transaction may have applied.

## Decision

**Two new codes.**

- **`WRITE_REJECTED`**, exit 11, `retryable: false`. The provider refused the
  write, or our own pre-flight did. Nothing was applied.
- **`WRITE_DIVERGED`**, exit 12, `retryable: false`. The write was accepted and
  the read-back does not match what was asked.

**`WRITE_REJECTED` carries the reason in `details.kind`, a closed vocabulary we
own:** `roster_lock`, `slot_limit`, `same_slot`, `not_current_period`,
`budget_exceeded`, `position_limit`, `already_dropped`, `roster_full`,
`player_on_waivers`, `player_unavailable`, `not_owned`, `auth`, `unknown`. Its `details` always hold five keys: `kind`,
`provider_type` (ESPN's string verbatim, or `null`), `http_status`, `team_id`
(a string), and `transaction_applied` (always `false`, and not a constructor argument, because a rejection never applies). The vocabulary is a
`Final` tuple exported from `core/errors.py`. A `kind` outside it raises
`ValueError`: it is a bug in the raise site, not a failure to report.
`kind` defaults to `unknown`, as `ConfigInvalidError.kind` defaults to
`config`.

**Amended 2026-10-15 (#18): two availability kinds.** The add/drop capture
(`docs/research/06-espn-add-drop-surface.md`) observed two refusals that ask
the agent for different re-plans, and neither fits an existing kind.
`TRAN_PLAYER_NOT_FREEAGENT` is `player_on_waivers`: the player is on waivers,
so file a claim instead. `TRAN_PLAYER_NOT_AVAILABLE` is `player_unavailable`:
the player is on another roster, so pick someone else. By this ADR's own
terms, extending the vocabulary is not an API change. `TRAN_ACQUISITION_BUDGET_NOT_SUPPORTED`
stays `unknown`, because `claim` refuses a bid in a budgetless league before
sending.

**`not_owned` is ours.** ESPN does not reject a commissioner's cross-team
write (brief §6), so ownership is a client-side guard and needs a name in the
same vocabulary.

**A write-host 401 never maps to `AUTH_EXPIRED`.** With nothing sent it is
`AUTH_MISSING`. With a credential sent it is `WRITE_REJECTED` with
`kind: "auth"` and `provider_type: "AUTH_MISSING_CREDENTIALS"`. `AUTH_EXPIRED`
needs positive evidence that the credential failed, and this body is not that:
the read that preceded the write just proved the session valid.

**`WRITE_DIVERGED` carries `details.expected` and `details.observed`**, field
names and ids only (player id to slot id), never provider free text. Both keys
are always present, as empty maps when the raise site has nothing to say.

## Consequences

**Easier:** a reason ESPN adds tomorrow arrives as `kind: "unknown"` with its
`TRAN_*` string in `provider_type`, so it is reported rather than lost, and
mapping it later is a vocabulary edit, not an API change or a new exit status.
Agents that only want "this request will not work as sent" branch on the exit
status or `retryable` and need nothing else.

**Harder:** an agent that wants to choose a re-plan per reason reads
`details.kind` instead of the exit status. Extending the vocabulary is still a
deliberate edit to a tuple and to this contract; the difference is that it does
not consume an exit number.

**Accepted:** two exit numbers are spent, 11 and 12, inside the portable range
ADR-0004 fixed. `remediation` on a `WRITE_REJECTED` is ESPN's message, redacted
at construction like every other string; it is the one place provider prose
reaches a payload, by design, because it is what a human acts on. The base-class
scrub redacts registered secrets in string values, not in mapping keys, so
`WriteDivergedError` redacts its own keys.

## Alternatives considered

**One top-level code per reason** (`ROSTER_LOCKED`, `SLOT_LIMIT`, ...).
Rejected. Every reason ESPN adds becomes an API change and a new exit status,
and the four observed are unlikely to be the whole set. Every reason also
gives an agent the same instruction, so the extra codes would duplicate it
under new names. This is the argument ADR-0009 made for `CONFIG_INVALID`.

**Fold divergence into `WRITE_REJECTED` as another `kind`.** Rejected. A
rejection did not apply and a divergence may have, so the safe next step
differs: re-plan and resend versus read and reconcile before anything is sent.
That is the one distinction an exit code exists to carry.

**Map the write-host 401 to `AUTH_EXPIRED`.** Rejected, and an earlier draft of
the brief did exactly this before review caught it. It would send the user to
re-extract working cookies on every attempt and hide a write-side policy change.

**Reuse `PROVIDER_UNAVAILABLE` for rejections.** Rejected. It is
`retryable: true`; a rejected write can never succeed unchanged.
