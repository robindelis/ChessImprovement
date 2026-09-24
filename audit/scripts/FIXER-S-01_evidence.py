#!/usr/bin/env python3
"""
FIXER-S-01 — proof that `init_db()` on a byte copy of the REAL production
database adds the SYNC-18 index and changes **no row**.

Production is never opened by SQLite: the copy is made with a plain
`open(..., 'rb')` (whose SHA-256 is printed so the copy is provably faithful),
and every connection afterwards is to the copy in a temp directory.  The only
production access in this script is `tests.probe_guard.fingerprint()`, which
reads bytes and opens no connection at all.

    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-S-01_evidence.py
"""
import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # noqa: E402  FIRST -- temp DB only

from tests import probe_guard  # noqa: E402

TABLES = ("settings", "games", "blunders", "drill_reviews", "repertoire_moves",
          "repertoire_studies", "study_moves", "explorer_cache", "scouted_content")


def digest(conn, table):
    rows = conn.execute("SELECT * FROM %s" % table).fetchall()
    hasher = hashlib.sha256()
    for row in rows:
        hasher.update(repr(tuple(row)).encode("utf-8", "replace"))
    return len(rows), hasher.hexdigest()[:16]


def main():
    production = probe_guard.PRODUCTION_DB_PATH
    if not os.path.exists(production):
        print("[0] no production database present; nothing to prove")
        return 0
    before_fp = probe_guard.fingerprint()

    scratch = tempfile.mkdtemp(prefix="fixer_s_evidence_")
    copy_path = os.path.join(scratch, "copy.db")
    hasher = hashlib.sha256()
    with open(production, "rb") as src, open(copy_path, "wb") as dst:
        for chunk in iter(lambda: src.read(1 << 20), b""):
            hasher.update(chunk)
            dst.write(chunk)
    print("[1] byte copy of production  sha256=%s  bytes=%d"
          % (hasher.hexdigest()[:16], os.path.getsize(copy_path)))

    import backend.db as db_mod

    os.environ["CHESS_DB_PATH"] = copy_path
    db_mod.set_db_path(copy_path)

    conn = db_mod.get_db()
    try:
        before = {t: digest(conn, t) for t in TABLES}
        print("[2] BEFORE init_db(): " + ", ".join("%s=%d" % (t, before[t][0]) for t in TABLES))
    finally:
        conn.close()

    print("[3] ---- init_db() output ----")
    db_mod.init_db()
    print("[4] ---- end init_db() output ----")

    conn = db_mod.get_db()
    try:
        after = {t: digest(conn, t) for t in TABLES}
        print("[5] AFTER  init_db(): " + ", ".join("%s=%d" % (t, after[t][0]) for t in TABLES))
        changed = [t for t in TABLES if before[t] != after[t]]
        print("[6] row digests changed: %s" % (changed or "NOTHING"))
        print("[7] user_version after boot: %d" % conn.execute("PRAGMA user_version").fetchone()[0])
        print("[8] indexes on games: %s" % [r[1] for r in conn.execute("PRAGMA index_list(games)")])
        print("[9] games rows with an empty/NULL id: %d"
              % conn.execute("SELECT COUNT(*) FROM games WHERE id IS NULL OR id = ''").fetchone()[0])
        # A SECOND boot must be a strict no-op, and the index must already exist.
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM games WHERE COALESCE(id, '') = ?",
            ("x",)).fetchall()
        print("[10] the COALESCE lookup plan: %s" % [tuple(r)[-1] for r in plan])
    finally:
        conn.close()

    print("[11] ---- second init_db() ----")
    db_mod.init_db()
    conn = db_mod.get_db()
    try:
        again = {t: digest(conn, t) for t in TABLES}
        print("[12] second boot changed: %s"
              % ([t for t in TABLES if after[t] != again[t]] or "NOTHING"))
    finally:
        conn.close()

    os.environ.pop("CHESS_DB_PATH", None)
    shutil.rmtree(scratch, ignore_errors=True)
    print("[13] production fingerprint unchanged by this script: %s"
          % (probe_guard.fingerprint() == before_fp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
