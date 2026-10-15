"""Config paths and multi-league resolution (jwulff/fantasy-sports#7).

Nothing here may read the developer's real ``~/.config``. Every test either
points ``XDG_CONFIG_HOME`` at ``tmp_path`` or points ``HOME`` there, so the
suite behaves identically on John's Mac and on a CI runner with no home
directory worth speaking of.
"""

from __future__ import annotations

import ast
import json
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from fantasy_sports.config import credentials, leagues, paths
from fantasy_sports.config.leagues import LeagueProfile
from fantasy_sports.core.errors import ConfigInvalidError, ErrorCode, LeagueNotFoundError

XDG_VARS = ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")

TWO_LEAGUES = """\
default = "dynasty"

[leagues.dynasty]
provider  = "espn"
league_id = "123456"
season    = 2026
sport     = "football"

[leagues.redraft]
provider  = "espn"
league_id = "789012"
season    = 2026
sport     = "football"
"""


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No test may see the real home directory or the real XDG environment."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in XDG_VARS:
        monkeypatch.delenv(var, raising=False)
    return home


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    """Point ``XDG_CONFIG_HOME`` at ``tmp_path`` and write ``text`` as the config."""
    xdg = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    path = xdg / "fantasy-sports" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A two-profile ``config.toml`` reachable through ``XDG_CONFIG_HOME``."""
    return _write_config(tmp_path, monkeypatch, TWO_LEAGUES)


# --------------------------------------------------------------------------
# paths — XDG forced on every platform (KTD6)
# --------------------------------------------------------------------------


def test_config_dir_is_xdg_style_not_library_on_macos(isolated_home: Path):
    """ARCHITECTURE §7: ``~/.config``, never ``~/Library/Application Support``."""
    resolved = paths.config_home()
    assert resolved == isolated_home / ".config" / "fantasy-sports"
    assert "Library" not in str(resolved)


def test_cache_and_data_dirs_are_xdg_style(isolated_home: Path):
    assert paths.cache_home() == isolated_home / ".cache" / "fantasy-sports"
    assert paths.data_home() == isolated_home / ".local" / "share" / "fantasy-sports"
    assert paths.state_home() == isolated_home / ".local" / "state" / "fantasy-sports"


def test_config_file_lives_under_the_config_dir(isolated_home: Path):
    assert paths.config_file() == paths.config_home() / "config.toml"


@pytest.mark.parametrize(
    ("var", "func_name"),
    [
        ("XDG_CONFIG_HOME", "config_home"),
        ("XDG_CACHE_HOME", "cache_home"),
        ("XDG_DATA_HOME", "data_home"),
        ("XDG_STATE_HOME", "state_home"),
    ],
)
def test_xdg_env_var_overrides_when_set(
    var: str, func_name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv(var, str(tmp_path / "elsewhere"))
    assert getattr(paths, func_name)() == tmp_path / "elsewhere" / "fantasy-sports"


def test_empty_xdg_var_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path
):
    """The XDG spec treats unset and empty identically."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert paths.config_home() == isolated_home / ".config" / "fantasy-sports"


