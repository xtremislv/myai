"""scripts/rotate_vault_key.py: every secret stays readable under the new key."""
from __future__ import annotations

import base64
import os

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db.session import Base
from app.vault import aesgcm_vault
from app.vault.aesgcm_vault import AESGCMVault
from scripts import rotate_vault_key


def test_rotation_rewraps_every_secret_and_checks_it(monkeypatch):
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    old = base64.b64decode(config.get_settings().vault_master_key)
    new = os.urandom(32)
    with Session() as s:
        AESGCMVault(s).put("mail/finance", '{"password": "p"}')
        AESGCMVault(s).put("system/notes-key", "k")
        assert rotate_vault_key.rotate(s, old, new) == 2
        s.commit()
    with Session() as s:
        with pytest.raises(InvalidTag):
            AESGCMVault(s).get("mail/finance")  # the old key no longer opens it
        monkeypatch.setattr(aesgcm_vault, "_load_master_key", lambda: new)
        assert AESGCMVault(s).get("mail/finance") == '{"password": "p"}'
        assert AESGCMVault(s).get("system/notes-key") == "k"


def test_env_rewrite_changes_only_the_key_line(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\nVAULT_MASTER_KEY=old\nB=2\n")
    assert rotate_vault_key._rewrite_env(env, "NEW") == "A=1\nVAULT_MASTER_KEY=NEW\nB=2\n"
