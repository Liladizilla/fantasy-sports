# `add`, `drop`, and `claim`: the irreversible writes (#18)

## Why

An agent managing waivers has to add, drop, and claim players unattended, and
none of those can be undone by this tool. A dropped player can be claimed by
another team within seconds. A processed claim spends waiver priority and FAAB
that restoring the roster does not give back. An add that needs a drop
inherits the drop's irreversibility. John decided on 2026-10-14 to build these
now rather than wait a season. `lineup set` (#15) proved the shared write path
first, and the notify hook (#113) landed first so that John hears about every
irreversible write without having to look (R7a). Part of #105.

## What changed

- **`commands/moves.py`** adds three top-level OBJECT commands.
  - `add --team T --player P [--drop D] [--slot S]` sends a `FREEAGENT`
    transaction with an `ADD` item, plus a `DROP` item with `--drop`.
  - `drop --team T --player P` sends a `ROSTER` transaction with one `DROP`
    item.
  - `claim --team T --player P [--bid N] [--drop D]` sends a `WAIVER`
    transaction, with `bidAmount` only when `--bid` is given.
  - Each takes `--acknowledge-irreversible "<why>"` and `--dry-run`.
    `execute_move` mirrors `execute_lineup`.
- **`commands/writes.py`** now holds the ownership pre-check and the two
  journal-closing helpers. `lineup set` imports them from there too.
- **`tests/_harness.py`**: `WritableEspn` answers `FREEAGENT`, `WAIVER`, and
  `CANCEL` bodies. It moves players between a four-player free-agent pool and
  the roster, queues pending claims where `mTransactions2` lists them, and
  `reject_type` plays any `TRAN_*` 409.
- **`tests/unit/test_moves.py`** adds 72 offline tests through the real
  adapter. `tests/live/test_moves_live.py` is double opt-in and was not run.
- **Samples**: `add`, `add.dry-run`, `drop.dry-run`, `claim.dry-run`, and
  `error-config-invalid.acknowledge`. `docs/commands.md` gains three sections,
  README "Writing" gains the irreversible loop, ARCHITECTURE §9 gains the
  differences from `lineup set`, and `help.txt` lists the new commands.
- **Memory**: `transactions-reads-drop-espns-transaction-id.md`.

## How (decisions that were not obvious)

- **The gate runs after the plan and before the journal.** The refusal has to
  name the player who goes to waivers, so the players are resolved first. A
  refused invocation still sends nothing and journals nothing. Ownership is
  checked before the gate, so a wrong `--team` is `not_owned` whether or not
  the flag was passed. A blank or whitespace-only reason counts as missing.
  The reason is stored verbatim, not stripped, in `request`, in the argv, and
  in `data.acknowledged_irreversible`.
- **The flag cannot prove `/sanity-gate` ran.** It makes a skipped gate
  visible. The remediation and the README say an autonomous agent clears the
  gate first. `--gate-record PATH`, from the issue's notes, was not built.
- **The pool status preflight reads ESPN's `FREEAGENT`/`WAIVERS` from `raw`.**
  An add of a player on waivers and a claim of a free agent are refused before
  sending, with the command to use instead. Status is not normalized: it is
  ESPN's acquisition state, used only to refuse. A missing status refuses
  nothing.
- **`--player` must be in the pool read with a 500-player page.** A rostered
  player is never in the pool, so an add can never take another team's
  player by id.
- **Read-back follows `is_pending`, not the command.** An executed result
  re-reads the roster. The added player must be present, in `--slot` when one
  was named, and the dropped player absent. Anything else is
  `WRITE_DIVERGED`. A pending result, the normal answer to a claim, looks in
  this period's `transactions` for a pending waiver claim by the team for that
  player. The read side has no ESPN transaction id (see the memory note). A
  match is `verified: true` with `matched_by`. No match is `verified: null`
  with a note, never `true`. A match proves only that the claim was queued:
  a later cancel is a new transaction, and the claim's own `mTransactions2`
  row keeps reading `PENDING`.
- **Notify runs with the stored entry after it closes.** That covers
  `executed`, the read-back `diverged`, and the identity `diverged` raised by
  the adapter. On a `WRITE_DIVERGED` the result rides in `details.notified`,
  since there is no `data`. A rejected write is not announced. An entry whose
  `complete()` failed stays `pending` and is not announced: `notified` says so,
  and `doctor` reports the row.
- **Built against #108's captures (#121).** The first push used the
  community shapes. After #121 merged, the follow-up commit aligns with what
  the test league answered:
  - `--bid` became optional, and `claim` calls `move_players(waiver=True)`.
    A league without an acquisition budget rejects every bid, even `1`, as
    `TRAN_ACQUISITION_BUDGET_NOT_SUPPORTED`, so a bidless claim sends no
    `bidAmount`. `cost.bid_amount` is then `null`.
  - A drop goes as `ROSTER`, because `FREEAGENT` requires exactly one `ADD`.
    The adapter picks the envelope, and the tests assert the bodies.
- **Two new `WRITE_REJECTED` kinds, decided for #18.** `TRAN_PLAYER_NOT_FREEAGENT`
  is `player_on_waivers`: the re-plan is to claim instead.
  `TRAN_PLAYER_NOT_AVAILABLE` is `player_unavailable`: the player is on
  another roster, so pick someone else. Neither re-plan fits an existing
  kind, so `unknown` would have hidden it. ADR-0010's Decision section is
  amended with the date. By the ADR's own terms, a vocabulary edit is not an
  API change.
- **A FAAB preflight instead of a third kind.** `claim --bid` reads
  `settings.acquisitionSettings.isUsingAcquisitionBudget` from the league
  the read already memoised, so it costs no request. When the flag is
  `false`, the bid is refused with `CONFIG_INVALID` before anything is sent,
  in `--dry-run` too. `TRAN_ACQUISITION_BUDGET_NOT_SUPPORTED` therefore stays
  `unknown`: it can only arrive if the setting changed between the read and
  the write. A claim's dry run reports `acquisition_type` and `uses_budget`,
  both `null` when ESPN omits them, and `null` refuses nothing.
- **Codex review on #122: three P2 findings, all fixed.**
  - *The pending-claim match ignored the bid and the drop.* An older pending
    claim for the same player could verify a new claim ESPN had not shown.
    The match now requires the exact adds, drops, and bid; ESPN records a
    bidless claim as `0`. Two or more identical pending claims are
    `verified: null`, because this one cannot be told apart from them.
  - *A read-back divergence was journaled without its error.* `complete`
    stored `error=None`, so the row and the owner's notification omitted
    what disagreed. `Journal.complete` now takes `error` for a `diverged`
    entry and refuses one on `executed`. The error is built first and
    stored before the notify hook runs. `lineup set` had the same gap and
    gets the same fix. A journal that fails to close a divergence rides in
    `details.journal`, as for the other failed writes, instead of becoming a
    warning lost with the raise.
  - *The live test's add ran outside its cleanup guard.* A `WRITE_DIVERGED`
    or timeout on an add ESPN applied would have skipped the cleanup. The add
    is inside the guard now, and the `finally` decides what to undo from a
    fresh roster read, not from the failed command's report.
- **Rejections pass through unchanged.** `_TRAN_KINDS` comes from #121. Five
  of its captured 409s are replayed verbatim. They map to `roster_full`,
  `already_dropped`, `player_on_waivers`, `player_unavailable`, and
  `unknown` for `TRAN_ACQUISITION_BUDGET_NOT_SUPPORTED`. ESPN's type and
  message pass through verbatim. The other
  mapped kinds are covered by relabelling p8's body.
- **The helpers moved to `commands/writes.py` rather than being imported as
  private names from `lineup.py`.** One command importing another's
  underscored functions is a coupling that breaks quietly on rename.
- **The live test restores the roster size, not the roster.** It mirrors
  probes F2-F7 in brief 06. It adds with a drop, drops the added player, then
  adds a second free agent with `--slot BE`. The roster ends at its starting
  size, with that player in place of the bench player. The bench player is on
  24-hour waivers and is not restored, and the test says so.

## Validation

- `uv run pytest` (on the first push): 1942 passed, 18 skipped. The figures
  after the follow-up are in the PR thread. Line and branch coverage is 100%
  on `commands/moves.py`, `commands/writes.py`, and `commands/lineup.py`, and
  99% overall.
- `ruff check`, `ruff format --check`, `check_budgets.py`,
  `check_startup.py`, and `render_readme_samples.py --check` are all clean.
- **No live write was sent.** The test league's pool is all on waivers until
  Thursday league-hour 11, so a `FREEAGENT` add cannot execute yet. The
  request bodies are the ones #121 captured, sent by the adapter #121
  amended. The harness replays #121's rejection captures verbatim, but its
  success answers are hand-built in the same shape.
