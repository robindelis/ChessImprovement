# ABUSE-20 — `_explorer_stats_locks` is unbounded and never pruned: one `threading.Lock` per distinct FEN ever requested, while the memo beside it is capped at 512
- **Severity:** low
- **Type:** security (unbounded resource) / memory leak
- **File:** `backend/api.py`
- **Line(s):** 1414 (`_explorer_stats_locks: Dict[str, "threading.Lock"] = {}`), 1418-1424 (`_explorer_stats_lock` — creates and stores, never removes), 1516-1519 (`_remember_explorer_stats` — the memo *is* bounded; contrast)
- **Component:** Lichess 2000+ Explorer integration (the SYNC-08 single-flight)
- **Repro script:** `audit/scripts/atk5_re2_2_memo_key.py` (section 6)
- **Detected by:** ATK-5 (re-attack pass 2)

## Expected

Any per-key bookkeeping introduced alongside a bounded cache should be bounded the same way. The author clearly
understood this: `_explorer_stats_memo` is capped at `EXPLORER_STATS_MEMO_LIMIT = 512` (`api.py:1410`) and
`_remember_explorer_stats` enforces it (`:1517-1518`). The lock dictionary introduced in the same commit has no
equivalent guard.

## Actual

`_explorer_stats_lock(fen)` inserts a `threading.Lock` per distinct FEN and never removes it:

```python
def _explorer_stats_lock(fen: str) -> "threading.Lock":
    with _explorer_stats_locks_guard:
        lock = _explorer_stats_locks.get(fen)
        if lock is None:
            lock = threading.Lock()
            _explorer_stats_locks[fen] = lock      # :1423  inserted, never evicted
        return lock
```

Measured (probe section 6), 2000 distinct FENs through `get_master_stats`:

```
    _explorer_stats_memo   : 461 entries (bound 512)
    _explorer_stats_locks  : 2000 entries <-- NO bound, no prune, one Lock per distinct FEN ever seen
```

The asymmetry is the tell: the memo stays under its limit while the lock dict grows monotonically past it. Each
entry is a `threading.Lock` (~64 bytes of object plus internal state) keyed by a ~70-character FEN string, so
2000 positions is roughly 300 KB of permanently retained bookkeeping in a desktop app whose entire working set
is meant to be a personal chess library.

## Root cause

The eviction logic added for the memo (`if len(...) > LIMIT: clear()`, `:1517-1518`) was not applied to the lock
dictionary. The two are created and used together in the same function and are always 1:1 in creation — every
memo key is also a lock key — so bounding one and not the other is an oversight rather than a design choice.

Note the *direction* of the existing memo eviction is also worth flagging to whoever fixes this: `clear()` drops
**every** entry rather than evicting one, so crossing 512 FENs discards the whole warm cache. That is
independently measurable (ABUSE-19, escalation variant 1) and a real amplifier, though not a memory-safety
problem. An LRU or `OrderedDict.move_to_end` + `popitem(last=False)` fixes both the cliff and this leak.

## Exploit / escalation

Reached by: any client that requests blunder details for many distinct positions. `GET /api/blunders/{id}`
takes the FEN from the stored blunder row (`api.py:1569`), so the natural driver is *the user's own library* —
simply browsing the blunder list grows the dict monotonically, and it never shrinks for the lifetime of the
process. A loopback client that walks arbitrary FENs through the explore route grows it faster.

Adjacent variants of the same root cause:

1. **The same pattern may exist in the sibling cache.** `RepertoireManager._explorer_count_memo`
   (`backend/repertoire.py:97`) is bounded the same way (`:258-260`, 512 + `clear()`), so it is consistent today
   — but it is the third per-FEN dict in the codebase and any future lock beside it would repeat this. Worth a
   single grep whenever a cache is added.
2. **Lock identity churn.** Because the memo is cleared wholesale, a later request for an evicted FEN reuses the
   *existing* lock object, so the single-flight still works — which is why this is a leak and not a correctness
   bug. Verified: after clearing the memo, a repeated request still made 0 extra calls (probe section 4).
3. **Not a security boundary bypass.** A `threading.Lock` per key is a correctness device; there is no way to
   make the guard *grant* access by exhausting the dict. Severity is low and this should not be over-escalated.

## Suggested fix shape (for the `api.py` owner)

* Bound `_explorer_stats_locks` the same way the memo is bounded, and prefer fixing both with one shared
  eviction helper so they cannot drift apart again.
* Cleaner still: make the lock map a `WeakValueDictionary`, or key a single striped lock array on
  `hash(fen) % N`. Striping is the smallest change — it makes the bound structural (N locks, always) rather than
  something a future edit can forget, and it is safe here because the critical sections are short (one outbound
  call) and do not acquire other locks.
* Fixed together with ABUSE-19, which is the same function and the same commit.
