"""Per-user encryption keys for private chat.

Each user has a random 256-bit key. It is stored only wrapped by a key derived from
their password, so the database, backups and the vault master key cannot open it.

Between requests the unwrapped key travels inside the user's JWT, sealed under a
key derived from the master key. The server therefore keeps no usable copy at rest:
opening a user's chats needs that user's password or a live token of theirs.

A key is first created when a user sets their own password. The admin chooses the
initial password, so a key wrapped by it would be readable by the admin.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer

from app.config import get_settings

_NONCE_BYTES = 12
_SCRYPT = {"n": 2**15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}
_CIPHER_NAME = "aes-256-gcm"


def _password_key(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)


def _wrap(user, password: str, key: bytes) -> None:
    user.chat_key_salt = os.urandom(16)
    user.chat_key_nonce = os.urandom(_NONCE_BYTES)
    user.wrapped_chat_key = AESGCM(_password_key(password, user.chat_key_salt)).encrypt(
        user.chat_key_nonce, key, user.id.encode()
    )


def provision(user, password: str) -> bytes:
    """Create this user's key, wrapped by their password."""
    key = os.urandom(32)
    _wrap(user, password, key)
    return key


def unlock(user, password: str) -> bytes:
    """Unwrap the key. Raises cryptography's InvalidTag if the password is wrong."""
    return AESGCM(_password_key(password, user.chat_key_salt)).decrypt(
        user.chat_key_nonce, user.wrapped_chat_key, user.id.encode()
    )


def rewrap(user, old_password: str, new_password: str) -> bytes:
    """Change the wrapping password. The key, and so all existing chats, stay the same."""
    key = unlock(user, old_password)
    _wrap(user, new_password, key)
    return key


# ---- sealing the key inside a token ----
def _token_cipher() -> AESGCM:
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"chat-key-token").derive(
        base64.b64decode(get_settings().vault_master_key)
    )
    return AESGCM(derived)


def seal_for_token(key: bytes, subject_id: str) -> str:
    nonce = os.urandom(_NONCE_BYTES)
    sealed = _token_cipher().encrypt(nonce, key, subject_id.encode())
    return base64.urlsafe_b64encode(nonce + sealed).decode()


def open_from_token(sealed: str, subject_id: str) -> bytes:
    """Raises if the value was tampered with or was sealed for another subject."""
    raw = base64.urlsafe_b64decode(sealed)
    return _token_cipher().decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], subject_id.encode())


# ---- encrypting checkpoints ----
class _ChatCipher:
    """LangGraph's CipherProtocol over AES-GCM with one user's key."""

    def __init__(self, key: bytes) -> None:
        self._aes = AESGCM(key)

    def encrypt(self, plaintext: bytes) -> tuple[str, bytes]:
        nonce = os.urandom(_NONCE_BYTES)
        return _CIPHER_NAME, nonce + self._aes.encrypt(nonce, plaintext, None)

    def decrypt(self, ciphername: str, ciphertext: bytes) -> bytes:
        if ciphername != _CIPHER_NAME:
            raise ValueError(f"unknown cipher {ciphername}")
        return self._aes.decrypt(ciphertext[:_NONCE_BYTES], ciphertext[_NONCE_BYTES:], None)


def checkpoint_serde(key: bytes) -> EncryptedSerializer:
    return EncryptedSerializer(_ChatCipher(key))
