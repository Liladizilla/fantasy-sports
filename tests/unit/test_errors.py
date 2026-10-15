"""The error taxonomy (``docs/ARCHITECTURE.md`` §5), enforced.

Adding, renaming, or removing a code here is an API change: agents branch on
these strings to tell "ask the human" from "retry later". The tests below pin
the code set, the retry semantics, and the fact that no error payload is built
out of provider bytes.
"""

from __future__ import annotations

import json
import traceback

import pytest

from fantasy_sports.core.errors import (
    ERROR_TYPES,
    WRITE_REJECTED_KINDS,
    AuthExpiredError,
    AuthMissingError,
    ConfigInvalidError,
    ErrorCode,
    FantasySportsError,
    LeagueNotFoundError,
    NotAvailableError,
    ProviderUnavailableError,
    RateLimitedError,
    SchemaDriftError,
    WriteDivergedError,
    WriteRejectedError,
    error_type_for,
)
from fantasy_sports.core.redaction import REDACTED, forget_secrets, remember_secret

# The taxonomy exactly as ARCHITECTURE.md §5 documents it.
DOCUMENTED_CODES = {
    "AUTH_MISSING",
    "AUTH_EXPIRED",
    "LEAGUE_NOT_FOUND",
    "CONFIG_INVALID",
    "NOT_AVAILABLE",
    "PROVIDER_UNAVAILABLE",
    "RATE_LIMITED",
    "SCHEMA_DRIFT",
    "WRITE_REJECTED",
    "WRITE_DIVERGED",
}

# A sentinel that cannot occur by accident anywhere in rendered output.
SECRET = "AEBqp7SENTINELs2cookievalue0123456789abcdefXYZ%2Fzz"


@pytest.fixture(autouse=True)
def _forget_secrets():
    """The scrub set is process-wide; keep it from leaking between tests."""
    forget_secrets()
    yield
    forget_secrets()


def test_the_taxonomy_is_exactly_the_documented_set():
    assert {code.value for code in ErrorCode} == DOCUMENTED_CODES


def test_every_code_has_exactly_one_exception_type():
    assert {cls.code for cls in ERROR_TYPES} == set(ErrorCode)
    assert len(ERROR_TYPES) == len(ErrorCode)


@pytest.mark.parametrize("cls", ERROR_TYPES, ids=lambda c: c.__name__)
def test_every_error_is_a_fantasy_sports_error_with_a_stable_shape(cls):
    exc = cls("something went wrong")
    assert isinstance(exc, FantasySportsError)
    assert str(exc) == "something went wrong"
    payload = exc.to_dict()
    assert payload["code"] == cls.code.value
    assert payload["message"] == "something went wrong"
    assert isinstance(payload["retryable"], bool)
    assert payload["agent_action"]


@pytest.mark.parametrize("cls", ERROR_TYPES, ids=lambda c: c.__name__)
def test_error_type_for_round_trips_every_code(cls):
    assert error_type_for(cls.code) is cls
    assert error_type_for(cls.code.value) is cls


def test_error_type_for_rejects_an_unknown_code():
    with pytest.raises(KeyError):
        error_type_for("NOT_A_CODE")


def test_credential_failures_are_not_retryable_and_availability_is():
    assert AuthMissingError("no creds").retryable is False
    assert AuthExpiredError("dead cookies").retryable is False
    assert LeagueNotFoundError("nope").retryable is False
    assert ConfigInvalidError("broken toml").retryable is False
    assert SchemaDriftError("shape changed").retryable is False
    assert NotAvailableError("espn refuses this request").retryable is False
    assert ProviderUnavailableError("espn 503").retryable is True
    assert RateLimitedError("slow down").retryable is True
    assert WriteRejectedError("locked", kind="roster_lock").retryable is False
    assert WriteDivergedError("moved").retryable is False


