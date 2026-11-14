"""YouTube through the official YouTube Data API v3 (key: vault secret ``youtube/api``).

Three things agents need: find videos (with links, views, dates), report on a channel (the company's
own channels, TechWiser and GadgetWiser, by default; or any other), and look closely at videos
(stats, tags, top comments). Only public data: watch time, CTR, revenue and audience need the
YouTube Analytics API with the channel owner's sign-in, which an API key can't give.

Quota: 10,000 units a day; a search costs 100, everything else 1. Answers are cached for 10 minutes.
The key travels in the URL, so it is scrubbed from every error and never returned.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit

from app.browsing.fetch import fetch
from app.config import get_settings

API = "https://www.googleapis.com/youtube/v3/"
CACHE_SECONDS = 600
_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_lock = threading.Lock()
ORDERS = {"relevance", "date", "viewCount", "rating"}


class YouTubeError(RuntimeError):
    pass


@dataclass
class Video:
    id: str
    title: str
    channel: str
    published: str
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    duration: str = ""
    tags: list[str] = field(default_factory=list)
    description: str = ""

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"

    @property
    def short(self) -> bool:
        return _seconds(self.duration) <= 180 and bool(self.duration)


@dataclass
class Channel:
    id: str
    title: str
    handle: str
    subscribers: int | None
    views: int
    videos: int
    created: str
    country: str
    description: str
    uploads: str
    ours: bool = False

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/{self.handle}" if self.handle else f"https://www.youtube.com/channel/{self.id}"


def own_channels() -> dict[str, str]:
    """The company's channels: id -> name, from settings (youtube_own_channels)."""
    out = {}
    for part in get_settings().youtube_own_channels.split(","):
        cid, _, name = part.strip().partition("=")
        if cid:
            out[cid.strip()] = name.strip() or cid.strip()
    return out


def _call(path: str, key: str, **params) -> dict:
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    cache_key = f"{path}?{query}"
    with _lock:
        hit = _cache.get(cache_key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    try:
        page = fetch(f"{API}{path}?{query}&key={key}")
    except Exception as e:  # noqa: BLE001 - never let the key ride out in an error text
        raise YouTubeError(f"YouTube could not be reached ({type(e).__name__})") from None
    try:
        data = json.loads(page.body)
    except json.JSONDecodeError:
        raise YouTubeError(f"YouTube answered {page.status} with no data") from None
    if page.status >= 400 or "error" in data:
        err = data.get("error") or {}
        reasons = {e.get("reason") for e in err.get("errors", []) if isinstance(e, dict)}
        if "quotaExceeded" in reasons or "dailyLimitExceeded" in reasons:
            raise YouTubeError("the YouTube API's daily quota is used up; it resets at midnight Pacific time")
        message = str(err.get("message") or page.status).replace(key, "***")
        raise YouTubeError(f"YouTube refused the request: {message[:200]}")
    with _lock:
        _cache[cache_key] = (time.monotonic(), data)
        while len(_cache) > 256:
            _cache.popitem(last=False)
    return data


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _seconds(iso: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _clock(iso: str) -> str:
    total = _seconds(iso)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _count(n: int | None) -> str:
    if n is None:
        return "hidden"
    for size, unit in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + unit
    return str(n)


def _videos(ids: list[str], key: str) -> list[Video]:
    out: list[Video] = []
    for i in range(0, len(ids), 50):
        data = _call("videos", key, part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50]))
        for it in data.get("items", []):
            sn, st = it.get("snippet", {}), it.get("statistics", {})
            out.append(Video(
                id=it["id"], title=sn.get("title", ""), channel=sn.get("channelTitle", ""),
                published=sn.get("publishedAt", "")[:10], views=_int(st.get("viewCount")),
                likes=_int(st.get("likeCount")), comments=_int(st.get("commentCount")),
                duration=it.get("contentDetails", {}).get("duration", ""), tags=sn.get("tags", [])[:15],
                description=sn.get("description", ""),
            ))
    order = {v: n for n, v in enumerate(ids)}
    return sorted(out, key=lambda v: order.get(v.id, 0))


