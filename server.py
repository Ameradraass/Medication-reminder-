#!/usr/bin/env python3
"""Medicine Alarm server: schedule sync, pairing, escalation, offline detection. Python 3.9+, stdlib only."""
import os, json, sqlite3, secrets, hashlib, time, threading, urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

DB_PATH = os.environ.get("DB_PATH", "medalarm.db")
PORT = int(os.environ.get("PORT", "8080"))
NTFY = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")
TICK = float(os.environ.get("TICK_SECONDS", "30"))
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
LOCK = threading.RLock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS patients(id TEXT PRIMARY KEY, name TEXT, token TEXT UNIQUE, tz TEXT, lang TEXT, created REAL,
  last_seen REAL, battery INTEGER, charging INTEGER DEFAULT 0, offline_alerted INTEGER DEFAULT 0,
  first_min REAL DEFAULT 10, urgent_min REAL DEFAULT 20, repeat_min REAL DEFAULT 10, max_repeats INTEGER DEFAULT 6, offline_min REAL DEFAULT 20);
CREATE TABLE IF NOT EXISTS doses(patient_id TEXT, dose_id INTEGER, hh INTEGER, mm INTEGER, days INTEGER, end_day INTEGER, label TEXT, created REAL,
  PRIMARY KEY(patient_id, dose_id));
CREATE TABLE IF NOT EXISTS occ(patient_id TEXT, dose_id INTEGER, due REAL, hh INTEGER, mm INTEGER, label TEXT, acked REAL,
  sent INTEGER DEFAULT 0, last_alert REAL DEFAULT 0, PRIMARY KEY(patient_id, dose_id, due));
