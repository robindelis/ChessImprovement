# TRAINER-09 — Opponent branches that end the line are never played while any continuing branch exists, so prepared lines are silently never trained and `is_line_end` is never reached
- **Severity:** medium
- **Type:** wrong-result
- **File:** backend/repertoire.py
- **Line(s):** 2637-2682 (opponent reply selection), 2686 (`is_line_ended`)
- **Component:** Repertoire trainer — opponent reply selection
- **Repro script:** `audit/scripts/atk2_05_answer_lineend_hint.py` (sections A and B)
- **Detected by:** ATTACKER-2 (TRAINER)

## Expected
`popularity_pct` exists so the trainer plays the opponent's *actual* main lines. A user who
prepares `1...c5` (the Sicilian) alongside `1...e5` expects both to be played back with
roughly their database frequency, and a branch that terminates the drill should sometimes be
chosen so `is_line_end` is exercised.

## Actual
The reply list is first partitioned into "has a prepared user follow-up" and the rest is
**discarded entirely** if the first partition is non-empty:

```python
if replies_with_followup:
    weights = [max(1.0, float(r[0].get("popularity_pct") or 0.0)) for r in replies_with_followup]
    chosen_pair = random.choices(replies_with_followup, weights=weights, k=1)[0]
    …
else:
    chosen = random.choices(opp_replies, weights=weights, k=1)[0]   # only when NO branch continues
```

`popularity_pct` is therefore only ever used to choose *between continuing* branches; a
popularity of 0 vs 100 makes no difference to whether a branch is played at all.

Observed — white study `T` = `1.e4` with `1...e5 2.Nf3` (pop 1) and `1...c5` (pop 100, leaf),
20 correct answers to `1.e4`:

```
A) 20 correct answers -- which opponent reply is ever played?
   opponent replies played: {'e5': 20}
   -> 1...c5 (pop=100, the line the repertoire actually prepares for) NEVER played
```

`is_line_end` is a constant `False` in that fixture, and the "Line Complete!" path
(`repertoire_trainer.js:780-802`) is unreachable for that card.

## Root cause
A greedy "prefer continuations" filter applied *before* weighting, instead of weighting all
replies and only using follow-up availability as a tie-break (or as a probability).

## Exploit / escalation
1. Any repertoire with a main line plus a shorter/sideline branch (extremely common: the user
   prepares `1...c5` and `1...e5`, or `1...Nf6` and `1...d5`) loses the sideline permanently.
2. `is_line_end` becoming unreachable means the `next_expected_uci = None` contract is never
   exercised, so the frontend's line-completion branch rots untested — exactly the path that
   `tests/test_trainer_hint_and_line_completion.py` claims to protect (that test only covers
   the case where *no* branch continues).
3. Related ordering bug: the SM-2 counters are **committed at line 2604**, before the
   opponent-reply phase runs. Any failure in that phase therefore leaves a committed write
   behind an error response, and the client's retry counts the card twice. Reproduced with a
   deliberately corrupted `popularity_pct` (not reachable through the validated
   `POST /api/repertoire/move`, which enforces `ge=0.0, le=100.0`):

   ```
   HTTP 400 {"detail":"could not convert string to float: 'abc'"}
   counters after: {'times_practiced': 1, 'times_correct': 1, 'interval_days': 1.0}
   ```
