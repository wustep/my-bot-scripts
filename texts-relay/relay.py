#!/usr/bin/env python3
"""Local Sendblue receive relay. Bind loopback only. Cloudflare Tunnel in front."""
from __future__ import annotations

import hmac
import json
import os
import threading
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

DIR = Path(__file__).resolve().parent
SECRET = (DIR / "webhook.secret").read_text().strip()
# Grok Bot gateway file (token + port) and the agent to wake. Both come from env.
GATEWAY_PATH = Path(os.environ.get("TEXTS_GATEWAY_PATH", "/home/box/agent-data/gateway.json"))
AGENT_ID = os.environ.get("TEXTS_AGENT_ID", "")
# Name shown in the wake prompt, e.g. "[date] iMessage from Owner (+1555...):"
SENDER_NAME = os.environ.get("TEXTS_SENDER_NAME", "Owner")
# Comma-separated E.164 numbers allowed to wake Texts (e.g. "+15551234567"). No default:
# when unset, every inbound message is ignored (fail closed).
ALLOWED = {n.strip() for n in os.environ.get("TEXTS_ALLOWED_NUMBERS", "").split(",") if n.strip()}
SEEN_PATH = DIR / "seen.json"
HOST = "127.0.0.1"
PORT = 8788
LOG = DIR / "relay.log"
TZ = ZoneInfo(os.environ.get("TEXTS_TZ", "UTC"))


def log(msg: str) -> None:
    with LOG.open("a") as f:
        f.write(msg.rstrip() + "\n")


def format_inbound(sent: str, content: str, number: str) -> str:
    dt = datetime.fromisoformat(sent.replace("Z", "+00:00")).astimezone(TZ)
    hour = dt.strftime("%I").lstrip("0") or "12"
    stamp = f"{dt.strftime('%Y-%m-%d')} {hour}:{dt.strftime('%M')} {dt.strftime('%p')}"
    return f"[{stamp}] iMessage from {SENDER_NAME} ({number}):\n\n{content}"


def load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    try:
        return set((json.loads(SEEN_PATH.read_text()).get("seen_handles") or []))
    except Exception:
        return set()


def save_seen(seen: set[str]) -> None:
    items = list(seen)[-200:]
    SEEN_PATH.write_text(json.dumps({"seen_handles": items}, indent=2) + "\n")


def wake_texts(payload: dict) -> None:
    if not AGENT_ID:
        raise RuntimeError("TEXTS_AGENT_ID is not set")
    gateway = json.loads(GATEWAY_PATH.read_text())
    token = gateway["token"]
    port = int(gateway.get("port") or 1340)
    content = (payload.get("content") or "").strip()
    handle = payload.get("message_handle") or ""
    sent = payload.get("date_sent") or ""
    media = payload.get("media_url") or ""
    visible = content if not media else f"{content}\n{media}".strip()
    number = payload.get("from_number") or payload.get("number") or ""
    prompt = format_inbound(sent, visible, number)
    (DIR / "last-inbound.json").write_text(
        json.dumps({"handle": handle, "sent": sent, "content": content, "media_url": media}, indent=2) + "\n"
    )
    body = json.dumps({"agentId": AGENT_ID, "prompt": prompt}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/sendPrompt",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def handle(payload: dict) -> None:
    try:
        if payload.get("is_outbound") is True:
            return
        from_number = payload.get("from_number") or payload.get("number") or ""
        if from_number not in ALLOWED:
            log(f"ignore number {from_number}")
            return
        if (payload.get("message_type") or "message") == "group" or payload.get("group_id"):
            log("ignore group")
            return
        handle_id = payload.get("message_handle") or ""
        if not handle_id:
            log("ignore missing handle")
            return
        seen = load_seen()
        if handle_id in seen:
            log(f"dedup {handle_id}")
            return
        seen.add(handle_id)
        save_seen(seen)
        wake_texts(payload)
        log(f"woke {handle_id}")
    except Exception as e:
        log(f"handle_error {type(e).__name__}: {e}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        log("http " + (fmt % args))

    def _ok(self, code: int = 200, body: bytes = b"OK") -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._ok(200, b"ok") if self.path in ("/", "/health") else self._ok(404, b"no")

    def do_POST(self) -> None:
        if self.path not in ("/", "/sendblue", "/sendblue/receive"):
            self._ok(404, b"no")
            return
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        got = self.headers.get("sb-signing-secret") or self.headers.get("x-webhook-secret") or ""
        if not SECRET or not hmac.compare_digest(got, SECRET):
            self._ok(401, b"no")
            return
        try:
            payload = json.loads(raw.decode() or "{}")
        except Exception:
            self._ok(400, b"bad")
            return
        self._ok(200, b"OK")
        threading.Thread(target=handle, args=(payload,), daemon=True).start()


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"listen {HOST}:{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
