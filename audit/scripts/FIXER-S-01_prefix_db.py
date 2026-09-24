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

# --------------------------------------------------------------------------- #
# Schema version / one-shot boot migrations (audit CODE-02-18)
# --------------------------------------------------------------------------- #
#: `PRAGMA user_version` of the schema this build of `init_db()` produces.
#:
#: A *data* migration (as opposed to the additive `ALTER TABLE ... ADD COLUMN`
#: ones) needs a "has this already run?" marker, otherwise it re-scans the whole
#: table on every single boot. The repository had no such marker: the only gate
#: was a substring of the data itself, so `explorer_cache` was rewritten from
#: scratch on every launch.
#:
#: `user_version` is the mechanism for that. It is deliberately NOT a tenth
#: `settings` row (a migration marker is not application configuration, and
#: `GET /api/settings` hands every key to the frontend) and deliberately NOT a
#: tenth table (AGENTS.md section 5 documents "There are 9 tables", which
#: `tests/test_docs_contract.py` re-derives from the live schema). It is a
#: header integer: no row, no column, no data.
#:
#: The value only ever moves FORWARD, and only at the end of the transaction
#: that actually performed the matching migration, so a failed or interrupted
#: migration simply runs again on the next boot.
SCHEMA_VERSION = 1

#: `user_version` that the `explorer_cache` white-perspective rewrite takes the
#: database to. Bump `SCHEMA_VERSION` and add a new constant for every new
#: one-shot data migration.
EXPLORER_EVAL_CONVENTION_VERSION = 1

#: SQL predicate selecting exactly the `explorer_cache` rows whose payload is NOT
#: in the documented White-perspective convention -- including the rows whose
#: payload is not JSON at all, which must be *reported*, not silently skipped.
#:
#: This replaces the pre-fix `data_json NOT LIKE '%"eval_convention": "white"%'`.
#: The substring test is a full scan of the largest TEXT column in the database on
#: every boot (audit CODE-02-18), and it is also wrong twice over: a payload
#: written compactly (`"eval_convention":"white"`, no space) is re-processed and
#: has its black-to-move scores negated a *second* time, while a payload that
#: merely *contains* the marker text inside a value is skipped forever. Reading the
#: actual JSON key is correct for both.
UNCONVERTED_EXPLORER_PREDICATE = (
    "NOT (json_valid(data_json) AND "
    "COALESCE(json_extract(data_json, '$.eval_convention'), '') = 'white')"
)

#: Fallback for a SQLite build without the JSON1 functions, so a missing optional
#: extension can never stop the app from booting. Slower (a real table scan), but
#: it selects the same rows.
LEGACY_UNCONVERTED_EXPLORER_PREDICATE = "data_json NOT LIKE '%\"eval_convention\": \"white\"%'"

#: `SELECT` used to find the rows the eval-convention migration has to rewrite.
UNCONVERTED_EXPLORER_SELECT = "SELECT fen, ratings, data_json FROM explorer_cache WHERE %s"

