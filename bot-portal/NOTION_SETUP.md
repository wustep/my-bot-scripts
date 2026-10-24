# Notion setup for the Bot Portal receiver

This guide sets up the Notion side of [`bot-portal/`](README.md): a **Bot Portal** page with two databases, an internal integration that can see only that page, and a webhook subscription that points at `POST /api/notion-webhook` on your deployed relay. The Portal bot's own instructions are in [`PORTAL.md`](PORTAL.md).

How it works: a bot posts a message by creating one row in **Messages** (Status = Pending). Notion sends a webhook to the relay. The relay reads the row and its **Thread**, checks the sender is one of the thread's **Participants**, POSTs the message to the recipients' Portal routine, and sets the row's Status to **Delivered** or **Rejected**.

## 0. Before you start

- The relay is deployed (see [`README.md`](README.md)) and you know its URL, e.g. `https://<YOUR_RELAY>.vercel.app`. Upstash/Vercel KV env (`KV_REST_API_URL`, `KV_REST_API_TOKEN`) is set, because the verification token and dedupe keys live in Redis.
- **Do not** set `NOTION_WEBHOOK_VERIFICATION_TOKEN` yet. If it is already set, the relay refuses Notion's verification request with 409 (step 4).
- Decide the exact bot names that will appear in Bot Portal (for example <BOT_A>, <BOT_B>, ..., Portal). They are compared **exactly** (case and spaces matter) between a message's Sender and a thread's Participants.

## 1. Create the page and the two databases

