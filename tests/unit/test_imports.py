"""The import budget (ADR-0008), enforced.

``--help`` and ``--version`` must not pay for an HTTP stack, a CLI framework, or
a keyring. These assertions are the reason ``cli/fastpath.py`` exists.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

EXPENSIVE = ("espn_api", "requests", "keyring", "typer", "click", "rich")


def _modules_after(code: str) -> set[str]:
    roots = "{m.split('.')[0] for m in sys.modules}"
    probe = f"{code}\nimport sys, json; print(json.dumps(sorted({roots})))"
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    import json

    return set(json.loads(proc.stdout.strip().splitlines()[-1]))


def test_importing_the_package_is_cheap():
    assert not (_modules_after("import fantasy_sports") & set(EXPENSIVE))


def test_importing_the_entry_point_is_cheap():
    assert not (_modules_after("import fantasy_sports.cli.fastpath") & set(EXPENSIVE))


def test_registry_is_typer_free():
    """ADR-0003: commands are plain functions; the CLI is a projection."""
    assert not (_modules_after("import fantasy_sports.commands") & set(EXPENSIVE))


def test_resolving_every_command_stays_free_of_the_cli_and_the_provider():
    """ADR-0003: a command is a plain function, importable without a CLI.

    Resolving all ten handlers must not drag in typer, rich, `espn_api`,
    `requests`, or `keyring`. That is the executable form of "the MCP server is
    a projection": whatever imports a command gets the command, and nothing
    else.
    """
    code = (
        "from fantasy_sports.commands import REGISTRY\n"
        "[spec.resolve() for spec in REGISTRY.values()]"
    )
    assert not (_modules_after(code) & set(EXPENSIVE))


def test_rendering_help_stays_cheap():
    code = "from fantasy_sports.cli.fastpath import render_help; render_help()"
    assert not (_modules_after(code) & set(EXPENSIVE))


def test_version_flag_does_not_import_typer():
    code = "from fantasy_sports.cli.fastpath import handle; handle(['--version'])"
    assert not (_modules_after(code) & set(EXPENSIVE))


def test_nothing_on_the_cli_path_imports_the_reports_layer():
    """ARCHITECTURE §4: ``core/`` and ``providers/`` never learn about ``reports/``.

    The notify hook lives there, and ``doctor`` reads its config from
    ``config/`` instead, so neither the entry point nor resolving every
    command pulls it in.
    """
    for code in (
        "import fantasy_sports.cli.fastpath",
        "import fantasy_sports.cli.app",
        "from fantasy_sports.commands import REGISTRY\n[s.resolve() for s in REGISTRY.values()]",
    ):
        assert "reports" not in {m.split(".")[1] for m in _submodules_after(code)}, code


def _submodules_after(code: str) -> set[str]:
    probe = f"{code}\nimport sys, json; print(json.dumps(sorted(sys.modules)))"
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    import json

    modules = json.loads(proc.stdout.strip().splitlines()[-1])
    return {m for m in modules if m.startswith("fantasy_sports.")}


def test_no_core_or_provider_module_mentions_the_reports_layer():
    root = Path(__file__).resolve().parents[2] / "src" / "fantasy_sports"
    offenders = [
        str(path.relative_to(root))
        for layer in ("core", "providers")
        for path in (root / layer).rglob("*.py")
        if "fantasy_sports.reports" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
