"""
CODE-05 repro #4: /api/repertoire/seed contract mismatch.

1. seed_default_repertoires() has NO return statement on the success path
   (backend/repertoire.py:1865-1911) -> returns None -> endpoint serialises JSON `null`.
2. frontend/repertoire_builder.js:542-547 does `res.json()` then `data.status === 'ok'`
   -> TypeError on null, and 'ok' is never a value the backend produces.

Safe: isolated temp DB only.
Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-05_repro_seed_contract.py
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

import tests.test_setup  # noqa: E402
import chess  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)
rm = RepertoireManager.get_instance()

# --- Seed a user move so the guard is NOT what we're testing -------------------
b = chess.Board()
client.post("/api/repertoire/move", json={"repertoire": "white", "parent_fen": b.fen(),
                                         "move_uci": "e2e4", "is_user_move": True})
conn = client.app  # noqa
import sqlite3, os as _os
c = sqlite3.connect(_os.environ["CHESS_DB_PATH"])
print("user moves before:", c.execute("select count(*) from repertoire_moves").fetchone()[0])
c.close()

print("\n--- A) guard path: no force, repertoire non-empty ---")
r = client.post("/api/repertoire/seed")
print("   HTTP", r.status_code, "body:", r.text)

print("\n--- B) SUCCESS path: force=true on a cleared repertoire ---")
c = sqlite3.connect(_os.environ["CHESS_DB_PATH"])
c.execute("DELETE FROM repertoire_moves"); c.commit(); c.close()
r2 = client.post("/api/repertoire/seed?force=true")
print("   HTTP", r2.status_code, "body:", repr(r2.text))
print("   frontend check `data.status === 'ok'` would evaluate data ==", repr(r2.json()))

c = sqlite3.connect(_os.environ["CHESS_DB_PATH"])
print("   moves actually seeded:", c.execute("select count(*) from repertoire_moves").fetchone()[0])
c.close()

print("\n--- C) replay the exact frontend code path ---")
try:
    data = r2.json()
    if data.status == 'ok':          # repertoire_builder.js:544
        print("   frontend would alert success")
    else:
        print("   frontend silently does nothing (no alert, no error)")
except Exception as exc:
    print(f"   FRONTEND CRASHES: {type(exc).__name__}: {exc}")
    print("   (caught by repertoire_builder.js:548 catch -> console.error only)")

print("\n--- D) direct call returns None? ---")
c = sqlite3.connect(_os.environ["CHESS_DB_PATH"]); c.execute("DELETE FROM repertoire_moves"); c.commit(); c.close()
print("   seed_default_repertoires(force=True) ->", repr(rm.seed_default_repertoires(force=True)))
