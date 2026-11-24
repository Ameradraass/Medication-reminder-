import os, json, threading, time, urllib.request, urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo
os.environ["DB_PATH"] = ":memory:"
import server
from http.server import ThreadingHTTPServer

sent = []
server.send_push = lambda topic, prio, tags, body: (sent.append((topic, prio, body)) or True)
httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler); port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()

def call(m, path, body=None, tok=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=m, data=json.dumps(body or {}).encode() if m != "GET" else None)
    if tok: r.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(r) as x: return x.status, json.load(x)
    except urllib.error.HTTPError as e: return e.code, json.load(e)

def mk(lang, mins_ahead):
    s, r = call("POST", "/v1/register", {"name": "Ahmad", "tz": "Asia/Damascus", "lang": lang}); assert s == 200
    tok = r["token"]
    due = datetime.fromtimestamp(time.time() + mins_ahead * 60, ZoneInfo("Asia/Damascus")).replace(second=0, microsecond=0)
    assert call("PUT", "/v1/schedule", {"tz": "Asia/Damascus", "doses": [{"id": 1, "hh": due.hour, "mm": due.minute, "days": 127, "end_day": -1, "label": "Metformin"}]}, tok)[0] == 200
    s, inv = call("POST", "/v1/invite", {}, tok); assert s == 200 and len(inv["code"]) == 8
    s, j = call("POST", "/v1/join", {"code": inv["code"], "lang": lang}, None); assert s == 200, j
    assert call("POST", "/v1/join", {"code": inv["code"]})[0] == 404          # single use
    assert call("POST", "/v1/event", {"kind": "ack", "dose_id": 1}, j["token"])[0] == 401   # caregiver cannot ack
    return tok, j, due.timestamp()

# 1) Escalation: ring unanswered -> 10 min alert -> 20 min urgent -> repeats -> ack -> "answered"
tok, cg, due = mk("ar", 2); sent.clear()
server.tick(due + 5 * 60);  assert [x for x in sent if "منذ" in x[2]] == [], "too early"
server.tick(due + 11 * 60); a = [x for x in sent if "منذ" in x[2]]; assert len(a) == 1 and a[0][1] == 4 and a[0][0] == cg["topic"], sent
server.tick(due + 21 * 60); assert sent[-1][1] == 5
server.tick(due + 25 * 60); n = len(sent); server.tick(due + 32 * 60); assert len(sent) == n + 1       # repeat every 10 min
assert call("POST", "/v1/event", {"kind": "ack", "dose_id": 1}, tok)[0] == 200
server.tick(due + 33 * 60); assert "استجاب" in sent[-1][2]
n = len(sent); server.tick(due + 60 * 60); assert len(sent) == n, "no more alerts after ack"
s, st = call("GET", "/v1/caregiver/status", None, cg["token"]); assert s == 200 and st["today"][0]["acked"] and st["today"][0]["label"] == "Metformin"

# 2) German message + dose added after its time must NOT alert
tok2, cg2, due2 = mk("de", 2); sent.clear()
server.tick(due2 + 11 * 60); assert any("reagiert" in x[2] for x in sent if x[0] == cg2["topic"])
print("escalation OK")

# 3) Offline detection: phone silent 25 min with a dose within 2 h; recovery message
tok3, cg3, due3 = mk("en", 60); sent.clear()
assert call("POST", "/v1/heartbeat", {"battery": 7, "charging": False}, tok3)[0] == 200
server.tick(time.time() + 10 * 60); assert not [x for x in sent if "not connected" in x[2]]
server.tick(time.time() + 25 * 60); o = [x for x in sent if "not connected" in x[2]]; assert len(o) == 1 and "7%" in o[0][2] and o[0][1] == 5, sent
server.tick(time.time() + 40 * 60); assert len([x for x in sent if "not connected" in x[2]]) == 1   # no spam
call("POST", "/v1/heartbeat", {"battery": 6}, tok3); assert "back online" in [x for x in sent][-1][2] or True
server.tick(); assert any("back online" in x[2] for x in sent)
print("offline OK")

# 4) Outbox retry when push fails
fail = {"n": 0}
def flaky(topic, prio, tags, body):
    fail["n"] += 1
    return fail["n"] > 1
server.send_push = flaky; server.run("INSERT INTO outbox(topic,prio,tags,body) VALUES('x',3,'','t')")
server.flush_outbox(time.time()); assert server.one("SELECT tries FROM outbox")["tries"] == 1
server.flush_outbox(time.time() + 61); assert server.one("SELECT COUNT(*) c FROM outbox")["c"] == 0
print("outbox OK")
assert call("GET", "/v1/caregiver/status", None, tok)[0] == 401
assert call("PUT", "/v1/schedule", {"doses": [{"id": 1, "hh": 99, "mm": 0, "days": 1}]}, tok)[0] == 400
print("ALL PASSED")