def test_relative_xdg_var_is_ignored(monkeypatch: pytest.MonkeyPatch, isolated_home: Path):
    """The XDG spec: a relative path in one of these variables is invalid."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/not-absolute")
    assert paths.config_home() == isolated_home / ".config" / "fantasy-sports"


def test_xdg_var_expands_a_tilde(monkeypatch: pytest.MonkeyPatch, isolated_home: Path):
    monkeypatch.setenv("XDG_CONFIG_HOME", "~/somewhere")
    assert paths.config_home() == isolated_home / "somewhere" / "fantasy-sports"


# --------------------------------------------------------------------------
# loading and enumeration
# --------------------------------------------------------------------------


def test_load_reads_every_configured_profile(config_file: Path):
    config = leagues.load()
    assert config.default == "dynasty"
    assert [p.name for p in config.profiles()] == ["dynasty", "redraft"]
    assert config.profiles()[0] == LeagueProfile(
        name="dynasty", provider="espn", league_id="123456", season=2026, sport="football"
    )


def test_list_leagues_enumerates_without_mutating_config(config_file: Path):
    before = config_file.read_bytes()
    before_mtime = config_file.stat().st_mtime_ns

    profiles = leagues.list_leagues()
    assert [p.name for p in profiles] == ["dynasty", "redraft"]

    profiles.append(
        LeagueProfile(name="ghost", provider="espn", league_id="0", season=2026, sport="football")
    )
    assert [p.name for p in leagues.list_leagues()] == ["dynasty", "redraft"]

    assert config_file.read_bytes() == before
    assert config_file.stat().st_mtime_ns == before_mtime


def test_profiles_returns_a_fresh_list_each_call(config_file: Path):
    config = leagues.load()
    config.profiles().clear()
    assert len(config.profiles()) == 2


def test_enumeration_does_not_create_a_config_file(isolated_home: Path):
    assert leagues.list_leagues() == []
    assert not paths.config_home().exists()


def test_missing_config_file_loads_as_empty(isolated_home: Path):
    config = leagues.load()
    assert config.profiles() == []
    assert config.default is None
    assert config.path == paths.config_file()


def test_load_accepts_an_explicit_path(tmp_path: Path):
    path = tmp_path / "elsewhere.toml"
    path.write_text(TWO_LEAGUES)
    assert [p.name for p in leagues.load(path).profiles()] == ["dynasty", "redraft"]


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------


def test_named_league_resolves(config_file: Path):
    assert leagues.resolve_league("redraft").league_id == "789012"


def test_two_profiles_are_each_targetable_without_editing_config(config_file: Path):
    """The plan's verification for U3."""
    before = config_file.read_bytes()
    assert leagues.resolve_league("dynasty").league_id == "123456"
    assert leagues.resolve_league("redraft").league_id == "789012"
    assert config_file.read_bytes() == before


def test_default_league_resolves_when_no_name_is_given(config_file: Path):
    assert leagues.resolve_league().name == "dynasty"


def test_unknown_league_raises_league_not_found(config_file: Path):
    with pytest.raises(LeagueNotFoundError) as excinfo:
        leagues.resolve_league("keeper")
    assert excinfo.value.code == "LEAGUE_NOT_FOUND"
    assert "keeper" in str(excinfo.value)
    assert "dynasty" in str(excinfo.value), "the message should name what is available"


def test_resolving_with_no_config_file_raises_league_not_found(isolated_home: Path):
    with pytest.raises(LeagueNotFoundError) as excinfo:
        leagues.resolve_league("dynasty")
    assert excinfo.value.code == "LEAGUE_NOT_FOUND"
    assert str(paths.config_file()) in str(excinfo.value)


def test_no_name_and_no_default_with_several_leagues_raises_league_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_config(tmp_path, monkeypatch, TWO_LEAGUES.replace('default = "dynasty"\n', ""))
    with pytest.raises(LeagueNotFoundError) as excinfo:
        leagues.resolve_league()
    assert "default" in str(excinfo.value)


def test_a_lone_league_is_its_own_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_config(
        tmp_path,
        monkeypatch,
        '[leagues.only]\nprovider = "espn"\nleague_id = "1"\nseason = 2026\n',
    )
    assert leagues.resolve_league().name == "only"


