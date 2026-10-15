# Architecture Decision Records

Individual decisions extracted from `docs/ARCHITECTURE.md`. The architecture doc
is the narrative; these are the citable, individually-supersedable records.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-python-as-implementation-language.md) | Python as the implementation language | Accepted |
| [0002](0002-normalize-shape-not-semantics.md) | Normalize shape, not semantics | Amended |
| [0003](0003-command-registry-cli-and-mcp-as-projections.md) | Commands are a typed registry; CLI and MCP are projections | Accepted |
| [0004](0004-versioned-output-contract-and-error-taxonomy.md) | Versioned output contract and error taxonomy | Accepted |
| [0005](0005-health-system-canary-manifest-client-check.md) | Health system — canary, manifest, client check | Accepted |
| [0006](0006-read-only-v01-gated-writes-later.md) | Read-only v0.1; writes gated later | Superseded by 0010 and the write-surface plan |
| [0007](0007-client-error-reporting-via-user-credential.md) | Client error reporting uses the operator's own `gh` credential | Accepted |
| [0008](0008-performance-and-quality-budgets.md) | Performance and quality budgets, enforced in CI | Accepted |
| [0009](0009-config-invalid-covers-a-bad-argument-too.md) | `CONFIG_INVALID` covers a provider-rejected argument too, no eighth code | Accepted |
| [0010](0010-write-codes-one-code-with-a-kind-discriminator.md) | `WRITE_REJECTED` is one code with a `kind` discriminator; `WRITE_DIVERGED` is separate | Accepted |

Use `0000-template.md` for new records.
