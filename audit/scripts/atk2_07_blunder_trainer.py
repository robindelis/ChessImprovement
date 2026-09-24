"""TRAINER probe 07 -- the *blunder* drill trainer: /api/train/evaluate_move
interval handling for non-exact-best answers, plus /api/train/paths.

One bounded depth-10 Stockfish call per scenario (BRIEF rule 5).

Run: /home/robin/miniconda3/bin/python3 audit/scripts/atk2_07_blunder_trainer.py
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk2_fixture import *  # noqa
import chess
from backend.analyzer import GameAnalyzer

ALT = "d2d4"        # the "good alternative" the user will play
BEST = "e2e4"       # the recorded engine best move
GAME = "b1c3"       # the move the user actually played in the game (= the blunder)

hdr("measure the engine score of the alternative so it counts as a 'good alternative'")
_a = GameAnalyzer()
try:
    _b = chess.Board(START)
    _b.push_san("d4")
    _e = _a._get_engine().analyse(_b, chess.engine.Limit(depth=10))
    ALT_SCORE = _e["score"].pov(not _b.turn).score(mate_score=10000)
finally:
    _a.close()
print("   engine score after 1.d4 (white pov) =", ALT_SCORE)

c = fresh()
now = int(time.time())
conn = get_db()
conn.execute("DELETE FROM blunders")
conn.execute("DELETE FROM drill_reviews")
conn.execute("""INSERT INTO blunders (id, game_id, move_number, ply, color, fen_before, fen_after,
                played_move, played_san, best_move, best_san, eval_before, cpl_loss, created_at)
                VALUES (1,'gTEST',1,1,'white',?,?,?,'Nc3',?,'e4',?,220,?)""",
             (START, fens(["d4"])[1], GAME, BEST, ALT_SCORE, now))
conn.execute("""INSERT INTO drill_reviews (blunder_id, attempts, successes, last_attempt_date,
                next_review_date, interval_days, ease_factor, status)
                VALUES (1, 5, 4, ?, ?, 100.0, 2.5, 'mastered')""", (now - 10 * 86400, now - 86400))
conn.commit()
conn.close()


def show(tag):
    conn = get_db()
    r = dict(conn.execute("SELECT * FROM drill_reviews WHERE blunder_id=1").fetchone())
    conn.close()
    print("   %-32s attempts=%-3s successes=%-3s interval_days=%-7s next_in_days=%-7s status=%s"
          % (tag, r["attempts"], r["successes"], r["interval_days"],
             round((r["next_review_date"] - int(time.time())) / 86400.0, 2), r["status"]))


hdr("A) well-trained card (interval=100d) + a GOOD ALTERNATIVE answer -> is it reset?")
show("initial")
r = c.post("/api/train/evaluate_move", json={"blunder_id": 1, "attempted_uci": ALT}).json()
print("   feedback_type=%s is_correct=%s" % (r.get("feedback_type"), r.get("is_correct")))
print("   message: %s" % r.get("message"))
show("after good-alternative  <-- interval NOT reset")

hdr("B) then the exact best move -> the STALE interval is multiplied by the ease factor")
r = c.post("/api/train/evaluate_move", json={"blunder_id": 1, "attempted_uci": BEST}).json()
print("   feedback_type=%s" % r.get("feedback_type"))
show("after 1st exact best")
c.post("/api/train/evaluate_move", json={"blunder_id": 1, "attempted_uci": BEST})
show("after 2nd exact best")
conn = get_db()
nr = conn.execute("SELECT next_review_date FROM drill_reviews WHERE blunder_id=1").fetchone()[0]
conn.close()
print("   => card jumped from a 1-day review to %.0f days (%.1f years)"
      % ((nr - now) / 86400.0, (nr - now) / 86400.0 / 365))

hdr("C) contrast: replaying the ORIGINAL blunder move DOES reset the interval")
conn = get_db()
conn.execute("UPDATE drill_reviews SET interval_days=100.0 WHERE blunder_id=1")
conn.commit(); conn.close()
r = c.post("/api/train/evaluate_move", json={"blunder_id": 1, "attempted_uci": GAME}).json()
print("   feedback_type=%s" % r.get("feedback_type"))
show("after replaying the game move")

hdr("D) NULL eval_before -> a TypeError is swallowed and reported as an ENGINE error")
conn = get_db()
conn.execute("UPDATE blunders SET eval_before=NULL WHERE id=1")
conn.commit(); conn.close()
r = c.post("/api/train/evaluate_move", json={"blunder_id": 1, "attempted_uci": ALT}).json()
print("   feedback_type=%r" % r.get("feedback_type"))
print("   message: %s" % r.get("message"))
print("   (the engine was never consulted; the real error is"
      " 'unsupported operand type(s) for -: NoneType and int' at api.py:718)")

hdr("E) good_alternative can never reach 'mastered'")
conn = get_db()
conn.execute("UPDATE blunders SET eval_before=? WHERE id=1", (ALT_SCORE,))
conn.execute("UPDATE drill_reviews SET attempts=1, successes=8, interval_days=1.0, status='learning' "
             "WHERE blunder_id=1")
conn.commit(); conn.close()
r = c.post("/api/train/evaluate_move", json={"blunder_id": 1, "attempted_uci": ALT}).json()
print("   successes=%s but drill_status=%r  (exact-best would say 'mastered')"
      % (r["successes"], r["drill_status"]))

hdr("F) /api/train/paths")
print("  ", c.get("/api/train/paths").json())
conn = get_db()
conn.execute("UPDATE blunders SET cpl_loss=NULL WHERE id=1")
conn.commit(); conn.close()
print("   with NULL cpl_loss ->", c.get("/api/train/paths").json())
conn = get_db()
conn.execute("INSERT OR REPLACE INTO drill_reviews (blunder_id, attempts, successes, last_attempt_date, next_review_date, interval_days, ease_factor, status) VALUES (2, 0, 0, 0, 0, 1.0, 2.5, 'new')")
conn.commit(); conn.close()
print("   due_today counts brand-new cards (next_review_date=0) ->", c.get("/api/train/paths").json()["due_today"])
