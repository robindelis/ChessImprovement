"""
FIXER-N evidence harness for backend/db.py (SYNC-01, SYNC-15, CODE-02-08/10, CODE-02-18).

Re-runnable and idempotent.  Two modes:

    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-N_01_evidence.py before
    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-N_01_evidence.py after

`before` loads the PRE-FIX copy of backend/db.py
(audit/scripts/FIXER-N_01_prefix_db.py), `after` loads the live backend/db.py.
Both are applied to:

  1. a genuinely FRESH database (empty file)  -> proves SYNC-01
  2. a BYTE-FAITHFUL COPY of the real production database, made with a plain
     `open(src,'rb').read()` (no SQLite connection to production at all, so
     production is never locked, read as WAL, or checkpointed)  -> proves the
     migrations are safe on the real 9-table schema.

PRODUCTION IS NEVER OPENED BY SQLITE.  The only production access is
`tests.probe_guard.fingerprint()` (plain os.stat + open(...,'rb')) and that same
raw byte read, whose sha256 is printed so the copy is provably faithful.

Everything else runs against throwaway files in the system temp directory.
"""
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import tests.test_setup  # MUST be first: fail-closed isolation of CHESS_DB_PATH

from tests.probe_guard import PRODUCTION_DB_PATH, fingerprint  # noqa: E402

PREFIX_DB = os.path.join(REPO, "audit", "scripts", "FIXER-N_01_prefix_db.py")
LIVE_DB = os.path.join(REPO, "backend", "db.py")

TABLES = ("blunders", "drill_reviews", "explorer_cache", "games", "repertoire_moves",
          "repertoire_studies", "scouted_content", "settings", "study_moves")


