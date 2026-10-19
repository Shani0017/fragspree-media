"""Comment Hub - one phone-friendly page with the comments of all four channels (FragSpree + Brain Massage, Instagram + YouTube),
a suggested reply for each, and one tap to approve and send them.   Runs on the VPS:  python hub.py   (needs: pip install requests)
Settings are in .env next to this file.  --demo shows made-up comments and never posts anything (for testing the page).
Nothing is sent without you pressing the green button.  Replies are sent slowly (random pause between them) so it looks human."""
import datetime as dt
import hmac
import http.server
import json
import os
import random
import re
import secrets
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "hub_state.json"
DEMO = "--demo" in sys.argv


def env(p):
    d = {}
    f = Path(p)
    if f.exists():
        for ln in f.read_text(encoding="utf-8-sig").splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                d[k.strip()] = v.strip()
    return d


CFG = env(HERE / ".env")
CFG.update({k: v for k, v in os.environ.items() if k.startswith(("HUB_", "GEMINI_", "DRY_"))})      # environment overrides the file
PASSWORD = CFG.get("HUB_PASSWORD", "")
PORT = int(CFG.get("HUB_PORT", "8780"))
BIND = CFG.get("HUB_BIND", "127.0.0.1")
DAYS = max(30, int(CFG.get("DAYS", "30")))          # look back a month so older unanswered comments do not disappear
GEMINI = CFG.get("GEMINI_API_KEY", "")
DRY = DEMO or CFG.get("DRY_RUN", "0") == "1"
CAPS = {"Instagram": int(CFG.get("IG_REPLY_CAP", "120")), "YouTube": int(CFG.get("YT_REPLY_CAP", "60"))}   # replies per day (YouTube: 50 quota units each)
AUTO = CFG.get("AUTO_SEND", "0") == "1"          # 1 = safe replies are sent without anyone looking; unclear ones are held back
REFRESH_MIN = int(CFG.get("AUTO_EVERY_MIN", "60"))
AUTO_MAX = int(CFG.get("AUTO_MAX_PER_RUN", "40"))
PAUSE = (int(CFG.get("PAUSE_MIN", "12")), int(CFG.get("PAUSE_MAX", "35")))
ACCOUNTS = {
    "FragSpree": dict(env=CFG.get("FRAG_ENV", r"C:\fragspree\.env"), yt="yt_token.json"),
    "Brain Massage": dict(env=CFG.get("BRAIN_ENV", r"C:\brain_poster\.env"), yt="yt_token_brain.json"),
}
LOCK = threading.Lock()
try:
    CTX = json.loads((HERE / "video_context.json").read_text(encoding="utf-8"))
except Exception:
    CTX = []


def norm_t(t):
    return re.sub(r"[^a-z0-9]+", " ", str(t).lower().replace("#shorts", "")).strip()


def ctx_for(where):
    """the reel's question / correct answer / explanation, found from the caption or title the comment sits under"""
    nw = norm_t(where)
    best = None
    for c in CTX:
        k = c["norm"]
        if nw.startswith(k) or (len(nw) >= 14 and k.startswith(nw)):
            if not best or len(k) > len(best["norm"]):
                best = c
    return None if not best else dict(kind=best["kind"], title=best["title"], q=best["q"], a=best["a"], why=best["why"])
SESSIONS = set()
ATTEMPTS = {}
JOB = {"running": False, "total": 0, "done": 0, "ok": 0, "fail": 0, "log": []}
REFRESH = {"running": False, "msg": "", "notes": []}

# ------------------------------------------------------------------ state
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": {}, "sent": {}}


STATE = load_state()


def save_state():
    with LOCK:
        STATE_FILE.write_text(json.dumps(STATE), encoding="utf-8")


