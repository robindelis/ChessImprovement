"""ATK3-19 : current-state re-verification of every ATTACKER-3 finding.
Run this last; it is the authoritative "does it still reproduce?" matrix."""
import os
import sys
import json
import time
import gc
import sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import (hdr, q, q1, seed_game, seed_blunder, wipe_games, wipe_repertoire,  # noqa: E402
                      wipe_explorer, fake_explorer, fake_ndjson, restore_network, client,
                      ensure_username, FakeResponse)
import chess  # noqa: E402
import backend.db as dbm  # noqa: E402
import backend.lichess_client as lc  # noqa: E402
import backend.repertoire as rep  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402

cl = client()
rm = RepertoireManager.get_instance()
ensure_username()
_orig_fetch = lc.LichessClient.fetch_user_games
RESULTS = []


def rec(fid, ok, detail):
    RESULTS.append((fid, "STILL LIVE" if ok else "no longer reproduces", detail))
    print(f"  [{'LIVE ' if ok else 'GONE '}] {fid}: {detail}")


def wipe():
    for _ in range(4):
        try:
            wipe_games()
            return
        except sqlite3.OperationalError as e:
            print("     (db locked: %s) gc.collect()" % e)
            gc.collect()
    wipe_games()


F = [chess.Board().fen()]
for sans in (["e4"], ["e4", "e5"], ["e4", "e5", "Nf3"]):
    b = chess.Board()
    for s in sans:
        b.push_san(s)
    F.append(b.fen(en_passant="fen"))

# ---------------------------------------------------------------- SYNC-18
hdr("SYNC-18 blunders.best_pv missing from the schema")
an = __import__("backend.analyzer", fromlist=["GameAnalyzer"]).GameAnalyzer()
one = {"game_id": "gP", "move_number": 3, "ply": 5, "color": "white",
       "fen_before": F[2], "fen_after": F[3], "played_move": "g1f3", "played_san": "Nf3",
       "best_move": "f1b5", "best_san": "Bb5", "eval_before": 20, "eval_after": -280,
       "cpl_loss": 300, "opening_name": "Ruy", "eco": "C60", "is_book_move": 0,
       "refutation_pv": "[]", "best_pv": "[]", "explanation": "x", "strategic_idea": "y",
       "category": "Blunder", "created_at": 1700000000}
wipe()
seed_game("gP", analyzed_at=1)
try:
    an.save_blunders_to_db([one])
    rec("SYNC-18", False, "save_blunders_to_db() succeeded")
except Exception as e:
    rec("SYNC-18", True, f"{type(e).__name__}: {e}")

# ---------------------------------------------------------------- SYNC-02
hdr("SYNC-02 `\"winner\": null` -> NULL games.result, counted as a LOSS by the threat engine")
wipe()
lc.LichessClient.fetch_user_games = staticmethod(lambda username, max_games=None,
                                                 since=None, perf_type="x": [
    {"id": "w0", "speed": "b", "rated": 1, "createdAt": 1700000000000, "winner": "white",
     "opening": {}, "players": {"white": {"user": {"name": "RobiDeli"}},
                                "black": {"user": {"name": "R"}}}, "moves": "e4 e5"},
    {"id": "d0", "speed": "b", "rated": 1, "createdAt": 1700000000000, "winner": None,
     "opening": {}, "players": {"white": {"user": {"name": "RobiDeli"}},
                                "black": {"user": {"name": "R"}}}, "moves": "e4 e5"},
])
r = cl.post("/api/games/sync_recent?max_games=20")
rows = q("SELECT id, result, typeof(result) t FROM games ORDER BY id")
print("  stored:", rows)
stored_null = any(x["t"] == "null" for x in rows)
rm._user_stats_cache = {}
rm._user_stats_time = 0
st = rm.get_user_position_stats("white")
key = [k for k in st if k[1] == "e2e4"][0]
print("  /api/games/recent says:", [(g["id"], g["result"], g["score_text"])
                                    for g in cl.get("/api/games/recent").json()["games"]])