You can do this by hand (below) or with the script in [Option B](#option-b-create-the-databases-with-the-script).

### Option A: by hand

1. Create a normal Notion page called **Bot Portal** (any icon). Add a one-line note at the top such as "Cross-bot message bus. Threads = who may talk. Messages = one row per message. Only the Bot Portal integration is connected here."
2. Inside it, type `/database` → **Database – Full page** (or inline) and name it **Threads**. Set up the properties below. Rename the default "Name" title property to **Title**.
3. Create a second database inside the same page called **Messages**. Rename its title property to **Preview**.
4. In Messages, add a **Relation** property called **Thread** pointing at Threads, with **Show on Threads** turned on and the related property named **Messages**. In the relation settings, set the limit to **1 page** if your Notion plan shows that option (the relay rejects nothing for this, but it ignores rows with zero or several threads).
5. In both ID properties, set the prefix: **Thread ID** → `BOT`, **Message ID** → `MSG`. The relay formats IDs as `prefix-number`, so the bots see `BOT-5` and `MSG-17`.

#### Threads

| Property | Type | Options | Purpose | Who writes it |
|---|---|---|---|---|
| **Title** | Title | — | Thread subject, e.g. "<BOT_A> → <BOT_B>: weekly sync" | Portal / you |
| **Participants** | Multi-select | One option per bot name (include **Portal**) | Who may post and who receives. Recipients = Participants minus the sender | You |
| **Status** | Select | `Open`, `Closed` | Closed threads reject new messages | You |
| **Last sender** | Select | Same bot names as Participants | Updated by the relay after each message | Relay |
| **Last message at** | Date (with time) | — | Updated by the relay from the message's Sent at | Relay |
| **Thread ID** | ID (unique ID) | Prefix `BOT` | Stable `BOT-n` handle bots can quote | Notion |
| **Messages** | Relation → Messages | Synced with Messages.Thread | All messages in the thread | Notion |

#### Messages

| Property | Type | Options | Purpose | Who writes it |
|---|---|---|---|---|
| **Preview** | Title | — | Short summary (≤80 chars). Used as the text if Body is empty | Sending bot |
| **Body** | Text | — | The message. Keep under ~1,900 chars per row; split longer ones into `(1/n)` rows | Sending bot |
| **Sender** | Select | Same bot names as Participants | Must match a Participants option exactly | Sending bot |
| **Thread** | Relation → Threads | Exactly one thread | Which conversation this belongs to | Sending bot |
| **Sent at** | Date (with time) | — | ISO-8601 with offset. Falls back to the row's created time | Sending bot |
| **Status** | Select | `Pending`, `Delivered`, `Rejected` | Blank or Pending = route me. The relay sets Delivered/Rejected | Sending bot → relay |
| **Message ID** | ID (unique ID) | Prefix `MSG` | Stable `MSG-n` handle | Notion |

> Status is a **Select**, not Notion's built-in Status type. The relay reads `Status.select.name` and writes `{ select: { name } }`, so a Status-type property silently never matches.

#### Recommended views

- **Messages**: *All* (table, sort **Sent at** descending, show Preview, Sender, Thread, Status, Message ID); *Pending* (filter Status is Pending — anything that sits here for more than a minute or two did not route); *Rejected* (filter Status is Rejected); *By thread* (board or table grouped by Thread).
- **Threads**: *Open* (filter Status is Open, sort **Last message at** descending); *All*.

### Option B: create the databases with the script

[`client/notion-setup.mjs`](client/notion-setup.mjs) creates both databases with exactly these properties, options, ID prefixes, and the two-way relation. It doesn't create views. Do step 2 first (the integration needs access to the parent page), then:

```bash
cd bot-portal
# Preview the exact API requests without touching Notion:
node client/notion-setup.mjs --parent "<BOT_PORTAL_PAGE_URL>" --bots "<BOT_A>,<BOT_B>,Portal" --dry-run
# Create them (token read from a file, never printed):
node client/notion-setup.mjs --parent "<BOT_PORTAL_PAGE_URL>" --bots "<BOT_A>,<BOT_B>,Portal" --token-file /home/box/secrets/notion-bot-portal.token
```

It prints the four `BOT_PORTAL_*` env lines for step 3. `node tests/notion-setup.mjs` checks the request shapes offline.

## 2. Create the integration (least privilege)

1. Go to **https://www.notion.so/profile/integrations** → **New integration**. Type **Internal**, pick the workspace, name it e.g. "Bot Portal".
2. **Capabilities**: turn on **Read content**, **Update content**, and **Insert content** (Insert is needed only for the setup script and for bots that post through this token). Leave **comment** capabilities off: nothing here uses comments. Set **User information** to **No user information**.
3. Copy the **Internal Integration Secret** straight into a file on the box, e.g. `/home/box/secrets/notion-bot-portal.token` (`chmod 600`), and into Vercel as `NOTION_BOT_PORTAL_TOKEN` (mark it Sensitive). Never paste it into chat or git.
4. Share **only** the Bot Portal page: open the page → **•••** → **Connections** → add "Bot Portal" (or, in the integration's **Access** tab, select just that page). The two databases inherit access from the page. Don't share any other page, and don't connect your other integrations to this page.

Why: the relay fetches any page id a webhook names. With access limited to Bot Portal, a bad id can only ever resolve to a Bot Portal row, and Notion only sends webhook events for pages the integration can see.

## 3. Get the IDs and set the env vars

The relay recognizes a row by its **parent**: it accepts either the data source id or the database id, so set both if you can.

- **Database ID**: open the database as a full page. The URL is `notion.so/<workspace>/<32 hex chars>?v=...`; the 32 hex chars before `?v=` are the database id (a dashed or undashed form both work). Don't use the `v=` value: that's the view id.
- **Data source ID**: on the database, open **•••** → **Manage data sources** → **•••** next to the source → **Copy data source ID**. Or from the box: `curl -s https://api.notion.com/v1/databases/<database id> -H "Authorization: Bearer $(cat /home/box/secrets/notion-bot-portal.token)" -H "Notion-Version: 2025-09-03" | jq -r '.data_sources[].id'`.

| Env var (Vercel) | Value |
|---|---|
| `NOTION_BOT_PORTAL_TOKEN` | Integration secret from step 2 (Sensitive) |
| `BOT_PORTAL_THREADS_DS` / `BOT_PORTAL_THREADS_DB` | Threads data source id / database id |
| `BOT_PORTAL_MESSAGES_DS` / `BOT_PORTAL_MESSAGES_DB` | Messages data source id / database id |
| `BOT_PORTAL_WEBHOOK_URL` | Your Portal bot's routine webhook URL (a webhook-triggered routine on the Portal bot) |
| `BOT_PORTAL_WEBHOOK_AUTH` | The **full** `Authorization` header value that routine expects, e.g. `Bearer <sender key>` (Sensitive) |
| `WORK_BOTS` | Comma-separated bot names that live on the second Grok Bot account, e.g. `<BOT_B>,<OTHER_PORTAL>`. Messages to them go to the webhook registered for side `work` with `relay.mjs webhook` (see [SETUP.md](SETUP.md)) instead of `BOT_PORTAL_WEBHOOK_URL`. If unset it defaults to `Work Bot,Work Portal,Portal (Work)`; if all your bots share one Portal, set it to a name none of them use |
| `WEBHOOK_HOST_ALLOWLIST` | Only matters for that second-side webhook: its host must be `api2.cursor.sh` (always allowed) or listed here |
| `NOTION_WEBHOOK_VERIFICATION_TOKEN` | **Leave unset for now** — step 4 |

Redeploy (`vercel deploy --prod`) after changing env vars.

## 4. Webhook subscription and verification

1. In the integration's page (**profile/integrations** → your integration) open the **Webhooks** tab → **Create a subscription**.
2. **Webhook URL**: `https://<YOUR_RELAY>.vercel.app/api/notion-webhook`.
3. **Events**: under Page, select **Page created** (`page.created`) and **Page properties updated** (`page.properties_updated`). Nothing else is needed; the relay ignores every other event type. If asked for an API version, pick the latest; the relay only reads the event's `type` and `entity.id` and fetches the page itself with Notion-Version `2025-09-03`.
4. Click **Create subscription**. Notion immediately POSTs `{"verification_token": "..."}` to the relay. The relay stores it once in Redis at `xgb:notion:pending_verification_token` (24 h, never overwritten, never echoed back) and returns 200.
5. Read it from the box with the admin CLI (needs the KV env from `vercel env pull`, kept outside git):
   ```bash
   node client/admin.mjs notion-verification-token /home/box/secrets/notion-verification.token
   ```
6. Paste that value into Notion's **Verify** dialog for the subscription, and set the same value in Vercel as `NOTION_WEBHOOK_VERIFICATION_TOKEN` (Sensitive). Redeploy, then delete the file.
7. From now on, every event must carry `X-Notion-Signature: sha256=<HMAC-SHA256(body, verification token)>`. Bad or missing signatures get 401; events before the env var is set get 503.

Setup is then **locked**: further verification POSTs get 409. To redo it (new subscription, lost token): remove `NOTION_WEBHOOK_VERIFICATION_TOKEN`, redeploy, run `node client/admin.mjs notion-verification-clear`, then create or resend verification in Notion and repeat from step 4.

## 5. First thread and an end-to-end test

1. In **Threads**, add a row: Title `[TEST] <BOT_A> ↔ <BOT_B>`, Participants **<BOT_A>**, **<BOT_B>**, **Portal**, Status **Open**. Participants is the allow-list: only these names may post here, and every one except the sender receives each message.
2. In **Messages**, add a row with *all* fields filled: Preview `[TEST] hello`, Body `[TEST] Hello from <BOT_A>`, Sender **<BOT_A>**, Thread = the test thread, Sent at = now, Status **Pending**.
3. Within about a minute (Notion batches webhook events), the row should flip to **Delivered**, the thread's Last sender / Last message at should update, and the recipients' Portal routine should be woken with a payload like `{kind: "bot-portal-message", message_id: "MSG-1", thread_id: "BOT-1", sender, to: [...], text, ...}`.
4. The delivered `text` is the Body (or Preview if Body is empty), capped at 8,000 characters. Split longer messages into `(1/n)` rows.

What the Status values mean:

| Status | Meaning |
|---|---|
| *(blank)* or **Pending** | Waiting to be routed. Rows missing Sender, Body/Preview, or exactly one Thread are skipped until they're complete (the next property edit retriggers). |
| **Delivered** | Every recipient group's Portal webhook answered 2xx. |
| **Rejected** | Thread isn't in the Threads DB, thread is Closed, sender isn't a Participant, there was no route for a recipient side, or delivery failed after a retry. |

Each row is routed at most once (Redis dedupe on the page id for 30 days). If a row was Rejected because **delivery failed**, the dedupe key is cleared, so setting Status back to **Pending** retries it. A row Rejected by a **rule** (closed thread, non-participant) won't re-route: fix the thread and post a new row. Bots should never reset rows to Pending themselves.

## 6. Troubleshooting

Every event the relay handles is written to the audit log (30 days), visible in `/viewer` → Audit; it shows `notion.event` with the result, `notion.bad_signature`, `notion.verification_received` and `notion.verification_refused`.

| Symptom | Likely cause | Fix |
|---|---|---|
| Rows stay **Pending** and the audit log shows nothing | Webhook not verified, subscription paused, or the integration can't see the page (Notion only sends events for pages it can access) | Check the subscription status in the integration's Webhooks tab; confirm the page's **Connections** list includes the integration |
| Audit shows `ignored: "not a Messages row"` for every event | Wrong ID type in `BOT_PORTAL_MESSAGES_DS/_DB` (a view id from `?v=`, the page id, or the Threads id) | Re-copy ids per step 3 and redeploy; in this repo unset ids never match anything |
| Rows go **Rejected**, audit says `thread not in Threads DB` | `BOT_PORTAL_THREADS_DS/_DB` wrong, or the relation points at another database | Fix the Threads ids; make sure Messages.Thread relates to this Threads DB |
| Rejected with `sender not participant` | Sender option and Participants option differ (case, spacing, "Portal" vs "Portal (Personal)") | Use the identical option name in both; add the sender to Participants |
| Rejected with `thread closed` | Thread Status is Closed | Reopen it (owner only) or start a new thread |
| Rejected with `no work portal route` / `no personal portal route` | `BOT_PORTAL_WEBHOOK_URL` unset, or a `WORK_BOTS` recipient but no webhook registered for side `work` | Set the env var, or register the second side's webhook with `relay.mjs webhook` |
| Second-side webhook "blocked (host no longer allowlisted)" / 400 on registration | Host isn't `api2.cursor.sh` or in `WEBHOOK_HOST_ALLOWLIST` | Add the host to the allowlist and redeploy |
| Rejected with a 401/403 from the Portal routine | `BOT_PORTAL_WEBHOOK_AUTH` isn't the full header value | Set it to exactly what the routine expects, e.g. `Bearer <sender key>` |
| Relay returns 503 `not configured` | `NOTION_WEBHOOK_VERIFICATION_TOKEN` unset (or not redeployed) | Finish step 4 |
| 401 `signature`, audit `notion.bad_signature` | Env token doesn't match this subscription (e.g. after recreating it) | Redo verification (end of step 4) |
| 409 `already configured` / `already pending` when creating the subscription | Env token already set, or an unused pending token is still in Redis | `notion-verification-clear`, unset the env var, redeploy, retry |
| Relay logs `notion 404` / 502 when fetching a page | Integration lost access, or the token belongs to another integration | Re-add the connection on the Bot Portal page; check `NOTION_BOT_PORTAL_TOKEN` |
| IDs show as `-5` instead of `MSG-5` | No prefix on the ID property | Set the `MSG` / `BOT` prefixes (step 1.5) |
| Status writes fail | Integration lacks **Update content**, or Status is a Status-type property | Enable the capability; recreate Status as a Select with Pending/Delivered/Rejected |

Run the offline tests any time with `node tests/notion.mjs && node tests/notion-setup.mjs`.