def test_a_dangling_default_raises_league_not_found_only_when_it_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A typo in ``default`` must not block an explicitly named league."""
    _write_config(tmp_path, monkeypatch, TWO_LEAGUES.replace('"dynasty"', '"dynsaty"', 1))
    assert leagues.resolve_league("redraft").league_id == "789012"
    with pytest.raises(LeagueNotFoundError) as excinfo:
        leagues.resolve_league()
    assert "dynsaty" in str(excinfo.value)


def test_season_override_applies_to_one_invocation_only(config_file: Path):
    """``--season`` for historical queries; the stored profile is untouched."""
    historical = leagues.resolve_league("dynasty", season=2019)
    assert historical.season == 2019
    assert historical.league_id == "123456"

    assert leagues.resolve_league("dynasty").season == 2026
    assert b"2019" not in config_file.read_bytes()


def test_season_override_applies_to_the_default_league_too(config_file: Path):
    assert leagues.resolve_league(season=2018) == LeagueProfile(
        name="dynasty", provider="espn", league_id="123456", season=2018, sport="football"
    )


def test_resolve_accepts_an_explicit_config(config_file: Path):
    config = leagues.load()
    assert config.resolve("redraft", season=2020).season == 2020


# --------------------------------------------------------------------------
# profile parsing
# --------------------------------------------------------------------------


def test_sport_defaults_to_football(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_config(
        tmp_path,
        monkeypatch,
        '[leagues.only]\nprovider = "espn"\nleague_id = "1"\nseason = 2026\n',
    )
    assert leagues.resolve_league("only").sport == "football"


def test_a_numeric_league_id_is_normalized_to_a_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """TOML lets a user write ``league_id = 123456``; ESPN ids are opaque strings."""
    _write_config(
        tmp_path,
        monkeypatch,
        '[leagues.only]\nprovider = "espn"\nleague_id = 123456\nseason = 2026\n',
    )
    assert leagues.resolve_league("only").league_id == "123456"


def test_a_quoted_season_is_normalized_to_an_int(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_config(
        tmp_path,
        monkeypatch,
        '[leagues.only]\nprovider = "espn"\nleague_id = "1"\nseason = "2026"\n',
    )
    assert leagues.resolve_league("only").season == 2026


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ('[leagues.only]\nleague_id = "1"\nseason = 2026\n', "provider"),
        ('[leagues.only]\nprovider = "espn"\nseason = 2026\n', "league_id"),
        ('[leagues.only]\nprovider = "espn"\nleague_id = "1"\n', "season"),
    ],
)
def test_a_profile_missing_a_required_field_names_the_field(
    text: str, fragment: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_config(tmp_path, monkeypatch, text)
    with pytest.raises(ConfigInvalidError) as excinfo:
        leagues.load()
    assert fragment in str(excinfo.value)
    assert "only" in str(excinfo.value)


def test_an_unrecognized_profile_key_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A typo like ``leage_id`` must not be silently ignored."""
    _write_config(
        tmp_path,
        monkeypatch,
        '[leagues.only]\nprovider = "espn"\nleague_id = "1"\nseason = 2026\nleage_id = "2"\n',
    )
    with pytest.raises(ConfigInvalidError) as excinfo:
        leagues.load()
    assert "leage_id" in str(excinfo.value)


def test_a_boolean_league_id_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``bool`` is a subclass of ``int``; ``league_id = true`` must not become ``"True"``."""
    _write_config(
        tmp_path,
        monkeypatch,
        '[leagues.only]\nprovider = "espn"\nleague_id = true\nseason = 2026\n',
    )
    with pytest.raises(ConfigInvalidError) as excinfo:
        leagues.load()
    assert "league_id" in str(excinfo.value)


def test_an_unreadable_config_path_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An OSError that is not "missing" is a config problem, not an empty config."""
    xdg = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    paths.config_file().mkdir(parents=True)
    with pytest.raises(ConfigInvalidError) as excinfo:
        leagues.load()
    assert str(paths.config_file()) in str(excinfo.value)


def test_an_empty_required_field_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_config(
        tmp_path,
        monkeypatch,
        '[leagues.only]\nprovider = ""\nleague_id = "1"\nseason = 2026\n',
    )
    with pytest.raises(ConfigInvalidError) as excinfo:
        leagues.load()
    assert "provider" in str(excinfo.value)


