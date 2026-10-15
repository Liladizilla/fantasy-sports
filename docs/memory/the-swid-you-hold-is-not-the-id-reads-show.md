# The SWID you hold is not the member id your reads show

**Found:** 2026-10-14, building the write transport (#107). **Applies to:**
anything that compares the configured SWID against a league payload: the
ownership guard, the journal's attribution (#16), `doctor`, any future
"which team is mine" lookup.

A command never parses a raw ESPN body. `open_read` always builds a
`CacheStore`, even for `--no-cache`, because the store is what scrubs, and the
store rewrites every brace-wrapped GUID to a **per-store pseudonym** before the
adapter sees it ([[swid-pseudonyms]]). So in `mNav`'s `members[].id` and every
`teams[].owners`, the session's own SWID appears as
`{00000000-…}`, keyed by a random salt that lives in that store.

Compare the credential against those ids as-is and nothing matches: the guard
owns no teams and refuses every write, or — worse, in a looser design — falls
back to something permissive. Unit tests without a store pass either way,
because a store-less provider parses raw bodies; the bug only shows up through
a real command.

**The rule:** match on both spellings — the GUID, case-insensitively, and
`swid_pseudonym(swid, salt=store.swid_salt)` — and *report* the pseudonym, so
the SWID never reaches output. Scrub the write host's response with the same
salt, so the `memberId` ESPN echoes is the same pseudonym `session_role`
reports and the two join. A store-less provider uses `CASSETTE_SWID_SALT` for
the same reason. The pseudonym is stable per store, not across machines: a
journal entry's member id joins to reads on the host that wrote it.

`test_ownership_survives_the_cache_pseudonymising_every_swid` holds this.

A smaller trap from the same unit: `espn_api.football.constant.POSITION_MAP`
holds both directions, but its *string* keys are the free-agent filter
vocabulary and omit `BE`, `IR`, and `RB/WR/TE`. Map a slot name to a
`lineupSlotId` through the integer keys, or the three slots a lineup change
most often targets are refused as unknown.

Related: [[swid-pseudonyms]], [[cache-redaction-and-tag-classes]],
[[commissioner-authority-is-two-flags-in-mnav]]
