#!/usr/bin/env python3
"""Offline checks for X dump parsing, Notion properties, notes, and the work list."""
from __future__ import annotations

import contextlib
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from body import build_blocks, raw_json_text, rt_chunks, utf16_len  # noqa: E402
from notes import compose_body, is_notes_marker, preserved_note_blocks  # noqa: E402
from posts import coerce_post_id, parse_status_url, parse_time, plan_post  # noqa: E402
from notion_io import normalize_notion_id, page_properties, parse_page  # noqa: E402
from sync import load_incoming_text, parse_args  # noqa: E402
from worklist import Row, assemble_work, refresh_existing_rows, row_in_scope  # noqa: E402
from xapi import posts_by_id  # noqa: E402


def _author(screen="wustep", author_id="3146745024"):
    return {"screen_name": screen, "id": author_id, "name": "Stephen Wu"}


def _tweet(**overrides):
    base = {
        "id": "2104610435571884086",
        "url": "https://x.com/wustep/status/2104610435571884086",
        "text": "Last week, we launched column permissions!",
        "author": _author(),
        "likes": 154,
        "retweets": 13,
        "replies": 19,
        "quotes": 9,
        "bookmarks": 79,
        "views": 54675,
        "created_timestamp": 1790613204,
        "created_at": "Mon Sep 28 16:33:24 +0000 2026",
        "lang": "en",
        "possibly_sensitive": False,
        "replying_to": None,
        "replying_to_status": None,
        "reposted_by": None,
        "media": {"photos": [], "all": []},
    }
    base.update(overrides)
    return base


def test_parse_urls():
    handle, sid, ok = parse_status_url("https://x.com/wustep/status/2104610435571884086?s=20")
    assert (handle, sid, ok) == ("wustep", "2104610435571884086", True)
    handle, sid, ok = parse_status_url("https://twitter.com/i/web/status/2104366065887138086")
    assert (handle, sid, ok) == ("i", "2104366065887138086", True)
    assert parse_status_url("https://x.com/home")[2] is False
    assert coerce_post_id("2104610435571884086") == "2104610435571884086"
    assert coerce_post_id("https://x.com/wustep/status/2104610435571884086") == "2104610435571884086"
    assert coerce_post_id("nope") is None


def test_scope_and_metrics():
    plan = plan_post(_tweet(), "wustep")
    assert plan.in_scope and plan.type_name == "Tweet"
    assert plan.conversation_id == plan.post_id
    assert plan.author_at == "@wustep"
    assert plan.metrics["likes"] == 154
    assert plan.metrics["reposts"] == 13
    assert plan.metrics["impressions"] == 54675
    assert plan.posted_iso == "2026-09-28T16:33:24Z"
    props = page_properties(plan, create=False)
    assert "Name" not in props
    assert props["Impressions"]["number"] == 54675
    assert props["Reposts"]["number"] == 13
    assert props["URL"]["url"].endswith("/2104610435571884086")
    assert props["Has media"]["checkbox"] is False
    assert props["Type"]["select"]["name"] == "Tweet"

    missing_views = _tweet()
    del missing_views["views"]
    plan = plan_post(missing_views, "wustep")
    assert plan.metrics["impressions"] is None
    props = page_properties(plan, create=True)
    assert "Impressions" not in props
    assert "Name" in props

    zero = plan_post(_tweet(views=0), "wustep")
    assert zero.metrics["impressions"] == 0
    assert page_properties(zero, create=False)["Impressions"]["number"] == 0

    quote = _tweet(
        id="2104366065887138086",
        text="Flashback to Pokemon",
        quote={
            "id": "2104289146206629951",
            "url": "https://x.com/TomFrankly/status/2104289146206629951",
            "text": "quoted",
            "author": {"screen_name": "TomFrankly"},
        },
        media={"photos": [{"url": "https://pbs.twimg.com/media/a.jpg"}], "all": []},
    )
    plan = plan_post(quote, "Wustep")
    assert plan.in_scope and plan.type_name == "Quote"
    assert plan.quoted_url == "https://x.com/TomFrankly/status/2104289146206629951"
    assert plan.has_media is True
    assert plan.quote_author == "TomFrankly"

    reply = _tweet(replying_to="wustep", replying_to_status="2104610435571884086", id="2104610435571884999")
    assert plan_post(reply, "wustep").reason == "reply"
    retweet = _tweet(reposted_by={"screen_name": "wustep"}, author=_author("someone"))
    assert plan_post(retweet, "wustep").reason == "retweet"
    other = _tweet(author=_author("someoneelse"))
    assert plan_post(other, "wustep").reason == "other_author"
    assert plan_post(_tweet(id=""), "wustep").reason == "no_id"


