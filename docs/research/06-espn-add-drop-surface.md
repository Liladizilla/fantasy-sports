# ESPN Fantasy API (v3) — The Add / Drop / Waiver Surface

**Status:** Research brief, 2026-10-15. Partial answer to jwulff/fantasy-sports#108;
feeds #18 (irreversible writes) and #109 (claims). Builds on
`05-espn-write-surface.md`, whose host, envelope, and status mapping it does
not repeat.

**Method:** Probed the write host through `scripts/probe_espn_add_drop.py`
against a throwaway *public* ESPN league John joined on 2026-10-14 for this
purpose (`[leagues.test]` in his config; 10 teams, PPR, NFL 2026 scoring
period 6). Thursday 2026-10-15, 06:32–06:35Z. The session's member owns team
7 there and is a plain member: `mNav` shows `isLeagueCreator: false` and
`isLeagueManager: false`. The script refuses any `--league` but `test`,
asserts before every POST that the URL names the test league and no other
configured league, that the body's `teamId` is the one team `mNav` says the
session owns, and that every `toTeamId`/`fromTeamId` names that team. It keeps
at least 5 s between writes across invocations. Every write was one request,
read back, decided, then the next.

**The waiver window.** The league's draft completed at 06:26Z, minutes before
the first probe, so **every unrostered player was `WAIVERS` and none was
`FREEAGENT`** (892 to 0 in `kona_player_info`). Waivers process at league
hour 11 on Thursdays. No `FREEAGENT` add could execute, so every probe that
needs one is in §6, with the exact command that runs it. Everything that a
`WAIVER` body can carry was probed on pending claims instead, which change
nothing until processing, and each claim was cancelled on the next request.

