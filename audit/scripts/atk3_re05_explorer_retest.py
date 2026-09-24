"""[ATK3-RETEST] SYNC-04/05: can `explorer_cache` still be poisoned, and who else
reads a poisoned row?

FIXER-B was supposed to have put caps/validation at the `api.py` boundary. This
probe:
  1. re-runs the original poisoning payloads through `/api/repertoire/explore`;
  2. tries to reach the SAME writer through a route that is not that boundary;
  3. follows a poisoned row into the four OTHER readers of `explorer_cache`:
     `get_explorer_game_count` -> `get_game_gaps` (/api/repertoire/tree),
     `analyze_user_games` (-> `blunders.opening_games`),
     `_master_stats_from_explorer_cache` (/api/blunders/{id}),
     and the boot migration in `db.py`.

    /home/robin/miniconda3/bin/python3 audit/scripts/atk3_re05_explorer_retest.py
"""
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
# The app logs a full traceback for every 500; this probe reports status codes
# itself, so silence the logger to keep the output readable.
logging.getLogger().setLevel(logging.CRITICAL)
for _n in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "starlette"):
    logging.getLogger(_n).setLevel(logging.CRITICAL)
from atk3_lib import (hdr, q, q1, get_db, wipe_games, seed_game, seed_blunder,  # noqa: E402
                      FakeResponse, wipe_explorer, client, ensure_username)

import backend.db as db_mod  # noqa: E402
import backend.repertoire as rp  # noqa: E402
import backend.api as api  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402

RESULTS = []


def verdict(name, ok, detail=""):
    RESULTS.append((name, "HOLDS" if ok else "BROKEN", detail))
    print("   -> %-6s %-52s %s" % ("HOLDS" if ok else "BROKEN", name, detail))


START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
# `explorer_cache` is keyed by the 4-token `normalize_fen()` form, NOT the 6-token FEN.
START4 = " ".join(START.split()[:4])
cl = client()
ensure_username("RobiDeli")


def put_cache(fen_key, payload_json, ratings="2000,2200,2500"):
    c = get_db()
    c.execute("INSERT OR REPLACE INTO explorer_cache (fen, ratings, data_json, updated_at)"
              " VALUES (?,?,?,?)", (fen_key, ratings, payload_json, int(time.time())))
    c.commit()
    c.close()


