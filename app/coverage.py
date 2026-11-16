"""What our channels already covered and what worked for competitors (owner, 2026-11-16), for the scouts.

The uploads of watched YouTube channels (ours: settings.youtube_own_channels; competitors: each scout's
`competitor_channels`) are kept in `channel_videos`, synced through the official API at most once a day per
channel: new uploads page by page until a known one appears (1 unit per 50), views re-read weekly (1 unit
per 50). Instagram can't be read from here (login wall), so "covered" means on our YouTube channels,
Shorts included.

  - `covered_matches`: our videos whose title or description shares the distinctive words of a product
    name; app/scout.py lets the model decide which candidates are really covered.
  - `hits`: competitor videos with at least N views and older than D days: proven topics the scout looks
    for better or similar products to.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from sqlalchemy.orm import Session

from app.db.session import ChannelSync, ChannelVideo

logger = logging.getLogger(__name__)

SYNC_EVERY = dt.timedelta(hours=24)
STATS_EVERY = dt.timedelta(days=7)
MAX_PAGES = 80  # 4,000 uploads per channel on the first sync (80 quota units)
# Words that say nothing about which product a video shows.
GENERIC = set("""a an the and or of for to in on with without by from at is it this that these those my your our you
i we me best top new cool cheap budget amazing awesome unique smart mini pro max plus ultra gadget gadgets product products
review reviews unboxing india indian rs price under buy vs versus test tested honest first look hands
video short shorts reel tech techwiser gadgetwiser wiser things thing stuff useful must have every one two three 2 3 4 5
10 in1 2in1 3in1 amazon flipkart meesho link links""".split())


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(t: dt.datetime | None) -> dt.datetime | None:
    return t if t is None or t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1 and w not in GENERIC}


def resolve(session: Session, refs: list[str], key: str) -> list[str]:
    """Channel ids for references ('ours', @handle, link, id, name), looked up once and remembered."""
    from app.browsing import youtube

    known = {r.lower(): row.channel_id for row in session.query(ChannelSync) for r in (row.refs or [])}
    out: list[str] = []
    for ref in refs:
        ref = " ".join(str(ref).split())
        if not ref:
            continue
        ids = [known[ref.lower()]] if ref.lower() in known and ref.lower() not in ("ours", "our") else None
        if ids is None:
            try:
                ids = youtube.channel_ids(ref, key)
            except youtube.YouTubeError as e:
                logger.warning("channel %r not resolved: %s", ref, e)
                continue
            for cid in ids:
                row = session.get(ChannelSync, cid) or ChannelSync(channel_id=cid, title="", refs=[], videos=0)
                if ref.lower() not in [r.lower() for r in row.refs or []] and ref.lower() not in ("ours", "our"):
                    row.refs = [*(row.refs or []), ref][:10]
                session.add(row)
            session.commit()
        out += [i for i in ids if i not in out]
    return out


def sync(session: Session, channel_id: str, key: str, force: bool = False) -> int:
    """Fetch a channel's new uploads (and refresh views weekly). Returns how many videos were added."""
    from app.browsing import youtube

    row = session.get(ChannelSync, channel_id) or ChannelSync(channel_id=channel_id, title="", refs=[], videos=0)
    now = _now()
    if not force and row.synced_at and now - _aware(row.synced_at) < SYNC_EVERY:
        return 0
    found = youtube.channels([channel_id], key)
    if not found:
        return 0
    channel = found[0]
    row.title = channel.title[:200]
    have = {i for (i,) in session.query(ChannelVideo.id).filter(ChannelVideo.channel_id == channel_id)}
    new_ids: list[str] = []
    page = ""
    for _ in range(MAX_PAGES):
        ids, page = youtube.upload_page(channel.uploads, key, page)
        fresh = [i for i in ids if i not in have]
        new_ids += fresh
        if not page or (have and len(fresh) < len(ids)):  # reached what we already have
            break
    refresh = not row.stats_at or now - _aware(row.stats_at) >= STATS_EVERY
    ids = new_ids + (sorted(have) if refresh else [])
    for v in youtube._videos(ids, key):
        video = session.get(ChannelVideo, v.id) or ChannelVideo(id=v.id, channel_id=channel_id)
        video.title = v.title[:300]
        video.description = (v.description or "")[:2000]
        video.views = v.views or 0
        try:
            video.published_at = dt.datetime.fromisoformat(v.published).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            video.published_at = None
        video.synced_at = now
        session.add(video)
    row.synced_at = now
    if refresh:
        row.stats_at = now
    row.videos = len(have) + len(new_ids)
    session.add(row)
    session.commit()
    return len(new_ids)


def ensure(session: Session, refs: list[str], key: str) -> list[str]:
    """Resolve and sync the channels (each at most once a day); a channel that fails is skipped."""
    ids = resolve(session, refs, key)
    for cid in ids:
        try:
            sync(session, cid, key)
        except Exception as e:  # noqa: BLE001 - one channel (or the quota) must not stop a round
            session.rollback()
            logger.warning("channel %s not synced: %s", cid, type(e).__name__)
    return ids


def index(session: Session, channel_ids: list[str]) -> list[tuple[ChannelVideo, set[str]]]:
    """Our videos with their distinctive words (title + start of the description), built once per round."""
    if not channel_ids:
        return []
    return [(v, words(v.title) | words(v.description[:1500]))
            for v in session.query(ChannelVideo).filter(ChannelVideo.channel_id.in_(channel_ids))]


def covered_matches(videos: list[tuple[ChannelVideo, set[str]]], name: str, limit: int = 6) -> list[ChannelVideo]:
    """Our videos most likely to show this product: sharing at least half its distinctive words (and at least
    one). The model then decides whether one really shows it (app/scout.py already_covered)."""
    want = words(name)
    if not want:
        return []
    scored = []
    for v, have in videos:
        share = len(want & have)
        if share and share / len(want) >= 0.5:
            scored.append((share / len(want), v.views, v))
    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [v for _, _, v in scored[:limit]]


def as_dict(v: ChannelVideo, channel: str = "") -> dict:
    return {"id": v.id, "title": v.title, "description": v.description, "views": v.views or 0, "channel": channel,
            "published": v.published_at.strftime("%Y-%m-%d") if v.published_at else "",
            "url": f"https://www.youtube.com/watch?v={v.id}"}


def hits(session: Session, channel_ids: list[str], min_views: int, min_age_days: int) -> list[ChannelVideo]:
    """Competitor videos that worked: at least `min_views` and older than `min_age_days`, most viewed first."""
    if not channel_ids:
        return []
    before = _now() - dt.timedelta(days=min_age_days)
    return (session.query(ChannelVideo).filter(ChannelVideo.channel_id.in_(channel_ids),
                                               ChannelVideo.views >= min_views, ChannelVideo.published_at <= before)
            .order_by(ChannelVideo.views.desc()).all())


def channel_names(session: Session, channel_ids: list[str]) -> dict[str, str]:
    return {r.channel_id: r.title for r in session.query(ChannelSync).filter(ChannelSync.channel_id.in_(channel_ids))}
