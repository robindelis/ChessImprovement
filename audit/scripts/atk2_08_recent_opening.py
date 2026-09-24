"""TRAINER probe 08 -- /api/repertoire/train/recent_opening stored state:
type confusion (500), unbounded length, no validation, cross-colour poisoning.

Run: /home/robin/miniconda3/bin/python3 audit/scripts/atk2_08_recent_opening.py
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk2_fixture import *  # noqa

c = fresh()
add_line(c, "white", "Alpha", ["e4", "e5"])
add_line(c, "black", "Beta", ["e4", "c5", "d4"])

hdr("A) type confusion -- non-string `name`")
for payload in ({"name": 123}, {"name": ["a"]}, {"name": {"a": 1}}, {"name": True},
                {"name": 0}, {"name": None}, {"opening_name": 3.5}):
    r = c.post("/api/repertoire/train/recent_opening", json=payload)
    print("   %-24s -> HTTP %s %s" % (json.dumps(payload), r.status_code, r.text[:70]))

hdr("B) unbounded length / no validation against real studies")
huge = "X" * 200000
r = c.post("/api/repertoire/train/recent_opening", json={"name": huge, "eco": "E" * 5000})
print("   200k-char name -> HTTP %s, stored bytes=%s"
      % (r.status_code, len(c.get("/api/repertoire/train/openings?color=white").json()["recent_opening"]["name"])))
r = c.post("/api/repertoire/train/recent_opening", json={"name": "Totally Made Up Opening", "eco": "Z99"})
print("   non-existent opening -> HTTP %s %s" % (r.status_code, r.json()))

hdr("C) cross-colour poisoning: set a BLACK study while drilling WHITE")
c.post("/api/repertoire/train/recent_opening", json={"name": "Beta", "eco": "B12"})
print("   white openings ->", json.dumps(c.get("/api/repertoire/train/openings?color=white").json()["recent_opening"]))
print("   black openings ->", json.dumps(c.get("/api/repertoire/train/openings?color=black").json()["recent_opening"]))
d = c.get("/api/repertoire/train/next?color=white").json()
print("   drill study_name=%r  (frontend will pin the filter to it)" % d.get("study_name"))

hdr("D) the trainer's 'most recent' filter is now pinned to a study the user never trained")
c.get("/api/repertoire/train/next?color=white&opening=Beta")
print("   -> HTTP 404:", c.get("/api/repertoire/train/next?color=white&opening=Beta").status_code)
print("      (frontend alert() + clearOpeningFocus() fallback)")

hdr("E) the setting survives independently of any study / is never cleared")
conn = get_db()
print("   settings row:", dict(conn.execute(
    "SELECT * FROM settings WHERE key='recent_learned_opening'").fetchone()))
conn.close()

hdr("F) eco field is never validated / never used for filtering")
c.post("/api/repertoire/train/recent_opening", json={"name": "Alpha", "eco": "<script>alert(1)</script>"})
o = c.get("/api/repertoire/train/openings?color=white").json()
print("   stored:", json.dumps(o["recent_opening"]))
print("   openings list eco fields:", [x["eco"] for x in o["openings"]])

hdr("G) openings endpoint with a nonsense colour")
for col in ("purple", "WHITE", "", "white%27"):
    r = c.get("/api/repertoire/train/openings", params={"color": col} if col else {})
    j = r.json()
    print("   color=%-9r -> HTTP %s openings=%s" % (col, r.status_code, [x["opening_name"] for x in j["openings"]]))
