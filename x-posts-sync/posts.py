"""Plan a Notion X Post from a normalized post dict.

Content comes from an X API dump (``xapi.py``). This module does not fetch posts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

ID_RE = re.compile(r"\d{6,22}")
REPLY_SETTINGS = {"everyone", "following", "mentionedUsers", "subscribers", "verified"}


def parse_status_url(tweet_url: str):
    """Return ``(handle, status_id, ok)``. ``handle`` may be ``i``."""
    if not tweet_url:
        return None, None, False
    u = tweet_url.strip().split("?")[0].split("#")[0].rstrip("/")
    if re.search(r"(?:x|twitter)\.com/home/?$", u):
        return None, None, False
    m = re.search(r"(?:x|twitter)\.com/i/web/status/(\d+)", u)
    if m:
        return "i", m.group(1), True
    m = re.search(r"(?:x|twitter)\.com/([^/]+)/status/(\d+)", u)
    if m:
        handle = m.group(1)
        if handle == "home":
            return None, None, False
        return handle, m.group(2), True
    return None, None, False


def coerce_post_id(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if ID_RE.fullmatch(text):
        return text
    _handle, sid, ok = parse_status_url(text)
    if ok and sid and ID_RE.fullmatch(sid):
        return sid
    return None


def _present(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _bitrate(variant: dict) -> int:
    raw = variant.get("bitrate")
    if raw is None:
        raw = variant.get("bit_rate")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def parse_time(value) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n > 10_000_000_000:
            n = n / 1000.0
        try:
            return datetime.fromtimestamp(n, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = None
    if dt is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    try:
        return datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y").astimezone(timezone.utc)
    except ValueError:
        return None


def as_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        n = value
    elif isinstance(value, float):
        if value != value:
            return None
        n = int(value)
    elif isinstance(value, str):
        text = value.replace(",", "").strip()
        if not re.fullmatch(r"-?\d+", text):
            return None
        n = int(text)
    else:
        return None
    if n < 0:
        return None
    return n


def first_metric(tweet: dict, *keys: str) -> int | None:
    for key in keys:
        if key in tweet and tweet.get(key) is not None:
            return as_int(tweet.get(key))
    return None


def tweet_text(tweet: dict) -> str:
    text = tweet.get("text") if isinstance(tweet.get("text"), str) else ""
    raw = tweet.get("raw_text")
    raw_text = raw.get("text") if isinstance(raw, dict) and isinstance(raw.get("text"), str) else ""
    if tweet.get("is_note_tweet") and raw_text and len(raw_text) > len(text) + 30:
        return raw_text
    if text:
        return text
    return raw_text or ""


def best_mp4(item: dict) -> str | None:
    variants = item.get("variants") or []
    mp4s = [
        v for v in variants
        if isinstance(v, dict) and (
            "mp4" in (v.get("content_type") or "") or ".mp4" in (v.get("url") or "")
        )
    ]
    if mp4s:
        mp4s.sort(key=_bitrate, reverse=True)
        return mp4s[0].get("url") or None
    formats = item.get("formats") or []
    mp4s = [
        f for f in formats
        if isinstance(f, dict) and (
            "mp4" in (f.get("container") or f.get("content_type") or f.get("type") or "")
            or ".mp4" in (f.get("url") or "")
        )
    ]
    if mp4s:
        mp4s.sort(key=_bitrate, reverse=True)
        return mp4s[0].get("url") or None
    url = item.get("url") or ""
    if ".mp4" in url or (item.get("format") or "").startswith("video"):
        return url or None
    return None


def extract_media(tweet: dict) -> tuple[list[str], list[str]]:
    """Photo URLs and video URLs, de-duplicated. Video thumbnails are photos."""
    photos: list[str] = []
    videos: list[str] = []
    seen_p: set[str] = set()
    seen_v: set[str] = set()
    media = tweet.get("media") or {}
    if not isinstance(media, dict):
        return photos, videos
    for photo in media.get("photos") or []:
        if not isinstance(photo, dict):
            continue
        url = photo.get("url") or photo.get("original_url") or ""
        if url and url not in seen_p:
            photos.append(url)
            seen_p.add(url)
    for item in media.get("all") or []:
        if not isinstance(item, dict):
            continue
        kind = (item.get("type") or "").lower()
        if kind in ("video", "gif") or item.get("variants") or item.get("duration") or item.get("duration_ms"):
            thumb = item.get("thumbnail_url") or ""
            if thumb and thumb not in seen_p:
                photos.append(thumb)
                seen_p.add(thumb)
            video = best_mp4(item)
            if video and video not in seen_v:
                videos.append(video)
                seen_v.add(video)
        elif kind == "photo":
            url = item.get("url") or ""
            if url and url not in seen_p:
                photos.append(url)
                seen_p.add(url)
    for video in media.get("videos") or []:
        if not isinstance(video, dict):
            continue
        url = best_mp4(video)
        if url and url not in seen_v:
            videos.append(url)
            seen_v.add(url)
        thumb = video.get("thumbnail_url") or ""
        if thumb and thumb not in seen_p:
            photos.append(thumb)
            seen_p.add(thumb)
    return photos, videos


def title_from_text(text: str, post_id: str, limit: int = 160) -> str:
    line = ""
    for part in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        part = re.sub(r"https?://t\.co/\w+", "", part)
        part = re.sub(r"\s+", " ", part).strip()
        if part:
            line = part
            break
    if not line:
        return f"Post {post_id}"
    if len(line) > limit:
        line = line[: limit - 1].rstrip() + "…"
    return line


def _short_ref(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        kept = {k: value.get(k) for k in ("id", "screen_name", "name", "url") if k in value}
        return kept or None
    return str(value)[:200]


def compact_raw(tweet: dict, photos: list[str], videos: list[str]) -> dict:
    author = tweet.get("author") or {}
    if not isinstance(author, dict):
        author = {}
    quote = tweet.get("quote") if isinstance(tweet.get("quote"), dict) else None
    raw = {
        "source": "x-api",
        "id": str(tweet.get("id") or ""),
        "url": tweet.get("url"),
        "text": tweet_text(tweet),
        "created_at": tweet.get("created_at"),
        "created_timestamp": tweet.get("created_timestamp"),
        "lang": tweet.get("lang"),
        "possibly_sensitive": tweet.get("possibly_sensitive"),
        "author": {
            "screen_name": author.get("screen_name"),
            "id": author.get("id"),
            "name": author.get("name"),
        },
        "metrics": {
            "likes": tweet.get("likes"),
            "retweets": tweet.get("retweets"),
            "replies": tweet.get("replies"),
            "quotes": tweet.get("quotes"),
            "bookmarks": tweet.get("bookmarks"),
            "views": tweet.get("views"),
        },
        "replying_to": _short_ref(tweet.get("replying_to")),
        "replying_to_status": _short_ref(tweet.get("replying_to_status")),
        "reposted_by": _short_ref(tweet.get("reposted_by")),
        "media": {"photos": photos, "videos": videos},
    }
    if quote:
        q_author = quote.get("author") if isinstance(quote.get("author"), dict) else {}
        raw["quote"] = {
            "id": str(quote.get("id") or ""),
            "url": quote.get("url"),
            "text": quote.get("text") if isinstance(quote.get("text"), str) else "",
            "author": (q_author or {}).get("screen_name"),
        }
    return raw


@dataclass
class PostPlan:
    in_scope: bool
    reason: str
    post_id: str
    type_name: str
    text: str
    title: str
    url: str
    author_handle: str
    author_at: str
    author_id: str
    posted_iso: str
    posted_dt: datetime | None
    lang: str
    sensitive: bool | None
    has_media: bool
    conversation_id: str
    quoted_url: str
    quote_author: str
    photos: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    thread_note: bool = False
    reply_settings: str = ""
    app_source: str = ""


def _out(reason: str, post_id: str = "") -> PostPlan:
    return PostPlan(
        in_scope=False,
        reason=reason,
        post_id=post_id,
        type_name="",
        text="",
        title="",
        url="",
        author_handle="",
        author_at="",
        author_id="",
        posted_iso="",
        posted_dt=None,
        lang="",
        sensitive=None,
        has_media=False,
        conversation_id="",
        quoted_url="",
        quote_author="",
        metrics={
            "likes": None,
            "reposts": None,
            "replies": None,
            "quotes": None,
            "bookmarks": None,
            "impressions": None,
        },
    )


def _reply_settings(tweet: dict) -> str:
    value = tweet.get("reply_settings")
    if isinstance(value, str) and value in REPLY_SETTINGS:
        return value
    return ""


def _app_source(tweet: dict) -> str:
    value = tweet.get("app_source")
    if isinstance(value, str):
        text = value.strip()
        if text and len(text) <= 200:
            return text
    return ""


def plan_post(tweet: dict, handle: str) -> PostPlan:
    """Classify one fetched post. In-scope rows are Tweet or Quote roots by ``handle``."""
    post_id = str((tweet or {}).get("id") or "").strip()
    if not ID_RE.fullmatch(post_id):
        return _out("no_id", post_id)
    if _present(tweet.get("reposted_by")) or _present(tweet.get("retweet")) or _present(tweet.get("retweeted_status")):
        return _out("retweet", post_id)
    author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
    screen = str((author or {}).get("screen_name") or "").lstrip("@").strip()
    want = (handle or "").lstrip("@").strip().lower()
    if not screen:
        return _out("no_author", post_id)
    if want and screen.lower() != want:
        return _out("other_author", post_id)
    if _present(tweet.get("replying_to")) or _present(tweet.get("replying_to_status")):
        return _out("reply", post_id)

    quote = tweet.get("quote") if isinstance(tweet.get("quote"), dict) else None
    type_name = "Quote" if quote else "Tweet"
    text = tweet_text(tweet)
    photos, videos = extract_media(tweet)
    posted_dt = parse_time(tweet.get("created_timestamp")) or parse_time(tweet.get("created_at"))
    posted_iso = posted_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if posted_dt else ""
    author_id = str((author or {}).get("id") or "").strip()
    if author_id.endswith(".0"):
        author_id = author_id[:-2]
    if not re.fullmatch(r"\d{1,32}", author_id):
        author_id = ""
    lang = tweet.get("lang") if isinstance(tweet.get("lang"), str) else ""
    lang = lang.strip()
    if not (1 <= len(lang) <= 16):
        lang = ""
    sensitive = None
    if "possibly_sensitive" in tweet and tweet.get("possibly_sensitive") is not None:
        sensitive = bool(tweet.get("possibly_sensitive"))
    quoted_url = ""
    quote_author = ""
    if quote:
        q_author = quote.get("author") if isinstance(quote.get("author"), dict) else {}
        quote_author = str((q_author or {}).get("screen_name") or "").lstrip("@").strip()
        qid = str(quote.get("id") or "").strip()
        if quote_author and ID_RE.fullmatch(qid):
            quoted_url = f"https://x.com/{quote_author}/status/{qid}"
        else:
            _h, sid, ok = parse_status_url(str(quote.get("url") or ""))
            if ok and sid and _h and _h not in ("i", "home"):
                quoted_url = f"https://x.com/{_h}/status/{sid}"
            elif quote.get("url"):
                quoted_url = str(quote.get("url")).split("?")[0].split("#")[0]
    metrics = {
        "likes": first_metric(tweet, "likes", "like_count"),
        "reposts": first_metric(tweet, "retweets", "repost_count"),
        "replies": first_metric(tweet, "replies", "reply_count"),
        "quotes": first_metric(tweet, "quotes", "quote_count"),
        "bookmarks": first_metric(tweet, "bookmarks", "bookmark_count"),
        # Missing views must stay None so Notion Impressions is left unchanged.
        "impressions": first_metric(tweet, "views", "impression_count", "impressions"),
    }
    return PostPlan(
        in_scope=True,
        reason="",
        post_id=post_id,
        type_name=type_name,
        text=text,
        title=title_from_text(text, post_id),
        url=f"https://x.com/{screen}/status/{post_id}",
        author_handle=screen,
        author_at=f"@{screen}",
        author_id=author_id,
        posted_iso=posted_iso,
        posted_dt=posted_dt,
        lang=lang,
        sensitive=sensitive,
        has_media=bool(photos or videos),
        conversation_id=post_id,
        quoted_url=quoted_url,
        quote_author=quote_author,
        photos=photos,
        videos=videos,
        metrics=metrics,
        raw=compact_raw(tweet, photos, videos),
        thread_note="🧵" in text,
        reply_settings=_reply_settings(tweet),
        app_source=_app_source(tweet),
    )
