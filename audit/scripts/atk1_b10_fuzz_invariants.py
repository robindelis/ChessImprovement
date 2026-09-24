"""BUILDER-10 probe: differential invariant fuzz over the Repertoire Builder write API.

Drives a deterministic pseudo-random sequence of *realistic* Builder actions through
the HTTP API and, after every step, checks the invariants the rest of the app relies on:

  I1  at most one is_user_move=1 row per (repertoire, parent_fen)
  I2  no dangling parent_fen: every non-root row's parent_fen must be produced by some
      other row's fen (or be the start position)
  I3  no dangling study_moves: every study_moves row must join to a repertoire_moves row
  I4  no repertoire_moves.study_name pointing at a study that does not exist
  I5  no study_moves row pointing at a study_id that does not exist

Run:  python3 audit/scripts/atk1_b10_fuzz_invariants.py [steps]
"""
import os
import sys
import random
import sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk1_fixture import *  # noqa

import backend.db as dbm

STEPS = int(sys.argv[1]) if len(sys.argv) > 1 else 260
START = normalize_fen(chess.STARTING_FEN)
violations = []


def check(tag):
    def bad(msg, rows=None):
        violations.append((tag, msg, rows or []))
        print(f"  !! [{tag}] {msg}")
        for r in (rows or [])[:8]:
            print("       ", r)
    return bad


def invariants(tag):
    bad = check(tag)
    r = raw("""SELECT repertoire, parent_fen, COUNT(*) n, GROUP_CONCAT(move_uci) ucis
              FROM repertoire_moves WHERE is_user_move = 1
              GROUP BY repertoire, parent_fen HAVING n > 1""")
    if r:
        bad("I1 one-user-move-per-position VIOLATED", r)

    r = raw("""SELECT rm.id, rm.move_san, rm.parent_fen FROM repertoire_moves rm
              WHERE rm.parent_fen != ? AND NOT EXISTS (
                  SELECT 1 FROM repertoire_moves p
                  WHERE p.repertoire = rm.repertoire AND p.fen = rm.parent_fen)""", (START,))
    if r:
        bad("I2 dangling parent_fen (orphaned move)", r)

    r = raw("""SELECT sm.* FROM study_moves sm LEFT JOIN repertoire_moves rm
              ON rm.repertoire = sm.repertoire AND rm.parent_fen = sm.parent_fen
             AND rm.move_uci = sm.move_uci
              WHERE rm.id IS NULL""")
    if r:
        bad("I3 dangling study_moves (link to a deleted move)", r)

    r = raw("""SELECT rm.id, rm.move_san, rm.study_name FROM repertoire_moves rm
              WHERE rm.study_name <> '' AND NOT EXISTS (
                  SELECT 1 FROM repertoire_studies s
                  WHERE s.name = rm.study_name AND s.repertoire = rm.repertoire)""")
    if r:
        bad("I4 study_name points at a non-existent study", r)

    r = raw("""SELECT sm.* FROM study_moves sm LEFT JOIN repertoire_studies s
              ON s.id = sm.study_id WHERE s.id IS NULL""")
    if r:
        bad("I5 study_moves points at a non-existent study", r)


def tree_for(repertoire):
    """All (parent_fen_normalised, legal uci) positions currently in the tree."""
    out = []
    rows = tree_rows(repertoire)
    for r in rows:
        try:
            b = chess.Board(r["parent_fen"])
            if not b.is_valid():
                continue
        except Exception:
            continue
        out.append((r["parent_fen"], b))
    return out


def seed_game(gid, color, sans):
    conn = sqlite3.connect(dbm.get_db_path())
    conn.execute("""INSERT OR REPLACE INTO games
        (id, speed, rated, played_at, white_player, black_player, white_rating, black_rating,
         user_color, result, opening_name, eco, moves_san, analyzed_at)
        VALUES (?,'rapid',1,1700000000,'RobiDeli','opp',2100,2050,?,'win','Ruy Lopez','C60',?,0)""",
        (gid, color, sans))
    conn.commit()
    conn.close()


wipe()
rnd = random.Random(20260926)
STUDIES = ["Anti French", "Caro-Kann"]
for s in STUDIES:
    CLIENT.post("/api/repertoire/studies", json={"name": s, "repertoire": "white"})

# Two real games of the user, one as White one as Black.
GAMES = {
    "g1": ("white", "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7 Re1 b5 Bb3 d6 c3 O-O"),
    "g2": ("white", "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 g6 Be3 Bg7 f3 O-O"),
    "g3": ("black", "e4 c5 Nf3 e6 d4 cxd4 Nxd4 Bc5 Be2 Nf6 Nc3 O-O O-O"),
}
for gid, (col, sans) in GAMES.items():
    seed_game(gid, col, sans)

print("=" * 78)
print(f"fuzzing {STEPS} Builder actions against the tree invariants")
print("=" * 78)
invariants("step 0 (after study creation)")