def load_db_module(which):
    """Import backend/db.py (or the pre-fix copy) under a private module name."""
    path = PREFIX_DB if which == "before" else LIVE_DB
    spec = importlib.util.spec_from_file_location("fixer_n_db_%s" % which, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def raw_copy(src, dst):
    """Byte-for-byte file copy. No SQLite handle is ever opened on `src`."""
    digest = hashlib.sha256()
    with open(src, "rb") as fh:
        data = fh.read()
    digest.update(data)
    with open(dst, "wb") as fh:
        fh.write(data)
    return digest.hexdigest(), len(data)


def plan(conn, sql, params=()):
    return [tuple(r) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]


def render(label, plans):
    print("  -- plan: %s" % label)
    for row in plans:
        print("     %s" % (row[-1],))
    return "\n".join("     %s" % (r[-1],) for r in plans)


QUERIES = [
    ("boot migration step 1: study_moves -> repertoire_studies -> repertoire_moves",
     """SELECT sm.study_id, rs.name, sm.repertoire, sm.parent_fen, sm.move_uci
        FROM study_moves sm
        JOIN repertoire_studies rs ON rs.id = sm.study_id
        LEFT JOIN repertoire_moves rm ON rm.repertoire = sm.repertoire
             AND rm.parent_fen = sm.parent_fen AND rm.move_uci = sm.move_uci
       WHERE rm.id IS NULL""", ()),
    ("boot migration step 2: study_name backfill (correlated subquery per move)",
     """SELECT id FROM repertoire_moves
        WHERE (study_name IS NULL OR study_name = '')
          AND EXISTS (SELECT 1 FROM study_moves sm
                       WHERE sm.repertoire = repertoire_moves.repertoire
                         AND sm.parent_fen = repertoire_moves.parent_fen
                         AND sm.move_uci = repertoire_moves.move_uci)""", ()),
    ("repertoire tree walk: children of a position",
     "SELECT * FROM repertoire_moves WHERE repertoire = ? AND parent_fen = ?",
     ("white", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -")),
    ("repertoire tree walk: one move lookup by natural key",
     "SELECT * FROM repertoire_moves WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?",
     ("white", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -", "e2e4")),
    # The shape `get_repertoire_tree()` uses (repertoire.py:2151) and the one
    # CODE-02-08 measured: a LEFT JOIN whose inner table had only the
    # study_id-leading PRIMARY KEY, i.e. `SCAN sm LEFT-JOIN` per repertoire row.
    ("app hot path: repertoire_moves LEFT JOIN study_moves (get_repertoire_tree)",
     """SELECT rm.id, rm.parent_fen, rm.fen, rm.move_uci, sm.study_id
        FROM repertoire_moves rm
        LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire
             AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
       WHERE rm.repertoire = ?""", ("white",)),
    ("app hot path: study membership for one colour (get_repertoire_tree map)",
     """SELECT sm.parent_fen, sm.move_uci, rs.name
        FROM study_moves sm JOIN repertoire_studies rs ON rs.id = sm.study_id
       WHERE sm.repertoire = ?""", ("white",)),
    ("app hot path: studies of one position+move (get_move_studies)",
     """SELECT s.name FROM study_moves sm
        JOIN repertoire_studies s ON s.id = sm.study_id
       WHERE sm.repertoire = ? AND sm.parent_fen = ? AND sm.move_uci = ?""",
     ("white", "x", "e2e4")),
    ("analyzer upsert: blunder natural-key lookup",
     "SELECT id FROM blunders WHERE game_id = ? AND ply = ? AND played_move = ?",
     ("x", 1, "e2e4")),
    ("duplicate detection: GROUP BY the natural key HAVING COUNT(*) > 1",
     """SELECT game_id, ply, played_move FROM blunders
        GROUP BY game_id, ply, played_move HAVING COUNT(*) > 1""", ()),
    ("blunders JOIN games (every read of a game's blunders)",
     """SELECT b.id FROM blunders b JOIN games g ON g.id = b.game_id
        WHERE b.game_id = ?""", ("x",)),
    ("blunders ordered by cpl_loss (the BIGGEST BLUNDER list)",
     "SELECT id FROM blunders ORDER BY cpl_loss DESC LIMIT 20", ()),
    ("blunders grouped by opening_name, color (dashboard)",
     """SELECT opening_name, color, COUNT(*), AVG(cpl_loss) FROM blunders
        GROUP BY opening_name, color""", ()),
    ("blunders most recent first (dashboard \"recent blunders\")",
     "SELECT b.id FROM blunders b JOIN games g ON b.game_id = g.id ORDER BY b.created_at DESC LIMIT 5",
     ()),
    ("explorer_cache staleness sweep (7-day TTL, API-side)",
     "SELECT fen FROM explorer_cache WHERE updated_at < ?", (0,)),
    ("scouted_content recent (ORDER BY scouted_at DESC)",
     "SELECT id FROM scouted_content ORDER BY scouted_at DESC LIMIT 20", ()),
    ("games recent/filtered (ORDER BY played_at DESC, WHERE user_color)",
     "SELECT id FROM games WHERE user_color = ? ORDER BY played_at DESC LIMIT 100", ("white",)),
    ("drill_reviews due (spaced repetition)",
     "SELECT id FROM drill_reviews WHERE next_review_date <= ?", (0,)),
]


def run(which):
    module = load_db_module(which)
    # The post-fix predicate is the module's own; the pre-fix build has neither the
    # constant nor the partial index, so it is only queried in `after` mode.
    UNCONVERTED_PREDICATE = getattr(module, "UNCONVERTED_EXPLORER_PREDICATE", "")
    UNCONVERTED_SELECT = getattr(module, "UNCONVERTED_EXPLORER_SELECT",
                                 "SELECT fen, ratings, data_json FROM explorer_cache WHERE %s")
    print("=" * 78)
    print("FIXER-N evidence :: mode=%s :: module=%s" % (which, module.__file__))
    print("=" * 78)

    saved = os.environ.get("CHESS_DB_PATH")
    try:
        # ---------------------------------------------------------------
        # 1. genuinely FRESH database -> SYNC-01
        # ---------------------------------------------------------------
        fresh = os.path.join(tempfile.gettempdir(), "chess_test_fixer_n_fresh_%s.db" % which)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(fresh + suffix):
                os.unlink(fresh + suffix)
        os.environ["CHESS_DB_PATH"] = fresh
        module.init_db()
        conn = sqlite3.connect(fresh)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(blunders)")]
        idx = [r[0] for r in conn.execute("PRAGMA index_list(blunders)")]
        print("\n[1] FRESH init_db()  (path=%s)" % fresh)
        print("    blunders columns (%d): %s" % (len(cols), cols))
        print("    'best_pv' present : %s" % ("best_pv" in cols))
        print("    indexes on blunders: %s" % idx)
        # Prove the analyzer's INSERT shape works (or raises).
        try:
            conn.execute(
                "INSERT INTO blunders (game_id, move_number, ply, color, played_move, best_pv)"
                " VALUES ('g', 1, 1, 'white', 'e2e4', '[]')")
            conn.commit()
            print("    analyzer-shaped INSERT with best_pv: OK")
        except sqlite3.Error as exc:
            print("    analyzer-shaped INSERT with best_pv: %s: %s"
                  % (type(exc).__name__, exc))
        # Prove re-analysis duplicates are refused.
        for attempt in (1, 2):
            try:
                conn.execute("INSERT INTO blunders (game_id, ply, played_move)"
                             " VALUES ('g', 1, 'e2e4')")
                conn.commit()
                print("    duplicate INSERT attempt %d: ACCEPTED" % attempt)
            except sqlite3.IntegrityError as exc:
                print("    duplicate INSERT attempt %d: refused (%s)" % (attempt, exc))
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(fresh + suffix):
                os.unlink(fresh + suffix)

        # ---------------------------------------------------------------
        # 2. byte-faithful copy of the REAL production database
        # ---------------------------------------------------------------
        before_fp = fingerprint()
        copy_path = os.path.join(tempfile.gettempdir(),
                                 "chess_test_fixer_n_prodcopy_%s.db" % which)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(copy_path + suffix):
                os.unlink(copy_path + suffix)
        sha, size = raw_copy(PRODUCTION_DB_PATH, copy_path)
        print("\n[2] COPY OF PRODUCTION  sha256=%s  bytes=%d  (fingerprint mtime_ns=%s size=%s)"
              % (sha[:32], size, before_fp[0], before_fp[1]))

        conn = sqlite3.connect(copy_path)
        conn.row_factory = sqlite3.Row
        before_counts = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                         for t in TABLES}
        print("    row counts BEFORE init_db(): %s" % before_counts)
        before_digest = content_digest(conn)

        os.environ["CHESS_DB_PATH"] = copy_path
        module.init_db()

        conn = sqlite3.connect(copy_path)
        conn.row_factory = sqlite3.Row
        after_counts = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                        for t in TABLES}
        after_digest = content_digest(conn)
        print("    row counts AFTER  init_db(): %s" % after_counts)
        print("    counts changed? %s" % ({t: (before_counts[t], after_counts[t])
                                           for t in TABLES
                                           if before_counts[t] != after_counts[t]} or "NO"))
        print("    content digest before=%s" % before_digest)
        print("    content digest after =%s" % after_digest)
        print("    content identical? %s" % (before_digest == after_digest))
        print("    blunders columns AFTER: %s"
              % [r[1] for r in conn.execute("PRAGMA table_info(blunders)")])

        print("\n[3] EXPLAIN QUERY PLAN on the real schema copy (mode=%s)" % which)
        plan_text = []
        for label, sql, params in QUERIES:
            try:
                plan_text.append((label, render(label, plan(conn, sql, params))))
            except sqlite3.Error as exc:
                plan_text.append((label, "     ERROR %s" % exc))
        print("\n[3] INDEXES after init_db() (mode=%s)" % which)
        for row in conn.execute("SELECT tbl_name, name FROM sqlite_master WHERE type='index'"
                                " AND sql IS NOT NULL ORDER BY tbl_name, name"):
            print("     %-20s %s" % (row[0], row[1]))
        conn.close()
        out = os.path.join(tempfile.gettempdir(), "fixer_n_plans_%s.txt" % which)
        with open(out, "w") as fh:
            for label, text in plan_text:
                fh.write("%s\n%s\n" % (label, text))
        print("\n[3] plans also written to %s" % out)

        # second boot must be a total no-op
        os.environ["CHESS_DB_PATH"] = copy_path
        module.init_db()
        conn = sqlite3.connect(copy_path)
        conn.row_factory = sqlite3.Row
        second_counts = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                         for t in TABLES}
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        probe_plan = [r[-1] for r in conn.execute(
            "EXPLAIN QUERY PLAN " + (UNCONVERTED_SELECT % UNCONVERTED_PREDICATE))] \
            if which == "after" else []
        print("\n[4] idempotence: after a SECOND init_db(): counts identical? %s"
              % (second_counts == after_counts))
        print("    PRAGMA user_version after the boots: %s" % user_version)
        if probe_plan:
            print("    eval-convention probe plan:")
            for line in probe_plan:
                print("       %s" % line)

        # [4b] self-healing: a row that turns up unconverted AFTER the version was
        # stamped must still be migrated on the next boot.
        dirty_fen = "8/8/8/8/8/5k1/8/4K3 b - - 0 1"
        conn.execute(
            "INSERT OR REPLACE INTO explorer_cache (fen, ratings, data_json, updated_at)"
            " VALUES (?, '2000,2200,2500', ?, 1)",
            (dirty_fen, json.dumps({"eval_convention": "stm", "moves": [{"san": "Kd5", "eval": 30}]})))
        conn.commit()
        conn.close()
        module.init_db()
        conn = sqlite3.connect(copy_path)
        payload = json.loads(conn.execute(
            "SELECT data_json FROM explorer_cache WHERE fen = ?", (dirty_fen,)).fetchone()[0])
        conn.close()
        print("[4b] self-healing: a row written dirty AFTER the version was stamped -> "
              "convention=%r eval=%r" % (payload.get("eval_convention"),
                                         payload["moves"][0]["eval"]))

        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(copy_path + suffix):
                os.unlink(copy_path + suffix)
    finally:
        if saved is None:
            os.environ.pop("CHESS_DB_PATH", None)
        else:
            os.environ["CHESS_DB_PATH"] = saved

    after_fp = fingerprint()
    print("\n[5] production fingerprint unchanged by this script? %s"
          % (after_fp == before_fp))
    return 0


def content_digest(conn):
    """Order-independent-ish digest over every row of every user table."""
    h = hashlib.sha256()
    for table in TABLES:
        order = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
        order_sql = ", ".join('"%s"' % c for c in order) or "1"
        for row in conn.execute("SELECT * FROM %s ORDER BY %s" % (table, order_sql)):
            h.update(repr(tuple(row)).encode("utf-8", "replace"))
    return h.hexdigest()[:32]


if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else "after"))
