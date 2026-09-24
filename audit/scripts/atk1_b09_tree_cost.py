"""BUILDER-09 probe: (a) import_game_opening ignores the game's user_color,
(b) GET /api/repertoire/tree has unbounded latency and an O(n^2) index build.
"""
import os
import sys
import time
import json
import threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk1_fixture import *  # noqa

import backend.repertoire as R
import sqlite3

# ---------------------------------------------------------------- (a)
print("=" * 78)
print("(a) import_game_opening never checks WHICH SIDE the user played")
print("=" * 78)
wipe()
add_line("white", ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4"], study_name="My Line")
show_tree("white", "prepared White line")
P = fen_after(["e4", "e5"])

# A game the user played as BLACK.  Its White moves are the OPPONENT's moves.
conn = sqlite3.connect(__import__("backend.db", fromlist=["get_db"]).get_db_path())
conn.execute("""INSERT OR REPLACE INTO games
    (id, speed, rated, played_at, white_player, black_player, white_rating, black_rating,
     user_color, result, opening_name, eco, moves_san, analyzed_at)
    VALUES ('asblack','rapid',1,1700000000,'somebody','RobiDeli',2200,2100,'black','loss',
            'Ruy Lopez','C60',?,0)""", ("e4 e5 Nc3 Nc6 Bb5 a6",))
conn.commit()
conn.close()
print("\ngame 'asblack': white_player=somebody, black_player=RobiDeli, user_color='black'")
print("moves_san: e4 e5 Nc3 Nc6 Bb5 a6   (Nc3/Bb5 are the OPPONENT's moves)")

r = CLIENT.post("/api/repertoire/import_game_opening", json={"game_id": "asblack", "repertoire": "white"})
print("\nPOST /api/repertoire/import_game_opening {repertoire: 'white'} ->", r.status_code, r.json())
show_tree("white", "after importing a BLACK game into the WHITE repertoire")
print("\nuser moves at the position after 1.e4 e5 (should still be the prepared 2.Nf3):",
      raw("SELECT move_uci, move_san, is_user_move FROM repertoire_moves WHERE parent_fen=?",
          (normalize_fen(P),)))

# ---------------------------------------------------------------- (b)
print("\n" + "=" * 78)
print("(b1) GET /api/repertoire/tree latency grows linearly with the tree, uncapped")
print("=" * 78)
print("backend/evaluator.py introduced a bounded SyncEnginePool, but")
print("get_candidate_moves_2000() still uses requests.get(..., timeout=10) per opponent position")
print("and get_explorer_game_count() uses timeout=6 per un-populated blunder position.\n")

DELAY = 0.5
real_requests = R.requests


class Slow:
    @staticmethod
    def get(url, params=None, headers=None, timeout=None, **kw):
        time.sleep(DELAY)                 # stand in for a network that is down
        raise OSError("network unreachable")


R.requests = Slow
R.RepertoireManager._instance = None

for n_positions in (2, 6, 12):
    wipe()
    # 1.e4 + n black replies, each followed by a white move -> n opponent positions
    board = chess.Board()
    add("white", board.fen(), "e2e4", is_user_move=True)
    board.push(chess.Move.from_uci("e2e4"))
    for i in range(n_positions):
        legal = list(board.legal_moves)
        m = legal[i % len(legal)]
        add("white", board.fen(), m.uci(), is_user_move=False)
        board.push(m)
        legal2 = list(board.legal_moves)
        m2 = legal2[0]
        add("white", board.fen(), m2.uci(), is_user_move=True)
        board.push(m2)
    n_rows = one("SELECT COUNT(*) c FROM repertoire_moves")["c"]
    t0 = time.time()
    resp = CLIENT.get("/api/repertoire/tree", params={"color": "white", "min_games": 0})
    el = time.time() - t0
    print(f"   repertoire rows={n_rows:<4} /api/repertoire/tree took {el:5.1f}s "
          f"(= {n_rows/2} explorer calls x {DELAY}s artificial latency; "
          f"real timeout is 10s each)")

R.requests = real_requests
R.RepertoireManager._instance = None

print("\n" + "=" * 78)
print("(b2) moves_by_parent duplicates every row (4-token key AND 3-token alias)")
print("=" * 78)
wipe()
# A long-ish line so the payload is measurable.
line = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6", "O-O", "Be7", "Re1", "b5",
        "Bb3", "d6", "c3", "O-O", "h3", "Nb8", "d4", "Nbd7"]
add_line("white", line)
for extra in (["c4", "e5", "Nc3", "Nf6"], ["d4", "d5", "c4", "e6"]):
    pass
n_rows = one("SELECT COUNT(*) c FROM repertoire_moves")["c"]
t0 = time.time()
resp = CLIENT.get("/api/repertoire/tree", params={"color": "white", "min_games": 0})
el = time.time() - t0
data = resp.json()
mbp = data["moves_by_parent"]
k4 = [k for k in mbp if len(k.split()) == 4]
k3 = [k for k in mbp if len(k.split()) == 3]
total_entries = sum(len(v) for v in mbp.values())
print(f"   repertoire_moves rows      : {n_rows}")
print(f"   moves_by_parent keys       : {len(mbp)}  (4-token {len(k4)}, 3-token alias {len(k3)})")
print(f"   row objects in the payload : {total_entries}  -> every row is emitted TWICE")
print(f"   response bytes             : {len(resp.content)}")
print(f"   time                       : {el:.2f}s")
# A representative alias pair
if k3:
    k = k3[0]
    same = k4[0] if k4 else None
    print(f"   example 3-token key {k!r} -> {len(mbp[k])} row(s)")
    print(f"   example 4-token key {same!r} -> {len(mbp[same])} row(s)")