# ------------------------------------------------------------------ classification
SPAM = re.compile(r"(https?://|www\.|\.com\b|dm me|check (out )?my|follow me|sub4sub|promo|giveaway|earn \$|make money|whatsapp|telegram|crypto|forex|cashapp)", re.I)
NEG = re.compile(r"\b(stupid|trash|garbage|scam|fake|boring|hate|worst|dislike|unfollow|cringe|waste|bad|useless|clickbait|wrong)\b", re.I)
ASK = re.compile(r"(\?|^\s*(what|how|why|which|where|when|who|can|could|does|do|is|are|will)\b)", re.I)
REQ = re.compile(r"\b(make|part 2|part two|more|next|tutorial|code|source|please|do one|can you do|try)\b", re.I)
POS = re.compile(r"(love|great|amazing|awesome|nice|cool|best|satisfying|wow|beautiful|perfect|thanks|thank you|fire|genius|brilliant|\u2764|\U0001F525|\U0001F44F|\U0001F60D)", re.I)


def classify(t):
    if SPAM.search(t):
        return "spam"
    if NEG.search(t):
        return "negative"
    if ASK.search(t):
        return "question"
    if REQ.search(t):
        return "request"
    if POS.search(t):
        return "positive"
    return "other"


# ------------------------------------------------------------------ fetching
def ig_fetch(acct, e, since):
    uid, tok = e.get("IG_USER_ID"), e.get("IG_TOKEN")
    if not uid or not tok:
        return [], "no Instagram id/token"
    base = "https://graph.instagram.com/v21.0"
    me = requests.get(f"{base}/{uid}", params={"fields": "username", "access_token": tok}, timeout=30).json()
    if "error" in me:
        return [], "Instagram token problem: " + me["error"].get("message", "")[:80]
    own = (me.get("username") or "").lower()
    med = requests.get(f"{base}/{uid}/media", params={"fields": "id,caption,permalink,comments_count", "limit": 50, "access_token": tok}, timeout=30).json()
    out, expected, blocked = [], 0, False
    for m in med.get("data", []):
        if not m.get("comments_count"):
            continue
        expected += m["comments_count"]
        c = requests.get(f"{base}/{m['id']}/comments", params={"fields": "id,text,from{username},username,timestamp,replies{id}", "limit": 50, "access_token": tok}, timeout=30).json()
        if "error" in c:
            blocked = True
            continue
        for x in c.get("data", []):
            ts = dt.datetime.fromisoformat(x["timestamp"].replace("+0000", "+00:00"))
            who = ((x.get("from") or {}).get("username") or x.get("username") or "").lower()
            if ts < since or who == own:
                continue
            answered = bool((x.get("replies") or {}).get("data"))       # any reply already under it (the API does not say who wrote nested replies)
            out.append(dict(acct=acct, platform="Instagram", id=x["id"], text=x.get("text", ""), user=who or "?", time=ts.isoformat(),
                            where=(m.get("caption") or "")[:90].replace("\n", " "), link=m.get("permalink", ""), answered=answered))
    note = ""
    if expected and not out and not blocked:
        note = f"Instagram counts {expected} comments but will not show them: switch the Meta app to Live mode (App Mode switch at the top of the app dashboard), then Refresh"
    if blocked:
        note = "Instagram refused some comment calls: the token needs the comments permission"
    return out, note


def yt_token(token_file):
    tf, secret = HERE / token_file, HERE / "client_secret.json"
    if not tf.exists() or not secret.exists():
        return None, "YouTube sign-in files missing (copy yt token + client_secret.json here)"
    c = json.loads(secret.read_text(encoding="utf-8"))
    c = c.get("installed") or c.get("web")
    rt = json.loads(tf.read_text(encoding="utf-8"))["refresh_token"]
    r = requests.post(c["token_uri"], data={"client_id": c["client_id"], "client_secret": c["client_secret"], "refresh_token": rt, "grant_type": "refresh_token"}, timeout=30).json()
    if "access_token" not in r:
        return None, "YouTube sign-in expired"
    return r["access_token"], ""


