# Live tests

Everything here is marked `@pytest.mark.live`, hits real ESPN with real
credentials, and is excluded from the default selection by `tests/conftest.py`.

```bash
ESPN_S2=... ESPN_SWID='{...}' uv run pytest -m live
```

Never put a live call in `tests/unit/` — `pytest-socket` blocks the network
there, and a cassette miss must fail loudly rather than quietly reaching ESPN.

`test_lineup_set_live.py` is the one live test that **writes**: it swaps two
players on the session's own team and reverses the swap. It runs only when
`FANTASY_SPORTS_ESPN_WRITE_LEAGUE` is set **and equals**
`FANTASY_SPORTS_ESPN_LEAGUE`, and both must name the throwaway test league,
never a real one.
