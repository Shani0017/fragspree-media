"""Analytics for the Comment Hub: reads views / likes / comments / shares / saves for every Instagram reel and YouTube video of both
accounts, keeps a daily snapshot of followers / subscribers, and answers the dashboard's date-filtered questions.
Totals are the CURRENT lifetime numbers of the posts that were published inside the chosen dates (the platforms give no per-day history of a post).
Follower / subscriber growth is measured from our own daily snapshots, so it starts on the day this was first switched on."""
import datetime as dt
import json
import re
import threading
import time
from pathlib import Path

import requests

MYT = dt.timezone(dt.timedelta(hours=8))
STATE = {"posts": {}, "snaps": {}, "updated": "", "notes": []}
RUN = {"running": False, "msg": ""}
_cfg = {}
LOCK = threading.Lock()


def init(here, env, accounts, yt_token, why=lambda j: "YouTube channel lookup failed"):
    _cfg.update(here=Path(here), env=env, accounts=accounts, yt_token=yt_token, why=why)
    f = Path(here) / "analytics.json"
    _cfg["file"] = f
    if f.exists():
        try:
            STATE.update(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass


def save():
    _cfg["file"].write_text(json.dumps(STATE), encoding="utf-8")


def kind_of(title):
    t = (title or "").lower()
    if "interview" in t or "quant" in t or "sql" in t:
        return "Interview"
    if any(w in t for w in ("riddle", "math", "puzzle", "paradox", "probability", "how old", "dollar")):
        return "Math"
    if any(w in t for w in ("piano", "melody", "music", "notes", "song")):
        return "Music"
    return "ASMR"


def _dt(s):
    return dt.datetime.fromisoformat(str(s).replace("+0000", "+00:00").replace("Z", "+00:00"))


def fetch_ig(acct, e):
    uid, tok = e.get("IG_USER_ID"), e.get("IG_TOKEN")
    if not uid or not tok:
        return None, "no Instagram id/token"
    b = "https://graph.instagram.com/v21.0"
    me = requests.get(f"{b}/{uid}", params={"fields": "username,followers_count,media_count", "access_token": tok}, timeout=30).json()
    if "error" in me:
        return None, "Instagram: " + me["error"].get("message", "")[:80]
    url, params, n = f"{b}/{uid}/media", {"fields": "id,caption,timestamp,like_count,comments_count,media_product_type,permalink", "limit": 50, "access_token": tok}, 0
    media = []
    while url and n < 12:
        j = requests.get(url, params=params, timeout=30).json()
        media += j.get("data", [])
        url, params, n = (j.get("paging") or {}).get("next"), None, n + 1
    now = time.time()
    for m in media:
        key = f"{acct}|Instagram|{m['id']}"
        old = STATE["posts"].get(key, {})
        ts = _dt(m["timestamp"])
        p = dict(old, key=key, acct=acct, plat="Instagram", id=m["id"], title=(m.get("caption") or "").split("\n")[0][:90], ts=ts.isoformat(),
                 likes=m.get("like_count", 0), comments=m.get("comments_count", 0), link=m.get("permalink", ""), type=m.get("media_product_type", ""))
        age_h = (now - ts.timestamp()) / 3600
        stale = now - old.get("ins_at", 0) > (3600 * 3 if age_h < 24 * 14 else 3600 * 24)
        if stale:
            r = requests.get(f"{b}/{m['id']}/insights", params={"metric": "views,reach,saved,shares", "access_token": tok}, timeout=30).json()
            for row in r.get("data", []):
                v = (row.get("values") or [{}])[0].get("value", 0)
                p[{"views": "views", "reach": "reach", "saved": "saves", "shares": "shares"}[row["name"]]] = v
            p["ins_at"] = now
            time.sleep(0.15)
        p.setdefault("views", 0), p.setdefault("reach", 0), p.setdefault("saves", 0), p.setdefault("shares", 0)
        p["kind"] = kind_of(p["title"])
        STATE["posts"][key] = p
    return dict(followers=me.get("followers_count", 0), posts=me.get("media_count", len(media))), ""


def fetch_yt(acct, token_file):
    tok, err = _cfg["yt_token"](token_file)
    if not tok:
        return None, err
    h = {"Authorization": "Bearer " + tok}
    api = "https://www.googleapis.com/youtube/v3"
    ch = requests.get(f"{api}/channels", params={"part": "contentDetails,statistics", "mine": "true"}, headers=h, timeout=30).json()
    if "items" not in ch:
        return None, _cfg["why"](ch)
    c = ch["items"][0]
    up = c["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, page = [], None
    for _ in range(12):
        j = requests.get(f"{api}/playlistItems", params={"part": "contentDetails", "playlistId": up, "maxResults": 50, **({"pageToken": page} if page else {})}, headers=h, timeout=30).json()
        ids += [x["contentDetails"]["videoId"] for x in j.get("items", [])]
        page = j.get("nextPageToken")
        if not page:
            break
    for i in range(0, len(ids), 50):
        v = requests.get(f"{api}/videos", params={"part": "snippet,statistics,status", "id": ",".join(ids[i:i + 50])}, headers=h, timeout=30).json()
        for it in v.get("items", []):
            if it.get("status", {}).get("privacyStatus") != "public":
                continue
            st, sn = it.get("statistics", {}), it["snippet"]
            key = f"{acct}|YouTube|{it['id']}"
            STATE["posts"][key] = dict(key=key, acct=acct, plat="YouTube", id=it["id"], title=sn["title"][:90], ts=_dt(sn["publishedAt"]).isoformat(),
                                       views=int(st.get("viewCount", 0)), likes=int(st.get("likeCount", 0)), comments=int(st.get("commentCount", 0)),
                                       reach=0, saves=0, shares=0, link=f"https://youtu.be/{it['id']}", type="SHORT", kind=kind_of(sn["title"]))
    s = c.get("statistics", {})
    return dict(followers=int(s.get("subscriberCount", 0)), posts=int(s.get("videoCount", 0)), channel_views=int(s.get("viewCount", 0))), ""


def refresh():
    if RUN["running"]:
        return
    RUN.update(running=True, msg="Reading Instagram and YouTube...")
    notes = []
    try:
        today = dt.datetime.now(MYT).strftime("%Y-%m-%d")
        snap = STATE["snaps"].setdefault(today, {})
        for acct, cfg in _cfg["accounts"].items():
            for plat in ("Instagram", "YouTube"):
                RUN["msg"] = f"{acct} {plat}..."
                try:
                    r, note = fetch_ig(acct, _cfg["env"](cfg["env"])) if plat == "Instagram" else fetch_yt(acct, cfg["yt"])
                except Exception as ex:
                    r, note = None, str(ex)[:80]
                if note:
                    notes.append(f"{acct} {plat}: {note}")
                if r:
                    snap[f"{acct}|{plat}"] = r
        STATE["updated"] = dt.datetime.now(MYT).isoformat()
        STATE["notes"] = notes
        save()
    except Exception as ex:
        STATE["notes"] = ["analytics error: " + str(ex)[:100]]
    RUN.update(running=False, msg="")


def loop(every_h=3):
    time.sleep(40)
    while True:
        try:
            refresh()
        except Exception:
            pass
        time.sleep(every_h * 3600)


def _eng(p):
    return p["likes"] + p["comments"] + p.get("shares", 0) + p.get("saves", 0)


def report(start, end, acct, plat, kind):
    """start/end: YYYY-MM-DD (MYT, inclusive); acct/plat/kind: '' for all."""
    posts = []
    for p in STATE["posts"].values():
        d = _dt(p["ts"]).astimezone(MYT)
        ds = d.strftime("%Y-%m-%d")
        if start and ds < start or end and ds > end:
            continue
        if acct and p["acct"] != acct or plat and p["plat"] != plat or kind and p["kind"] != kind:
            continue
        posts.append(dict(p, day=ds, hour=d.hour, wd=d.weekday()))
    tot = dict(posts=len(posts), views=sum(p["views"] for p in posts), likes=sum(p["likes"] for p in posts), comments=sum(p["comments"] for p in posts),
               shares=sum(p.get("shares", 0) for p in posts), saves=sum(p.get("saves", 0) for p in posts), reach=sum(p.get("reach", 0) for p in posts))
    tot["engagement"] = round(100 * (tot["likes"] + tot["comments"] + tot["shares"] + tot["saves"]) / tot["views"], 2) if tot["views"] else 0
    tot["avg_views"] = round(tot["views"] / len(posts)) if posts else 0

    def group(keyf):
        g = {}
        for p in posts:
            g.setdefault(keyf(p), []).append(p)
        return [dict(name=k, posts=len(v), views=sum(x["views"] for x in v), avg_views=round(sum(x["views"] for x in v) / len(v)),
                     avg_likes=round(sum(x["likes"] for x in v) / len(v), 1), avg_comments=round(sum(x["comments"] for x in v) / len(v), 2),
                     eng=round(100 * sum(_eng(x) for x in v) / max(1, sum(x["views"] for x in v)), 2)) for k, v in g.items()]
    wd = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by_kind = sorted(group(lambda p: p["kind"]), key=lambda r: -r["avg_views"])
    by_hour = sorted(group(lambda p: f"{p['hour']:02d}:00"), key=lambda r: r["name"])
    by_wd = sorted(group(lambda p: wd[p["wd"]]), key=lambda r: wd.index(r["name"]))
    by_plat = sorted(group(lambda p: p["acct"] + " " + p["plat"]), key=lambda r: -r["views"])
    daily = {}
    for p in posts:
        d = daily.setdefault(p["day"], dict(day=p["day"], posts=0, views=0, likes=0, comments=0))
        d["posts"] += 1
        d["views"] += p["views"]
        d["likes"] += p["likes"]
        d["comments"] += p["comments"]
    top = sorted(posts, key=lambda p: -p["views"])[:15]
    top = [dict(title=p["title"], acct=p["acct"], plat=p["plat"], kind=p["kind"], day=p["day"], views=p["views"], likes=p["likes"], comments=p["comments"],
                shares=p.get("shares", 0), saves=p.get("saves", 0), link=p["link"]) for p in top]
    # followers / subscribers from snapshots
    growth = []
    days = sorted(STATE["snaps"])
    for k in sorted({k for d in STATE["snaps"].values() for k in d}):
        a, pl = k.split("|")
        if acct and a != acct or plat and pl != plat:
            continue
        series = [(d, STATE["snaps"][d][k]["followers"]) for d in days if k in STATE["snaps"][d] and (not start or d >= start) and (not end or d <= end)]
        if series:
            growth.append(dict(name=f"{a} {pl}", now=series[-1][1], gain=series[-1][1] - series[0][1], since=series[0][0], series=series))
    return dict(totals=tot, by_kind=by_kind, by_hour=by_hour, by_weekday=by_wd, by_account=by_plat, daily=sorted(daily.values(), key=lambda d: d["day"]),
                top=top, growth=growth, updated=STATE.get("updated", ""), notes=STATE.get("notes", []), running=RUN["running"], msg=RUN["msg"],
                first_snapshot=days[0] if days else "", n_posts_all=len(STATE["posts"]))
