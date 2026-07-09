#!/usr/bin/env bash
# One cycle of the notifier: run a check every CHECK_INTERVAL seconds for CYCLE_SECONDS
# total, then return so the workflow can persist dedup state to cache. $SECONDS resets
# per bash invocation, so each cycle step gets a fresh CYCLE_SECONDS window.
set -u
CYCLE_SECONDS="${CYCLE_SECONDS:-2400}"
CHECK_INTERVAL="${CHECK_INTERVAL:-90}"
i=0
while [ "$SECONDS" -lt "$CYCLE_SECONDS" ]; do
  i=$((i + 1))
  echo "::group::check #$i (t=${SECONDS}s)"
  python notifier.py || echo "check #$i failed, continuing"
  echo "::endgroup::"
  [ "$SECONDS" -lt "$CYCLE_SECONDS" ] && sleep "$CHECK_INTERVAL"
done
echo "cycle finished: $i checks in ${SECONDS}s"
