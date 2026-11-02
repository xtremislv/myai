"""Chat endpoint that drives the LangGraph orchestrator."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.acl.models import Subject
from app.api.deps import current_subject
from app.orchestrator.execution import scoped_thread
from app.orchestrator.graph import compiled_graph
from app.orchestrator.multi_graph import compiled_team_graph

router = APIRouter(prefix="/v1/chat", tags=["chat"])


class ChatIn(BaseModel):
    message: str
    thread_id: str = Field(min_length=1, max_length=128)  # conversation id, private to the caller
    # "single" = one agent + tools loop; "team" = planner -> researcher -> writer.
    mode: Literal["single", "team"] = "single"


class ChatOut(BaseModel):
    thread_id: str
    reply: str
    mode: str


@router.post("", response_model=ChatOut)
def chat(body: ChatIn, subject: Subject = Depends(current_subject)) -> ChatOut:
    config = {"configurable": {"thread_id": scoped_thread(subject.id, body.thread_id)}}
    initial = {
        "messages": [{"role": "user", "content": body.message}],
        "subject_id": subject.id,
        "subject_roles": list(subject.roles),
        "scratchpad": {},
    }
    graph_cm = compiled_team_graph() if body.mode == "team" else compiled_graph()
    with graph_cm as graph:
        final = graph.invoke(initial, config=config)
    reply = ""
    for msg in reversed(final.get("messages", [])):
        if msg.get("role") == "assistant":
            reply = msg.get("content", "")
            break
    return ChatOut(thread_id=body.thread_id, reply=reply, mode=body.mode)