def test_media_and_body_order():
    tweet = _tweet(
        text="trailer 🧵",
        media={
            "photos": [],
            "all": [{
                "type": "video",
                "thumbnail_url": "https://pbs.twimg.com/ext_tw_video_thumb/x.jpg",
                "url": "https://video.twimg.com/ext_tw_video/x.mp4",
                "variants": [
                    {"url": "https://video.twimg.com/ext_tw_video/low.mp4", "content_type": "video/mp4", "bitrate": 256000},
                    {"url": "https://video.twimg.com/ext_tw_video/high.mp4", "content_type": "video/mp4", "bitrate": 832000},
                    {"url": "https://video.twimg.com/ext_tw_video/x.m3u8", "content_type": "application/x-mpegURL", "bitrate": 0},
                ],
            }],
        },
    )
    plan = plan_post(tweet, "wustep")
    assert plan.videos == ["https://video.twimg.com/ext_tw_video/high.mp4"]
    assert plan.photos == ["https://pbs.twimg.com/ext_tw_video_thumb/x.jpg"]
    assert plan.thread_note is True
    blocks = build_blocks(plan, video_uploads={plan.videos[0]: "upload-1"})
    kinds = [block["type"] for block in blocks]
    assert kinds == ["callout", "quote", "embed", "image", "video", "toggle"]
    assert blocks[4]["video"]["type"] == "file_upload"
    external = build_blocks(plan, include_raw=False)
    assert [block["type"] for block in external][-1] == "video"
    assert external[-1]["video"]["type"] == "external"
    long = "a" * 5000
    chunks = rt_chunks(long)
    assert sum(utf16_len(chunk["text"]["content"]) for chunk in chunks) == 5000
    assert all(utf16_len(chunk["text"]["content"]) <= 2000 for chunk in chunks)
    huge = {"text": "x" * 20000, "id": "1"}
    dumped = raw_json_text(huge, limit=1000)
    assert json.loads(dumped)["truncated"] is True


