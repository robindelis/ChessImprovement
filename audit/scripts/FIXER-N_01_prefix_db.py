import sqlite3
import os
import json
import time
from datetime import datetime

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chess_improvement.db")
DB_PATH = os.environ.get("CHESS_DB_PATH", DEFAULT_DB_PATH)

# Starting position, truncated to the 3 FEN tokens the migration graph-walk uses
# (piece placement + side to move + castling rights).
START_FEN_3 = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq'

# Studies that older versions of the boot migration invented and stamped onto the
# user's own repertoire moves. AGENTS.md section 9.3 forbids seeding these generic
# lines into RobiDeli's production repertoire; they may only be created by the
# explicit, user-triggered `POST /api/repertoire/seed?force=true` endpoint.
FORBIDDEN_AUTO_STUDY_NAMES = ("Italian Game", "Sicilian Dragon", "King's Indian Defense")


def _env_flag(name: str) -> bool:
    """True when an environment variable is set to a truthy value."""
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _fen3(fen: str) -> str:
    """3-token FEN normalisation used for the reachability graph walk."""
    if not fen or not isinstance(fen, str):
        return ""
    return " ".join(fen.strip().split()[:3])

def set_db_path(path: str):
    global DB_PATH
    DB_PATH = path

def get_db_path() -> str:
    return os.environ.get("CHESS_DB_PATH") or DB_PATH