def test_a_positive_refusal_is_not_an_unclassifiable_one():
    """The reason `NOT_AVAILABLE` was added (decision on #45).

    `PROVIDER_UNAVAILABLE` is the honest landing place for a failure we cannot
    classify, per R12 — bounded retry is safe when we are wrong. A refusal the
    adapter can positively identify, like ESPN's pre-2019 box-score refusal, is
    the opposite case: retrying it is never safe, because it will never
    succeed. The two must stay distinguishable in the payload.
    """
    unclassifiable = ProviderUnavailableError("espn 503")
    refused = NotAvailableError("ESPN does not serve box scores for the 2018 season.")

    assert unclassifiable.code is ErrorCode.PROVIDER_UNAVAILABLE
    assert refused.code is ErrorCode.NOT_AVAILABLE
    assert refused.code is not ErrorCode.PROVIDER_UNAVAILABLE
    assert unclassifiable.retryable is True
    assert refused.retryable is False
    assert refused.to_dict()["agent_action"] != unclassifiable.to_dict()["agent_action"]


def test_a_broken_config_is_not_a_missing_league():
    """The reason `CONFIG_INVALID` was added (decision on #6).

    `LEAGUE_NOT_FOUND` tells an agent to retry with a different `--league`.
    That cannot work when the file itself will not parse, so the two causes
    must be distinguishable from the payload alone.
    """
    broken = ConfigInvalidError("config.toml is not valid TOML")
    absent = LeagueNotFoundError("no league named 'keeper'")

    assert broken.code is ErrorCode.CONFIG_INVALID
    assert broken.code is not ErrorCode.LEAGUE_NOT_FOUND
    assert broken.to_dict()["code"] != absent.to_dict()["code"]
    assert broken.to_dict()["agent_action"] != absent.to_dict()["agent_action"]
    # Not retryable, and distinguishable from both availability causes.
    assert broken.retryable is False
    assert broken.code not in {ErrorCode.PROVIDER_UNAVAILABLE, ErrorCode.RATE_LIMITED}


def test_config_invalid_defaults_to_kind_config_and_records_it_in_details():
    """jwulff/fantasy-sports#48 (ADR-0004 amended by ADR-0009).

    `CONFIG_INVALID` covers two causes now — a config file that will not
    parse, and a CLI argument only the provider can validate — rather than an
    eighth taxonomy code for the second. `kind` is the discriminator, and it
    defaults to `"config"` because every pre-existing raise site (a broken
    `config.toml`, an unknown provider name, a malformed credentials file) is
    that cause and none of them pass `kind` explicitly.
    """
    default = ConfigInvalidError("config.toml is not valid TOML")
    assert default.kind == "config"
    assert default.to_dict()["details"] == {"kind": "config"}

    argument = ConfigInvalidError("bad --pos", kind="argument")
    assert argument.kind == "argument"
    assert argument.to_dict()["details"] == {"kind": "argument"}

    # The class-level instruction no longer names a config file specifically —
    # it has to be honest for both causes now.
    assert "config file" not in default.agent_action.lower()
    assert default.agent_action == argument.agent_action
    # Exit status and retry semantics are identical either way; `kind` is
    # metadata for a consumer that wants it, not a second taxonomy surface.
    assert default.retryable is argument.retryable is False
    assert default.code is argument.code is ErrorCode.CONFIG_INVALID


# --- scrubbing: the guarantee that travelled with deleting `AuthError` ------


def test_a_non_auth_error_scrubs_a_credential_a_caller_interpolated():
    """The constraint that moved when `auth.chain.AuthError` was deleted (#35).

    `AuthError` scrubbed its message at construction. If that had not moved
    down to `FantasySportsError`, every *other* error type would silently have
    lost the protection and #34's redaction guarantee would be worth less than
    it reads.

    This is deliberately built from a **non-auth** error. An auth-shaped test
    would still pass if the scrubbing had stayed behind in `auth/`, which is
    exactly the regression it has to catch. The realistic leak is the one in
    `docs/memory/credential-leak-channels.md`: a caller three layers away
    formatting a request URL that carries a cookie.
    """
    remember_secret(SECRET)
    exc = ProviderUnavailableError(
        f"GET https://fantasy.espn.com/apis/v3/x?espn_s2={SECRET} failed",
        details={"url": f"https://fantasy.espn.com/apis/v3/x?espn_s2={SECRET}"},
    )

    assert SECRET not in str(exc)
    assert SECRET not in repr(exc)
    assert SECRET not in "".join(str(arg) for arg in exc.args)
    assert SECRET not in exc.message
    assert SECRET not in json.dumps(exc.to_dict())
    assert SECRET not in "".join(traceback.format_exception(exc))
    assert REDACTED in exc.message


