"""ATK3-11 : /api/blunders filters + bounds, /api/blunders/{id}, /api/dashboard correctness,
             /api/repertoire/import_game_opening validation."""
import os
import sys
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import (hdr, wipe_games, wipe_repertoire, q, q1, seed_game, seed_blunder,  # noqa: E402
                      fake_explorer, restore_network, client)
import chess  # noqa: E402
import backend.repertoire as rep  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402

cl = client()
rm = RepertoireManager.get_instance()
rm.get_explorer_game_count = lambda fen, ratings="2000,2200,2500": 100000

F = [chess.Board().fen()]
for sans in (["e4"], ["e4", "e5"], ["e4", "e5", "Nf3"], ["e4", "e5", "Nf3", "Nc6"],
             ["e4", "e5", "Nf3", "Nc6", "Bc4"]):
    b = chess.Board()
    for s in sans:
        b.push_san(s)
    F.append(b.fen(en_passant="fen"))


def seed_all():
    wipe_games()
    seed_game("gW", user_color="white", result="white", analyzed_at=1)
    seed_game("gB", user_color="black", result="black", analyzed_at=1)
    seed_blunder("gW", ply=1, move_number=1, color="white", cpl=100, eco="A10",
                 fen_before=F[0], best_move="e2e4", played_move="d2d4")
    seed_blunder("gW", ply=3, move_number=2, color="white", cpl=900, eco="B20",
                 fen_before=F[2], best_move="e2e4", played_move="d2d4")
    seed_blunder("gW", ply=5, move_number=3, color="white", cpl=500, eco="B20",
                 fen_before=F[4], best_move="g1f3", played_move="b1c3")
    seed_blunder("gB", ply=2, move_number=1, color="black", cpl=700, eco="C50",
                 fen_before=F[1], best_move="e7e5", played_move="d7d5")


hdr("ATK3-11a — /api/blunders filtering")
seed_all()
for qs in ["", "?color=white", "?color=black", "?color=WHITE", "?color=purple",
           "?eco=B2", "?eco=b2", "?min_loss=0", "?min_loss=500", "?min_loss=99999",
           "?category=Blunder", "?category=nope", "?color=white&min_loss=500"]:
    r = cl.get("/api/blunders" + qs)
    try:
        j = r.json()
        print(f"  {qs or '(none)':<32} HTTP {r.status_code} rows={len(j)} "
              f"{[(b['color'], b['cpl_loss']) for b in j]}")
    except Exception:
        print(f"  {qs or '(none)':<32} HTTP {r.status_code} {r.text[:90]}")

hdr("ATK3-11b — /api/blunders limit is UNBOUNDED (SQLite: LIMIT -1 == no limit)")
for lim in ("50", "-1", "0", "1000000", "1e9", "abc"):
    r = cl.get(f"/api/blunders?limit={lim}")
    try:
        print(f"  limit={lim:<9} HTTP {r.status_code} rows={len(r.json())}")
    except Exception:
        print(f"  limit={lim:<9} HTTP {r.status_code} {r.text[:80]}")
print("  -> /api/blunders has NO ge/le bound and NO offset; LIMIT -1 disables the cap entirely")
print("     Compare /api/games/recent which correctly uses Query(25, ge=1, le=100).")

hdr("ATK3-11c — /api/blunders/{id} bounds")
REAL_ID = q1("SELECT id FROM blunders ORDER BY id LIMIT 1")["id"]
print("  a real blunder id in the temp db:", REAL_ID)
for bid in (str(REAL_ID), "0", "-1", "99999999", "abc", "1.5",
            "9223372036854775808", "99999999999999999999999999", "%27%20OR%201=1--"):
    r = cl.get(f"/api/blunders/{bid}")
    print(f"  id={bid:<28} HTTP {r.status_code} {r.text[:90]}")
print("  -> values beyond sqlite's signed 64-bit INTEGER raise OverflowError -> unhandled 500")

hdr("ATK3-11d — /api/blunders/{id} performs a blocking outbound explorer call")
import backend.lichess_client as lc  # noqa: E402
calls = []


def counting_explorer(self, fen, play=""):
    calls.append(fen)
    import time as _t
    _t.sleep(0.3)          # simulated 8s network timeout
    return {"white": 1, "draws": 0, "black": 1, "moves": []}


