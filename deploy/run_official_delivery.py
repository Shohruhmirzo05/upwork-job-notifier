#!/usr/bin/env python3
"""Relay an approved result batch using the existing server bot configuration.

Run as upwork-notifier on the bot host. This is not an Upwork client and cannot
search Upwork, submit proposals or extract the host app's plugin credentials.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deploy.run_notifier import load_environment


def main():
    load_environment()
    os.chdir(ROOT)
    os.environ.setdefault("BRIDGE_STATE_PATH", "/var/lib/upwork-notifier/official-delivery.sqlite3")
    from official_bridge import main as deliver
    deliver()


if __name__ == "__main__":
    main()
