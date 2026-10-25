#!/usr/bin/env python3
"""Drain the Vercel Sendblue inbox and wake Texts. Does not send iMessages."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import relay as r

DIR = Path(__file__).resolve().parent
# Daytime poll (seconds).
DAY_INTERVAL = 5
# Quiet hours in TEXTS_TZ (default UTC): midnight–7am (hours 0..6).
NIGHT_INTERVAL = 60
NIGHT_HOURS = range(0, 7)
TZ = ZoneInfo(os.environ.get("TEXTS_TZ", "UTC"))
TIMEOUT = 10
# Exit so start.sh can respawn with a clean TLS state instead of looping forever.
MAX_CONSECUTIVE_ERRORS = 8


def current_interval() -> int:
    hour = datetime.now(TZ).hour
    return NIGHT_INTERVAL if hour in NIGHT_HOURS else DAY_INTERVAL


def pull_once() -> list[dict]:
    url = (DIR / "pull.url").read_text().strip()
    secret = (DIR / "pull.secret").read_text().strip()
    req = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
    )
    # Bypass any ambient proxies; textsbot is direct HTTPS.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode() or "{}")
    return list(data.get("messages") or [])


def main() -> None:
    r.log(
        f"puller start day={DAY_INTERVAL}s night={NIGHT_INTERVAL}s "
        f"({TZ.key} hours {list(NIGHT_HOURS)})"
    )
    consecutive = 0
    while True:
        try:
            for payload in pull_once():
                r.handle(payload)
            consecutive = 0
        except urllib.error.HTTPError as e:
            consecutive += 1
            r.log(f"pull_http {e.code} ({consecutive}/{MAX_CONSECUTIVE_ERRORS})")
        except Exception as e:
            consecutive += 1
            r.log(
                f"pull_error {type(e).__name__}: {e} ({consecutive}/{MAX_CONSECUTIVE_ERRORS})"
            )
        if consecutive >= MAX_CONSECUTIVE_ERRORS:
            r.log("puller exit: too many consecutive errors")
            raise SystemExit(1)
        interval = current_interval()
        # Back off while failing so we don't spam SSL errors every interval.
        sleep_for = interval if consecutive == 0 else min(60, interval * consecutive)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
