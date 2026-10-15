# `claims` and `cancel-claim`: the waiver queue (#109)

## Why

An agent managing waivers has to see what is queued and withdraw a claim
before it processes, not only submit one. Until now it could submit (`claim`,
#18) but had no way to list its pending claims by id, and no way to cancel
one short of the ESPN app. `claim`'s own read-back could not join on the id
ESPN returned either, because `transactions` drops ESPN's transaction ids.
Part of #105. Builds on #118 (`cancel_transaction`), #121 (the captured
`WAIVER`/`CANCEL` shapes and `mPendingTransactions`), and #122 (`claim` and
the shared write helpers).

## How (decisions that were not obvious)

- **The queue comes from `view=mPendingTransactions`, not `mTransactions2`.**
  A cancelled claim's own `mTransactions2` row keeps reading `PENDING`
  (brief 06 §2), so reading the log reports withdrawn claims as live. The
  queue view drops a claim the moment it is cancelled, and it carries ESPN's
  transaction id. `EspnProvider.fetch_pending_claims` reads it through the
  league's read transport, so it is cached under the transactions TTL and
  scrubbed like every other read. A successful write already purges every
  league-tagged entry, so it is never stale after a claim or a cancel.
- **Shape changes are drift, including a missing key.** The captures kept only
  team 7's rows, so they could not show whether ESPN sends
  `pendingTransactions` for an empty queue. A read-only check against the
  test league before the live test showed it does, as `[]`. A body without
  the key therefore raises `SCHEMA_DRIFT` instead of reporting an empty
  queue, which would invite a duplicate claim. A key that is not a list, a
  row that is not an object, and a pending `WAIVER` row without `id`,
  `teamId`, or `items` raise too, each with a path. Rows of other types are
  skipped without validation.
- **`PendingClaim` is a new model, not a `Transaction`.** `Transaction`
  narrows to completed moves. `PendingClaim` carries `provider_id` (ESPN's
  id), `team_id`, `players_in`, `players_out`, `bid_amount`, `proposed_at`,
  `processes_at`, `status`, `sub_order`, and `raw`. `processes_at` was not in
  the brief for this issue. It is ESPN's `processDate`, which the captured
  queue row carries, and it tells an agent how long it has to cancel.
  `sub_order` is `null` from this view, which omits it. The model is kept out
  of `NORMALIZED_MODELS`, like `SessionRole`, because the writing protocol
  serves it.
- **`cancel-claim`, not `claim cancel`.** A typer group named `claim` shadows
  the top-level `claim` command, so `claim --player ...` stops parsing. This
  was checked with a two-command typer app before choosing the name. A test
  drives both through the CLI.
- **An id not in the team's queue is `CONFIG_INVALID`, not `WRITE_REJECTED`.**
  The issue's acceptance criteria say `WRITE_REJECTED` for a processed claim
  or another team's claim. This PR refuses both before sending, so ESPN never
  rejects anything, and the taxonomy reserves `WRITE_REJECTED` for a
  provider's refusal. The check reads the owning team's queue and requires
  the id there, with `details.pending_ids` and a pointer to `claims`. A
  `--team` the session does not own is still `WRITE_REJECTED` `not_owned`,
  from the shared `require_owned`, before the queue is read. So another
  team's claim is never sent, whether `--team` names that team or yours.
- **No `--acknowledge-irreversible` on a cancel.** The flag makes a caller
  state why it is giving something up. A cancel spends no FAAB and no
  priority, changes no roster, and a resubmitted claim restores what it
  withdrew. It is still `reversible: false`, because the tool cannot
  un-cancel. For the same reason `notify_irreversible` is not called and
  `notified` is `null`.
- **Two ids, both journaled.** ESPN answers a cancel with a new transaction
  id, which becomes the entry's `provider_transaction_id`. The claim's id is
  `data.cancelled_claim_id` and `applied_state.cancelled_claim_id`, next to
  ESPN's echoed `related_transaction_id`. The entry is kind `cancel`, with
  the claim as `prior_state`. The journal's remediation for reversing a claim
  now points at `cancel-claim`.
- **The cancel read-back reports, it does not raise.** The queue is re-read
  and the claim must be gone. Still listed is `verified: false` with the id
  in `diverged`, and an unreadable queue is `verified: false` with `error`.
  Each comes with a warning to run `claims` before acting again, and the
  entry closes `executed` with the read-back in `applied_state`. ESPN did
  accept the cancel, and the captured queue dropped a cancelled claim
  immediately, so a lingering row is more likely lag than a cancel that did
  not apply.
- **`claim`'s read-back joins on the id now, and the details match is
  removed.** The claim's id in the queue is `verified: true` with
  `matched_by: ["provider_transaction_id"]`. That join is exact, so two
  identical claims no longer make each other unverifiable. Not listed, with
  the queue empty or holding other claims, is `verified: null` with a note
  naming the claim id and the `claims --team` command to confirm it.
  `read_back.queue_size` says whether the queue was empty. Right after a
  `PENDING` answer, absence from the queue is lag, not doubt about the queue. The first push of this PR kept #18's details match against
  `transactions` as a fallback for an empty queue. Codex's post-merge P2
  finding on [#122](https://github.com/jwulff/fantasy-sports/pull/122)
  showed why that is unsafe: a claim cancelled earlier keeps an
  identical `PENDING` row in `mTransactions2` forever. With the new claim not
  yet visible, that ghost was the single match and verified a claim ESPN had
  not shown. Folding cancelled rows out needs the cancel's
  `relatedTransactionId` joined to the claim's id, and the `transactions`
  read carries neither, so the fallback was removed rather than patched. A
  regression test plays exactly that case.
- **`claims --team` defaults to the session's own team.** It derives the team
  from `session_role().owned_team_ids`. With none, for example without a
  SWID, or with several, `--team` is required. Reading another team's queue
  is allowed, as in the ESPN app.
- **The harness plays ESPN's queue.** `WritableEspn` serves
  `mPendingTransactions` in the captured row shape. It gives a second claim
  its own id and answers a `CANCEL` the way p5 was captured: a new id,
  `CANCELED`, the claim's items, and `relatedTransactionId`. It keeps the
  claim's `PENDING` row plus the `CANCELED` row in `mTransactions2`, as ESPN
  did. `pending_view=False` empties only the queue, to play the stale
  cancelled row with nothing queued.

## Validation

- `uv run pytest`: 2045 passed, 19 skipped. Line and branch coverage is 100%
  on `commands/claims.py` and `commands/moves.py`, and 99% overall.
  `tests/unit/test_claims.py` adds 40 tests. `test_moves.py` rewrites its
  claim read-back tests for the id join and adds the stale-cancelled-row
  regression from the #122 finding.
- `ruff check`, `ruff format --check`, `check_budgets.py`,
  `check_startup.py`, and `render_readme_samples.py --check` are all clean.
  The `claims` and `claim-cancel` samples were generated offline through
  `WritableEspn`.
- **One live round-trip in the throwaway test league**, through
  `tests/live/test_claims_live.py` with the double opt-in. It claimed the
  lowest-owned player on waivers, bidless, with a bench drop because the
  roster was full. The claim was `PENDING` with the read-back verified by id.
  `claims` listed it, `cancel-claim` answered `CANCELED` with the read-back
  verified, and the queue was empty afterwards. A separate read of `claims`
  confirmed nothing was left pending. No add or drop was sent, and no other
  league was touched.
