"""
CODE-2 follow-up #3: verb/shadowing behaviour, ValueError handler leak,
drill_reviews dead-surface, and the double init_db() at startup.

Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-2_routes.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST

from fastapi.testclient import TestClient
from backend.api import app
import backend.api as api_mod

client = TestClient(app, raise_server_exceptions=False)


def hdr(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


hdr("1) Wrong HTTP verb on an existing API path")
for verb, path in [("GET", "/api/sync"), ("GET", "/api/repertoire/clear"),
                   ("GET", "/api/repertoire/move"), ("POST", "/api/blunders")]:
    r = client.request(verb, path)
    print("  %-4s %-28s -> %s %s" % (verb, path, r.status_code, r.text[:60]))

hdr("2) /api/blunders/{blunder_id} with a non-int -> shadowing / 422?")
r = client.get("/api/blunders/not-a-number")
print("  GET /api/blunders/not-a-number ->", r.status_code, r.text[:100])
r = client.get("/api/blunders/999999999")
print("  GET /api/blunders/999999999  ->", r.status_code, r.text[:100])

hdr("3) Global @app.exception_handler(ValueError) turns internal errors into 400")
# force an internal ValueError from deep inside a route
import backend.repertoire as rep_mod
orig = rep_mod.RepertoireManager.get_repertoire_tree
def boom(self, *a, **k):
    raise ValueError("no such column: totally_internal_secret_schema_detail")
rep_mod.RepertoireManager.get_repertoire_tree = boom
r = client.get("/api/repertoire/tree?color=white")
print("  /api/repertoire/tree ->", r.status_code, r.text[:200])
rep_mod.RepertoireManager.get_repertoire_tree = orig

hdr("4) /api/repertoire/sync_games (non-stream) vs ..._stream -- both live?")
print("  frontend uses: /api/repertoire/sync_games_stream only (repertoire_builder.js:666)")
r = client.post("/api/repertoire/sync_games?max_games=1")
print("  POST /api/repertoire/sync_games ->", r.status_code, r.text[:150])

hdr("5) drill_reviews write on every blunder even though every read path is orphaned")
import inspect, backend.db as db
print("  analyzer inserts a drill_reviews row:",
      "INSERT OR IGNORE INTO drill_reviews" in inspect.getsource(
          __import__("backend.analyzer", fromlist=["x"]).GameAnalyzer.save_blunders_to_db))
print("  readers:", [l.strip() for l in inspect.getsource(api_mod).splitlines()
                     if "drill_reviews" in l])

hdr("6) start.py runs init_db() and so does @app.on_event('startup')")
print("  start.py:19  init_db()")
print("  api.py:74-76 @app.on_event('startup') def startup_event(): init_db()")
print("  -> migrate_studies_and_cleanup_blunders() (the DELETING migration) runs twice per boot")

print("\nDONE")
