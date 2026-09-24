# TRAINER-06 — `/train/attempt` is not bound to the drill that was served: the same answer can be replayed to walk a card up the whole SM-2 ladder
- **Severity:** medium
- **Type:** spec-violation
- **File:** backend/repertoire.py
- **Line(s):** 2469-2533 (`evaluate_trainer_move` signature and prepared lookup)
- **Component:** Repertoire trainer — attempt endpoint
- **Repro script:** `audit/scripts/atk2_04_studyfilter_replay.py` (section D)
- **Detected by:** ATTACKER-2 (TRAINER)

## Expected
An attempt should be bound to the drill it answers: the endpoint should take the drill `id`
(or a signed token) and reject attempts for positions that were never served, for a colour
that does not match, or for a study that does not contain the position. At minimum a replay
of an identical request must be idempotent.

## Actual
`TrainerMovePayload` carries only `repertoire`, `current_fen`, `attempted_uci`,
`study_name`, `opening_filter`. The position is resolved purely from `current_fen`
(normalised to 3 tokens) and the study filter is **not** applied to the graded row:

```python
cursor.execute("""SELECT rm.*, COALESCE(rs.name, rm.study_name) as effective_study
                   FROM repertoire_moves rm …
                   WHERE rm.repertoire = ? AND (rm.parent_fen = ? OR rm.parent_fen = ?
                         OR rm.parent_fen LIKE ?) AND rm.is_user_move = 1
                   LIMIT 1""", …)          # <- no study predicate, no drill id
```

Observed:

```
d1) never served by /train/next at all - direct attempts on the deepest position:
   attempt 1 -> correct=True tp=1 tc=1
   attempt 2 -> correct=True tp=2 tc=2
   attempt 3 -> correct=True tp=3 tc=3
   (prerequisites 1.e4/2.Nf3 were never learned - the drill is still credited)

d2) the SAME answer POSTed 5 times (double-click / retry storm):
   1.e4 times_practiced=5 times_correct=5 interval_days=31.9

d3) stale FEN (same position, older move counters / other ep token):
   halfmove=99 fullmove=250 -> correct=True (accepted, tp=6)

d4) study_name that does not exist / belongs to the other colour:
   study_name='Does Not Exist' -> correct=True study_name='Does Not Exist'
   study_name='Alpha'          -> correct=True study_name='Alpha'
   study_name='Sicilian Dragon' -> correct=True study_name='Sicilian Dragon'
```

Five identical requests take a brand-new card from `unlearned` to a 31.9-day interval, and
the answer is credited for a position whose prerequisites were never learned (`is_frontier`
/`prerequisites_met` are advisory only — nothing checks them on write).

## Root cause
The write path trusts the client for *which* card is being answered and derives the card from
an unauthenticated, unvalidated FEN, then applies Python-side arithmetic to absolute values
(amplified by TRAINER-02).

## Exploit / escalation
1. No crafted input needed: the frontend's own double-submit (TRAINER-12) produces exactly
   this, and the interval ladder `1 → 3 → 6.6 → 14.5 → 31.9 → 70.2 → 154.4 → 339.7 → 747.3
   → 1643.3 → 3650` can be traversed in under a second of wall clock.
2. Because a wrong attempt returns `expected_uci`/`expected_san` for *any* in-tree position,
   the same endpoint doubles as a full-repertoire enumeration oracle: walk the tree with
   deliberately wrong moves and read the prepared move out of the error message.
3. `study_name` is echoed back into the response (`study_name` / `next_study_name`) without
   ever being validated, so a client can persist an arbitrary string into the trainer's
   notion of "current study" for the rest of the line.