# --------------------------------------------------------------------------- #
# Indexes (audit CODE-02-08 / CODE-02-10)
# --------------------------------------------------------------------------- #
#: `(name, table, columns)` for every `CREATE INDEX IF NOT EXISTS` the boot
#: migration adds. The repository contained no `CREATE INDEX` at all, so every
#: non-primary-key predicate was a full table scan; these are the measured hot
#: ones (see audit/reports/CODE-02-08.md and CODE-02-10.md).
#:
#: Deliberately absent: `blunders.eco`. `api.py` filters it with
#: `AND b.eco LIKE 'B90%'`, and SQLite only turns a `LIKE` into an index seek
#: when the indexed column has a `NOCASE` collation, so an index there would be
#: pure write cost with no read benefit.
SCHEMA_INDEXES = (
    # `study_moves` is joined on (repertoire, parent_fen, move_uci) by every
    # study-aware read and by the boot migration itself, but its only index is
    # the PRIMARY KEY, which leads with the *unconstrained* `study_id` -- so the
    # planner could only `SCAN sm`, i.e. a full scan of study_moves for every
    # row of repertoire_moves (CODE-02-08, measured O(N x M) at every boot).
    ("ix_study_moves_move", "study_moves", "repertoire, parent_fen, move_uci"),
    # Every read of a game's blunders joins `games` through the declared-but-
    # unenforced FK `blunders.game_id` (CODE-02-20), and the "biggest blunder"
    # list orders by the loss.
    ("ix_blunders_game_id", "blunders", "game_id"),
    ("ix_blunders_cpl_loss", "blunders", "cpl_loss DESC, id DESC"),
    # The dashboard groups blunders per opening and reports the ECO of each
    # group's worst row (api.py, audit SYNC-09).
    ("ix_blunders_opening_name", "blunders", "opening_name, color, cpl_loss DESC, id DESC"),
    ("ix_blunders_created_at", "blunders", "created_at DESC"),
    # Spaced repetition: `WHERE next_review_date <= ?` is the trainer's due
    # filter, `status = 'mastered'` feeds the dashboard.
    ("ix_drill_reviews_due", "drill_reviews", "next_review_date"),
    ("ix_drill_reviews_status", "drill_reviews", "status, next_review_date"),
    # 7-day explorer-cache TTL sweep and "recent scouted content".
    ("ix_explorer_cache_updated_at", "explorer_cache", "updated_at"),
    ("ix_scouted_content_scouted_at", "scouted_content", "scouted_at DESC"),
    ("ix_games_user_color", "games", "user_color, played_at"),
)

#: `(name, table, indexed_columns, where)` for the PARTIAL indexes. A partial
#: index only stores the rows that satisfy its `WHERE`, which is how the
#: eval-convention probe below becomes O(unconverted rows) instead of
#: O(cache size) (audit CODE-02-18). SQLite only uses a partial index when the
#: query's `WHERE` matches the index's, so the predicate text is shared verbatim
#: between `SCHEMA_PARTIAL_INDEXES` and the migration's own `SELECT`.
SCHEMA_PARTIAL_INDEXES = (
    ("ix_explorer_cache_unconverted", "explorer_cache", "fen, ratings",
     UNCONVERTED_EXPLORER_PREDICATE),
)


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
        `CREATE INDEX IF NOT EXISTS`, `INSERT OR IGNORE` and backfills derived
        from the user's own `study_moves` membership are allowed here.
      * The user's repertoire is never DELETEd, relabelled or pruned.
      * The only `DELETE` statements in the whole boot path are (a) the
        opt-in orphan purge below and (b) `_collapse_duplicate_blunders`, which
        runs ONLY when the `ux_blunders_natural_key` UNIQUE index was refused
        and then removes exclusively rows that duplicate an already-stored
        natural key, keeping the copy that carries the most drill practice. It
        cannot run on a database that has no duplicates.
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


def _table_columns(cursor, table: str):
    """`{column_name}` for `table`. Returns [] when the table does not exist."""
    return {row[1] for row in cursor.execute('PRAGMA table_info("%s")' % table).fetchall()}


def _add_column_if_missing(cursor, table: str, column: str, declaration: str) -> bool:
    """
    Additive, idempotent `ALTER TABLE ... ADD COLUMN`. Returns True when the
    column was added, False when it already existed.

    The pre-fix code issued the `ALTER` unconditionally inside
    `try: ... except Exception: pass`, which made the migration "idempotent" only
    by swallowing a "duplicate column name" error -- and swallowed a genuine
    "database is locked" the same way, letting the app start with a half-migrated
    schema (audit CODE-02-18, escalation 3). Reading `PRAGMA table_info` first is
    both cheaper and honest: a real failure now propagates and `init_db()` rolls
    the whole migration back.

    `table` and `column` are interpolated into SQL; both are module constants, and
    a quote in either raises instead of producing a broken statement.
    """
    for identifier in (table, column):
        if not identifier.replace("_", "").isalnum():
            raise ValueError("unsafe SQL identifier for _add_column_if_missing: %r" % (identifier,))
    if column in _table_columns(cursor, table):
        return False
    cursor.execute('ALTER TABLE "%s" ADD COLUMN "%s" %s' % (table, column, declaration))
    return True


