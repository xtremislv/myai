"""Company document templates in the database, checked by the ACL.

Resource: template:<scope>/<name>, where scope is "global" or a department. Grants in
app/departments/packs.py: each department manages its own; everyone at work reads global ones;
the admin manages all.
"""
from __future__ import annotations

import re
import uuid

from sqlalchemy.orm import Session

from app.acl.engine import ACLEngine
from app.acl.models import Action, Resource, Subject
from app.db.session import DocTemplate

MAX_BYTES = 5 * 1024 * 1024
_NAME = re.compile(r"^[\w][\w \-.()]{0,127}$")


def _resource(scope: str, name: str) -> Resource:
    return Resource("template", f"{scope}/{name}")


def clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not _NAME.match(name):
        raise ValueError("a template name is 1-128 letters, digits, spaces, dots, dashes or brackets")
    return name


def visible(session: Session, subject: Subject, scopes: list[str]) -> list[DocTemplate]:
    """Templates in these scopes that the subject may read, in the order of `scopes`, then by name."""
    acl = ACLEngine(session)
    rows = session.query(DocTemplate).filter(DocTemplate.scope.in_(scopes)).all()
    rows.sort(key=lambda r: (scopes.index(r.scope), r.name.lower()))  # own department's first
    return [r for r in rows if acl.check(subject, Action.READ, _resource(r.scope, r.name)).allowed]


def find(session: Session, subject: Subject, name: str, scopes: list[str]) -> DocTemplate | None:
    """By name (case-insensitive, with or without the file extension), first matching scope wins."""
    # Tolerate a name copied with its listing details, e.g. "Offer letter [hr, docx]".
    wanted = re.sub(r"\s*[\[(].*$", "", name.strip()).strip().lower() or name.strip().lower()
    for scope in scopes:
        for row in visible(session, subject, [scope]):
            stem = row.filename.rsplit(".", 1)[0].lower()
            if wanted in (row.name.lower(), row.filename.lower(), stem):
                return row
    return None


def save(session: Session, subject: Subject, scope: str, name: str, filename: str, content: bytes,
         description: str = "") -> DocTemplate:
    name = clean_name(name)
    if len(content) > MAX_BYTES:
        raise ValueError("templates are limited to 5 MB")
    ACLEngine(session).enforce(subject, Action.WRITE, _resource(scope, name))
    row = session.query(DocTemplate).filter(DocTemplate.scope == scope, DocTemplate.name == name).first()
    if row is None:
        row = DocTemplate(id=uuid.uuid4().hex, scope=scope, name=name)
        session.add(row)
    row.filename = filename
    row.content = content
    row.description = description[:512]
    row.created_by = subject.id
    session.commit()
    return row


def get(session: Session, subject: Subject, template_id: str) -> DocTemplate | None:
    row = session.get(DocTemplate, template_id)
    if row is None or not ACLEngine(session).check(subject, Action.READ, _resource(row.scope, row.name)).allowed:
        return None
    return row


def delete(session: Session, subject: Subject, template_id: str) -> bool:
    row = session.get(DocTemplate, template_id)
    if row is None:
        return False
    ACLEngine(session).enforce(subject, Action.DELETE, _resource(row.scope, row.name))
    session.delete(row)
    session.commit()
    return True