def _posts_dump():
    return {
        "data": [
            {
                "id": "2104610435571884086",
                "text": "Last week, we launched column permissions!\n\ntrailer 🧵 https://t.co/abc",
                "author_id": "3146745024",
                "conversation_id": "2104610435571884086",
                "created_at": "2026-09-28T16:33:24.000Z",
                "lang": "en",
                "possibly_sensitive": False,
                "reply_settings": "everyone",
                "source": "Twitter for iPhone",
                "public_metrics": {
                    "bookmark_count": 79,
                    "impression_count": 54676,
                    "like_count": 154,
                    "quote_count": 9,
                    "reply_count": 19,
                    "repost_count": 13,
                },
                "attachments": {"media_keys": ["13_vid"]},
            },
            {
                "id": "2104366065887138086",
                "text": "Flashback",
                "note_tweet": {"text": "Flashback full note that is longer than the short text field by plenty"},
                "author_id": "3146745024",
                "conversation_id": "2104366065887138086",
                "created_at": "2026-09-28T00:22:21.000Z",
                "lang": "en",
                "reply_settings": "following",
                "public_metrics": {
                    "bookmark_count": 0,
                    "like_count": 18,
                    "quote_count": 1,
                    "reply_count": 6,
                    "repost_count": 0,
                },
                "referenced_tweets": [{"type": "quoted", "id": "2104289146206629951"}],
                "attachments": {"media_keys": ["3_photo"]},
            },
            {
                "id": "999999999999999991",
                "text": "self reply",
                "author_id": "3146745024",
                "conversation_id": "2104610435571884086",
                "created_at": "2026-09-28T17:00:00.000Z",
                "referenced_tweets": [{"type": "replied_to", "id": "2104610435571884086"}],
                "public_metrics": {"like_count": 1, "repost_count": 0, "reply_count": 0, "quote_count": 0, "bookmark_count": 0, "impression_count": 4},
            },
            {
                "id": "999999999999999992",
                "text": "a repost",
                "author_id": "3146745024",
                "conversation_id": "999999999999999992",
                "created_at": "2026-09-28T18:00:00.000Z",
                "referenced_tweets": [{"type": "retweeted", "id": "777777777777777777"}],
                "public_metrics": {"like_count": 1, "repost_count": 0, "reply_count": 0, "quote_count": 0, "bookmark_count": 0, "impression_count": 1},
            },
        ],
        "includes": {
            "users": [
                {"id": "3146745024", "name": "Stephen Wu", "username": "wustep"},
                {"id": "39262778", "name": "Thomas Frank", "username": "TomFrankly"},
            ],
            "media": [
                {
                    "media_key": "13_vid",
                    "type": "video",
                    "preview_image_url": "https://pbs.twimg.com/media/thumb.jpg",
                    "variants": [
                        {"bit_rate": 256000, "content_type": "video/mp4", "url": "https://video.twimg.com/low.mp4"},
                        {"bit_rate": 2176000, "content_type": "video/mp4", "url": "https://video.twimg.com/high.mp4"},
                        {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/x.m3u8"},
                    ],
                },
                {"media_key": "3_photo", "type": "photo", "url": "https://pbs.twimg.com/media/pic.jpg"},
            ],
            "posts": [{
                "id": "2104289146206629951",
                "author_id": "39262778",
                "text": "quoted body",
                "conversation_id": "2104289146206629951",
            }],
        },
    }


def test_posts_json_properties_and_body():
    tweets = posts_by_id(_posts_dump(), handle="wustep", author_id="3146745024")
    assert set(tweets) == {
        "2104610435571884086",
        "2104366065887138086",
        "999999999999999991",
        "999999999999999992",
    }
    video = plan_post(tweets["2104610435571884086"], "wustep")
    assert video.in_scope and video.type_name == "Tweet"
    assert video.metrics == {
        "likes": 154,
        "reposts": 13,
        "replies": 19,
        "quotes": 9,
        "bookmarks": 79,
        "impressions": 54676,
    }
    assert video.reply_settings == "everyone"
    assert video.app_source == "Twitter for iPhone"
    assert video.videos == ["https://video.twimg.com/high.mp4"]
    assert video.photos == ["https://pbs.twimg.com/media/thumb.jpg"]
    assert video.thread_note is True
    props = page_properties(video, create=False)
    assert props["Impressions"]["number"] == 54676
    assert props["Reposts"]["number"] == 13
    assert props["Likes"]["number"] == 154
    assert props["Reply settings"]["select"]["name"] == "everyone"
    assert props["Source"]["rich_text"][0]["text"]["content"] == "Twitter for iPhone"
    assert props["Has media"]["checkbox"] is True
    assert "Name" not in props
    blocks = build_blocks(video, video_uploads={video.videos[0]: "upload-1"})
    assert [block["type"] for block in blocks] == ["callout", "quote", "embed", "image", "video", "toggle"]
    assert blocks[4]["video"]["type"] == "file_upload"
    page = compose_body(blocks, [])
    assert page[-2]["type"] == "divider"
    assert page[-1]["type"] == "heading_2"
    assert is_notes_marker(page[-1])

    quote = plan_post(tweets["2104366065887138086"], "wustep")
    assert quote.in_scope and quote.type_name == "Quote"
    assert quote.text.startswith("Flashback full note")
    assert quote.metrics["impressions"] is None
    assert quote.quoted_url == "https://x.com/TomFrankly/status/2104289146206629951"
    assert quote.photos == ["https://pbs.twimg.com/media/pic.jpg"]
    qprops = page_properties(quote, create=True)
    assert "Impressions" not in qprops
    assert qprops["Quoted post"]["rich_text"][0]["text"]["content"].endswith("2104289146206629951")
    assert qprops["Name"]["title"][0]["text"]["content"].startswith("Flashback full note")
    assert qprops["Reply settings"]["select"]["name"] == "following"

    assert plan_post(tweets["999999999999999991"], "wustep").reason == "reply"
    assert plan_post(tweets["999999999999999992"], "wustep").reason == "retweet"

    bare = posts_by_id([{
        "id": "2104610435571884086",
        "text": "inline photo",
        "author_id": "3146745024",
        "conversation_id": "2104610435571884086",
        "created_at": "2026-09-28T16:33:24.000Z",
        "public_metrics": {"like_count": 1, "repost_count": 0, "reply_count": 0, "quote_count": 0, "bookmark_count": 0},
        "media": [{"type": "photo", "url": "https://pbs.twimg.com/media/only.jpg"}],
    }], handle="wustep", author_id="3146745024")
    bare_plan = plan_post(bare["2104610435571884086"], "wustep")
    assert bare_plan.in_scope and bare_plan.photos == ["https://pbs.twimg.com/media/only.jpg"]
    assert "Impressions" not in page_properties(bare_plan, create=False)


def test_notes_are_preserved():
    existing = [
        {"type": "quote", "quote": {"rich_text": [{"plain_text": "old tweet text", "text": {"content": "old tweet text"}}]}},
        {"type": "divider", "divider": {}},
        {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "Notes", "text": {"content": "Notes"}}]}},
        {
            "type": "paragraph",
            "paragraph": {"rich_text": [{
                "plain_text": "keep this",
                "text": {"content": "keep this", "link": {"url": "https://wustep.me"}},
                "annotations": {"bold": True, "italic": False, "strikethrough": False, "underline": False, "code": False, "color": "default"},
            }]},
        },
        {
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"plain_text": "a bullet", "text": {"content": "a bullet"}}]},
        },
    ]
    assert is_notes_marker(existing[2])
    notes = preserved_note_blocks(existing)
    assert [block["type"] for block in notes] == ["heading_2", "paragraph", "bulleted_list_item"]
    assert notes[1]["paragraph"]["rich_text"][0]["text"]["content"] == "keep this"
    assert notes[1]["paragraph"]["rich_text"][0]["text"]["link"]["url"] == "https://wustep.me"
    assert notes[1]["paragraph"]["rich_text"][0]["annotations"]["bold"] is True
    sync = [{"object": "block", "type": "quote", "quote": {"rich_text": [{"type": "text", "text": {"content": "new"}}]}}]
    merged = compose_body(sync, notes)
    assert [block["type"] for block in merged] == ["quote", "divider", "heading_2", "paragraph", "bulleted_list_item"]
    assert "old tweet text" not in json.dumps(merged)
    fresh = compose_body(sync, [])
    assert fresh[-1]["type"] == "heading_2"
    sentinel = {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "<!-- x-posts-sync:notes -->", "text": {"content": "<!-- x-posts-sync:notes -->"}}]}}
    assert is_notes_marker(sentinel)
    kept = preserved_note_blocks([
        {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "above", "text": {"content": "above"}}]}},
        sentinel,
        {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "below", "text": {"content": "below"}}]}},
    ])
    assert [block_plain(block) for block in kept] == ["<!-- x-posts-sync:notes -->", "below"]