CREATE TABLE IF NOT EXISTS caregivers(id TEXT PRIMARY KEY, patient_id TEXT, token TEXT UNIQUE, topic TEXT, lang TEXT, created REAL);
CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY, patient_id TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT, prio INTEGER, tags TEXT, body TEXT, tries INTEGER DEFAULT 0, next_try REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(patient_id TEXT, dose_id INTEGER, kind TEXT, ts REAL);
""")
db.commit()

M = {
 "en": {"late": "{name}: no response to the medicine alarm ({what}) for {m} min. Please contact them.",
        "answered": "{name} has now responded to the alarm ({what}).",
        "offline": "{name}: the phone has not connected for {m} min (last battery {b}). It may be off or out of battery. Next dose: {nxt}.",
        "back": "{name}: the phone is back online.", "joined": "Connected to {name}. You will get alerts here."},
 "de": {"late": "{name}: reagiert seit {m} Min. nicht auf den Medikamenten-Alarm ({what}). Bitte kontaktieren.",
        "answered": "{name} hat jetzt auf den Alarm reagiert ({what}).",
        "offline": "{name}: Das Handy ist seit {m} Min. nicht verbunden (letzter Akku {b}). Es ist evtl. aus oder leer. Nächste Einnahme: {nxt}.",
        "back": "{name}: Das Handy ist wieder online.", "joined": "Mit {name} verbunden. Warnungen erscheinen hier."},
 "ar": {"late": "{name}: لا يجيب على المنبّه ({what}) منذ {m} دقيقة. يرجى التواصل معه.",
        "answered": "{name} استجاب الآن للمنبّه ({what}).",
        "offline": "{name}: الهاتف غير متصل منذ {m} دقيقة (آخر بطارية {b}). قد يكون مغلقاً أو نفدت بطاريته. الجرعة القادمة: {nxt}.",
        "back": "{name}: عاد الهاتف للاتصال.", "joined": "تم الربط مع {name}. ستصلك التنبيهات هنا."},
}

class ApiError(Exception):
    def __init__(self, code, msg): self.code, self.msg = code, msg

def h(t): return hashlib.sha256(t.encode()).hexdigest()
def run(sql, *a):
    with LOCK:
        cur = db.execute(sql, a); db.commit(); return cur
def rows(sql, *a):
    with LOCK: return db.execute(sql, a).fetchall()
def one(sql, *a):
    r = rows(sql, *a); return r[0] if r else None

def send_push(topic, prio, tags, body):
    req = urllib.request.Request(f"{NTFY}/{topic}", data=body.encode("utf-8"), method="POST",
        headers={"Priority": str(prio), "Title": "Medicine Alarm", "Tags": tags})
    if NTFY_TOKEN: req.add_header("Authorization", "Bearer " + NTFY_TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=10) as r: return 200 <= r.status < 300
    except Exception: return False

def enqueue(p, kind, prio, tags, **fmt):
    for c in rows("SELECT * FROM caregivers WHERE patient_id=?", p["id"]):
        lang = c["lang"] if c["lang"] in M else (p["lang"] if p["lang"] in M else "en")
        run("INSERT INTO outbox(topic,prio,tags,body) VALUES(?,?,?,?)", c["topic"], prio, tags, M[lang][kind].format(name=p["name"], **fmt))

def flush_outbox(now):
    for r in rows("SELECT * FROM outbox WHERE next_try<=? AND tries<8 ORDER BY id LIMIT 50", now):
        if send_push(r["topic"], r["prio"], r["tags"], r["body"]): run("DELETE FROM outbox WHERE id=?", r["id"])
        else: run("UPDATE outbox SET tries=tries+1,next_try=? WHERE id=?", now + min(300, 30 * 2 ** r["tries"]), r["id"])

def due_times(p, d, now):
    tz = ZoneInfo(p["tz"]); nl = datetime.fromtimestamp(now, tz); out = []
    for back in (0, 1):
        day = (nl - timedelta(days=back)).date()
        if d["end_day"] >= 0 and day.toordinal() - 719163 > d["end_day"]: continue
        if not (d["days"] >> day.weekday()) & 1: continue
        due = datetime(day.year, day.month, day.day, d["hh"], d["mm"], tzinfo=tz).timestamp()
        if d["created"] <= due <= now and now - due < 6 * 3600: out.append(due)
    return out

def next_due(p, now):
    tz = ZoneInfo(p["tz"]); nl = datetime.fromtimestamp(now, tz); best = None
    for d in rows("SELECT * FROM doses WHERE patient_id=?", p["id"]):
        for i in range(9):
            day = (nl + timedelta(days=i)).date()
            if d["end_day"] >= 0 and day.toordinal() - 719163 > d["end_day"]: break
            if not (d["days"] >> day.weekday()) & 1: continue
            t = datetime(day.year, day.month, day.day, d["hh"], d["mm"], tzinfo=tz).timestamp()
            if t > now: best = t if best is None else min(best, t); break
    return best

def what(o): return ((o["label"] + " ") if o["label"] else "") + "%02d:%02d" % (o["hh"], o["mm"])

def tick(now=None):
    now = now or time.time()
    for p in rows("SELECT * FROM patients"):
        for d in rows("SELECT * FROM doses WHERE patient_id=?", p["id"]):
            for due in due_times(p, d, now):
                run("INSERT OR IGNORE INTO occ(patient_id,dose_id,due,hh,mm,label) VALUES(?,?,?,?,?,?)", p["id"], d["dose_id"], due, d["hh"], d["mm"], d["label"] or "")
        pending = rows("SELECT * FROM occ WHERE patient_id=? AND acked IS NULL AND due<=? AND ?-due<?", p["id"], now, now, 6 * 3600)
        for o in pending:
            age = (now - o["due"]) / 60; n = o["sent"]
            go = (n == 0 and age >= p["first_min"]) or (n == 1 and age >= p["urgent_min"]) or \
                 (2 <= n < 2 + p["max_repeats"] and now - o["last_alert"] >= p["repeat_min"] * 60)
            if go:
                enqueue(p, "late", 4 if n == 0 else 5, "warning" if n == 0 else "rotating_light,warning", what=what(o), m=int(age))
                run("UPDATE occ SET sent=sent+1,last_alert=? WHERE patient_id=? AND dose_id=? AND due=?", now, p["id"], o["dose_id"], o["due"])
        nd = next_due(p, now)
        offline = p["last_seen"] and now - p["last_seen"] > p["offline_min"] * 60
        if offline and not p["offline_alerted"] and (pending or (nd and nd - now < 120 * 60)):
            b = "%d%%" % p["battery"] if p["battery"] is not None else "?"
            nxt = datetime.fromtimestamp(nd, ZoneInfo(p["tz"])).strftime("%H:%M") if nd else "-"
            enqueue(p, "offline", 5, "battery,rotating_light", m=int((now - p["last_seen"]) / 60), b=b, nxt=nxt)
            run("UPDATE patients SET offline_alerted=1 WHERE id=?", p["id"])
    flush_outbox(now)

def loop():
    while True:
        try: tick()
        except Exception as e: print("tick error", e, flush=True)
        time.sleep(TICK)

RATE = {}
def limited(ip, key, mx, per):
    t = time.time(); k = (ip, key); RATE[k] = [x for x in RATE.get(k, []) if t - x < per]
    if len(RATE[k]) >= mx: return True
    RATE[k].append(t); return False

def num(v, lo, hi, name):
    try: v = float(v)
    except Exception: raise ApiError(400, name)
    if not lo <= v <= hi: raise ApiError(400, name)
    return v

def route(method, path, body, headers, ip):
    now = time.time()
    if path == "/healthz": return 200, {"ok": True}
    a = headers.get("Authorization", "")
    kind = who = None
    if a.startswith("Bearer "):
        t = h(a[7:])
        who = one("SELECT * FROM patients WHERE token=?", t); kind = "patient" if who else None
        if not who: who = one("SELECT * FROM caregivers WHERE token=?", t); kind = "caregiver" if who else None
    def need(k):
        if kind != k: raise ApiError(401, "unauthorized")

    if method == "POST" and path == "/v1/register":
        if limited(ip, "reg", 20, 3600): raise ApiError(429, "slow down")
        name = str(body.get("name", "")).strip()[:60] or "?"
        tz = str(body.get("tz", "UTC"))
        try: ZoneInfo(tz)
        except Exception: raise ApiError(400, "tz")
        lang = body.get("lang", "en"); lang = lang if lang in M else "en"
        pid, tok = secrets.token_hex(8), secrets.token_urlsafe(32)
        run("INSERT INTO patients(id,name,token,tz,lang,created,last_seen) VALUES(?,?,?,?,?,?,?)", pid, name, h(tok), tz, lang, now, now)
        return 200, {"patient_id": pid, "token": tok}

    if method == "PUT" and path == "/v1/schedule":
        need("patient"); p = who
        ds = body.get("doses", [])
        if not isinstance(ds, list) or len(ds) > 100: raise ApiError(400, "doses")
        if body.get("tz"):
            try: ZoneInfo(body["tz"]); run("UPDATE patients SET tz=? WHERE id=?", body["tz"], p["id"])
            except Exception: raise ApiError(400, "tz")
        ids = []
        for d in ds:
            try: i, hh, mm, days, end = int(d["id"]), int(d["hh"]), int(d["mm"]), int(d["days"]), int(d.get("end_day", -1))
            except Exception: raise ApiError(400, "dose")
            if not (0 <= hh < 24 and 0 <= mm < 60 and 0 <= days < 128): raise ApiError(400, "dose")
            label = str(d.get("label", ""))[:60]; ids.append(i)
            if one("SELECT 1 FROM doses WHERE patient_id=? AND dose_id=?", p["id"], i):
                run("UPDATE doses SET hh=?,mm=?,days=?,end_day=?,label=? WHERE patient_id=? AND dose_id=?", hh, mm, days, end, label, p["id"], i)
            else:
                run("INSERT INTO doses VALUES(?,?,?,?,?,?,?,?)", p["id"], i, hh, mm, days, end, label, now)
        for r in rows("SELECT dose_id FROM doses WHERE patient_id=?", p["id"]):
            if r["dose_id"] not in ids:
                run("DELETE FROM doses WHERE patient_id=? AND dose_id=?", p["id"], r["dose_id"])
                run("DELETE FROM occ WHERE patient_id=? AND dose_id=?", p["id"], r["dose_id"])
        return 200, {"ok": True}

    if method == "PUT" and path == "/v1/settings":
        need("patient"); p = who
        for k, lo, hi in (("first_min", 0, 240), ("urgent_min", 0, 240), ("repeat_min", 0.05, 240), ("offline_min", 1, 1440)):
            if k in body: run(f"UPDATE patients SET {k}=? WHERE id=?", num(body[k], lo, hi, k), p["id"])
        if "max_repeats" in body: run("UPDATE patients SET max_repeats=? WHERE id=?", int(num(body["max_repeats"], 0, 50, "max_repeats")), p["id"])
        return 200, {"ok": True}

    if method == "POST" and path == "/v1/invite":
        need("patient"); p = who
        run("DELETE FROM invites WHERE patient_id=? OR expires<?", p["id"], now)
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        run("INSERT INTO invites VALUES(?,?,?)", code, p["id"], now + 86400)
        return 200, {"code": code, "expires_in": 86400}

    if method == "POST" and path == "/v1/join":
        if limited(ip, "join", 10, 3600): raise ApiError(429, "slow down")
        code = str(body.get("code", "")).strip().upper().replace("-", "")
        inv = one("SELECT * FROM invites WHERE code=? AND expires>?", code, now)
        if not inv: raise ApiError(404, "invalid code")
        p = one("SELECT * FROM patients WHERE id=?", inv["patient_id"])
        lang = body.get("lang", p["lang"]); lang = lang if lang in M else p["lang"]
        cid, tok, topic = secrets.token_hex(6), secrets.token_urlsafe(32), "ma-" + secrets.token_hex(12)
        run("INSERT INTO caregivers VALUES(?,?,?,?,?,?)", cid, p["id"], h(tok), topic, lang, now)
        run("DELETE FROM invites WHERE code=?", code)
        run("INSERT INTO outbox(topic,prio,tags,body) VALUES(?,?,?,?)", topic, 3, "white_check_mark", M[lang]["joined"].format(name=p["name"]))
        return 200, {"token": tok, "topic": topic, "ntfy_server": NTFY, "patient_name": p["name"]}

    if method == "POST" and path == "/v1/event":
        need("patient"); p = who
        k = body.get("kind")
        if k not in ("ring", "ack", "snooze"): raise ApiError(400, "kind")
        i = int(body.get("dose_id", -1)); ts = float(body.get("ts", now))
        run("INSERT INTO events VALUES(?,?,?,?)", p["id"], i, k, ts)
        if k == "ack":
            o = one("SELECT * FROM occ WHERE patient_id=? AND dose_id=? AND acked IS NULL ORDER BY due DESC LIMIT 1", p["id"], i)
            if o:
                run("UPDATE occ SET acked=? WHERE patient_id=? AND dose_id=? AND due=?", ts, p["id"], i, o["due"])
                if o["sent"] > 0: enqueue(p, "answered", 3, "white_check_mark", what=what(o))
        return 200, {"ok": True}

    if method == "POST" and path == "/v1/heartbeat":
        need("patient"); p = who
        b = body.get("battery"); b = int(b) if b is not None else None
        run("UPDATE patients SET last_seen=?,battery=?,charging=?,offline_alerted=0 WHERE id=?", now, b, 1 if body.get("charging") else 0, p["id"])
        if p["offline_alerted"]: enqueue(p, "back", 3, "white_check_mark")
        return 200, {"ok": True, "now": now}

    if method == "GET" and path == "/v1/caregiver/status":
        need("caregiver"); c = who
        p = one("SELECT * FROM patients WHERE id=?", c["patient_id"])
        today = [{"hh": o["hh"], "mm": o["mm"], "label": o["label"], "due": o["due"], "acked": o["acked"] is not None}
                 for o in rows("SELECT * FROM occ WHERE patient_id=? AND due>? ORDER BY due", p["id"], now - 86400)]
        return 200, {"name": p["name"], "last_seen": p["last_seen"], "now": now, "battery": p["battery"], "charging": bool(p["charging"]),
                     "offline": bool(p["last_seen"] and now - p["last_seen"] > p["offline_min"] * 60), "today": today, "next_due": next_due(p, now)}

    if method == "GET" and path == "/v1/caregivers":
        need("patient")
        return 200, {"caregivers": [{"id": c["id"], "created": c["created"]} for c in rows("SELECT * FROM caregivers WHERE patient_id=?", who["id"])]}
    if method == "DELETE" and path.startswith("/v1/caregivers/"):
        need("patient"); run("DELETE FROM caregivers WHERE id=? AND patient_id=?", path.rsplit("/", 1)[1], who["id"])
        return 200, {"ok": True}
    raise ApiError(404, "not found")

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def go(self, method):
        ip = self.client_address[0]
        if TRUST_PROXY and self.headers.get("X-Forwarded-For"): ip = self.headers["X-Forwarded-For"].split(",")[0].strip()
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n > 65536: raise ApiError(413, "too large")
            body = json.loads(self.rfile.read(n)) if n else {}
            code, obj = route(method, self.path.split("?")[0], body, self.headers, ip)
        except ApiError as e: code, obj = e.code, {"error": e.msg}
        except (ValueError, KeyError, TypeError): code, obj = 400, {"error": "bad request"}
        except Exception as e: print("error", e, flush=True); code, obj = 500, {"error": "server"}
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    do_GET = lambda s: s.go("GET"); do_POST = lambda s: s.go("POST"); do_PUT = lambda s: s.go("PUT"); do_DELETE = lambda s: s.go("DELETE")

if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    print(f"listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
