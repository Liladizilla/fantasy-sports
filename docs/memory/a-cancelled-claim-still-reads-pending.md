# A cancelled waiver claim still reads `PENDING` in `mTransactions2`

**Found:** 2026-10-15, capturing the add/drop surface for #108 in a
throwaway league. **Applies to:** `claims` / claim cancellation (#109), the
journal's reconciliation of pending writes (#16), and any read that answers
"what is still queued?".

**The trap.** A claim is one `mTransactions2` row with `status: "PENDING"`,
`isPending: true`. Cancelling it does not touch that row. ESPN writes a
**second** row: a new id, `executionType: "CANCEL"`, `status: "CANCELED"`, the
claim's items echoed, and `relatedTransactionId` naming the claim. Thirty
seconds later the claim's own row still reads `PENDING`. A reader that filters
`mTransactions2` on `isPending` reports a withdrawn claim as live, and an agent
that "re-cancels" it, or refuses a new claim because one "is already queued",
acts on a ghost.

`view=mPendingTransactions` is the authoritative queue: it held the claim while
it was live and was empty immediately after the cancel
(`docs/research/06-espn-add-drop-surface/pending-transactions.json`). Read
that, or fold `mTransactions2` by `relatedTransactionId` before trusting a
`PENDING` status. A processed claim was not observed, so how its row reads
afterwards is unknown.

The view answers with the league envelope plus `pendingTransactions`, and
sends that key as `[]` when nothing is queued (seen live, #109). So a body
without the key is drift, not an empty queue: `_pending_claims` raises
rather than tell an agent nothing is queued. The queue row carries
`processDate`, when waivers will run it, but not `subOrder`.

**The second, smaller trap: a just-drafted league has no free agents.** Every
unrostered player is `WAIVERS` until the first waiver run after the draft, so a
`FREEAGENT` add is `TRAN_PLAYER_NOT_FREEAGENT` whatever its shape. A probe or
test plan that assumes a free-agent pool right after a draft stalls; check
`kona_player_info` with `filterStatus: ["FREEAGENT"]` first. ESPN also checks
availability before roster size, so on a full roster that rejection hides the
`TRAN_ROSTER_LIMIT_EXCEEDED_ONE` you might have expected.

Related: [[espn-lineup-write-authority-is-broader-than-team-ownership]],
[[espn-401-tells-you-nothing]]
