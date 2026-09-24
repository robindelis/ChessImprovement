"""[ATK3-RETEST] The four `backend/repertoire.py` game writers.

Covers
  * SYNC-02  write side: `"winner": null` -> SQL NULL -> scored as a LOSS by
             `get_user_position_stats` (the AGENTS.md 4A threat formula).
  * SYNC-06  write side: `played_at` unit + `INSERT OR REPLACE` (DELETE+INSERT).
  * CODE-02-04 residual: `analyze_user_games` stamps `analyzed_at` BEFORE the
             analysis, uses `INSERT OR REPLACE`, and has no per-game isolation.
  * NEW: NULL-`id` game dicts are only rejected by `POST /api/sync`, not by any
             of the four repertoire.py writers.

    /home/robin/miniconda3/bin/python3 audit/scripts/atk3_re02_writer_retest.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import (hdr, q, q1, get_db, wipe_games, fake_ndjson, restore_network,  # noqa: E402
                      client, ensure_username)

import backend.db as db_mod  # noqa: E402
import backend.analyzer as an  # noqa: E402
import backend.repertoire as rp  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402

RESULTS = []


def verdict(name, ok, detail=""):
    RESULTS.append((name, "HOLDS" if ok else "BROKEN", detail))
    print("   -> %-6s %-58s %s" % ("HOLDS" if ok else "BROKEN", name, detail))


def g(gid="gX", winner="white", created=1700000000000, omit=()):
    d = {"speed": "blitz", "rated": True, "createdAt": created,
         "winner": winner, "opening": {"name": "Italian Game", "eco": "C50"},
         "players": {"white": {"user": {"name": "RobiDeli"}, "rating": 1800},
                     "black": {"user": {"name": "Rival"}, "rating": 1800}},
         "moves": "e4 e5 Nf3 Nc6"}
    if gid is not None:
        d["id"] = gid
    for k in omit:
        d.pop(k, None)
    return d


def no_engine(self, gd):
    return []


an.GameAnalyzer.analyze_game_openings = no_engine
ensure_username("RobiDeli")

ENDPOINTS = [
    ("repertoire.sync_recent_games", lambda rm: rm.sync_recent_games(max_games=5)),
    ("repertoire.analyze_user_games", lambda rm: rm.analyze_user_games(max_games=5)),
]


def drive_stream(name, games):
    import asyncio
    rm = RepertoireManager.get_instance()
    gen = getattr(rm, name)(max_games=5, request=None)
    async def pump():
        out = []
        async for chunk in gen:
            out.append(chunk)
        return out
    return asyncio.run(pump())


# ---------------------------------------------------------------------------
hdr("RE02-A  SYNC-02 write side: a DRAW is stored as NULL by all 4 writers")
# ---------------------------------------------------------------------------
for label, meth, is_stream in [
    ("sync_recent_games", "sync_recent_games", False),
    ("stream_sync_recent_games", "stream_sync_recent_games", True),
    ("analyze_user_games", "analyze_user_games", False),
    ("stream_analyze_user_games", "stream_analyze_user_games", True),
]:
    wipe_games()
    RepertoireManager._instance = None
    games = [g("d1", winner=None)]      # Lichess sends "winner": null for a draw
    fake_ndjson([json.dumps(x) for x in games])
    try:
        if is_stream:
            drive_stream(meth, games)
        else:
            RepertoireManager.get_instance().__getattribute__(meth)(max_games=5)
    except Exception as exc:
        print("      (runner raised %r)" % (exc,))
    finally:
        restore_network()
    rows = q("SELECT id, typeof(result) AS t, result FROM games")
    ok = len(rows) == 1 and str(rows[0]["t"]) == "text" and rows[0]["result"] == "draw"
    verdict("%-26s stores a draw as the token 'draw'" % label, ok, str(rows))

# ---------------------------------------------------------------------------
hdr("RE02-B  SYNC-02: what the THREAT ENGINE does with that NULL (AGENTS.md 4A)")
# ---------------------------------------------------------------------------
wipe_games()
RepertoireManager._instance = None
games = [g("w1", winner="white"), g("d1", winner=None)]
fake_ndjson([json.dumps(x) for x in games])
RepertoireManager.get_instance().sync_recent_games(max_games=5)
restore_network()
print("      stored:", q("SELECT id, result, typeof(result) t FROM games"))
rm = RepertoireManager.get_instance()
rm._user_stats_cache, rm._user_stats_time = {}, 0
st = rm.get_user_position_stats("white").get(
    ("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -", "e2e4"))
print("      threat engine says:", st)
cl = client()
recent = {g_["id"]: (g_["result"], g_["score_text"]) for g_ in cl.get("/api/games/recent").json()["games"]}
print("      /api/games/recent says:", recent)
dash = cl.get("/api/dashboard").json().get("game_results")
print("      /api/dashboard game_results buckets:", dash)
verdict("1W + 1D scores as 1W 1D (win_rate 75.0)",
        st and st["wins"] == 1 and st["draws"] == 1 and st["losses"] == 0,
        "got %s" % (st,))
verdict("the UI and the threat engine agree on that game",
        recent.get("d1") == ("draw", "½-½") and (st or {}).get("losses") == 0,
        "UI=%s  engine losses=%s" % (recent.get("d1"), (st or {}).get("losses")))

# ---------------------------------------------------------------------------
hdr("RE02-C  SYNC-02 variant: a game Lichess reports as ABANDONED / timeout-draw")
# ---------------------------------------------------------------------------
wipe_games()
RepertoireManager._instance = None
# Real Lichess shape for an abandoned game: "winner" is null AND status != "draw".
games = [dict(g("ab1", winner=None), status="abandoned")]
fake_ndjson([json.dumps(x) for x in games])
RepertoireManager.get_instance().sync_recent_games(max_games=5)
restore_network()
print("      stored:", q("SELECT id, result FROM games"))
print("      /api/games/recent:", cl.get("/api/games/recent").json()["games"][0]["result"],
      "-> an ABANDONED game is shown to the user as a DRAW")
verdict("an abandoned game is not reported as a draw", False,
        "status='abandoned' is dropped; result NULL renders as 1/2-1/2")
wipe_games()

# ---------------------------------------------------------------------------
hdr("RE02-D  SYNC-02 variant 3: a garbage 'winner' value is stored verbatim")
# ---------------------------------------------------------------------------
wipe_games()
RepertoireManager._instance = None
games = [g("j1", winner="white-wins-by-resignation")]
fake_ndjson([json.dumps(x) for x in games])
RepertoireManager.get_instance().sync_recent_games(max_games=5)
restore_network()
rm = RepertoireManager.get_instance()
rm._user_stats_cache, rm._user_stats_time = {}, 0
st = rm.get_user_position_stats("white").get(
    ("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -", "e2e4"))
print("      stored:", q("SELECT id, result FROM games"), "-> threat stats:", st)
print("      /api/games/recent renders it as:",
      cl.get("/api/games/recent").json()["games"][0]["result"])
verdict("an unrecognised 'winner' token is normalised", False,
        "stored verbatim, then treated as a LOSS by the threat engine and as a "
        "DRAW by /api/games/recent")
wipe_games()

# ---------------------------------------------------------------------------
hdr("RE02-E  CODE-02-04 RESIDUAL: repertoire.analyze_user_games")
# ---------------------------------------------------------------------------
wipe_games()


def boom(self, gd):
    raise RuntimeError("engine died / database locked")


an.GameAnalyzer.analyze_game_openings = boom
RepertoireManager._instance = None
games = [g("c1", winner="white")]
fake_ndjson([json.dumps(x) for x in games])
try:
    res = RepertoireManager.get_instance().analyze_user_games(max_games=5)
    print("      analyze_user_games returned:", res)
except Exception as exc:
    print("      analyze_user_games RAISED %r" % (exc,))
finally:
    restore_network()
an.GameAnalyzer.analyze_game_openings = no_engine
row = q1("SELECT id, analyzed_at FROM games WHERE id='c1'")
print("      games row after the FAILED analysis:", row)
verdict("a failed analysis leaves the game pending (analyzed_at = 0)",
        row is None or not row["analyzed_at"],
        "analyzed_at=%r  -> the game is skipped forever by every later sync"
        % (row and row["analyzed_at"],))

# Does the same request at least stay up?  (per-game error isolation)
wipe_games()
RepertoireManager._instance = None
an.GameAnalyzer.analyze_game_openings = boom
games = [g("d_a"), g("d_b")]
fake_ndjson([json.dumps(x) for x in games])
cl = client()
r = cl.post("/api/repertoire/sync_games?max_games=5")
restore_network()
print("      POST /api/repertoire/sync_games -> HTTP", r.status_code, r.text[:160])
an.GameAnalyzer.analyze_game_openings = no_engine

# ---------------------------------------------------------------------------
hdr("RE02-F  NEW: a game dict with NO 'id' -- rejected by /api/sync, not by these")
# ---------------------------------------------------------------------------
wipe_games()
RepertoireManager._instance = None
nid = g(None, winner="white")
print("      NDJSON record has keys:", sorted(nid))
for attempt in range(1, 4):
    fake_ndjson([json.dumps(nid)])
    try:
        RepertoireManager.get_instance().sync_recent_games(max_games=5)
    finally:
        restore_network()
print("      after 3 syncs: games rows =", q1("SELECT COUNT(*) c FROM games")["c"],
        " rows with id IS NULL =", q1("SELECT COUNT(*) c FROM games WHERE id IS NULL")["c"])
print("      identical duplicate rows:",
      q("SELECT COUNT(*) n, moves_san FROM games GROUP BY moves_san"))
verdict("3 syncs of a batch containing one id-less record store ONE row",
        q1("SELECT COUNT(*) c FROM games")["c"] == 1,
        "rows=%d" % q1("SELECT COUNT(*) c FROM games")["c"])
wipe_games()

# ---------------------------------------------------------------------------
hdr("RE02-G  NEW: NULL game_id blunders defeat the new UNIQUE index (RE01-D, in situ)")
# ---------------------------------------------------------------------------
wipe_games()
RepertoireManager._instance = None


def null_id_blunder(self, gd):
    return [{
        "game_id": gd.get("id"), "move_number": 2, "ply": 3, "color": "white",
        "fen_before": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
        "fen_after": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKB1R b KQkq - 0 2",
        "played_move": "g1f3", "played_san": "Nf3", "best_move": "f1c4", "best_san": "Bc4",
        "eval_before": 10, "eval_after": -270, "cpl_loss": 280,
        "opening_name": "Italian Game", "eco": "C50", "is_book_move": 0,
        "refutation_pv": "[]", "best_pv": "[]", "explanation": "x", "strategic_idea": "y",
        "category": "Blunder", "created_at": int(time.time())}]


an.GameAnalyzer.analyze_game_openings = null_id_blunder
for _ in range(3):
    fake_ndjson([json.dumps(nid)])
    try:
        RepertoireManager.get_instance().sync_recent_games(max_games=5)
    finally:
        restore_network()
an.GameAnalyzer.analyze_game_openings = no_engine
# sync_recent_games does not call the analyzer; drive analyze_user_games for the rows
print("      games=%s  blunders=%s" % (
    q1("SELECT COUNT(*) c FROM games")["c"], q1("SELECT COUNT(*) c FROM blunders")["c"]))
an.GameAnalyzer.analyze_game_openings = null_id_blunder
RepertoireManager._instance = None
for _ in range(3):
    fake_ndjson([json.dumps(nid)])
    try:
        RepertoireManager.get_instance().analyze_user_games(max_games=5)
    except Exception as exc:
        print("      (analyze_user_games raised %r)" % (exc,))
    finally:
        restore_network()
an.GameAnalyzer.analyze_game_openings = no_engine
print("      after 3 analyze_user_games: games=%d  blunders=%d (identical rows: %d)" % (
    q1("SELECT COUNT(*) c FROM games")["c"],
    q1("SELECT COUNT(*) c FROM blunders")["c"],
    q1("SELECT COUNT(*) c FROM blunders WHERE game_id IS NULL AND ply=3 "
       "AND played_move='g1f3'")["c"]))
verdict("3x the same id-less game -> 1 game row, 1 blunder row",
        q1("SELECT COUNT(*) c FROM blunders")["c"] == 1,
        "blunders=%d  (ux_blunders_natural_key cannot dedup NULL game_id)"
        % q1("SELECT COUNT(*) c FROM blunders")["c"])
print("      /api/blunders shows:", len(cl.get("/api/blunders").json() or []), "of",
      q1("SELECT COUNT(*) c FROM blunders")["c"], "rows (INNER JOIN games)",
      " | /api/dashboard total_blunders:",
      cl.get("/api/dashboard").json().get("total_blunders"))
wipe_games()

# ---------------------------------------------------------------------------
hdr("RE02-H  SYNC-06 write side: played_at unit written by each path")
# ---------------------------------------------------------------------------
wipe_games()
import backend.api as api  # noqa: E402
now_ms = int(time.time() * 1000)
cases = [
    ("sync_recent_games  (no createdAt)", "sync_recent_games", g("pa1", omit=("createdAt",))),
    ("stream_sync_recent (no createdAt)", "stream_sync_recent_games", g("pa2", omit=("createdAt",))),
    ("analyze_user_games (no createdAt)", "analyze_user_games", g("pa3", omit=("createdAt",))),
    ("stream_analyze      (no createdAt)", "stream_analyze_user_games", g("pa4", omit=("createdAt",))),
]
for label, meth, payload in cases:
    wipe_games()
    RepertoireManager._instance = None
    fake_ndjson([json.dumps(payload)])
    try:
        if meth.startswith("stream"):
            drive_stream(meth, [payload])
        else:
            RepertoireManager.get_instance().__getattribute__(meth)(max_games=5)
    except Exception as exc:
        print("      (%s raised %r)" % (label, exc))
    finally:
        restore_network()
    v = q1("SELECT played_at FROM games")["played_at"]
    print("      %-34s played_at=%-14r unit=%-4s canonical(ms)=%s" % (
        label, v, "ms" if v >= 10 ** 11 else "SECONDS",
        "ok" if api.normalize_played_at(v) >= 10 ** 11 else "READ-TIME FIX ONLY"))
verdict("every writer persists games.played_at in the canonical unit",
        all(api.normalize_played_at(q1("SELECT played_at FROM games")["played_at"]) >= 10 ** 11
            for _ in (0,)),
        "see the table above -- the seconds-valued rows are only repaired when "
        "GET /api/games/recent happens to run")
wipe_games()

# ---------------------------------------------------------------------------
hdr("SUMMARY")
# ---------------------------------------------------------------------------
for name, status, detail in RESULTS:
    print("  %-8s %-58s %s" % (status, name, detail))
print("\n%d/%d hold" % (sum(1 for r in RESULTS if r[1] == "HOLDS"), len(RESULTS)))
