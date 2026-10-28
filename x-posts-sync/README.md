# x-posts-sync

Deterministic sync of a Notion **X Posts** database (one account's originals and quote tweets). A weekly Grok Bot routine runs it. The script does not call an LLM.

Text, media, and engagement come only from an X API-shaped JSON dump the routine writes after a free X read (`get_posts_by_ids` or a user timeline). This sync does not call FxTwitter, VxTwitter, or the X API itself.

## Sunday command

1. The routine reads the posts with the X reader. Ask for `author_id`, `attachments.media_keys`, `referenced_tweets.id`, and `referenced_tweets.id.author_id`, plus media fields `url`, `preview_image_url`, and `variants`, and user fields `id`, `name`, `username`.
2. Write that tool response to a file (the whole `{data, includes}` object, or a list of post objects).
3. Refresh every in-scope row that the file covers, and insert new Tweet/Quote roots from the file:

```bash
python3 x-posts-sync/sync.py --refresh-all --posts-json /path/to/posts.json
```

Rows already in Notion but missing from the file are left unchanged and logged as `not_in_posts_json`. Replies, reposts, and thread posts that are not the root are not inserted.

`--from-json` and `--ids` still accept a list of status ids or URLs, but they only choose which dump rows to apply. They do not load text or metrics. Without `--posts-json` the script exits before it talks to Notion.

Smoke test, no writes:

```bash
python3 x-posts-sync/sync.py --refresh-all --posts-json /path/to/posts.json --limit 1 --dry-run
```

Continue an interrupted run (skips Post IDs already marked ok). Do not pass `--resume` on the next Sunday run.

```bash
python3 x-posts-sync/sync.py --refresh-all --posts-json /path/to/posts.json --resume
```

## Dump shape

Either of these:

- `{"data": [post, ...], "includes": {"media": [...], "users": [...], "posts": [...]}}` (what `get_posts_by_ids` returns; quoted posts may also be under `includes.tweets`)
- a JSON array of post objects, with `media` inline or a sibling `includes` if you wrap them

Each post should include `id`, `text` and/or `note_tweet`, `public_metrics`, `created_at`, `author_id`, `conversation_id`, `referenced_tweets`, and `attachments.media_keys`. `note_tweet.text` is used when it is longer than `text`. `impression_count` maps to Impressions; if that field is missing, Impressions is left unchanged. `repost_count` maps to Reposts.

## What it updates

Scope is **Type Tweet or Quote**, author `@handle` (`X_POSTS_HANDLE`, or `X_POSTS_AUTHOR_ID` when the dump has an id but no username), and thread roots only (`conversation_id` equal to the post id, and no `replied_to` / `retweeted` reference).

For each in-scope post in the dump:

- Upserts by **Post ID** (updates every matching page; does not create a duplicate).
- Sets Likes, Reposts, Replies, Quotes, Bookmarks, and Impressions from `public_metrics`.
- Sets Has media, Type, URL, Posted, Author (`@handle`), Author ID, Conversation ID, Lang, Sensitive, and Quoted post.
- Sets **Reply settings** when the dump includes one of `everyone`, `following`, `mentionedUsers`, `subscribers`, `verified`.
- Sets **Source** when the dump includes a short `source` string (the client name).
- Does not overwrite **Name** on an existing row. New rows get a short title from the first line of the post.
- Does not clear Reply to post, Reply to user, or Retweeted post.

## Page body

Top to bottom, replaced on every successful sync:

1. Optional thread callout when the text contains 🧵 (root only; self-replies are not stored).
2. Quote block with the post text.
3. “Quoting @user” line when this post quotes another.
4. Native embed of the status URL.
5. Photo blocks (external image URLs) and video blocks. Videos are hosted with Notion `file_uploads` `mode=external_url` when that works, otherwise an external video block. `--no-host-videos` skips hosting. `--host-images` hosts photos too. `--no-raw-json` omits the toggle.
6. Raw JSON toggle (the normalized post, not the whole includes blob).
7. A divider, then a **Notes** heading.

**Notes.** Everything from the first heading titled `Notes` (any level, optional trailing colon) or from a block that contains `<!-- x-posts-sync:notes -->`, through the end of the page, is copied back under the divider. Sync does not edit that section. On a new page the heading is empty so notes can be added underneath. Text above the marker is replaced each run.

A post that is missing from the dump does not have its body rewritten.

## Auth

```bash
export NOTION_API_TOKEN=...   # or rely on the fallback file below
```

If `NOTION_API_TOKEN` is unset, the script reads it from `--env-file` / `NOTION_ENV_FILE`, defaulting to `.env` next to `sync.py` (gitignored). Do not commit that file or print the token.

| Env / flag | Value |
|---|---|
| `--handle` / `X_POSTS_HANDLE` | required (no `@`) |
| `--author-id` / `X_POSTS_AUTHOR_ID` | optional; numeric X user id |
| `--database-id` / `X_POSTS_DATABASE_ID` | required |
| `--data-source-id` / `X_POSTS_DATA_SOURCE_ID` | required |

The integration must be able to read and update that database. No new Notion properties are created; Reply settings and Source already exist.

## Other flags

| Flag | Effect |
|---|---|
| `--posts-json PATH` | required. `-` reads stdin |
| `--refresh-all` | also log existing rows the dump did not include |
| `--new-only` | create rows that are not in the database |
| `--from-json` / `--ids` | limit the run to these ids; content still comes from `--posts-json` |
| `--since YYYY-MM-DD` | skip older posts |
| `--limit N` | process at most N posts (after `--resume`) |
| `--only ID` | one post |
| `--progress PATH` | default `x-posts-sync/progress.json` (gitignored) |

Exit `0` when every processed post succeeded. Exit `1` when a post failed or Notion auth failed. Exit `2` when `--posts-json` parsed no posts.

Offline checks: `python3 test_logic.py`.
