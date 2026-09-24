# TRAINER-02 — Concurrent `/train/attempt` calls lose updates: 24 accepted answers recorded as 9, and one card jumps to a 747-day interval
- **Severity:** high
- **Type:** race
- **File:** backend/repertoire.py
- **Line(s):** 2509-2604 (read `prepared` → compute → `UPDATE`), 2555, 2582-2593
- **Component:** Repertoire trainer — spaced-repetition write path
- **Repro script:** `audit/scripts/atk2_06_concurrency.py`
- **Detected by:** ATTACKER-2 (TRAINER)

> Overlaps `ABUSE-05` (same lost-update class, found independently). This report adds the
> trainer-specific trigger (one user move through the UI produces two POSTs — TRAINER-12),
> the full SM-2 interval ladder reachable in milliseconds, and the
> *mastery-granted-on-dropped-writes* consequence (`tc == tp` while six graded mistakes vanish).

## Expected
`times_practiced` must equal the number of graded attempts, and `times_correct <= times_practiced`
must hold, no matter how many requests arrive at once. Counters should be updated with an
atomic `SET times_practiced = times_practiced + 1` (or under `BEGIN IMMEDIATE`).

## Actual
`evaluate_trainer_move` opens a connection, **reads** the row, computes in Python, then
**writes** absolute values:

```python
cursor.execute("""SELECT rm.*, … FROM repertoire_moves rm … LIMIT 1""")   # READ
prepared = cursor.fetchone()
…
prev_tp = max(0, int(prepared.get("times_practiced") or 0))               # in Python
new_tp  = min(9223372036854775807, prev_tp + 1)
cursor.execute("""UPDATE repertoire_moves SET times_practiced = ?, times_correct = ?,
                  interval_days = ?, last_practiced = ?, next_review = ? WHERE id = ?""", …)
```

No transaction, no `BEGIN IMMEDIATE`, no `WHERE times_practiced = ?` guard, and sqlite3's
default `isolation_level=""` only wraps the single `UPDATE` — so every concurrent request
reads the same starting values and the last writer wins.

Observed (24 threads released from a barrier onto one card):

```
A) 24 CONCURRENT correct attempts on the SAME card (single user answer)
   24 requests, 0.06s, HTTP codes=[200] correct=24
   times_practiced=9  times_correct=9  interval_days=747.3
   LOST UPDATES: 15 of 24 accepted answers were thrown away (tp should be 24)
   every response claimed: [True]

B) 24 CONCURRENT *wrong* attempts -> interval clobbering
   codes=[200]  final times_practiced=3 (expected 24)
   responses each reported times_practiced = [1, 2, 3]

C) mixed correct/wrong concurrently, then read the DB
   correct answers accepted: 6 / 6
   DB: times_practiced=2 times_correct=2 interval_days=3.0 next_review-now=259200
   invariant tc<=tp holds: True
```

Scenario C is the worst: **6 correct + 6 wrong answers were all answered HTTP 200 and
`correct: true/false` respectively, yet the database ends with `times_practiced=2
times_correct=2`** — i.e. the card is classified `mastered` (`tc>=2 and tc==tp`) although the
user failed it six times.

`interval_days=747.3` (>2 years) after only 9 recorded successes is the scheduling
corruption: the 3650-day cap is per *request*, not per *review*, so a burst of duplicate
requests walks the interval up the 1→3→6.6→…→747.3 ladder in milliseconds.

## Root cause
Read-modify-write of derived scheduling state outside any transaction, combined with
Python-side arithmetic. Note this is *not* SQLite lock contention (`timeout=30`, no
`database is locked` observed) — it is plain lost update.

## Exploit / escalation
1. **Reachable without any attacker**: `repertoire_trainer.js:633` guards entry with
   `if (!this.isWaitingForUser …) return;` but only sets `isWaitingForUser = false` *after*
   `await fetch(...)` resolves (line 698). While the POST is in flight the board is still
   interactive, and the `h`/`ArrowLeft` shortcut calls `retryCurrentPosition()`
   (line 606) which unconditionally re-arms input. Verified end-to-end in
   `audit/scripts/atk2_10_frontend_race.mjs` — see TRAINER-12.
2. **Interval inflation without concurrency**: `POST /train/attempt` is not bound to a
   served drill (TRAINER-06), so a loop of N requests walks the interval N steps.
3. **Blast radius**: the same pattern exists in `add_repertoire_move` (a re-add racing a
   trainer attempt can resurrect a stale row, see TRAINER-03).
