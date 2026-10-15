"""Generate every sample in the docs, and splice them into place.

The command reference (``docs/commands.md``) and the README show real output
for every command, every output mode, and every error code. None of it is
typed by hand: this script runs the CLI, trims what came back, writes each
sample to ``docs/samples/``, and replaces the block between every
``<!-- sample: NAME -->`` / ``<!-- /sample -->`` marker pair in the docs with
the current content. ``tests/unit/test_readme_samples.py`` re-runs the splice
in check mode, so a sample edited by hand — or a doc that drifted from its
sample — fails CI (jwulff/fantasy-sports#93).

Three sources, and every sample says which one it came from:

* **live** — ESPN's public test league ``1234``, season ``2018``, fetched at
  generation time. It needs no credentials: the CLI resolves the credential
  chain before it sends anything, so the sandbox supplies placeholder cookies
  through the environment, and ESPN serves a public league without reading
  them. ``docs/testing.md`` §6 is why this league and no other is
  committable.
* **replayed** — the invented league ``99``, season ``2026``, served offline
  from ``tests/cassettes/espn/synthetic_2026.yaml`` through the same
  transport stub the unit tests use. It covers ``box-scores`` and
  ``free-agents``, which ESPN refuses before 2019 (the refusal itself is a
  live sample), and the ``auth`` commands, whose Keychain is replaced by an
  in-memory store and whose prompt is answered with placeholder cookies.
* **synthetic** — an error envelope built directly from the exception class
  in ``core/errors.py`` and rendered by the output layer, for the four codes
  that need a live failure nobody can safely provoke: an expired cookie, an
  outage, a throttle, and a schema change.

Nothing here touches ``~/.config``, ``~/.cache``, or the macOS Keychain. Every
command runs against a throwaway XDG tree, and the only substitution made to
captured output is rewriting that tree's path back to ``~/.config`` and
``~/.cache`` so the samples read as they would on a real machine.

Usage::

    uv run python scripts/render_readme_samples.py           # regenerate + splice
    uv run python scripts/render_readme_samples.py --splice  # splice committed samples
    uv run python scripts/render_readme_samples.py --check   # exit 1 if the docs drifted

This is a development script. It is not part of the wheel and nothing in
``src/`` imports it; the test imports only its pure functions.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SAMPLES_DIR = REPO / "docs" / "samples"
INDEX_FILE = SAMPLES_DIR / "index.json"
DOCS = (REPO / "README.md", REPO / "docs" / "commands.md")

SCHEMA = "fantasy-sports/v1"
"""Mirrors ``fantasy_sports.output.SCHEMA`` without importing the package at
module scope; ``test_readme_samples.py`` asserts the two agree."""

PUBLIC_LEAGUE = "ESPN public league 1234, season 2018"
SYNTHETIC_LEAGUE = "synthetic league 99, season 2026"

#: Placeholders, never real. ESPN serves the public league without reading them;
#: the chain only insists they exist. The SWID is a valid-shaped GUID because
#: the chain repairs a malformed one and would otherwise pass it through.
PLACEHOLDER_S2 = "placeholder-not-a-real-cookie"
PLACEHOLDER_SWID = "{00000000-0000-0000-0000-000000000000}"

PUBLIC_CONFIG = """\
default = "public"

[leagues.public]
provider  = "espn"
league_id = "1234"
season    = 2018
"""

SYNTHETIC_CONFIG = """\
default = "synthetic"

