#!/bin/bash
# Keeps the Mac-side logs from growing forever: launchd's jobs (Ollama, Glances, the backup...)
# append to ~/Library/Logs/*.log and nothing rotates them. A log past 10 MB keeps its last 2 MB
# in <name>.log.1 and starts over. Emptied in place rather than moved, so a running job keeps
# writing to the same file. Run nightly by backup.sh.
set -euo pipefail
for f in ~/Library/Logs/*.log; do
  [[ -f $f ]] || continue
  (( $(stat -f %z "$f") > 10 * 1024 * 1024 )) || continue
  tail -c $((2 * 1024 * 1024)) "$f" >"$f.1"
  : >"$f"
done
