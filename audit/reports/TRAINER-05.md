# TRAINER-05 — Blunder trainer: a "good alternative" answer never resets the interval, so the next correct answer multiplies a stale interval and the card jumps to 625 days
- **Severity:** high
- **Type:** wrong-result
- **File:** backend/api.py
- **Line(s):** 686-700, 703-741, 744-754
- **Component:** Blunder drill trainer (`POST /api/train/evaluate_move`)
- **Repro script:** `audit/scripts/atk2_07_blunder_trainer.py`
- **Detected by:** ATTACKER-2 (TRAINER)

## Expected
Every branch of the spaced-repetition update that is *not* the exact engine best move is a
"failed/partial review" and must reset `interval_days` to 1.0 before the next
`next_review_date` is computed. The `blunder` and `suboptimal` branches do this
(`interval = 1.0`); `good_alternative` and `engine_error` do not.

## Actual
`interval` is read from the row and only overwritten in *some* branches:

```python
interval = blunder["interval_days"] or 1.0          # 100.0 in the fixture
…
if is_exact_best:
    interval = min(interval * ease, 3650.0) if successes > 1 else 1.0   # 100 * 2.5 = 250
elif is_original_blunder:
    interval = 1.0                                                     # correct
else:
    …
    if cpl_diff <= 35:
        successes += 1
        feedback_type = "good_alternative"
        …
        next_review = now + 86400          # 1 day …
        # … but `interval` is left at 100.0 and is written back by the INSERT below
    else:
        feedback_type = "suboptimal"
        interval = 1.0                     # correct
```

Observed (card with `attempts=5 successes=4 interval_days=100`):

```
A) well-trained card (interval=100d) + a GOOD ALTERNATIVE answer -> is it reset?
   initial                          attempts=5   successes=4   interval_days=100.0   next_in_days=-1.0    status=mastered
   feedback_type=good_alternative is_correct=True
   message: Good alternative: d4 (Diff: -6 cp). Engine top choice: e4.
   after good-alternative  <-- interval NOT reset attempts=6 successes=5 interval_days=100.0 next_in_days=1.0 status=learning

B) then the exact best move -> the STALE interval is multiplied by the ease factor
   feedback_type=correct
   after 1st exact best               attempts=7   successes=6   interval_days=250.0   next_in_days=250.0    status=mastered
   after 2nd exact best               attempts=8   successes=7   interval_days=625.0   next_in_days=625.0    status=mastered
   => card jumped from a 1-day review to 625 days (1.7 years)

C) contrast: replaying the ORIGINAL blunder move DOES reset the interval
   reset to interval=100              attempts=8   successes=6   interval_days=100.0   next_in_days=625.0    status=mastered
   feedback_type=blunder
   after replaying the game move      attempts=9   successes=7   interval_days=1.0     next_in_days=1.0     status=learning
```

The user is shown "due tomorrow", answers correctly, and the card disappears for 1.7 years.

## Root cause
`interval` is a local variable that is conditionally reassigned; the final write
(`… interval_days …`, line 754) always persists whatever value survived, so any branch that
forgets the reset leaks the previous (possibly 3650-day) interval into the next multiplication.

## Exploit / escalation
1. `engine_error` has the identical defect and is trivially reachable: a `NULL`
   `blunders.eval_before` makes line 718 raise
   `TypeError("unsupported operand type(s) for -: 'NoneType' and 'int'")`, which the bare
   `except Exception` converts into `feedback_type="engine_error"`, and that branch also
   leaves `interval` untouched (observed in the same run, section D). The user is told
   "could not evaluate position due to engine error" although the engine was never started.
2. `status = "mastered" if successes >= 2 else "learning"` is only applied in the
   `is_exact_best` branch, so a card answered correctly via `good_alternative` 9 times stays
   `learning` forever (observed: `successes=9 but drill_status='learning'`), and
   `/api/train/paths` will never count it as mastered.
3. `ease_factor` is never updated, so the multiplier is frozen at 2.5 forever — a
   well-known SM-2 omission that amplifies (2): one stale interval is enough.