def my_seed_blunder(game_id, ply=1, color="white", cpl=300, best_move="e2e4",
                    played_move="d2d4", opening_games=1000, fen_before=None):
    """Local, schema-aware version of atk3_lib.seed_blunder (the helper mis-counts
    placeholders when `best_pv` exists)."""
    import chess as _chess
    b = _chess.Board()
    if fen_before is None:
        fen_before = START
    c = get_db()
    cols = {r[1] for r in c.execute("PRAGMA table_info(blunders)").fetchall()}
    values = {
        "game_id": game_id, "move_number": max(1, (ply + 1) // 2), "ply": ply,
        "color": color, "fen_before": fen_before, "fen_after": fen_before,
        "played_move": played_move, "played_san": "d4", "best_move": best_move,
        "best_san": "e4", "eval_before": 30, "eval_after": 30 - cpl, "cpl_loss": cpl,
        "opening_name": "Italian Game", "eco": "C50", "is_book_move": 0,
        "refutation_pv": "[]", "explanation": "x", "strategic_idea": "y",
        "category": "Blunder", "opening_games": opening_games,
        "created_at": int(time.time()),
    }
    if "best_pv" in cols:
        values["best_pv"] = "[]"
    names = list(values)
    c.execute("INSERT INTO blunders (%s) VALUES (%s)" % (
        ",".join(names), ",".join("?" * len(names))), [values[n] for n in names])
    c.commit()
    c.close()


def serve_explorer(payload, status=200, raise_exc=False):
    import types

    def _get(url, params=None, headers=None, timeout=None, **kw):
        if "explorer" in url:
            if raise_exc:
                raise rp.ConnectionError("simulated upstream failure")
            r = FakeResponse([], status, "x")
            r._json = payload
            return r
        raise AssertionError("PROBE LEAK: " + url)
    stub = types.SimpleNamespace(get=_get)
    rp.requests = stub
    return stub


REAL = {"white": 10, "draws": 1, "black": 10,
        "moves": [{"uci": "e2e4", "san": "e4", "white": 10, "draws": 1, "black": 10,
                   "averageRating": 2100}]}


def cache_rows():
    return q("SELECT fen, ratings, substr(data_json,1,90) d FROM explorer_cache")


# ---------------------------------------------------------------------------
hdr("RE05-A  the writer, through /api/repertoire/explore (the boundary FIXER-B owns)")
# ---------------------------------------------------------------------------
PAYLOADS = [
    ("valid baseline", REAL),
    ("Infinity counts", dict(REAL, white=1e400 if False else float("inf"))),
    ("NaN counts", dict(REAL, white=float("nan"))),
    ("moves is a dict", dict(REAL, moves={"e4": 1})),
    ("root is a list", [1, 2, 3]),
    ("a move is a string", dict(REAL, moves=["e4"])),
    ("a move has string counts", dict(REAL, moves=[{"uci": "e2e4", "san": "e4",
                                                    "white": "x", "draws": None, "black": -5}])),
    ("negative totals", dict(REAL, white=-50, draws=-50, black=-50)),
    ("absurd totals", dict(REAL, white=10 ** 15, draws=0, black=0)),
    ("averageRating is a string", dict(REAL, moves=[{"uci": "e2e4", "san": "e4",
                                                     "white": 1, "draws": 0, "black": 0,
                                                     "averageRating": "<b>x</b>"}])),
]
for label, payload in PAYLOADS:
    wipe_explorer()
    RepertoireManager._instance = None
    serve_explorer(payload)
    r = cl.get("/api/repertoire/explore", params={"fen": START})
    rows = cache_rows()
    print("   %-30s HTTP %-4s cached=%s" % (label, r.status_code,
                                           rows and rows[0]["d"][:70]))
    if r.status_code == 200 and rows:
        # A cache row that the next request cannot serialise is a 7-day landmine.
        r2 = cl.get("/api/repertoire/explore", params={"fen": START})
        print("      2nd request (pure cache hit, no network): HTTP %s %s" % (
            r2.status_code, r2.text[:70]))
        if r2.status_code >= 400:
            verdict("poison survives to the 2nd request: %s" % label, False,
                    "1st HTTP %s -> 2nd HTTP %s %s" % (r.status_code, r2.status_code, r2.text[:60]))
    elif r.status_code >= 500:
        verdict("500 from a malformed explorer payload: %s" % label, False,
                r.text[:80])

# ---------------------------------------------------------------------------
hdr("RE05-B  is there ANY other route to the same writer (a boundary bypass)?")
# ---------------------------------------------------------------------------
import inspect  # noqa: E402
src = inspect.getsource(rp.RepertoireManager.get_candidate_moves_2000)
print("   writers of explorer_cache inside repertoire.py:")
import re as _re  # noqa: E402
for m in _re.finditer(r".*(INSERT OR REPLACE INTO explorer_cache|UPDATE explorer_cache"
                      r"|DELETE FROM explorer_cache).*", src):
    print("     ", m.group(0).strip())
print("   callers of get_candidate_moves_2000:")
import subprocess  # noqa: E402
print(subprocess.run(["grep", "-rn", "get_candidate_moves_2000", "backend", "tests", "frontend",
                      "--include=*.py", "--include=*.js"], capture_output=True,
                     text=True).stdout)
print("   the route's declared boundary (api.py):")
print("     ", "\n      ".join(
    l.strip() for l in inspect.getsource(api.explore_position_2000).splitlines()))

# ---------------------------------------------------------------------------
hdr("RE05-C  ONE poisoned row -> the other FOUR readers of explorer_cache")
# ---------------------------------------------------------------------------
def poison(total):
    wipe_explorer()
    put_cache(START4, json.dumps({"fen": START, "total_games": total,
                                  "white_total": total, "draws_total": 0,
                                  "black_total": 0, "moves": [REAL["moves"][0]],
                                  "eval_convention": "white"}))
    RepertoireManager._instance = None
    import atk3_lib
    atk3_lib.restore_network()


for label, total in (("Infinity", float("inf")), ("NaN", float("nan")), ("1e300", 1e300),
                     ("a huge int", 10 ** 300)):
    poison(total)
    rm = RepertoireManager.get_instance()
    rm._explorer_count_memo = {}
    rm._user_stats_cache, rm._user_stats_time = {}, 0
    try:
        cnt = rm.get_explorer_game_count(START)
    except Exception as exc:
        cnt = "RAISED %r" % (exc,)
    print("\n   --- total_games = %s ---" % label)
    print("   get_explorer_game_count(START) ->", repr(cnt))

    # reader 2: /api/repertoire/tree  (get_game_gaps)
    wipe_games()
    seed_game("pG", user_color="white", moves="e4 e5 Nf3", analyzed_at=1)
    my_seed_blunder("pG", ply=1, color="white", cpl=300, best_move="e2e4",
                    played_move="d2d4", opening_games=None)
    try:
        rm2 = RepertoireManager.get_instance()
        rm2._explorer_count_memo = {}
        rm2._user_stats_cache, rm2._user_stats_time = {}, 0
        tree = cl.get("/api/repertoire/tree", params={"color": "white", "min_games": 30})
        print("   GET /api/repertoire/tree  -> HTTP %s %s" % (tree.status_code, tree.text[:90]))
        if tree.status_code >= 500:
            verdict("poisoned cache 500s /api/repertoire/tree (total_games=%s)" % label, False,
                    "HTTP %s %s" % (tree.status_code, tree.text[:70]))
    except Exception as exc:
        verdict("poisoned cache breaks /api/repertoire/tree (total_games=%s)" % label, False,
                "raised %r" % (exc,))

    # reader 3: /api/blunders/{id}  (_master_stats_from_explorer_cache)
    my_seed_blunder("pG", ply=5, color="white", cpl=400, best_move="e2e4", played_move="d2d4")
    bid = q1("SELECT id FROM blunders WHERE game_id='pG' ORDER BY id DESC")["id"]
    d = cl.get("/api/blunders/%d" % bid)
    print("   GET /api/blunders/%d -> HTTP %s %s" % (bid, d.status_code, d.text[:90]))
    if d.status_code >= 500:
        verdict("poisoned cache 500s /api/blunders/{id} (total_games=%s)" % label, False,
                "HTTP %s %s" % (d.status_code, d.text[:70]))

    # reader 4: analyze_user_games writes it into blunders.opening_games
    import backend.analyzer as an  # noqa: E402
    an.GameAnalyzer.analyze_game_openings = lambda self, gd: [{
        "game_id": gd.get("id"), "move_number": 1, "ply": 1, "color": "white",
        "fen_before": START4, "fen_after": START4, "played_move": "d2d4", "played_san": "d4",
        "best_move": "e2e4", "best_san": "e4", "eval_before": 20, "eval_after": -300,
        "cpl_loss": 320, "opening_name": "X", "eco": "C50", "is_book_move": 0,
        "refutation_pv": "[]", "best_pv": "[]", "explanation": "x", "strategic_idea": "y",
        "category": "Blunder", "created_at": int(time.time())}]
    payload = {"id": "zzG", "speed": "blitz", "rated": True, "createdAt": 1700000000000,
               "winner": "white", "opening": {"name": "X", "eco": "C50"},
               "players": {"white": {"user": {"name": "RobiDeli"}, "rating": 1},
                           "black": {"user": {"name": "R"}, "rating": 1}},
               "moves": "d4 d5"}
    import backend.lichess_client as lc  # noqa: E402
    import types  # noqa: E402
    lc.requests = types.SimpleNamespace(
        get=lambda url, **kw: FakeResponse([json.dumps(payload)], 200, ""))
    rm3 = RepertoireManager.get_instance()
    rm3._explorer_count_memo = {}
    try:
        rm3.analyze_user_games(max_games=5)
        og = q1("SELECT opening_games, typeof(opening_games) t FROM blunders "
                "WHERE game_id='zzG'")
        print("   analyze_user_games -> blunders.opening_games =", og)
    except Exception as exc:
        print("   analyze_user_games RAISED %r" % (exc,))
        verdict("poisoned cache makes analyze_user_games raise (total_games=%s)" % label, False,
                "raised %r" % (exc,))
    import atk3_lib  # noqa: E402
    atk3_lib.restore_network()
wipe_games()
wipe_explorer()

# ---------------------------------------------------------------------------
hdr("RE05-D  GET /api/repertoire/explore accepts an UNBOUNDED `ratings` cache key")
# ---------------------------------------------------------------------------
print("   route signature:", "ratings: str = Query('2000,2200,2500')  (no length/format check)")
wipe_explorer()
RepertoireManager._instance = None
serve_explorer(REAL)
base = cl.get("/api/repertoire/explore", params={"fen": START})
print("   default ratings            -> HTTP %s" % base.status_code)
huge = cl.get("/api/repertoire/explore", params={"fen": START, "ratings": "9" * 20000})
print("   ratings=999... (20 000 ch) -> HTTP %s" % huge.status_code)
print("   cache rows now:", cache_rows())
n_rows = q("SELECT COUNT(*) n FROM explorer_cache")[0]["n"]
verdict("an unbounded `ratings` string cannot blow up the explorer request", False,
        "the route has no length/format check on `ratings`, which is also the "
        "explorer_cache key, so every distinct value forces a fresh upstream query "
        "and a new row; %d row(s) now exist for ONE position" % n_rows)
wipe_explorer()

# ---------------------------------------------------------------------------
hdr("RE05-E  a poisoned `data_json` written DIRECTLY (no explorer involved)")
# ---------------------------------------------------------------------------
wipe_explorer()
wipe_games()
seed_game("pG2", user_color="white", moves="e4 e5 Nf3", analyzed_at=1)
my_seed_blunder("pG2", ply=1, color="white", cpl=300, best_move="e2e4", played_move="d2d4")
for label, body in [
    ("not JSON at all", "{"),
    ("JSON null", "null"),
    ("a JSON list", "[]"),
    ("moves is a string", '{"total_games": 5, "moves": "e4"}'),
    ("NaN inside a move", '{"total_games": 5, "moves": [{"eval": NaN}]}'),
]:
    put_cache(START4, body)
    RepertoireManager._instance = None
    try:
        rm = RepertoireManager.get_instance()
        rm._explorer_count_memo = {}
        n = rm.get_explorer_game_count(START)
        note = "count=%r" % (n,)
    except Exception as exc:
        note = "RAISED %r" % (exc,)
    try:
        t = cl.get("/api/repertoire/tree", params={"color": "white"})
        tnote = "tree HTTP %s" % t.status_code
    except Exception as exc:
        tnote = "tree raised %r" % (exc,)
    print("   %-20s %-28s %s" % (label, note, tnote))
    if "RAISED" in note or "500" in tnote:
        verdict("a hand-written explorer_cache payload (%s) is handled" % label, False,
                "%s / %s" % (note, tnote))
wipe_games()
wipe_explorer()

# ---------------------------------------------------------------------------
hdr("RE05-F  *** THE BOUNDARY BYPASS ***  /api/repertoire/tree calls the same writer")
# ---------------------------------------------------------------------------
# `get_repertoire_tree()` -> `get_candidate_moves_2000(pos_fen, color=repertoire)`
# for EVERY opponent position in the tree (backend/repertoire.py:1699-1701).
# So a guard placed on `explore_position_2000` protects nothing.
print(subprocess.run(["sed", "-n", "1691,1702p", "backend/repertoire.py"],
                     capture_output=True, text=True).stdout)

import atk3_lib  # noqa: E402

TREE_MALFORMED = [
    ("moves is a dict", dict(REAL, moves={"e4": 1})),
    ("a move is a string", dict(REAL, moves=["e4"])),
    ("a move has string counts", dict(REAL, moves=[{"uci": "e2e4", "san": "e4",
                                                    "white": "x", "draws": None, "black": -5}])),
    ("a move has NO 'san' key", dict(REAL, moves=[{"uci": "e2e4", "white": 1,
                                                  "draws": 0, "black": 0}])),
    ("root is a list", [1, 2, 3]),
    ("Infinity counts", dict(REAL, white=float("inf"))),
]


def build_tree():
    """One prepared white move 1.e4, so the tree has exactly one opponent position."""
    wipe_games()
    wipe_explorer()
    atk3_lib.wipe_repertoire()
    rm = RepertoireManager.get_instance()
    return rm.add_repertoire_move("white", START4, "e2e4", is_user_move=True,
                                  popularity_pct=30.0, win_rate=55.0)


for label, payload in TREE_MALFORMED:
    build_tree()
    RepertoireManager._instance = None
    atk3_lib.restore_network()
    serve_explorer(payload)
    r = cl.get("/api/repertoire/tree", params={"color": "white"})
    rows = cache_rows()
    print("   %-30s HTTP %-4s cached=%s%s" % (
        label, r.status_code, bool(rows), "  <-- 500" if r.status_code >= 500 else ""))
    if r.status_code >= 500:
        verdict("GET /api/repertoire/tree 500s on explorer payload %r" % label, False,
                "HTTP %s %s  (the api.py explore guard does not cover this path)" % (
                    r.status_code, r.text[:60]))
    if rows and "Infinity" in label:
        atk3_lib.restore_network()          # the network is now a tripwire
        RepertoireManager._instance = None
        r2 = cl.get("/api/repertoire/tree", params={"color": "white"})
        print("      2nd request, network dead -> HTTP %s %s" % (r2.status_code, r2.text[:70]))
        if r2.status_code >= 400:
            verdict("Infinity poison persists via /api/repertoire/tree", False,
                    "1st HTTP %s -> 2nd HTTP %s %s" % (r.status_code, r2.status_code, r2.text[:60]))
atk3_lib.restore_network()
atk3_lib.wipe_repertoire()
wipe_games()
wipe_explorer()

# ---------------------------------------------------------------------------
hdr("SUMMARY")
# ---------------------------------------------------------------------------
for name, status, detail in RESULTS:
    print("  %-8s %-52s %s" % (status, name, detail))
print("\n%d/%d hold" % (sum(1 for r in RESULTS if r[1] == "HOLDS"), len(RESULTS)))
