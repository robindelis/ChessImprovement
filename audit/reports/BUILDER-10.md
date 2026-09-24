# BUILDER-10 — `GET /api/repertoire/explore?ratings=<free text>` is an unbounded, never-evicted write into `explorer_cache`
- **Severity:** low
- **Type:** security
- **File:** backend/repertoire.py
- **Line(s):** 218-221 (`get_candidate_moves_2000` signature: `ratings: str = "2000,2200,2500"`), 236 (the cache read + 7-day validity window), 355-360 (the `INSERT OR REPLACE INTO explorer_cache`); the query parameter is `backend/api.py:1048` (`ratings: str = Query("2000,2200,2500")`); the schema is `backend/db.py` `explorer_cache(fen, ratings, data_json, updated_at, PRIMARY KEY (fen, ratings))`
- **Component:** Repertoire Builder — 2000+ explorer cache
- **Repro script:** `audit/scripts/atk1_b10_explorer_cache.py`
- **Detected by:** ATTACKER-1 (BUILDER)

## Expected

A cache should be bounded in both directions: the key space should be validated/canonicalised,
and expired entries should be reaped. `explorer_cache` is keyed on `(fen, ratings)` where
`ratings` is a raw client-supplied string, and nothing in the codebase ever deletes from the
table.

## Actual

```
A. ratings is a free-text query parameter and is part of the cache PRIMARY KEY
   ratings='2000,2200,2500'                            -> HTTP 200, 3 moves
   ratings='2000'                                      -> HTTP 200, 3 moves
   ratings='1500,1600,1700,1800,1900'                  -> HTTP 200, 3 moves
   ratings='not-a-rating-list'                         -> HTTP 200, 3 moves
   ratings="'; DROP TABLE repertoire_moves; --"        -> HTTP 200, 3 moves
   ratings='xxxx…' (4000 chars)                        -> HTTP 200, 3 moves

   explorer_cache now holds 6 rows:
     fen='rnbqkbnr/pppppppp/8/8/3P4/8/'  ratings='1500,1600,1700,1800,1900'  len=24
     fen='rnbqkbnr/pppppppp/8/8/3P4/8/'  ratings='xxxx…'                      len=4000
     fen='rnbqkbnr/pppppppp/8/8/4P3/8/'  ratings="'; DROP TABLE repertoire_moves" len=34
     fen='rnbqkbnr/pppppppp/8/8/4P3/8/'  ratings='2000'                       len=4
     fen='rnbqkbnr/pppppppp/8/8/8/8/PP'  ratings='2000,2200,2500'             len=14
     fen='rnbqkbnr/pppppppp/8/8/8/8/PP'  ratings='not-a-rating-list'          len=17

B. nothing ever deletes from explorer_cache
   DELETE/VACUUM against explorer_cache in repertoire.py: 0 occurrence(s)
   DELETE/VACUUM against explorer_cache in db.py         : 0 occurrence(s)
   INSERT OR REPLACE INTO explorer_cache in repertoire.py: 1

   the read path treats an entry as valid for 7 days and then simply ignores it:
     # Cache valid for 7 days
     if time.time() - cached["updated_at"] < 86400 * 7

   ageing every row past the 7-day window and re-reading:
     explorer_cache rows after a full expiry + a fresh read: 6 (nothing reaped)
```

Every distinct `(fen, ratings)` pair is a permanent row. The 7-day window only affects whether
a row is *read*; an expired row is left on disk forever, and the same FEN re-queried with the
same `ratings` string `INSERT OR REPLACE`s it (so the natural growth driver is
*positions × rating-bucket variants*, all of which stay resident).

## Root cause

```python
# backend/repertoire.py:236-238
cached = cursor.fetchone()
if cached:
    # Cache valid for 7 days
    if time.time() - cached["updated_at"] < 86400 * 7:
        ...                       # <- expired rows fall through and are never removed
```

```python
# backend/repertoire.py:355-360
if req_success and (len(candidate_moves) > 0 or total_games == 0):
    cursor.execute("""
    INSERT OR REPLACE INTO explorer_cache (fen, ratings, data_json, updated_at)
    VALUES (?, ?, ?, ?)
    """, (norm_fen, ratings, json.dumps(result_payload), int(time.time())))
```

`ratings` is never parsed, canonicalised, length-limited or allow-listed, and there is no
`DELETE FROM explorer_cache WHERE updated_at < …` anywhere in the repo (verified by counting
occurrences in both `repertoire.py` and `db.py`). `clear_repertoire_database()` — the only
"reset" in the app — does not touch `explorer_cache` either.

## Exploit / escalation

Adjacent weaker variants:

1. **Local, not remote, exposure** — this is a single-user desktop app, so the realistic
   trigger is the *frontend* varying the string. `/api/repertoire/explore` is called with only
   the hard-coded default today, but any future rating-band selector multiplies the key space
   by the number of bands, and the rows are never collected.
2. **The same unbounded-cache shape exists for the eval cache** in `evaluator.py` — that one at
   least has an LRU bound (`_cache_max_size = 1000`).
3. **No SQL injection** (all statements are parameterised) and **no XSS** — worth recording
   explicitly: the `'; DROP TABLE …` string is stored verbatim as a primary-key component and
   is never rendered into HTML (`opt.textContent`, `textContent` sinks only).
4. **Compounding factor for BUILDER-06:** because a failed/unavailable upstream request is
   never negatively cached, a degraded network re-issues the request on every refresh — and each
   distinct `ratings` string the user (or a script) supplies mints a fresh permanent row.