for step in range(1, STEPS + 1):
    action = rnd.choice([
        "add_user", "add_user", "add_user", "add_opp", "add_opp",
        "delete", "meta", "meta_star", "import_game", "delete_study",
    ])
    color = rnd.choice(["white", "black"])
    nodes = tree_for(color)
    tag = f"step {step} [{action}/{color}]"

    if action in ("add_user", "add_opp") and nodes:
        parent, board = rnd.choice(nodes)
        legal = list(board.legal_moves)
        if not legal:
            continue
        m = rnd.choice(legal)
        is_user = (board.turn == chess.WHITE) == (color == "white")
        if (action == "add_opp") == is_user:
            is_user = not is_user
        body = {"repertoire": color, "parent_fen": board.fen(), "move_uci": m.uci(),
                "is_user_move": is_user}
        if rnd.random() < 0.6:
            body["study_name"] = rnd.choice(STUDIES + ["", "  "])
        CLIENT.post("/api/repertoire/move", json=body)

    elif action == "delete" and nodes:
        parent, board = rnd.choice(nodes)
        rows = raw("SELECT move_uci FROM repertoire_moves WHERE parent_fen=? AND repertoire=?",
                   (parent, color))
        if not rows:
            continue
        CLIENT.request("DELETE", "/api/repertoire/move", json={
            "repertoire": color, "parent_fen": parent,
            "move_uci": rnd.choice(rows)["move_uci"]})

    elif action in ("meta", "meta_star") :
        nodes = tree_for(color)
        if not nodes:
            continue
        parent, board = rnd.choice(nodes)
        legal = list(board.legal_moves)
        m = rnd.choice(legal) if legal else None
        if m is None:
            continue
        if action == "meta_star" and rnd.random() < 0.6:
            # an "unprepared" move: legal here but usually not the prepared one
            alts = [x for x in legal if x.uci() != m.uci()]
            m = rnd.choice(alts) if alts else m
        body = {"repertoire": color, "parent_fen": board.fen(), "move_uci": m.uci()}
        if action == "meta_star":
            body["is_important"] = True
        else:
            body["comment"] = "note"
            if rnd.random() < 0.5:
                body["study_name"] = rnd.choice(STUDIES)
        CLIENT.post("/api/repertoire/move_meta", json=body)

    elif action == "import_game":
        gid = rnd.choice(list(GAMES))
        col = rnd.choice(["white", "black"])     # deliberately may not match user_color
        CLIENT.post("/api/repertoire/import_game_opening", json={"game_id": gid, "repertoire": col})

    elif action == "delete_study":
        rows = raw("SELECT id FROM repertoire_studies WHERE repertoire=?", (color,))
        if rows:
            sid = rnd.choice(rows)["id"]
            CLIENT.delete(f"/api/repertoire/studies/{sid}", params={"delete_moves": rnd.random() < 0.5})

    invariants(tag)
    if step % 25 == 0:
        n = one("SELECT COUNT(*) c FROM repertoire_moves")["c"]
        print(f"  .. step {step}: {n} moves, {len(violations)} violations so far")

print()
print("=" * 78)
print("NOW SIMULATE ONE APP RESTART (init_db -> migrate_studies_and_cleanup_blunders)")
print("=" * 78)
before = one("SELECT COUNT(*) c FROM repertoire_moves")["c"]
dup_before = len(raw("""SELECT parent_fen FROM repertoire_moves WHERE is_user_move=1
                       GROUP BY repertoire, parent_fen HAVING COUNT(*)>1"""))
print(f"   before restart: {before} moves, {dup_before} positions with 2+ user moves")
restart_app_db()
after = one("SELECT COUNT(*) c FROM repertoire_moves")["c"]
dups = raw("""SELECT repertoire, parent_fen, COUNT(*) n, GROUP_CONCAT(move_san) sans
              FROM repertoire_moves WHERE is_user_move = 1
              GROUP BY repertoire, parent_fen HAVING n > 1""")
print(f"   after  restart: {after} moves, {len(dups)} positions with 2+ user moves")
for d in dups[:10]:
    print("     ", d)
check("AFTER RESTART")("I1' one-user-move-per-position VIOLATED BY THE STARTUP MIGRATION", dups)

print()
print("=" * 78)
print(f"RESULT: {len(violations)} invariant violations over {STEPS} actions + 1 restart")
print("=" * 78)
kinds = {}
for tag, msg, _ in violations:
    kinds.setdefault(msg.split("(")[0].strip(), 0)
    kinds[msg.split("(")[0].strip()] += 1
for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]):
    print(f"   {v:4d} x {k}")
if violations:
    print("\nfirst violating step for each kind:")
    seen = set()
    for tag, msg, rows in violations:
        k = msg.split("(")[0].strip()
        if k in seen:
            continue
        seen.add(k)
        print(f"   {tag}: {msg}")
        for r in rows[:4]:
            print("      ", r)