**Evidence:** `docs/research/06-espn-add-drop-surface/`: one JSON per probe
(`p<N>-*.json`, request and response in 05's format), `roster-readbacks.json`,
`audit-trail-mTransactions2.json`, and `pending-transactions.json`
(`view=mPendingTransactions` while a claim was live and after it was
cancelled). Redactions follow 05 and `docs/testing.md` §6, listed in each
file. The league id is `99`. The member's SWID is `{SWID-REDACTED}`. Player
ids on team 7, and the free agent it claimed, use a stable synthetic map that
continues 05's (`9000015` upward, D/ST `-9000002`), and the names ESPN
interpolates into messages use the same label. The one other team's player
id is `0`, and its name and team number are withheld. Transaction ids are
`<uuid-N>`, numbered per distinct id across all files, so a cancel still joins
to the claim it withdrew. The unredacted captures exist locally under
`tests/cassettes/private/108/`, which is gitignored, and are not in git.

**Writes sent:** 13, all to team 7 of the test league. Nine were rejected,
two claims went `PENDING`, and two cancels withdrew them. Nothing executed
against the roster, and the final read-back is identical to the first,
player for player and slot for slot.

The audit trail also shows two `ROSTER` lineup transactions on team 7 at
06:39:47Z and 06:39:50Z under the same member. They are a swap and its
reversal. This probe session did not send them, since the script cannot send
a lineup move. They were sent four minutes after the last probe by another
writer holding the same cookie, and they cancel each other out.

---

## 0. Headline answers

| # | Question | Answer | Evidence |
|---|---|---|---|
| 1 | `ADD` item shape | `{"playerId", "type": "ADD", "toTeamId"}`. `toTeamId` is **required**: without it, `409 TRAN_ITEM_TO_TEAM_ID_MISSING`. ESPN echoes `fromTeamId: 0` and `fromLineupSlotId: -1`, and `toLineupSlotId: -1` unless one was sent. | p4, p7a |
| 2 | `DROP` item shape | `{"playerId", "type": "DROP", "fromTeamId"}`. `fromTeamId` is **required**: without it, `409 TRAN_ITEM_FROM_TEAM_ID_MISSING`. ESPN fills `fromLineupSlotId` with the player's current slot itself. | p4, p7b |
| 3 | `toLineupSlotId` on an `ADD` | Accepted and echoed (`20`). Optional. | p7c |
| 4 | Drop without an add | **Not a `FREEAGENT`.** That envelope must hold exactly one `ADD`: `409 TRAN_INVALID_ITEM_TYPE_COUNT`. A `ROSTER` envelope with a `DROP` item passed shape validation and reached the player check (`TRAN_PLAYER_NOT_ON_TEAM`). That an executing standalone drop is `ROSTER` is therefore **strongly indicated, not observed** (§6). | p6b, p6c |
| 5 | `WAIVER` claim | `200`, `status: "PENDING"`, `isPending: true`, a server id, `bidAmount: 0` echoed, `subOrder: 1`. Roster unchanged. The claim appears in `mTransactions2` and `mPendingTransactions`. | p4, p7c, pending |
| 6 | Claim on a full roster with no drop | Rejected **at submit**, not at processing: `409 TRAN_ROSTER_LIMIT_EXCEEDED_ONE`, message `Too many players on roster (maximum 16).` This is the only rejection that filled `resolution`. | p3 |
| 7 | `CANCEL` | `type: "WAIVER"`, `executionType: "CANCEL"`, `relatedTransactionId`, no items: **the community shape, first try.** `200` with a **new** id, `status: "CANCELED"`, the claim's items echoed. No alternative `type` was needed or tried. | p5, p7c-r |
| 8 | Does a cancelled claim leave the queue | Yes in `mPendingTransactions`, at once. **No in `mTransactions2`**: the claim's own row keeps reading `PENDING`, and the `CANCELED` row pointing at it is the only record of the withdrawal. | p5, p7c-r, pending, audit trail |
| 9 | Any bid with FAAB off | `409 TRAN_ACQUISITION_BUDGET_NOT_SUPPORTED`, `This League does not support bids on Transactions.`, for `bidAmount: 1` and `1000` alike. A claim in such a league must omit `bidAmount`. | p2, p6a |
| 10 | `FREEAGENT` add of a player on waivers | `409 TRAN_PLAYER_NOT_FREEAGENT`, `<player> is not a free agent`. Checked **before** roster size: the roster was full and that was not what ESPN said. | p1 |
| 11 | Add of a player on another roster | `409 TRAN_PLAYER_NOT_AVAILABLE`, `<player> is not available, currently on team <n>`. | p6d |
| 12 | Drop of a player not on the team | `409 TRAN_PLAYER_NOT_ON_TEAM`, `<player> is not on team 7`. | p6c |
| 13 | Position limit, budget exceeded | **Not observed** (§5). | — |
| 14 | `FREEAGENT` add, add+drop, executing drop | **Not observed**: no free agents existed (§6). | — |

Every rejection was `409` with one typed `details[]` entry, as in 05 §5. No
`400`, no `401`, and no throttling header appeared on any of the 13 responses.
Write latency was 70–110 ms, as in 05.

---

## 1. The claim, captured verbatim (`p4-waiver-claim-with-drop.json`)

Request body:

```json
{
  "isLeagueManager": false,
  "teamId": 7,
  "type": "WAIVER",
  "scoringPeriodId": 6,
  "executionType": "EXECUTE",
  "items": [
    {"playerId": 9000030, "type": "ADD",  "toTeamId": 7},
    {"playerId": 9000023, "type": "DROP", "fromTeamId": 7}
  ]
}
```

Response, `200`:

```json
{
  "bidAmount": 0,
  "executionType": "EXECUTE",
  "id": "<uuid-1>",
  "isActingAsTeamOwner": false,
  "isLeagueManager": false,
  "isPending": true,
  "items": [
    {"fromLineupSlotId": -1, "fromTeamId": 0, "isKeeper": false, "overallPickNumber": 0,
     "playerId": 9000030, "toLineupSlotId": -1, "toTeamId": 7, "type": "ADD"},
    {"fromLineupSlotId": 20, "fromTeamId": 7, "isKeeper": false, "overallPickNumber": 0,
     "playerId": 9000023, "toLineupSlotId": -1, "toTeamId": 0, "type": "DROP"}
  ],
  "memberId": "{SWID-REDACTED}",
  "proposedDate": 1792045970518,
  "rating": 0,
  "scoringPeriodId": 6,
  "skipTransactionCounters": false,
  "status": "PENDING",
  "subOrder": 1,
  "teamId": 7,
  "type": "WAIVER"
}
```

- `-1` is ESPN's "no slot": the claimed player has none yet, and a dropped
  player has none after. The drop's `fromLineupSlotId: 20` was filled in by
  ESPN, so a caller never needs to send it.
- `subOrder: 1` on the claim and `0` on the cancel is plausibly the member's
  own claim priority. **[unverified]**: only one claim was ever live at once.
- The read-back right after showed the roster unchanged and one new
  `PENDING` row for team 7 in `mTransactions2`.

## 2. The cancel, captured verbatim (`p5-cancel-waiver-claim.json`)

Request body (what `EspnProvider.cancel_transaction` sends, unchanged):

```json
{
  "isLeagueManager": false,
  "teamId": 7,
  "type": "WAIVER",
  "scoringPeriodId": 6,
  "executionType": "CANCEL",
  "relatedTransactionId": "<uuid-1>"
}
```

Response, `200`: `"id": "<uuid-2>"`, `"executionType": "CANCEL"`,
`"status": "CANCELED"`, `"isPending": false`,
`"relatedTransactionId": "<uuid-1>"`, and the claim's two items echoed in full.

**Where the queue lives.** 30 seconds after the cancel, `mTransactions2` still
carried the claim's own row as `status: "PENDING"`, `isPending: true`, next to
the new `CANCELED` row. `view=mPendingTransactions` listed nothing. On the
second claim (p7c), that view held exactly that claim while it was live and
was empty right after its cancel (p7c-r). So the queue is
`mPendingTransactions`, or `mTransactions2` folded by `relatedTransactionId`.
A plain `isPending` filter reports withdrawn claims as live.
`docs/memory/a-cancelled-claim-still-reads-pending.md` records this. How a
*processed* claim's row reads was not observed.

## 3. Item-shape rules, one variation at a time

Each was sent as a `WAIVER` claim that would otherwise have gone pending, so
nothing could execute. The `FREEAGENT` repeat is in §6.

| Variation | Result | Capture |
|---|---|---|
| `ADD` without `toTeamId` | `409 TRAN_ITEM_TO_TEAM_ID_MISSING`, `Required field toTeamId missing from ADD TransactionItem` | p7a |
| `DROP` without `fromTeamId` | `409 TRAN_ITEM_FROM_TEAM_ID_MISSING`, `Required field fromTeamId missing from DROP TransactionItem` | p7b |
| `ADD` with `toLineupSlotId: 20` | `200 PENDING`, slot echoed | p7c |
| `FREEAGENT` with only a `DROP` | `409 TRAN_INVALID_ITEM_TYPE_COUNT`, `TransactionType FREEAGENT may only contain one item for MoveType: ADD.` | p6b |
| `ROSTER` with only a `DROP` | Passed shape validation; `409 TRAN_PLAYER_NOT_ON_TEAM` for the player named | p6c |

The `FREEAGENT` message names "one item for MoveType: ADD". It is the reason a
drop-only body fails, so it reads as "exactly one". Whether two adds fail the
same way, and whether `WAIVER` has the same rule, was not tried. The provider
refuses more than one add on both.

The order of checks, from p1 and p3: **availability before roster size**. A
`FREEAGENT` add of a waiver player on a full roster said "not a free agent",
and a `WAIVER` claim of the same player on the same roster said "too many
players".

## 4. Rejection vocabulary added by this brief

| HTTP | `details[0].type` | Message (synthetic labels) | Provoked by | `kind` | Capture |
|---|---|---|---|---|---|
| 409 | `TRAN_PLAYER_NOT_FREEAGENT` | `Player 9000030 is not a free agent` | `FREEAGENT` add of a player on waivers | `unknown` (§7) | p1 |
| 409 | `TRAN_ACQUISITION_BUDGET_NOT_SUPPORTED` | `This League does not support bids on Transactions.` | any `bidAmount` with FAAB off (`1`, `1000`) | `unknown` (§7) | p2, p6a |
| 409 | `TRAN_ROSTER_LIMIT_EXCEEDED_ONE` | `Too many players on roster (maximum 16).`; `resolution`: `You must drop at least 1 player to clear your roster for the incoming player.`; `metaData: {"teamid": "7"}` | claim with no drop on a full roster | **`roster_full`** | p3 |
| 409 | `TRAN_INVALID_ITEM_TYPE_COUNT` | `TransactionType FREEAGENT may only contain one item for MoveType: ADD.` | `FREEAGENT` with only a `DROP` | `unknown` (our bug) | p6b |
| 409 | `TRAN_PLAYER_NOT_ON_TEAM` | `Player 9000030 is not on team 7` | `DROP` of a player not on the team | **`already_dropped`** | p6c |
| 409 | `TRAN_PLAYER_NOT_AVAILABLE` | `<another team's player> is not available, currently on team <other>` | add of a player on another roster | `unknown` (§7) | p6d |
| 409 | `TRAN_ITEM_TO_TEAM_ID_MISSING` | `Required field toTeamId missing from ADD TransactionItem` | `ADD` without `toTeamId` | `unknown` (our bug) | p7a |
| 409 | `TRAN_ITEM_FROM_TEAM_ID_MISSING` | `Required field fromTeamId missing from DROP TransactionItem` | `DROP` without `fromTeamId` | `unknown` (our bug) | p7b |

The `_ONE` suffix on the roster-full type suggests siblings, perhaps
`_MANY`, for a transaction that overshoots by more than one. Only `_ONE` was
seen, so only `_ONE` is mapped.

## 5. Not observed, and why

| Reason | Why not | How to provoke it safely |
|---|---|---|
| budget exceeded (FAAB) | This league has no acquisition budget, so every bid is `TRAN_ACQUISITION_BUDGET_NOT_SUPPORTED` before any budget check. | Needs a league with `isUsingAcquisitionBudget: true`, which this one cannot become without a commissioner. A claim with `bidAmount` = budget + 1. **Still unverified.** |
| position limit | Team 7 holds 2 QBs against a limit of 4, and every other position is further from its limit. Reaching it needs two add/drop pairs, each dropping a real player to 24-hour waivers. | Once free agents exist: add QBs with drops until 4, then a 5th. Skipped as too many drops for a research probe. |
| roster full on a `FREEAGENT` add | Availability is checked first (§3), and no free agent existed. | §6 probe F1. Expected to match p3's type. |
| a processed claim's rows | Every claim was cancelled long before processing, by design. | Let one bidless claim on a worthless player process in the test league, then read both views. Not part of #108. |
| non-commissioner cross-team write (05 §6.1) | Explicitly out of scope for this task. | 05 §6.1. |

## 6. Not yet observed — waiting on the waiver pool to clear

**Unblocking condition:** the probe script's `pool` command shows
`FREEAGENT` > 0, after a waiver run at league hour 11 (Thursday 2026-10-15 at
the earliest). Check first, read-only:

```bash
uv run python scripts/probe_espn_add_drop.py --league test pool
uv run python scripts/probe_espn_add_drop.py --league test role       # re-derive the team
uv run python scripts/probe_espn_add_drop.py --league test readback f0
```

Then, one at a time with a read-back after each. `<FA>`, `<FA2>`, and `<FA3>`
are low-owned `FREEAGENT` players. `<BENCH>` is the least valuable bench
player. Every id is read fresh, never taken from this brief:

| # | Probe | Command | Expect | Drops |
|---|---|---|---|---|
| F1 | `FREEAGENT` add, no drop, full roster | `move f1-fa-add-no-drop --add <FA> --note "..."` | `409 TRAN_ROSTER_LIMIT_EXCEEDED_ONE` | 0 |
| F2 | `FREEAGENT` add + drop, executing | `move f2-fa-add-drop --add <FA> --drop <BENCH> --note "..."` | `200 EXECUTED`; `<BENCH>` to waivers | 1 |
| F3 | Standalone drop, executing, as `ROSTER` | `move f3-roster-drop --drop <FA> --note "..."` (the provider sends `ROSTER`) | `200 EXECUTED`; roster 15 | 2 |
| F4 | Bidless claim with no drop and room on the roster | `move f4-claim-no-drop --add <BENCH> --waiver --note "..."` | `200 PENDING` | — |
| F5 | Cancel it | `cancel f5-cancel --related <id from F4> --note "..."` | `200 CANCELED` | — |
| F6 | `FREEAGENT` `ADD` without `toTeamId` | `raw f6-fa-add-no-toTeamId --body '{"type":"FREEAGENT","executionType":"EXECUTE","items":[{"playerId":<FA2>,"type":"ADD"}]}' --note "..."` | `409 TRAN_ITEM_TO_TEAM_ID_MISSING`, as on `WAIVER` | — |
| F7 | `FREEAGENT` add with `toLineupSlotId: 20`, restoring 16 | `move f7-fa-add-to-bench --add <FA3> --add-slot BE --note "..."` | `200 EXECUTED`; roster 16 | — |

`move` runs the provider's own `move_players`, so F1–F5 and F7 exercise
exactly the code this PR ships. F2 to F7 end at roster 16, with `<FA3>` in
place of `<BENCH>`. Two drops in total, both to 24-hour waivers. A follow-up
must also re-run `redact` and extend §0 and §4 with what F1–F7 show.

## 7. What this means for #18 / #109

**Shipped in this PR** (`providers/espn.py`, with a unit test per change built
from these captures):

- `move_players` gains `waiver: bool = False`, also on the `WritingProvider`
  Protocol and `WritingFake`. `waiver=True` or a `bid` sends `WAIVER`, and
  `bidAmount` is sent **only** when `bid` is given.
- Drops only now sends `ROSTER`, not `FREEAGENT`.
- More than one add, or a claim with no add, is refused before sending as
  `CONFIG_INVALID` with `kind: "argument"`.
- `cancel_transaction` and `_move_item` docstrings now say "captured" rather
  than "community".
- `_TRAN_KINDS` gains the two confirmed mappings:

  ```python
  "TRAN_ROSTER_LIMIT_EXCEEDED_ONE": "roster_full",
  "TRAN_PLAYER_NOT_ON_TEAM": "already_dropped",
  ```

**Left as `unknown` on purpose**, with `provider_type` verbatim, and listed in
the `_TRAN_KINDS` docstring:

- `TRAN_ACQUISITION_BUDGET_NOT_SUPPORTED` is not `budget_exceeded`. The fix is
  "send no bid", not "bid less". #18 should pre-flight it from
  `settings.acquisitionSettings.isUsingAcquisitionBudget`, so the request is
  never sent.
- `TRAN_PLAYER_NOT_FREEAGENT` and `TRAN_PLAYER_NOT_AVAILABLE` both mean the
  player cannot be added that way. Their re-plans differ: file a claim
  instead, or pick another player. No existing kind fits either. **#18 should
  decide whether ADR-0010 gains a kind for player availability.** This research
  PR does not change the contract.
- The three item-shape reasons are bugs in a body we built, not a user's
  mistake, and the provider now cannot build those bodies.

**Still community shape**, unconfirmed by an *executing* request: the
`FREEAGENT` envelope's item shape (§6 F2) and an executing `ROSTER` drop (F3).
Both passed every validation they reached, and the code comments say so.

**For #109 (claims):**

- Read the queue from `view=mPendingTransactions`, or fold `mTransactions2` by
  `relatedTransactionId`. Never trust a row's own `PENDING` (§2).
- A cancel's `WriteResult` carries the **cancel's** id in
  `provider_transaction_id`. The claim's id is in
  `raw["relatedTransactionId"]`, so the journal should store both.
- A league's bid rule is a setting, not a guess. Read `isUsingAcquisitionBudget`
  and `minimumBid` before offering `--bid`.

**For #18 (pre-flight):**

- Roster size: refuse an add with no drop when the roster is at the size of
  `lineupSlotCounts` minus IR. The limit was 16 here, which is 9 starters plus
  7 bench, with the IR slot not counted.
- Availability: read the player's `status` (`FREEAGENT` or `WAIVERS`) first,
  and route a `WAIVERS` player to a claim.
- Timeout: a claim that times out must be resolved through
  `mPendingTransactions` before any retry (05 §8 item 1). That view is now
  known to be the right one.