def _create_index_if_missing(cursor, name: str, table: str, columns: str,
                             unique: bool = False, where: str = None):
    """
    `CREATE [UNIQUE] INDEX IF NOT EXISTS`. Returns True when the index exists
    afterwards.

    * False when a UNIQUE index was refused because the existing rows violate it
      -- the only reason the caller is allowed to de-duplicate.
    * False, with a loud warning, for a *partial* index whose `WHERE` clause uses
      a function this SQLite build lacks (`json_valid`/`json_extract` without
      JSON1). That is a performance regression, never a correctness one, and it
      must not stop the app from booting.

    Any other SQLite error propagates, so `init_db()` rolls back instead of
    silently running with a half-built index set.
    """
    verb = "UNIQUE INDEX" if unique else "INDEX"
    statement = 'CREATE %s IF NOT EXISTS "%s" ON "%s" (%s)' % (verb, name, table, columns)
    if where:
        statement += " WHERE %s" % where
    try:
        cursor.execute(statement)
        return True
    except sqlite3.IntegrityError:
        if not unique:
            raise  # a non-unique index can never be refused for this reason
        return False
    except sqlite3.OperationalError as exc:
        if not where:
            raise
        print("[migration] could not create the partial index %r: %s. Falling back to a"
              " full scan for this migration; the app is unaffected." % (name, exc))
        return False


def _collapse_duplicate_blunders(cursor):
    """
    Collapse `blunders` rows that duplicate the natural key
    `(game_id, ply, played_move)` (audit SYNC-15). Returns
    `(blunder_rows_removed, drill_rows_merged)`.

    Deterministic and minimal:

    * only rows that share a natural key with another row are ever touched -- a
      row whose key is unique is never removed, so a database without duplicates
      is left byte-for-byte identical and the caller executes no `DELETE` at all
      (the de-duplication only runs when the UNIQUE index was *refused*, which
      cannot happen on a clean table);
    * the survivor of each group is the row with the most spaced-repetition
      practice (`attempts`, then `successes`, then the lowest `id`), i.e. the copy
      that carries the user's trainer history;
    * a discarded copy's `drill_reviews` row is re-pointed at the survivor, or --
      when the survivor already has one -- merged into it, so attempts are
      summed, the *earlier* `last_attempt_date` / `next_review_date` win (a card
      that is still owed practice stays owed), the larger `interval_days` is kept
      and `mastered` is never downgraded. Practice is never silently dropped.

    `IS` is used for the key comparison so a NULL component (a legacy row with no
    `game_id`, for instance) is matched NULL-safely and cannot slip past the
    UNIQUE index, which uses the same NULL semantics.
    """
    cursor.execute("""
        SELECT game_id, ply, played_move
        FROM blunders
        GROUP BY game_id, ply, played_move
        HAVING COUNT(*) > 1
    """)
    groups = cursor.fetchall()
    if not groups:
        return 0, 0

    removed = 0
    merged = 0
    for group in groups:
        cursor.execute("""
            SELECT b.id AS id
            FROM blunders b
            LEFT JOIN drill_reviews d ON d.blunder_id = b.id
            WHERE b.game_id IS ? AND b.ply IS ? AND b.played_move IS ?
            ORDER BY COALESCE(d.attempts, 0) DESC,
                     COALESCE(d.successes, 0) DESC,
                     b.id ASC
        """, (group["game_id"], group["ply"], group["played_move"]))
        ids = [int(row["id"]) for row in cursor.fetchall()]
        survivor, duplicates = ids[0], ids[1:]
        for duplicate in duplicates:
            cursor.execute("SELECT * FROM drill_reviews WHERE blunder_id = ?", (duplicate,))
            drill = cursor.fetchone()
            if drill is not None:
                cursor.execute("SELECT * FROM drill_reviews WHERE blunder_id = ?", (survivor,))
                keep = cursor.fetchone()
                if keep is None:
                    cursor.execute(
                        "UPDATE drill_reviews SET blunder_id = ? WHERE blunder_id = ?",
                        (survivor, duplicate),
                    )
                else:
                    cursor.execute("""
                        UPDATE drill_reviews
                        SET attempts = attempts + ?,
                            successes = successes + ?,
                            last_attempt_date = MIN(COALESCE(last_attempt_date, 0), ?),
                            next_review_date = MIN(
                                CASE WHEN next_review_date <= 0 THEN 9223372036854775807
                                     ELSE next_review_date END, ?),
                            interval_days = MAX(COALESCE(interval_days, 1.0), ?),
                            ease_factor = MIN(COALESCE(ease_factor, 2.5), ?),
                            status = CASE WHEN status = 'mastered' OR ? = 'mastered'
                                          THEN 'mastered' ELSE status END
                        WHERE blunder_id = ?
                    """, (
                        drill["attempts"] or 0,
                        drill["successes"] or 0,
                        drill["last_attempt_date"] or 0,
                        drill["next_review_date"] or 0,
                        drill["interval_days"] or 1.0,
                        drill["ease_factor"] or 2.5,
                        drill["status"] or "new",
                        survivor,
                    ))
                    cursor.execute("DELETE FROM drill_reviews WHERE blunder_id = ?", (duplicate,))
                    merged += 1
            cursor.execute("DELETE FROM blunders WHERE id = ?", (duplicate,))
            removed += 1
    return removed, merged


