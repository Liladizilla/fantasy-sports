# The mutation journal store: record the intent before the write (#16)

The journal is the control that replaced dry-run defaults and interactive
confirmation for reversible writes (#105). It only does that job if, after the
fact, John can ask "what did my agent do, acting as whom, and what did it
cost?" and get an answer from a file the agent cannot quietly skip writing.
Today nothing writes, so there is nothing to answer with. This change builds
the store and the read side, so that every write unit that follows has one
place to record to and one place a person can audit from. `journal reverse`
is not here: it needs the write transport from #107.

## What changed

- **`core/journal.py`** (standard library only; `sqlite3` is imported on first
  use). A frozen `JournalEntry` with the fields #16 lists, and a `Journal`
  store at `$XDG_STATE_HOME/fantasy-sports/journal.sqlite`, file `0600` in a
  `0700` directory, both asserted by tests (including under a permissive
  umask and over a pre-existing loose directory).
- **Two-phase writes.** `begin()` stores the entry with its prior state and
  request *before* anything is sent. `complete()`, `reject()`, and `diverge()`
  close it. A process that dies in between leaves a `pending` row that
  `pending()` returns and `doctor` reports.
- **`journal list [--league] [--limit] [--irreversible]` and `journal show
  --id`**, read-only, `takes_league=False`, with the envelope shaped exactly
  like `doctor`'s (`provider`, `league_id`, `season` null). An unknown id is
  `CONFIG_INVALID` with `kind: "argument"`, the nearest ids in
  `details.nearest`, and a remediation naming `journal list`.
- **A `journal` check in `doctor`**: path, existence, entry count, pending
  count and ids, file and directory modes. It never creates the file. A
  corrupt file is a `fail` finding, pending rows are a `warn`, loose modes are
  a `warn`.
- `config.paths.state_home()`, the generator and its two new samples
  (`journal-list`, `journal-show`), `docs/commands.md`, the README table, and
  the `doctor` and `--help` samples.

## Decisions worth recording

- **`member_id` is stored as a pseudonym, not as ESPN echoed it.** #16 asks
  for the acting member "as ESPN echoes it" and also for a test proving no
  SWID GUID can land in the file. ESPN's `memberId` *is* the account's SWID
  (research brief §2.3), so the two cannot both hold literally. The source of
  the attribution is what the issue cares about (the response, never the
  request), so the value is run through the same keyed-BLAKE2b pseudonym the
  response cache uses, under a random salt kept in the journal file itself
  (`journal_meta`). It is stable within one journal, and a GUID in a request
  body maps to the same pseudonym, so "same member every time" and "request
  matches response" are both readable. The join to ESPN's ledger is the
  transaction `id` and `proposedDate`, which are stored verbatim. If John
  wants the raw GUID after all, it is one line in `Journal._clean_member`, and
  it would contradict the acceptance criterion.
- **Absent is empty; unreadable is loud.** A missing file, or a zero-byte one
  that was never given a schema, reads as an empty journal. A file with
  content that cannot be read, or that has lost its `journal` table, raises `CONFIG_INVALID`
  (`kind: "config"`, `details.path`; no new `kind`, per ADR-0009) from every read, because "no entries" about a
  corrupt audit trail would be false. `doctor` is the one caller that turns it
  into a finding (`Journal.health()` never raises). This is the opposite of
  the response cache, which degrades to a live fetch: a write that cannot be
  journaled must not proceed, so `begin()` raises with a remediation naming
  the path. It also refuses to recreate the schema over a populated file
  that lost its table, so damage is never turned into a healthy empty journal.
- **Attribution comes only from the response.** `begin()` resets `member_id`
  along with the other result fields, so a pending or rejected row never
  carries an identity taken from the request. The pseudonym exists because the
  SWID is a secret (CLAUDE.md rule 5) and ESPN's transaction `id` is the ledger
  join.
- **State dir, not data dir.** #16 says the XDG *state* directory, so
  `state_home()` is new; `data_home()` was reserved for this and stays unused.
- **No new error code.** An entry closes once; closing it twice is a
  `ValueError` and an unknown id a `LookupError`, both programming errors in
  the (future) caller, like `DataShapeError`. Adding a code is an API change
  and nothing here needs one.
- **Timestamps are millisecond-truncated at construction**, so an entry equals
  its own round trip and ESPN's epoch-ms `proposedDate` survives exactly.
  Naive datetimes are refused, as in `output/envelope.py`.
- **The samples are seeded.** Nothing writes to the journal yet, so the two
  samples seed an invented journal through the store's own API with fixed ids,
  timestamps, and a fixed salt (the `swid_salt` argument to `Journal.open`,
  documented as for fixtures only, and ignored if the file already has one).
  They are regenerated offline and compared by the existing sample test. The
  live `doctor` sample was updated by inserting the new check rather than
  regenerated, because a clean sandbox has an empty cache and would have
  rewritten an unrelated line.

## Validation

- `uv run pytest -m "not live" --cov`: all pass, coverage 98.9% (floor 90).
  New tests cover the permissions, the two-phase lifecycle, a byte-level read
  of the SQLite file after a fake `espn_s2` and several GUIDs were placed in
  the command, request, prior state, error message, a mapping key, and
  `team_id`, the corrupt, zero-byte, missing, and unwritable cases, every new
  `doctor` branch, and the CLI projection.
- `ruff check` and `ruff format --check` clean; `check_budgets.py` and
  `check_startup.py` pass (cold start `--help` 28 ms against 50), and an
  import test asserts the module pulls in no CLI stack and no `sqlite3`.
- `render_readme_samples.py --check` is clean, and the sample test
  regenerates both journal samples byte-for-byte apart from timestamps.

## Left for later

- `journal reverse` (needs #107).
- Retention and cross-host consolidation, out of scope in #16.
