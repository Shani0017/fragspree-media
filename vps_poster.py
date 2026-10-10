"""Instagram poster for a VPS (Python 3.8+ and `pip install requests`; nothing else).
Reads plan.json from the media repo, prepares each reel 10 min before its slot and publishes it at the slot.
Config in .env next to this file:  IG_USER_ID=...  IG_TOKEN=...   (and optionally PLAN_URL=...)
Run:  python3 vps_poster.py            (keep it running: systemd / nohup / screen)
      python3 vps_poster.py --dry      show what is due, call nothing"""
import json, os, sys, time, datetime as dt
from pathlib import Path
import requests

HERE = Path(__file__).resolve().parent
GRAPH = "v21.0"
PREP_BEFORE = 600          # create the reel this many seconds before the slot
LATE_LIMIT = 7200          # skip a slot that is more than 2 h late
STATE = HERE / "vps_state.json"
LOG = HERE / "vps_poster.log"


def env():
    d = dict(os.environ)
    f = HERE / ".env"
    if f.exists():
        for ln in f.read_text(encoding="utf-8-sig").splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                d[k.strip()] = v.strip()
    return d


E = env()
PLAN_URL = E.get("PLAN_URL", "https://raw.githubusercontent.com/Shani0017/fragspree-media/main/plan.json")


def log(*a):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} " + " ".join(str(x) for x in a)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def api(method, path, **kw):
    r = requests.request(method, f"https://graph.instagram.com/{GRAPH}/{path}", timeout=60, **kw)
    return r.json()


def refresh_token(state):
    """long-lived tokens last 60 days; refresh about every 30 days (needs the token to be at least 24 h old)"""
    if time.time() - state.get("token_refreshed", 0) < 30 * 86400:
        return
    r = requests.get("https://graph.instagram.com/refresh_access_token", params={"grant_type": "ig_refresh_token", "access_token": E["IG_TOKEN"]}, timeout=30).json()
    if "access_token" in r:
        E["IG_TOKEN"] = r["access_token"]
        f = HERE / ".env"
        lines = [("IG_TOKEN=" + r["access_token"]) if l.startswith("IG_TOKEN=") else l for l in f.read_text(encoding="utf-8-sig").splitlines()]
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        state["token_refreshed"] = time.time()
        log("token refreshed, expires in", r.get("expires_in", "?"), "s")
    else:
        log("token refresh failed:", r.get("error", r))
        state["token_refreshed"] = time.time() - 29 * 86400        # try again tomorrow


def step(plan, state, dry=False):
    now = time.time()
    for it in plan:
        k = str(it["n"])
        s = state.setdefault(k, {})
        if s.get("done") or s.get("skipped"):
            continue
        due = it["unix"]
        if now > due + LATE_LIMIT:
            s["skipped"] = True
            log(f"#{k} SKIPPED (more than {LATE_LIMIT // 3600} h late)")
            continue
        if now < due - PREP_BEFORE:
            continue
        if dry:
            log(f"#{k} due {dt.datetime.fromtimestamp(due):%m-%d %H:%M}  {it['file'][:60]}")
            continue
        try:
            if "container" not in s:
                c = api("POST", f"{E['IG_USER_ID']}/media", data={"media_type": "REELS", "video_url": it["video_url"], "caption": it["caption"],
                                                                   "share_to_feed": "true", "access_token": E["IG_TOKEN"]})
                if "id" not in c:
                    s["tries"] = s.get("tries", 0) + 1
                    log(f"#{k} container failed ({s['tries']}):", c.get("error", c))
                    if s["tries"] >= 3:
                        s["skipped"] = True
                    continue
                s["container"] = c["id"]
                log(f"#{k} prepared")
            if now >= due:
                st = api("GET", s["container"], params={"fields": "status_code", "access_token": E["IG_TOKEN"]}).get("status_code")
                if st == "FINISHED":
                    p = api("POST", f"{E['IG_USER_ID']}/media_publish", data={"creation_id": s["container"], "access_token": E["IG_TOKEN"]})
                    if "id" in p:
                        s["done"] = True
                        s["media_id"] = p["id"]
                        log(f"#{k} PUBLISHED media {p['id']}  {it['file'][:50]}")
                    else:
                        log(f"#{k} publish failed:", p.get("error", p))
                elif st in ("ERROR", "EXPIRED"):
                    log(f"#{k} container {st}; will prepare again")
                    s.pop("container", None)
                    s["tries"] = s.get("tries", 0) + 1
                    if s["tries"] >= 3:
                        s["skipped"] = True
        except Exception as e:
            log(f"#{k} error: {e}")


def main():
    dry = "--dry" in sys.argv
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    log("started" + (" (dry run)" if dry else ""))
    while True:
        try:
            plan = requests.get(PLAN_URL, timeout=30).json()
            if not dry:
                refresh_token(state)
            step(plan, state, dry)
        except Exception as e:
            log("loop error:", e)
        STATE.write_text(json.dumps(state))
        if dry:
            return
        time.sleep(30)


if __name__ == "__main__":
    main()