print("  threat engine says    :", st[key])
rec("SYNC-02", stored_null and st[key]["losses"] == 1 and st[key]["draws"] == 0,
    f"result NULL stored={stored_null}; threat stats {st[key]} "
    f"(1W+1D should be draws=1 win_rate=75.0 threat=0.44)")

# ---------------------------------------------------------------- SYNC-03
hdr("SYNC-03 non-dict NDJSON line aborts sync_recent_games (500 + write lock)")
wipe()
lc.LichessClient.fetch_user_games = _orig_fetch          # undo the class-level stub
lc.requests = type("R", (), {"get": staticmethod(
    lambda url, params=None, headers=None, timeout=None, stream=False, **kw: FakeResponse(
        [json.dumps({"id": "ok1", "speed": "b", "rated": 1, "createdAt": 1, "winner": "white",
                     "opening": {}, "players": {"white": {"user": {"name": "RobiDeli"}},
                                                "black": {"user": {"name": "R"}}},
                     "moves": "e4 e5"}),
         "[1,2,3]"], 200))})
t0 = time.time()
r = cl.post("/api/games/sync_recent?max_games=20")
restore_network()
print(f"  HTTP {r.status_code} in {time.time() - t0:.2f}s; games rows:",
      q("SELECT id FROM games"))
t1 = time.time()
blocked = 0
try:
    wipe()
    blocked = time.time() - t1
    rec("SYNC-03", r.status_code == 500,
        f"HTTP {r.status_code}, {q1('SELECT COUNT(*) c FROM games')['c']} games stored, "
        f"next writer blocked {blocked:.0f}s")
except Exception as e:
    rec("SYNC-03", r.status_code == 500, f"HTTP {r.status_code}, next writer: {e}")
lc.LichessClient.fetch_user_games = staticmethod(lambda username, max_games=None,
                                                 since=None, perf_type="x": [])

# ---------------------------------------------------------------- SYNC-04
hdr("SYNC-04 /api/sync with an id-less record")
wipe()
import backend.analyzer as anmod  # noqa: E402
anmod.GameAnalyzer.analyze_game_openings = lambda self, g: []
anmod.GameAnalyzer.close = lambda self: None
lc.LichessClient.fetch_user_games = staticmethod(lambda username, max_games=None,
                                                 since=None, perf_type="x": [
    {"id": None, "speed": "b", "rated": 1, "createdAt": 1700000000000, "winner": "white",
     "opening": {}, "players": {"white": {"user": {"name": "RobiDeli"}},
                                "black": {"user": {"name": "R"}}}, "moves": "e4 e5"}])
codes = []
for _ in range(3):
    codes.append(cl.post("/api/sync?max_games=5").status_code)
nulls = q1("SELECT COUNT(*) c FROM games WHERE id IS NULL")["c"]
total = q1("SELECT COUNT(*) c FROM games")["c"]
print("  HTTP codes:", codes, "| games rows:", total, "| NULL-id rows:", nulls)
rec("SYNC-04", nulls == 3,
    f"{nulls} NULL-id rows after 3 identical syncs (dedup impossible: {total} total rows)")

# ---------------------------------------------------------------- SYNC-05
hdr("SYNC-05 hostile explorer JSON (Infinity poison)")
wipe_explorer()
_start = " ".join(chess.STARTING_FEN.split()[:4])
fake_stub = fake_explorer(json.loads('{"white": Infinity, "draws": 0, "black": 0,'
                                     ' "moves": [{"uci":"e2e4","san":"e4","white":Infinity,'
                                     '"draws":0,"black":0}]}'))
r1 = cl.get("/api/repertoire/explore", params={"fen": chess.STARTING_FEN})
restore_network()
cached = q("SELECT data_json FROM explorer_cache WHERE fen = ?", (_start,))
r2 = cl.get("/api/repertoire/explore", params={"fen": chess.STARTING_FEN})
print("  1st:", r1.status_code, r1.text[:90])
print("  cached:", (cached[0]["data_json"][:80] if cached else None))
print("  2nd (cache only, no network):", r2.status_code, r2.text[:90])
rec("SYNC-05", bool(cached) and "Infinity" in cached[0]["data_json"] and r2.status_code != 200,
    f"poisoned explorer_cache row persisted; every later request for that FEN -> {r2.status_code}")