def get_db():
    current_path = os.environ.get("CHESS_DB_PATH") or DB_PATH
    conn = sqlite3.connect(current_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db(purge_orphans: bool = False):
    """
    Create the schema if missing and run the additive, idempotent boot migrations.

    This runs on EVERY application boot (`start.py` and the FastAPI `startup`
    event), so it is the single most dangerous place in the codebase to put a
    statement that touches user data. Safety contract (audit CODE-05-03):

      * Only `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN`,
        `INSERT OR IGNORE` and backfills derived from the user's own
        `study_moves` membership are allowed here.
      * The user's repertoire is never DELETEd, relabelled or pruned.
      * The historical orphan purge is available only behind an explicit opt-in
        (`init_db(purge_orphans=True)` or `CHESS_PURGE_ORPHANS=1`), it is off by
        default, and it logs loudly before it deletes anything.
      * The whole body runs in one explicit transaction; any failure rolls the
        database back to its pre-migration state instead of half-migrating it.
    """
    if _env_flag("CHESS_PURGE_ORPHANS"):
        purge_orphans = True

    conn = get_db()
    previous_isolation = conn.isolation_level
    conn.isolation_level = None  # we drive BEGIN/COMMIT/ROLLBACK explicitly
    try:
        conn.execute("BEGIN IMMEDIATE")
        _init_db_body(conn, purge_orphans=purge_orphans)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.isolation_level = previous_isolation
        conn.close()


def _init_db_body(conn, purge_orphans: bool = False):
    cursor = conn.cursor()
    
    # Settings table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    
    # Games table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS games (
        id TEXT PRIMARY KEY,
        speed TEXT,
        rated INTEGER,
        played_at INTEGER,
        white_player TEXT,
        black_player TEXT,
        white_rating INTEGER,
        black_rating INTEGER,
        user_color TEXT,
        result TEXT,
        opening_name TEXT,
        eco TEXT,
        moves_san TEXT,
        analyzed_at INTEGER DEFAULT 0
    )
    """)
    
    # Blunders table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS blunders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id TEXT,
        move_number INTEGER,
        ply INTEGER,
        color TEXT,
        fen_before TEXT,
        fen_after TEXT,
        played_move TEXT,
        played_san TEXT,
        best_move TEXT,
        best_san TEXT,
        eval_before INTEGER,
        eval_after INTEGER,
        cpl_loss INTEGER,
        opening_name TEXT,
        eco TEXT,
        is_book_move INTEGER DEFAULT 0,
        refutation_pv TEXT,
        explanation TEXT,
        strategic_idea TEXT,
        category TEXT,
        opening_games INTEGER DEFAULT NULL,
        created_at INTEGER,
        FOREIGN KEY(game_id) REFERENCES games(id)
    )
    """)

    # Migration: ensure opening_games column exists if table was already created
    try:
        cursor.execute("ALTER TABLE blunders ADD COLUMN opening_games INTEGER DEFAULT NULL")
    except Exception:
        pass

    
    # Training Drills (Spaced Repetition for blunders)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS drill_reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        blunder_id INTEGER UNIQUE,
        attempts INTEGER DEFAULT 0,
        successes INTEGER DEFAULT 0,
        last_attempt_date INTEGER DEFAULT 0,
        next_review_date INTEGER DEFAULT 0,
        interval_days REAL DEFAULT 1.0,
        ease_factor REAL DEFAULT 2.5,
        status TEXT DEFAULT 'new', -- 'new', 'learning', 'mastered'
        FOREIGN KEY(blunder_id) REFERENCES blunders(id)
    )
    """)

    # Repertoire Moves (Chessbook-style tree)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS repertoire_moves (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        repertoire TEXT, -- 'white' or 'black'
        parent_fen TEXT,
        fen TEXT,
        move_uci TEXT,
        move_san TEXT,
        is_user_move INTEGER DEFAULT 1,
        popularity_pct REAL DEFAULT 0.0,
        win_rate REAL DEFAULT 0.0,
        eval_score INTEGER DEFAULT 0,
        opening_name TEXT DEFAULT '',
        eco TEXT DEFAULT '',
        is_important INTEGER DEFAULT 0,
        comment TEXT DEFAULT '',
        arrows TEXT DEFAULT '',
        study_name TEXT DEFAULT '',
        times_practiced INTEGER DEFAULT 0,
        times_correct INTEGER DEFAULT 0,
        last_practiced INTEGER DEFAULT 0,
        next_review INTEGER DEFAULT 0,
        interval_days REAL DEFAULT 1.0,
        created_at INTEGER,
        UNIQUE(repertoire, parent_fen, move_uci)
    )
    """)

    # Migrations for repertoire_moves columns
    try:
        cursor.execute("ALTER TABLE repertoire_moves ADD COLUMN is_important INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE repertoire_moves ADD COLUMN comment TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE repertoire_moves ADD COLUMN arrows TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE repertoire_moves ADD COLUMN study_name TEXT DEFAULT ''")
    except Exception:
        pass

    # Repertoire Studies (Named study collections like "Caro-Kann Fantasy Variation")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS repertoire_studies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        repertoire TEXT NOT NULL, -- 'white' or 'black'
        description TEXT DEFAULT '',
        created_at INTEGER,
        updated_at INTEGER,
        UNIQUE(name, repertoire)
    )
    """)

    # Study Moves (Associates moves with one or more studies)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS study_moves (
        study_id INTEGER NOT NULL,
        repertoire TEXT NOT NULL,
        parent_fen TEXT NOT NULL,
        move_uci TEXT NOT NULL,
        PRIMARY KEY (study_id, repertoire, parent_fen, move_uci),
        FOREIGN KEY(study_id) REFERENCES repertoire_studies(id) ON DELETE CASCADE
    )
    """)

    # 2000+ Online Database Cache
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS explorer_cache (
        fen TEXT,
        ratings TEXT,
        data_json TEXT,
        updated_at INTEGER,
        PRIMARY KEY (fen, ratings)
    )
    """)

    # Scouted Online Content (Videos, Studies, Guides)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scouted_content (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_type TEXT DEFAULT 'youtube', -- 'youtube', 'lichess_study', 'pgn'
        source_id TEXT UNIQUE,
        title TEXT,
        channel TEXT,
        opening_name TEXT DEFAULT '',
        eco TEXT DEFAULT '',
        thumbnail_url TEXT DEFAULT '',
        duration INTEGER DEFAULT 0,
        data_json TEXT,
        scouted_at INTEGER,
        is_favorite INTEGER DEFAULT 0
    )
    """)
    
    # Migration: Ensure explorer_cache adheres to White-perspective eval convention
    try:
        cursor.execute("SELECT fen, ratings, data_json FROM explorer_cache WHERE data_json NOT LIKE '%\"eval_convention\": \"white\"%'")
        for row in cursor.fetchall():
            fen_val = row["fen"]
            ratings_val = row["ratings"]
            try:
                d = json.loads(row["data_json"])
                parts = fen_val.strip().split()
                if len(parts) >= 2 and parts[1] == 'b':
                    for m in d.get("moves", []):
                        if m.get("eval") is not None:
                            m["eval"] = -m["eval"]
                d["eval_convention"] = "white"
                cursor.execute("UPDATE explorer_cache SET data_json = ? WHERE fen = ? AND ratings = ?", (json.dumps(d), fen_val, ratings_val))
            except Exception:
                pass
    except Exception:
        pass

    # Seed default settings if not exists
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('max_opening_ply', '24')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('blunder_threshold_cp', '150')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('gap_threshold', '5.0')")

    # Migration: restore study-linked moves, backfill study_name, and REPORT
    # (never delete, unless explicitly opted in) moves that are unreachable from
    # the starting position. See audit/reports/CODE-05-03.md.
    try:
        migrate_studies_and_cleanup_blunders(conn, purge_orphans=purge_orphans)
    except Exception as e:
        print(f"Warning in migrate_studies_and_cleanup_blunders: {e}")


