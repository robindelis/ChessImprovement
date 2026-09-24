# TRAINER-08 — Tier 2 (`due_review`) sorts `is_important` backwards: priority moves are never selected when urgency ties
- **Severity:** medium
- **Type:** wrong-result
- **File:** backend/repertoire.py
- **Line(s):** 2297-2307 (Tier 2), 2333 & 2340 (Tier 3, for contrast)
- **Component:** Repertoire trainer — drill selection tie-break
- **Repro script:** `audit/scripts/atk2_01_selection_scheduling.py` (section B)
- **Detected by:** ATTACKER-2 (TRAINER)

## Expected
`is_important` is documented in `AGENTS.md` §5 as "`1` if marked as priority move for
opening trainer". When two due cards have the same Ebbinghaus urgency, the priority card must
win. Tier 3 gets this right; Tier 2 does not.

## Actual
Tier 2 negates the flag **and** reverses the whole sort:

```python
available_due.sort(key=lambda n: (n["urgency"], -n["move"].get("is_important", 0)), reverse=True)
chosen_node = available_due[0]
```

With `reverse=True` the tuple is sorted descending, so for equal `urgency` the tuple
`(1.0, 0)` (not important) sorts before `(1.0, -1)` (important) — the negation is cancelled
by the reversal and the priority card is pushed to the back.

Tier 3 does the same negation but sorts ascending, which is why it is correct:

```python
matching_frontier.sort(key=lambda n: (n["ply_depth"], -n["move"].get("is_important", 0), …))  # ascending -> important first
```

Observed — two depth-3 white cards (`2.Nf3` after `1...e5` and after `1...c5`) with
identical `urgency` (both `tp=1, tc=1, interval=1.0, next_review = now-86400`), one flagged
important via `POST /api/repertoire/move_meta`:

```
B) TIER 2 due_review: is_important tie-break at identical urgency
   due pick: imp=False san=Nf3 reason=due_review      (x12)
   distinct positions drilled: 1 -> [12]
```

The important card is selected **0 of 12** times. For contrast, the same fixture in
`mode=frontier_only` (Tier 3) selects the important card 20/20 times:

```
   important id = 13  plain id = 10
   picks over 20 frontier_only calls: {13: 20}
   -> important move selected 20/20 times
```

## Root cause
Sign error: `-is_important` is only correct for an ascending sort. Line 2305 combines it
with `reverse=True` (which exists to prioritise the highest `urgency`).

## Exploit / escalation
1. The realistic case is a daily review session where several cards came due at the same
   time (very common right after a bulk import or a `seed?force=true`): the cards the user
   explicitly starred with "Mark Important" are exactly the ones deferred.
2. `mode=due_only` (the "spaced repetition only" mode in the UI) consists *purely* of Tier 2
   picks, so in that mode `is_important` has no effect at all.
3. Because the ordering is a total order over the whole due list, a single non-important card
   that is marginally more overdue will permanently shadow the priority card in that session
   (`urgency` is a float ratio, so near-ties are common).
