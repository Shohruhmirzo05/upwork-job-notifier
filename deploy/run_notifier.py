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


def load_environment(path: Path = ENV_PATH) -> None:
    """Use the bot's existing server credentials without printing or exporting them."""
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise RuntimeError(f"{path} must contain a JSON object")

    for name, value in values.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise RuntimeError(f"{path} must contain string keys and values")
    os.environ.update(values)


def main() -> None:
    load_environment()

    runpy.run_path("notifier.py", run_name="__main__")


if __name__ == "__main__":
    main()
