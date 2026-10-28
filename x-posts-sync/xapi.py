"""Turn an X API v2 dump into the normalized post dicts ``plan_post`` expects.

Accepts the JSON written by a Grok Bot X read (`get_posts_by_ids` or a user
timeline): a ``{"data": [...], "includes": {...}}`` object, or a bare array
of post objects. Media may be ``includes.media``, a top-level ``media`` list,
or ``media`` on each post. Quoted posts may be in ``includes.posts`` or
``includes.tweets``.
"""
from __future__ import annotations

import json
from pathlib import Path

from posts import ID_RE

DEFAULT_AUTHOR_ID = ""  # set X_POSTS_AUTHOR_ID or --author-id


def load_posts_file(path: str) -> object:
    if path == "-":
        import sys
        text = sys.stdin.read()
    else:
        file_path = Path(path)
        if not file_path.is_file():
            raise SystemExit(f"--posts-json not found: {path}")
        text = file_path.read_text()
    stripped = text.strip()
    if not stripped:
        raise SystemExit("--posts-json is empty")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--posts-json is not valid JSON: {exc}") from None


def iter_post_records(payload: object) -> tuple[list[dict], dict]:
    """Return ``(posts, includes)`` from the dump shapes we accept."""
    if isinstance(payload, list):
        posts = [item for item in payload if isinstance(item, dict)]
        return posts, {}
    if not isinstance(payload, dict):
        raise SystemExit("--posts-json must be a list of posts or an object with data/posts")
    includes = dict(payload.get("includes") or {})
    if isinstance(payload.get("media"), list):
        media = list(includes.get("media") or [])
        media.extend(item for item in payload["media"] if isinstance(item, dict))
        includes["media"] = media
    if isinstance(payload.get("users"), list) and "users" not in includes:
        includes["users"] = payload["users"]
    if isinstance(payload.get("data"), list):
        return [item for item in payload["data"] if isinstance(item, dict)], includes
    if isinstance(payload.get("data"), dict) and payload["data"].get("id"):
        return [payload["data"]], includes
    for key in ("posts", "tweets"):
        if isinstance(payload.get(key), list):
            return [item for item in payload[key] if isinstance(item, dict)], includes
    if payload.get("id") and (payload.get("text") is not None or payload.get("public_metrics")):
        return [payload], includes
    raise SystemExit("--posts-json has no post list (expected data, posts, or an array)")


def _username(user: dict) -> str:
    if not isinstance(user, dict):
        return ""
    return str(user.get("username") or user.get("screen_name") or "").lstrip("@").strip()


def _index_by(items: list, key: str) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for item in items:
        if isinstance(item, dict) and item.get(key) is not None:
            found[str(item[key])] = item
    return found


def _note_text(post: dict) -> str:
    note = post.get("note_tweet")
    if isinstance(note, str):
        return note
    if not isinstance(note, dict):
        return ""
    if isinstance(note.get("text"), str):
        return note["text"]
    result = note.get("note_tweet_results")
    if isinstance(result, dict):
        inner = result.get("result") if isinstance(result.get("result"), dict) else result
        if isinstance(inner, dict) and isinstance(inner.get("text"), str):
            return inner["text"]
    return ""


def post_text(post: dict) -> str:
    text = post.get("text") if isinstance(post.get("text"), str) else ""
    note = _note_text(post)
    if note and len(note) > len(text):
        return note
    return text or note


def _ref(post: dict, kind: str) -> dict | None:
    for item in post.get("referenced_tweets") or []:
        if isinstance(item, dict) and item.get("type") == kind and item.get("id"):
            return item
    return None


def _variant_bitrate(variant: dict) -> int:
    raw = variant.get("bit_rate")
    if raw is None:
        raw = variant.get("bitrate")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _media_url(item: dict) -> str:
    return str(item.get("url") or item.get("media_url_https") or item.get("preview_image_url") or "")


