#!/usr/bin/env python3
"""ATK-5 re2-2: the `get_master_stats` single-flight is keyed by the RAW FEN.

The fixer's stated goal (api.py:1403) is "N concurrent views of the same
POSITION make ONE outbound call, not N".  Both the memo and the lock are keyed
by the raw 6-token FEN (api.py:1487, :1499), while `explorer_cache` is keyed by
the 4-token `normalize_fen` form that AGENTS.md 4F declares to be THE cache key
for this app.

Two blunders reached at the same position in two different games differ only in
the halfmove/fullmove counters -- which is the ordinary case, not an edge case.
This probe measures how many outbound calls that costs, and whether an attacker
can drive the memo past its bound.

Isolated temp DB, no real network, idempotent.
"""
import os
import sys
import json
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
from backend.db import get_db  # noqa: E402
import backend.api as apimod  # noqa: E402

print("=" * 78)
print("ATK-5 re2-2  get_master_stats memo/lock key")
print("=" * 78)

# ------------------------------------------------- 1. key-shape demonstration
print("\n[1] the two key functions, side by side")
START = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
print(f"    raw FEN                     : {START!r}")
print(f"    _normalize_explorer_fen()    : {apimod._normalize_explorer_fen(START)!r}"
      "   <- the explorer_cache key")
print(f"    memo / lock key             : {START!r}"
      "   <- raw, NOT normalised")
print("    AGENTS.md 4F: the 4-token form is the cache key for explorer_cache.fen,")
print("    repertoire_moves.parent_fen, study_moves.parent_fen, blunders.fen_before.")

# ------------------------------------------- 2. how many distinct raw FENs?
print("\n[2] distinct raw FENs vs distinct positions, over a real game's plies")
import chess  # noqa: E402
board = chess.Board()
raw_fens, positions = set(), set()
line = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6", "O-O", "Be7",
        "Re1", "b5", "Bb3", "d6", "c3", "O-O", "h3", "Nb8", "d4", "Nbd7"]
for san in line:
    board.push_san(san)
    if board.turn == chess.BLACK:
        f = board.fen()
        raw_fens.add(f)
        positions.add(apimod._normalize_explorer_fen(f))
print(f"    20-ply game, {len(raw_fens)} black-to-move positions")
print(f"    distinct RAW FENs            : {len(raw_fens)}")
print(f"    distinct 4-token POSITIONS  : {len(positions)}")
print("    (identical here -- same game. The divergence needs the SAME position")
print("     reached in DIFFERENT games, which is what a real library looks like.)")

# ---------------------------------- 3. two games, one shared opening position
print("\n[3] the realistic case: the same opening reached in different games")
# A true transposition: the same position reached by two different move orders.
# 1.e4 c6 2.d4 d5 3.Nc3  vs  1.e4 c6 2.Nc3 d5 3.d4  -- identical board, different clocks.
g1 = chess.Board()
for san in ("e4", "c6", "d4", "d5", "Nc3"):
    g1.push_san(san)
g2 = chess.Board()
for san in ("e4", "c6", "Nc3", "d5", "d4"):
    g2.push_san(san)
f1, f2 = g1.fen(), g2.fen()
print(f"    game A fen_before : {f1!r}")
print(f"    game B fen_before : {f2!r}")
print(f"    same raw string?  : {f1 == f2}")
print(f"    same 4-token key? : {apimod._normalize_explorer_fen(f1) == apimod._normalize_explorer_fen(f2)}")
assert f1 != f2 and apimod._normalize_explorer_fen(f1) == apimod._normalize_explorer_fen(f2)
print("    -> PROVEN: a genuine transposition. Two blunders at ONE position -> two")
print("       memo keys, two locks, TWO outbound calls, for ONE explorer_cache row.")

# --------------------------------------- 4. measure the outbound amplification
print("\n[4] measured: N blunders at ONE position -> how many outbound calls?")
CALLS = {"n": 0}
_c = threading.Lock()


