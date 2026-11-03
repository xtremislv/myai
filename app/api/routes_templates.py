"""Company document templates: list, upload, download, delete. The ACL decides (template:<scope>/*):
departments manage their own, everyone reads global ones, the admin manages all. Personal
templates are files in My files (templates/ folder)."""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.acl.models import Subject
from app.api.deps import current_subject
from app.db.session import get_session
from app.departments.packs import DEFAULT_DEPARTMENTS
from app.documents import engine, store

router = APIRouter(prefix="/v1/templates", tags=["templates"])


class TemplateOut(BaseModel):
    id: str
    name: str
    scope: str
    description: str
    filename: str
    fields: list[str]
    updated_at: dt.datetime | None = None


def _scopes(subject: Subject) -> list[str]:
    if "admin" in subject.roles:
        return [d.name for d in DEFAULT_DEPARTMENTS] + ["global"]
    return [r for r in subject.roles if r != "admin"] + ["global"]


def _out(row) -> TemplateOut:
    try:
        names = engine.fields(row.filename, row.content) if engine.kind_of(row.filename) != "pdf" else []
    except engine.TemplateError:
        names = []
    return TemplateOut(id=row.id, name=row.name, scope=row.scope, description=row.description,
                       filename=row.filename, fields=names, updated_at=row.updated_at)


@router.get("", response_model=list[TemplateOut])
def list_templates(subject: Subject = Depends(current_subject), session: Session = Depends(get_session)) -> list[TemplateOut]:
    return [_out(r) for r in store.visible(session, subject, _scopes(subject))]


@router.post("", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
def upload_template(
    file: UploadFile, name: str = Form(max_length=128), scope: str = Form(max_length=64),
    description: str = Form(default="", max_length=512), subject: Subject = Depends(current_subject),
    session: Session = Depends(get_session),
) -> TemplateOut:
    from app.api.routes_chat import safe_filename

    data = file.file.read(store.MAX_BYTES + 1)
    filename = safe_filename(file.filename)
    if engine.kind_of(filename) == "text" and not filename.lower().endswith(engine.TEXT_SUFFIXES):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "templates can be Word (.docx), PDF (as an example) or text: .md .txt .html .csv .json")
    try:
        engine.fields(filename, data)  # rejects broken placeholders and unreadable Word files early
        row = store.save(session, subject, scope.strip().lower(), name, filename, data, description)
    except (engine.TemplateError, ValueError) as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    except Exception as e:  # noqa: BLE001 - e.g. a corrupt .docx
        if isinstance(e, PermissionError):
            raise
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "that file can't be read as a template") from None
    return _out(row)


@router.get("/{template_id}")
def download_template(template_id: str, subject: Subject = Depends(current_subject),
                      session: Session = Depends(get_session)) -> Response:
    row = store.get(session, subject, template_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "template not found")
    return Response(content=row.content, media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.filename)}", "X-Content-Type-Options": "nosniff",
    })


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(template_id: str, subject: Subject = Depends(current_subject),
                    session: Session = Depends(get_session)) -> None:
    if not store.delete(session, subject, template_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "template not found")
