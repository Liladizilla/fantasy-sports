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
    SESSION_ROLES,
    TRANSACTION_TYPES,
    BoxScore,
    CredentialSpec,
    FreeAgent,
    League,
    LineupEntry,
    Matchup,
    Player,
    ProviderObject,
    RosterMove,
    RosterSlot,
    SessionRole,
    Team,
    Transaction,
    WriteResult,
)

__all__ = [
    "ERROR_TYPES",
    "NORMALIZED_MODELS",
    "SESSION_ROLES",
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
    "RosterMove",
    "RosterSlot",
    "SchemaDriftError",
    "SessionRole",
    "Team",
    "Transaction",
    "WriteResult",
    "WriteDivergedError",
    "WriteRejectedError",
    "error_type_for",
]
