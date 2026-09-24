"""[ATK3-RETEST] SYNC-01 + SYNC-15 schema fixes, and the holes the new
UNIQUE index / new `best_pv` migration leave behind.

Re-runnable and idempotent: every scenario builds its own throwaway database or
resets the shared isolated one before it runs.

    /home/robin/miniconda3/bin/python3 audit/scripts/atk3_re01_schema_retest.py
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import hdr, q, q1, get_db, wipe_games, restore_network, TEMP_DB  # noqa: E402

import backend.db as db_mod  # noqa: E402
from backend.analyzer import GameAnalyzer  # noqa: E402

RESULTS = []


def verdict(name, ok, detail=""):
    RESULTS.append((name, "HOLDS" if ok else "BROKEN", detail))
    print("   -> %-6s %s   %s" % ("HOLDS" if ok else "BROKEN", name, detail))


def fresh_db_file():
    fd, path = tempfile.mkstemp(prefix="chess_test_re01_", suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


# ---------------------------------------------------------------------------
hdr("RE01-A  SYNC-01: does a *fresh* init_db() create blunders.best_pv?")
# ---------------------------------------------------------------------------
path = fresh_db_file()
os.environ["CHESS_DB_PATH"] = path
db_mod.init_db()
c = sqlite3.connect(path)
cols = [r[1] for r in c.execute("PRAGMA table_info(blunders)")]
idx = [(r[1], r[2]) for r in c.execute("PRAGMA index_list(blunders)")]
c.close()
verdict("fresh init_db() creates blunders.best_pv", "best_pv" in cols,
        "columns=%d, %s" % (len(cols), cols[-4:]))
print("      indexes on blunders (name, unique-flag):", idx)
ux = [i for i in idx if i[0] == "ux_blunders_natural_key"]
verdict("ux_blunders_natural_key exists", bool(ux), str(ux))
verdict("ux_blunders_natural_key is UNIQUE", bool(ux) and ux[0][1] == 1, str(ux))

# Does a real analyzer write survive on that fresh schema?
os.environ["CHESS_DB_PATH"] = TEMP_DB  # put the shared DB back
_have_best_pv = "best_pv" in cols
wipe_games()
row = {
    "game_id": "re01A", "move_number": 2, "ply": 3, "color": "white",
    "fen_before": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
    "fen_after": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKB1R b KQkq - 0 2",
    "played_move": "g1f3", "played_san": "Nf3", "best_move": "f1c4", "best_san": "Bc4",
    "eval_before": 10, "eval_after": -270, "cpl_loss": 280,
    "opening_name": "Italian Game", "eco": "C50", "is_book_move": 0,
    "refutation_pv": "[]", "best_pv": "[]", "explanation": "x", "strategic_idea": "y",
    "category": "Blunder", "created_at": int(time.time()),
}
try:
    summary = GameAnalyzer().save_blunders_to_db([dict(row)])
    stored = q1("SELECT best_pv FROM blunders WHERE game_id='re01A'")
    verdict("save_blunders_to_db on a fresh DB", stored is not None,
            "summary=%s stored_best_pv=%r" % (summary, stored and stored["best_pv"]))
except Exception as exc:
    verdict("save_blunders_to_db on a fresh DB", False, "raised %r" % (exc,))

# The rest of this probe needs a usable schema. If SYNC-01 is open, add the
# column by hand (exactly what tests/test_analyzer_correctness.py does) so the
# REMAINING scenarios can be measured.  This is the probe working AROUND the
# bug, not fixing it.
if not _have_best_pv:
    print("\n  !! SYNC-01 is open on this tree -> adding blunders.best_pv BY HAND so the")
    print("     remaining scenarios in this probe can run.  The production app cannot.")
    _c = get_db()
    _c.execute("ALTER TABLE blunders ADD COLUMN best_pv TEXT")
    _c.commit()
    _c.close()
wipe_games()

# ---------------------------------------------------------------------------
hdr("RE01-B  SYNC-01: does a LEGACY database (no best_pv) gain the column on boot?")
# ---------------------------------------------------------------------------
path = fresh_db_file()
leg = sqlite3.connect(path)
leg.executescript("""
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE games (id TEXT PRIMARY KEY, speed TEXT, rated INTEGER, played_at INTEGER,
  white_player TEXT, black_player TEXT, white_rating INTEGER, black_rating INTEGER,
  user_color TEXT, result TEXT, opening_name TEXT, eco TEXT, moves_san TEXT,
  analyzed_at INTEGER DEFAULT 0);