def migrate_studies_and_cleanup_blunders(conn, purge_orphans: bool = False):
    """
    Additive, idempotent boot migration. Runs on EVERY app launch, so it is
    written to be incapable of destroying the user's repertoire.

    1. Restores moves from study_moves that are missing in repertoire_moves
       (additive `INSERT OR IGNORE` of rows the user themselves filed under a study).
    2. Backfills `study_name` ONLY for moves that are members of a study and whose
       `study_name` is still empty. Never overwrites a non-empty user value.
    3. Reports the moves that are not reachable from the starting position.
       Deleting them is destructive and therefore opt-in only
       (`init_db(purge_orphans=True)` or `CHESS_PURGE_ORPHANS=1`, default False).

    REMOVED (was step 3, audit finding CODE-05-03): an unconditional
    `DELETE FROM repertoire_moves WHERE id IN (orphans)` that ran on every boot
    and silently destroyed 157 of the user's 243 prepared moves.
    REMOVED (was step 4): automatic creation of the "Italian Game" /
    "Sicilian Dragon" / "King's Indian Defense" studies plus the blanket
    `UPDATE repertoire_moves SET study_name = ...` that relabelled every one of
    the user's moves with one of those generic names. AGENTS.md section 9.3
    forbids seeding those lines into the production repertoire; they are only
    ever created by the explicit `POST /api/repertoire/seed?force=true` endpoint.

    The whole migration runs inside a SAVEPOINT, so a failure rolls back to a
    consistent pre-migration state instead of leaving a half-applied migration.
    """
    cursor = conn.cursor()
    savepoint = "migrate_repertoire_savepoint"
    try:
        cursor.execute(f"SAVEPOINT {savepoint}")
    except Exception as e:
        print(f"Warning in migrate_studies_and_cleanup_blunders (could not open savepoint): {e}")
        return

    try:
        now = int(time.time())

        # ---- 1. Restore any moves from study_moves that are missing in repertoire_moves
        cursor.execute("""
        SELECT sm.study_id, rs.name as study_name, sm.repertoire, sm.parent_fen, sm.move_uci
        FROM study_moves sm
        JOIN repertoire_studies rs ON rs.id = sm.study_id
        LEFT JOIN repertoire_moves rm ON rm.repertoire = sm.repertoire AND rm.parent_fen = sm.parent_fen AND rm.move_uci = sm.move_uci
        WHERE rm.id IS NULL
        """)
        missing = cursor.fetchall()
        if missing:
            import chess
            from backend.openings import OpeningBook
            from backend.repertoire import normalize_fen
            book = OpeningBook.get_instance()
            for r in missing:
                try:
                    b = chess.Board(r["parent_fen"])
                    m = chess.Move.from_uci(r["move_uci"])
                    if m not in b.legal_moves:
                        continue
                    san = b.san(m)
                    is_user = (b.turn == chess.WHITE) if r["repertoire"] == "white" else (b.turn == chess.BLACK)
                    b.push(m)
                    fen_after = normalize_fen(b.fen())
                    op_info = book.get_opening_for_fen(b.fen()) or {"name": "", "eco": ""}
                    cursor.execute("""
                    INSERT OR IGNORE INTO repertoire_moves (
                        repertoire, parent_fen, fen, move_uci, move_san,
                        is_user_move, popularity_pct, win_rate, eval_score,
                        opening_name, eco, is_important, comment, arrows, study_name, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 100.0, 55.0, 0, ?, ?, 0, '', '', ?, ?)
                    """, (
                        r["repertoire"], r["parent_fen"], fen_after, r["move_uci"], san,
                        1 if is_user else 0,
                        op_info.get("name", "") or r["study_name"],
                        op_info.get("eco", ""),
                        r["study_name"],
                        now
                    ))
                except Exception as ex:
                    print(f"Error restoring missing study move {r['move_uci']}: {ex}")

        # ---- 2. Backfill study_name ONLY where it is still empty and the move is in a study
        cursor.execute("""
        UPDATE repertoire_moves
        SET study_name = (
            SELECT rs.name FROM study_moves sm
            JOIN repertoire_studies rs ON rs.id = sm.study_id
            WHERE sm.repertoire = repertoire_moves.repertoire
              AND sm.parent_fen = repertoire_moves.parent_fen
              AND sm.move_uci = repertoire_moves.move_uci
            LIMIT 1
        )
        WHERE (study_name IS NULL OR study_name = '')
          AND EXISTS (
            SELECT 1 FROM study_moves sm
            WHERE sm.repertoire = repertoire_moves.repertoire
              AND sm.parent_fen = repertoire_moves.parent_fen
              AND sm.move_uci = repertoire_moves.move_uci
          )
        """)

        # ---- 3. Reachability report (read-only unless explicitly opted in)
        _report_unreachable_repertoire_moves(conn, purge_orphans=purge_orphans)

        cursor.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception as e:
        try:
            cursor.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            cursor.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            pass
        print(f"Warning in migrate_studies_and_cleanup_blunders: {e}")


