"""core layer. See docs/ARCHITECTURE.md.

The normalized domain model and the error taxonomy, both free of any provider,
CLI, or LLM dependency. Re-exported here so adapters can write
``from fantasy_sports.core import League, Team`` as
``docs/research/02-provider-data-shapes.md`` §5 assumes.
"""

from __future__ import annotations

from fantasy_sports.core.errors import (
    ERROR_TYPES,
    WRITE_REJECTED_KINDS,
    AuthExpiredError,
    AuthMissingError,
    ConfigInvalidError,
    ErrorCode,
    FantasySportsError,
    LeagueNotFoundError,
    ProviderUnavailableError,
    RateLimitedError,
    SchemaDriftError,
    WriteDivergedError,
    WriteRejectedError,
    error_type_for,
)
from fantasy_sports.core.models import (
    NORMALIZED_MODELS,
    TRANSACTION_TYPES,
    BoxScore,
    CredentialSpec,
    FreeAgent,
    League,
    LineupEntry,
    Matchup,
    Player,
    ProviderObject,
    RosterSlot,
    Team,
    Transaction,
)

__all__ = [
    "ERROR_TYPES",
    "NORMALIZED_MODELS",
    "TRANSACTION_TYPES",
    "WRITE_REJECTED_KINDS",
    "AuthExpiredError",
    "AuthMissingError",
    "ConfigInvalidError",
    "CredentialSpec",
    "ErrorCode",
    "FantasySportsError",
    "FreeAgent",
    "League",
    "LeagueNotFoundError",
    "BoxScore",
    "LineupEntry",
    "Matchup",
    "Player",
    "ProviderObject",
    "ProviderUnavailableError",
    "RateLimitedError",
    "RosterSlot",
    "SchemaDriftError",
    "Team",
    "Transaction",
    "WriteDivergedError",
    "WriteRejectedError",
    "error_type_for",
]
