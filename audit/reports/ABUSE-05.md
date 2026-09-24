# ABUSE-05 — Read-modify-write on the spaced-repetition counters loses 70–90 % of concurrent updates and inflates the review interval
- **Severity:** high
- **Type:** race
- **File:** `backend/repertoire.py`
- **Line(s):** 2770-2826 (SELECT … `prev_tp`/`prev_tc` → UPDATE with an absolute value); sibling in `backend/api.py:940` + 1002-1012 (`drill_reviews.attempts`)
- **Component:** trainer / spaced repetition
- **Repro script:** `audit/scripts/atk5_concurrency_repro.py`
- **Detected by:** ATK-5

## Expected
`times_practiced`, `times_correct`, `attempts`, `successes`, `interval_days` and
`next_review` are the *entire* state of the spaced-repetition scheduler. Two
concurrent increments of the same counter must both be recorded, and the value
must be derived atomically (`SET times_practiced = times_practiced + 1`) inside a
single transaction, never read-then-written.

## Actual
`backend/repertoire.py` reads the previous values, then writes back
`prev + 1` as an absolute value:

```python
            prev_tp = max(0, int(prepared.get("times_practiced") or 0))
            prev_tc = max(0, int(prepared.get("times_correct") or 0))
            ...
            if not is_match:
                new_tp = min(9223372036854775807, prev_tp + 1)
                cursor.execute("""UPDATE repertoire_moves
                    SET times_practiced = ?, interval_days = 1.0, last_practiced = ?, next_review = ?
                    WHERE id = ?""", (new_tp, now, now + 86400, prepared["id"]))
            ...
            new_tp = min(9223372036854775807, prev_tp + 1)
            new_tc = min(9223372036854775807, prev_tc + 1)
            if prev_tc == 0:   new_interval = 1.0
            elif prev_tc == 1: new_interval = 3.0
            else:              new_interval = min(3650.0, max(3.0, round(prev_interval * 2.2, 1)))
            cursor.execute("""UPDATE repertoire_moves
                SET times_practiced = ?, times_correct = ?, interval_days = ?,
                    last_practiced = ?, next_review = ? WHERE id = ?""", …)
```

`backend/api.py:940` has the identical shape for the blunder drills:

```python
    attempts = (blunder["attempts"] or 0) + 1
    successes = (blunder["successes"] or 0)
    …
    INSERT INTO drill_reviews (blunder_id, attempts, successes, …) VALUES (…)
    ON CONFLICT(blunder_id) DO UPDATE SET attempts = excluded.attempts, successes = excluded.successes, …
```

### Observed (`atk5_concurrency_repro.py`, 20 threads on 20 `TestClient`s, temp DB)

```
A) /api/repertoire/train/attempt  x20 PARALLEL correct answers
  before: {'id': 1, 'move_uci': 'e2e4', 'times_practiced': 0, 'times_correct': 0, 'interval_days': 1.0}
  statuses: Counter({200: 20})   errors: []
  reported times_practiced values: [1, 2, 3, 4, 5, 6]
  after  : {'id': 1, 'times_practiced': 6, 'times_correct': 6, 'interval_days': 70.2}
  >>> 20 requests issued, times_practiced = 6 (expected 20) -> LOST UPDATES: 14 lost
```

(an earlier run of the same probe against the pre-patch tree lost 18 of 20)

```
B) /api/train/evaluate_move  x20 PARALLEL on one blunder
  before drill_reviews: {'attempts': 300, 'successes': 300}
  after  drill_reviews: {'attempts': 303, 'successes': 303}   # 17 of 20 lost
```

All 20 requests returned HTTP 200, so the loss is completely silent.
Worse than the count loss, the *derived* state is corrupted: 6 recorded "correct
answers" pushed `interval_days` from 1.0 to **70.2** days, i.e. the scheduler
now thinks a move has been reviewed successfully many times when it was reviewed
fewer times than the requests, and mastery gating
(`mastered = new_tc >= 2 and new_tc == new_tp`) can be reached by duplicated
traffic.

## Root cause
Classic TOCTOU / lost update: `SELECT` (line 2770) and `UPDATE` (line 2813) are
separate statements on separate connections inside the FastAPI threadpool, with no
`BEGIN IMMEDIATE`, no `WHERE times_practiced = <observed>` optimistic guard, and
no `times_practiced = times_practiced + 1` arithmetic. The `ON CONFLICT DO UPDATE
SET attempts = excluded.attempts` clause in `api.py` has the same problem: the
new value is computed outside the transaction from a stale read.

## Exploit / escalation
1. **Normal UI, not just a fuzzer.** `POST /api/repertoire/train/attempt` is fired
   by `frontend/repertoire_trainer.js` on *every* user move with no client-side
   de-duplication and no in-flight guard. A double-click, an impatient drag
   (the trainer auto-plays the opponent reply while the user clicks ahead), a
   browser retry, or two browser tabs pointing at the same backend all produce
   exactly this race. The desktop app is a single window, but the backend is a
   plain HTTP server on 38281 that any tab in the default browser can also reach.
2. **Adjacent — progress inflation is unbounded in the other direction.** Because
   the interval is recomputed from the *stale* `prev_interval` on every request,
   a client that replays the same correct move 20× serially drives
   `interval_days` toward the 3650-day cap and permanently removes the move from
   the due queue. The probe demonstrates the serial case too:
   `before=2  after 5 identical POSTs=7 (NOT idempotent: a retried request
   double-counts)`.
3. **Adjacent — cross-tenant escalation of a different kind:** since there is no
   auth (ABUSE-03), *any* local page can inflate `times_correct` to force
   `mastery_status = "mastered"` and thereby corrupt the trainer's pedagogical
   selection and the `/api/repertoire/train/progress` statistics the user relies on.

**Required fix:** make the increment atomic
(`SET times_practiced = times_practiced + 1, times_correct = times_correct + ?`)
inside `BEGIN IMMEDIATE`, derive `interval_days` from the value read *inside* the
same transaction (or recompute with a pure function of the post-update row), and
return the row's post-update values so the client sees the truth.
