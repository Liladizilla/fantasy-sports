# Tell John about irreversible writes without being asked (#113)

## Why

R7a: an add, a drop, or a claim cannot be undone by this tool, and agents will
be making them unattended across three leagues. The journal (#16) records each
one, but a record nobody opens is not an audit. The 2026-08-27 review flagged
exactly that gap: a drop John never sees. #18 (the first irreversible command)
is blocked until something pushes these writes at him, so this lands first.

## What changed

- **`journal digest --since <duration|timestamp> [--irreversible] [--league]
  [--limit]`**, read-only, `takes_league=False`, shaped like `journal list`.
  `--since` is `30m`/`24h`/`7d` or an ISO-8601 timestamp with an offset;
  anything else is `CONFIG_INVALID` with `kind: "argument"`. `Journal.list`
  gained a `since` filter (a comparison on the stored UTC millisecond text).
- **`reports/notify.py`** (new layer, standard library only, `subprocess`
  imported inside the call): `notify_irreversible(entry, *, config, runner)`
  runs the `[notify] irreversible_command` shell string with the entry as JSON
  on stdin and returns a frozen `NotifyResult`. `should_notify(entry)` is true
  for an irreversible entry that ended `executed` or `diverged`.
- **`config/notify.py`** parses the `[notify]` table; `config/document.py`
  gained a public `read_document` that `leagues` now delegates to.
- **`doctor` `notify` check**, and **`scripts/notify-irreversible-example.sh`**.

## How (the non-obvious decisions)

- **Which hook, per the issue's "pick one and wire it".** The issue offered a
  vault `Project Events.md` entry or a Cotrugli task. Both would make this
  package know about John's vault or Cotrugli, which criterion 3 forbids. So
  the package ships a generic mechanism, a digest to pull and a configured
  command to push, and John's delivery is a script of his own behind
  `irreversible_command`. That script can file a Cotrugli task or write the
  vault entry; none of it is in this repo.
- **Notification never affects the write.** The write has already happened by
  the time we tell anyone. `notify_irreversible` therefore never raises: a
  missing command (shell exit 127), non-zero exit, timeout, `OSError`, and an
  unreadable `config.toml` are all values in the result. Error text names the
  failure class only, never the command line or its output, since a script we
  do not own could echo anything.
- **No second path around redaction.** The stdin payload is exactly
  `entry.to_dict()`, which the journal redacted on the way in. A test journals
  an entry carrying a registered secret through a real store and asserts the
  secret is absent from what the subprocess is given.
- **Rejected attempts do not notify.** A rejected irreversible write applied
  nothing. `pending` does not either: it has not ended, and `doctor` already
  flags one that never does.
- **The config reader lives in `config/`, not `reports/`.** `doctor` needs to
  report on `[notify]`, and `reports/` must stay off the CLI import path. A
  test asserts it is absent after importing the entry point and resolving every
  command, and that nothing in `core/` or `providers/` mentions it.
- **`[notify]` is a table `save()` does not own**, so it survives a save like
  `[credentials]` does; a test extends that pattern. Unknown keys inside it are
  tolerated. A table of the wrong type is `CONFIG_INVALID` on read and a `warn`
  in `doctor`, because silently ignoring it would leave John believing he would
  be told.
- **A timeout kills the whole process group.** `subprocess.run(timeout=)`
  kills only `/bin/sh`, so a hung delivery script's child could fire after we
  had reported a timeout. The default runner is a `Popen` in its own session
  that sends SIGKILL to the group on timeout and reaps it. The `runner` seam
  keeps `subprocess.run`'s signature; only the default changed.
- **The `--since` cutoff rounds up, never down,** to the stored millisecond,
  so a cutoff of `.000999Z` cannot admit an entry at `.000Z`.
- **The example hook sets `umask 077`** so its audit log is not world-readable.
- **The `--since` window is on `recorded_at`**, when the entry was begun. A
  write begun before the window and closed inside it appears only in the
  earlier window. The help says to overlap windows and de-duplicate on `id`.

## Validation

- New tests: `test_notify.py` (config, `should_notify` matrix, injected-runner
  behavior, one real `cat > file` end to end, the secret test, the example
  script), digest cases in `test_journal_commands.py`, `notify` cases in
  `test_doctor.py`, and the import-layering tests in `test_imports.py`.
- The `journal-digest` sample was generated offline with the script's own
  `_journal_samples`. `doctor.json` is an `offline: false` live sample and
  regenerating it needs ESPN, so its `notify` entry was inserted by hand in
  the shape the code emits; regenerate it with the next live refresh.
- Not done: calling `notify_irreversible` from a write command. There is no
  write command yet; #18 does that.

Refs #105, #16, #18.