def video_id(ref: str) -> str | None:
    ref = ref.strip()
    if re.fullmatch(r"[\w-]{11}", ref):
        return ref
    parts = urlsplit(ref if "://" in ref else f"https://{ref}")
    host = (parts.hostname or "").removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        return parts.path.strip("/")[:11] or None
    if host.endswith("youtube.com"):
        if parts.path == "/watch":
            return (parse_qs(parts.query).get("v") or [None])[0]
        m = re.match(r"/(?:shorts|embed|live)/([\w-]{11})", parts.path)
        return m.group(1) if m else None
    return None


def _channel_from(it: dict) -> Channel:
    sn, st = it.get("snippet", {}), it.get("statistics", {})
    return Channel(
        id=it["id"], title=sn.get("title", ""), handle=sn.get("customUrl", ""),
        subscribers=None if st.get("hiddenSubscriberCount") else _int(st.get("subscriberCount")),
        views=_int(st.get("viewCount")) or 0, videos=_int(st.get("videoCount")) or 0,
        created=sn.get("publishedAt", "")[:10], country=sn.get("country", ""),
        description=sn.get("description", "")[:300],
        uploads=it.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", ""),
        ours=it["id"] in own_channels(),
    )


def channel_ids(ref: str, key: str) -> list[str]:
    """Channel ids for a reference: 'ours' (all the company's), an own channel's name, a channel id,
    an @handle, a channel link, or any name (YouTube's best match). Several, comma-separated, work too.
    Gemma sometimes leaks other arguments into the value ("ours', videos:10"): those bits are dropped."""
    parts = [p.strip().strip("'\"` ") for p in (ref or "ours").split(",")]
    parts = [p for p in parts if p and not re.fullmatch(r"\w+\s*[:=]\s*(?!/)\S*", p)]
    if len(parts) > 1:
        out: list[str] = []
        for p in parts[:5]:
            out += [i for i in channel_ids(p, key) if i not in out]
        return out
    ref = parts[0] if parts else "ours"
    own = own_channels()
    low = ref.lower().replace(" ", "")
    if low in ("ours", "our", "own", "mine", "all", "company", "both"):
        return list(own)
    for cid, name in own.items():
        if low in (name.lower().replace(" ", ""), cid.lower()):
            return [cid]
    if re.fullmatch(r"UC[\w-]{22}", ref):
        return [ref]
    parts = urlsplit(ref) if "://" in ref or "youtube.com" in ref else None
    if parts is not None:
        path = urlsplit(ref if "://" in ref else f"https://{ref}").path
        m = re.match(r"/channel/(UC[\w-]{22})", path)
        if m:
            return [m.group(1)]
        m = re.match(r"/(@[\w.-]+)", path)
        if m:
            ref = m.group(1)
    if ref.startswith("@"):
        data = _call("channels", key, part="id", forHandle=ref)
        if data.get("items"):
            return [data["items"][0]["id"]]
    data = _call("search", key, part="snippet", q=ref, type="channel", maxResults=1)
    return [it["snippet"]["channelId"] for it in data.get("items", [])]


def channels(ids: list[str], key: str) -> list[Channel]:
    if not ids:
        return []
    data = _call("channels", key, part="snippet,statistics,contentDetails", id=",".join(ids[:50]))
    found = {it["id"]: _channel_from(it) for it in data.get("items", [])}
    return [found[i] for i in ids if i in found]


def recent_uploads(channel: Channel, key: str, limit: int = 15) -> list[Video]:
    if not channel.uploads:
        return []
    data = _call("playlistItems", key, part="contentDetails", playlistId=channel.uploads,
                 maxResults=max(1, min(limit, 50)))
    ids = [it["contentDetails"]["videoId"] for it in data.get("items", [])]
    return _videos(ids, key)


