# WRITE_REJECTED and WRITE_DIVERGED in the error taxonomy (#106)

The write surface (epic #105) is about to start raising failures that no
existing code describes. A `409 TRAN_LINEUP_LOCKED` is not
`PROVIDER_UNAVAILABLE` (retryable, so an agent would loop on it) and not
`CONFIG_INVALID` (a human did nothing wrong). A write that ESPN accepted but
that read back differently is a third thing again. Every write command depends
on these classes existing first, so they land alone, before the transport
(#107) that raises them.

## What changed

- `core/errors.py` gains `WriteRejectedError` (`WRITE_REJECTED`, exit 11) and
  `WriteDivergedError` (`WRITE_DIVERGED`, exit 12), both `retryable=False`,
  both in `ErrorCode`, `ERROR_TYPES`, `error_type_for` and, in
  `output/errors.py`, `EXIT_CODES`. Still standard library only.
- `WRITE_REJECTED_KINDS`, a `Final` tuple of the eleven `kind` values, is
  exported from `core`. A `kind` outside it raises `ValueError`.
- ARCHITECTURE §5 gains the two table rows, the exit statuses, a paragraph on
  the `kind` discriminator, and the write-host-401 rule.
- `docs/adr/0010-...` records the decision; ADR-0004's status line and the ADR
  index are amended.
- CHANGELOG entry; this file.

## Decisions worth recording

- **`kind` defaults to `unknown`** rather than being required. The issue lists
  it as a constructor argument, but the existing generic tests build every
  `ERROR_TYPES` member from a message alone, and `ConfigInvalidError` already
  defaults its `kind`. `unknown` is also the honest answer for a raise site
  that has not classified the reason. The cost is that a raise site can forget;
  #107's tests should assert the kind it raises.
- **All five `details` keys are always present** (`provider_type`,
  `http_status` and `team_id` as `null` when unknown), so the payload shape
  does not depend on the reason. This matches the brief's example and the
  "every key, every time" rule of ADR-0004.
- **`team_id` is stored as a string**, whatever was passed, matching the plan's
  success payload (`"team_id": "1"`) rather than the brief's bare `1`.
- **`not_owned` is in the vocabulary** although ESPN never sends it. It is the
  client-side ownership guard (brief §6), and giving it a `kind` keeps one code
  for "this write will not go through".
- **`WriteDivergedError` redacts its map keys itself.** `core.redaction.scrub`
  walks values but not mapping keys, and `expected`/`observed` are keyed by
  caller-supplied ids. Fixing `scrub` for every error is the better change but
  is outside this issue; the gap is recorded in the ADR.
- **No docs sample, a hand-authored golden instead.** The sample generator
  needs a command line that resolves against the registry, and no command
  raises these yet. `tests/unit/golden/error-write-rejected.json` and
  `error-write-diverged.json` hold the shapes, and the
  "every code has a sample" test names the two codes it excuses and says to
  remove them when the first write command lands. `docs/commands.md` and the
  README are left alone so they do not advertise codes nothing can emit.

## Validation

- `uv run pytest -m "not live" --cov`: 1459 passed, 99.05% coverage
  (fail-under 90).
- `uv run ruff check .` and `uv run ruff format --check .`: clean.
- `scripts/check_budgets.py`: dependencies 5/5, wheel 169,888 bytes, cold start
  about 21 ms. `scripts/check_startup.py` passes.
- New tests cover construction, payload key sets and exact `details`, exit
  codes, the closed-vocabulary refusal (six bad strings), `kind` defaulting,
  scrubbing of a registered `espn_s2` and SWID across message, remediation,
  provider type, team id, details and divergence maps, the 401 rule, and
  rendering both codes to stderr through `emit` with the right exit status.