[leagues.synthetic]
provider  = "espn"
league_id = "99"
season    = 2026
"""

SAMPLE_START = re.compile(r"^<!-- sample: (?P<name>[A-Za-z0-9_.-]+) -->$", re.MULTILINE)
SAMPLE_END = "<!-- /sample -->"


# --------------------------------------------------------------------------- #
# What one sample is
# --------------------------------------------------------------------------- #


@dataclass
class Sample:
    """One captured invocation, as written to ``docs/samples/`` and the index."""

    name: str
    command: str
    """The invocation as a reader would type it."""
    source: str
    """``live``, ``replayed``, or ``synthetic``."""
    league: str
    exit: int
    stream: str
    """``stdout`` or ``stderr`` — which one the shown text came from."""
    format: str
    """Fence language for the shown text: ``json`` or ``text``."""
    text: str = field(repr=False)
    trimmed: bool = False
    note: str | None = None
    """One extra sentence for the header comment, when the sample needs it."""
    offline: bool = True
    """Whether the unit test can regenerate this sample without the network.

    Replayed and synthetic samples always can. A live sample can when the
    canary recording (``tests/cassettes/espn/canary_2018.yaml``) holds every
    request it makes; the few that depend on the machine, the network, or a
    request the canary never recorded say ``False`` and are held by the
    envelope tests only.
    """

    @property
    def file(self) -> str:
        return f"{self.name}.{'json' if self.format == 'json' else 'txt'}"

    def index_entry(self) -> dict[str, Any]:
        entry = asdict(self)
        entry.pop("text")
        entry["file"] = self.file
        return entry


# --------------------------------------------------------------------------- #
# Trimming — valid JSON with a visible ellipsis
# --------------------------------------------------------------------------- #

ELLIPSIS = "…"
PASSTHROUGH_KEYS = frozenset({"raw", "payload"})


def trim(
    value: Any,
    *,
    items: int = 2,
    inner_items: int = 8,
    raw_keys: int = 4,
    raw_depth: int = 1,
) -> Any:
    """A shorter ``value`` that is still valid JSON and still shows its shape.

    Every top-level envelope key survives untouched. ``data`` lists keep their
    first ``items`` entries, lists nested inside them keep ``inner_items``, and
    a provider passthrough (``raw``, or ``raw``'s ``payload``) keeps its first
    ``raw_keys`` keys with nested containers collapsed past ``raw_depth``
    levels. Whatever was cut is replaced by a string carrying ``…`` and a
    count, so a reader can see that something was omitted and how much.
    """
    if not isinstance(value, Mapping):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key == "data":
            out[key] = _trim_data(item, items, inner_items, raw_keys, raw_depth)
        elif key == "untrusted" and isinstance(item, Mapping):
            out[key] = _trim_mapping(item, items)
        elif key == "error" and isinstance(item, Mapping):
            out[key] = {
                k: (
                    _trim_passthrough(v, raw_keys, raw_depth)
                    if k == "details" and isinstance(v, Mapping) and len(v) > raw_keys
                    else v
                )
                for k, v in item.items()
            }
        else:
            out[key] = item
    return out


def _trim_data(value: Any, items: int, inner: int, raw_keys: int, raw_depth: int) -> Any:
    if isinstance(value, list):
        kept = [_trim_item(v, inner, raw_keys, raw_depth) for v in value[:items]]
        if len(value) > items:
            kept.append(_more(len(value) - items, "item"))
        return kept
    return _trim_item(value, inner, raw_keys, raw_depth)


def _trim_item(value: Any, inner: int, raw_keys: int, raw_depth: int) -> Any:
    if isinstance(value, Mapping):
        return {
            k: (
                _trim_passthrough(v, raw_keys, raw_depth)
                if k in PASSTHROUGH_KEYS
                else _trim_item(v, inner, raw_keys, raw_depth)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        kept = [_trim_item(v, inner, raw_keys, raw_depth) for v in value[:inner]]
        if len(value) > inner:
            kept.append(_more(len(value) - inner, "item"))
        return kept
    return value


def _trim_passthrough(value: Any, raw_keys: int, depth: int) -> Any:
    """Collapse a provider payload: a few keys, then a count of the rest."""
    if isinstance(value, Mapping):
        if depth <= 0:
            return _count(len(value), "key")
        keys = list(value)
        kept = {k: _trim_passthrough(value[k], raw_keys, depth - 1) for k in keys[:raw_keys]}
        if len(keys) > raw_keys:
            kept[ELLIPSIS] = _count(len(keys) - raw_keys, "more key")
        return kept
    if isinstance(value, list):
        if not value:
            return []
        if depth <= 0:
            return _count(len(value), "item")
        kept = [_trim_passthrough(v, raw_keys, depth - 1) for v in value[:raw_keys]]
        if len(value) > raw_keys:
            kept.append(_more(len(value) - raw_keys, "item"))
        return kept
    return value


def _count(n: int, noun: str) -> str:
    return f"{ELLIPSIS} {n} {noun}{'' if n == 1 else 's'}"


def _more(n: int, noun: str) -> str:
    return _count(n, f"more {noun}")


def _trim_mapping(value: Mapping[str, Any], items: int) -> dict[str, Any]:
    keys = list(value)
    kept = {k: value[k] for k in keys[:items]}
    if len(keys) > items:
        kept[ELLIPSIS] = _count(len(keys) - items, "more entry").replace("entrys", "entries")
    return kept


# --------------------------------------------------------------------------- #
# Splicing — the part the test re-runs
# --------------------------------------------------------------------------- #


def render_block(entry: Mapping[str, Any], text: str) -> str:
    """The Markdown that stands between one marker pair."""
    header = f"{entry['league']} ({entry['source']}). Exit {entry['exit']}, {entry['stream']}"
    if entry.get("trimmed"):
        header += f", trimmed with {ELLIPSIS}"
    if entry.get("note"):
        header += f". {entry['note']}"
    lines = [
        "```bash",
        entry["command"],
        f"# {header}:",
        "```",
        f"```{entry['format']}",
        text.rstrip("\n"),
        "```",
    ]
    return "\n".join(lines) + "\n"


def load_index(path: Path = INDEX_FILE) -> dict[str, dict[str, Any]]:
    entries = json.loads(path.read_text(encoding="utf-8"))
    return {entry["name"]: entry for entry in entries}


def splice(document: str, index: Mapping[str, Mapping[str, Any]], samples_dir: Path) -> str:
    """``document`` with every marker pair's body replaced by its sample."""
    out: list[str] = []
    pos = 0
    for match in SAMPLE_START.finditer(document):
        name = match.group("name")
        if name not in index:
            raise KeyError(f"no sample named {name!r} in {INDEX_FILE.name}")
        end = document.find(SAMPLE_END, match.end())
        if end < 0:
            raise ValueError(f"unterminated sample block {name!r}")
        entry = index[name]
        text = (samples_dir / entry["file"]).read_text(encoding="utf-8")
        out.append(document[pos : match.end()])
        out.append("\n" + render_block(entry, text))
        pos = end
    out.append(document[pos:])
    return "".join(out)


def referenced_names(document: str) -> set[str]:
    return {match.group("name") for match in SAMPLE_START.finditer(document)}


# --------------------------------------------------------------------------- #
# The sandbox — a throwaway XDG tree, and the environment that points at it
# --------------------------------------------------------------------------- #


class Sandbox:
    """A private ``HOME`` with config, cache, and data directories."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.config = root / "config"
        self.cache = root / "cache"
        self.data = root / "data"
        self.state = root / "state"
        for path in (self.config, self.cache, self.data, self.state):
            path.mkdir(parents=True, exist_ok=True)

    def write_config(self, text: str | None) -> None:
        target = self.config / "fantasy-sports" / "config.toml"
        target.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(text, encoding="utf-8")

    def env(self, *, credentials: bool = True) -> dict[str, str]:
        env = {
            "HOME": str(self.root),
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_CACHE_HOME": str(self.cache),
            "XDG_DATA_HOME": str(self.data),
            "XDG_STATE_HOME": str(self.state),
            # Keep the samples deterministic across regenerations: the sandbox
            # never has a Keychain, so the null backend is the honest one.
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        }
        if credentials:
            env["FANTASY_SPORTS_ESPN_S2"] = PLACEHOLDER_S2
            env["FANTASY_SPORTS_SWID"] = PLACEHOLDER_SWID
        return env

    def rewrite(self, text: str) -> str:
        """Paths under the sandbox, as they would read on a real machine."""
        for actual, shown in (
            (self.config, "~/.config"),
            (self.cache, "~/.cache"),
            (self.data, "~/.local/share"),
            (self.state, "~/.local/state"),
            (self.root, "~"),
        ):
            text = text.replace(str(actual), shown)
        return text


@contextlib.contextmanager
def environment(values: Mapping[str, str], *, drop: tuple[str, ...] = ()) -> Iterator[None]:
    """Temporarily replace environment variables, restoring every one after."""
    saved = {name: os.environ.get(name) for name in (*values, *drop)}
    try:
        for name in drop:
            os.environ.pop(name, None)
        os.environ.update(values)
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


CREDENTIAL_VARS = (
    "FANTASY_SPORTS_ESPN_S2",
    "FANTASY_SPORTS_SWID",
    "ESPN_S2",
    "ESPN_SWID",
    "SWID",
)


def capture(argv: list[str]) -> tuple[str, str, int]:
    """Run one CLI invocation in-process; return ``(stdout, stderr, exit)``.

    The same code path the console script takes after its fast path, with
    stdout redirected to a buffer — which is exactly what a pipe looks like to
    the format detector, so a command run here without ``--output`` emits
    JSON for the same reason it would under ``| jq``.
    """
    from fantasy_sports.cli.app import run

    out, err = io.StringIO(), io.StringIO()
    saved_argv, sys.argv = sys.argv, ["fantasy-sports", *argv]  # typer's usage line reads argv[0]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run(argv)
    finally:
        sys.argv = saved_argv
    return out.getvalue(), err.getvalue(), code


def console_script() -> Path:
    """The installed ``fantasy-sports`` entry point beside this interpreter."""
    candidate = Path(sys.executable).parent / "fantasy-sports"
    if not candidate.exists():
        raise SystemExit(f"{candidate} not found; run this through `uv run`.")
    return candidate


# --------------------------------------------------------------------------- #
# Building samples
# --------------------------------------------------------------------------- #


class Recorder:
    """Runs invocations inside a sandbox and collects :class:`Sample` objects."""

    def __init__(self, sandbox: Sandbox, *, offline_only: bool = False) -> None:
        self.sandbox = sandbox
        self.samples: list[Sample] = []
        self.offline_only = offline_only
        """Skip every sample that needs the network or the real console script.

        The unit test sets this and points ``requests.get`` at a recording, so
        the same functions that generate the docs regenerate them offline.
        """

    def run(
        self,
        name: str,
        argv: list[str],
        *,
        source: str,
        league: str,
        env: Mapping[str, str] | None = None,
        drop: tuple[str, ...] = (),
        expect: int | None = 0,
        note: str | None = None,
        stderr_ok: bool = False,
        offline: bool = True,
        **trim_options: Any,
    ) -> Sample | None:
        if self.offline_only and not offline:
            return None
        with environment(env if env is not None else self.sandbox.env(), drop=drop):
            out, err, code = capture(argv)
        if expect is not None and code != expect:
            raise SystemExit(
                f"{name}: expected exit {expect}, got {code}\nstdout: {out}\nstderr: {err}"
            )
        if code == 0:
            if err and not stderr_ok:
                raise SystemExit(f"{name}: a success wrote to stderr: {err!r}")
            stream, text = "stdout", out
        else:
            if out:
                raise SystemExit(f"{name}: a failure wrote to stdout: {out!r}")
            stream, text = "stderr", err
        return self.add(
            name,
            argv,
            source,
            league,
            code,
            stream,
            self.sandbox.rewrite(text),
            note,
            trim_options,
            offline=offline,
        )

    def add(
        self,
        name: str,
        argv: list[str],
        source: str,
        league: str,
        code: int,
        stream: str,
        text: str,
        note: str | None,
        trim_options: Mapping[str, Any],
        *,
        command: str | None = None,
        parse: bool = True,
        offline: bool = True,
    ) -> Sample:
        fmt = "text"
        trimmed = False
        if parse and text.lstrip().startswith("{"):
            payload = json.loads(text)
            shorter = trim(payload, **trim_options)
            trimmed = shorter != payload
            if trimmed:
                # Re-serialised so the ellipsis is readable; an untrimmed sample
                # is the CLI's own bytes, `\uXXXX` escapes and all.
                text = json.dumps(shorter, indent=2, ensure_ascii=False) + "\n"
            fmt = "json"
        sample = Sample(
            name=name,
            command=command or shlex.join(["fantasy-sports", *argv]),
            source=source,
            league=league,
            exit=code,
            stream=stream,
            format=fmt,
            text=text,
            trimmed=trimmed,
            note=note,
            offline=offline,
        )
        self.samples.append(sample)
        return sample


def live_samples(recorder: Recorder) -> None:
    """Every sample that reaches ESPN — public league only."""
    sandbox = recorder.sandbox
    sandbox.write_config(PUBLIC_CONFIG)
    live = {"source": "live", "league": PUBLIC_LEAGUE}

    # --help comes from the fast path, which `run()` bypasses; use the real
    # console script so the sample is the exact text a shell would print.
    if not recorder.offline_only:
        help_text = subprocess.run(
            [str(console_script()), "--help"],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, **sandbox.env()},
        ).stdout
        recorder.add(
            "help", ["--help"], "live", "no league", 0, "stdout", help_text, None, {}, offline=False
        )

    recorder.run("league-info", ["league", "info"], **live)
    recorder.run("league-info.table", ["league", "info", "--output", "table"], **live)
    recorder.run("teams", ["teams"], **live)
    recorder.run("teams.no-raw", ["teams", "--no-raw"], **live)
    recorder.run("standings", ["standings"], **live)
    recorder.run("standings.table", ["standings", "--output", "table"], **live)
    recorder.run("standings.csv", ["standings", "--output", "csv", "--no-raw"], **live)
    recorder.run("roster", ["roster", "--team", "1"], **live, raw_keys=3)
    recorder.run(
        "roster.week", ["roster", "--team", "FANTASY GOD", "--week", "1", "--no-raw"], **live
    )
    recorder.run("matchups", ["matchups", "--week", "1"], **live)
    recorder.run("transactions", ["transactions", "--limit", "5"], **live, raw_depth=2)
    recorder.run("raw", ["raw", "--view", "mSettings"], **live, raw_keys=8, raw_depth=2)
    # The canary recording has no kona_player_info interaction, so the two
    # player-pool samples, the 2019 probe below, and `doctor` (whose python and
    # dependency versions are the machine's, and whose version check reaches
    # the health manifest) cannot be regenerated offline.
    recorder.run(
        "raw.unfiltered", ["raw", "--view", "kona_player_info"], **live, raw_keys=3, offline=False
    )
    recorder.run(
        "raw.filter",
        [
            "raw",
            "--view",
            "kona_player_info",
            "--filter",
            '{"players":{"limit":2,"sortPercOwned":{"sortPriority":1,"sortAsc":false}}}',
        ],
        **live,
        raw_keys=3,
        raw_depth=2,
        offline=False,
    )
    recorder.run("doctor", ["doctor"], **live, raw_keys=4, offline=False)
    recorder.run("auth-status.env", ["auth", "status"], **live | {"league": "no league"})

    # Global options. `--league` names the profile explicitly; `--fresh` and
    # `--no-cache` are visible only in `sources[].cached`, so both keep one row.
    recorder.run("option-league", ["standings", "--league", "public", "--no-raw"], **live, items=1)
    recorder.run("option-fresh", ["standings", "--fresh", "--no-raw"], **live, items=1)
    recorder.run("option-no-cache", ["standings", "--no-cache", "--no-raw"], **live, items=1)
    recorder.run(
        "option-season",
        ["league", "info", "--season", "2019"],
        **live,
        expect=5,
        note="League 1234 exists only for 2018, so this is ESPN's real 404",
        offline=False,
    )

    # The pipe: no --output, stdout is a pipe, JSON comes out. `head` is the
    # downstream so the sample shows only the first lines.
    if not recorder.offline_only:
        _pipe_sample(recorder)

    # Provoked errors, all real.
    recorder.run(
        "error-not-available",
        ["box-scores", "--week", "1"],
        **live,
        expect=10,
        note="ESPN does not serve box scores before 2019",
    )
    recorder.run(
        "error-not-available.free-agents",
        ["free-agents", "--pos", "RB", "--limit", "5"],
        **live,
        expect=10,
    )
    recorder.run("error-league-not-found", ["standings", "--league", "nope"], **live, expect=5)
    recorder.run("error-league-not-found.team", ["roster", "--team", "Nobody"], **live, expect=5)
    recorder.run("error-league-not-found.ambiguous", ["roster", "--team", "Team"], **live, expect=5)
    recorder.run(
        "error-config-invalid.filter",
        ["raw", "--view", "mSettings", "--filter", "not-json"],
        **live,
        expect=6,
    )
    recorder.run(
        "error-auth-missing",
        ["standings"],
        **live,
        env=sandbox.env(credentials=False),
        drop=CREDENTIAL_VARS,
        expect=3,
        note="No cookie in the environment, the Keychain, or config.toml",
    )

    sandbox.write_config('default = "public"\n[leagues.public\nprovider = "espn"\n')
    recorder.run(
        "error-config-invalid",
        ["standings"],
        **live,
        expect=6,
        note="config.toml is missing a closing bracket",
    )
    sandbox.write_config(None)
    recorder.run(
        "error-no-config",
        ["standings"],
        **live | {"league": "no league"},
        expect=5,
        note="No config.toml at all",
    )
    sandbox.write_config(PUBLIC_CONFIG)

    # Usage errors are typer's own: prose on stderr, exit 2, no envelope.
    recorder.run("error-usage", ["standings", "--output", "yaml"], **live, expect=2)
    recorder.run("error-usage.missing", ["roster"], **live, expect=2)


def _pipe_sample(recorder: Recorder) -> None:
    """``fantasy-sports standings | head -4``: no ``--output``, stdout a pipe.

    The producer runs first with its stdout captured — which is a pipe, and is
    all the format detector looks at — and is refused unless it exited 0
    with nothing on stderr and an envelope on stdout. Only then is the
    captured payload handed to ``head -4`` for the four lines the docs show.
    Two steps rather than one live pipeline on purpose: with ``head`` reading
    directly, a payload larger than the pipe buffer ends the producer with
    EPIPE, which is indistinguishable by exit status from a real failure —
    and this is one of the six samples the offline regeneration cannot
    re-check, so a broken capture has to fail here, not reach the docs.
    """
    script = console_script()
    env = {**os.environ, **recorder.sandbox.env()}
    producer = subprocess.run(
        [str(script), "standings"], capture_output=True, text=True, env=env, check=False
    )
    if producer.returncode != 0 or producer.stderr:
        raise SystemExit(f"pipe: producer exited {producer.returncode}\nstderr: {producer.stderr}")
    if f'"schema": "{SCHEMA}"' not in producer.stdout:
        raise SystemExit(f"pipe: expected an envelope, got {producer.stdout[:200]!r}")
    head = subprocess.run(
        ["head", "-4"], input=producer.stdout, capture_output=True, text=True, check=True
    )
    recorder.add(
        "pipe",
        ["standings"],
        "live",
        PUBLIC_LEAGUE,
        producer.returncode,
        "stdout",
        head.stdout,
        "No --output given; stdout is a pipe, so the renderer chose JSON",
        {},
        command="fantasy-sports standings | head -4",
        parse=False,
        offline=False,
    )


def replayed_samples(recorder: Recorder) -> None:
    """``box-scores``, ``free-agents``, and the ``auth`` commands, offline."""
    sys.path.insert(0, str(REPO / "tests"))
    import requests
    from _harness import RecordedEspn  # the stub the unit tests replay through

    sandbox = recorder.sandbox
    sandbox.write_config(SYNTHETIC_CONFIG)
    replayed = {"source": "replayed", "league": SYNTHETIC_LEAGUE}

    original_get = requests.get
    requests.get = RecordedEspn()
    try:
        recorder.run(
            "box-scores", ["box-scores", "--week", "1"], **replayed, items=1, inner_items=3
        )
        recorder.run(
            "free-agents",
            ["free-agents", "--pos", "WR", "--limit", "5", "--week", "2"],
            **replayed,
        )
        recorder.run(
            "error-config-invalid.pos",
            ["free-agents", "--pos", "PUNTER", "--week", "2"],
            **replayed,
            expect=6,
        )
    finally:
        requests.get = original_get

    _auth_samples(recorder)
    _journal_samples(recorder)
    sandbox.write_config(PUBLIC_CONFIG)


JOURNAL_SALT = "fantasy-sports/sample-journal/v1"
"""A fixed pseudonym salt, so the seeded journal renders byte-identically each
time. A real journal mints a random one (``core/journal.py``)."""

JOURNAL_MEMBER = "{7E57ED00-1111-4222-8333-444455556666}"
"""An invented member GUID, echoed back as ESPN's response would carry it."""

JOURNAL_SWAP_ID = "0f8a9c2e-6b1d-4e57-9a3c-2d7b5e1f4a60"
JOURNAL_CLAIM_ID = "c47d19ab-52e0-4f3c-8a16-9e0b3d7a2c55"


def _journal_samples(recorder: Recorder) -> None:
    """``journal list`` and ``journal show`` against a seeded, invented journal.

    Nothing writes to the journal yet (the write commands land after the store),
    so these entries are seeded through the store's own API: one lineup swap
    that ESPN accepted, in the request shape ``docs/research/05-espn-write-
    surface.md`` captured, and one claim that was begun and never closed, which
    is how a crash between the intent and the outcome looks. Ids and timestamps
    are fixed so the sample can be regenerated byte-for-byte.
    """
    from datetime import UTC, datetime

    from fantasy_sports.core.journal import Journal, JournalEntry

    sandbox = recorder.sandbox
    path = sandbox.state / "fantasy-sports" / "journal.sqlite"
    path.unlink(missing_ok=True)
    common = {"provider": "espn", "league_id": "99", "season": 2026, "scoring_period": 1}
    with environment(sandbox.env()), Journal.open(swid_salt=JOURNAL_SALT) as journal:
        swap = journal.begin(
            JournalEntry(
                id=JOURNAL_SWAP_ID,
                recorded_at=datetime(2026, 9, 12, 18, 29, 24, 880000, tzinfo=UTC),
                command=["fantasy-sports", "lineup", "set", "--team", "1"],
                role="creator",
                team_id="1",
                kind="lineup",
                reversible=True,
                prior_state={"9000013": 4, "9000012": 20},
                request={
                    "teamId": 1,
                    "type": "ROSTER",
                    "scoringPeriodId": 1,
                    "executionType": "EXECUTE",
                    "items": [
                        {
                            "playerId": 9000013,
                            "type": "LINEUP",
                            "fromLineupSlotId": 4,
                            "toLineupSlotId": 20,
                        },
                        {
                            "playerId": 9000012,
                            "type": "LINEUP",
                            "fromLineupSlotId": 20,
                            "toLineupSlotId": 4,
                        },
                    ],
                },
                **common,
            )
        )
        journal.complete(
            swap.id,
            member_id=JOURNAL_MEMBER,
            provider_transaction_id="3b1f6c52-8d0e-4a47-9b6a-5c2e7d9f0a11",
            proposed_at=datetime.fromtimestamp(1789237765019 / 1000, tz=UTC),
            applied_state={"9000013": 20, "9000012": 4},
        )
        journal.begin(
            JournalEntry(
                id=JOURNAL_CLAIM_ID,
                recorded_at=datetime(2026, 9, 12, 18, 33, 40, 215000, tzinfo=UTC),
                command=["fantasy-sports", "claim", "--team", "1", "--bid", "7"],
                role="creator",
                team_id="1",
                kind="claim",
                reversible=False,
                prior_state={"9000012": 4},
                request={"teamId": 1, "type": "WAIVER", "bidAmount": 7},
                cost={"bid_amount": 7, "displaced_player_id": "9000012"},
                **common,
            )
        )
    replayed = {"source": "replayed", "league": SYNTHETIC_LEAGUE}
    note = "A seeded journal: the write commands that fill it land after the store"
    recorder.run("journal-list", ["journal", "list"], **replayed, note=note)
    recorder.run(
        "journal-show", ["journal", "show", "--id", JOURNAL_SWAP_ID], **replayed, note=note
    )
    path.unlink()


def _auth_samples(recorder: Recorder) -> None:
    """``auth login`` / ``status`` / ``logout`` against an in-memory Keychain.

    The prompt is answered by this function rather than a person, with
    placeholder values — the SWID deliberately without its braces so the
    ``repaired`` field has something to say. Nothing reaches the real
    Keychain: :class:`MemoryKeyring` is installed for the duration and every
    credential it held dies with it.
    """
    import getpass

    import keyring

    answers = iter([PLACEHOLDER_S2, PLACEHOLDER_SWID.strip("{}")])
    original_getpass, original_keyring = getpass.getpass, keyring.get_keyring()
    getpass.getpass = lambda prompt="": next(answers)  # type: ignore[assignment]
    keyring.set_keyring(MemoryKeyring())
    env = recorder.sandbox.env(credentials=False)
    env.pop("PYTHON_KEYRING_BACKEND")
    replayed = {"source": "replayed", "league": "no league", "env": env, "drop": CREDENTIAL_VARS}
    try:
        recorder.run("auth-status.missing", ["auth", "status"], **replayed)
        recorder.run(
            "auth-login",
            ["auth", "login"],
            **replayed,
            note="Prompts answered with placeholder cookies, the SWID pasted without braces; "
            "the prompt guidance went to stderr",
            stderr_ok=True,
        )
        recorder.run("auth-status", ["auth", "status"], **replayed)
        recorder.run("auth-logout", ["auth", "logout"], **replayed)
    finally:
        getpass.getpass = original_getpass  # type: ignore[assignment]
        keyring.set_keyring(original_keyring)


def _memory_keyring_class() -> type:
    import keyring.backend
    import keyring.errors

    class MemoryKeyring(keyring.backend.KeyringBackend):
        """A Keychain that forgets everything when the process exits."""

        priority = 1  # type: ignore[assignment]

        def __init__(self) -> None:
            super().__init__()
            self._store: dict[tuple[str, str], str] = {}

        def get_password(self, service: str, username: str) -> str | None:
            return self._store.get((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            self._store[(service, username)] = password

        def delete_password(self, service: str, username: str) -> None:
            if (service, username) not in self._store:
                raise keyring.errors.PasswordDeleteError(username)
            del self._store[(service, username)]

    return MemoryKeyring


def MemoryKeyring() -> Any:  # noqa: N802 - reads as the class it builds
    return _memory_keyring_class()()


def synthetic_samples(recorder: Recorder) -> None:
    """The four codes that need a failure nobody can safely provoke.

    Each is the exception the ESPN adapter raises for that case, with the
    message and details it uses, rendered by the output layer exactly as a
    real one would be — only the trigger is invented.
    """
    from fantasy_sports.core.errors import (
        AuthExpiredError,
        ProviderUnavailableError,
        RateLimitedError,
        SchemaDriftError,
    )
    from fantasy_sports.output import exit_code_for
    from fantasy_sports.output.envelope import Envelope
    from fantasy_sports.output.json import render

    cases = [
        (
            "error-auth-expired",
            ["standings"],
            AuthExpiredError(
                "ESPN rejected the configured credentials.",
                remediation=(
                    "Re-extract espn_s2 and SWID from a logged-in browser session and "
                    "run `fantasy-sports auth login`."
                ),
                details={"view": "mTeam", "status": 401},
            ),
            "No observed ESPN response proves a cookie is dead, so this code has never been "
            "emitted; a rejected cookie surfaces as LEAGUE_NOT_FOUND today",
        ),
        (
            "error-provider-unavailable",
            ["standings"],
            ProviderUnavailableError(
                "ESPN request failed during fetch_league: HTTPSConnectionPool(host="
                "'lm-api-reads.fantasy.espn.com', port=443): Read timed out. (read timeout=30)",
                details={"view": "mTeam", "cause": "ReadTimeout"},
            ),
            None,
        ),
        (
            "error-rate-limited",
            ["standings"],
            RateLimitedError(
                "ESPN throttled this request (HTTP 429).",
                retry_after=30.0,
                remediation="Wait for `details.retry_after` seconds, then retry.",
                details={"status": 429, "view": "mTeam"},
            ),
            None,
        ),
        (
            "error-schema-drift",
            ["standings"],
            SchemaDriftError(
                "ESPN's mTeam response no longer has the shape espn-api reads "
                "(missing key 'teams', in League._fetch_teams).",
                path=["mTeam", "League._fetch_teams", "teams"],
                provider="espn",
                remediation=(
                    "File an issue with this error payload; the provider's response shape changed."
                ),
                details={"view": "mTeam", "operation": "fetch_standings", "cause": "KeyError"},
            ),
            None,
        ),
    ]
    for name, argv, error, note in cases:
        envelope = Envelope.failure(error, provider="espn", league_id="1234", season=2018)
        recorder.add(
            name,
            argv,
            "synthetic",
            "no league contacted",
            exit_code_for(error),
            "stderr",
            render(envelope),
            note,
            {},
        )


# --------------------------------------------------------------------------- #
# Writing and checking
# --------------------------------------------------------------------------- #


def write_samples(samples: list[Sample]) -> None:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    names = {sample.name for sample in samples}
    if len(names) != len(samples):
        raise SystemExit("duplicate sample names")
    for stale in SAMPLES_DIR.iterdir():
        if stale.is_file():
            stale.unlink()
    for sample in samples:
        (SAMPLES_DIR / sample.file).write_text(sample.text, encoding="utf-8")
    INDEX_FILE.write_text(
        json.dumps([s.index_entry() for s in samples], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def splice_docs(*, check: bool) -> int:
    """Splice every doc from the committed samples; in check mode, report drift."""
    index = load_index()
    drifted: list[Path] = []
    unused = set(index)
    for doc in DOCS:
        before = doc.read_text(encoding="utf-8")
        after = splice(before, index, SAMPLES_DIR)
        unused -= referenced_names(before)
        if after != before:
            if check:
                drifted.append(doc)
            else:
                doc.write_text(after, encoding="utf-8")
    if unused:
        print(f"warning: samples referenced by no doc: {', '.join(sorted(unused))}")
    if drifted:
        for doc in drifted:
            print(f"{doc.relative_to(REPO)} is out of date; run scripts/render_readme_samples.py")
        return 1
    return 0


def generate() -> None:
    with tempfile.TemporaryDirectory(prefix="fantasy-sports-samples-") as tmp:
        recorder = Recorder(Sandbox(Path(tmp)))
        live_samples(recorder)
        replayed_samples(recorder)
        synthetic_samples(recorder)
        write_samples(recorder.samples)
    print(f"wrote {len(recorder.samples)} samples to {SAMPLES_DIR.relative_to(REPO)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--splice", action="store_true", help="splice the committed samples only")
    mode.add_argument("--check", action="store_true", help="fail if the docs are out of date")
    args = parser.parse_args(argv)
    if not (args.splice or args.check):
        generate()
    return splice_docs(check=args.check)


if __name__ == "__main__":
    sys.exit(main())
