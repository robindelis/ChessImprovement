"""FIXER-I — perf / DoS evidence for ABUSE-14, SCOUT-05 and SCOUT-10.

    PYTHONPATH=. /home/robin/miniconda3/bin/python3 audit/scripts/fixeri_perf_repro.py

No network, no yt-dlp, no Stockfish: everything is a pure CPU measurement
against the temp test DB.
"""
import time

import tests.test_setup  # FIRST  (BRIEF hard rule 2)

from backend.scout import ContentScout

scout = ContentScout.get_instance()

print("A) description scan (ABUSE-14: quadratic pattern)")
for n in (1_000, 10_000, 50_000):
    desc = "A" * n
    t0 = time.time()
    scout._extract_lines_from_description(desc)
    print(f"   len={n:<7} {time.time() - t0:8.3f}s")

print()
print("B) timeline spam (SCOUT-05: 400 identical captions)")
snips = [{"start": float(i), "text": "e4 e5 Nf3 Nc6"} for i in range(400)]
t0 = time.time()
timeline, variations = scout._extract_chess_moves_and_lines(snips, "", [], "Sicilian Test", 3000)
pairs = [(m["fen_before"], m["uci"]) for m in timeline]
print(f"   timeline rows={len(timeline)} unique={len(set(pairs))} "
      f"variations={len(variations)} elapsed={time.time() - t0:.3f}s")

print()
print("C) timestamps (SCOUT-05b)")
snips = [{"start": -50.0, "text": "e4"}, {"start": 900.0, "text": "e5"},
         {"start": 33.0, "text": "Nf3 Nc6"}]
timeline, variations = scout._extract_chess_moves_and_lines(snips, "", [], "Test", 120)
ts = [m["timestamp"] for m in timeline]
print(f"   rows={len(timeline)} min={min(ts) if ts else None} max={max(ts) if ts else None} (duration=120)")

print()
print("D) check_tree with 200 lines x 50 000 moves (SCOUT-10b: 262.9 s of CPU pre-fix)")
line = {"id": "l1", "moves": [{"fen_before": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -",
                               "uci": "e2e4", "san": "e4"}] * 50_000}
t0 = time.time()
try:
    scout.check_lines_overlap("white", [line])
    print("   ACCEPTED (no cap) elapsed=%.3fs" % (time.time() - t0))
except ValueError as e:
    print(f"   rejected after {time.time() - t0:.4f}s: {e}")