@pytest.mark.parametrize("cls", ERROR_TYPES, ids=lambda c: c.__name__)
def test_every_error_type_scrubs_not_just_the_auth_ones(cls):
    """Scrubbing is a property of the base class, so it holds for all of them."""
    remember_secret(SECRET)
    assert SECRET not in json.dumps(cls(f"leaked {SECRET}").to_dict())


def test_an_unregistered_value_is_left_alone():
    """Control: proves the tests above measure the scrub set, not a coincidence.

    Without this, a `redact()` that blanked every message would pass every
    assertion above while destroying the error text.
    """
    exc = ProviderUnavailableError(f"GET https://espn.com/x?espn_s2={SECRET} failed")
    assert SECRET in exc.message, "nothing was registered; the value must survive"


def test_a_detail_added_after_construction_is_also_scrubbed():
    """`SchemaDriftError` writes `path`/`provider` after `super().__init__`."""
    remember_secret(SECRET)
    exc = SchemaDriftError("shape changed", path=[f"Team.{SECRET}"], provider=SECRET)
    assert SECRET not in json.dumps(exc.to_dict())


def test_scrubbing_reaches_into_nested_details():
    """`details` is caller-assembled, so a credential can hide below the top level."""
    remember_secret(SECRET)
    exc = ProviderUnavailableError(
        "espn 503",
        details={
            "request": {"url": f"https://espn.com/x?espn_s2={SECRET}"},
            "tried": (f"env:{SECRET}", "keychain"),
            "attempts": 3,
        },
    )
    payload = exc.to_dict()
    assert SECRET not in json.dumps(payload)
    # Shape and non-string scalars survive; only the credential is replaced.
    assert payload["details"]["attempts"] == 3
    assert payload["details"]["tried"][1] == "keychain"
    assert REDACTED in payload["details"]["request"]["url"]


def test_rate_limited_carries_retry_after():
    exc = RateLimitedError("slow down", retry_after=30.0)
    assert exc.retry_after == 30.0
    assert exc.to_dict()["details"]["retry_after"] == 30.0


def test_rate_limited_without_a_retry_after_reports_null_details():
    assert RateLimitedError("slow down").to_dict()["details"] is None


def test_schema_drift_records_the_offending_path():
    exc = SchemaDriftError("missing teams", path="mTeams.teams", provider="espn")
    details = exc.to_dict()["details"]
    assert details["path"] == ["mTeams.teams"]
    assert details["provider"] == "espn"


def test_schema_drift_accepts_several_paths():
    exc = SchemaDriftError("missing fields", path=["Team.name", "Team.wins"])
    assert exc.to_dict()["details"]["path"] == ["Team.name", "Team.wins"]


def test_schema_drift_without_a_path_reports_null_details():
    assert SchemaDriftError("shape changed").to_dict()["details"] is None


def test_the_error_payload_keys_are_always_present():
    """ADR-0004 as amended by U5 (#6): every key, every time, `None` when empty.

    A key that disappears when it is empty is a key some consumers will never
    read for, which is exactly what promoting `remediation` out of `details`
    was meant to prevent.
    """
    expected = ["code", "message", "retryable", "agent_action", "remediation", "details"]
    for error_cls in ERROR_TYPES:
        assert list(error_cls("bare").to_dict()) == expected, error_cls.__name__


def test_remediation_is_a_first_class_key_and_is_scrubbed():
    exc = ProviderUnavailableError("espn 503", remediation="Retry in a minute.")
    assert exc.remediation == "Retry in a minute."
    assert exc.to_dict()["remediation"] == "Retry in a minute."
    assert exc.details == {}, "it is not a detail"

    remember_secret(SECRET)
    leaky = ProviderUnavailableError("espn 503", remediation=f"Rotate {SECRET} in DevTools.")
    assert SECRET not in leaky.to_dict()["remediation"]
    assert REDACTED in leaky.to_dict()["remediation"]


def test_details_are_copied_so_a_caller_cannot_mutate_the_payload_later():
    supplied = {"league_id": "123456"}
    exc = ProviderUnavailableError("espn 503", details=supplied)
    supplied["league_id"] = "mutated"
    assert exc.to_dict()["details"]["league_id"] == "123456"


