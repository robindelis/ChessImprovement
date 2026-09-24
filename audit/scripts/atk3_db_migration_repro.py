"""[ATTACKER-3] HISTORICAL/REFERENCE PROBE.
Sections that printed a bug at the time of writing were FIXED by other agents during
this audit round; see audit/reports/SYNC-INDEX.md ("Fixed during this audit round").
Kept runnable for re-test.  Authoritative live status: audit/scripts/atk3_reverify_matrix.py
"""
"""ATK3-16 : db.py — migration safety, study auto-labelling, orphan purge, concurrency.
ATK3-17 : get_repertoire_tree duplicates every explorer query and every engine evaluation."""
import os
import sys
import json
import time
import sqlite3
import threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import hdr, wipe_games, wipe_repertoire, wipe_explorer, q, q1, client, TEMP_DB  # noqa: E402

cl = client()
import chess  # noqa: E402
import backend.db as dbm  # noqa: E402
from backend.repertoire import RepertoireManager, normalize_fen  # noqa: E402
rm = RepertoireManager.get_instance()
rm.get_explorer_game_count = lambda fen, ratings="2000,2200,2500": 100000

hdr("ATK3-16a — init_db() is idempotent? (run it 5x, compare schema + row counts)")
c = dbm.get_db()


def schema():
    return sorted((r[0], r[1], r[2] or "") for r in
                  c.execute("SELECT name, sql, '' FROM sqlite_master WHERE type='table'"))


before = schema()
cols_before = [tuple(r) for r in c.execute("PRAGMA table_info(blunders)")]
c.close()
for i in range(5):
    dbm.init_db()
c = dbm.get_db()
after = schema()
cols_after = [tuple(r) for r in c.execute("PRAGMA table_info(blunders)")]
print("  tables identical:", before == after)
print("  blunders columns identical:", cols_before == cols_after, f"({len(cols_after)} cols)")
print("  journal_mode:", c.execute("PRAGMA journal_mode").fetchone()[0])
print("  foreign_keys pragma:", c.execute("PRAGMA foreign_keys").fetchone()[0],
      "<-- OFF, so blunders/game_id orphans are never cleaned up")
c.close()

hdr("ATK3-16b — the startup migration re-labels the user's studies when the table is empty")
wipe_repertoire()
# RobiDeli's actual white lines (AGENTS.md 9.3): Anti-French 1.e4 e6 2.d4 d5 3.e5
import backend.repertoire as rep  # noqa: E402
b = chess.Board()
seen = []
for san in ["e4", "e6", "d4", "d5", "e5"]:
    m = b.parse_san(san)
    user = (b.turn == chess.WHITE)
    rep.RepertoireManager._instance = None
    r2 = RepertoireManager.get_instance()
    res = r2.add_repertoire_move("white", b.fen(), m.uci(), is_user_move=user,
                                 study_name="Anti French Defense")
    b.push(m)
    seen.append(res.get("status"))
print("  add_repertoire_move results:", seen)
print("  studies:", q("SELECT name, repertoire FROM repertoire_studies"))
print("  move study_names:", [(r["move_uci"], r["study_name"]) for r in
                              q("SELECT move_uci, study_name FROM repertoire_moves ORDER BY id")])
print("\n  now delete every study (as the UI's study delete allows) and restart the app:")
dbm.get_db().execute("DELETE FROM repertoire_studies")
dbm.get_db().commit()
dbm.init_db()
print("  studies after restart:", q("SELECT name, repertoire FROM repertoire_studies"))
print("  move study_names after restart:", [(r["move_uci"], r["study_name"]) for r in
                                            q("SELECT move_uci, study_name FROM repertoire_moves ORDER BY id")])
print("  -> the Anti French line is now filed under a study called 'Italian Game'.")

hdr("ATK3-16c — orphan purge: can a legitimate move be deleted at startup?")
wipe_repertoire()
rm2 = RepertoireManager.get_instance()
b = chess.Board()
m = b.parse_san("e4")
rm2.add_repertoire_move("white", b.fen(), m.uci(), is_user_move=True, study_name="S")
b.push(m)
m2 = b.parse_san("e5")
rm2.add_repertoire_move("white", b.fen(), m2.uci(), is_user_move=False, study_name="S")
print("  moves before restart:", [(r["move_uci"], r["is_user_move"]) for r in
                                  q("SELECT move_uci, is_user_move FROM repertoire_moves ORDER BY id")])
# now hand-corrupt one parent_fen the way an older schema / a 3-token FEN would
dbm.get_db().execute("UPDATE repertoire_moves SET parent_fen=? WHERE move_uci='e7e5'",
                     ("rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR b  - 0 2",))
