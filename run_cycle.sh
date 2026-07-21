#!/usr/bin/env bash
# One segment of the five-hour worker: scan jobs every ~90 seconds and long-poll
# Telegram for proposal button taps, then return so the workflow can persist state.
set -u
export SERVE_SECONDS="${CYCLE_SECONDS:-2250}"
python notifier.py || echo "cycle exited non-zero (continuing)"
