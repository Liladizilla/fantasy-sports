# Budget-aware waiver claim hint (#126)

## Why

The #110 docs pass (PR #125) found two inaccuracies in
`commands/moves.py` that it deliberately left alone. The `claim_player`
docstring still called the cancel command `claim cancel`, though it shipped as
`cancel-claim` in #123. And when `add` names a player on waivers, the
preflight refusal told the agent to run `claim --bid N`. A league without an
acquisition budget rejects any bid as `TRAN_ACQUISITION_BUDGET_NOT_SUPPORTED`,
so the remediation sent agents straight into a provider error.

## How

- `plan_move` takes the league's `acquisition` settings, which `execute_move`
  already reads for the dry run. `execute_move` now builds that dict before
  planning rather than after, and passes it in.
- The remediation adds ` --bid N` to the suggested command only when
  `uses_budget is True`. `False` and `None` (the provider did not say) both get
  a plain `claim`. Defaulting the unknown case to no bid matches the existing
  rule in `execute_move`: a bid is only safe where a budget is confirmed, and
  a missing bid in a budget league is still a valid claim.
- The bid flag sits inside the code span, so the hint stays one
  copy-pasteable command in every case.
- `docs/commands.md` has no sample of this hint, so nothing was regenerated.

Contributed by @Liladizilla in PR #127; brought to repo conventions
(changes-file name and shape, single-command hint wording) before merge.

## Validation

- New parametrized test in `tests/unit/test_moves.py` covers
  `uses_budget` true, false, and unknown: the remediation always names
  `fantasy-sports claim` and contains `--bid N` only for true.
- `uv run pytest -m 'not live'`, `ruff check`, `ruff format --check`,
  `scripts/check_startup.py`, and `scripts/check_budgets.py` all pass.

Closes #126.