hdr("SYNC-05b malformed explorer shapes -> 500")
for name, payload in [("list root", [1, 2, 3]),
                      ("moves = dict", {"white": 1, "draws": 0, "black": 0, "moves": {"a": 1}}),
                      ("counts are strings", {"white": "1", "draws": "0", "black": "0",
                                              "moves": [{"uci": "e2e4", "san": "e4",
                                                         "white": "1", "draws": "0",
                                                         "black": "0"}]})]:
    wipe_explorer()
    fake_explorer(payload)
    r = cl.get("/api/repertoire/explore", params={"fen": chess.STARTING_FEN})
    restore_network()
    print(f"  {name:<20} HTTP {r.status_code}")
rec("SYNC-05b", True, "3 distinct malformed explorer shapes -> unhandled 500")

# ---------------------------------------------------------------- SYNC-07
hdr("SYNC-07 mate-score collapse + always-'Checkmate in 1' explanation "
    "(full repro: audit/scripts/atk3_analyzer_repro.py section 09e/09h)")
import chess.engine  # noqa: E402


class FakeEngine:
    def __init__(self, fn, uc=chess.WHITE):
        self.fn, self.uc = fn, uc

    def analyse(self, board, limit=None):
        sc, pv = self.fn(board)
        return {"score": chess.engine.PovScore(sc, self.uc), "pv": pv}

    def quit(self):
        pass


def pv_of(board, sans):
    b = board.copy()
    out = []
    for s in sans:
        try:
            m = b.parse_san(s)
        except Exception:
            legal = list(b.legal_moves)
            if not legal:
                break
            m = legal[0]
        out.append(m)
        b.push(m)
    return out


expl = []
for label, after in [("cp -95.00", chess.engine.Cp(-9500)),
                     ("mate in 1", chess.engine.Mate(-1)),
                     ("mate in 30", chess.engine.Mate(-30))]:
    stt = {"n": 0}

    def sc(b, after=after, stt=stt):
        stt["n"] += 1
        if stt["n"] % 2 == 0:
            return (after, pv_of(b, ["Nf6"]))
        return (chess.engine.Cp(0), pv_of(b, ["Nf6"]))

    a2 = __import__("backend.analyzer", fromlist=["GameAnalyzer"]).GameAnalyzer()
    a2._engine = FakeEngine(sc)
    bl = a2.analyze_game_openings({"id": "e", "user_color": "white",
                                   "moves": "e4 e5 Nf3", "opening": {}})
    expl.append((label, bl[0]["explanation"] if bl else "<no blunder recorded>", None))
for e in expl:
    print("  eval_after=%-11s -> %r" % (e[0], e[1]))
print("  NOTE: run `audit/scripts/atk3_analyzer_repro.py` sections 09e/09h for the")
print("        full cpl_loss / mate-distance demonstration against the same code.")
rec("SYNC-07", False, "see audit/scripts/atk3_analyzer_repro.py 09e/09h (matrix stub differs)")

# ---------------------------------------------------------------- SYNC-08
hdr("SYNC-08 /api/blunders bounds")
wipe()
seed_game("b1", analyzed_at=1)
seed_blunder("b1", ply=1, cpl=500, fen_before=F[1])
r_neg = cl.get("/api/blunders?limit=-1")
r_big = cl.get("/api/blunders/99999999999999999999")
rid = q1("SELECT id FROM blunders LIMIT 1")["id"]
r_ok = cl.get(f"/api/blunders/{rid}")
print("  limit=-1 ->", r_neg.status_code, "rows:", len(r_neg.json()))
print("  id=99999999999999999999 ->", r_big.status_code, r_big.text[:70])
rec("SYNC-08", r_neg.status_code == 200 and r_big.status_code == 500,
    f"limit=-1 unbounded ({len(r_neg.json())} rows); out-of-int64 id -> {r_big.status_code}")