def test_an_unclassifiable_failure_is_available_shaped_not_throttled():
    """R12: unclassifiable maps to availability with bounded retry, never throttling."""
    exc = ProviderUnavailableError("unclassifiable upstream failure")
    assert exc.code is ErrorCode.PROVIDER_UNAVAILABLE
    assert exc.retryable is True
    assert exc.code is not ErrorCode.RATE_LIMITED


# --- WRITE_REJECTED / WRITE_DIVERGED (#106, ADR-0010) -----------------------

# The write host's own 401 body, which cannot tell expired from absent from refused.
WRITE_401_TYPE = "AUTH_MISSING_CREDENTIALS"


def test_the_write_vocabulary_is_closed_and_exactly_the_documented_set():
    assert set(WRITE_REJECTED_KINDS) == {
        "roster_lock",
        "slot_limit",
        "same_slot",
        "not_current_period",
        "budget_exceeded",
        "position_limit",
        "already_dropped",
        "roster_full",
        "player_on_waivers",
        "player_unavailable",
        "not_owned",
        "auth",
        "unknown",
    }
    assert len(set(WRITE_REJECTED_KINDS)) == len(WRITE_REJECTED_KINDS)
    assert isinstance(WRITE_REJECTED_KINDS, tuple)


def test_write_rejected_records_every_field_in_details():
    exc = WriteRejectedError(
        "Lineup transaction could not be completed",
        kind="roster_lock",
        provider_type="TRAN_LINEUP_LOCKED",
        http_status=409,
        team_id=1,
        remediation="Lineup transaction could not be completed, a player is locked",
    )
    payload = exc.to_dict()
    assert payload["code"] == "WRITE_REJECTED"
    assert payload["retryable"] is False
    assert payload["agent_action"] == "Do not retry unchanged; read the roster and re-plan."
    assert payload["remediation"] == "Lineup transaction could not be completed, a player is locked"
    assert payload["details"] == {
        "kind": "roster_lock",
        "provider_type": "TRAN_LINEUP_LOCKED",
        "http_status": 409,
        "team_id": "1",
        "transaction_applied": False,
    }
    assert exc.kind == "roster_lock"
    assert exc.transaction_applied is False


def test_write_rejected_details_keys_do_not_depend_on_what_was_supplied():
    """A client-side refusal has no provider type or status; the keys stay."""
    details = WriteRejectedError("not yours", kind="not_owned").to_dict()["details"]
    assert list(details) == [
        "kind",
        "provider_type",
        "http_status",
        "team_id",
        "transaction_applied",
    ]
    assert details["provider_type"] is None
    assert details["http_status"] is None
    assert details["team_id"] is None
    assert details["transaction_applied"] is False


def test_write_rejected_keeps_a_caller_supplied_detail_and_the_kind_wins():
    exc = WriteRejectedError(
        "locked", kind="roster_lock", details={"scoring_period": 6, "kind": "forged"}
    )
    assert exc.details["scoring_period"] == 6
    assert exc.details["kind"] == "roster_lock"


def test_write_rejected_reports_a_provider_type_we_have_not_mapped():
    exc = WriteRejectedError("new rule", kind="unknown", provider_type="TRAN_BRAND_NEW_THING")
    assert exc.to_dict()["details"]["provider_type"] == "TRAN_BRAND_NEW_THING"
    assert exc.to_dict()["details"]["kind"] == "unknown"


@pytest.mark.parametrize("kind", WRITE_REJECTED_KINDS)
def test_every_vocabulary_kind_constructs(kind):
    assert WriteRejectedError("x", kind=kind).details["kind"] == kind


@pytest.mark.parametrize("bad", ["", "ROSTER_LOCK", "roster-lock", "locked", "TRAN_LINEUP_LOCKED"])
def test_an_unknown_kind_is_a_programming_error_not_a_taxonomy_error(bad):
    with pytest.raises(ValueError, match="unknown WRITE_REJECTED kind"):
        WriteRejectedError("x", kind=bad)
    assert not issubclass(ValueError, FantasySportsError)


