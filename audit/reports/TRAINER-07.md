# TRAINER-07 — Tier 1 "in-session lapse" re-test dies as soon as the session contains two mistakes, so mistakes are never re-tested
- **Severity:** medium
- **Type:** wrong-result
- **File:** backend/repertoire.py
- **Line(s):** 2281-2295 (Tier 1), 2298-2355 (Tiers 2/3 fallbacks)
- **Component:** Repertoire trainer — pedagogical drill selection
- **Repro script:** `audit/scripts/atk2_02_selection_edges.py` (sections 1 and 2b)
- **Detected by:** ATTACKER-2 (TRAINER)

## Expected
`AGENTS.md`/docstring: "1. In-Session Lapses (re-test mistakes made 2-5 moves ago)" is the
**highest priority** tier. A move the user just got wrong in this session must be re-served
before any due review, frontier move or maintenance move, regardless of how many other
lapses exist.

## Actual
Tier 1 only fires for a lapse that is *not* in `recent_ids` — or when there is exactly one
lapse:

```python
if recent_session_lapses:
    valid_lapses = [mid for mid in recent_session_lapses if mid in graph]
    if valid_lapses:
        for mid in valid_lapses:
            if mid not in recent_ids_set or len(valid_lapses) == 1:   # <-- dead once >=2
                chosen_node = graph[mid]; drill_reason = "session_lapse"; break
```

The frontend puts every lapse id into `sessionDrilledIds` (it pushes `data.prepared_id` on
every correct answer, `repertoire_trainer.js:668`, and every drill id on line 452-454), so a
lapse is *always* already in `recent_ids` by the time it could be re-tested. With one lapse
the `len(valid_lapses) == 1` escape hatch saves it; with two or more, Tier 1 is skipped
entirely and the session silently drops to `frontier_new` / `maintenance`.

Observed (two deep lapses, both already in `recent_ids` — the exact state after two mistakes):

```
e4 id=18  Nf3 ids=[17, 20]
2 deep lapses already in recent_ids -> reason=frontier_new id=18 san=e4 ply=1  (lapses were [17, 20])
same but recent_ids omitted        -> reason=session_lapse id=17 san=Nf3 ply=3
```

and with shallow lapses (where the maintenance fallback happens to coincide):

```
a) ONE lapse, and it is also in recent_ids (the normal frontend state):
   -> reason=session_lapse id=4 san=e4
b) TWO lapses, both also in recent_ids (after 2 mistakes in one session):
   -> reason=maintenance id=4 san=e4 ply=1
   (the two just-missed moves are NEVER re-tested: reason is not session_lapse)
```

Also observed: bogus ids are silently dropped (`lapses=999999,abc,-5` → `reason=maintenance`),
so a client cannot distinguish "no lapses" from "lapses filtered out".

## Root cause
The `mid not in recent_ids_set` condition is meant to avoid re-serving a card the user *just
drilled*, but `recent_ids` already contains every lapse id, so it excludes exactly the set
Tier 1 is supposed to prioritise. There is no separate "recently served" vs "already lapsed"
bookkeeping, and no ageing rule (the docstring promises "2-5 moves ago").

## Exploit / escalation
1. Normal flow: a user who makes two mistakes in a 20-move session — exactly the population
   the intra-session buffer exists for — never sees either mistake again, and the UI still
   shows the moves as buffered in `sessionLapses`.
2. Because `lapses` is a plain comma list from the client, sending
   `lapses=<one id>&recent_ids=<same id>` forces Tier 1 forever for that card, so a single
   "poisoned" card can be pinned into the drill rotation indefinitely.
3. Combined with TRAINER-06, the lapse id that is never re-tested also never gets a second
   `times_practiced`, so the SM-2 interval it earned on the wrong answer is never repaired.