def yt_why(j):
    r = ((j.get("error") or {}).get("errors") or [{}])[0].get("reason", "")
    if r in ("quotaExceeded", "rateLimitExceeded", "dailyLimitExceeded"):
        return "YouTube's daily quota is used up (it resets at 15:00 Malaysia time), will retry automatically"
    if r in ("authError", "forbidden", "insufficientPermissions") or (j.get("error") or {}).get("code") in (401, 403):
        return "YouTube sign-in problem: run the YouTube comment sign-in .bat again and re-paste"
    return "YouTube channel lookup failed" + (f" ({r})" if r else "")


def yt_fetch(acct, token_file, since):
    tok, err = yt_token(token_file)
    if not tok:
        return [], err
    h = {"Authorization": "Bearer " + tok}
    api = "https://www.googleapis.com/youtube/v3"
    ch = requests.get(f"{api}/channels", params={"part": "id", "mine": "true"}, headers=h, timeout=30).json()
    if "items" not in ch:
        return [], yt_why(ch)
    cid = ch["items"][0]["id"]
    out, page, n = [], None, 0
    while n < 5:
        params = {"part": "snippet", "allThreadsRelatedToChannelId": cid, "maxResults": 100, "order": "time"}
        if page:
            params["pageToken"] = page
        resp = requests.get(f"{api}/commentThreads", params=params, headers=h, timeout=30)
        js = resp.json()
        if not resp.ok:
            if any(e.get("reason") == "insufficientPermissions" for e in js.get("error", {}).get("errors", [])):
                return out, "YouTube needs the comment sign-in (run the sign-in .bat on the PC and copy the token here)"
            return out, "YouTube: " + js.get("error", {}).get("message", "")[:80]
        stop = False
        for it in js.get("items", []):
            top = it["snippet"]["topLevelComment"]["snippet"]
            ts = dt.datetime.fromisoformat(top["publishedAt"].replace("Z", "+00:00"))
            if ts < since:
                stop = True
                break
            if top.get("authorChannelId", {}).get("value") == cid:
                continue
            out.append(dict(acct=acct, platform="YouTube", id=it["snippet"]["topLevelComment"]["id"], text=top.get("textOriginal", top.get("textDisplay", "")),
                            user=top.get("authorDisplayName", "?"), time=ts.isoformat(), where=top.get("videoId", ""),
                            link=f"https://youtu.be/{top.get('videoId', '')}&lc={it['snippet']['topLevelComment']['id']}", answered=it["snippet"].get("totalReplyCount", 0) > 0))
        page = js.get("nextPageToken")
        n += 1
        if stop or not page:
            break
    ids = sorted({x["where"] for x in out if x["where"]})
    titles = {}
    for i in range(0, len(ids), 50):
        v = requests.get(f"{api}/videos", params={"part": "snippet", "id": ",".join(ids[i:i + 50])}, headers=h, timeout=30).json()
        for it in v.get("items", []):
            titles[it["id"]] = it["snippet"]["title"]
    for x in out:
        x["where"] = titles.get(x["where"], x["where"])[:90]
    return out, ""


# ------------------------------------------------------------------ suggested replies
POS_TEMPLATES = ["Thank you so much! \u2764", "Really glad you liked it!", "Thanks for watching!", "Appreciate it, more coming soon!", "Thank you! Glad it was a good watch."]


def numbers(t):
    return set(re.findall(r"\d+(?:\.\d+)?", str(t).replace(",", "")))