CREATE TABLE blunders (id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT,
  move_number INTEGER, ply INTEGER, color TEXT, fen_before TEXT, fen_after TEXT,
  played_move TEXT, played_san TEXT, best_move TEXT, best_san TEXT, eval_before INTEGER,
  eval_after INTEGER, cpl_loss INTEGER, opening_name TEXT, eco TEXT,
  is_book_move INTEGER DEFAULT 0, refutation_pv TEXT, explanation TEXT,
  strategic_idea TEXT, category TEXT, created_at INTEGER,
  FOREIGN KEY(game_id) REFERENCES games(id));
INSERT INTO games (id) VALUES ('legacyGame');
INSERT INTO blunders (game_id, ply, played_move, cpl_loss) VALUES ('legacyGame', 3, 'g1f3', 280);
""")
leg.commit()
leg.close()
os.environ["CHESS_DB_PATH"] = path
db_mod.init_db()
c = sqlite3.connect(path)
cols = [r[1] for r in c.execute("PRAGMA table_info(blunders)")]
kept = c.execute("SELECT COUNT(*) FROM blunders").fetchone()[0]
c.close()
verdict("legacy DB gains best_pv", "best_pv" in cols, "columns=%d" % len(cols))
verdict("legacy blunder row survives the migration", kept == 1, "rows=%d" % kept)
os.environ["CHESS_DB_PATH"] = TEMP_DB

# ---------------------------------------------------------------------------
hdr("RE01-C  SYNC-15: LEGACY db already holding duplicates -> boot must collapse them")
# ---------------------------------------------------------------------------
path = fresh_db_file()
leg = sqlite3.connect(path)
leg.executescript("""
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE games (id TEXT PRIMARY KEY, speed TEXT, rated INTEGER, played_at INTEGER,
  white_player TEXT, black_player TEXT, white_rating INTEGER, black_rating INTEGER,
  user_color TEXT, result TEXT, opening_name TEXT, eco TEXT, moves_san TEXT,
  analyzed_at INTEGER DEFAULT 0);
CREATE TABLE blunders (id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT,
  move_number INTEGER, ply INTEGER, color TEXT, fen_before TEXT, fen_after TEXT,
  played_move TEXT, played_san TEXT, best_move TEXT, best_san TEXT, eval_before INTEGER,
  eval_after INTEGER, cpl_loss INTEGER, opening_name TEXT, eco TEXT,
  is_book_move INTEGER DEFAULT 0, refutation_pv TEXT, explanation TEXT,
  strategic_idea TEXT, category TEXT, created_at INTEGER,
  FOREIGN KEY(game_id) REFERENCES games(id));
CREATE TABLE drill_reviews (id INTEGER PRIMARY KEY AUTOINCREMENT,
  blunder_id INTEGER UNIQUE, attempts INTEGER DEFAULT 0, successes INTEGER DEFAULT 0,
  last_attempt_date INTEGER DEFAULT 0, next_review_date INTEGER DEFAULT 0,
  interval_days REAL DEFAULT 1.0, ease_factor REAL DEFAULT 2.5, status TEXT DEFAULT 'new');
