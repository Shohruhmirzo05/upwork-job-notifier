"""Authenticated encryption for the relay receipt cache in a public repository."""
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet


def transform(action, path=Path("official-delivery.sqlite3")):
    encrypted = Path(str(path) + ".enc")
    key = os.environ.get("OFFICIAL_RELAY_STATE_KEY", "")
    if not key:
        raise SystemExit("Delivery receipt encryption key is missing")
    cipher = Fernet(key.encode())
    if action == "check":
        return
    if action == "encrypt":
        if not path.exists():
            return
        encrypted.write_bytes(cipher.encrypt(path.read_bytes()))
        encrypted.chmod(0o600)
    elif action == "decrypt":
        # Authentication failure stops delivery; never silently create a new ledger.
        path.write_bytes(cipher.decrypt(encrypted.read_bytes()))
        path.chmod(0o600)
    else:
        raise SystemExit("Use encrypt or decrypt")


if __name__ == "__main__":
    transform(sys.argv[1])