def suggest_batch(items):
    """Replies are written about THAT video: the AI is given the reel's question, correct answer and explanation.
    Without an AI key a rule-based reply compares the number in the comment with the real answer."""
    if not items:
        return
    if GEMINI and not DEMO:
        payload = []
        for i, it in enumerate(items):
            c = it.get("ctx") or {}
            payload.append({"i": i, "video": it["where"], "video_question": c.get("q", "")[:500], "correct_answer": c.get("a", "")[:200],
                            "solution_summary": c.get("why", "")[:500], "comment": it["text"][:400]})
        prompt = ("You reply to viewer comments for a small short-video channel ({acct}). Each comment is under a specific video; the video's question, correct answer and "
                  "solution are given when we have them. Write ONE short, warm, human reply for each comment (max 200 characters, at most one emoji, no links, no hashtags). Rules: "
                  "if the viewer gives an answer, say kindly whether it matches the correct answer, and if it is wrong give a one-line hint from the solution without being rude; "
                  "if they ask about the solution, answer it from the solution summary; if they ask something we cannot know, say you'll look into it; if they suggest a video, thank them "
                  "and say it is noted; if it is praise, thank them in a specific way that mentions the video's topic; never invent facts that are not in the data; if the comment is rude "
                  "or spam leave the reply empty. Every other comment MUST get a draft, never empty: if it is negative, rude, offensive or contains a stereotype, write a short, calm, polite reply that does not argue, does not repeat or agree with the offensive part and does not insult back (mark \"safe\": false). If you are not sure what the viewer means, or they disagree or debate the answer, or tell a story about themselves, write a short, polite, neutral DRAFT that engages with what they said using only the given data (for a disagreement about how the puzzle is read, say the answer depends on the wording and give the solution's reasoning briefly), and mark it \"safe\": false so a person approves it first. Never claim you agree with something you do not understand. Vary the wording so replies do not look copied. Also give each row \"safe\": true ONLY when you clearly understand the comment AND it is one of: a clear answer attempt to the video's question, plain praise or thanks or emojis, a simple greeting, a question the given data answers, a suggestion for a new video; otherwise \"safe\": false (jokes, sarcasm, comparisons, stories about the viewer, insults, anything unclear or sensitive). Return ONLY a JSON list like [{{\"i\":0,\"reply\":\"...\",\"safe\":true}}].\n\n").format(acct=items[0]["acct"]) + json.dumps(payload)
        try:
            r = requests.post("https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent", params={"key": GEMINI},
                              json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8}}, timeout=90).json()
            txt = r["candidates"][0]["content"]["parts"][0]["text"]
            for row in json.loads(txt):
                it = items[int(row["i"])]
                it["suggestion"] = re.sub(r"https?://\S+", "", str(row.get("reply", ""))).strip()[:260]
                it["auto_ok"] = bool(row.get("safe")) and bool(it["suggestion"])
            for it in items:
                if not (it.get("suggestion") or "").strip() and it["kind"] != "spam":
                    it["suggestion"], it["auto_ok"] = "Thanks for watching and for sharing your thoughts!", False
            return
        except Exception as e:
            REFRESH["notes"].append("AI suggestions unavailable (using simple rules): " + str(e)[:80])
    for it in items:
        if it.get("suggestion"):
            continue
        c = it.get("ctx")
        mine = numbers(it["text"])
        if c and c.get("a") and mine and it["kind"] in ("question", "other", "request", "positive"):
            right = numbers(c["a"].split(".")[0])
            it["auto_ok"] = True
            if right and right & mine:
                it["suggestion"] = random.choice(["Correct! Nicely done.", "Yes, that's right! Great thinking.", "Spot on! Well done."])
            else:
                it["suggestion"] = random.choice(["Good try! Watch the explanation again, the key step is easy to miss.", "Close, but not quite. Check the solution in the video and try again!"])
        elif it["kind"] in ("positive", "other"):
            it["suggestion"] = random.choice(POS_TEMPLATES)
            it["auto_ok"] = it["kind"] == "positive"
        elif it["kind"] == "request":
            it["suggestion"] = random.choice(["Great idea, noted! Thanks for the suggestion.", "Love that idea, adding it to the list!"])
        elif it["kind"] == "question" and DEMO:
            it["suggestion"] = "Great question! The solution is explained step by step in the video."


