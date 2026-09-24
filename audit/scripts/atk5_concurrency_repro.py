#!/usr/bin/env python3
"""
ATK5-06 repro: concurrency / transactions / lost updates.

 A) /api/repertoire/train/attempt  -> read-modify-write on times_practiced
    (SELECT prev_tp -> UPDATE times_practiced = prev_tp+1). N parallel correct
    attempts must yield times_practiced == N; a lost update yields < N.
 B) /api/train/evaluate_move      -> read-modify-write on drill_reviews.attempts
 C) /api/scout/favorite           -> read-modify-write toggle (non-idempotent,
    last-writer-wins + a request can be duplicated)
 D) parallel WRITERS on the same SQLite file -> "database is locked" 500s?
 E) idempotency: replaying the same POST N times

Idempotent, isolated temp DB, network stubbed, no engine used (exact-best path).
"""
import os
import sys
import threading
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # MUST be first backend-adjacent import

import requests


class _R:
    status_code = 503
    text = "stub"

    def json(self):
        return {}

    def iter_lines(self, decode_unicode=False):
        return iter(())


requests.get = lambda *a, **k: _R()
requests.post = lambda *a, **k: _R()
import yt_dlp


class _Y:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, *a, **k):
        return None


yt_dlp.YoutubeDL = _Y

from backend.api import app  # noqa: E402
from backend.db import get_db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
START_N = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"   # normalize_fen() form
clients = [TestClient(app, raise_server_exceptions=False) for _ in range(24)]
cc = 0


def parallel(fn, n):
    global cc
    out = [None] * n
    errs = [None] * n

    def run(i):
        c = clients[i % len(clients)]
        try:
            out[i] = fn(c, i)
        except Exception as e:
            errs[i] = f"{type(e).__name__}: {str(e)[:120]}"

    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    t0 = time.time()
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out, errs, time.time() - t0


def seed():
    c = clients[0]
    c.post("/api/repertoire/move", json={
        "repertoire": "white", "parent_fen": START, "move_uci": "e2e4",
        "is_user_move": True, "popularity_pct": 50.0, "win_rate": 50.0,
    })


def read_row():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, move_uci, times_practiced, times_correct, interval_days, next_review "
                "FROM repertoire_moves WHERE parent_fen = ? AND move_uci = 'e2e4'", (START_N,))
    r = cur.fetchone()
    conn.close()
    return dict(r) if r else None


print("=" * 78)
print("A) /api/repertoire/train/attempt  x20 PARALLEL correct answers")
print("=" * 78)
seed()
before = read_row()
print("  before:", before)


def attempt(c, i):
    r = c.post("/api/repertoire/train/attempt", json={
        "repertoire": "white", "current_fen": START, "attempted_uci": "e2e4"})
    return (r.status_code, (r.json().get("times_practiced") if r.status_code == 200 else r.text[:60]))


out, errs, dt = parallel(attempt, 20)
after = read_row()
print(f"  statuses: {Counter(s for s, _ in out)}   errors: {[e for e in errs if e]}")
print("  reported times_practiced values:", sorted({v for _, v in out if isinstance(v, int)}))
print("  after  :", after)
print(f"  >>> 20 requests issued, times_practiced = {after['times_practiced']} "
      f"(expected 20) -> {'LOST UPDATES: ' + str(20 - after['times_practiced']) + ' lost' if after['times_practiced'] != 20 else 'ok'}")

print()
print("=" * 78)
print("B) /api/train/evaluate_move  x20 PARALLEL on one blunder")
print("=" * 78)
conn = get_db()
cur = conn.cursor()
cur.execute("SELECT id, best_move FROM bluders_tmp" if False else
            "SELECT id, best_move, fen_before FROM blunders WHERE best_move IS NOT NULL AND best_move != '' LIMIT 1")
row = cur.fetchone()
conn.close()
if not row:
    print("  no blunder rows in the (temp) DB -- skipping")
else:
    bid, best_move, _fen = row["id"], row["best_move"], row["fen_before"]

    def ev(c, i):
        r = c.post("/api/train/evaluate_move", json={"blunder_id": bid, "attempted_uci": best_move})
        return (r.status_code, (r.json().get("attempts") if r.status_code == 200 else r.text[:60]))

    def dr_att():
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT attempts, successes FROM drill_reviews WHERE blunder_id = ?", (bid,))
        r = cur.fetchone()
        conn.close()
        return dict(r) if r else None

    print("  before drill_reviews:", dr_att())
    out, errs, dt = parallel(ev, 20)
    print(f"  statuses: {Counter(s for s, _ in out)}  errors: {[e for e in errs if e]}")
    print("  reported attempts values:", sorted({v for _, v in out if isinstance(v, int)}))
    print("  after  drill_reviews:", dr_att())

print()
print("=" * 78)
print("C) /api/scout/favorite x10 PARALLEL toggle (non-idempotent read-modify-write)")
print("=" * 78)
c = clients[0]
conn = get_db()
conn.execute("INSERT OR REPLACE INTO scouted_content (source_id, source_type, title, scouted_at, is_favorite) "
             "VALUES ('atk5fav','youtube','x',0,0)")
conn.commit()
conn.close()


def fav(c, i):
    r = c.post("/api/scout/favorite", json={"source_id": "atk5fav"})
    return (r.status_code, r.json().get("is_favorite") if r.status_code == 200 else r.text[:40])


out, errs, dt = parallel(fav, 10)
conn = get_db()
cur = conn.cursor()
cur.execute("SELECT is_favorite FROM scouted_content WHERE source_id='atk5fav'")
final = cur.fetchone()["is_favorite"]
conn.close()
print("  reported:", [v for _, v in out])
print("  final is_favorite in DB:", final, "(10 toggles from 0 should end at 0)")

print()
print("=" * 78)
print("D) 30 PARALLEL WRITERS across different endpoints -> 'database is locked'?")
print("=" * 78)


def writer(c, i):
    if i % 3 == 0:
        r = c.post("/api/settings", json={"gap_threshold": 5.0 + (i % 10)})
    elif i % 3 == 1:
        r = c.post("/api/repertoire/move", json={
            "repertoire": "black", "parent_fen": START, "move_uci": "e7e5", "is_user_move": True})
    else:
        r = c.post("/api/scout/favorite", json={"source_id": "atk5fav"})
    return (r.status_code, r.text[:120])


out, errs, dt = parallel(writer, 30)
print(f"  statuses: {Counter(s for s, _ in out)}  elapsed {dt:.2f}s  errors: {[e for e in errs if e]}")
bad = [(s, t) for s, t in out if s >= 400]
for s, t in bad[:10]:
    print(f"   {s}: {t}")

print()
print("=" * 78)
print("E) IDEMPOTENCY: same POST replayed 5x sequentially")
print("=" * 78)
c = clients[0]
r0 = read_row()
for i in range(5):
    c.post("/api/repertoire/train/attempt", json={
        "repertoire": "white", "current_fen": START, "attempted_uci": "e2e4"})
r5 = read_row()
print(f"  before={r0['times_practiced']}  after 5 identical POSTs={r5['times_practiced']} "
      f"(NOT idempotent: a retried request double-counts)")

print()
print("DONE")
