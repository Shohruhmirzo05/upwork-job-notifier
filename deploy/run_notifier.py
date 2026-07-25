#!/usr/bin/env python3
"""Load the server-managed environment and start the notifier.

The environment is JSON instead of a shell file so API keys and URLs do not
need shell escaping. The file is installed outside the repository with
root:upwork-notifier ownership and 0640 permissions.
"""

from __future__ import annotations

import json
import os
import runpy
from pathlib import Path


ENV_PATH = Path("/etc/upwork-notifier/env.json")


def main() -> None:
    values = json.loads(ENV_PATH.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise RuntimeError(f"{ENV_PATH} must contain a JSON object")

    for name, value in values.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise RuntimeError(f"{ENV_PATH} must contain string keys and values")
        os.environ[name] = value

    runpy.run_path("notifier.py", run_name="__main__")


if __name__ == "__main__":
    main()