def do_refresh():
    REFRESH.update(running=True, msg="Reading comments...", notes=[])
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)
    try:
        if DEMO:
            demo_items()
        else:
            for acct, cfg in ACCOUNTS.items():
                for plat in ("Instagram", "YouTube"):
                    REFRESH["msg"] = f"{acct} {plat}..."
                    try:
                        items, note = ig_fetch(acct, env(cfg["env"]), since) if plat == "Instagram" else yt_fetch(acct, cfg["yt"], since)
                    except Exception as e:
                        items, note = [], f"{plat} error: {str(e)[:80]}"
                    if note:
                        REFRESH["notes"].append(f"{acct} {plat}: {note}")
                    for x in items:
                        key = f"{acct}|{plat}|{x['id']}"
                        if key in STATE["items"]:
                            if STATE["items"][key].get("user") in ("?", "", None) and x.get("user") not in ("?", "", None):
                                STATE["items"][key]["user"] = x["user"]
                            if x["answered"] and STATE["items"][key]["status"] == "new":
                                STATE["items"][key]["status"] = "answered"
                            continue
                        x["key"], x["kind"], x["suggestion"], x["ctx"] = key, classify(x["text"]), "", ctx_for(x["where"])
                        x["status"] = "answered" if x["answered"] else "new"
                        STATE["items"][key] = x
        REFRESH["msg"] = "Writing suggested replies..."
        todo = [it for it in STATE["items"].values() if it["status"] == "new" and not it.get("suggestion") and it["kind"] != "spam" and not it.get("sugg_done")]
        by = {}
        for it in todo:
            by.setdefault(it["acct"], []).append(it)
        for acct, lst in by.items():
            for i in range(0, len(lst), 20):
                suggest_batch(lst[i:i + 20])
                for it in lst[i:i + 20]:
                    it["sugg_done"] = True
        save_state()
        REFRESH["msg"] = "Done"
    except Exception as e:
        REFRESH["msg"] = "Error: " + str(e)[:120]
    REFRESH["running"] = False


DEMO_CTX = {"Two Eggs 100 Floors": dict(kind="interview", q="A building has one hundred floors. You have two eggs. What's the fewest drops that guarantees you find the breaking floor?", a="14 DROPS. Fourteen drops, the fewest that always works.", why="Drop at 14, then 27, 39 ... each drop on egg one shrinks the crawl by one."),
            "Guess The Output Aliasing": dict(kind="interview", q="a = [1,2,3]; b = a; b.append(4); print(len(a))", a="4. b = a gives the same list a second name.", why="Both names point at one list."),
            "Second Highest Salary": dict(kind="interview", q="Return the second highest salary.", a="6500. Distinct, order desc, offset one.", why="SELECT DISTINCT salary ORDER BY salary DESC LIMIT 1 OFFSET 1")}


def demo_items():
    if STATE["items"]:
        return
    rows = [("FragSpree", "Instagram", "ballsfan", "What song is this?", "200 Balls Plinko #1"), ("FragSpree", "Instagram", "asmr_lover", "So satisfying \U0001F525\U0001F525", "Bell Tower Drop"),
            ("FragSpree", "Instagram", "promo_king", "check my page www.cheap-followers.com", "Neon Pad Bounce"), ("FragSpree", "YouTube", "Aisha", "Can you make one with more balls?", "Rays Bouncing In A Funnel"),
            ("FragSpree", "YouTube", "Tom", "this is boring honestly", "Brick Rain"), ("FragSpree", "YouTube", "Lee", "Love these, keep going!", "Glass Bowl Drop"),
            ("Brain Massage", "Instagram", "dev_raj", "Is it 14 drops? I did it differently", "Two Eggs 100 Floors"), ("Brain Massage", "Instagram", "mia.codes", "Great explanation, thanks", "Gray Code"),
            ("Brain Massage", "YouTube", "Sam", "Why does b = a not copy the list?", "Guess The Output Aliasing"), ("Brain Massage", "YouTube", "Nina", "Please do more SQL ones", "Second Highest Salary")]
    now = dt.datetime.now(dt.timezone.utc)
    for i, (acct, plat, user, text, where) in enumerate(rows):
        key = f"{acct}|{plat}|demo{i}"
        STATE["items"][key] = dict(key=key, acct=acct, platform=plat, id=f"demo{i}", text=text, user=user, time=(now - dt.timedelta(minutes=17 * i + 5)).isoformat(),
                                   where=where, link="https://example.com", answered=False, kind=classify(text), suggestion="", status="new", ctx=ctx_for(where) or DEMO_CTX.get(where))


