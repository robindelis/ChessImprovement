"""ATK3-12 : is_patched / auto-promotion (AGENTS.md 4D) and the root-cause 'earlier_mistake' (4B.2).
ATK3-13 : import_game_opening silently REPLACES + PRUNES the user's prepared line (one click)."""
import os
import sys
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import (hdr, wipe_games, wipe_repertoire, q, q1, seed_game, seed_blunder,  # noqa: E402
                      client)
import chess  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402

cl = client()
rm = RepertoireManager.get_instance()
rm.get_explorer_game_count = lambda fen, ratings="2000,2200,2500": 100000

F = {}
for key, sans in (("start", []), ("after_e4", ["e4"]), ("after_e4e5", ["e4", "e5"]),
                  ("after_e4e5f3", ["e4", "e5", "Nf3"]), ("after_d4", ["d4"]),
                  ("after_d4d5", ["d4", "d5"]), ("after_d4d5c3", ["d4", "d5", "Nc3"])):
    b = chess.Board()
    for s in sans:
        b.push_san(s)
    F[key] = b.fen(en_passant="fen")


def norm(f):
    return " ".join(f.split()[:4])


hdr("ATK3-12a — two blunders in the tree; patch #1 and see whether #2 is promoted")
wipe_games()
wipe_repertoire()
seed_game("pg", user_color="white", result="black", analyzed_at=1,
          moves="e4 e5 Nf3 Nc6 Bb5 a6")
seed_blunder("pg", ply=1, move_number=1, cpl=900, fen_before=F["start"],
             best_move="e2e4", played_move="d2d4", opening_name="Queen's Pawn", eco="A40")
seed_blunder("pg", ply=5, move_number=3, cpl=400, fen_before=F["after_e4e5"],
             best_move="f1b5", played_move="c2c3", opening_name="Queen's Pawn", eco="A40")
out = rm.get_game_gaps("white", min_games=30)
print("  before patch: total=%d unpatched=%d patched=%d" %
      (out["total_gaps"], out["unpatched_count"], out["patched_count"]))
for g in out["gaps"]:
    print(f"    #{g['cpl_loss']} ply{g['ply']} {g['played_san']}->{g['best_san']} "
          f"patched={g['is_patched']} earlier_mistake="
          f"{(g['earlier_mistake'] or {}).get('ply')}")

print("\n  -> now adopt the engine's fix for the #1 gap into the repertoire:")
print("   ", rm.add_repertoire_move("white", norm(F["start"]), "e2e4", is_user_move=True))
out2 = rm.get_game_gaps("white", min_games=30)
print("  after patch : total=%d unpatched=%d patched=%d gaps=%d" %
      (out2["total_gaps"], out2["unpatched_count"], out2["patched_count"], len(out2["gaps"])))
for g in out2["gaps"]:
    print(f"    NEW #1 -> #{g['cpl_loss']} ply{g['ply']} {g['played_san']}->{g['best_san']}")
for g in out2["patched_gaps"]:
    print(f"    patched  -> #{g['cpl_loss']} ply{g['ply']} {g['played_san']}")
r = cl.get("/api/repertoire/game_gaps?color=white&min_games=30").json()
print("  HTTP include_patched=false -> gaps[0] id:", r["gaps"][0]["id"], "cpl:", r["gaps"][0]["cpl_loss"])
r2 = cl.get("/api/repertoire/game_gaps?color=white&min_games=30&include_patched=true").json()
print("  HTTP include_patched=true  ->", len(r2["gaps"]), "gaps, first cpl:",
      r2["gaps"][0]["cpl_loss"], "(patched 900 re-appears at the top)")

hdr("ATK3-12b — 'Show Patched (N)' toggle: is it wired up in the UI at all?")
import re  # noqa: E402
js = open("frontend/repertoire_builder.js", encoding="utf-8").read()
html = open("frontend/index.html", encoding="utf-8").read()
import glob  # noqa: E402
files = sorted(glob.glob("frontend/*.js") + glob.glob("frontend/*.html") + glob.glob("frontend/*.css"))
print("  occurrences of showPatchedGaps in repertoire_builder.js:",
      [(js[max(0, m.start() - 45):m.start() + 30].replace("\n", " "))
       for m in re.finditer(r"showPatchedGaps", js)])
print("  'Patched' appears in index.html:", "Patched" in html, "| in app.js:",
      "Patched" in open("frontend/app.js", encoding="utf-8").read())


def count(token):
    return {os.path.basename(f): open(f, encoding="utf-8").read().count(token)
            for f in files if open(f, encoding="utf-8").read().count(token)}


print("  frontend occurrences of earlier_mistake :", count("earlier_mistake") or "NONE")
print("  frontend occurrences of patched_count   :", count("patched_count") or "NONE")
print("  frontend occurrences of unpatched_count :", count("unpatched_count") or "NONE")
print("  frontend occurrences of is_patched      :", count("is_patched") or "NONE")
print("  -> AGENTS.md 4D 'A toggle allows viewing patched moves (Show Patched (N))' and")
print("     4B.2 'providing a one-click jump to the root error' are not implemented anywhere.")

hdr("ATK3-13 — import_game_opening on a game that deviates from the prepared line")
wipe_games()
wipe_repertoire()
# the user's prepared White repertoire: 1.e4 e5 2.Nf3  3.Bc4
rm.add_repertoire_move("white", norm(F["start"]), "e2e4", is_user_move=True)
rm.add_repertoire_move("white", norm(F["after_e4"]), "e7e5", is_user_move=False)
rm.add_repertoire_move("white", norm(F["after_e4e5"]), "g1f3", is_user_move=True)
rm.add_repertoire_move("white", norm(F["after_e4e5f3"]), "f1c4", is_user_move=True)
print("  prepared white repertoire:")
for r in q("SELECT parent_fen, move_uci, is_user_move FROM repertoire_moves ORDER BY id"):
    print("   ", r)
seed_game("dev", user_color="white", result="white", analyzed_at=1, moves="d4 d5 Nc3 Nf6")
print("\n  user plays 1.d4 and clicks the one-click 'Add Opening' on the game card")
print("  (repertoire_builder.js:1031 -> importGameOpening(g.id, g.user_color)):")
out = rm.import_game_opening("dev", "white")
print("   ", json.dumps(out))
print("  repertoire AFTER the import:")
for r in q("SELECT parent_fen, move_uci, move_san, is_user_move FROM repertoire_moves ORDER BY id"):
    print("   ", r)
left = q("SELECT COUNT(*) c FROM repertoire_moves") [0]["c"]
print("  moves before: 4  after:", left)
print("  -> 1.e4 and the whole 1.e4 e5 2.Nf3 3.Bc4 sub-tree were DELETED by a single")
print("     click on 'Add Opening', with no confirmation dialog anywhere in the UI.")