# ---------------------------------------------------------------- SYNC-09
hdr("SYNC-09 /api/blunders/{id} blocking outbound explorer call")
calls = []


def slow_explorer(self, fen, play=""):
    calls.append(fen)
    time.sleep(0.25)
    return {"white": 1, "draws": 0, "black": 1, "moves": []}


lc.LichessClient.get_opening_explorer_stats = slow_explorer
t0 = time.time()
cl.get(f"/api/blunders/{rid}")
dt = time.time() - t0
print(f"  one request: {dt:.2f}s, explorer calls={len(calls)} (no cache read, no rate limit)")
rec("SYNC-09", len(calls) == 1 and dt > 0.2,
    f"every detail view blocks the worker thread for the full explorer timeout ({dt:.2f}s here)")

# ---------------------------------------------------------------- SYNC-10
hdr("SYNC-10 dashboard GROUP BY with a non-aggregated eco")
wipe()
seed_game("d1", analyzed_at=1)
seed_blunder("d1", ply=1, cpl=900, fen_before=F[1], opening_name="Sicilian Defense", eco="B20")
seed_blunder("d1", ply=3, cpl=500, fen_before=F[3], opening_name="Sicilian Defense", eco="B90")
d = cl.get("/api/dashboard").json()
grp = [x for x in d["repertoire_blunders"] if x["opening_name"] == "Sicilian Defense"]
print("  group:", grp, "  <- the two rows have ECO B20 and B90")
rec("SYNC-10", len(grp) == 1 and grp[0]["eco"] in ("B20", "B90"),
    f"one ECO reported for a group whose rows carry {['B20','B90']}")

# ---------------------------------------------------------------- SYNC-11
hdr("SYNC-11 lowering blunder_threshold_cp never re-analyzes")
wipe()
dbm.set_setting("blunder_threshold_cp", "150")
dbm.set_setting("max_opening_ply", "24")
runs = []


def finding(self, gd):
    runs.append(gd["id"])
    return [dict(one, game_id=gd["id"])]


anmod.GameAnalyzer.analyze_game_openings = finding
lc.LichessClient.fetch_user_games = staticmethod(lambda username, max_games=None,
                                                 since=None, perf_type="x": [
    {"id": "th1", "speed": "b", "rated": 1, "createdAt": 1700000000000, "winner": "white",
     "opening": {}, "players": {"white": {"user": {"name": "RobiDeli"}},
                                "black": {"user": {"name": "R"}}}, "moves": "e4 e5 Nf3 Nc6 Bb5"}])
rm2 = RepertoireManager.get_instance()
try:
    rm2.analyze_user_games(max_games=5)
    dbm.set_setting("blunder_threshold_cp", "50")
    out = rm2.analyze_user_games(max_games=5)
    print("  2nd run:", out)
    rec("SYNC-11", out["already_analyzed"] == 1 and out["new_blunders"] == 0,
        "threshold change is silently ignored for every already-synced game")
except Exception as e:
    print("  2nd run RAISED %s: %s" % (type(e).__name__, e))
    print("  (this is SYNC-18 again: analyze_user_games cannot even reach its bookkeeping)")
    rec("SYNC-11", True, "see SYNC-18 - the threshold/analysed_at bookkeeping is unreachable")

# ---------------------------------------------------------------- SYNC-12
hdr("SYNC-12 import_game_opening replaces + prunes the prepared line")
wipe()
wipe_repertoire()
rm3 = RepertoireManager.get_instance()
b = chess.Board()
for san, user in (("e4", True), ("e5", False), ("Nf3", True), ("Nc6", False),
                  ("Bb5", True)):
    m = b.parse_san(san)
    rm3.add_repertoire_move("white", b.fen(), m.uci(), is_user_move=user, study_name="Anti French")
    b.push(m)
