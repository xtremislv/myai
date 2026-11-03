"""Rotate the vault master key (VAULT_MASTER_KEY).

    scripts/start_native.sh status                 # 1. note what is running
    kill $(cat /var/log/ai-platform/gateway.pid) $(cat /var/log/ai-platform/scheduler.pid)
    .venv/bin/python -m scripts.rotate_vault_key   # 2. rotate (asks to confirm)
    scripts/start_native.sh                        # 3. start again

What it changes:
  * every vault secret's data key is re-wrapped under the new master key (the secrets
    themselves, the notes key that protects reminders and notifications, and mailbox logins
    are untouched and stay readable);
  * VAULT_MASTER_KEY in .env (the old file is kept as .env.bak-<time>, mode 600).
Everyone is signed out: their tokens carry their chat key sealed under the old master key.
Private chats are not affected: they are encrypted with each person's own key.

Order of operations, so a crash can't leave the database and .env on different keys: the new
.env is written beside the old one first, then every secret is re-wrapped and checked inside
one transaction, then the transaction commits, then the new .env replaces the old one. If the
script dies between the commit and the rename, .env.rotating holds the right key.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import os
import shutil
import sys
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings
from app.db.session import SessionLocal, VaultSecret

ROOT = Path(__file__).resolve().parent.parent


def _gateway_running() -> bool:
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=2)
        return True
    except Exception:  # noqa: BLE001
        return False


def _rewrite_env(env: Path, new_key: str) -> str:
    lines = env.read_text().splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.startswith("VAULT_MASTER_KEY=")]
    if len(hits) != 1:
        raise SystemExit(f"expected exactly one VAULT_MASTER_KEY line in {env}, found {len(hits)}")
    lines[hits[0]] = f"VAULT_MASTER_KEY={new_key}\n"
    return "".join(lines)


def rotate(session, old: bytes, new: bytes) -> int:
    """Re-wrap every data key and verify it opens under the new key. Does not commit."""
    rows = session.query(VaultSecret).all()
    for row in rows:
        dk = AESGCM(old).decrypt(row.key_nonce, row.wrapped_key, row.name.encode())
        nonce = os.urandom(12)
        row.wrapped_key = AESGCM(new).encrypt(nonce, dk, row.name.encode())
        row.key_nonce = nonce
    session.flush()
    for row in rows:  # every secret must still open, end to end
        dk = AESGCM(new).decrypt(row.key_nonce, row.wrapped_key, row.name.encode())
        AESGCM(dk).decrypt(row.nonce, row.ciphertext, row.name.encode())
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument("--new-key", help="base64 32-byte key (default: a fresh random one)")
    parser.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    parser.add_argument("--force", action="store_true", help="rotate even though the gateway is running")
    args = parser.parse_args()

    env = Path(args.env)
    old = base64.b64decode(get_settings().vault_master_key)
    new_b64 = args.new_key or base64.b64encode(os.urandom(32)).decode()
    new = base64.b64decode(new_b64)
    if len(old) != 32 or len(new) != 32:
        raise SystemExit("both keys must be 32 bytes, base64-encoded")
    if new == old:
        raise SystemExit("the new key is the same as the old one")
    if _gateway_running() and not args.force:
        raise SystemExit("the gateway is running: stop it and the scheduler first (see --help), or pass --force")
    if not args.yes:
        answer = input(f"Rotate the vault master key in {env} and the database? Everyone will be signed out. [y/N] ")
        if answer.strip().lower() != "y":
            raise SystemExit("cancelled")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = env.with_name(f"{env.name}.bak-{stamp}")
    staged = env.with_name(f"{env.name}.rotating")
    shutil.copy2(env, backup)
    os.chmod(backup, 0o600)
    staged.write_text(_rewrite_env(env, new_b64))
    os.chmod(staged, 0o600)

    with SessionLocal() as session:
        try:
            count = rotate(session, old, new)
            session.commit()
        except Exception:
            session.rollback()
            staged.unlink(missing_ok=True)
            print("rotation failed; nothing was changed", file=sys.stderr)
            raise
    os.replace(staged, env)
    print(f"re-wrapped {count} secret(s); {env} now holds the new key (old file: {backup.name}).")
    print("Start the services again: scripts/start_native.sh. Everyone needs to sign in again.")
    print("Keep a copy of the new key with your backups: without it the vault can't be opened.")


if __name__ == "__main__":
    main()
