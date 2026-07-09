#!/usr/bin/env bash
# Realtime terminal monitor for a best-of-N parallel build.
#
# Redraws build_status.py every INTERVAL seconds until every worker reaches a
# terminal state (done/incomplete/error/stalled), then exits. Run it in its own
# terminal alongside `parallel_build.py <slug> --candidates N`.
#
# Usage:
#   ./monitor.sh [slug] [interval_seconds]
#   ./monitor.sh                       # slug=3dbenchystepfile, refresh 5s
#   ./monitor.sh my_part 3             # slug=my_part, refresh 3s
#
# Safe to start BEFORE the builds launch — it shows "(no candidates yet)" and
# keeps polling until workers appear. Ctrl-C to stop early.

set -u
SLUG="${1:-3dbenchystepfile}"
INTERVAL="${2:-5}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATUS="python3 $HERE/build_status.py $SLUG"

trap 'printf "\n[monitor stopped]\n"; exit 0' INT

start=$(date +%s)
while true; do
  # only clear when attached to a real terminal (keeps piped/captured output clean)
  [ -t 1 ] && clear
  elapsed=$(( $(date +%s) - start ))
  printf '\033[1m═══ parallel build · slug=%s · %s · elapsed %dm%02ds · refresh %ss ═══\033[0m\n\n' \
    "$SLUG" "$(date '+%H:%M:%S')" "$((elapsed/60))" "$((elapsed%60))" "$INTERVAL"

  $STATUS
  echo

  # best score so far across candidates
  $STATUS --json 2>/dev/null | python3 -c '
import sys, json
try:
    c = json.load(sys.stdin)["candidates"]
except Exception:
    sys.exit()
s = [x["score"] for x in c if x["score"] is not None]
done = sum(1 for x in c if x["state"] in ("done","incomplete","error","stalled"))
if c:
    tag = "\033[32m>= 95 reached\033[0m" if (s and max(s) >= 95) else ""
    print(f"best so far: {max(s):.1f} {tag}" if s else "best so far: (none scored yet)")
    print(f"finished: {done}/{len(c)}")
'

  if $STATUS --all-done 2>/dev/null; then
    printf '\n\033[1mALL WORKERS FINISHED.\033[0m\n'
    break
  fi
  sleep "$INTERVAL"
done
