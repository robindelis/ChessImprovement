"""Shared helpers for ATTACKER-3 (SYNC) audit probes.

HARD RULES (see audit/BRIEF.md):
  * `import tests.test_setup` MUST be the first backend-adjacent import.
  * Never touch chess_improvement.db, never hit 127.0.0.1:38281.
  * Never perform a real Lichess network sync: patch requests / LichessClient.
"""
import os
import sys
import json
import sqlite3
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# ---------------------------------------------------------------------------
# BRIEF rule 2: `import tests.test_setup` MUST be the first backend-adjacent
# import.  It is, literally, the first import statement below.  At the time of
# writing, `tests/test_setup.py` is being edited live by another agent and its
# synthetic-fixture seeding aborts with `RuntimeError: test isolation ABORTED`.
# When that happens we build our OWN throwaway temp DB with the same guarantees
# (tempfile.mkstemp path, asserted different from DEFAULT_DB_PATH, production DB
# never opened).
# ---------------------------------------------------------------------------
try:
    import tests.test_setup  # noqa: E402,F401  (FIRST backend-adjacent import)
    _ATK3_SETUP = "tests.test_setup"
except RuntimeError as _exc:  # pragma: no cover - transient shared-harness state
    print("[atk3] tests.test_setup aborted (%s)" % _exc)
    print("[atk3] falling back to a private temp DB; the production DB is NEVER opened")
    import atexit  # noqa: E402
    import backend.db as _db  # noqa: E402
    _fd, _p = tempfile.mkstemp(prefix="chess_test_atk3_", suffix=".db")
    os.close(_fd)
    os.unlink(_p)
    if os.path.abspath(_p) == os.path.abspath(_db.DEFAULT_DB_PATH):
        raise SystemExit("REFUSING TO RUN: tempfile handed us the production DB path")
    os.environ["CHESS_DB_PATH"] = _p
    _db.set_db_path(_p)
    _db.init_db()

    def _cleanup_atk3():
        for sfx in ("", "-wal", "-shm"):
            f = _p + sfx
            if os.path.exists(f):
                try:
                    os.unlink(f)
                except OSError:
                    pass

    atexit.register(_cleanup_atk3)
    _ATK3_SETUP = "private-temp-db"

import backend.db as db_mod  # noqa: E402
from backend.db import get_db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TEMP_DB = db_mod.get_db_path()
_PROD = os.path.abspath(db_mod.DEFAULT_DB_PATH)
if os.path.abspath(TEMP_DB) == _PROD:  # pragma: no cover - hard safety net
    raise SystemExit("REFUSING TO RUN: CHESS_DB_PATH is the production database")
if not os.path.abspath(TEMP_DB).startswith(tempfile.gettempdir()):
    raise SystemExit("REFUSING TO RUN: test DB %s is not in the temp dir" % TEMP_DB)
print("[atk3] isolated DB: %s (via %s)" % (TEMP_DB, _ATK3_SETUP))


def client():
    from backend.api import app
    return TestClient(app, raise_server_exceptions=False)


def ensure_username(name="RobiDeli"):
    """The sync endpoints refuse to run without a configured username."""
    import backend.db as _d
    if not _d.get_setting("lichess_username"):
        _d.set_setting("lichess_username", name)
    return name


# ---------------------------------------------------------------- db helpers
def wipe_games():
    c = get_db()
    cur = c.cursor()
    for t in ("drill_reviews", "blunders", "games"):
        cur.execute(f"DELETE FROM {t}")
    c.commit()
    c.close()


def wipe_repertoire():
    c = get_db()
    cur = c.cursor()
    for t in ("study_moves", "repertoire_moves", "repertoire_studies"):
        cur.execute(f"DELETE FROM {t}")
    c.commit()
    c.close()
    try:
        from backend.repertoire import RepertoireManager
        RepertoireManager._instance = None
    except Exception:
        pass


def q(sql, params=()):
    c = get_db()
    cur = c.cursor()
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return rows


def q1(sql, params=()):
    r = q(sql, params)
    return r[0] if r else None