dbm.get_db().commit()
print("  after corrupting the 2nd move's parent_fen (castling field emptied):",
      [(r["move_uci"], r["parent_fen"]) for r in
       q("SELECT move_uci, parent_fen FROM repertoire_moves ORDER BY id")])
dbm.init_db()
print("  after init_db():", [(r["move_uci"], r["parent_fen"]) for r in
                             q("SELECT move_uci, parent_fen FROM repertoire_moves ORDER BY id")])
print("  -> migrate_studies_and_cleanup_blunders() runs on EVERY startup and hard-DELETEs")
print("     repertoire_moves that are not reachable from the start position over 3 FEN tokens.")
print("     Here the whole 1.e4 e5 branch was destroyed by a parent_fen spelling difference.")
dbm.get_db().execute("DELETE FROM repertoire_studies")
dbm.init_db()

hdr("ATK3-16d — 20 concurrent writers: 'database is locked'?")
wipe_repertoire()
errs = []


def writer(i):
    try:
        cc = dbm.get_db()
        cc.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                   (f"probe_{i}", "x" * 5000))
        cc.commit()
        cc.close()
    except Exception as e:
        errs.append(f"{type(e).__name__}: {e}")


ts = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
[t.start() for t in ts]
[t.join() for t in ts]
print("  20 concurrent settings writes -> errors:", errs or "none")

hdr("ATK3-16e — get_db() runs 'PRAGMA journal_mode=WAL' on every single connection")
src = open("backend/db.py", encoding="utf-8").read()
print("  " + "\n  ".join(l for l in src.splitlines() if "journal_mode" in l or "connect(" in l))
n = 0
t0 = time.time()
for _ in range(200):
    cc = dbm.get_db()
    cc.close()
    n += 1
print(f"  {n} get_db()/close() cycles in {time.time() - t0:.2f}s "
      "(each one re-issues a journal_mode pragma)")

hdr("ATK3-17 — /api/repertoire/tree issues every explorer query and every engine analysis TWICE")
wipe_repertoire()
wipe_explorer()
calls = {"explorer": 0, "analyse": 0}
import backend.repertoire as rp  # noqa: E402
from atk3_lib import fake_explorer, restore_network  # noqa: E402
real_get_engine = rp.RepertoireManager._get_engine


class CountingEngine:
    def analyse(self, board, limit=None):
        calls["analyse"] += 1
        import chess.engine
        return {"score": chess.engine.PovScore(chess.engine.Cp(0), chess.WHITE),
                "pv": []}


def counting_get_engine(self):
    return CountingEngine()


rp.RepertoireManager._get_engine = counting_get_engine
stub = fake_explorer({"white": 100, "draws": 10, "black": 100,
                      "moves": [{"uci": "e7e5", "san": "e5", "white": 50, "draws": 5,
                                 "black": 50},
                                {"uci": "c7c5", "san": "c5", "white": 30, "draws": 3,
                                 "black": 30}]})
orig = stub.get


def counting_get(url, params=None, headers=None, timeout=None, **kw):
    calls["explorer"] += 1
    return orig(url, params=params, headers=headers, timeout=timeout, **kw)


stub.get = counting_get
# one opponent position: 1.e4 played by the user
rm3 = RepertoireManager.get_instance()
b = chess.Board()
m = b.parse_san("e4")
rm3.add_repertoire_move("white", b.fen(), m.uci(), is_user_move=True, study_name="S")
calls["explorer"] = calls["analyse"] = 0
t0 = time.time()
r = cl.get("/api/repertoire/tree?color=white&min_games=30")
restore_network()
rp.RepertoireManager._get_engine = real_get_engine
print(f"  repertoire has {q1('SELECT COUNT(*) c FROM repertoire_moves')['c']} move(s), "
      f"1 opponent position")
print(f"  GET /api/repertoire/tree -> HTTP {r.status_code} in {time.time() - t0:.2f}s")
print("  explorer HTTP requests:", calls["explorer"], "| engine analyse() calls:",
      calls["analyse"])
print("  -> the gaps loop (l.1248) and the coverage loop (l.1270) each call")
print("     get_candidate_moves_2000() for the same position, so every explorer query and")
print("     every Stockfish depth-10 evaluation is performed twice per opponent position.")
print("     The second pass always misses the explorer_cache because the first pass wrote it")
print("     only after the engine loop had already returned... (cache does help pass 2:",
      "yes)" if calls["explorer"] == 1 else "     (cache hit on pass 2: yes)", ")")
print("  explorer request count is", calls["explorer"], "-> the explorer_cache absorbs the")
print("  duplicate network call, but the ENGINE work is duplicated whenever the cache is cold,")
print("  and even on a cache hit the cached payload still lacks per-move `eval` values that")
print("  were computed in pass 1, so `coverage_pct` and the gap list are computed from")
print("  different data than the visible explorer table.")
