"""Notion block tree for one X Post page body.

Order: optional thread callout, quote (tweet text), optional quoting line,
native embed, image and video blocks, optional raw-JSON toggle.
The Notes section is appended by ``notes.compose_body``, not here.
"""
from __future__ import annotations

import json
import urllib.parse

from posts import PostPlan

RICH_TEXT_MAX = 2000
RAW_JSON_MAX = 12000
NOTION_URL_MAX = 2000


def utf16_len(text: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def rt_chunks(text: str) -> list[dict]:
    text = text if text else "(no text)"
    chunks = []
    buf: list[str] = []
    units = 0
    for ch in text:
        cu = 2 if ord(ch) > 0xFFFF else 1
        if units + cu > RICH_TEXT_MAX and buf:
            chunks.append({"type": "text", "text": {"content": "".join(buf)}})
            buf = []
            units = 0
        buf.append(ch)
        units += cu
    if buf:
        chunks.append({"type": "text", "text": {"content": "".join(buf)}})
    return chunks


def _paragraph(rich: list[dict]) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich}}


def _text(content: str, link: str | None = None) -> dict:
    text: dict = {"content": content}
    if link:
        text["link"] = {"url": link}
    return {"type": "text", "text": text}


def looks_like_mp4(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith(".mp4") or ".mp4" in path


def filename_for(url: str, fallback: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = path.rsplit("/", 1)[-1] or fallback
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)[:80]
    return cleaned or fallback


def raw_json_text(raw: dict, limit: int = RAW_JSON_MAX) -> str:
    text = json.dumps(raw, indent=2, ensure_ascii=False)
    if utf16_len(text) <= limit:
        return text
    smaller = dict(raw)
    smaller["truncated"] = True
    quote = smaller.get("quote")
    if isinstance(quote, dict) and quote.get("text"):
        quote = dict(quote)
        quote["text"] = (quote.get("text") or "")[:280]
        smaller["quote"] = quote
    text = json.dumps(smaller, indent=2, ensure_ascii=False)
    if utf16_len(text) <= limit:
        return text
    # Keep the payload valid JSON by shrinking the tweet text, not slicing mid-token.
    smaller["text"] = (smaller.get("text") or "")[:500]
    text = json.dumps(smaller, indent=2, ensure_ascii=False)
    if utf16_len(text) <= limit:
        return text
    return json.dumps({"source": "x-api", "id": raw.get("id"), "truncated": True}, indent=2)


def image_block(url: str, upload_id: str | None) -> dict | None:
    if upload_id:
        return {
            "object": "block",
            "type": "image",
            "image": {"type": "file_upload", "file_upload": {"id": upload_id}},
        }
    if not url or len(url) > NOTION_URL_MAX:
        return None
    return {
        "object": "block",
        "type": "image",
        "image": {"type": "external", "external": {"url": url}},
    }


def video_block(url: str, upload_id: str | None) -> dict | None:
    if upload_id:
        return {
            "object": "block",
            "type": "video",
            "video": {"type": "file_upload", "file_upload": {"id": upload_id}},
        }
    if not url or len(url) > NOTION_URL_MAX:
        return None
    return {
        "object": "block",
        "type": "video",
        "video": {"type": "external", "external": {"url": url}},
    }


def build_blocks(
    plan: PostPlan,
    *,
    image_uploads: dict[str, str | None] | None = None,
    video_uploads: dict[str, str | None] | None = None,
    include_raw: bool = True,
) -> list[dict]:
    image_uploads = image_uploads or {}
    video_uploads = video_uploads or {}
    blocks: list[dict] = []
    if plan.thread_note:
        blocks.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": "Thread root. Only this first post is stored; self-replies are excluded.",
                    },
                }],
                "icon": {"type": "emoji", "emoji": "🧵"},
                "color": "blue_background",
            },
        })
    blocks.append({
        "object": "block",
        "type": "quote",
        "quote": {"rich_text": rt_chunks(plan.text)},
    })
    if plan.quoted_url:
        if plan.quote_author:
            rich = [
                _text("Quoting "),
                _text(f"@{plan.quote_author}", plan.quoted_url),
            ]
        else:
            rich = [_text(plan.quoted_url, plan.quoted_url)]
        blocks.append(_paragraph(rich))
    if plan.url:
        blocks.append({
            "object": "block",
            "type": "embed",
            "embed": {"url": plan.url},
        })
    for url in plan.photos:
        block = image_block(url, image_uploads.get(url))
        if block:
            blocks.append(block)
    for url in plan.videos:
        block = video_block(url, video_uploads.get(url))
        if block:
            blocks.append(block)
    if include_raw:
        blocks.append({
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": [_text("Raw JSON")],
                "children": [{
                    "object": "block",
                    "type": "code",
                    "code": {
                        "language": "json",
                        "rich_text": rt_chunks(raw_json_text(plan.raw)),
                    },
                }],
            },
        })
    return blocks