def block_plain(block):
    from notes import block_plain as plain
    return plain(block)


def test_worklist_and_incoming():
    t0 = datetime(2026, 9, 28, tzinfo=timezone.utc)
    rows = [
        Row("page-old", "100", t0 - timedelta(days=10), "", "old", ""),
        Row("page-new", "200", t0, "", "new", ""),
        Row("page-dup", "200", t0, "", "dup", ""),
    ]
    items = assemble_work(rows, [], refresh_existing=True, new_only=False, since=None, limit=0, only="")
    assert [item.post_id for item in items] == ["200", "100"]
    assert items[0].page_ids == ("page-new", "page-dup")
    assert items[0].create is False

    recent = assemble_work(
        rows, [], refresh_existing=True, new_only=False, since=t0 - timedelta(days=1), limit=0, only="",
    )
    assert [item.post_id for item in recent] == ["200"]

    mixed = assemble_work(
        rows, ["300", "200"], refresh_existing=False, new_only=False, since=None, limit=0, only="",
    )
    assert [(item.post_id, item.create) for item in mixed] == [("300", True), ("200", False)]

    only_new = assemble_work(
        rows, ["300", "200"], refresh_existing=False, new_only=True, since=None, limit=1, only="",
    )
    assert [(item.post_id, item.create) for item in only_new] == [("300", True)]

    assert row_in_scope("Tweet", "200", "200", "@wustep", "wustep")
    assert row_in_scope("Quote", "200", "", "", "wustep")
    assert not row_in_scope("Reply", "200", "200", "@wustep", "wustep")
    assert not row_in_scope("Tweet", "200", "199", "@wustep", "wustep")
    assert not row_in_scope("Tweet", "200", "200", "@other", "wustep")

    assert refresh_existing_rows(new_only=False, refresh_all=False, has_source=False) is True
    assert refresh_existing_rows(new_only=False, refresh_all=False, has_source=True) is False
    assert refresh_existing_rows(new_only=True, refresh_all=True, has_source=True) is False
    assert refresh_existing_rows(new_only=False, refresh_all=True, has_source=True) is True

    loaded = load_incoming_text(json.dumps([
        "https://x.com/wustep/status/2104610435571884086",
        {"id": "2104366065887138086"},
    ]))
    assert loaded[0]["raw"].endswith("2104610435571884086")
    assert loaded[1]["raw"] == "2104366065887138086"
    lines = load_incoming_text("# c\n\n2104610435571884086\n")
    assert lines == [{"raw": "2104610435571884086"}]


