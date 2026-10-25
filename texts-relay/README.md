# texts-relay

Docs and retired helpers for a **Texts** Grok Bot (iMessage inbox + router).

## Live path (Mac)

Inbound **and** outbound for the command thread run on a Mac signed into Messages (not this folder):

- Poll `~/Library/Messages/chat.db` for a dedicated command thread (an iMessage address on the same Apple ID).
- Fail-closed allowlist of sender numbers/emails.
- `POST` the Texts bot’s webhook with `{ "source": "mac-mini", "from_number", "content", "date_sent", "media_url" }`.
- Outbound (replies **and** digests/pings) go into that same thread via Mac Messages send — **never Sendblue**. Prefer other bots hand the body to Texts.
- Bot description and routines: [`TEXTS.md`](TEXTS.md)

Implement watch + send in your own machine-config repo (Stephen’s is `texts-mac-inbound` in [wustep/dotfiles](https://github.com/wustep/dotfiles)).

## This folder

| Path | Role |
|------|------|
| [`TEXTS.md`](TEXTS.md) | Copy-pasteable bot description + routines |
| [`legacy-sendblue/`](legacy-sendblue/) | Retired Sendblue → Vercel inbox puller / loopback receiver |

Do **not** commit secrets, phone numbers, wake tokens, pids, or logs.
