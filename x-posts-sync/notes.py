"""Keep freeform notes at the bottom of an X Post page.

Sync-owned blocks are replaced every run. Anything from a Notes heading
(or an ``<!-- x-posts-sync:notes -->`` sentinel) through the end of the page
is copied back under a divider. New pages get an empty Notes heading.
"""
from __future__ import annotations

NOTES_SENTINEL = "<!-- x-posts-sync:notes -->"
_HEADINGS = {"heading_1", "heading_2", "heading_3"}
_RICH = {
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "quote",
    "callout",
    "toggle",
    "code",
}
_MAX_DEPTH = 4


def block_plain(block: dict) -> str:
    kind = block.get("type") or ""
    payload = block.get(kind) if isinstance(block.get(kind), dict) else {}
    parts: list[str] = []
    for item in payload.get("rich_text") or []:
        if not isinstance(item, dict):
            continue
        if item.get("plain_text"):
            parts.append(item["plain_text"])
        else:
            parts.append((item.get("text") or {}).get("content") or "")
    return "".join(parts)


def is_notes_marker(block: dict) -> bool:
    kind = block.get("type") or ""
    text = block_plain(block).strip()
    if NOTES_SENTINEL in text:
        return True
    if kind in _HEADINGS and text.lower().rstrip(":").strip() == "notes":
        return True
    return False


def notes_heading() -> dict:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": "Notes"}}],
        },
    }


def _sanitize_rich_text(items: list) -> list[dict]:
    cleaned: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        text = item.get("text") if isinstance(item.get("text"), dict) else {}
        content = text.get("content")
        if content is None:
            content = item.get("plain_text") or ""
        content = str(content)
        if len(content) > 2000:
            content = content[:2000]
        if content == "" and not (text.get("link") or {}).get("url"):
            continue
        entry: dict = {"type": "text", "text": {"content": content}}
        link = (text.get("link") or {}).get("url") if isinstance(text.get("link"), dict) else None
        if link:
            entry["text"]["link"] = {"url": link}
        annotations = item.get("annotations") if isinstance(item.get("annotations"), dict) else {}
        styled = any(annotations.get(flag) for flag in ("bold", "italic", "strikethrough", "underline", "code"))
        color = annotations.get("color") or "default"
        if styled or (color and color != "default"):
            entry["annotations"] = {
                "bold": bool(annotations.get("bold")),
                "italic": bool(annotations.get("italic")),
                "strikethrough": bool(annotations.get("strikethrough")),
                "underline": bool(annotations.get("underline")),
                "code": bool(annotations.get("code")),
                "color": color,
            }
        cleaned.append(entry)
    if not cleaned:
        cleaned.append({"type": "text", "text": {"content": " "}})
    return cleaned


def export_block(block: dict, children_of=None, depth: int = 0) -> dict | None:
    """Turn a Notion read-block into a block the append API will accept."""
    kind = block.get("type") or ""
    payload = block.get(kind) if isinstance(block.get(kind), dict) else {}
    children = _export_children(block, payload, children_of, depth)
    if kind in _RICH:
        body: dict = {"rich_text": _sanitize_rich_text(payload.get("rich_text") or [])}
        color = payload.get("color")
        if color and color != "default":
            body["color"] = color
        if kind == "to_do":
            body["checked"] = bool(payload.get("checked"))
        if kind == "code":
            body["language"] = payload.get("language") or "plain text"
        if kind == "callout":
            icon = payload.get("icon") if isinstance(payload.get("icon"), dict) else None
            if icon and icon.get("type") == "emoji" and icon.get("emoji"):
                body["icon"] = {"type": "emoji", "emoji": icon["emoji"]}
        if children and depth < _MAX_DEPTH:
            body["children"] = children
        return {"object": "block", "type": kind, kind: body}
    if kind == "divider":
        return {"object": "block", "type": "divider", "divider": {}}
    if kind == "bookmark":
        url = payload.get("url") or ""
        if not url:
            return None
        body = {"url": url}
        if payload.get("caption"):
            body["caption"] = _sanitize_rich_text(payload.get("caption") or [])
        return {"object": "block", "type": "bookmark", "bookmark": body}
    if kind == "embed":
        url = payload.get("url") or ""
        if not url:
            return None
        return {"object": "block", "type": "embed", "embed": {"url": url}}
    if kind in ("image", "video"):
        external = (payload.get("external") or {}).get("url") if isinstance(payload.get("external"), dict) else ""
        if payload.get("type") == "external" and external:
            return {
                "object": "block",
                "type": kind,
                kind: {"type": "external", "external": {"url": external}},
            }
        text = block_plain(block).strip() or f"[{kind}]"
        return {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
        }
    text = block_plain(block).strip()
    if not text:
        return None
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
    }


def _export_children(block: dict, payload: dict, children_of, depth: int) -> list[dict]:
    if depth >= _MAX_DEPTH:
        return []
    raw = block.get("children") or payload.get("children") or []
    if not raw and block.get("has_children") and block.get("id") and children_of:
        raw = children_of(block["id"]) or []
    exported = []
    for child in raw:
        if not isinstance(child, dict):
            continue
        item = export_block(child, children_of, depth + 1)
        if item:
            exported.append(item)
    return exported


def preserved_note_blocks(blocks: list[dict], children_of=None) -> list[dict]:
    """Copy the marker and every block under it. Empty when there is no marker."""
    for index, block in enumerate(blocks):
        if is_notes_marker(block):
            exported = []
            for item in blocks[index:]:
                copied = export_block(item, children_of)
                if copied:
                    exported.append(copied)
            return exported
    return []


def compose_body(sync_blocks: list[dict], notes: list[dict] | None) -> list[dict]:
    """Sync-owned blocks, a divider, then the Notes section."""
    body = list(sync_blocks)
    body.append({"object": "block", "type": "divider", "divider": {}})
    if notes:
        body.extend(notes)
    else:
        body.append(notes_heading())
    return body