def test_parse_page_and_time():
    page = {
        "id": "page-1",
        "url": "https://www.notion.so/page-1",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "Hello"}]},
            "Post ID": {"type": "rich_text", "rich_text": [{"plain_text": "200"}]},
            "URL": {"type": "url", "url": "https://x.com/wustep/status/200"},
            "Type": {"type": "select", "select": {"name": "Tweet"}},
            "Posted": {"type": "date", "date": {"start": "2026-09-28T16:33:00.000Z"}},
            "Author": {"type": "rich_text", "rich_text": [{"plain_text": "@wustep"}]},
            "Conversation ID": {"type": "rich_text", "rich_text": [{"plain_text": "200"}]},
        },
    }
    parsed = parse_page(page)
    assert parsed["title"] == "Hello"
    assert parsed["post_id"] == "200"
    assert parsed["type_name"] == "Tweet"
    assert parse_time(parsed["posted"]) == datetime(2026, 9, 28, 16, 33, tzinfo=timezone.utc)
    assert parse_time("Mon Sep 28 16:33:24 +0000 2026") == datetime(2026, 9, 28, 16, 33, 24, tzinfo=timezone.utc)
    assert normalize_notion_id("0123456789abcdef0123456789abcdef") == "01234567-89ab-cdef-0123-456789abcdef"
    assert normalize_notion_id("https://www.notion.so/fedcba9876543210fedcba9876543210") == "fedcba98-7654-3210-fedc-ba9876543210"
    assert normalize_notion_id("") == ""


def _rejects(argv: list[str]) -> None:
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            parse_args(argv)
        except SystemExit as exc:
            assert exc.code not in (0, None)
            return
    raise AssertionError(f"expected rejection: {argv}")


def test_cli_validation():
    args = parse_args([])
    assert args.limit == 0
    assert args.dry_run is False
    assert args.posts_json == ""
    assert args.refresh_all is False
    _rejects(["--only", "not-a-status"])
    _rejects(["--since", "yesterday"])
    ok = parse_args(["--only", "https://x.com/wustep/status/2104610435571884086", "--since", "2026-09-01"])
    assert "2104610435571884086" in ok.only
    assert ok.since == "2026-09-01"


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"passed {len(tests)}")


if __name__ == "__main__":
    main()
