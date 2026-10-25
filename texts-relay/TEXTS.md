# TEXTS.md

**Texts** is an iMessage inbox and router Grok Bot. You text a dedicated iMessage address on your Apple ID; a Mac watcher reads Messages (`chat.db`), wakes Texts’ webhook, and Texts hands the text to the right bot. Replies go back in the **same** command thread via Mac Messages send. **No Sendblue** for inbound or outbound.

How it fits:

- **Live inbound:** Mac launchd watcher on the machine signed into Messages. Filter one command thread; allowlist senders fail-closed.
- **Wake body:** JSON `{ "source": "mac-mini", "from_number", "content", "date_sent", "media_url" }` POSTed to Texts’ inbound-wake webhook.
- **Live outbound:** the answering bot sends into that same command thread using your Mac Messages send helper (see your machine-config README). Never Sendblue.
- **Texts** stays the router; it does not send unless the owner asks in that chat.
- **Retired:** Sendblue + a Vercel inbox + [`legacy-sendblue/`](legacy-sendblue/).

## Bot description

```text
<OWNER>'s iMessage inbox and router. Stay tiny. Live inbound: Mac watcher wakes this chat on the <COMMAND_THREAD> thread (source mac-mini). Allow only <YOUR_PHONE> and <YOUR_EMAIL> (fail closed). Route to the right bot (default <MAIN_BOT>). Outbound replies go into the same <COMMAND_THREAD> via Mac Messages send — never Sendblue. I do not text anyone unless asked in this chat. Empty runs stay silent. Do not grow memory, do not browse, do not take other work.
```

## Routines

### iMessage inbound wake (webhook)

```text
An inbound iMessage just woke this routine from the Mac path (chat.db watcher → this webhook). This is not the user typing here.

1. Read the wake payload. Prefer JSON with source "mac-mini" (from_number, content, date_sent, media_url). If the prompt already contains the message text, use that. Do not pull a Vercel inbox.
2. Only handle messages from <YOUR_PHONE> or <YOUR_EMAIL>. Ignore everything else.
3. Route to the right bot (default <MAIN_BOT>; adjust your own routing table). Do not reply on iMessage yourself unless asked in this chat.
4. Tell the target bot to reply in the <COMMAND_THREAD> thread using Mac Messages send — never Sendblue.
5. Route silently unless the user is already in this chat waiting.
6. Stay quiet if there was nothing to route.
```

### iMessage inbound health (cron `*/15 * * * *`)

```text
Check that Texts’ live inbound path is healthy. Live path is the Mac chat.db watcher → this webhook, not Sendblue.

Stay quiet if everything is healthy. Do not send a filler “all good” message.

What to check (on the Mac when you have access):
1. The launchd job for the watcher is running.
2. Its log has no repeating chat.db open / Full Disk Access errors after the last bootstrap.
3. Do not repair, poll, or re-point Sendblue. Do not start legacy-sendblue pull.py.

If the watcher is down, repair or ask whoever owns the Mac install, then tell the owner in this chat what died and the fix.

Empty healthy runs stay silent.
```

## Setup: make your own Texts

1. On a Mac always signed into Messages, install a chat.db watcher + Messages send helper for one command-thread address (keep that address unchecked under that Mac’s Messages → Send & Receive if you see duplicate bubbles).
2. Grant Full Disk Access to the watcher app; store the Texts webhook URL/auth beside it (gitignored).
3. Create a Grok Bot named Texts, paste the description and routines above with your placeholders filled in.
4. Leave [`legacy-sendblue/`](legacy-sendblue/) unused.
