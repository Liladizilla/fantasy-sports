# Research

Findings from parallel research agents, 2026-08-26 (pre-implementation), plus
later single-question briefs as they land.

Each file is a standalone brief. They inform `docs/ARCHITECTURE.md` and the
ADRs in `docs/adr/`; where a brief and the architecture doc disagree, the
architecture doc is being updated, not the brief.

| File | Question |
|---|---|
| `01-telemetry-auto-issues.md` | Semi-secure, flood-resistant auto-filing of GitHub issues from client errors |
| `02-provider-data-shapes.md` | Data models of the top 5 fantasy providers; what normalizes and what does not |
| `03-espn-api-surface.md` | ESPN v3 endpoint/view surface, auth mechanics, breakage history |
| `04-python-cli-packaging.md` | 2026 best practice for uv-distributed typer CLIs, testing, secrets |
| `05-espn-write-surface.md` | ESPN write host, lineup transaction shape, rejection vocabulary, credential and ownership scope — probed live against a real league (#14); captures in `05-espn-write-surface/` |
| `06-espn-add-drop-surface.md` | ESPN add/drop/waiver/cancel item shapes and rejection vocabulary, probed in a throwaway public league during its post-draft waiver window (#108, partial); captures in `06-espn-add-drop-surface/` |
