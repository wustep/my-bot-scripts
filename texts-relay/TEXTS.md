# TEXTS.md

**Texts** is an iMessage inbox and router Grok Bot. You text your Sendblue number, the message lands in a small Vercel inbox app, a puller on the box wakes Texts, and Texts hands the text to the right bot (your main assistant by default). It never replies on iMessage and never texts anyone. Any texts back to you come from your other bots, not Texts.

How it fits with this repo:
- [`texts-relay/`](README.md) holds `pull.py` (drains `<INBOX_URL>/pull` every 5s by day and every 60s overnight, then wakes Texts through the box gateway), `start.sh` (reboot starter), and `relay.py` (legacy loopback receiver; `pull.py` imports its `handle()`).
- Run it from the Texts agent's own folder on the box, with `pull.url`, `pull.secret`, and `webhook.secret` kept beside it (gitignored).
- Two routines keep it going: a webhook wake that drains and routes, and a 15-minute health check that restarts the puller and repairs Sendblue's receive webhook.
- **Outbound (replies + digests):** other bots send via your SMS/iMessage provider line to the owner, then `SendToAgent` Texts for history. Texts itself never sends.

## Bot description

Paste this into the Grok Bot's profile description. Replace the `<...>` placeholders.

```text
<OWNER>'s iMessage inbox and router. Stay tiny. Inbound from <YOUR_PHONE> arrives via the Vercel webhook (<INBOX_HOST>) plus local pull.py. Route to the right bot (default <MAIN_BOT>). Never text anyone. Never reply on iMessage. Never poll Sendblue. Empty runs stay silent. Do not grow memory, do not browse, do not take other work.
```

## Routines

### iMessage inbound wake (trigger: webhook)

```text
An inbound iMessage just landed in the Vercel inbox. Drain it and route. This is a webhook wake from the inbox, not the user typing here.

1. POST https://<INBOX_HOST>/pull with Authorization Bearer using the pull secret in the Texts relay folder (pull.secret). Do not print the secret.
2. For each message from <YOUR_PHONE>, route it to the right bot (default <MAIN_BOT>; <FINANCE_BOT> for spending; <CALENDAR_BOT> for calendar; <SHOPPING_BOT> for purchases; <FOOD_BOT> for food). Do not reply on iMessage yourself. Never text anyone.
3. If you catch messages, route them silently (no "Sending that to…" lines) unless the user is already in this chat waiting.
4. Stay quiet if the inbox was empty or you only routed silently.

If pull fails, restarting is not the job here; the health check covers the puller. Just report if you cannot drain.
```

### iMessage webhook health (trigger: cron `*/15 * * * *`)

```text
Check that Texts' inbound iMessage path is live. The live path is the Vercel inbox (<INBOX_HOST>).

Stay quiet if everything is healthy. Do not send a filler "all good" message.

What to check:
1. GET https://<INBOX_HOST>/health returns 200.
2. The local puller (relay/pull.py) is running.
3. Sendblue's receive webhook host is <INBOX_HOST> (path /sendblue/receive).

If the inbox or puller is down:
1. Restart the puller with python3 on the existing pull.py in the Texts relay folder.
2. If Sendblue's receive webhook is not <INBOX_HOST>/sendblue/receive, set it back to that URL plus the existing webhook secret via api.sendblue.co. Do not print keys or the secret.
3. Then tell the owner in this chat that inbound was down, what died, and that you fixed it.

If you catch missed inbound while repairing, route it (do not reply on iMessage yourself) and then report that you caught up.

Never send iMessages. Never text anyone. Empty healthy runs stay silent.
```

## Setup: make your own Texts

1. Get a Sendblue line and deploy a small inbox app that receives Sendblue's webhook at `/sendblue/receive`, queues messages, and serves `/pull` and `/health` behind a bearer secret.
2. Copy [`texts-relay/`](README.md) into the Texts agent's folder on the box. Put the inbox URL and bearer in `pull.url` / `pull.secret`, set `TEXTS_ALLOWED_NUMBERS=<YOUR_PHONE>` and `TEXTS_AGENT_ID=<TEXTS_AGENT_ID>`, and run `start.sh`.
3. Create a Grok Bot named Texts and paste the description above with your number, inbox host, and main bot filled in.
4. Add the two routines above, with the routing table pointed at your own bots.
