# Capturing the add/drop/waiver surface in a throwaway league (#108, partial)

## Why

#107 shipped `move_players` and `cancel_transaction` with item shapes taken
from community code, and nothing could use them against a real league until
they were captured (#108). #18 and #109 both build on those shapes. John
joined a throwaway public league for this on 2026-10-14. The draft there
finished minutes before the first probe, so every unrostered player sat in
post-draft waivers and no `FREEAGENT` add could execute. This PR ships what
the waiver window could prove and leaves the executing `FREEAGENT` probes on
#108, which stays open (`Refs`, not `Closes`).

## How

- **Probe through the provider wherever it can express the shape.** `move`
  and `cancel` call `move_players` and `cancel_transaction`, so a capture
  tests the code that ships. `raw` exists only for bodies the provider
  refuses to build, such as an `ADD` without `toTeamId`. Even then it goes
  through `_begin_write` and `_post`, so the ownership guard and the current
  period still apply.
- **Make the wrong league unreachable.** The script takes only `--league
  test`. Before every POST it asserts the URL against the configured ids, the
  body's `teamId` against `mNav` ownership, and every item's team id. It
  keeps at least 5 s between writes through a state file, and it refuses to
  re-run a probe name.
- **Probe shape variations on pending claims, not executing adds.** A
  `WAIVER` claim validates items at submit time and changes nothing until
  processing. So each variation could be tried and cancelled with no player
  dropped. The roster ends identical to where it started.
- **`waiver=True`, not "bid 0 means bidless".** A FAAB-off league rejects
  every `bidAmount`, but a FAAB league legitimately takes a 0 bid. Making 0
  mean "omit" would break the second case. The flag is additive on the
  Protocol and the fake.
- **Two `kind` mappings, not seven.** Roster full and not-on-team map onto
  existing kinds. The availability pair, the bid-not-supported reason, and
  the item-shape reasons stay `unknown` with `provider_type` verbatim.
  Adding a kind is an ADR-0010 contract change, and #18 is the place to
  decide it, not a research PR.
- **Redaction keeps the joins.** Transaction ids become `<uuid-N>` rather
  than 05's single `<uuid>`. The cancel to claim link is the finding, and
  one placeholder would erase it. The synthetic player map continues 05's
  numbering so no id names two players.

## Validation

- 13 writes to team 7 of the test league. Nine were rejected, two claims
  went pending, and two cancels withdrew them. `mPendingTransactions` is
  empty for the team afterwards, and the final `mRoster` read-back matches
  the first slot for slot.
- No real league id, member id, transaction id, player id, or player name
  survives in `docs/research/06-espn-add-drop-surface/`, checked by grep
  against the private captures' values.
  `test_every_committed_fixture_names_only_public_leagues` passes.
- Unit tests replay the captured bodies: request bodies are compared
  field for field, and every captured rejection maps to its `kind`.
  `uv run pytest`, ruff, `check_budgets.py`, `check_startup.py`, and
  `render_readme_samples.py --check` all pass.
