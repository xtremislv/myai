"""The Android app as published by this server, for download from the web app and self-update.

scripts/publish_android.sh builds a signed APK and writes two files into ANDROID_RELEASE_DIR:
  twai.apk       the app
  release.json   {"version_code", "version_name", "sha256", "size", "notes", "min_version_code", "published_at"}

The app compares version_code with its own, downloads /v1/app/android/<version_code>.apk, checks
the SHA-256 and installs it. Phones below min_version_code must update before they go on. The APK
holds no secrets, so it is served to anyone, like the web app's own files.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import get_settings

APK = "twai.apk"
META = "release.json"

logger = logging.getLogger(__name__)


def release_dir() -> Path:
    return Path(get_settings().android_release_dir)


def latest() -> dict | None:
    """The published release, or None when there is none (or its files don't agree)."""
    folder = release_dir()
    try:
        meta = json.loads((folder / META).read_text())
        apk = folder / APK
        code = int(meta["version_code"])
        size = apk.stat().st_size
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if size != int(meta.get("size", -1)) or not str(meta.get("sha256", "")).strip():
        logger.warning("android release in %s doesn't match its release.json: not offered", folder)
        return None
    return {
        "version_code": code,
        "version_name": str(meta.get("version_name") or code),
        "sha256": str(meta["sha256"]).lower(),
        "size": size,
        "notes": str(meta.get("notes") or "")[:2000],
        "min_version_code": int(meta.get("min_version_code") or 0),
        "published_at": str(meta.get("published_at") or ""),
        "url": f"/v1/app/android/{code}.apk",
    }


def apk_path() -> Path:
    return release_dir() / APK