# ------------------------------------------------------------------ sending
def clean_reply(t):
    t = re.sub(r"https?://\S+", "", t).strip()
    return t[:300]


def send_one(it, text):
    if DRY:
        return True, "dry run (nothing posted)"
    cfg = ACCOUNTS[it["acct"]]
    if it["platform"] == "Instagram":
        e = env(cfg["env"])
        r = requests.post(f"https://graph.instagram.com/v21.0/{it['id']}/replies", data={"message": text, "access_token": e.get("IG_TOKEN", "")}, timeout=30).json()
        return ("id" in r), (r.get("error", {}).get("message", "") or "sent")[:100]
    tok, err = yt_token(cfg["yt"])
    if not tok:
        return False, err
    r = requests.post("https://www.googleapis.com/youtube/v3/comments", params={"part": "snippet"}, headers={"Authorization": "Bearer " + tok},
                      json={"snippet": {"parentId": it["id"], "textOriginal": text}}, timeout=30)
    return r.ok, ("sent" if r.ok else r.json().get("error", {}).get("message", "")[:100])


def run_send(batch):
    JOB.update(running=True, total=len(batch), done=0, ok=0, fail=0, log=[])
    today = dt.datetime.now().strftime("%Y-%m-%d")
    sent = STATE["sent"].setdefault(today, {"Instagram": 0, "YouTube": 0})
    for it, text in batch:
        plat = it["platform"]
        if sent.get(plat, 0) >= CAPS[plat]:
            JOB["fail"] += 1
            JOB["log"].append(f"{it['user']}: skipped (daily limit for {plat})")
        else:
            try:
                ok, msg = send_one(it, text)
            except Exception as e:
                ok, msg = False, str(e)[:80]
            if ok and DRY:
                JOB["ok"] += 1                      # test mode: nothing was posted, so nothing is marked as sent or counted
                JOB["log"].append(f"{it['user']}: test mode, not posted")
            elif ok:
                it["status"], it["sent_text"], it["sent_at"] = "sent", text, dt.datetime.now().isoformat()
                sent[plat] = sent.get(plat, 0) + 1
                JOB["ok"] += 1
            else:
                JOB["fail"] += 1
                JOB["log"].append(f"{it['user']}: {msg}")
            save_state()
            if not DRY and JOB["done"] < len(batch) - 1:
                time.sleep(random.randint(*PAUSE))
        JOB["done"] += 1
    JOB["running"] = False


def auto_cycle():
    """Refresh, then send every reply the AI marked safe (never spam/negative/unclear). Everything else is held for you."""
    if REFRESH.get("running") or JOB.get("running"):
        return
    do_refresh()
    if DRY:
        return
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    batch = []
    for it in STATE["items"].values():
        if it["status"] != "new" or it["kind"] in ("spam", "negative") or not it.get("auto_ok") or not (it.get("suggestion") or "").strip():
            it["held"] = it["status"] == "new"
            continue
        try:
            if dt.datetime.fromisoformat(it["time"]) > cutoff:
                continue
        except Exception:
            pass
        it["held"] = False
        it["auto"] = True
        batch.append((it, clean_reply(it["suggestion"])))
        if len(batch) >= AUTO_MAX:
            break
    save_state()
    if batch:
        run_send(batch)