def test_write_rejected_kind_defaults_to_unknown():
    assert WriteRejectedError("x").kind == "unknown"
    assert WriteRejectedError("x").to_dict()["details"]["kind"] == "unknown"


def test_a_write_host_401_with_a_credential_sent_is_auth_not_auth_expired():
    """Brief §5.1 item 1. The code a caller reaches for here is `WRITE_REJECTED`.

    The write host's 401 is a constant that cannot tell expired from absent.
    Nothing in the taxonomy lets `AUTH_EXPIRED` be built from it, so this pins
    the shape the transport (#107) is told to use instead.
    """
    exc = WriteRejectedError(
        "ESPN refused the write.",
        kind="auth",
        provider_type=WRITE_401_TYPE,
        http_status=401,
        team_id="1",
    )
    assert exc.code is ErrorCode.WRITE_REJECTED
    assert exc.code is not ErrorCode.AUTH_EXPIRED
    assert exc.retryable is False
    assert exc.agent_action != AuthExpiredError.agent_action
    assert exc.to_dict()["details"]["kind"] == "auth"


def test_write_rejected_scrubs_a_credential_in_every_field():
    spec = "AEBqp7WRITEs2cookie0123456789abcdefXYZ"
    swid = "{0A1B2C3D-4E5F-6789-ABCD-EF0123456789}"
    remember_secret(spec)
    remember_secret(swid)
    exc = WriteRejectedError(
        f"POST ?espn_s2={spec} for {swid} refused",
        kind="auth",
        provider_type=f"TRAN_{spec}",
        team_id=swid,
        remediation=f"Use {spec}",
        details={"url": f"https://x/y?espn_s2={spec}&swid={swid}"},
    )
    rendered = json.dumps(exc.to_dict()) + str(exc) + repr(exc) + repr(vars(exc))
    assert spec not in rendered
    assert swid not in rendered
    assert REDACTED in exc.message
    # The public attributes are what `vars()` and a captured-locals traceback read.
    assert spec not in exc.provider_type
    assert swid not in exc.team_id
    assert exc.provider_type == f"TRAN_{REDACTED}"
    assert exc.team_id == REDACTED


def test_write_rejected_cannot_be_told_a_rejection_applied():
    with pytest.raises(TypeError):
        WriteRejectedError("x", kind="unknown", transaction_applied=True)  # type: ignore[call-arg]
    assert WriteRejectedError("x").transaction_applied is False


def test_write_diverged_carries_expected_and_observed():
    exc = WriteDivergedError(
        "Read-back does not match.",
        expected={"3139": "RB", 4046: "BE"},
        observed={"3139": "BE", "4046": "BE"},
    )
    payload = exc.to_dict()
    assert payload["code"] == "WRITE_DIVERGED"
    assert payload["retryable"] is False
    assert payload["agent_action"]
    assert payload["details"] == {
        "expected": {"3139": "RB", "4046": "BE"},
        "observed": {"3139": "BE", "4046": "BE"},
    }
    assert exc.expected == payload["details"]["expected"]


def test_write_diverged_with_nothing_supplied_still_names_both_fields():
    assert WriteDivergedError("moved").to_dict()["details"] == {"expected": {}, "observed": {}}


def test_write_diverged_copies_its_maps():
    expected = {"1": "RB"}
    exc = WriteDivergedError("moved", expected=expected)
    expected["1"] = "mutated"
    assert exc.to_dict()["details"]["expected"] == {"1": "RB"}


def test_write_diverged_scrubs_its_maps():
    remember_secret(SECRET)
    exc = WriteDivergedError("moved", expected={"1": SECRET}, observed={SECRET: "RB"})
    assert SECRET not in json.dumps(exc.to_dict())
    assert SECRET not in json.dumps(exc.expected) + json.dumps(exc.observed)
    assert SECRET not in repr(vars(exc))


def test_a_rejection_and_a_divergence_are_distinguishable():
    """A rejection did not apply; a divergence may have. Different next steps."""
    rejected = WriteRejectedError("no", kind="slot_limit")
    diverged = WriteDivergedError("moved")
    assert rejected.code is not diverged.code
    assert rejected.agent_action != diverged.agent_action
    assert rejected.details["transaction_applied"] is False
