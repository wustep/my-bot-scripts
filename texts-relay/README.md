# texts-relay

Wakes a Grok Bot "Texts" agent when an iMessage arrives through Sendblue. A small Vercel inbox (not in this repo) receives Sendblue's webhook; `pull.py` drains it and wakes the agent through the box's local gateway.

The Texts bot's copy-pasteable description and routines are in [`TEXTS.md`](TEXTS.md).

- `pull.py`: polls the inbox URL in `pull.url` with the bearer in `pull.secret`. Poll cadence: **5s** by day, **60s** overnight (12am–7am in `TEXTS_TZ`) to stay under Upstash free-tier commands.
- `start.sh`: starts the puller after a reboot (paths are relative to this folder).
- `relay.py`: legacy loopback receiver (binds 127.0.0.1; put a tunnel in front). `pull.py` imports its `handle()`.

## Config

| Env var | Required | Meaning |
|---|---|---|
| `TEXTS_ALLOWED_NUMBERS` | yes | Comma-separated E.164 numbers (e.g. `+15551234567`) whose iMessages may wake the agent. No default: if unset, every inbound message is ignored. |
| `TEXTS_AGENT_ID` | yes | Grok Bot agent id to wake. |
| `TEXTS_GATEWAY_PATH` | no | Gateway JSON with `token` and `port` (default `/home/box/agent-data/gateway.json`). |
| `TEXTS_SENDER_NAME` | no | Name used in the wake prompt (default `Owner`). |
| `TEXTS_TZ` | no | IANA time zone for timestamps and quiet hours (default `UTC`). |

Local files (gitignored, kept beside the scripts): `webhook.secret`, `pull.url`, `pull.secret`, `seen.json`, `last-inbound.json`, `health-state.json`, logs and pids.

Never commit secrets, phone numbers, tunnel credentials, pids, or logs.