def _as_media_item(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    kind = str(item.get("type") or "").lower()
    if kind in ("photo", "image"):
        url = _media_url(item)
        if not url:
            return None
        return {"type": "photo", "url": url}
    if kind in ("video", "animated_gif", "gif"):
        variants = []
        for variant in item.get("variants") or []:
            if not isinstance(variant, dict) or not variant.get("url"):
                continue
            variants.append({
                "url": variant.get("url"),
                "content_type": variant.get("content_type") or "",
                "bitrate": _variant_bitrate(variant),
            })
        return {
            "type": "gif" if kind in ("animated_gif", "gif") else "video",
            "thumbnail_url": item.get("preview_image_url") or item.get("thumbnail_url") or "",
            "url": item.get("url") or "",
            "variants": variants,
        }
    return None


def _collect_media(post: dict, media_by_key: dict[str, dict]) -> dict:
    items: list[dict] = []
    keys = ((post.get("attachments") or {}).get("media_keys") or []) if isinstance(post.get("attachments"), dict) else []
    for key in keys:
        found = media_by_key.get(str(key))
        if found:
            items.append(found)
    for inline in post.get("media") or []:
        if isinstance(inline, dict):
            items.append(inline)
    photos: list[dict] = []
    all_m: list[dict] = []
    seen: set[str] = set()
    for item in items:
        converted = _as_media_item(item)
        if not converted:
            continue
        marker = converted.get("url") or converted.get("thumbnail_url") or ""
        if marker and marker in seen:
            continue
        if marker:
            seen.add(marker)
        all_m.append(converted)
        if converted["type"] == "photo" and converted.get("url"):
            photos.append({"url": converted["url"]})
    return {"photos": photos, "all": all_m}


def _metric(metrics: dict, *keys: str):
    for key in keys:
        if key in metrics and metrics.get(key) is not None:
            return metrics.get(key)
    return None


def normalize_post(
    post: dict,
    *,
    handle: str,
    author_id: str,
    users: dict[str, dict],
    media_by_key: dict[str, dict],
    posts_by_id: dict[str, dict],
) -> dict | None:
    post_id = str(post.get("id") or "").strip()
    if not ID_RE.fullmatch(post_id):
        return None
    author_key = str(post.get("author_id") or "").strip()
    inline = post.get("author") if isinstance(post.get("author"), dict) else {}
    user = users.get(author_key) or {}
    username = _username(user) or _username(inline)
    name = ""
    if isinstance(user, dict):
        name = str(user.get("name") or "")
    if not name and isinstance(inline, dict):
        name = str(inline.get("name") or "")
    if not username and author_key and author_id and author_key == str(author_id):
        username = (handle or "").lstrip("@")
    resolved_author_id = author_key or str(inline.get("id") or "")

    conversation_id = str(post.get("conversation_id") or "").strip()
    replied = _ref(post, "replied_to")
    retweeted = _ref(post, "retweeted")
    quoted = _ref(post, "quoted")
    replying_to_status = None
    if replied:
        replying_to_status = str(replied.get("id"))
    elif conversation_id and conversation_id != post_id:
        replying_to_status = conversation_id

    quote = None
    if quoted:
        quoted_id = str(quoted.get("id"))
        quoted_post = posts_by_id.get(quoted_id) or {}
        q_user = users.get(str(quoted_post.get("author_id") or "")) or {}
        q_name = _username(q_user)
        if q_name:
            q_url = f"https://x.com/{q_name}/status/{quoted_id}"
        elif isinstance(quoted_post.get("url"), str) and "/status/" in quoted_post["url"]:
            q_url = quoted_post["url"].split("?")[0]
        else:
            q_url = f"https://x.com/i/status/{quoted_id}"
        quote = {
            "id": quoted_id,
            "url": q_url,
            "text": post_text(quoted_post) if quoted_post else "",
            "author": {"screen_name": q_name, "id": quoted_post.get("author_id")},
        }

    metrics = post.get("public_metrics") if isinstance(post.get("public_metrics"), dict) else {}
    tweet: dict = {
        "provider": "x-api",
        "id": post_id,
        "url": post.get("url") or (f"https://x.com/{username}/status/{post_id}" if username else ""),
        "text": post_text(post),
        "author": {"screen_name": username, "id": resolved_author_id, "name": name},
        "created_at": post.get("created_at"),
        "lang": post.get("lang"),
        "possibly_sensitive": post.get("possibly_sensitive"),
        "reply_settings": post.get("reply_settings"),
        "replying_to": None,
        "replying_to_status": replying_to_status,
        "reposted_by": {"id": str(retweeted.get("id"))} if retweeted else None,
        "quote": quote,
        "media": _collect_media(post, media_by_key),
        "conversation_id": conversation_id or post_id,
    }
    likes = _metric(metrics, "like_count", "likes")
    reposts = _metric(metrics, "repost_count", "retweet_count", "retweets")
    replies = _metric(metrics, "reply_count", "replies")
    quotes = _metric(metrics, "quote_count", "quotes")
    bookmarks = _metric(metrics, "bookmark_count", "bookmarks")
    if likes is not None:
        tweet["likes"] = likes
    if reposts is not None:
        tweet["retweets"] = reposts
    if replies is not None:
        tweet["replies"] = replies
    if quotes is not None:
        tweet["quotes"] = quotes
    if bookmarks is not None:
        tweet["bookmarks"] = bookmarks
    if "impression_count" in metrics or "views" in metrics:
        views = _metric(metrics, "impression_count", "views")
        if views is not None:
            tweet["views"] = views
    source = post.get("source")
    if isinstance(source, str) and source.strip() and not source.strip().startswith("http"):
        tweet["app_source"] = source.strip()
    return tweet


def posts_by_id(payload: object, *, handle: str, author_id: str = DEFAULT_AUTHOR_ID) -> dict[str, dict]:
    posts, includes = iter_post_records(payload)
    users = _index_by(list(includes.get("users") or []), "id")
    media_items = [item for item in (includes.get("media") or []) if isinstance(item, dict)]
    for post in posts:
        nested = post.get("includes") if isinstance(post.get("includes"), dict) else {}
        media_items.extend(item for item in (nested.get("media") or []) if isinstance(item, dict))
        for user in nested.get("users") or []:
            if isinstance(user, dict) and user.get("id") is not None:
                users.setdefault(str(user["id"]), user)
    media_by_key = _index_by(media_items, "media_key")
    expanded: dict[str, dict] = {}
    for key in ("posts", "tweets"):
        for item in includes.get(key) or []:
            if isinstance(item, dict) and item.get("id") is not None:
                expanded[str(item["id"])] = item
    for post in posts:
        if post.get("id") is not None:
            expanded.setdefault(str(post["id"]), post)
    found: dict[str, dict] = {}
    for post in posts:
        tweet = normalize_post(
            post,
            handle=handle,
            author_id=author_id,
            users=users,
            media_by_key=media_by_key,
            posts_by_id=expanded,
        )
        if tweet:
            found[tweet["id"]] = tweet
    return found
