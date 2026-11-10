"""Erase a person's data on request.

Removes their private chats (encrypted checkpoints and the chat list), attachments, files,
reminders, notifications, mailbox login, settings, and the work requests and approvals they
made (which hold their message text). With delete_account the account goes too.

Kept on purpose: NSFW reports (evidence for HR) and the audit log (who did what to which
resource, never message text).
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import Approval, ChatHistory, ChatThread, DepartmentTask, User
from app.mail.connector import secret_name
from app.reminders import service as reminders
from app.storage import userfiles
from app.vault.aesgcm_vault import AESGCMVault

logger = logging.getLogger(__name__)

CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


def _remove_object(key: str) -> None:
    from app.storage.objects import remove_expired_object

    remove_expired_object(key)


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def erase_user(session: Session, user_id: str, *, delete_account: bool = False, remove_object=_remove_object) -> dict:
    counts: dict[str, int] = {}

    # Private and work conversation checkpoints: "private:<id>:<thread>" and "<id>:<thread>".
    if session.get_bind().dialect.name == "postgresql":
        uid = _like_escape(user_id)
        for table in CHECKPOINT_TABLES:
            exists = session.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar()
            if exists is None:
                continue
            result = session.execute(
                text(f"DELETE FROM {table} WHERE thread_id LIKE :a ESCAPE '\\' OR thread_id LIKE :b ESCAPE '\\'"),  # noqa: S608 - fixed table names
                {"a": f"private:{uid}:%", "b": f"{uid}:%"},
            )
            counts[table] = result.rowcount or 0
        session.commit()

    attachments = session.query(ChatHistory).filter(ChatHistory.owner_id == user_id).all()
    for row in attachments:
        for artifact in row.artifacts or []:
            try:
                remove_object(artifact["ref"])
            except Exception:  # noqa: BLE001 - already gone
                pass
        session.delete(row)
    counts["attachments"] = len(attachments)
    counts["chats"] = session.query(ChatThread).filter(ChatThread.owner_id == user_id).delete()
    counts["approvals"] = session.query(Approval).filter(Approval.subject_id == user_id).delete()
    counts["tasks"] = session.query(DepartmentTask).filter(DepartmentTask.requester_id == user_id).delete()
    session.commit()

    counts["files"] = userfiles.erase_owner(session, user_id)
    from app.browsing import logins

    logins.erase_owner(session, user_id, remove_object)
    from app import wallet

    counts["saved_details"] = wallet.erase_owner(session, user_id)
    counts["reminders_and_notifications"] = reminders.erase_owner(session, user_id)
    AESGCMVault(session).delete(secret_name("personal", user_id))
    from app import devices

    counts["devices"] = devices.erase_owner(session, user_id)

    user = session.get(User, user_id)
    if user is not None:
        if delete_account:
            session.delete(user)
        else:
            user.prefs = {}
            user.last_digest_at = None
            # Without these nothing encrypted for them before can be opened, even from a backup
            # restored later: a fresh key is made at their next password change.
            user.chat_key_salt = user.chat_key_nonce = user.wrapped_chat_key = None
            user.recovery_salt = user.recovery_nonce = user.wrapped_recovery_key = None
        session.commit()
    counts["account_deleted"] = int(delete_account and user is not None)
    logger.info("erased the data of one user")
    return counts
