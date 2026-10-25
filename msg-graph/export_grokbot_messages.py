#!/usr/bin/env python3
"""Export per-day, per-bot counts of human-authored user messages sent to Grok Bot.
Stdlib only. Read-only (opens SQLite with mode=ro).

Rules: user-role rows from search-index.db; drop bot-to-bot messages (entries flagged
fromAgent in each agent's store.db, plus bodies starting "You're live as"); unwrap
iMessage relays (historical sendblue webhook format / "[date] iMessage from <name>") and attribute them to
"iMessage relay"; dedupe identical normalized text within 10 minutes; days in MSG_GRAPH_TZ (default America/Los_Angeles).

Speed: precompiled regexes; incremental cache seals complete past days and rescans from
local-midnight today (10-minute dedupe lookback) plus any rows with id > high_water_id
(late index arrivals). Cache beside --out as "<out>.cache.json". Prints unchanged=1 when
counts match an existing --out so callers can skip CopyFromBox.
"""
import argparse, sqlite3, glob, json, os, re, collections, datetime as dt, hashlib
from zoneinfo import ZoneInfo

TZ = os.environ.get("MSG_GRAPH_TZ", "America/Los_Angeles")
Z = ZoneInfo(TZ)
RELAY_ID = "imessage-relay"
RELAY = "iMessage relay"
CACHE_VERSION = 3
DEDUP_MS = 600_000

RE_LIVE = re.compile(r"You.re live as")
RE_SEND = re.compile(r"\[sendblue webhook\].*?content:\s*(.*)", re.S)
RE_IM = re.compile(
    r"\[\d{4}-\d\d-\d\d [\d:]+ [AP]M\] iMessage from [^:\n]*:\s*(.*)", re.S
)
RE_NORM = re.compile(r"\W+")


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def cache_path(out):
    return out + ".cache.json"


def store_sigs(agents_dir):
    sigs = []
    for d in sorted(glob.glob(os.path.join(agents_dir, "*/"))):
        sp = os.path.join(d, "store.db")
        if not os.path.exists(sp):
            continue
        st = os.stat(sp)
        sigs.append([os.path.basename(d.rstrip("/")), int(st.st_mtime_ns), st.st_size])
    return sigs


def load_names(agents_dir):
    names = {}
    for d in sorted(glob.glob(os.path.join(agents_dir, "*/"))):
        aid = os.path.basename(d.rstrip("/"))
        try:
            names[aid] = json.load(open(os.path.join(d, "profile.json"))).get("name") or aid[:8]
        except Exception:
            names[aid] = aid[:8]
    return names


def load_from_agent(agents_dir):
    fa = set()
    for d in sorted(glob.glob(os.path.join(agents_dir, "*/"))):
        aid = os.path.basename(d.rstrip("/"))
        sp = os.path.join(d, "store.db")
        if not os.path.exists(sp):
            continue
        for (e,) in ro(sp).execute(
            "select entry from transcript_entries where entry like '%fromAgent%'"
        ):
            j = json.loads(e)
            if j.get("kind") == "message" and j.get("fromAgent"):
                fa.add((aid, j["id"]))
    return fa


def process_message(aid, t, b, names, days, bots, seen):
    """Apply one user message. Returns True if it counted."""
    if RE_LIVE.match(b):
        return False
    m = RE_SEND.match(b) or RE_IM.match(b)
    txt = (m.group(1) if m else b).split("Route to the right bot")[0]
    norm = RE_NORM.sub(" ", txt.lower()).strip()[:200]
    p = seen.get(norm)
    if norm and p is not None and t - p < DEDUP_MS:
        return False
    if norm:
        seen[norm] = t
    bid, nm = (RELAY_ID, RELAY) if m else (aid, names.get(aid, aid[:8]))
    bots[bid] = {"name": nm}
    days[dt.datetime.fromtimestamp(t / 1000, Z).date()][nm] += 1
    return True


def seed_dedupe(aid, t, b, seen):
    if RE_LIVE.match(b):
        return
    m = RE_SEND.match(b) or RE_IM.match(b)
    txt = (m.group(1) if m else b).split("Route to the right bot")[0]
    norm = RE_NORM.sub(" ", txt.lower()).strip()[:200]
    if norm:
        # Keep the latest timestamp for each norm in the lookback window
        prev = seen.get(norm)
        if prev is None or t > prev:
            seen[norm] = t


def days_to_list(days):
    out = []
    if not days:
        return out
    x, end = min(days), max(days)
    while x <= end:
        out.append({"date": x.isoformat(), "byBot": dict(sorted(days[x].items()))})
        x += dt.timedelta(1)
    return out


def list_to_days(day_list):
    days = collections.defaultdict(collections.Counter)
    for d in day_list:
        date = dt.date.fromisoformat(d["date"])
        for nm, n in d.get("byBot", {}).items():
            days[date][nm] = int(n)
    return days


def counts_fingerprint(bots, day_list):
    payload = {"bots": bots, "days": day_list}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.blake2b(raw.encode(), digest_size=16).hexdigest()


def load_cache(path):
    try:
        c = json.load(open(path))
    except Exception:
        return None
    if c.get("version") != CACHE_VERSION:
        return None
    return c


