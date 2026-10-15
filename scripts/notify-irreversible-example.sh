#!/bin/sh
# Example [notify] irreversible_command for fantasy-sports (#113).
#
# fantasy-sports runs this once per irreversible write (add, drop, claim) and
# writes the journal entry to standard input as one line of JSON. This script
# appends a single summary line to
#   $XDG_STATE_HOME/fantasy-sports/irreversible.log   (default ~/.local/state/...)
# in the form:
#   <recorded_at> <kind> <team_id> <league_id> <cost as compact JSON>
#
# Wire it up in ~/.config/fantasy-sports/config.toml:
#
#   [notify]
#   irreversible_command = "/path/to/scripts/notify-irreversible-example.sh"
#
# This is a template. Real delivery (a task in a tracker, a vault entry, a
# push notification) is a script of the owner's own, not this package's concern.
# The entry on stdin is already redacted; do not add credentials to what you log.
set -eu

# The log is an audit trail: keep it (and its directory) private to the owner.
umask 077

state="${XDG_STATE_HOME:-$HOME/.local/state}/fantasy-sports"
mkdir -p "$state"

python3 -c '
import json, sys
entry = json.load(sys.stdin)
print(
    entry["recorded_at"],
    entry["kind"],
    entry["team_id"],
    entry["league_id"],
    json.dumps(entry["cost"], separators=(",", ":"), sort_keys=True),
)
' >> "$state/irreversible.log"
