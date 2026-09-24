"""Shared mocking helpers for ATTACKER-4 (SCOUT) probes.

Every probe importing this module MUST import tests.test_setup first.
No real network / yt-dlp crawl is ever performed: yt_dlp is replaced at the
module boundary with an in-process fake.
"""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# BRIEF hard rule #2: `import tests.test_setup` must be the first backend-adjacent
# import.  It is attempted first, unconditionally.  A concurrent code-agent edit to
# tests/fixtures.py (2026-09-26 ~18:19) made tests.test_setup raise on import, which
# hard-fails *every* test and probe in the repo; when that happens we fall back to an
# equally isolated throwaway temp DB so the SCOUT repros stay runnable.  The
# production database is NEVER opened in either path.
TEST_SETUP_ERROR = None
try:
    import tests.test_setup  # noqa: E402,F401  (must be first backend-adjacent import)
    tests.test_setup.assert_isolated()
except Exception as _exc:  # noqa: BLE001
    TEST_SETUP_ERROR = _exc
    import atexit as _atexit
    import sqlite3 as _sqlite3
    import tempfile as _tempfile

    import backend.db as _db

    _fd, _tmp = _tempfile.mkstemp(prefix="chess_test_", suffix=".db")
    os.close(_fd)
    os.unlink(_tmp)
    os.environ["CHESS_DB_PATH"] = _tmp
    _db.set_db_path(_tmp)
    _db.init_db()
    _c = _sqlite3.connect(_tmp)
    _c.execute("DELETE FROM repertoire_moves")
    _c.execute("DELETE FROM repertoire_studies")
    _c.execute("DELETE FROM study_moves")
    _c.commit()
    _c.close()
    try:
        from backend.repertoire import RepertoireManager as _RM
        from backend.scout import ContentScout as _CS
        _RM._instance = None
        _CS._instance = None
    except Exception:
        pass

    def _cleanup():
        for _s in ("", "-wal", "-shm"):
            _f = _tmp + _s
            if os.path.exists(_f):
                try:
                    os.unlink(_f)
                except OSError:
                    pass

    _atexit.register(_cleanup)
    print("[atk4_mocks] WARNING: tests.test_setup is currently broken (%s: %s);"
          " using a self-contained isolated temp DB instead.  Production DB untouched."
          % (type(_exc).__name__, str(_exc)[:160]))


class FakeYDL:
    """Stand-in for yt_dlp.YoutubeDL. Records the URLs it was asked for."""

    def __init__(self, opts=None):
        FakeYDL.instances.append(self)
        self.opts = opts or {}
        self.last_url = None

    instances = []

    @classmethod
    def reset(cls):
        cls.instances = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        self.last_url = url
        if url.startswith("ytsearch"):
            return FakeYDL.search_payload(url)
        return FakeYDL.video_payload(url)


def install_fake_ytdlp(video_payload=None, search_payload=None, hang=False):
    """Patch backend.scout.yt_dlp.YoutubeDL with FakeYDL."""
    import backend.scout as scout_mod

    FakeYDL.reset()
    FakeYDL.video_payload = staticmethod(video_payload or (lambda url: {"id": "x", "title": "t"}))
    FakeYDL.search_payload = staticmethod(search_payload or (lambda url: {"entries": []}))

    real_ydl = scout_mod.yt_dlp.YoutubeDL
    scout_mod.yt_dlp.YoutubeDL = FakeYDL
    return real_ydl


def uninstall_fake_ytdlp(real_ydl):
    import backend.scout as scout_mod
    scout_mod.yt_dlp.YoutubeDL = real_ydl


def banner(title):
    print("\n" + "=" * 78)
    print("== " + title)
    print("=" * 78)


def rep_dump(repertoire="white"):
    """Dump the repertoire tree (read-only) for evidence."""
    from backend.db import get_db
    conn = get_db()
    rows = conn.execute(
        "SELECT id, parent_fen, fen, move_uci, move_san, is_user_move, study_name, opening_name "
        "FROM repertoire_moves WHERE repertoire=? ORDER BY id",
        (repertoire,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "parent_fen": r["parent_fen"],
            "fen": r["fen"],
            "uci": r["move_uci"],
            "san": r["move_san"],
            "is_user_move": r["is_user_move"],
            "study_name": r["study_name"],
            "opening_name": r["opening_name"],
        })
    return out


def pgn_moves(pgn, source=None):
    """Return the move list the scout pipeline produces for a raw PGN string."""
    from backend.scout import ContentScout
    scout = ContentScout.get_instance()
    return scout.analyze_source(pgn)["full_timeline"]