def build_output(generated_at, bots, days):
    return {
        "generatedAt": generated_at or dt.datetime.now(Z).isoformat(timespec="seconds"),
        "timezone": TZ,
        "bots": dict(sorted(bots.items())),
        "days": days_to_list(days),
    }


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Export daily per-bot message counts for msg-graph.")
    ap.add_argument("--db", default="/home/box/agent-data/search-index.db")
    ap.add_argument("--out", default=os.path.join(here, "data", "messages.json"),
                    help="output JSON (default: data/messages.json next to this script)")
    ap.add_argument("--generated-at", help="override timestamp (for reproducibility checks)")
    ap.add_argument("--full", action="store_true", help="ignore cache and rescan everything")
    a = ap.parse_args()

    agents_dir = os.path.join(os.path.dirname(os.path.abspath(a.db)), "agents")
    today = dt.datetime.now(Z).date()
    yesterday = today - dt.timedelta(1)
    start_today = dt.datetime.combine(today, dt.time.min, tzinfo=Z)
    start_today_ms = int(start_today.timestamp() * 1000)
    lookback_ms = start_today_ms - DEDUP_MS

    names = load_names(agents_dir)
    cp = cache_path(a.out)
    cache = None if a.full else load_cache(cp)
    sigs = store_sigs(agents_dir)
    if (
        cache
        and not a.full
        and cache.get("store_sigs") == sigs
        and isinstance(cache.get("from_agent"), list)
    ):
        fa = {tuple(x) for x in cache["from_agent"]}
    else:
        fa = load_from_agent(agents_dir)

    days = collections.defaultdict(collections.Counter)
    bots = {}
    seen = {}
    mode = "full"
    max_id = 0

    sealed_ok = (
        cache
        and cache.get("sealed_through") == yesterday.isoformat()
        and isinstance(cache.get("high_water_id"), int)
    )

    if sealed_ok:
        mode = "incremental"
        days = list_to_days(cache.get("days", []))
        for d in list(days.keys()):
            if d >= today:
                del days[d]
        bots = {k: {"name": v["name"]} for k, v in cache.get("bots", {}).items()}
        high_water_id = cache["high_water_id"]

        # Seed dedupe from the 10 minutes before today (already counted in sealed days)
        seed_q = (
            "select agent_id, entry_id, timestamp_ms, body from messages "
            "where role='user' and timestamp_ms >= ? and timestamp_ms < ? "
            "order by timestamp_ms, id"
        )
        for aid, eid, t, b in ro(a.db).execute(seed_q, (lookback_ms, start_today_ms)):
            if (aid, eid) in fa:
                continue
            seed_dedupe(aid, t, b, seen)

        # New today rows + any late-indexed rows with id > high_water_id
        q = (
            "select id, agent_id, entry_id, timestamp_ms, body from messages "
            "where role='user' and (timestamp_ms >= ? or id > ?) "
            "order by timestamp_ms, id"
        )
        for mid, aid, eid, t, b in ro(a.db).execute(q, (start_today_ms, high_water_id)):
            max_id = max(max_id, mid)
            if (aid, eid) in fa:
                continue
            if t < start_today_ms and mid <= high_water_id:
                continue  # already in sealed counts
            if t < start_today_ms and mid > high_water_id:
                # Late arrival into a sealed day — count it
                process_message(aid, t, b, names, days, bots, seen)
                continue
            process_message(aid, t, b, names, days, bots, seen)
        max_id = max(max_id, high_water_id)
    else:
        q = (
            "select id, agent_id, entry_id, timestamp_ms, body from messages "
            "where role='user' order by timestamp_ms, id"
        )
        for mid, aid, eid, t, b in ro(a.db).execute(q):
            max_id = max(max_id, mid)
            if (aid, eid) in fa:
                continue
            process_message(aid, t, b, names, days, bots, seen)

    for bid in list(bots):
        if bid != RELAY_ID and bid in names:
            bots[bid] = {"name": names[bid]}

    out = build_output(a.generated_at, bots, days)
    day_list = out["days"]
    fp = counts_fingerprint(out["bots"], day_list)

    unchanged = False
    if os.path.exists(a.out):
        try:
            prev = json.load(open(a.out))
            unchanged = counts_fingerprint(prev.get("bots", {}), prev.get("days", [])) == fp
        except Exception:
            unchanged = False

    if not unchanged:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
        with open(a.out, "w") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)

    sealed_days = [d for d in day_list if d["date"] <= yesterday.isoformat()]
    sealed_names = {nm for d in sealed_days for nm in d["byBot"]}
    sealed_bots = {
        bid: meta for bid, meta in out["bots"].items() if meta["name"] in sealed_names
    }
    cache_out = {
        "version": CACHE_VERSION,
        "sealed_through": yesterday.isoformat(),
        "high_water_id": max_id,
        "bots": sealed_bots or dict(out["bots"]),
        "days": sealed_days,
        "fingerprint": fp,
        "store_sigs": sigs,
        "from_agent": sorted([list(x) for x in fa]),
    }
    os.makedirs(os.path.dirname(os.path.abspath(cp)) or ".", exist_ok=True)
    with open(cp, "w") as f:
        json.dump(cache_out, f, indent=2, ensure_ascii=False)

    total = sum(sum(d["byBot"].values()) for d in day_list)
    print(
        f"total={total} days={len(day_list)} mode={mode} "
        f"unchanged={'1' if unchanged else '0'} -> {a.out}"
    )


if __name__ == "__main__":
    main()
