"""
CODE-2 follow-up #5: prove that games.result == NULL is reported as a LOSS by
GET /api/games/recent (api.py:341-351), and that played_at unit mixing breaks
ORDER BY played_at DESC.

Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-2_results.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST

from fastapi.testclient import TestClient
from backend.db import get_db
from backend.api import app

client = TestClient(app, raise_server_exceptions=False)

now_ms = int(time.time() * 1000)   # what repertoire.py:1476 (sync_recent_games) writes
now_s = int(time.time())           # what api.py:252 (/api/sync) writes

conn = get_db()
conn.execute("DELETE FROM games WHERE id LIKE 'ztest%'")
conn.execute("INSERT INTO games (id,user_color,result,played_at,moves_san) VALUES ('ztest_draw','white',NULL,?,?)",
             (now_ms, "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7"))
conn.execute("INSERT INTO games (id,user_color,result,played_at,moves_san) VALUES ('ztest_draws','white','draw',?,?)",
             (now_ms + 1, "e4 e5 Nf3 Nc6 Bb5 a6"))
conn.execute("INSERT INTO games (id,user_color,result,played_at,moves_san) VALUES ('ztest_mixed','white','white',?,?)",
             (now_s, "d4 d5 c4"))   # newest game, but written in SECONDS
conn.commit()
conn.close()

r = client.get("/api/games/recent?limit=100")
print("GET /api/games/recent?limit=100 ->", r.status_code)
print("  (note: ?search= only matches player/opening/eco, never `id`)")
for g in [x for x in r.json()["games"] if str(x["id"]).startswith("ztest")]:
    print("  id=%-12s stored_result=%-6r user_color=%s  ->  api result=%-5r score=%s  played_at=%s (%s)"
          % (g["id"], "NULL", g["user_color"], g["result"], g["score_text"],
             g["played_at"], g["played_at_formatted"]))

print("\nOrder check: games listed newest-first by stored played_at")
conn = get_db()
for r2 in conn.execute("SELECT id, played_at FROM games WHERE id LIKE 'ztest%' ORDER BY played_at DESC"):
    print("   %-12s played_at=%s" % (r2["id"], r2["played_at"]))
conn.close()
print("  ^ the SECONDS-valued game sorts last although it is the newest by wall clock")
print("\nDONE")
