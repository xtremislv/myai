"""The person's live view of their agent's browser: see it, click, type, scroll, open a page.

Used when the agent hands over (ask_person_to_browse) for what it must never do itself: signing
in, OTPs, card or UPI details, choosing how to pay. What the person types goes to the page and
nowhere else: it isn't logged, stored or shown to the agent (the agent's page snapshot hides
values of password, card and OTP fields).

Scopes: "private" (the person's own browser, logins saved with their key) or one of their
departments (the browser their work requests use there).
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.acl.models import Subject
from app.api.deps import chat_key_from_token, current_subject
from app.browsing import logins
from app.browsing.browser import BrowserError, browser
from app.browsing.netguard import Blocked

router = APIRouter(prefix="/v1/browser", tags=["browser"])
_bearer = HTTPBearer(auto_error=True)


class StateOut(BaseModel):
    open: bool
    url: str = ""
    title: str = ""


class ActIn(BaseModel):
    action: Literal["open", "click", "type", "key", "scroll", "back"]
    x: int = 0
    y: int = 0
    text: str = Field(default="", max_length=500)
    url: str = Field(default="", max_length=2000)


class SecretIn(BaseModel):
    ref: int
    value: str = Field(min_length=1, max_length=500)
    site: str = Field(max_length=255)  # the site the agent asked on: the page must still be there


class CloseIn(BaseModel):
    forget: bool = False  # also drop the saved logins (sign out of every site)


def _scope_key(scope: str, subject: Subject, creds: HTTPAuthorizationCredentials) -> tuple[str, bytes | None]:
    if scope == "private":
        return logins.session_key(subject.id, scope), chat_key_from_token(creds)
    if scope in subject.roles:  # a department's browser (its saved sign-ins) is for its members
        return logins.session_key(subject.id, scope), None
    raise HTTPException(status.HTTP_403_FORBIDDEN, "you are not in that department")


@router.get("/{scope}", response_model=StateOut)
def state(scope: str, subject: Subject = Depends(current_subject), creds=Depends(_bearer)) -> StateOut:
    key, _ = _scope_key(scope, subject, creds)
    b = browser()
    if not b.has(key):
        return StateOut(open=False)
    try:
        _, url, title = b.call(b.screen, key)
    except BrowserError:
        return StateOut(open=False)
    return StateOut(open=True, url=url, title=title[:200])


@router.get("/{scope}/screen")
def screen(scope: str, subject: Subject = Depends(current_subject), creds=Depends(_bearer)) -> Response:
    key, _ = _scope_key(scope, subject, creds)
    b = browser()
    if not b.has(key):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no browser is open")
    try:
        png, url, _ = b.call(b.screen, key)
    except BrowserError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
    return Response(png, media_type="image/jpeg", headers={"Cache-Control": "no-store", "X-Page-Url": url[:500]})


@router.post("/{scope}/act", response_model=StateOut)
def act(scope: str, body: ActIn, subject: Subject = Depends(current_subject), creds=Depends(_bearer)) -> StateOut:
    key, key_for_logins = _scope_key(scope, subject, creds)
    b = browser()
    if body.action == "open" or not b.has(key):
        logins.open_session(subject.id, scope, key_for_logins)
    try:
        url = b.call(b.human, key, body.action, body.x, body.y, body.text, body.url)
    except (BrowserError, Blocked) as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    # Keep the login the person just made, so the agent (and next visit) find it signed in.
    logins.persist(subject.id, scope, key_for_logins, force=True)
    return StateOut(open=True, url=url)


@router.post("/{scope}/close", status_code=status.HTTP_204_NO_CONTENT)
def close(scope: str, body: CloseIn, subject: Subject = Depends(current_subject), creds=Depends(_bearer)) -> None:
    key, key_for_logins = _scope_key(scope, subject, creds)
    b = browser()
    if b.has(key) and not body.forget:
        logins.persist(subject.id, scope, key_for_logins, force=True)
    if b.has(key):
        b.call(b.close, key)
    if body.forget:
        logins.forget(subject.id, scope)


@router.post("/{scope}/secret", response_model=StateOut)
def secret(scope: str, body: SecretIn, subject: Subject = Depends(current_subject), creds=Depends(_bearer)) -> StateOut:
    """The person answers the agent's secure prompt (a password, CVV or OTP): typed into that one
    field if the page is still on the same site. Not logged, stored, or shown to the agent."""
    from app.wallet import site_of

    key, key_for_logins = _scope_key(scope, subject, creds)
    b = browser()
    if not b.has(key):
        raise HTTPException(status.HTTP_409_CONFLICT, "the browser closed; ask the assistant to open the page again")

    def same_site(url: str) -> str | None:
        return None if site_of(url) == body.site else f"the page moved to {site_of(url)}, so nothing was typed"

    try:
        b.call(b.fill_value, key, body.ref, body.value, same_site)
    except BrowserError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
    logins.persist(subject.id, scope, key_for_logins, force=True)
    return StateOut(open=True, url=b.call(b.url_of, key))