lc.LichessClient.get_opening_explorer_stats = counting_explorer
import time as _t  # noqa: E402
t0 = _t.time()
r = cl.get(f"/api/blunders/{REAL_ID}")
print(f"  one detail request took {_t.time() - t0:.2f}s, explorer calls={len(calls)} "
      f"(fen={calls[0][:40]!r}...)")
print("  master_stats returned to the client:", json.dumps(r.json().get("master_stats"))[:80])
print("  -> every blunder detail view is a synchronous, un-cached, un-rate-limited")
print("     outbound HTTPS call inside a sync def endpoint (blocks the worker thread).")
print("     Lichess 401s are silently swallowed, but there is no explorer_cache reuse here")

hdr("ATK3-11e — /api/dashboard: does the eco column survive the GROUP BY?")
seed_all()
import sqlite3  # noqa: E402
c = __import__("backend.db", fromlist=["get_db"]).get_db()
c.execute("UPDATE blunders SET opening_name='Sicilian Defense' WHERE id IN "
          "(SELECT id FROM blunders WHERE cpl_loss=900)")
c.execute("UPDATE blunders SET opening_name='Sicilian Defense', eco='B90' WHERE id IN "
          "(SELECT id FROM blunders WHERE cpl_loss=500)")
c.commit()
c.close()
d = cl.get("/api/dashboard").json()
print("  repertoire_blunders:", json.dumps(d["repertoire_blunders"], indent=1))
print("  -> `SELECT opening_name, eco, color, COUNT(*), AVG(cpl_loss) ... GROUP BY opening_name, color`")
print("     selects a NON-aggregated `eco`; SQLite returns an arbitrary row's value, so the")
print("     ECO shown for 'Sicilian Defense' is whichever row happened to come first.")

hdr("ATK3-11f — /api/dashboard counters vs reality")
print("  games rows      :", q1("SELECT COUNT(*) c FROM games")["c"], "| dashboard total_games:",
      d["total_games"])
print("  blunders rows   :", q1("SELECT COUNT(*) c FROM blunders")["c"], "| dashboard total_blunders:",
      d["total_blunders"])
print("  drill_reviews   :", q1("SELECT COUNT(*) c FROM drill_reviews")["c"],
      "| due_reviews_count:", d["due_reviews_count"])
print("  -> every blunder gets a drill_reviews row with next_review_date=now,")
print("     so due_reviews_count == COUNT(blunders) until the user drills them:")
print("     due =", d["due_reviews_count"], "blunders =", d["total_blunders"])

hdr("ATK3-11g — import_game_opening: can it inject an arbitrary move?")
wipe_repertoire()
seed_all()
out = rm.import_game_opening("gW", "white", 8)
print("  import ->", json.dumps(out))
rows = q("SELECT parent_fen, fen, move_uci, move_san, is_user_move FROM repertoire_moves ORDER BY id")
for r in rows:
    print("   ", r)
print("  legal? every move must be legal in its parent position:")
bad = []
for r in rows:
    b = chess.Board(r["parent_fen"])
    if chess.Move.from_uci(r["move_uci"]) not in b.legal_moves:
        bad.append(r)
print("   illegal rows:", bad)

print("\n  now poison the game record itself with UCI tokens / garbage and re-import:")
c = __import__("backend.db", fromlist=["get_db"]).get_db()
c.execute("UPDATE games SET moves_san='e4 e5 e2e4 ZZZ e7e5 1-0' WHERE id='gW'")
c.commit()
c.close()
wipe_repertoire()
out = rm.import_game_opening("gW", "white", 8)
print("  import ->", json.dumps(out))
print("  rows:", q("SELECT move_uci, is_user_move FROM repertoire_moves ORDER BY id"))

print("\n  import with repertoire='black' from a game the user played as WHITE:")
wipe_repertoire()
out = rm.import_game_opening("gW", "black", 8)
print("  ->", json.dumps(out))
print("  rows:", q("SELECT move_uci, is_user_move FROM repertoire_moves ORDER BY id"))
print("  (black moves get is_user_move=1 for the odd plies — i.e. the *opponent's* moves")
print("   from this game are imported as if they were the user's Black repertoire.)")
