"""The offline harness the command and CLI tests share.

It lives beside ``conftest.py`` rather than inside it because the cassette
scrub hooks in that file are a *security control* with its own owner
(jwulff/fantasy-sports#12), and this is ordinary test scaffolding. Both
``tests/unit/test_commands.py`` and ``tests/integration/test_cli.py`` import
from here; ``tests/`` is on ``sys.path`` for the same reason ``from conftest
import ...`` works.

**Why a stub on ``requests.get`` rather than vcrpy.** ``transactions`` walks
scoring periods backward and asks ``mTransactions2`` about periods the fixture
never recorded. A cassette can only answer "miss", but the honest upstream
answer is a **200 with no transactions in it** — a quiet week is a successful
empty result, not a failure
(``docs/memory/espn-api-is-a-shape-reader-not-a-client.md``). The stub
synthesises exactly that, refuses everything else, and counts requests, which
is the only way the upstream-call cap can be asserted at all.

``pytest-socket`` is on, so anything this stub does not answer fails loudly
rather than quietly reaching ESPN.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

CASSETTE = "espn/synthetic_2026.yaml"
LEAGUE_ID = "99"
SEASON = 2026

#: Synthetic, and shaped like the real thing so the normalizer and the
#: credential scrubber behave as they would in production. Neither value is a
#: real credential, and neither may ever be replaced by one.
FAKE_S2 = "AEB" + "q" * 200
FAKE_SWID = "{1A2B3C4D-5E6F-7A8B-9C0D-1E2F3A4B5C6D}"

CONFIG_TOML = (
    'default = "synthetic"\n'
    "\n"
    "[leagues.synthetic]\n"
    'provider = "espn"\n'
    f'league_id = "{LEAGUE_ID}"\n'
    f"season = {SEASON}\n"
    "\n"
    "[leagues.broken]\n"
    'provider = "nonesuch"\n'
    'league_id = "1"\n'
    "season = 2026\n"
)


class FakeResponse:
    """The three attributes the adapter's transport reads off a response."""

    def __init__(self, body: Any, status_code: int = 200) -> None:
        self.status_code = status_code
        self._body = body
        self.headers: Mapping[str, str] = {}

    @property
    def content(self) -> bytes:
        if isinstance(self._body, bytes):
            return self._body
        if isinstance(self._body, str):
            return self._body.encode()
        return json.dumps(self._body).encode()

    def json(self) -> Any:
        return json.loads(self.content)


def query_key(url: str, extra: list[tuple[str, str]] | None = None) -> tuple[Any, ...]:
    """A request's identity: its path and its query pairs, order-insensitive."""
    parts = urlsplit(url)
    pairs = list(parse_qsl(parts.query)) + list(extra or [])
    return (parts.path, tuple(sorted(pairs)))


class RecordedEspn:
    """Serves a committed cassette, and answers a quiet scoring period honestly.

    ``overrides`` substitutes one view's payload for every request of it,
    whatever the scoring period. A *payload* variation on an otherwise healthy
    league -- a view ESPN shipped without a key it usually carries -- belongs
    next to the assertion that needs it, not in a second cassette to keep in
    step with the first.
    """

    def __init__(self, name: str = CASSETTE, overrides: Mapping[str, Any] | None = None) -> None:
        import yaml
        from conftest import CASSETTE_LIBRARY_DIR

        document = yaml.safe_load(Path(CASSETTE_LIBRARY_DIR / name).read_text())
        self.bodies: dict[tuple[Any, ...], str] = {}
        self.by_view: dict[str, str] = {}
        self.overrides = dict(overrides or {})
        for interaction in document["interactions"]:
            uri = interaction["request"]["uri"]
            body = interaction["response"]["body"]["string"]
            self.bodies[query_key(uri)] = body
            views = [value for key, value in parse_qsl(urlsplit(uri).query) if key == "view"]
            if len(views) == 1:
                self.by_view.setdefault(views[0], body)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url, params=None, headers=None, cookies=None) -> FakeResponse:
        pairs: list[tuple[str, str]] = []
        for name, value in (params or {}).items():
            values = value if isinstance(value, list | tuple) else [value]
            pairs.extend((str(name), str(item)) for item in values)
        flat = dict(pairs)
        self.calls.append({"url": url, "params": flat, "headers": dict(headers or {})})

        if flat.get("view") in self.overrides:
            return FakeResponse(self.overrides[flat["view"]])
        body = self.bodies.get(query_key(url, pairs))
        if body is None and len([v for k, v in pairs if k == "view"]) == 1:
            # `raw` asks for one view with no scoring period; the fixture
            # recorded some of the same views with one. Same payload.
            body = self.by_view.get(flat.get("view", ""))
        if body is not None:
            return FakeResponse(body)
        if flat.get("view") == "mTransactions2":
            # A period nobody traded in. `espn-api` turns the absent key into a
            # bare `Exception('No transactions found')`, which the adapter
            # reads as the successful empty result it is.
            return FakeResponse({"id": int(LEAGUE_ID), "seasonId": SEASON})
        raise AssertionError(f"no recorded response for {url} {flat}")

    def views(self) -> list[str]:
        """Every ``view=`` requested, in order, with its scoring period."""
        found = []
        for call in self.calls:
            view = call["params"].get("view", "?")
            period = call["params"].get("scoringPeriodId")
            found.append(view if period is None else f"{view}@{period}")
        return found


def install_espn(
    monkeypatch: Any, name: str = CASSETTE, overrides: Mapping[str, Any] | None = None
) -> RecordedEspn:
    """Route the adapter's transport at the recorded fixture."""
    import requests

    http = RecordedEspn(name, overrides)
    monkeypatch.setattr(requests, "get", http)
    return http


def isolate_home(tmp_path: Path, monkeypatch: Any) -> Path:
    """A private XDG tree, a synthetic credential pair, and a config file.

    The environment variables matter twice over. They are the first link of the
    credential chain, so the Keychain — which on a developer's machine holds
    John's **real** ESPN cookies under this exact service name — is never
    reached by a test. And clearing the XDG variables is what stops a
    developer's own ``config.toml`` and response cache from leaking into the
    run (``docs/memory/config-toml-is-a-shared-namespace.md``).
    """
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("FANTASY_SPORTS_ESPN_S2", FAKE_S2)
    monkeypatch.setenv("FANTASY_SPORTS_SWID", FAKE_SWID)

    config = tmp_path / "config" / "fantasy-sports"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.toml").write_text(CONFIG_TOML, encoding="utf-8")
    return tmp_path