def find_unreachable_repertoire_moves(conn):
    """
    READ-ONLY diagnostic. Returns `{repertoire: [repertoire_moves.id, ...]}` for the
    moves that cannot be reached from the starting position by following
    `parent_fen -> fen` links (3-token FEN normalisation).

    A move is reported whenever its `parent_fen` is not the `fen` of another
    stored move and is not the starting position. That is NOT proof of garbage.
    It is exactly the shape of a legitimate fragment:
      * a side-line / sub-line that begins mid-variation (a study line, a Scout
        line import, `import_game_opening`, ...),
      * a move stored before its parent move was added (out-of-tree-order entry,
        which AGENTS.md section 3E describes as a first-class feature: "stepping
        back or undoing moves and playing alternatives automatically branches
        into new variations").

    Treat the result as a hint for a human to review, never as a delete list.
    Performs no writes of any kind.
    """
    result = {}
    for rep in ('white', 'black'):
        cursor = conn.cursor()
        cursor.execute("SELECT id, parent_fen, fen FROM repertoire_moves WHERE repertoire = ?", (rep,))
        rows = cursor.fetchall()
        if not rows:
            continue

        parent_map = {}
        dest_map = {}
        for r in rows:
            parent_map.setdefault(_fen3(r["parent_fen"]), []).append(r["id"])
            dest_map[r["id"]] = _fen3(r["fen"])

        # Breadth-first walk forward from the starting position.
        queue = [START_FEN_3]
        visited = set()
        reachable_ids = set()
        while queue:
            curr = queue.pop(0)
            if curr in visited:
                continue
            visited.add(curr)
            for m_id in parent_map.get(curr, ()):
                reachable_ids.add(m_id)
                dest = dest_map.get(m_id)
                if dest and dest not in visited:
                    queue.append(dest)

        unreachable = sorted(r["id"] for r in rows if r["id"] not in reachable_ids)
        if unreachable:
            result[rep] = unreachable
    return result


