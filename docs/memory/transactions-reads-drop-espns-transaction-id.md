# `transactions` cannot find a write by its id

**Found:** 2026-10-15, building `add`/`drop`/`claim` (#18). **Applies to:**
anything that joins a write's `provider_transaction_id` to the read side:
`claims` and `cancel-claim` (#109), a pending-claim read-back, reconciling a
`pending` journal row after a timeout.

A write answers with ESPN's transaction UUID (`WriteResult.provider_transaction_id`,
journaled as the join key). The read side never shows it. `espn-api`'s
`Transaction` keeps `type`, `status`, `scoringPeriodId`, `bidAmount`,
`processDate`/`proposedDate`, and the items, and throws away `id`, so
`_transaction()` builds `provider_id` as `f"{type}-{scoring_period}"`.
Every waiver claim in a period has the same `provider_id`.

So #18's first claim read-back matched a pending row (superseded in #109, see
"Done in #109" below) on everything the claim
submitted: type, team, the exact adds and drops, the bid (ESPN records a
bidless claim as `0`), and `raw.status == "PENDING"`. It reports `matched_by`
for exactly one match. No match, or several identical ones, is
`verified: null`, never `true`: an earlier identical claim satisfies the same
match, and an id join would not.

**For #109:** a `claims` command that lists pending claims *by id*, which is
what `cancel-claim` takes, has to read ESPN's body itself rather than go
through `league.transactions()`. Do it inside the adapter, and normalize the
id onto the row, so the command layer never parses ESPN's body. Prefer
`view=mPendingTransactions` (#121 captured it): a cancelled claim leaves it
at once. In `mTransactions2`, the claim's own row keeps reading `PENDING`
after a cancel. The cancel is a **new** transaction with
`status: "CANCELED"` and `relatedTransactionId` naming the claim. So a
`PENDING` row there does not mean the claim is still live.

**Done in #109.** `EspnProvider.fetch_pending_claims` reads
`mPendingTransactions` and puts the id on `PendingClaim.provider_id`. `claim`'s
read-back now joins on it, and the details match above is **gone**, not a
fallback. Codex on #122 found that a claim cancelled earlier with identical
details keeps a `PENDING` row in `mTransactions2` forever, so the match took
that ghost for a new claim and reported `verified: true`. Folding it out
needs the cancel's `relatedTransactionId` joined to the claim's id, and the
`transactions` read has neither id. An empty queue is now `verified: null`.

Related: [[espn-api-is-a-shape-reader-not-a-client]],
[[a-cancelled-claim-still-reads-pending]]
