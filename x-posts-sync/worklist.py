"""Decide which Post IDs to create or refresh. Pure: no network."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

DATETIME_MIN = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Row:
    page_id: str
    post_id: str
    posted: datetime | None
    notion_url: str
    title: str
    status_url: str


@dataclass(frozen=True)
class WorkItem:
    post_id: str
    page_ids: tuple[str, ...]
    create: bool
    origin: str  # "incoming" or "db"


def author_matches(author: str, handle: str) -> bool:
    if not (author or "").strip():
        return True
    return author.lstrip("@").strip().lower() == (handle or "").lstrip("@").strip().lower()


def row_in_scope(type_name: str, post_id: str, conversation_id: str, author: str, handle: str) -> bool:
    """Tweet/Quote roots only. Empty conversation id is kept (treated as a root)."""
    if type_name not in ("Tweet", "Quote"):
        return False
    if not post_id:
        return False
    if conversation_id and conversation_id != post_id:
        return False
    if not author_matches(author, handle):
        return False
    return True


def refresh_existing_rows(*, new_only: bool, refresh_all: bool, has_source: bool) -> bool:
    """Default with no source flags refreshes the whole in-scope archive."""
    if new_only:
        return False
    return bool(refresh_all or not has_source)


def assemble_work(
    rows: list[Row],
    incoming_ids: list[str],
    *,
    refresh_existing: bool,
    new_only: bool,
    since: datetime | None,
    limit: int,
    only: str,
) -> list[WorkItem]:
    by_post: dict[str, list[Row]] = {}
    for row in rows:
        by_post.setdefault(row.post_id, []).append(row)

    items: list[WorkItem] = []
    seen: set[str] = set()

    def consider(post_id: str, origin: str) -> None:
        if not post_id or post_id in seen:
            return
        if only and post_id != only:
            return
        pages = by_post.get(post_id) or []
        if origin == "db":
            if not refresh_existing:
                return
            if since is not None:
                posted = [page.posted for page in pages if page.posted]
                if posted and max(posted) < since:
                    return
        if pages and new_only:
            return
        if not pages and origin == "db":
            return
        seen.add(post_id)
        items.append(WorkItem(
            post_id=post_id,
            page_ids=tuple(page.page_id for page in pages),
            create=not pages,
            origin=origin,
        ))

    for post_id in incoming_ids:
        consider(post_id, "incoming")
    if refresh_existing:
        ordered = sorted(rows, key=lambda row: (row.posted or DATETIME_MIN, row.post_id), reverse=True)
        for row in ordered:
            consider(row.post_id, "db")
    if limit and limit > 0:
        return items[:limit]
    return items
