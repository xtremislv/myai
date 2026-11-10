"""The phone app's sign-in (device refresh tokens), push wake-ups and app config."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import devices, push
from app.auth.security import decode_token, issue_token
from app.db.session import Base, DeviceSession, User, get_session
from app.departments.registry import seed_departments
from app.main import app
from app.reminders import service
from app.vault import user_keys
from app.vault.aesgcm_vault import AESGCMVault

INITIAL = "initial-from-admin"
MINE = "my-own-long-password"
ADMIN = {"authorization": f"Bearer {issue_token('root', 'root', ['admin'])}"}


@pytest.fixture
def api(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        seed_departments(s)

    def _get_session():
        with Session() as s:
            yield s

    sent: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(push, "_dispatch", lambda account, tokens, data: sent.append((tokens, data)))
    app.dependency_overrides[get_session] = _get_session
    client = TestClient(app)
    assert client.post(
        "/v1/auth/users", headers=ADMIN, json={"username": "alice", "password": INITIAL, "roles": ["hr"]}
    ).status_code == 201
    yield SimpleNamespace(client=client, Session=Session, sent=sent)
    app.dependency_overrides.clear()


def _auth(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


def _set_own_password(api) -> None:
    token = api.client.post("/v1/auth/login", json={"username": "alice", "password": INITIAL}).json()["access_token"]
    r = api.client.post("/v1/auth/change-password", headers=_auth(token),
                        json={"old_password": INITIAL, "new_password": MINE})
    assert r.status_code == 200


def _device_login(api, password=MINE, **extra):
    return api.client.post("/v1/auth/device/login",
                           json={"username": "alice", "password": password, "name": "Pixel", **extra})


def test_a_phone_signs_in_and_its_token_opens_private_chat(api):
    _set_own_password(api)
    r = _device_login(api)
    assert r.status_code == 200
    out = r.json()
    assert out["device_id"] and out["refresh_token"] and not out["must_change_password"]
    assert "ck" in decode_token(out["access_token"])
    assert api.client.get("/v1/chat/threads", headers=_auth(out["access_token"])).status_code == 200


def test_wrong_password_is_refused(api):
    _set_own_password(api)
    assert _device_login(api, password="nope-nope-nope").status_code == 401


def test_refresh_rotates_the_token_and_keeps_the_chat_key(api):
    _set_own_password(api)
    first = _device_login(api).json()
    r = api.client.post("/v1/auth/device/refresh",
                        json={"device_id": first["device_id"], "refresh_token": first["refresh_token"]})
    assert r.status_code == 200
    second = r.json()
    assert second["refresh_token"] != first["refresh_token"]
    with api.Session() as s:
        user = s.get(User, decode_token(second["access_token"])["sub"])
        key = user_keys.unlock(user, MINE)
    sealed = decode_token(second["access_token"])["ck"]
    assert user_keys.open_from_token(sealed, user.id) == key


def test_the_previous_token_works_once_after_a_lost_answer_then_never(api):
    _set_own_password(api)
    first = _device_login(api).json()
    body = {"device_id": first["device_id"], "refresh_token": first["refresh_token"]}
    lost = api.client.post("/v1/auth/device/refresh", json=body).json()  # the phone never got this
    retry = api.client.post("/v1/auth/device/refresh", json=body)
    assert retry.status_code == 200
    # Now neither the first nor the lost one is valid; only the newest.
    assert api.client.post("/v1/auth/device/refresh", json=body).status_code == 401
    assert api.client.post("/v1/auth/device/refresh", json={
        "device_id": first["device_id"], "refresh_token": lost["refresh_token"]}).status_code == 401
    assert api.client.post("/v1/auth/device/refresh", json={
        "device_id": first["device_id"], "refresh_token": retry.json()["refresh_token"]}).status_code == 200


def test_a_forged_device_secret_cannot_open_the_chat_key(api):
    _set_own_password(api)
    first = _device_login(api).json()
    rotating = first["refresh_token"].split(".")[0]
    forged = rotating + "." + "A" * 43
    r = api.client.post("/v1/auth/device/refresh", json={"device_id": first["device_id"], "refresh_token": forged})
    assert r.status_code == 401


def test_the_database_alone_cannot_open_the_chat_key(api):
    _set_own_password(api)
    _device_login(api)
    with api.Session() as s:
        row = s.query(DeviceSession).one()
        user = s.get(User, row.owner_id)
        key = user_keys.unlock(user, MINE)
        assert row.wrapped_chat_key and key not in row.wrapped_chat_key
        assert row.refresh_hash and "." not in row.refresh_hash


def test_initial_password_then_re_register_the_same_phone_with_the_new_key(api):
    first = _device_login(api, password=INITIAL).json()
    assert first["must_change_password"]
    assert "ck" not in decode_token(first["access_token"])
    r = api.client.post("/v1/auth/change-password", headers=_auth(first["access_token"]),
                        json={"old_password": INITIAL, "new_password": MINE})
    token = r.json()["access_token"]
    again = api.client.post("/v1/devices", headers=_auth(token), json={"device_id": first["device_id"]})
    assert again.status_code == 201
    assert again.json()["device_id"] == first["device_id"]
    refreshed = api.client.post("/v1/auth/device/refresh", json={
        "device_id": first["device_id"], "refresh_token": again.json()["refresh_token"]}).json()
    assert "ck" in decode_token(refreshed["access_token"]) and not refreshed["must_change_password"]


def test_signing_a_phone_out_stops_its_refresh(api):
    _set_own_password(api)
    out = _device_login(api).json()
    listed = api.client.get("/v1/devices", headers=_auth(out["access_token"])).json()
    assert [d["id"] for d in listed] == [out["device_id"]]
    assert api.client.delete(f"/v1/devices/{out['device_id']}", headers=_auth(out["access_token"])).status_code == 204
    assert api.client.post("/v1/auth/device/refresh", json={
        "device_id": out["device_id"], "refresh_token": out["refresh_token"]}).status_code == 401


def test_someone_else_cannot_sign_my_phone_out(api):
    _set_own_password(api)
    out = _device_login(api).json()
    other = _auth(issue_token("mallory", "mallory", ["hr"]))
    assert api.client.delete(f"/v1/devices/{out['device_id']}", headers=other).status_code == 404
    assert api.client.put(f"/v1/devices/{out['device_id']}/push", headers=other,
                          json={"push_token": "x"}).status_code == 404


def test_admin_password_reset_signs_every_phone_out(api):
    _set_own_password(api)
    out = _device_login(api).json()
    with api.Session() as s:
        uid = s.query(User).filter(User.username == "alice").one().id
    assert api.client.post(f"/v1/auth/users/{uid}/reset-password", headers=ADMIN,
                           json={"password": "temporary-password-1"}).status_code == 200
    assert api.client.post("/v1/auth/device/refresh", json={
        "device_id": out["device_id"], "refresh_token": out["refresh_token"]}).status_code == 401


def test_a_disabled_account_cannot_refresh(api):
    _set_own_password(api)
    out = _device_login(api).json()
    with api.Session() as s:
        user = s.query(User).filter(User.username == "alice").one()
        user.disabled = True
        s.commit()
    assert api.client.post("/v1/auth/device/refresh", json={
        "device_id": out["device_id"], "refresh_token": out["refresh_token"]}).status_code == 401


def test_too_many_phones_signs_the_oldest_out(api):
    _set_own_password(api)
    ids = [_device_login(api).json()["device_id"] for _ in range(devices.MAX_DEVICES + 1)]
    with api.Session() as s:
        live = {r.id for r in devices.list_for(s, s.query(User).filter(User.username == "alice").one().id)}
    assert len(live) == devices.MAX_DEVICES and ids[0] not in live and ids[-1] in live


def _fcm_configured(api):
    with api.Session() as s:
        AESGCMVault(s).put(push.SECRET, json.dumps(
            {"project_id": "p", "client_email": "svc@p.iam.gserviceaccount.com", "private_key": "k"}))
        s.commit()


def test_app_config_says_whether_push_is_on(api):
    assert api.client.get("/v1/app/config").json()["push_enabled"] is False
    _fcm_configured(api)
    out = api.client.get("/v1/app/config").json()
    assert out["push_enabled"] is True and out["api_version"] == 1


def test_a_notification_wakes_the_phone_without_its_content(api):
    _set_own_password(api)
    out = _device_login(api, push_token="fcm-token-1").json()
    _fcm_configured(api)
    with api.Session() as s:
        uid = s.query(User).filter(User.username == "alice").one().id
        service.notify(s, uid, "reminder", "Call the bank about the loan", "secret body")
    assert len(api.sent) == 1
    tokens, data = api.sent[0]
    assert tokens == ["fcm-token-1"] and data["type"] == "notification" and data["kind"] == "reminder"
    assert "bank" not in json.dumps(data) and "secret" not in json.dumps(data)
    # Signed out: no more pushes.
    api.client.delete(f"/v1/devices/{out['device_id']}", headers=_auth(out["access_token"]))
    with api.Session() as s:
        service.notify(s, uid, "reminder", "again")
    assert len(api.sent) == 1


def test_no_push_without_fcm_setup(api):
    _set_own_password(api)
    _device_login(api, push_token="fcm-token-1")
    with api.Session() as s:
        uid = s.query(User).filter(User.username == "alice").one().id
        service.notify(s, uid, "reminder", "x")
    assert api.sent == []


def test_a_push_token_moves_with_the_install(api):
    _set_own_password(api)
    a = _device_login(api, push_token="shared").json()
    b = _device_login(api).json()
    assert api.client.put(f"/v1/devices/{b['device_id']}/push", headers=_auth(b["access_token"]),
                          json={"push_token": "shared"}).json()["push"] is True
    with api.Session() as s:
        assert s.get(DeviceSession, a["device_id"]).push_token is None


def test_push_failures_never_break_the_notification(api, monkeypatch):
    _set_own_password(api)
    _device_login(api, push_token="t")
    _fcm_configured(api)

    def boom(*a):
        raise RuntimeError("down")

    monkeypatch.setattr(push, "_dispatch", boom)
    with api.Session() as s:
        uid = s.query(User).filter(User.username == "alice").one().id
        row = service.notify(s, uid, "reminder", "still saved")
        assert row.id
    with api.Session() as s:
        assert service.unread_count(s, uid) == 1


def test_erasing_a_person_removes_their_phones(api):
    _set_own_password(api)
    _device_login(api)
    from app.privacy import erase_user

    with api.Session() as s:
        uid = s.query(User).filter(User.username == "alice").one().id
        counts = erase_user(s, uid, remove_object=lambda key: None)
        assert counts["devices"] == 1
        assert s.query(DeviceSession).count() == 0


# ---- the published Android app: download and self-update ----
def _publish(folder, code: int, data: bytes = b"PK-fake-apk", **meta) -> None:
    import hashlib

    (folder / "twai.apk").write_bytes(data)
    (folder / "release.json").write_text(json.dumps({
        "version_code": code, "version_name": f"1.0.{code}", "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data), "notes": "Faster chat", **meta,
    }))


@pytest.fixture
def release_dir(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "android_release_dir", str(tmp_path))
    return tmp_path


def test_no_app_is_offered_until_one_is_published(api, release_dir):
    assert api.client.get("/v1/app/config").json()["android"] is None
    assert api.client.get("/v1/app/android/latest.apk", follow_redirects=False).status_code == 404
    assert api.client.get("/v1/app/android/1.apk").status_code == 404


def test_the_published_app_is_offered_and_downloaded_by_version(api, release_dir):
    import hashlib

    _publish(release_dir, 42, min_version_code=40)
    rel = api.client.get("/v1/app/config").json()["android"]
    assert rel["version_code"] == 42 and rel["version_name"] == "1.0.42" and rel["min_version_code"] == 40
    assert rel["url"] == "/v1/app/android/42.apk" and rel["notes"] == "Faster chat"
    latest = api.client.get("/v1/app/android/latest.apk", follow_redirects=False)
    assert latest.status_code == 302 and latest.headers["location"] == rel["url"]
    apk = api.client.get(rel["url"])  # no sign-in needed: the APK holds no secrets
    assert apk.status_code == 200 and apk.headers["content-type"] == "application/vnd.android.package-archive"
    assert hashlib.sha256(apk.content).hexdigest() == rel["sha256"]
    assert "TW-ai-1.0.42.apk" in apk.headers["content-disposition"]
    # An old version's URL is never answered with the new file (caches key on the URL).
    assert api.client.get("/v1/app/android/41.apk").status_code == 404


def test_a_half_written_release_is_not_offered(api, release_dir):
    _publish(release_dir, 7)
    (release_dir / "twai.apk").write_bytes(b"truncated")  # the APK no longer matches release.json
    assert api.client.get("/v1/app/config").json()["android"] is None
    assert api.client.get("/v1/app/android/7.apk").status_code == 404