@pytest.mark.parametrize("season", ['"this year"', "true", "2026.5"])
def test_a_non_integer_season_is_rejected(
    season: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_config(
        tmp_path,
        monkeypatch,
        f'[leagues.only]\nprovider = "espn"\nleague_id = "1"\nseason = {season}\n',
    )
    with pytest.raises(ConfigInvalidError) as excinfo:
        leagues.load()
    assert "season" in str(excinfo.value)


def test_a_profile_that_is_not_a_table_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_config(tmp_path, monkeypatch, "[leagues]\nonly = 3\n")
    with pytest.raises(ConfigInvalidError):
        leagues.load()


def test_a_non_table_leagues_key_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_config(tmp_path, monkeypatch, "leagues = 3\n")
    with pytest.raises(ConfigInvalidError):
        leagues.load()


def test_a_non_string_default_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_config(tmp_path, monkeypatch, "default = 3\n")
    with pytest.raises(ConfigInvalidError):
        leagues.load()


def test_an_unrecognized_top_level_section_is_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``config.toml`` is shared: ARCHITECTURE §6 puts credentials in the same file."""
    _write_config(tmp_path, monkeypatch, TWO_LEAGUES + '\n[auth.espn]\nswid = "{...}"\n')
    assert leagues.resolve_league("dynasty").league_id == "123456"


def test_malformed_toml_raises_config_error_naming_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = _write_config(tmp_path, monkeypatch, "default = \n")
    with pytest.raises(ConfigInvalidError) as excinfo:
        leagues.load()
    assert str(path) in str(excinfo.value)


def test_config_error_carries_the_config_invalid_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """ADR-0004 as amended: a malformed config is `CONFIG_INVALID` (#35, via #6).

    It must stay distinguishable from `LEAGUE_NOT_FOUND`, which would tell an
    agent to retry with a different `--league` — impossible when the file
    itself will not parse.
    """
    _write_config(tmp_path, monkeypatch, "default = 3\n")
    with pytest.raises(ConfigInvalidError) as excinfo:
        leagues.load()
    assert excinfo.value.code == "CONFIG_INVALID"
    assert excinfo.value.retryable is False
    assert not isinstance(excinfo.value, LeagueNotFoundError)


# --------------------------------------------------------------------------
# the [credentials] table — the last link of the auth chain (ARCHITECTURE §6)
# --------------------------------------------------------------------------


def _write_credentials(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_credentials_reads_the_credentials_table(tmp_path: Path):
    path = _write_credentials(tmp_path, '[credentials]\nespn_s2 = "abc"\nswid = "{x}"\n')
    assert credentials.load_credentials(path) == {"espn_s2": "abc", "swid": "{x}"}


def test_load_credentials_on_malformed_toml_raises_config_invalid(tmp_path: Path):
    """A file the user can fix must say so, not report a missing credential.

    Failing soft here sends the human off to re-extract a cookie they already
    have, while the real problem is a typo three lines away
    (jwulff/fantasy-sports#37).
    """
    path = tmp_path / "config.toml"
    path.write_text('[credentials]\nespn_s2 = "unterminated\n')
    with pytest.raises(ConfigInvalidError) as err:
        credentials.load_credentials(path)
    assert err.value.code == ErrorCode.CONFIG_INVALID
    assert err.value.retryable is False
    assert str(path) in err.value.details["path"]


def test_load_credentials_when_the_table_is_not_a_table_raises(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('credentials = "oops"\n')
    with pytest.raises(ConfigInvalidError) as err:
        credentials.load_credentials(path)
    assert err.value.details["found_type"] == "str"


def test_load_credentials_on_an_unquoted_value_raises_and_names_the_key(tmp_path: Path):
    """TOML reads an unquoted cookie as an integer. Dropping it is as opaque
    as coercing it; only naming the key tells the human what to fix."""
    path = tmp_path / "config.toml"
    path.write_text("[credentials]\nespn_s2 = 12345\n")
    with pytest.raises(ConfigInvalidError) as err:
        credentials.load_credentials(path)
    assert err.value.details["key"] == "espn_s2"
    assert "espn_s2" in err.value.remediation


def test_load_credentials_on_an_absent_file_is_empty_not_an_error(tmp_path: Path):
    """Absence is ordinary: an env-only host has no config file at all."""
    assert credentials.load_credentials(tmp_path / "nope.toml") == {}


def test_load_credentials_on_an_unreadable_file_is_empty_not_an_error(tmp_path: Path):
    """Environmental, not malformed — the same class as a locked Keychain."""
    path = tmp_path / "config.toml"
    path.write_text('[credentials]\nespn_s2 = "x"\n')
    path.chmod(0o000)
    try:
        assert credentials.load_credentials(path) == {}
    finally:
        path.chmod(0o600)


def test_load_credentials_without_the_table_is_empty_not_an_error(tmp_path: Path):
    """A config file that only configures leagues is not damaged."""
    path = tmp_path / "config.toml"
    path.write_text('[leagues.main]\nprovider = "espn"\n')
    assert credentials.load_credentials(path) == {}


def test_load_credentials_defaults_to_the_xdg_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No explicit path means the config layer's own file, not a private one."""
    _write_config(tmp_path, monkeypatch, '[credentials]\nespn_s2 = "abc"\n')
    assert credentials.load_credentials() == {"espn_s2": "abc"}


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def test_save_round_trips_through_tomllib(isolated_home: Path):
    config = leagues.LeagueConfig(
        path=paths.config_file(),
        default="dynasty",
        profiles_by_name={
            "dynasty": LeagueProfile(
                name="dynasty", provider="espn", league_id="123456", season=2026, sport="football"
            )
        },
    )
    written = leagues.save(config)
    assert written == paths.config_file()
    assert leagues.load() == config


def test_save_creates_the_config_directory(isolated_home: Path):
    assert not paths.config_home().exists()
    leagues.save(leagues.LeagueConfig(path=paths.config_file(), default=None, profiles_by_name={}))
    assert paths.config_home().is_dir()


def test_save_omits_an_unset_default(isolated_home: Path):
    written = leagues.save(
        leagues.LeagueConfig(path=paths.config_file(), default=None, profiles_by_name={})
    )
    assert "default" not in written.read_text()


# jwulff/fantasy-sports#85: save() must not drop the tables it does not own


def _dynasty(**overrides: object) -> LeagueProfile:
    fields: dict = {
        "name": "dynasty",
        "provider": "espn",
        "league_id": "123456",
        "season": 2026,
        "sport": "football",
    }
    fields.update(overrides)
    return LeagueProfile(**fields)


def test_save_keeps_every_table_it_does_not_own(tmp_path: Path):
    """``config.toml`` is a shared namespace: ``[credentials]`` lives there on
    every host without a Keychain, and a rewrite that only knew about
    ``default`` and ``[leagues]`` silently discarded it. Every table this
    module does not own must survive a save, including ones that do not
    exist yet.
    """
    path = tmp_path / "config.toml"
    path.write_text(
        'default = "old"\n'
        "[credentials]\n"
        'espn_s2 = "s2-value"\n'
        'SWID = "{guid}"\n'
        "[future]\n"
        "knob = 3\n"
        "[leagues.old]\n"
        'provider = "espn"\n'
        'league_id = "1"\n'
        "season = 2025\n"
    )
    config = leagues.LeagueConfig(
        path=path, default="dynasty", profiles_by_name={"dynasty": _dynasty()}
    )
    leagues.save(config)

    document = tomllib.loads(path.read_text())
    assert document["credentials"] == {"espn_s2": "s2-value", "SWID": "{guid}"}
    assert document["future"] == {"knob": 3}
    assert document["default"] == "dynasty"
    assert set(document["leagues"]) == {"dynasty"}, "the old profile is replaced, not merged"
    assert leagues.load(path) == config
    assert credentials.load_credentials(path) == {"espn_s2": "s2-value", "SWID": "{guid}"}


def test_save_removes_the_keys_it_owns_when_they_are_unset(tmp_path: Path):
    """An unset ``default`` and an empty profile set clear the stale keys
    rather than leaving last week's values behind them."""
    path = tmp_path / "config.toml"
    path.write_text(
        'default = "gone"\n'
        "[credentials]\n"
        'espn_s2 = "x"\n'
        "[leagues.gone]\n"
        'provider = "espn"\n'
        'league_id = "1"\n'
        "season = 2025\n"
    )
    leagues.save(leagues.LeagueConfig(path=path, default=None, profiles_by_name={}))
    document = tomllib.loads(path.read_text())
    assert document == {"credentials": {"espn_s2": "x"}}


def test_save_keeps_the_file_mode_and_leaves_no_temp_file(tmp_path: Path):
    """The same atomic, mode-preserving rewrite ``remove_credentials`` uses:
    a ``0600`` file holding cookies stays ``0600``, and no partial file is
    ever visible at the path."""
    path = tmp_path / "config.toml"
    path.write_text('[credentials]\nespn_s2 = "x"\n')
    path.chmod(0o600)
    leagues.save(
        leagues.LeagueConfig(path=path, default="d", profiles_by_name={"d": _dynasty(name="d")})
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".config-*.tmp"))


def test_save_creates_a_new_file_private(tmp_path: Path):
    """A file that will hold cookies on the next ``auth login`` starts private."""
    path = tmp_path / "nested" / "config.toml"
    leagues.save(leagues.LeagueConfig(path=path, default=None, profiles_by_name={}))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_writes_through_a_symlink_rather_than_replacing_it(tmp_path: Path):
    """A config managed from a dotfiles repo is a symlink at the XDG path.
    ``write_text`` followed it; an atomic ``replace`` on the link itself would
    swap the link for a plain file and leave the canonical copy stale. The
    rewrite lands on the referent and the link survives. Same for
    ``remove_credentials``, which shares the writer."""
    real = tmp_path / "dotfiles" / "fantasy-sports.toml"
    real.parent.mkdir()
    real.write_text('[credentials]\nespn_s2 = "x"\nSWID = "{g}"\n')
    link = tmp_path / "config.toml"
    link.symlink_to(real)

    leagues.save(
        leagues.LeagueConfig(path=link, default="d", profiles_by_name={"d": _dynasty(name="d")})
    )
    assert link.is_symlink() and link.resolve() == real.resolve()
    assert tomllib.loads(real.read_text())["leagues"]["d"]["league_id"] == "123456"
    assert not list(tmp_path.glob(".config-*.tmp"))

    assert credentials.remove_credentials(["SWID"], link) == ("SWID",)
    assert link.is_symlink()
    assert tomllib.loads(real.read_text())["credentials"] == {"espn_s2": "x"}


def test_save_refuses_to_overwrite_a_file_it_cannot_parse(tmp_path: Path):
    """A save that cannot read the document cannot carry the other tables
    through, and replacing a broken file with a partial one would turn a
    syntax error into silent data loss. Same code ``load`` raises."""
    path = tmp_path / "config.toml"
    path.write_text("[credentials\nespn_s2 = ")
    with pytest.raises(ConfigInvalidError):
        leagues.save(leagues.LeagueConfig(path=path, default=None, profiles_by_name={}))
    assert path.read_text() == "[credentials\nespn_s2 = "


# --------------------------------------------------------------------------
# layering
# --------------------------------------------------------------------------

FORBIDDEN = {"typer", "click", "rich", "espn_api", "platformdirs"}
CONFIG_SOURCES = sorted(
    (Path(__file__).resolve().parents[2] / "src" / "fantasy_sports" / "config").glob("*.py")
)


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_config_sources_were_found():
    assert {p.name for p in CONFIG_SOURCES} >= {"paths.py", "leagues.py"}


@pytest.mark.parametrize("path", CONFIG_SOURCES, ids=lambda p: p.name)
def test_config_never_imports_a_cli_or_provider_module(path: Path):
    """ARCHITECTURE §4: config sits below the CLI and below every provider."""
    offenders = _imported_roots(path) & FORBIDDEN
    assert not offenders, f"{path.name} imports {sorted(offenders)}"


def test_importing_config_stays_cheap():
    """``tomli_w`` is a write-path dependency and must not load on import."""
    probe = (
        "import fantasy_sports.config.leagues, fantasy_sports.config.paths\n"
        "import sys, json; print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))"
    )
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    loaded = set(json.loads(proc.stdout.strip().splitlines()[-1]))
    assert not (loaded & (FORBIDDEN | {"requests", "keyring", "tomli_w"}))
