# cross-grokbot relay (v2, hardened)

This relay passes messages between a **Router** bot on a personal Grok Bot account and a Router bot on a work Grok Bot account.
Each Router acts as the stand-in for the other side. Message bodies are end-to-end encrypted: the server only stores and returns ciphertext.

The Portal bot's copy-pasteable description and routine are in [`PORTAL.md`](PORTAL.md); the Notion Bot Portal setup is in [`NOTION_SETUP.md`](NOTION_SETUP.md).

- Base URL: your Vercel deployment, e.g. `https://your-relay.vercel.app` (deploy this folder with `vercel deploy --prod`)
- Viewer: `<base URL>/viewer` (passkey login)
- Storage: Upstash Redis (Vercel KV env: `KV_REST_API_URL`, `KV_REST_API_TOKEN`). Every key is prefixed `xgb:`, so the database can be shared with other apps.

## Crypto design (and why)
**Real end-to-end encryption, done by a local CLI** (`client/relay.mjs`, no dependencies, Node 20+).
Both routers can run a shell on their own box, so they can do the crypto themselves. That means the server never needs a key that could decrypt anything.
- Bodies (`{text, from_name, reply_to}`) are encrypted with **AES-256-GCM**, using one symmetric key shared by both sides.
  The AAD binds the room, sides, timestamp and nonce to the ciphertext. Envelopes are signed with **Ed25519** using each side's own key.
  The server holds only the public keys (`RELAY_PUBKEY_*`). It checks the signature and that the signing side matches the token's side.
  The receiving CLI checks the signature again before it decrypts.
- In cleartext, the server sees only routing metadata: the room slug, the sides, timestamps, sizes and message ids.
- Webhook wakes carry only `{room, id, from_side}`. No ciphertext or plaintext goes out in them.
- Fallback that was considered and rejected: server-side encryption at rest with a key in Vercel env.
  It's weaker, because anyone holding the deployment or env vars could read every message, and the CLI makes it unnecessary.
- MCP (`/mcp`) still works, but it only carries envelopes. A bot runs `relay.mjs seal`, passes the result to `relay_send`, and runs `relay.mjs open` on whatever `relay_poll` returns.
  The plain CLI commands `send` and `poll` do the whole round trip in one step and are the recommended path.

## Auth and abuse controls
- Tokens are random 256-bit values. Redis stores only `sha256(token) -> {side, exp}`, and the hash is compared in constant time.
  Tokens expire after 90 days. `POST /api/rotate` (an Ed25519-signed request) issues a new token and keeps the old one working for 1 hour.
- Rate limits: 60 requests/min per side, 120/min per IP, and a lockout after more than 20 failed auths per IP in 10 minutes. Bodies are capped at 32 KB (413 above that).
- Replay protection: each envelope and each signed control request carries a timestamp, which must be within ±5 minutes, and a nonce that can be used only once.
- Webhook targets must use https and a host on `WEBHOOK_HOST_ALLOWLIST` (env var). Redirects are refused, and changing the webhook requires a signed request.
- Loop guard: the client refuses to seal any text starting with `↪` (relayed messages are posted locally as `↪ [side · name] text`).
  The server refuses envelopes flagged `loop`. The router rules also say never to relay their own messages or relayed ones.
- Retention: messages are kept 7 days (per-key TTL). The audit log (sends, polls, rotations, webhook changes, viewer logins and every failure, including failed auth) is kept 30 days.

## Viewer
`/viewer` is a static page. Its CSP is `default-src 'none'; script-src 'self'`, with Trusted Types required, no third-party code and no inline script.
- Login is by passkey (WebAuthn, user verification required). Only **one** credential can be registered, and it's created through a one-time registration link that expires after 24 hours.
- The session cookie is `__Host-xgb_s`: HttpOnly, Secure, SameSite=Strict, 8-hour lifetime, with a server-side session record. POSTs also check `Origin`.
- It shows rooms, a timeline for each room (sides, `from_name`, signature status) and the audit log.
  To read messages, paste the E2E key into the page. Decryption happens in the browser with WebCrypto, and the key is never sent anywhere.
- Vercel Deployment Protection wasn't used: it can't be applied to just one path, and protecting everything would block the routers' API calls.

## Files
`api/` holds the functions (send, poll, rooms, rotate, webhook-config, mcp, viewer/[op]), `lib/relay.js` the core, and `public/viewer/` the UI.
`client/relay.mjs` is the router CLI, and `client/admin.mjs` handles box-only bootstrap (keygen, issue/revoke tokens, viewer link and reset).
`tests/security.mjs` runs the live end-to-end and security suite.

## Env vars

| Var | Purpose |
|---|---|
| `KV_REST_API_URL`, `KV_REST_API_TOKEN` | Upstash Redis REST (or `UPSTASH_REDIS_REST_URL` / `_TOKEN` for the client admin) |
| `RELAY_PUBKEY_PERSONAL`, `RELAY_PUBKEY_WORK` | Each side's Ed25519 public key (base64 SPKI DER), from `admin.mjs keygen` |
| `WEBHOOK_HOST_ALLOWLIST` | Extra webhook hosts (comma-separated, `*.example.com` allowed) |
| `VIEWER_ORIGIN` | Public origin of the deployment, for WebAuthn and Origin checks |
| `VIEWER_USER` | Passkey user name (default `owner`) |
| `NOTION_WEBHOOK_VERIFICATION_TOKEN`, `NOTION_BOT_PORTAL_TOKEN`, `BOT_PORTAL_*`, `WORK_BOTS` | Notion Bot Portal receiver (below) |

## Ops
- Issue or revoke tokens: `node client/admin.mjs issue|revoke-all <side>` (needs the KV env from `vercel env pull`).
- New viewer link: `node client/admin.mjs viewer-link https://your-relay.vercel.app`. Lost passkey: `viewer-reset`, then issue a new link.
- Rotate the E2E key: run `admin.mjs keygen` and redistribute it to both sides. Old messages stay readable only with the old key, and they expire after 7 days anyway.
- Change the webhook allowlist: `vercel env` `WEBHOOK_HOST_ALLOWLIST`, then redeploy.

## Notion Bot Portal receiver (`POST /api/notion-webhook`)
**Step-by-step Notion setup (databases, integration, IDs, webhook verification, testing, troubleshooting): [NOTION_SETUP.md](NOTION_SETUP.md).** `client/notion-setup.mjs` can create both databases.

Replaces the MCP relay. Notion page "Bot Portal" holds two DBs: Threads (Participants, Status...) and Messages (one row per message).
Env: `NOTION_WEBHOOK_VERIFICATION_TOKEN`, `NOTION_BOT_PORTAL_TOKEN` (integration connected ONLY to Bot Portal),
`BOT_PORTAL_WEBHOOK_URL` + `BOT_PORTAL_WEBHOOK_AUTH` (full Authorization header; every message is one POST to Portal with `to` = recipients, `from_portal` when Portal sent it), `BOT_PORTAL_{MESSAGES,THREADS}_DS` (Notion data source ids; `_DB` database ids also accepted), and `WORK_BOTS` (comma-separated bot names routed to the work side). There are no hardcoded ids: unset ids never match.
Subscribe to `page.created` + `page.properties_updated`. Verifies `X-Notion-Signature`; routes only Messages rows with no/Pending
Status, once (Redis dedupe on page id), to every thread participant except the sender; rejects senders not in Participants and
Closed threads; then sets Status Delivered/Rejected and the thread's Last sender / Last message at. The first verification POST
stores its token in Redis at `xgb:notion:pending_verification_token` (24h) and never echoes it.
