#!/usr/bin/env bash
# One cycle: run the notifier in SERVE mode for CYCLE_SECONDS (it internally scans jobs
# every ~90s AND long-polls Telegram for "Generate Proposal" button taps), then return so
# the workflow can persist state to cache.
set -u
export SERVE_SECONDS="${CYCLE_SECONDS:-2400}"
python notifier.py || echo "cycle exited non-zero (continuing)"
