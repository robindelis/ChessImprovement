"""BUILDER-04 probe: silent data-loss paths in the Builder's write endpoints.

Part A: POST /api/repertoire/move_meta is a WRITE endpoint - it INSERTS a move that
        is not in the tree.  The Builder enables "Mark Important" + the note box for
        *every* non-root board position, including positions whose move is not part of
        the repertoire.  Marking such a move important therefore (a) inserts a new user
        move and (b) REPLACES + PRUNES the user's real prepared move.

Part B: POST /api/repertoire/import_game_opening ("Add Opening" on a recent-game card)
        replays the *game's* moves as repertoire moves.  Any ply where the user deviated
        from their own repertoire silently replaces the prepared move and prunes its
        branch - no confirmation, no diff, no undo.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk1_fixture import *  # noqa

print("=" * 78)
print("PART A - /api/repertoire/move_meta silently INSERTS and can REPLACE a user move")
print("=" * 78)
wipe()
add_line("white", ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4"], study_name="My Line")
print("prepared White line (Anti-Sicilian-ish): 1.e4 e5 2.Nf3 Nc6 3.Bb5 a6 4.Ba4")
show_tree("white", "before")

P = fen_after(["e4", "e5"])          # white to move, prepared move is Nf3
print("\nposition after 1.e4 e5:", P)
print("moves stored at that position:",
      raw("SELECT move_uci, move_san, is_user_move FROM repertoire_moves WHERE parent_fen=?",
          (normalize_fen(P),)))

print("\n-> user plays 2.f4 on the board (NOT in the repertoire), then clicks 'Mark Important'.")
print("   The Builder posts move_meta for (parent, 'f2f4') - see renderMoveToolsPanel():")
r = CLIENT.post("/api/repertoire/move_meta", json={
    "repertoire": "white", "parent_fen": P, "move_uci": "f2f4",
    "is_important": True, "comment": "looks better to me"})
print("   ->", r.status_code, {k: r.json().get(k) for k in ("status", "is_important", "comment")})
show_tree("white", "after move_meta on an un-prepared move")
print("\n   the prepared 2.Nf3 and its whole 3.Bb5 line are GONE.  user_moves at that position:",
      raw("SELECT move_uci, move_san, is_user_move FROM repertoire_moves WHERE parent_fen=?",
          (normalize_fen(P),)))

print("\n" + "=" * 78)
print("PART B - /api/repertoire/import_game_opening ('Add Opening') overwrites the line")
print("=" * 78)
wipe()
add_line("white", ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4"], study_name="My Line")
show_tree("white", "prepared line before import")

# A real game of the user's that follows 1.e4 e5 but deviates at move 2 (Nc3 instead of Nf3).
GAME = "e4 e5 Nc3 Nc6 Bb5 a6 Ba4 Nf6 O-O"
conn = __import__("sqlite3").connect(__import__("backend.db", fromlist=["get_db"]).get_db_path())
conn.execute("""INSERT OR REPLACE INTO games
    (id, speed, rated, played_at, white_player, black_player, white_rating, black_rating,
     user_color, result, opening_name, eco, moves_san, analyzed_at)
    VALUES ('deviated1','rapid',1,1700000000,'RobiDeli','opponent',2100,2050,'white','win',
            'Ruy Lopez','C60',?,0)""", (GAME,))
conn.commit()
conn.close()
print("\nimported game moves_san:", GAME)
print("user colour = white, so White's plies (e4, Nc3, Bb5, Ba4) become user moves -- 2.Nc3 diverges from the prepared 2.Nf3")

r = CLIENT.post("/api/repertoire/import_game_opening", json={"game_id": "deviated1", "repertoire": "white"})
print("\nPOST /api/repertoire/import_game_opening ->", r.status_code, r.json())
show_tree("white", "after import (prepared 2.Nf3 / 3.Bb5 line replaced by the game's moves)")
print("\nAny warning that a prepared move was replaced?  response keys:", sorted(r.json().keys()))

print("\n" + "=" * 78)
print("PART C - and the pruned branch comes back on the next app start (see BUILDER-01)")
print("=" * 78)
print("dangling study_moves after the import:", len(dangling_study_moves()))
restart_app_db()
show_tree("white", "after restart")