def _report_unreachable_repertoire_moves(conn, purge_orphans: bool = False):
    """
    Logs how many repertoire moves are unreachable from the starting position.

    Default (`purge_orphans=False`): purely informational, deletes nothing.
    Opt-in (`purge_orphans=True` / `CHESS_PURGE_ORPHANS=1`): DESTRUCTIVE. Prints a
    loud banner and then deletes the unreachable rows. Back up the database first:
    this can legitimately destroy side-lines and sub-line fragments.
    """
    unreachable = find_unreachable_repertoire_moves(conn)
    total = sum(len(v) for v in unreachable.values())
    if not total:
        return

    summary = ", ".join(f"{rep}={len(ids)}" for rep, ids in sorted(unreachable.items()))
    if not purge_orphans:
        print(
            f"[migration] {total} repertoire move(s) are not reachable from the starting "
            f"position ({summary}). These are most likely side-line/sub-line fragments or "
            f"imported sub-lines; nothing was deleted. Set CHESS_PURGE_ORPHANS=1 to opt in "
            f"to the (lossy) orphan purge."
        )
        return

    print("!" * 78)
    print("!! DESTRUCTIVE ORPHAN PURGE ENABLED (init_db(purge_orphans=True) or CHESS_PURGE_ORPHANS=1)")
    print(f"!! ABOUT TO PERMANENTLY DELETE {total} repertoire_moves row(s): {summary}")
    sample = [mid for ids in unreachable.values() for mid in ids[:5]]
    print(f"!! sample of the affected move ids: {sample}")
    print("!! These may be legitimate side-lines / imported sub-lines. BACK UP THE DATABASE FIRST.")
    print("!" * 78)

    cursor = conn.cursor()
    deleted = 0
    for rep, ids in unreachable.items():
        if not ids:
            continue
        placeholders = ','.join('?' for _ in ids)
        cursor.execute(
            f"DELETE FROM repertoire_moves WHERE repertoire = ? AND id IN ({placeholders})",
            tuple([rep] + list(ids)),
        )
        deleted += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    print(f"[migration] orphan purge deleted {deleted} repertoire move(s)")

def get_setting(key: str, default: str = None) -> str:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key: str, value: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def save_scouted_content(
    source_id: str,
    source_type: str,
    title: str,
    channel: str,
    opening_name: str,
    eco: str,
    thumbnail_url: str,
    duration: int,
    data_json: str
) -> int:
    conn = get_db()
    cursor = conn.cursor()
    now = int(time.time())
    cursor.execute("""
    INSERT INTO scouted_content (
        source_id, source_type, title, channel, opening_name, eco,
        thumbnail_url, duration, data_json, scouted_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(source_id) DO UPDATE SET
        title = excluded.title,
        channel = excluded.channel,
        opening_name = excluded.opening_name,
        eco = excluded.eco,
        thumbnail_url = excluded.thumbnail_url,
        duration = excluded.duration,
        data_json = excluded.data_json,
        scouted_at = excluded.scouted_at
    """, (source_id, source_type, title, channel, opening_name, eco, thumbnail_url, duration, data_json, now))
    row_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return row_id

def get_scouted_content(source_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM scouted_content WHERE source_id = ?", (source_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    res = dict(row)
    if res.get("data_json"):
        try:
            res["data"] = json.loads(res["data_json"])
        except Exception:
            res["data"] = {}
    return res

def list_recent_scouted(limit: int = 20):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT id, source_type, source_id, title, channel, opening_name, eco,
           thumbnail_url, duration, scouted_at, is_favorite
    FROM scouted_content
    ORDER BY scouted_at DESC
    LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def toggle_favorite_scouted(source_id: str) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT is_favorite FROM scouted_content WHERE source_id = ?", (source_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    new_fav = 0 if row["is_favorite"] else 1
    cursor.execute("UPDATE scouted_content SET is_favorite = ? WHERE source_id = ?", (new_fav, source_id))
    conn.commit()
    conn.close()
    return bool(new_fav)

