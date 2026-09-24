# ABUSE-19 — The new `get_master_stats` single-flight is keyed by the RAW FEN, so it never dedupes the case it was built for: 40 views of one position still make 40 outbound calls
- **Severity:** medium
- **Type:** race / security (resource amplification against a third party) / spec-violation
- **File:** `backend/api.py`
- **Line(s):** 1477-1519 (`get_master_stats`); specifically the memo lookup at **1487**, the lock acquisition at **1499**, and `_remember_explorer_stats` at **1516-1519** — all keyed on the raw `fen` argument; contrast `_normalize_explorer_fen()` at 1427-1432, which is the key actually used for `explorer_cache` at 1445
- **Component:** Lichess 2000+ Explorer integration / blunder detail view (the SYNC-08 fix)
- **Repro script:** `audit/scripts/atk5_re2_2_memo_key.py`
- **Detected by:** ATK-5 (re-attack pass 2)

## Expected

The fix states its own goal at `api.py:1403-1404`:

> `3. a single-flight lock, so N concurrent views of the same position make ONE outbound call, not N.`

and `AGENTS.md` §4F is unambiguous about what a cache key is in this codebase:

> `normalize_fen()` keeps the **first four** FEN tokens … This 4-token form is the join/cache key for
> `repertoire_moves.parent_fen`, `study_moves.parent_fen`, `explorer_cache.fen` and `blunders.fen_before`.

So the memo and the lock must be keyed by the **4-token** form. Keyed that way, two blunder rows that describe
the same position — the normal case, because the same opening is reached in many games with different clocks —
would share one memo entry and one lock, and the stated goal would hold.

## Actual

The memo and the lock are keyed on the **raw 6-token FEN**, while the database layer directly beneath them uses
the normalised form. The result is three different keys for one position.

### The two key functions disagree (probe §1)

```
    raw FEN                     : 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1'
    _normalize_explorer_fen()    : 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3'   <- the explorer_cache key
    memo / lock key             : 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1'   <- raw, NOT normalised
```

### A real transposition proves the divergence is not theoretical (probe §3)

`1.e4 c6 2.d4 d5 3.Nc3` and `1.e4 c6 2.Nc3 d5 3.d4` reach an **identical board** by different move orders. The
probe asserts this rather than asserting it in prose:

```
    game A fen_before : 'rnbqkbnr/pp2pppp/2p5/3p4/3PP3/2N5/PPP2PPP/RNBQKBNR b KQkq - 1 3'
    game B fen_before : 'rnbqkbnr/pp2pppp/2p5/3p4/3PP3/2N5/PPP2PPP/RNBQKBNR b KQkq - 0 3'
    same raw string?  : False
    same 4-token key? : True
    -> PROVEN: a genuine transposition. Two blunders at ONE position -> two
       memo keys, two locks, TWO outbound calls, for ONE explorer_cache row.
```

This is not a contrived input. Transpositions are the normal case in a chess library: the user has 153 blunders
recorded today, and the blunder detail view is opened per row.

### Measured (probe §4)

40 blunder FENs, all sharing ONE 4-token position, differing only in the clock tokens:

```
    concurrent -> outbound calls: 40 for 40 views (1.00 per request), wall 0.03s
    memo entries created       : 40 (expected 40 if keyed per raw FEN)

    the same N requests, SEQUENTIALLY (memo now warm):
    outbound calls             : 0  (memo works per raw FEN)

    the same POSITION, one raw FEN, N concurrent (what the fixer intended):
    outbound calls             : 1 for 40 views  <-- single-flight DOES work when the key is identical
```

The third line is the important control: the single-flight is **correct**, it is just **keyed wrong**. It fires
only when two requests carry the *byte-identical* FEN, which is the same-blunder-reopened case the memo already
served. The cross-blunder, same-position case — the one the comment claims to fix — is untouched.

End-to-end over a real uvicorn socket, 48 concurrent blunder-detail views seeded to share one position
(`audit/scripts/atk5_re2_1c_threadpool_final.py`):

