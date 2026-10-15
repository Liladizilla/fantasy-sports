"""The registry is the single source of truth for what commands exist."""

from __future__ import annotations

import inspect

import pytest

from fantasy_sports.commands import (
    COMMON_PARAMS,
    NO_RAW_PARAM,
    OUTPUT_PARAM,
    REGISTRY,
    CommandSpec,
    DataShape,
    Param,
    groups,
    in_group,
    register,
    top_level,
)


@pytest.fixture
def clean_registry():
    saved = dict(REGISTRY)
    REGISTRY.clear()
    yield REGISTRY
    REGISTRY.clear()
    REGISTRY.update(saved)


#: The v0.1 command surface, exactly as ARCHITECTURE §12 lists it, plus
#: `doctor` (jwulff/fantasy-sports#10, ADR-0005 §11.4) and `auth logout`
#: (jwulff/fantasy-sports#62), and the first two writes: `lineup set` (#15) and
#: `journal reverse` (#16).
V01_SURFACE = frozenset(
    {
        "auth login",
        "auth logout",
        "auth status",
        "box-scores",
        "doctor",
        "free-agents",
        "journal digest",
        "journal list",
        "journal reverse",
        "journal show",
        "league info",
        "lineup set",
        "matchups",
        "raw",
        "roster",
        "standings",
        "teams",
        "transactions",
    }
)


def test_the_v01_surface_is_registered_and_nothing_else_is():
    """ARCHITECTURE §12, as a set. A new command here is a scope change.

    `box-scores` is one, made deliberately for jwulff/fantasy-sports#29: the
    Gazette needs per-player lines and folding them into `matchups` would have
    changed that command's data shape, which is a runtime contract. `doctor`
    is another, added for jwulff/fantasy-sports#10. `auth logout` is the
    third: `SECURITY.md` could not describe a leak remediation without it
    (jwulff/fantasy-sports#62). `journal list` and `journal show` are the
    fourth and fifth, the read side of the mutation journal
    (jwulff/fantasy-sports#16). `lineup set` and `journal reverse` are the
    first writes, from the write-surface plan (jwulff/fantasy-sports#105):
    the reversible half, ahead of `add`/`drop`/`claim` (#18).
    """
    assert set(REGISTRY) == V01_SURFACE


def test_every_command_declares_a_shape_and_a_summary():
    for spec in REGISTRY.values():
        assert isinstance(spec.shape, DataShape), spec.invocation
        assert spec.summary.endswith("."), f"{spec.invocation}: help is a sentence"
        assert len(spec.summary) <= 95, f"{spec.invocation}: help must fit a terminal"


def test_declared_parameters_match_the_handler_they_name():
    """The projections are built from `params`, so a drift there is invisible.

    Nothing checks a handler's real signature at import time -- resolving one
    is deliberately lazy -- so this is the test that keeps the declaration and
    the implementation from disagreeing until an agent invokes the command.
    """
    for spec in REGISTRY.values():
        signature = inspect.signature(spec.resolve())
        accepted = {
            name
            for name, param in signature.parameters.items()
            if param.kind is inspect.Parameter.KEYWORD_ONLY
        }
        declared = {param.name for param in spec.handler_params}
        assert declared == accepted, spec.invocation


def test_common_options_reach_every_league_reading_command():
    for spec in REGISTRY.values():
        names = {param.name for param in spec.handler_params}
        common = {param.name for param in COMMON_PARAMS}
        assert (common <= names) is spec.takes_league, spec.invocation
        assert "output" not in names, f"{spec.invocation}: rendering is not a handler concern"
        assert "no_raw" not in names, f"{spec.invocation}: raw-stripping is not a handler concern"


def test_the_cli_adds_output_to_every_command():
    for spec in REGISTRY.values():
        assert spec.cli_params[-2] is OUTPUT_PARAM


def test_the_cli_adds_no_raw_to_every_command():
    for spec in REGISTRY.values():
        assert spec.cli_params[-1] is NO_RAW_PARAM


def test_a_flag_defaults_to_its_dashed_name():
    assert Param(name="no_cache", help="h", annotation=bool).cli_flags == ("--no-cache",)
    assert Param(name="week", help="h", annotation=int | None).metavar == "INTEGER"
    assert Param(name="fresh", help="h", annotation=bool).metavar is None
    assert Param(name="fresh", help="h", annotation=bool).display == "--fresh"


def test_register_keys_by_full_invocation(clean_registry):
    register(
        CommandSpec(name="status", summary="Report credential age.", handler="m:f", group="auth")
    )
    assert "auth status" in clean_registry
    assert groups() == ["auth"]
    assert [s.name for s in in_group("auth")] == ["status"]
    assert top_level() == []


def test_top_level_commands_have_no_group(clean_registry):
    register(CommandSpec(name="standings", summary="Show standings.", handler="m:f"))
    assert [s.name for s in top_level()] == ["standings"]
    assert groups() == []


def test_duplicate_registration_is_rejected(clean_registry):
    spec = CommandSpec(name="teams", summary="List teams.", handler="m:f")
    register(spec)
    with pytest.raises(ValueError, match="already registered"):
        register(spec)


def test_handler_resolves_lazily():
    spec = CommandSpec(name="x", summary="s", handler="fantasy_sports.commands:groups")
    assert spec.resolve() is groups


def test_a_malformed_handler_is_rejected_at_resolve_time():
    with pytest.raises(ValueError, match="module:function"):
        CommandSpec(name="x", summary="s", handler="no_colon_here").resolve()