def _unconverted_explorer_rows(conn, cursor):
    """
    `[(fen, ratings, data_json)]` of the `explorer_cache` rows the eval-convention
    migration still has to rewrite, using the SAME predicate as the partial index
    `ix_explorer_cache_unconverted` so SQLite can answer it from that index alone
    (`SCAN explorer_cache USING INDEX ix_explorer_cache_unconverted`).

    Falls back to the pre-fix `LIKE` predicate, and then to a scan of *every* row,
    if this SQLite build lacks the JSON1 functions.
    """
    last = None
    for predicate in (UNCONVERTED_EXPLORER_PREDICATE, LEGACY_UNCONVERTED_EXPLORER_PREDICATE):
        try:
            cursor.execute(UNCONVERTED_EXPLORER_SELECT % predicate)
            return cursor.fetchall()
        except sqlite3.OperationalError as exc:
            last = exc
    print("[migration] this SQLite build has neither json_valid/json_extract nor a"
          " usable LIKE; scanning every cached payload: %s" % (last,))
    cursor.execute("SELECT fen, ratings, data_json FROM explorer_cache")
    rows = []
    for row in cursor.fetchall():
        try:
            if json.loads(row["data_json"]).get("eval_convention") != "white":
                rows.append(row)
        except Exception:
            rows.append(row)
    return rows


