# Budget-aware waiver guidance

The `add` preflight now suggests `--bid N` only when league settings confirm an acquisition budget is enabled. Leagues without a budget, and providers that do not report the setting, receive a plain `claim` hint instead. Also corrected the stale `cancel-claim` command name in the `claim` docstring.

This keeps the suggested command compatible with the league settings already read during preflight and avoids directing users into a known provider rejection.
