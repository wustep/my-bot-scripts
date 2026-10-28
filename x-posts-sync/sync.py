#!/usr/bin/env python3
"""Sync a Notion X Posts archive from an X API post dump.

No LLM and no FxTwitter/VxTwitter calls. The Sunday routine reads posts with
the free X reader, writes that JSON to a file, then runs:

    python3 x-posts-sync/sync.py --refresh-all --posts-json /path/to/posts.json

The Notion token is read from ``NOTION_API_TOKEN`` or a .env file next to this script.
It is never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from body import build_blocks, filename_for, looks_like_mp4  # noqa: E402
from notes import compose_body  # noqa: E402
from posts import (  # noqa: E402
    coerce_post_id,
    parse_status_url,
    parse_time,
    plan_post,
)
from xapi import DEFAULT_AUTHOR_ID, load_posts_file, posts_by_id  # noqa: E402
from notion_io import (  # noqa: E402
    DEFAULT_DATABASE_ID,
    DEFAULT_DATA_SOURCE_ID,
    DEFAULT_ENV_FILE,
    NotionClient,
    NotionError,
    load_token,
    page_properties,
    parse_page,
)
from worklist import Row, WorkItem, assemble_work, row_in_scope  # noqa: E402

PROGRESS_NAME = "progress.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sync.py",
        description="Refresh and upsert the Notion X Posts database (Tweet and Quote roots).",
    )
    parser.add_argument("--dry-run", action="store_true", help="read Notion and the posts dump, write nothing")
    parser.add_argument("--limit", type=int, default=0, help="max posts to process this run (0 = no cap)")
    parser.add_argument("--only", default="", help="one status id or status URL")
    parser.add_argument("--refresh-all", action="store_true", help="also log archive rows missing from --posts-json")
    parser.add_argument("--new-only", action="store_true", help="create missing rows only; do not update existing pages")
    parser.add_argument("--resume", action="store_true", help="skip Post IDs already ok in the progress file")
    parser.add_argument("--since", default="", help="ISO date or datetime lower bound (UTC)")
    parser.add_argument("--posts-json", default="", help="X API v2 dump: {data, includes} or a list of posts")
    parser.add_argument("--from-json", default="", help="optional id/URL list; content still comes from --posts-json")
    parser.add_argument("--ids", default="", help="comma or space separated status ids or URLs")
    parser.add_argument("--no-host-videos", action="store_true", help="external video blocks instead of Notion file uploads")
    parser.add_argument("--host-images", action="store_true", help="host photos via Notion file uploads (default: external pbs.twimg)")
    parser.add_argument("--no-raw-json", action="store_true", help="omit the raw JSON toggle")
    parser.add_argument("--sleep", type=float, default=0.35, help="pause between Notion writes, in seconds")
    parser.add_argument("--handle", default=os.environ.get("X_POSTS_HANDLE", ""))
    parser.add_argument("--author-id", default=os.environ.get("X_POSTS_AUTHOR_ID", DEFAULT_AUTHOR_ID))
    parser.add_argument("--database-id", default=os.environ.get("X_POSTS_DATABASE_ID", DEFAULT_DATABASE_ID))
    parser.add_argument("--data-source-id", default=os.environ.get("X_POSTS_DATA_SOURCE_ID", DEFAULT_DATA_SOURCE_ID))
    parser.add_argument("--env-file", default=os.environ.get("NOTION_ENV_FILE", DEFAULT_ENV_FILE))
    parser.add_argument("--progress", default=str(ROOT / PROGRESS_NAME))
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit must be >= 0")
    if args.only.strip() and not coerce_post_id(args.only.strip()):
        parser.error(f"--only is not a status id or URL: {args.only}")
    if args.since and parse_time(args.since) is None:
        parser.error(f"--since is not a date or datetime: {args.since}")
    return args


def parse_since(value: str) -> datetime:
    parsed = parse_time(value)
    if parsed is None:
        raise SystemExit(f"--since is not a date or datetime: {value}")
    return parsed


def load_incoming_text(text: str) -> list[dict]:
    stripped = text.strip()
    if not stripped:
        return []
    if stripped[0] in "[{":
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as e:
            raise SystemExit(f"--from-json is not valid JSON: {e}") from None
        if isinstance(data, dict):
            for key in ("posts", "ids", "items", "statuses"):
                if key in data:
                    data = data[key]
                    break
            else:
                data = [data]
        if not isinstance(data, list):
            raise SystemExit("--from-json must be a list or an object with posts/ids")
        rows = []
        for item in data:
            if isinstance(item, str):
                rows.append({"raw": item})
            elif isinstance(item, dict):
                raw = item.get("url") or item.get("status_url") or item.get("id") or item.get("post_id") or item.get("status_id") or ""
                handle = item.get("handle") or item.get("author") or ""
                rows.append({"raw": str(raw), "handle": str(handle or "")})
        return rows
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append({"raw": line})
    return rows


def load_incoming_file(path: str) -> list[dict]:
    if path == "-":
        return load_incoming_text(sys.stdin.read())
    file_path = Path(path)
    if not file_path.is_file():
        raise SystemExit(f"--from-json not found: {path}")
    return load_incoming_text(file_path.read_text())


def explicit_ids(args: argparse.Namespace) -> list[tuple[str, str]]:
    """Return ``(post_id, handle_hint)`` from --from-json, --ids, and --only."""
    blobs: list[dict] = []
    if args.from_json:
        blobs.extend(load_incoming_file(args.from_json))
    if args.ids.strip():
        for part in args.ids.replace(",", " ").split():
            blobs.append({"raw": part})
    if args.only.strip():
        blobs.append({"raw": args.only.strip()})
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for blob in blobs:
        raw = blob.get("raw") or ""
        handle_hint = (blob.get("handle") or "").lstrip("@")
        post_id = coerce_post_id(raw)
        if not post_id:
            _handle, sid, ok = parse_status_url(raw)
            post_id = sid if ok else None
            if _handle and _handle not in ("i", "home"):
                handle_hint = handle_hint or _handle
        if not post_id:
            print(f"SKIP unparsed id {raw[:120]}")
            continue
        if not handle_hint:
            handle, _sid, ok = parse_status_url(raw)
            if ok and handle and handle not in ("i", "home"):
                handle_hint = handle
        if post_id in seen:
            continue
        seen.add(post_id)
        found.append((post_id, handle_hint))
    return found


class DumpCache:
    """Posts already loaded from --posts-json. Never calls the network."""

    def __init__(self, tweets: dict[str, dict]):
        self.tweets = tweets

    def fetch(self, post_id: str, handle_hint: str = "") -> tuple[dict | None, str | None]:
        del handle_hint
        tweet = self.tweets.get(post_id)
        if tweet:
            return tweet, None
        return None, "not_in_posts_json"


def rows_from_pages(pages: list[dict], handle: str) -> tuple[list[Row], Counter]:
    skipped: Counter = Counter()
    rows: list[Row] = []
    for page in pages:
        parsed = parse_page(page)
        if parsed["archived"]:
            skipped["archived"] += 1
            continue
        post_id = coerce_post_id(parsed["post_id"]) or coerce_post_id(parsed["status_url"]) or ""
        if not row_in_scope(parsed["type_name"], post_id, parsed["conversation_id"], parsed["author"], handle):
            if parsed["type_name"] not in ("Tweet", "Quote"):
                skipped["type"] += 1
            elif parsed["conversation_id"] and post_id and parsed["conversation_id"] != post_id:
                skipped["not_root"] += 1
            elif not post_id:
                skipped["no_post_id"] += 1
            else:
                skipped["author"] += 1
            continue
        rows.append(Row(
            page_id=parsed["page_id"],
            post_id=post_id,
            posted=parse_time(parsed["posted"]),
            notion_url=parsed["notion_url"],
            title=parsed["title"],
            status_url=parsed["status_url"],
        ))
    return rows, skipped


def load_progress(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                data.setdefault("ok", [])
                data.setdefault("fail", [])
                data.setdefault("skip", [])
                return data
        except (OSError, json.JSONDecodeError):
            print(f"progress file unreadable, starting fresh: {path}")
    return {"ok": [], "fail": [], "skip": []}


def save_progress(path: Path, prog: dict) -> None:
    prog["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(prog, indent=2))
    tmp.replace(path)


def record_fail(prog: dict, post_id: str, reason: str) -> None:
    prog["fail"] = [entry for entry in prog.get("fail") or [] if entry.get("post_id") != post_id]
    prog.setdefault("fail", []).append({"post_id": post_id, "reason": reason[:300]})


def metric_line(plan) -> str:
    parts = [plan.type_name]
    labels = (
        ("likes", "likes"),
        ("reposts", "reposts"),
        ("replies", "replies"),
        ("quotes", "quotes"),
        ("bookmarks", "bookmarks"),
    )
    for label, key in labels:
        value = plan.metrics.get(key)
        if value is not None:
            parts.append(f"{label}={value}")
    impressions = plan.metrics.get("impressions")
    if impressions is None:
        parts.append("impressions=unchanged")
    else:
        parts.append(f"impressions={impressions}")
    parts.append(f"has_media={'yes' if plan.has_media else 'no'}")
    return " ".join(parts)


def host_media(notion: NotionClient, plan, *, host_videos: bool, host_images: bool) -> tuple[dict, dict]:
    image_uploads: dict[str, str | None] = {}
    video_uploads: dict[str, str | None] = {}
    if host_images:
        for index, url in enumerate(plan.photos):
            image_uploads[url] = notion.host_external_url(url, filename_for(url, f"{plan.post_id}_img_{index}.jpg"))
    if host_videos:
        for index, url in enumerate(plan.videos):
            if not looks_like_mp4(url):
                video_uploads[url] = None
                continue
            video_uploads[url] = notion.host_external_url(url, filename_for(url, f"{plan.post_id}_{index}.mp4"))
    return image_uploads, video_uploads


def write_one(notion: NotionClient, item: WorkItem, plan, *, host_videos: bool, host_images: bool, include_raw: bool) -> str:
    image_uploads, video_uploads = host_media(
        notion, plan, host_videos=host_videos, host_images=host_images,
    )
    children = build_blocks(
        plan,
        image_uploads=image_uploads,
        video_uploads=video_uploads,
        include_raw=include_raw,
    )
    if item.create:
        page = notion.create_page(page_properties(plan, create=True), compose_body(children, []))
        return page.get("url") or page.get("id") or ""
    last_url = ""
    for page_id in item.page_ids:
        notion.update_properties(page_id, page_properties(plan, create=False))
        notion.replace_children(page_id, children)
        last_url = page_id
    return last_url


def _catalog_ids(plans: dict, since_dt: datetime | None) -> tuple[list[str], Counter]:
    """In-scope dump ids, newest first, plus skip reasons for the rest."""
    skipped: Counter = Counter()
    chosen: list[tuple[datetime, str]] = []
    for post_id, plan in plans.items():
        if not plan.in_scope:
            skipped[plan.reason or "out_of_scope"] += 1
            continue
        if since_dt and plan.posted_dt and plan.posted_dt < since_dt:
            skipped["before_since"] += 1
            continue
        chosen.append((plan.posted_dt or datetime.min.replace(tzinfo=timezone.utc), post_id))
    chosen.sort(reverse=True)
    return [post_id for _when, post_id in chosen], skipped


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.posts_json:
        raise SystemExit(
            "Pass --posts-json with an X API dump (data + includes, or a list of posts). "
            "Sunday: python3 x-posts-sync/sync.py "
            "--refresh-all --posts-json /path/to/posts.json"
        )
    handle = args.handle.lstrip("@")
    missing = [n for n, v in (("--handle/X_POSTS_HANDLE", handle), ("--database-id/X_POSTS_DATABASE_ID", args.database_id), ("--data-source-id/X_POSTS_DATA_SOURCE_ID", args.data_source_id)) if not v]
    if missing:
        raise SystemExit("Set " + ", ".join(missing))
    supplied = posts_by_id(load_posts_file(args.posts_json), handle=handle, author_id=args.author_id)
    if not supplied:
        print("--posts-json contained no posts")
        print(json.dumps({"ok": 0, "fail": 0, "skip": 0, "posts_json": 0}))
        return 2
    plans = {post_id: plan_post(tweet, handle) for post_id, tweet in supplied.items()}
    catalog, catalog_skips = _catalog_ids(plans, parse_since(args.since) if args.since else None)
    print(f"posts_json={len(supplied)} in_scope={len(catalog)} {dict(catalog_skips)}")

    token = load_token(args.env_file)
    notion = NotionClient(token, args.database_id, args.data_source_id, pause_s=0 if args.dry_run else args.sleep)
    print(f"query handle=@{handle} dry_run={args.dry_run}")
    pages = notion.query_pages()
    rows, skipped = rows_from_pages(pages, handle)
    print(f"existing_in_scope={len(rows)} existing_skipped={sum(skipped.values())} {dict(skipped)}")

    since_dt = parse_since(args.since) if args.since else None
    only_id = coerce_post_id(args.only.strip()) or ""
    explicit = explicit_ids(args)
    explicit_order = [post_id for post_id, _hint in explicit]
    narrow = bool(args.from_json or args.ids or args.only)
    cache = DumpCache(supplied)

    counts = Counter()
    for reason, count in catalog_skips.items():
        print(f"skip_dump {reason}={count}")

    if narrow:
        incoming = []
        for post_id in explicit_order:
            plan = plans.get(post_id)
            if plan is None:
                counts["skip"] += 1
                print(f"SKIP {post_id} not_in_posts_json")
                continue
            if not plan.in_scope:
                counts["skip"] += 1
                print(f"SKIP {post_id} {plan.reason}")
                continue
            if since_dt and plan.posted_dt and plan.posted_dt < since_dt:
                counts["skip"] += 1
                print(f"SKIP {post_id} before_since")
                continue
            incoming.append(post_id)
    else:
        incoming = list(catalog)

    if args.refresh_all and not narrow:
        for row in rows:
            if row.post_id not in supplied:
                counts["skip"] += 1
                print(f"SKIP {row.post_id} not_in_posts_json")
                continue
            plan = plans.get(row.post_id)
            if plan and not plan.in_scope:
                counts["skip"] += 1
                print(f"SKIP {row.post_id} {plan.reason}")

    # Content only exists in the dump, so do not walk Notion rows looking for a live fetch.
    do_refresh = False
    items = assemble_work(
        rows,
        incoming,
        refresh_existing=do_refresh,
        new_only=args.new_only,
        since=since_dt,
        limit=0,
        only=only_id,
    )
    progress_path = Path(args.progress)
    prog = load_progress(progress_path) if args.resume else {
        "run_started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ok": [],
        "fail": [],
        "skip": [],
    }
    if args.resume:
        done = {str(item) for item in prog.get("ok") or []}
        before = len(items)
        items = [item for item in items if item.post_id not in done]
        print(f"resume skipped_ok={before - len(items)}")
    if args.limit:
        items = items[: args.limit]

    creates = sum(1 for item in items if item.create)
    updates = len(items) - creates
    print(f"plan update={updates} create={creates} refresh_existing={do_refresh}")

    samples: list[str] = []

    for item in items:
        tweet, err = cache.fetch(item.post_id)
        if not tweet:
            counts["skip"] += 1
            print(f"SKIP {item.post_id} {err or 'not_in_posts_json'}")
            continue
        plan = plans.get(item.post_id) or plan_post(tweet, handle)
        if not plan.in_scope:
            counts["skip"] += 1
            print(f"SKIP {item.post_id} {plan.reason}")
            continue
        if since_dt and plan.posted_dt and plan.posted_dt < since_dt and item.origin == "incoming":
            counts["skip"] += 1
            print(f"SKIP {item.post_id} before_since")
            continue
        action = "CREATE" if item.create else "UPDATE"
        print(f"{'DRY ' if args.dry_run else ''}{action} {item.post_id} {metric_line(plan)} pages={len(item.page_ids) or 1}")
        if args.dry_run:
            counts["dry_run"] += 1
            continue
        try:
            url = write_one(
                notion,
                item,
                plan,
                host_videos=not args.no_host_videos,
                host_images=args.host_images,
                include_raw=not args.no_raw_json,
            )
        except NotionError as e:
            counts["fail"] += 1
            record_fail(prog, item.post_id, str(e))
            print(f"FAIL {item.post_id} {e}")
            save_progress(progress_path, prog)
            continue
        except Exception as e:
            counts["fail"] += 1
            record_fail(prog, item.post_id, str(e))
            print(f"FAIL {item.post_id} {e}")
            save_progress(progress_path, prog)
            continue
        counts["ok"] += 1
        if item.create:
            counts["created"] += 1
        else:
            counts["updated"] += 1
        if url and len(samples) < 3:
            samples.append(str(url))
        ok_ids = [str(value) for value in prog.setdefault("ok", [])]
        if item.post_id not in ok_ids:
            prog["ok"].append(item.post_id)
        prog["fail"] = [entry for entry in prog.get("fail") or [] if entry.get("post_id") != item.post_id]
        save_progress(progress_path, prog)

    summary = {
        "ok": counts["ok"],
        "fail": counts["fail"],
        "skip": counts["skip"],
        "created": counts["created"],
        "updated": counts["updated"],
        "dry_run": counts["dry_run"],
        "existing_in_scope": len(rows),
        "posts_json": len(supplied),
        "in_scope": len(catalog),
        "dump_skipped": sum(catalog_skips.values()),
        "samples": samples,
    }
    print(json.dumps(summary))
    if counts["fail"]:
        return 1
    return 0


def main() -> None:
    try:
        code = run()
    except NotionError as e:
        print(f"FAIL {e}")
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
