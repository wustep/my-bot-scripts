# texts-relay

Docs and retired helpers for a **Texts** Grok Bot (iMessage inbox + router).

## Live path (Mac)

Command inbound is a always-on watcher on a Mac signed into Messages (not this folder):

- Poll `~/Library/Messages/chat.db` for a dedicated command thread (an iMessage address on the same Apple ID).
- Fail-closed allowlist of sender numbers/emails.
- `POST` the Texts bot’s webhook with `{ "source": "mac-mini", "from_number", "content", "date_sent", "media_url" }`.
- Bot description and routines: [`TEXTS.md`](TEXTS.md)

Implement the watcher in your own machine-config repo (Stephen’s is `texts-mac-inbound` in [wustep/dotfiles](https://github.com/wustep/dotfiles)).

## This folder

| Path | Role |
|------|------|
| [`TEXTS.md`](TEXTS.md) | Copy-pasteable bot description + routines |
| [`legacy-sendblue/`](legacy-sendblue/) | Retired Sendblue → Vercel inbox puller / loopback receiver |

Do **not** commit secrets, phone numbers, wake tokens, pids, or logs.