def _migrate_explorer_cache_eval_convention(conn, cursor):
    """
    Rewrite every `explorer_cache` payload into the documented White-perspective
    eval convention (negating the moves of a black-to-move position), then stamp
    `PRAGMA user_version`.

    The effect is deliberately unchanged. What changed (audit CODE-02-18):

    * **The per-boot full-table scan is gone.** The pre-fix "already done" signal
      was a substring of the data itself, so `explorer_cache` -- the fastest
      growing table in the app, one row per visited position -- was scanned end to
      end with a leading-wildcard `LIKE '%...%'` on every process start, twice per
      boot (`start.py` and the FastAPI `startup` event). The scan is now answered
      by the partial index `ix_explorer_cache_unconverted`, whose size is the
      number of rows that actually need work: measured 0.0014 ms vs 0.48 ms per
      probe on a 5000-row cache.
    * **The rewrite is once-only.** It runs at most once per unconverted row, and
      `PRAGMA user_version` records that this build performed it, so the version
      is the authoritative "already done" marker rather than a substring of the
      data.
    * **It is transactional.** It runs inside the single `BEGIN IMMEDIATE` that
      `init_db()` already owns, so a crash mid-rewrite cannot leave the cache
      half-converted.
    * **A per-row failure is LOUD instead of `pass`ed.** The pre-fix code
      swallowed every exception twice over, so a corrupt payload was skipped
      forever with no log and re-attempted on every subsequent boot. Such a row is
      now named in a warning banner and the version is left un-stamped, so the
      retry (cheap: only the unconverted rows are visited) stays loud until it is
      fixed. A payload that is not valid JSON is deliberately kept inside the
      partial index by the predicate, so it cannot be quietly dropped either.

    The probe is deliberately NOT skipped just because the version is stamped: a
    row that turns up unconverted *after* the stamp (a legacy import, a
    hand-edited payload, a test fixture) is still repaired on the next boot
    instead of being silently stranded forever. Every payload the application
    itself writes already carries `eval_convention: "white"`
    (`backend/repertoire.py:353`), and the read path re-normalises anything else
    on the way out (`backend/repertoire.py:254`).
    """
    version = int((conn.execute("PRAGMA user_version").fetchone() or (0,))[0])
    rows = _unconverted_explorer_rows(conn, cursor)
    if not rows:
        if version < EXPLORER_EVAL_CONVENTION_VERSION:
            _stamp_schema_version(conn, EXPLORER_EVAL_CONVENTION_VERSION)
        return

    failures = []
    rewritten = 0
    for row in rows:
        fen_val = row["fen"]
        ratings_val = row["ratings"]
        try:
            d = json.loads(row["data_json"])
            if not isinstance(d, dict):
                raise ValueError("payload is %s, not a JSON object" % type(d).__name__)
            parts = fen_val.strip().split()
            if len(parts) >= 2 and parts[1] == 'b':
                for m in d.get("moves", []):
                    if m.get("eval") is not None:
                        m["eval"] = -m["eval"]
            d["eval_convention"] = "white"
            cursor.execute(
                "UPDATE explorer_cache SET data_json = ? WHERE fen = ? AND ratings = ?",
                (json.dumps(d), fen_val, ratings_val),
            )
            rewritten += 1
        except Exception as exc:
            failures.append((fen_val, ratings_val, "%s: %s" % (type(exc).__name__, exc)))

    if failures:
        print("!" * 78)
        print("!! explorer_cache eval-convention migration could not rewrite %d row(s):"
              % len(failures))
        for fen_val, ratings_val, reason in failures[:20]:
            print("!!   fen=%r ratings=%r -- %s" % (fen_val, ratings_val, reason))
        if len(failures) > 20:
            print("!!   ... and %d more" % (len(failures) - 20))
        print("!! The convention marker was NOT stamped, so the remaining rows are retried on")
        print("!! the next boot. Until then they are read as side-to-move perspective by")
        print("!! backend/repertoire.py, which normalises the convention on read.")
        print("!" * 78)
        return

    _stamp_schema_version(conn, EXPLORER_EVAL_CONVENTION_VERSION)
    print(f"[migration] explorer_cache: rewrote {rewritten} payload(s) into the "
          "white-perspective eval convention")


