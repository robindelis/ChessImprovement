# TRAINER-15 — Trainer correctness/messaging defects: lies in the drill FEN clock, percentages that disagree with their counts, `due_today` counting unreviewed cards, `good_alternative` never mastering, `NULL eval_before` masked as an engine error, over-eager UCI truncation, and a misleading `move_count`
- **Severity:** low
- **Type:** wrong-result
- **File:** backend/repertoire.py
- **Line(s):** 2381-2386 (FEN rebuild), 1985-1991 & 2005-2008 (percent rounding), 1958-1969 (mastery rule), 2588-2589; backend/api.py 617-620 (`due_today`), 713-741 (engine branch), 724-730 (`good_alternative`), 658-666 (UCI sanitizer), backend/repertoire.py 2452 (`move_count`)
- **Component:** Repertoire trainer + blunder trainer — reporting/scheduling edges
- **Repro script:** `audit/scripts/atk2_09_progress_dataloss.py`, `audit/scripts/atk2_07_blunder_trainer.py`, `audit/scripts/atk2_05_answer_lineend_hint.py`
- **Detected by:** ATTACKER-2 (TRAINER)

## Expected / Actual (seven independent, individually verified defects)

**15a — the drill FEN is rebuilt with a hard-coded `0 1` clock** (`repertoire.py:2381-2386`
`if len(parts) == 4: p_fen += " 0 1"`). A ply-9 castling drill is announced as move 1:
```
ply_depth=9  drill_reason=frontier_new  expected=O-O
stored parent_fen  : r1bqkb1r/1ppp1ppp/p1n2n2/4p3/B3P3/5N2/PPPP1PPP/RNBQK2R w KQkq -
drill parent_fen   : r1bqkb1r/1pp1ppp/p1n2n2/4p3/B3P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 1
```
The halfmove clock is also forced to 0, so the 50-move counter is meaningless in the trainer,
and any client that reads `fullmove_number` from the drill is wrong.

**15b — the three progress segments do not match their own counts** (`unlearned_pct` is
computed as `100 - mastered_pct - learning_pct` from *rounded* values):
```
total=7 mastered=2 learning=3 unlearned=2
pcts: mastered=28.6 learning=42.9 unlearned=28.5  (sum=100.0)
true value for unlearned 2/7 = 28.6
```
The UI renders three bars whose widths contradict the legend counts (`repertoire_trainer.js:229-260`).

**15c — `due_today` counts every never-reviewed card** (`api.py:617-620`,
`WHERE next_review_date <= ?` with the column defaulting to 0):
```
due_today counts brand-new cards (next_review_date=0) -> 1
```
`GET /api/train/paths` therefore reports a growing "due today" that includes cards the user has
never seen, and `avg_loss` comes back as JSON `null` when `cpl_loss` is NULL
(`{'group_name': 'Other', …, 'avg_loss': None}`).

**15d — a card answered correctly via `good_alternative` can never be `mastered`**
(`api.py:729` hard-codes `status = "learning"`):
```
successes=9 but drill_status='learning'  (exact-best would say 'mastered')
```
so `/api/train/paths` reports 0 % mastered for a card the user has answered correctly nine
times. Same defect class as TRAINER-05.

**15e — `NULL eval_before` produces a `TypeError` that is reported as an engine failure**
(`api.py:718` `cpl_diff = score_best - score_after`, bare `except Exception` at 736):
```
feedback_type='engine_error'
message: Move d4 is legal, but could not evaluate position due to engine error.
(the engine was never consulted; the real error is
 'unsupported operand type(s) for -: NoneType and int' at api.py:718)
```
`blunders.eval_before` is nullable in the schema and the value is never defaulted on read.

**15f — the UCI sanitizer truncates junk 5-character strings into a valid answer**
(`api.py:658-666` / `repertoire.py:2486-2493`): any 5-char string whose 4th character is not
`1`/`8` is silently cut to 4 chars, so `e2e4q` is graded as a correct `e4`:
```
'e2e4q'  -> correct=True  Correct! e4 is your prepared move.
'e2e4Q'  -> correct=True  Correct! e4 is your prepared move.
'e2e4xyz'-> correct=False Invalid move format: e2e4xyz.
```
Promotions are handled correctly (`a7a8r` → wrong, `a7a8Q` → correct), so this is tolerance
rather than a wrong grade, but it means the endpoint accepts input that is not a move.

**15g — `GET /train/openings` reports `move_count` for studies that have no user moves**
(`repertoire.py:2452` `s["user_moves"] or s["total_moves"] or 0`):
```
{'id': 2, 'name': 'OnlyOpponent', 'total_moves': 1, 'user_moves': 0}
-> {'opening_name': 'OnlyOpponent', 'move_count': 1, 'repertoire': 'white'}
```
Selecting such a study yields an empty drill queue; the count should reflect drillable moves.

## Root cause
Reporting is derived from raw counters with ad-hoc rounding/fallbacks, and the blunder
trainer's status/interval state machine has a branch (`good_alternative`) that is not part of
the same reset path as the others.

## Exploit / escalation
1. 15a/15b are user-visible lies in the trainer header and progress bar; a user cannot trust
   the move number or the segment widths.
2. 15c makes "due today" monotonically grow for the blunder trainer regardless of real due
   dates, which is the number used to decide whether to drill.
3. 15e is reachable for any legacy/imported blunder row with a NULL `eval_before`; combined
   with TRAINER-05 it also leaves `interval_days` un-reset.

## Unproven suspicions (could not demonstrate — do not treat as confirmed)
1. **`evaluate_trainer_move` never checks that the FEN's side to move matches `repertoire`.**
   `add_repertoire_move` has an explicit "BUG-10" guard for exactly this
   (`repertoire.py:444-454`), but the grading path does not, so a white drill could in
   principle be graded against a black move. I could not reach it through the public API
   because the add path is the only writer and it refuses the mismatch. Latent only.
2. **`-n["move"].get("is_important", 0)` (lines 2305/2333/2340) raises `TypeError` if
   `is_important` is SQL NULL**, which would surface as a 500 from `/train/next`. The column
   is `DEFAULT 0` and every writer coerces with `int()`, so I could not produce NULL through
   the API (only by hand-editing the row, which I did not do).
3. **"The advertised next position is always reachable" was only spot-checked**, not fuzzed:
   in TRAINER-09 section B I verified one continuing branch
   (`next expected legal in opponent fen: True`). I did not run a randomised tree fuzz, so a
   shape exists where `next_expected_uci` is not legal in `opponent_reply.fen`.
4. **The full-repertoire enumeration oracle** (TRAINER-06 variant 2) was demonstrated on a
   single position only; I did not walk a complete tree to dump every prepared move.
5. **Keyboard handler**: `window.addEventListener('keydown', …)` (line 117) has no
   modifier-key guard and is never removed, so `Ctrl+H` / `Cmd+L` also fire
   `retryCurrentPosition()` / `showHint()` and the listener lives for the app's lifetime.
   Verified by code reading only — I did not drive real key events in a browser.
6. **`board.playMove` failing silently on a desynced opponent reply**
   (`repertoire_trainer.js:707` ignores the `null` return that `board.js:523` produces) could
   not be triggered in the harness, because the desync it needs requires a real board drag
   that my stub cannot originate. This is the most likely *second* half of TRAINER-11's race.
7. The 5-char UCI truncation (15f) was tested only with suffixes of the form `[a-zA-Z0-9]` on
   a non-promotion move; I did not enumerate every 5-character string.