def search(query: str, key: str, order: str = "relevance", days: int = 0, channel: str = "",
           limit: int = 10, region: str = "IN") -> list[Video]:
    params = {"part": "snippet", "q": query, "type": "video", "maxResults": max(1, min(limit, 25)),
              "order": order if order in ORDERS else "relevance", "regionCode": region}
    if days and days > 0:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        params["publishedAfter"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    if channel:
        ids = channel_ids(channel, key)
        if not ids:
            raise YouTubeError(f"no YouTube channel found for '{channel}'")
        if len(ids) > 1:  # 'ours': one search per channel, merged by the order asked for
            found = [v for cid in ids for v in search(query, key, order, days, cid, limit, region)]
            found.sort(key=lambda v: (v.views or 0) if order == "viewCount" else v.published, reverse=True)
            return found[:limit]
        params["channelId"] = ids[0]
    data = _call("search", key, **params)
    ids = [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]
    return _videos(ids, key)


def top_comments(vid: str, key: str, limit: int = 15) -> list[str]:
    try:
        data = _call("commentThreads", key, part="snippet", videoId=vid, order="relevance",
                     maxResults=max(1, min(limit, 50)), textFormat="plainText")
    except YouTubeError as e:
        return [f"(comments unavailable: {e})"]
    out = []
    for it in data.get("items", []):
        c = it["snippet"]["topLevelComment"]["snippet"]
        text = " ".join(str(c.get("textDisplay", "")).split())[:300]
        out.append(f"{c.get('likeCount', 0)} likes: {text}")
    return out


# ---- text for the model ----
def _line(i: int, v: Video) -> str:
    kind = "Short" if v.short else _clock(v.duration) if v.duration else "?"
    stats = f"{_count(v.views)} views, {_count(v.likes)} likes, {_count(v.comments)} comments"
    return f"{i}. {v.title} — {v.channel} [{v.published}; {kind}; {stats}]\n   {v.url}"


def render_videos(videos: list[Video]) -> str:
    if not videos:
        return "No matching videos."
    return "\n".join(_line(i, v) for i, v in enumerate(videos, 1))


def _days_since(day: str) -> int:
    try:
        return max(1, (dt.date.today() - dt.date.fromisoformat(day)).days)
    except ValueError:
        return 1


def render_channel(c: Channel, videos: list[Video]) -> str:
    who = " (OUR channel)" if c.ours else ""
    out = [
        f"{c.title}{who} {c.handle} — {c.url}",
        f"Subscribers {_count(c.subscribers)} · total views {_count(c.views)} ({c.views:,}) · {c.videos} videos"
        f" · since {c.created}" + (f" · {c.country}" if c.country else ""),
    ]
    if c.videos:
        out.append(f"Average views per video (lifetime): {_count(c.views // c.videos)}")
    if videos:
        seen = [v for v in videos if v.views is not None]
        longs = [v for v in seen if not v.short]
        shorts = [v for v in seen if v.short]
        def avg(vs): return sum(v.views for v in vs) // len(vs) if vs else 0
        engaged = [((v.likes or 0) + (v.comments or 0)) / v.views for v in seen if v.views]
        out.append(f"\nLast {len(videos)} uploads (newest first): average {_count(avg(seen))} views"
                   + (f"; long videos {_count(avg(longs))} ({len(longs)})" if longs else "")
                   + (f"; Shorts {_count(avg(shorts))} ({len(shorts)})" if shorts else "")
                   + (f"; engagement (likes+comments)/views {100 * sum(engaged) / len(engaged):.2f}%" if engaged else ""))
        if len(videos) >= 2:
            span = _days_since(videos[-1].published) - _days_since(videos[0].published)
            out.append(f"Upload pace: {len(videos)} videos in {max(span, 1)} days")
        for i, v in enumerate(videos, 1):
            per_day = (v.views or 0) // _days_since(v.published)
            out.append(_line(i, v) + f"  (~{_count(per_day)} views/day)")
        best = max(seen, key=lambda v: v.views or 0, default=None)
        if best:
            out.append(f"Best of these: {best.title} ({_count(best.views)} views)")
    return "\n".join(out)


def render_details(v: Video, comments: list[str]) -> str:
    out = [_line(1, v)[3:], ""]
    if v.views:
        out.append(f"Likes per 100 views: {100 * (v.likes or 0) / v.views:.2f}; "
                   f"comments per 1,000 views: {1000 * (v.comments or 0) / v.views:.2f}; "
                   f"~{_count((v.views or 0) // _days_since(v.published))} views/day since upload")
    if v.tags:
        out.append("Tags: " + ", ".join(v.tags))
    if v.description:
        out.append("Description: " + " ".join(v.description.split())[:800])
    if comments:
        out.append(f"\nTop comments ({len(comments)}):")
        out += [f"- {c}" for c in comments]
    return "\n".join(out)