INSERT INTO games (id) VALUES ('dupGame');
""")
for _ in range(3):
    leg.execute("INSERT INTO blunders (game_id, ply, played_move, cpl_loss) "
                "VALUES ('dupGame', 3, 'g1f3', 280)")
ids = [r[0] for r in leg.execute("SELECT id FROM blunders WHERE game_id='dupGame'")]
for i, bid in enumerate(ids):
    leg.execute("INSERT INTO drill_reviews (blunder_id, attempts, successes, next_review_date) "
                "VALUES (?,?,?,?)", (bid, i, i, 1700000000 + i))
leg.commit()
leg.close()
os.environ["CHESS_DB_PATH"] = path
db_mod.init_db()
c = sqlite3.connect(path)
rows = c.execute("SELECT id FROM blunders WHERE game_id='dupGame'").fetchall()
left = c.execute("SELECT blunder_id, attempts FROM drill_reviews ORDER BY blunder_id").fetchall()
has_ux = [r[1] for r in c.execute("PRAGMA index_list(blunders)")]
c.close()
verdict("boot collapses duplicate blunder rows", len(rows) == 1,
        "rows=%d (was 3)" % len(rows))
verdict("ux_blunders_natural_key present after the collapse",
        "ux_blunders_natural_key" in has_ux, str(has_ux))
print("      surviving blunder id:", [r[0] for r in rows], "drill rows left:", left,
      "(the copy with the MOST practice must survive)")
os.environ["CHESS_DB_PATH"] = TEMP_DB

# ---------------------------------------------------------------------------
hdr("RE01-D  NEW VARIANT: does the UNIQUE index actually dedup when game_id IS NULL?")
# ---------------------------------------------------------------------------
wipe_games()
n_before = q1("SELECT COUNT(*) c FROM blunders")["c"]
null_row = dict(row)
null_row["game_id"] = None
for _ in range(4):
    GameAnalyzer().save_blunders_to_db([dict(null_row)])
dupes = q1("SELECT COUNT(*) c FROM blunders WHERE game_id IS NULL AND ply=3 "
           "AND played_move='g1f3'")["c"]
try:
    idx_cols = [r[1] for r in get_db().execute(
        "PRAGMA index_xinfo(ux_blunders_natural_key)").fetchall()]
except Exception:
    idx_cols = "(no ux_blunders_natural_key index on this tree)"
get_db().close()
verdict("ux index dedups NULL game_id (it cannot: NULL != NULL in SQLite)", dupes == 1,
        "identical rows after 4 saves = %d, index cols=%s" % (dupes, idx_cols))
wipe_games()

# ---------------------------------------------------------------------------
hdr("RE01-E  SYNC-15 residual: blunder rows that a RE-analysis no longer produces")
#             are never removed -> the gap list keeps reporting them forever
# ---------------------------------------------------------------------------
L = sys.modules["atk3_lib"]
wipe_games()
L.seed_game("staleG", user_color="white", moves="e4 e5 Nf3 Nc6 Bb5 a6",
            analyzed_at=1)
b = dict(row)
b["game_id"] = "staleG"
b["ply"] = 9
b["move_number"] = 5
b["cpl_loss"] = 700
GameAnalyzer().save_blunders_to_db([b])
print("      before re-analysis: blunders =",
      q1("SELECT COUNT(*) c FROM blunders")["c"], " (a ply-9 blunder exists)")

# Now re-analyse the SAME game with a much smaller opening window, exactly the
# way every caller does it: analyze_game_openings() + save_blunders_to_db().
import backend.db as _d  # noqa: E402
prev = _d.get_setting("max_opening_ply", "24")
_d.set_setting("max_opening_ply", "4")
import backend.analyzer as an  # noqa: E402

fake_reanalysis_calls = {"n": 0}
_real_analyze = an.GameAnalyzer.analyze_game_openings


def scripted(self, gd):
    """Stand-in for a real engine pass over a 4-ply window: no blunder found."""
    fake_reanalysis_calls["n"] += 1
    return []


an.GameAnalyzer.analyze_game_openings = scripted
try:
    re_blunders = an.GameAnalyzer().analyze_game_openings({"id": "staleG", "user_color": "white",
                                                           "moves": "e4 e5 Nf3 Nc6 Bb5 a6"})
    an.GameAnalyzer().save_blunders_to_db(re_blunders)
finally:
    an.GameAnalyzer.analyze_game_openings = _real_analyze
    _d.set_setting("max_opening_ply", prev)

left = q1("SELECT COUNT(*) c FROM blunders WHERE game_id='staleG' AND ply=9")["c"]
verdict("re-analysis drops blunder rows it no longer detects", left == 0,
        "ply-9 rows left after a 4-ply re-analysis = %d" % left)
print("      total blunders for staleG:", q1(
    "SELECT COUNT(*) c FROM blunders WHERE game_id='staleG'")["c"])
wipe_games()

# ---------------------------------------------------------------------------
hdr("RE01-F  SYNC-15 residual: 4 CONCURRENT analyses of the same game")
# ---------------------------------------------------------------------------
wipe_games()
L.seed_game("raceG", user_color="white", moves="e4 e5 Nf3", analyzed_at=0)
errs = []
made = []


def worker():
    try:
        s = GameAnalyzer().save_blunders_to_db([dict(row, game_id="raceG")])
        made.append(s)
    except Exception as exc:
        errs.append(repr(exc))


ts = [threading.Thread(target=worker) for _ in range(8)]
for t in ts:
    t.start()
for t in ts:
    t.join()
n = q1("SELECT COUNT(*) c FROM blunders WHERE game_id='raceG'")["c"]
verdict("8 concurrent save_blunders_to_db -> 1 row, no errors", n == 1 and not errs,
        "rows=%d errors=%s summaries=%s" % (n, errs[:2], made))
wipe_games()

# ---------------------------------------------------------------------------
hdr("SUMMARY")
# ---------------------------------------------------------------------------
for name, status, detail in RESULTS:
    print("  %-8s %-62s %s" % (status, name, detail))
print("\n%d/%d hold" % (sum(1 for r in RESULTS if r[1] == "HOLDS"), len(RESULTS)))
