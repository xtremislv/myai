"""App-managed vault using AES-256-GCM envelope encryption (SQLCipher/age-style
confidentiality, no external daemon).

Scheme per secret:
  1. generate random 32-byte data key (DK)
  2. ciphertext = AES-GCM(DK, plaintext, nonce)
  3. wrapped_key = AES-GCM(master_key, DK, key_nonce)
Only wrapped_key + nonces + ciphertext are persisted. The master key lives in
env / OS keyring, never in the database.
"""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import VaultSecret


class VaultError(RuntimeError):
    pass


def _load_master_key() -> bytes:
    raw = get_settings().vault_master_key
    if not raw:
        raise VaultError(
            "VAULT_MASTER_KEY is not set. Generate one with:\n"
            '  python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"'
        )
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise VaultError("VAULT_MASTER_KEY must decode to exactly 32 bytes.")
    return key


class AESGCMVault:
    """Postgres-backed envelope vault. Pass a SQLAlchemy Session per call."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._master = _load_master_key()

    def put(self, name: str, value: str) -> None:
        dk = AESGCM.generate_key(bit_length=256)
        payload_nonce = os.urandom(12)
        ciphertext = AESGCM(dk).encrypt(payload_nonce, value.encode(), name.encode())

        key_nonce = os.urandom(12)
        wrapped = AESGCM(self._master).encrypt(key_nonce, dk, name.encode())

        row = self._session.get(VaultSecret, name)
        if row is None:
            row = VaultSecret(name=name)
            self._session.add(row)
        row.wrapped_key = wrapped
        row.nonce = payload_nonce
        row.ciphertext = ciphertext
        row.key_nonce = key_nonce
        self._session.commit()

    def get(self, name: str) -> str | None:
        row = self._session.get(VaultSecret, name)
        if row is None:
            return None
        dk = AESGCM(self._master).decrypt(row.key_nonce, row.wrapped_key, name.encode())
        plaintext = AESGCM(dk).decrypt(row.nonce, row.ciphertext, name.encode())
        return plaintext.decode()

    def delete(self, name: str) -> None:
        row = self._session.get(VaultSecret, name)
        if row is not None:
            self._session.delete(row)
            self._session.commit()

    def list_names(self) -> list[str]:
        return [r.name for r in self._session.query(VaultSecret.name).all()]

    def rotate_master_key(self, new_master_b64: str) -> int:
        """Re-wrap every data key under a new master key (payloads untouched)."""
        new_master = base64.b64decode(new_master_b64)
        if len(new_master) != 32:
            raise VaultError("New master key must decode to 32 bytes.")
        count = 0
        for row in self._session.query(VaultSecret).all():
            dk = AESGCM(self._master).decrypt(row.key_nonce, row.wrapped_key, row.name.encode())
            key_nonce = os.urandom(12)
            row.wrapped_key = AESGCM(new_master).encrypt(key_nonce, dk, row.name.encode())
            row.key_nonce = key_nonce
            count += 1
        self._session.commit()
        self._master = new_master
        return count