def auto_loop():
    time.sleep(20)
    while True:
        try:
            auto_cycle()
        except Exception as e:
            REFRESH["notes"].append("auto error: " + str(e)[:80])
        time.sleep(max(10, REFRESH_MIN) * 60)


# ------------------------------------------------------------------ web
import analytics
ANPAGE = open(HERE / "analytics.html", encoding="utf-8").read() if (HERE / "analytics.html").exists() else "<h1>analytics.html missing</h1>"
PAGE = open(HERE / "index.html", encoding="utf-8").read() if (HERE / "index.html").exists() else "<h1>index.html missing</h1>"
LOGIN = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><meta name=theme-color content="#0a0c12"><title>Sign in - Comment Hub</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel=stylesheet>
<style>*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(900px 480px at 70% -10%,rgba(124,107,255,.16),transparent 60%),#0a0c12;color:#e9ebf3;font:15px/1.5 Inter,system-ui,-apple-system,Segoe UI,sans-serif;-webkit-font-smoothing:antialiased}}
.card{{width:min(380px,calc(100vw - 32px));background:#12151d;border:1px solid #232938;border-radius:20px;padding:32px 28px;box-shadow:0 24px 60px rgba(0,0,0,.45)}}
.mark{{width:48px;height:48px;border-radius:14px;background:linear-gradient(135deg,#7c6bff,#5b8cff);display:grid;place-items:center;box-shadow:0 10px 24px rgba(124,107,255,.4);margin-bottom:18px}}
.mark svg{{width:24px;height:24px;stroke:#fff;fill:none;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}}
h1{{margin:0;font-size:24px;letter-spacing:-.02em}}p.s{{margin:4px 0 22px;color:#8c94aa;font-size:14px}}
label{{display:block;font-size:12.5px;font-weight:600;color:#8c94aa;margin-bottom:6px}}
input{{width:100%;padding:13px 14px;border-radius:12px;border:1px solid #2d3447;background:#0e1118;color:#fff;font:inherit;font-size:16px}}
input:focus{{outline:2px solid #7c6bff;outline-offset:-1px;border-color:transparent}}
button{{margin-top:16px;width:100%;padding:13px;border-radius:12px;border:0;background:linear-gradient(135deg,#7c6bff,#5b8cff);color:#fff;font:inherit;font-weight:600;font-size:16px;cursor:pointer;box-shadow:0 8px 20px rgba(124,107,255,.3)}}
button:hover{{filter:brightness(1.08)}}.err{{color:#f87171;font-size:13.5px;min-height:20px;margin:12px 0 0}}</style>
<form class=card method=post action=/login><div class=mark><svg viewBox="0 0 24 24"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg></div><h1>Comment Hub</h1><p class=s>Sign in to manage replies and analytics.</p><label for=p>Password</label><input id=p type=password name=p placeholder="Enter your password" autofocus autocomplete=current-password><button>Sign in</button><p class=err>{msg}</p></form>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def ip(self):
        return self.headers.get("CF-Connecting-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or self.client_address[0]

    def authed(self):
        c = self.headers.get("Cookie", "")
        m = re.search(r"hub=([\w\-]+)", c)
        return bool(m and m.group(1) in SESSIONS)

    def send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj), "application/json")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/ui.css":
            return self.send(200, (HERE / "ui.css").read_bytes(), "text/css; charset=utf-8")
        if not self.authed():
            return self.send(200, LOGIN.format(msg=""))
        if path == "/":
            return self.send(200, PAGE.replace("__DEMO__", "true" if DRY else "false").replace("__AUTO__", "true" if AUTO else "false"))
        if path == "/analytics":
            return self.send(200, ANPAGE)
        if path == "/api/analytics":
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            return self.json(analytics.report(q.get("from", ""), q.get("to", ""), q.get("acct", ""), q.get("plat", ""), q.get("kind", "")))
        if path == "/api/items":
            items = sorted(STATE["items"].values(), key=lambda x: x["time"], reverse=True)
            return self.json({"items": [i for i in items if i["status"] in ("new", "sent")][:400], "refresh": REFRESH, "job": JOB, "caps": CAPS, "auto": AUTO,
                              "sent_today": STATE["sent"].get(dt.datetime.now().strftime("%Y-%m-%d"), {})})
        self.send(404, "not found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        if path == "/login":
            ip = self.ip()
            now = time.time()
            ATTEMPTS[ip] = [t for t in ATTEMPTS.get(ip, []) if now - t < 600]
            if len(ATTEMPTS[ip]) >= 5:
                return self.send(429, LOGIN.format(msg="Too many tries. Wait 10 minutes."))
            pw = urllib.parse.parse_qs(raw.decode()).get("p", [""])[0]
            if PASSWORD and hmac.compare_digest(pw.encode(), PASSWORD.encode()):
                tok = secrets.token_urlsafe(32)
                SESSIONS.add(tok)
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                return self.send(303, "", extra={"Location": "/", "Set-Cookie": f"hub={tok}; HttpOnly; SameSite=Strict; Max-Age=2592000; Path=/{secure}"})
            ATTEMPTS[ip].append(now)
            return self.send(401, LOGIN.format(msg="Wrong password."))
        if not self.authed() or self.headers.get("X-Hub") != "1":
            return self.send(403, "forbidden")
        body = json.loads(raw or b"{}")
        if path == "/api/refresh":
            if not REFRESH["running"]:
                threading.Thread(target=do_refresh, daemon=True).start()
            return self.json({"ok": True})
        if path == "/api/send":
            if JOB["running"]:
                return self.json({"ok": False, "error": "already sending"})
            batch = []
            for key, text in (body.get("replies") or {}).items():
                it = STATE["items"].get(key)
                text = clean_reply(text)
                if it and it["status"] == "new" and text:
                    batch.append((it, text))
            if not batch:
                return self.json({"ok": False, "error": "nothing to send"})
            threading.Thread(target=run_send, args=(batch,), daemon=True).start()
            return self.json({"ok": True, "n": len(batch)})
        if path == "/api/skip":
            for key in body.get("keys", []):
                if key in STATE["items"]:
                    STATE["items"][key]["status"] = "skipped"
            save_state()
            return self.json({"ok": True})
        if path == "/api/an_refresh":
            threading.Thread(target=analytics.refresh, daemon=True).start()
            return self.json({"ok": True})
        if path == "/api/save":
            for key, text in (body.get("edits") or {}).items():
                if key in STATE["items"]:
                    STATE["items"][key]["suggestion"] = text[:300]
            save_state()
            return self.json({"ok": True})
        self.send(404, "not found")


def main():
    if not PASSWORD or len(PASSWORD) < 8:
        raise SystemExit("Set HUB_PASSWORD (at least 8 characters) in .env first.")
    if DEMO:
        demo_items()
        do_refresh()
    for it in STATE["items"].values():      # re-write old suggestions once so they carry the safe/unsafe flag
        if it.get("status") == "new" and ("auto_ok" not in it or (not it.get("suggestion") and it["kind"] != "spam" and not it.get("draft3"))):
            it["draft3"] = True
            it["suggestion"], it["sugg_done"] = "", False
    analytics.init(HERE, env, ACCOUNTS, yt_token, yt_why)
    if not DEMO:
        threading.Thread(target=analytics.loop, daemon=True).start()
    if AUTO and not DEMO:
        threading.Thread(target=auto_loop, daemon=True).start()
    srv = http.server.ThreadingHTTPServer((BIND, PORT), H)
    print(f"Comment Hub on http://{BIND}:{PORT}" + ("   (DEMO / dry run: nothing is posted)" if DRY else ""), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
