# Live tests

Everything here is marked `@pytest.mark.live`, hits real ESPN with real
credentials, and is excluded from the default selection by `tests/conftest.py`.

```bash
ESPN_S2=... ESPN_SWID='{...}' uv run pytest -m live
```

Never put a live call in `tests/unit/` — `pytest-socket` blocks the network
there, and a cassette miss must fail loudly rather than quietly reaching ESPN.

Two live tests **write**. `test_lineup_set_live.py` swaps two players on the
session's own team and reverses the swap. `test_moves_live.py` adds the
least-owned outright free agent with a drop of the weakest unlocked bench
player, drops the added player again, and adds a second free agent to the
bench so the roster ends at its starting size (probe F7). That one is
**irreversible**: the bench player ends on 24-hour waivers and is not
restored. It skips while fewer than two players have ESPN status `FREEAGENT`. Both run only when `FANTASY_SPORTS_ESPN_WRITE_LEAGUE` is set **and
equals** `FANTASY_SPORTS_ESPN_LEAGUE`, and both must name the throwaway test
league, never a real one.