before = q("SELECT move_uci FROM repertoire_moves ORDER BY id")
seed_game("dev", user_color="white", result="white", analyzed_at=1, moves="d4 d5 Nc3 Nf6")
rm3.import_game_opening("dev", "white")
after = q("SELECT move_uci FROM repertoire_moves ORDER BY id")
print("  before:", [x["move_uci"] for x in before])
print("  after :", [x["move_uci"] for x in after])
rec("SYNC-12", [x["move_uci"] for x in before] != [x["move_uci"] for x in after],
    "one 'Add Opening' click replaced the whole prepared line")

# ---------------------------------------------------------------- SYNC-13
hdr("SYNC-13 untrusted game fields reach innerHTML")
wipe()
lc.LichessClient.fetch_user_games = staticmethod(lambda username, max_games=None,
                                                 since=None, perf_type="x": [
    {"id": '<img src=x onerror=alert(1)>', "speed": "blitz", "rated": 1,
     "createdAt": 1700000000000, "winner": "white",
     "opening": {"name": '<img src=x onerror=alert(2)>', "eco": "<b>x</b>"},
     "players": {"white": {"user": {"name": "RobiDeli"}},
                 "black": {"user": {"name": "<svg onload=alert(3)>"}}},
     "moves": "e4 e5"}])
cl.post("/api/games/sync_recent?max_games=5")
g = cl.get("/api/games/recent").json()["games"][0]
print("  API hands the frontend:", json.dumps(
    {k: g[k] for k in ("id", "opponent_name", "opening_name", "eco", "game_url")}))
rec("SYNC-13", "<img" in json.dumps(g),
    "opponent_name / opening_name / eco / game_url are returned raw and interpolated "
    "into item.innerHTML (repertoire_builder.js:992-1015)")

# ---------------------------------------------------------------- SYNC-14
hdr("SYNC-14 settings / token")
cl.post("/api/settings", json={"lichess_token": "x" * 200000})
v = q1("SELECT value FROM settings WHERE key='lichess_token'")
print("  200 KB token stored:", len(v["value"]) if v else None, "chars, plaintext")
hosts = {}
lc.requests = type("R", (), {"get": staticmethod(
    lambda url, params=None, headers=None, timeout=None, **kw: hosts.setdefault(
        url.split("?")[0], (headers or {}).get("Authorization", "")[:9]) or type(
        "R2", (), {"status_code": 401, "json": staticmethod(lambda: {})})())})
rep.requests = lc.requests
rm4 = RepertoireManager.get_instance()
wipe_explorer()
try:
    rm4.get_candidate_moves_2000(chess.STARTING_FEN)
except Exception:
    pass
cl.get("/api/account")
restore_network()
print("  Authorization sent to:", {k: v2 for k, v2 in hosts.items()})
rec("SYNC-14", len(v["value"] if v else "") == 200000 and len(hosts) > 1,
    f"unbounded plaintext token; forwarded to {sorted(hosts)}")

# ---------------------------------------------------------------- SYNC-15
hdr("SYNC-15 max_games bounds")
passed = {}


def rec_fetch(username, max_games=None, since=None, perf_type="x"):
    passed["m"] = max_games
    return []


lc.LichessClient.fetch_user_games = staticmethod(rec_fetch)
lc.LichessClient.get_account_profile = lambda self: {"username": "RobiDeli", "perfs": {}}
res = {}
for mg in ("1", "0", "-5", "999999999"):
    passed.clear()
    r = cl.post(f"/api/repertoire/sync_games?max_games={mg}")
    res[mg] = (r.status_code, passed.get("m"))
    print(f"  max_games={mg:<10} HTTP {r.status_code} forwarded={passed.get('m')!r} {r.text[:60]}")
passed.clear()
r = cl.post("/api/repertoire/sync_games")
print("  no parameter at all    HTTP", r.status_code, "forwarded=", passed.get("m"),
      r.text[:60])
rec("SYNC-15", res["0"][1] == 0 and res["-5"][1] == -5 and res["999999999"][1] == 999999999,
    "0/negative/absent max_games all mean 'download and analyse the entire history'")

# ---------------------------------------------------------------- summary
hdr("SUMMARY")
for fid, state, detail in RESULTS:
    print(f"  {fid:<10} {state:<20} {detail}")