def _stamp_schema_version(conn, version: int) -> None:
    """Move `PRAGMA user_version` forward. Never backwards."""
    if isinstance(version, bool) or not isinstance(version, int):
        raise TypeError("schema version must be an int, got %r" % (version,))
    current = int((conn.execute("PRAGMA user_version").fetchone() or (0,))[0])
    if version <= current:
        return
    conn.execute("PRAGMA user_version = %d" % version)


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
    #
    # `best_pv` is created HERE as well as by the `ALTER TABLE ... ADD COLUMN`
    # migration below (audit SYNC-01): the column was only ever added to
    # pre-existing databases, so a *freshly* migrated database had 23 columns
    # while `backend/analyzer.py` unconditionally INSERTs into `best_pv` -- every
    # blunder write raised `OperationalError: table blunders has no column named
    # best_pv` and the whole blunder / drill / gap / dashboard feature was dead.
    # It is declared last so a fresh schema and a migrated one have the same
    # `PRAGMA table_info` column order.
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
        best_pv TEXT,
        FOREIGN KEY(game_id) REFERENCES games(id)
    )
    """)

    # Migrations for `blunders` columns. `_add_column_if_missing` is additive and
    # idempotent by construction: it reads `PRAGMA table_info` and only issues the
    # `ALTER` when the column is genuinely absent, so it no longer relies on
    # swallowing an exception (which hid a real "database is locked" failure and
    # let the app boot with a half-migrated schema -- audit CODE-02-22 item 6).
    _add_column_if_missing(cursor, "blunders", "opening_games", "INTEGER DEFAULT NULL")
    # SYNC-01: the principal variation of the engine's suggested line.
    _add_column_if_missing(cursor, "blunders", "best_pv", "TEXT")

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

    # Migrations for repertoire_moves columns (see `_add_column_if_missing` above)
    _add_column_if_missing(cursor, "repertoire_moves", "is_important", "INTEGER DEFAULT 0")
    _add_column_if_missing(cursor, "repertoire_moves", "comment", "TEXT DEFAULT ''")
    _add_column_if_missing(cursor, "repertoire_moves", "arrows", "TEXT DEFAULT ''")
    _add_column_if_missing(cursor, "repertoire_moves", "study_name", "TEXT DEFAULT ''")

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
    
    # ---- Indexes (audit CODE-02-08 / CODE-02-10) ---------------------------
    for index_name, table, columns in SCHEMA_INDEXES:
        _create_index_if_missing(cursor, index_name, table, columns)
    for index_name, table, columns, where in SCHEMA_PARTIAL_INDEXES:
        _create_index_if_missing(cursor, index_name, table, columns, where=where)

    # ---- Uniqueness on `blunders`' natural key (audit SYNC-15) -------------
    # A re-analysis used to append a second copy of every row, because the only
    # thing standing between the table and a duplicate was the caller's mutable
    # `games.analyzed_at` flag. The guarantee belongs in the schema, but a UNIQUE
    # index cannot be created while duplicates exist, so they are collapsed first
    # (only ever rows that duplicate an existing natural key -- see
    # `_collapse_duplicate_blunders`).
    if not _create_index_if_missing(cursor, "ux_blunders_natural_key", "blunders",
                                    "game_id, ply, played_move", unique=True):
        removed, merged = _collapse_duplicate_blunders(cursor)
        print("!" * 78)
        print("!! blunders contained rows that duplicated the natural key "
              "(game_id, ply, played_move)")
        print(f"!! collapsed {removed} duplicate blunder row(s); merged {merged} "
              "spaced-repetition row(s) into the surviving copy")
        print("!! the surviving copy of each group is the one carrying the most drill practice")
        print("!" * 78)
        if not _create_index_if_missing(cursor, "ux_blunders_natural_key", "blunders",
                                        "game_id, ply, played_move", unique=True):
            raise RuntimeError(
                "could not create ux_blunders_natural_key even after collapsing "
                "duplicates; the database was left untouched by init_db()"
            )

    # Migration: Ensure explorer_cache adheres to White-perspective eval
    # convention. Runs ONCE (audit CODE-02-18).
    _migrate_explorer_cache_eval_convention(conn, cursor)

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