def stub(self, fen, play=""):
    with _c:
        CALLS["n"] += 1
    time.sleep(float(os.environ.get("ATK5_LAT", "0.4")))
    return {"white": 100, "draws": 20, "black": 80, "moves": [], "total_games": 200}


import backend.lichess_client as lc  # noqa: E402
lc.LichessClient.get_opening_explorer_stats = stub

N = 40
POS = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
fens = [f"rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 {i} {i // 2 + 1}"
        for i in range(N)]
print(f"    {N} blunder FENs, all the SAME 4-token position "
      f"('{apimod._normalize_explorer_fen(POS)}')")
apimod._explorer_stats_memo.clear()
with _c:
    CALLS["n"] = 0
res = [None] * N
bar = threading.Barrier(N)


def work(i):
    bar.wait()
    res[i] = apimod.get_master_stats(fens[i])


ts = [threading.Thread(target=work, args=(i,)) for i in range(N)]
t0 = time.time()
for t in ts:
    t.start()
for t in ts:
    t.join(60)
wall = time.time() - t0
print(f"    concurrent -> outbound calls: {CALLS['n']} for {N} views "
      f"({CALLS['n'] / N:.2f} per request), wall {wall:.2f}s")
print(f"    memo entries created       : {len(apimod._explorer_stats_memo)} "
      f"(expected {N} if keyed per raw FEN)")

print("\n    the same N requests, SEQUENTIALLY (memo now warm):")
with _c:
    CALLS["n"] = 0
for f in fens:
    apimod.get_master_stats(f)
print(f"    outbound calls             : {CALLS['n']}  "
      f"({'memo works per raw FEN' if CALLS['n'] == 0 else 'some misses'})")

print("\n    the same POSITION, one raw FEN, N concurrent (what the fixer intended):")
one = [POS] * N
apimod._explorer_stats_memo.clear()
with _c:
    CALLS["n"] = 0
bar2 = threading.Barrier(N)


def work2():
    bar2.wait()
    apimod.get_master_stats(POS)


ts2 = [threading.Thread(target=work2) for _ in range(N)]
for t in ts2:
    t.start()
for t in ts2:
    t.join(60)
print(f"    outbound calls             : {CALLS['n']} for {N} views  <-- "
      f"single-flight DOES work when the key is identical")

# ------------------------------------------- 5. can the memo be walked/bounded?
print("\n[5] memo bound: EXPLORER_STATS_MEMO_LIMIT =", apimod.EXPLORER_STATS_MEMO_LIMIT)
apimod._explorer_stats_memo.clear()
CALLS["n"] = 0
t0 = time.time()
for i in range(700):
    apimod.get_master_stats(f"8/8/8/8/8/8/8/K1k5 w - - 0 {i}")
print(f"    700 distinct FENs, sequential: {CALLS['n']} outbound calls, "
      f"{time.time() - t0:.2f}s")
print(f"    memo size now              : {len(apimod._explorer_stats_memo)}")
print("    NOTE: _explorer_stats_memo is bounded by CLEARING THE WHOLE DICT, so one")
print("          flood past the limit evicts every warm entry (a cliff, not LRU).")
print("    the DB layer (explorer_cache) is NOT written by get_master_stats, so a")
print("    cleared memo means the next view re-fetches.")

print("\n[6] the lock dict is unbounded and never pruned")
apimod._explorer_stats_locks.clear()
apimod._explorer_stats_memo.clear()
CALLS["n"] = 0
for i in range(2000):
    apimod.get_master_stats(f"8/8/8/8/8/8/8/K1k5 w - - 0 {i}")
print(f"    _explorer_stats_memo   : {len(apimod._explorer_stats_memo)} entries "
      f"(bound {apimod.EXPLORER_STATS_MEMO_LIMIT})")
print(f"    _explorer_stats_locks  : {len(apimod._explorer_stats_locks)} entries "
      "<-- NO bound, no prune, one Lock per distinct FEN ever seen")
print("    a threading.Lock is ~64 bytes + the FEN string, so this grows without")
print("    limit across a long session that walks many positions.")
print("done.")