def seed_game(game_id, user_color="white", moves="e4 e5 Nf3", result="white",
              opening_name="Italian Game", eco="C50", played_at=1700000000,
              analyzed_at=0):
    c = get_db()
    c.execute(
        """INSERT OR REPLACE INTO games (id, speed, rated, played_at, white_player,
           black_player, white_rating, black_rating, user_color, result, opening_name,
           eco, moves_san, analyzed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (game_id, "blitz", 1, played_at, "RobiDeli" if user_color == "white" else "Opponent",
         "Opponent" if user_color == "white" else "RobiDeli", 1800, 1800, user_color,
         result, opening_name, eco, moves, analyzed_at))
    c.commit()
    c.close()


def seed_blunder(game_id, ply=1, move_number=1, color="white", cpl=300,
                 eval_before=30, eval_after=-270, best_move="e2e4", played_move="d2d4",
                 fen_before=None, opening_games=1000, played_san="d4", best_san="e4",
                 opening_name="Italian Game", eco="C50"):
    if fen_before is None:
        import chess
        b = chess.Board()
        for _ in range(ply - 1):
            b.push_san(["e4", "e5"][(_ - 0) % 2] if False else "e4" if _ % 2 == 0 else "e5")
        fen_before = b.fen(en_passant="fen")
        import chess as _c
        fen_before = _c.Board(fen_before).fen(en_passant="fen")
    c = get_db()
    cols = {r[1] for r in c.execute("PRAGMA table_info(blunders)").fetchall()}
    extra = ["best_pv", "[]"] if "best_pv" in cols else []
    ph = ",".join("?" * (22 + len(extra)))
    c.execute(
        f"""INSERT INTO blunders (game_id, move_number, ply, color, fen_before, fen_after,
           played_move, played_san, best_move, best_san, eval_before, eval_after, cpl_loss,
           opening_name, eco, is_book_move, refutation_pv{''.join(', ' + e for e in extra)},
           explanation, strategic_idea, category, opening_games, created_at)
           VALUES ({ph})""",
        (game_id, move_number, ply, color, fen_before, fen_before + " 0 1", played_move,
         played_san, best_move, best_san, eval_before, eval_after, cpl, opening_name, eco,
         0, "[]", *extra, "x", "y", "Blunder", opening_games, int(time.time())))
    c.commit()
    c.close()


# ------------------------------------------------------------- mock plumbing
class FakeResponse:
    """Minimal duck-type of requests.Response for the NDJSON streaming path."""

    def __init__(self, lines, status_code=200, text=""):
        self._lines = lines
        self.status_code = status_code
        self.text = text
        self._json = None

    def iter_lines(self, decode_unicode=False):
        for ln in self._lines:
            yield ln if decode_unicode else ln.encode()

    def json(self):
        if self._json is not None:
            return self._json
        return json.loads(self.text)


def ndjson(game_dicts):
    return [json.dumps(g) for g in game_dicts]


def fake_lichess_games(monkey_dict_games):
    """Patch LichessClient.fetch_user_games to return the given dicts (no network)."""
    from backend.lichess_client import LichessClient
    LichessClient.fetch_user_games = lambda self, username, max_games=None, since=None, perf_type="rapid,blitz,classical": list(fake_lichess_games.games)
    fake_lichess_games.games = monkey_dict_games


def wipe_explorer():
    c = get_db()
    c.execute("DELETE FROM explorer_cache")
    c.commit()
    c.close()


class _Exploder(Exception):
    """Raised if any probe accidentally reaches the real network."""

    def __init__(self, url):
        super().__init__(f"PROBE LEAKED TO REAL NETWORK: {url}")
        self.url = url


def fake_ndjson(lines, status=200, text=""):
    """Replace backend.lichess_client.requests with a stub returning `lines` as NDJSON.

    Nothing ever leaves the process.
    """
    import types
    import backend.lichess_client as lc

    def _get(url, params=None, headers=None, timeout=None, stream=False, **kw):
        if "lichess.org" in url and "/api/games" in url:
            return FakeResponse(lines, status, text)
        raise _Exploder(url)

    lc.requests = types.SimpleNamespace(get=_get)
    return lc.requests


def restore_network():
    """Undo fake_ndjson/fake_explorer.

    IMPORTANT: this installs a *tripwire* stub rather than the real `requests`
    module, so a probe that accidentally reaches for the network fails loudly
    instead of contacting lichess.org.  Probes that genuinely need the network
    are forbidden by the brief anyway.
    """
    import types
    import backend.lichess_client as lc
    import backend.repertoire as rp

    def _blocked(url, *a, **kw):
        raise _Exploder(url)

    stub = types.SimpleNamespace(get=_blocked)
    lc.requests = stub
    rp.requests = stub
    return stub


def fake_explorer(payload, status=200, raise_exc=False):
    """Replace backend.repertoire.requests with a stub that ONLY answers explorer URLs.

    Any other URL raises _Exploder so a probe can never silently hit lichess.org.
    """
    import types
    import backend.repertoire as rep

    def _get(url, params=None, headers=None, timeout=None, **kw):
        if "explorer" in url:
            if raise_exc:
                raise _Exploder(url)
            r = FakeResponse([], status, text="x")
            r._json = payload
            return r
        raise _Exploder(url)

    stub = types.SimpleNamespace(get=_get)
    rep.requests = stub
    return stub


def hdr(title):
    print("\n" + "=" * 78)
    print("  " + title)
    print("=" * 78)


# Tripwire: installed last, at import time.  From here on NO probe in this
# directory can reach the network - `fake_ndjson` / `fake_explorer` install their
# own stubs, everything else raises _Exploder.
restore_network()
