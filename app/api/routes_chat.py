"""Private chat: a conversation between one user and the AI.

No endpoint exposes these conversations, and they have no tools, audit rows or
content logging. Their text is encrypted at rest with the user's own key
(app/vault/user_keys.py). The only thing that ever leaves this channel is a single
message flagged as explicit or illegal, reported to hr and admin.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.acl.models import Subject
from app.api.deps import chat_key, current_subject
from app.db.session import get_session
from app.orchestrator import execution
from app.request_manager import guards

router = APIRouter(prefix="/v1/chat", tags=["chat"])


class ChatIn(BaseModel):
    message: str
    thread_id: str = Field(min_length=1, max_length=128)  # conversation id, private to the caller
    # "single" = one agent loop; "team" = planner -> researcher -> writer.
    mode: Literal["single", "team"] = "single"


class ChatOut(BaseModel):
    thread_id: str
    reply: str
    mode: str
    outcome: Literal["ok", "flagged_nsfw"] = "ok"
    reason: str = ""


@router.post("", response_model=ChatOut)
def chat(
    body: ChatIn,
    subject: Subject = Depends(current_subject),
    key: bytes = Depends(chat_key),
    session: Session = Depends(get_session),
) -> ChatOut:
    flagged, why = guards.is_nsfw(body.message)
    if flagged:
        guards.report_nsfw(session, subject.id, body.message)
        return ChatOut(
            thread_id=body.thread_id, reply="", mode=body.mode, outcome="flagged_nsfw",
            reason=f"{why}; reported to hr, admin",
        )
    reply = execution.run(subject, body.message, body.thread_id, body.mode, None, chat_key=key)
    return ChatOut(thread_id=body.thread_id, reply=reply, mode=body.mode)
