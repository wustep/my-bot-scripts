"""Notion public API for the X Posts data source.

Token: ``NOTION_API_TOKEN``, else the first assignment in ``--env-file``
(default ``.env`` next to this script, same fallback as
``saved-tweets/rewrite_tweets_ntn.py``). The token is never printed.

Queries prefer ``POST /v1/data_sources/{id}/query`` (Notion-Version
2025-09-03). If that route is missing, falls back to
``POST /v1/databases/{id}/query`` on 2022-06-28. Block children and
``file_uploads`` use 2022-06-28, matching saved-tweets.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from notes import compose_body, preserved_note_blocks
from posts import REPLY_SETTINGS, PostPlan

API = "https://api.notion.com/v1"
NOTION_VERSION_BLOCKS = "2022-06-28"
NOTION_VERSION_DATA = "2025-09-03"
DEFAULT_ENV_FILE = str(Path(__file__).with_name(".env"))
DEFAULT_DATABASE_ID = ""  # set X_POSTS_DATABASE_ID or --database-id
DEFAULT_DATA_SOURCE_ID = ""  # set X_POSTS_DATA_SOURCE_ID or --data-source-id

SCOPE_FILTER = {
    "or": [
        {"property": "Type", "select": {"equals": "Tweet"}},
        {"property": "Type", "select": {"equals": "Quote"}},
    ]
}

NUMBER_PROPS = {
    "likes": "Likes",
    "reposts": "Reposts",
    "replies": "Replies",
    "quotes": "Quotes",
    "bookmarks": "Bookmarks",
    "impressions": "Impressions",
}


class NotionError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def dash_id(value: str) -> str:
    compact = (value or "").replace("-", "").strip().lower()
    if len(compact) != 32 or any(ch not in "0123456789abcdef" for ch in compact):
        return value
    return f"{compact[0:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:32]}"


def normalize_notion_id(value: str) -> str:
    """Accept a dashed id, a 32-hex id, or a Notion URL. Empty stays empty."""
    import re
    text = (value or "").strip()
    if not text:
        return ""
    match = re.search(r"([0-9a-fA-F]{32})", text.replace("-", ""))
    if not match:
        raise SystemExit(f"not a Notion id: {value}")
    return dash_id(match.group(1))


def load_token(env_file: str | None = None) -> str:
    token = os.environ.get("NOTION_API_TOKEN") or ""
    path = Path(env_file or os.environ.get("NOTION_ENV_FILE") or DEFAULT_ENV_FILE)
    if not token and path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if line.startswith("NOTION_API_TOKEN=") and not line.startswith("#"):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                os.environ["NOTION_API_TOKEN"] = token
                break
    if not token:
        raise SystemExit(
            "NOTION_API_TOKEN is not set. Export it, or put it in "
            f"{path} (that file is not committed)."
        )
    return token


def plain(prop) -> str:
    if not isinstance(prop, dict):
        return ""
    kind = prop.get("type")
    if kind in ("title", "rich_text"):
        return "".join((part.get("plain_text") or "") for part in (prop.get(kind) or [])).strip()
    if kind == "url":
        return (prop.get("url") or "").strip()
    if kind == "select":
        return ((prop.get("select") or {}).get("name") or "").strip()
    if kind == "status":
        return ((prop.get("status") or {}).get("name") or "").strip()
    if kind == "date":
        return ((prop.get("date") or {}).get("start") or "").strip()
    if kind == "number":
        number = prop.get("number")
        return "" if number is None else str(number)
    if kind == "checkbox":
        return "yes" if prop.get("checkbox") else ""
    return ""


def parse_page(page: dict) -> dict:
    props = page.get("properties") or {}
    return {
        "page_id": page.get("id") or "",
        "notion_url": page.get("url") or "",
        "archived": bool(page.get("archived") or page.get("in_trash")),
        "post_id": plain(props.get("Post ID")),
        "status_url": plain(props.get("URL")),
        "type_name": plain(props.get("Type")),
        "posted": plain(props.get("Posted")),
        "author": plain(props.get("Author")),
        "conversation_id": plain(props.get("Conversation ID")),
        "title": plain(props.get("Name")),
    }


def _rt(content: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": content[:2000]}}]}


def page_properties(plan: PostPlan, *, create: bool) -> dict:
    """Property patch. Omits Impressions when ``views`` was missing (metrics value None).

    Existing Name titles are not included on update.
    """
    props: dict = {}
    if create:
        props["Name"] = {"title": [{"type": "text", "text": {"content": (plan.title or f"Post {plan.post_id}")[:2000]}}]}
    if plan.type_name in ("Tweet", "Quote"):
        props["Type"] = {"select": {"name": plan.type_name}}
    if plan.post_id:
        props["Post ID"] = _rt(plan.post_id)
    if plan.url:
        props["URL"] = {"url": plan.url}
    if plan.posted_iso:
        props["Posted"] = {"date": {"start": plan.posted_iso}}
    if plan.author_at:
        props["Author"] = _rt(plan.author_at)
    if plan.author_id:
        props["Author ID"] = _rt(plan.author_id)
    if plan.conversation_id:
        props["Conversation ID"] = _rt(plan.conversation_id)
    if plan.lang:
        props["Lang"] = _rt(plan.lang)
    props["Has media"] = {"checkbox": bool(plan.has_media)}
    if plan.sensitive is not None:
        props["Sensitive"] = {"checkbox": bool(plan.sensitive)}
    if plan.quoted_url:
        props["Quoted post"] = _rt(plan.quoted_url)
    if plan.reply_settings in REPLY_SETTINGS:
        props["Reply settings"] = {"select": {"name": plan.reply_settings}}
    if plan.app_source:
        props["Source"] = _rt(plan.app_source)
    for key, name in NUMBER_PROPS.items():
        value = (plan.metrics or {}).get(key)
        if value is None:
            continue
        props[name] = {"number": value}
    return props


class NotionClient:
    def __init__(self, token: str, database_id: str, data_source_id: str, pause_s: float = 0.35):
        self.token = token
        self.database_id = normalize_notion_id(database_id)
        self.data_source_id = normalize_notion_id(data_source_id)
        self.pause_s = pause_s
        self._parent_mode: str | None = None

    def _redact(self, text: str) -> str:
        if self.token and self.token in text:
            return text.replace(self.token, "[redacted]")
        return text

    def request(self, method: str, path: str, body: dict | None = None, version: str = NOTION_VERSION_BLOCKS,
                retries: int = 5) -> dict:
        url = path if path.startswith("http") else f"{API}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": version,
            "Content-Type": "application/json",
        }
        last = "request failed"
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    return json.loads(raw.decode("utf-8")) if raw else {}
            except urllib.error.HTTPError as e:
                err_body = self._redact(e.read().decode("utf-8", errors="replace"))
                if e.code in (401, 403):
                    raise NotionError(
                        f"HTTP {e.code}: Notion rejected the token or the integration cannot access this database. "
                        f"{err_body[:300]}",
                        status=e.code,
                    ) from None
                if e.code == 429:
                    wait = min(30, 2 ** attempt)
                    try:
                        wait = float(e.headers.get("Retry-After") or wait)
                    except (TypeError, ValueError):
                        pass
                    time.sleep(min(30, max(0, wait)))
                    last = f"429 {err_body[:200]}"
                    continue
                if e.code in (500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    last = f"{e.code} {err_body[:200]}"
                    continue
                raise NotionError(f"HTTP {e.code}: {err_body[:500]}", status=e.code) from None
            except NotionError:
                raise
            except Exception as e:
                last = self._redact(str(e))[:300]
                if attempt < retries - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise NotionError(last) from None
        raise NotionError(self._redact(last))

    def _should_fallback(self, err: NotionError) -> bool:
        if err.status == 404:
            return True
        msg = str(err).lower()
        needles = (
            "invalid request url",
            "could not find database",
            "could not find data source",
            "could not find data_source",
            "unrecognized path",
            "not supported in this api version",
            "multiple data sources",
        )
        return err.status == 400 and any(needle in msg for needle in needles)

    def _paginate(self, path: str, version: str, query_filter: dict | None) -> list[dict]:
        pages: list[dict] = []
        cursor = None
        for _ in range(500):
            body: dict = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            if query_filter:
                body["filter"] = query_filter
            data = self.request("POST", path, body, version=version)
            batch = data.get("results") or []
            pages.extend(batch)
            if not data.get("has_more"):
                return pages
            cursor = data.get("next_cursor")
            if not cursor:
                return pages
            time.sleep(0.05)
        raise NotionError("pagination cap exceeded while reading X Posts")

    def _query_all(self, path: str, version: str) -> list[dict]:
        try:
            return self._paginate(path, version, SCOPE_FILTER)
        except NotionError as e:
            if e.status == 400 and not self._should_fallback(e):
                print("Type filter was rejected; reading every row and filtering locally")
                return self._paginate(path, version, None)
            raise

    def query_pages(self) -> list[dict]:
        if self.data_source_id and self._parent_mode != "database":
            try:
                pages = self._query_all(
                    f"/data_sources/{self.data_source_id}/query",
                    NOTION_VERSION_DATA,
                )
                self._parent_mode = "data_source"
                return pages
            except NotionError as e:
                if not self._should_fallback(e) or not self.database_id:
                    raise
                print(f"data source query failed (HTTP {e.status}); using database endpoint")
                self._parent_mode = "database"
        if not self.database_id:
            raise NotionError("no database id or data source id configured")
        self._parent_mode = "database"
        return self._query_all(f"/databases/{self.database_id}/query", NOTION_VERSION_BLOCKS)

    def update_properties(self, page_id: str, properties: dict) -> dict:
        result = self.request("PATCH", f"/pages/{dash_id(page_id)}", {"properties": properties})
        if self.pause_s:
            time.sleep(self.pause_s)
        return result

    def create_page(self, properties: dict, children: list[dict]) -> dict:
        first, rest = children[:100], children[100:]
        if self._parent_mode == "database" or not self.data_source_id:
            page = self._create_database(properties, first)
        else:
            try:
                page = self._create_data_source(properties, first)
                self._parent_mode = "data_source"
            except NotionError as e:
                if self._parent_mode == "data_source" or not self._should_fallback(e) or not self.database_id:
                    raise
                print(f"data source create failed (HTTP {e.status}); retrying with database parent")
                self._parent_mode = "database"
                page = self._create_database(properties, first)
        if rest:
            self.append_children(page["id"], rest)
        return page

    def _create_data_source(self, properties: dict, children: list[dict]) -> dict:
        body = {
            "parent": {"type": "data_source_id", "data_source_id": self.data_source_id},
            "properties": properties,
        }
        if children:
            body["children"] = children
        result = self.request("POST", "/pages", body, version=NOTION_VERSION_DATA)
        if self.pause_s:
            time.sleep(self.pause_s)
        return result

    def _create_database(self, properties: dict, children: list[dict]) -> dict:
        body = {"parent": {"database_id": self.database_id}, "properties": properties}
        if children:
            body["children"] = children
        result = self.request("POST", "/pages", body, version=NOTION_VERSION_BLOCKS)
        if self.pause_s:
            time.sleep(self.pause_s)
        return result

    def list_children(self, page_id: str) -> list[dict]:
        results = []
        cursor = None
        pid = dash_id(page_id)
        while True:
            query = f"/blocks/{pid}/children?page_size=100"
            if cursor:
                query += f"&start_cursor={cursor}"
            data = self.request("GET", query)
            results.extend(data.get("results") or [])
            if not data.get("has_more"):
                return results
            cursor = data.get("next_cursor")
            if not cursor:
                return results
            time.sleep(0.05)

    def append_children(self, page_id: str, children: list[dict]) -> None:
        pid = dash_id(page_id)
        for index in range(0, len(children), 100):
            batch = children[index:index + 100]
            self.request("PATCH", f"/blocks/{pid}/children", {"children": batch})
            if self.pause_s:
                time.sleep(self.pause_s)

    def replace_children(self, page_id: str, sync_blocks: list[dict]) -> int:
        """Replace sync-owned blocks and keep the Notes section at the bottom.

        A failed append leaves the old body in place. Notes are copied from the
        first Notes heading (or the sentinel comment) through the end of the page.
        """
        existing = self.list_children(page_id)
        notes = preserved_note_blocks(existing, self.list_children)
        children = compose_body(sync_blocks, notes)
        old_ids = [block["id"] for block in existing if block.get("id")]
        self.append_children(page_id, children)
        deleted = 0
        for block_id in old_ids:
            try:
                self.request("DELETE", f"/blocks/{block_id}")
                deleted += 1
                time.sleep(0.05)
            except NotionError as e:
                if "archived" not in str(e).lower():
                    raise
        return deleted

    def host_external_url(self, file_url: str, filename: str, poll_s: float = 45.0) -> str | None:
        """Notion ``file_uploads`` mode ``external_url``. None if hosting fails."""
        try:
            created = self.request("POST", "/file_uploads", {
                "mode": "external_url",
                "external_url": file_url,
                "filename": filename,
            })
        except NotionError as e:
            print(f"  file_upload create failed: {e}")
            return None
        upload_id = created.get("id")
        status = created.get("status")
        if status == "uploaded":
            return upload_id
        deadline = time.time() + poll_s
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                info = self.request("GET", f"/file_uploads/{upload_id}")
            except NotionError as e:
                print(f"  file_upload poll failed: {e}")
                return None
            status = info.get("status")
            if status == "uploaded":
                return upload_id
            if status in ("failed", "expired"):
                print(f"  file_upload status={status}")
                return None
        print(f"  file_upload poll timeout status={status}")
        return None