```
    requests 48 | wall 4.07s   (one call = 2.0s, fully parallel ~2.00s)
    outbound calls : 47 for 48 requests (0.98 per request)
    single-flight: NOT engaged -- 47 calls
```

and the control, 16 concurrent views of the *same blunder id*:

```
    requests 16 | wall 0.03s
    outbound calls : 0 for 16 requests (0.00 per request)
```

So the fix works exactly as hard as the memo, and the memo works exactly as hard as the key. The key is the bug.

## Root cause

`get_master_stats(fen)` receives `data["fen_before"]` — the full 6-token FEN stored on the blunder row
(`api.py:1569`). It then uses that string three ways:

```python
memo = _explorer_stats_memo.get(fen)          # :1487  raw
...
lock = _explorer_stats_lock(fen)              # :1499  raw
...
cursor.execute("... WHERE fen = ? ...", (_normalize_explorer_fen(fen), EXPLORER_STATS_RATINGS))   # :1445  normalised
```

`_normalize_explorer_fen()` already exists in the same file, 50 lines above, and is already applied to the one
query that needs a stable key. The memo and the lock were simply not routed through it. The consequence is that
the single-flight lock provides mutual exclusion only for *identical strings*, so:

* the TTL is per-raw-FEN — a position reached in 20 games gets 20 memo entries and 20 re-fetches per 6 h window
  instead of one;
* the single-flight never engages across rows, so N concurrent views of one position still make N calls;
* `explorer_cache` is checked first (`_master_stats_from_explorer_cache`, `:1494`) and *would* have absorbed
  the repeat calls — but only for positions some other code path has already cached. `get_master_stats` itself
  never writes `explorer_cache`, so on a cold cache the amplification is complete.

## Exploit / escalation

Reached by: opening the blunder detail view (`GET /api/blunders/{id}`, `include_master_stats` defaults to
**True** at `api.py:1529` and **nothing in `frontend/` ever passes `false`** — `grep -rn include_master_stats
frontend/` returns 0 hits) for many blunders that share a position. The origin boundary means this is a
loopback/local action rather than a drive-by, but the app is unauthenticated by design on the loopback
interface and has no per-caller outbound budget.

Adjacent variants of the same root cause:

1. **Cache-destruction cliff (measured, probe §5).** The memo is bounded by *clearing the whole dict*, not by
   evicting one entry:
   ```
       700 distinct FENs, sequential: 700 outbound calls, 7.77s
       memo size now: 187
   ```
   Because `get_master_stats` does not populate `explorer_cache`, a client that walks 513 distinct FENs
   discards **every** warm entry, and the next view of any previously-warm position re-fetches. That converts a
   6-hour TTL into a per-flood cost.
2. **Lock-dictionary growth — reported separately as ABUSE-20.** The memo beside the lock is capped at 512; the
   lock dict is not capped at all.
3. **Compounds ABUSE-17.** That finding is the same stampede in the sibling function
   (`get_candidate_moves_2000`), which has no single-flight at all. Both are in the same area of the same
   outbound budget and should be fixed in one pass.
4. **The failure TTL does not cover this path.** `EXPLORER_FAILURE_TTL = 120` is applied per raw FEN, so a
   position that fails is retried once per *row* rather than once per *position*.

## Suggested fix shape (for the `api.py` owner)

* Key the memo, the lock **and** `_remember_explorer_stats` on `_normalize_explorer_fen(fen)`. That is a
  three-line change and it makes the stated goal true.
* Replace the whole-dict `clear()` at `:1518` with a real eviction (drop the oldest entry, or use
  `collections.OrderedDict` + `move_to_end`), so a flood cannot discard the entire warm cache.
* Consider writing the successful payload into `explorer_cache` (the table `get_candidate_moves_2000` already
  writes, and which `_master_stats_from_explorer_cache` already reads) so the amplification is bounded at the
  durable layer and survives a restart.
* Regression test to add: seed two blunder rows with the **same 4-token position but different move counters**,
  request both concurrently, and assert exactly one outbound call. The existing SYNC-08 test evidently re-opens
  one row, which is why this was not caught.
