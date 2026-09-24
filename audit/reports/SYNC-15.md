# SYNC-15 — `blunders` has no uniqueness constraint, so re-analysing a game duplicates its blunders
- **Severity:** medium
- **Type:** data-loss
- **File:** backend/analyzer.py
- **Line(s):** 236-268 (`save_blunders_to_db`), backend/db.py:118-141 (no unique index)
- **Component:** blunder persistence
- **Repro script:** `audit/scripts/atk3_dup_blunder_repro.py`
- **Detected by:** ATTACKER-3 (SYNC)

## Expected
A blunder is a property of `(game_id, ply, played_move)`. Re-running the analysis for the
same game either updates the existing rows or is a no-op; it never appends a second copy.

## Actual
`save_blunders_to_db` is a plain `INSERT` with no conflict clause, and `blunders` has **no
unique index at all** (`PRAGMA index_list(blunders)` → `[]`). The only thing preventing
duplicates is the caller's `if existing["analyzed_at"] > 0: continue` guard, i.e. a single
mutable flag on the parent row.

Observed (game present with `analyzed_at = 0` and one existing blunder, then two analysis
runs):

```
  before: blunders = 1 | drill rows = 0 | analyzed_at = 0
  analyze run 1: {'status': 'success', 'synced_games': 1, 'new_blunders': 1, 'already_analyzed': 0}
  analyze run 2: {'status': 'success', 'synced_games': 1, 'new_blunders': 0, 'already_analyzed': 1}
  after : blunders = 2 | drill rows = 1
  rows: [(8, 1, 300), (9, 1, 300)]
  unique index on blunders? []
```

The second run re-inserts the identical `(game_id='dup1', ply=1, played_move='d2d4',
cpl_loss=300)` row.

## Root cause
```python
# backend/analyzer.py:242-257
cursor.execute("""
INSERT INTO blunders ( game_id, move_number, ply, color, ... ) VALUES (?, ?, ...)
""", (...))
```
with no `ON CONFLICT`, and the schema declares no
`UNIQUE(game_id, ply, played_move)`. Idempotency is delegated entirely to `analyzed_at`.

## Exploit / escalation
1. **Reachable state #1 — legacy databases:** `games.analyzed_at` was added by a later
   migration with `DEFAULT 0`, so every row in a pre-migration database is `0` while its
   blunders already exist. The first `/api/sync` or `/api/repertoire/sync_games` after the
   upgrade therefore **doubles the entire blunder corpus**.
2. **Reachable state #2 — the quick-sync path:** `sync_recent_games` and
   `stream_sync_recent_games` insert games with
   `analyzed_at = COALESCE((SELECT analyzed_at FROM games WHERE id = ?), 0)`, i.e. `0` for
   every newly downloaded game. Any future code path that analyses a game without stamping
   `analyzed_at` re-introduces the duplication.
3. **Reachability was wider before this audit round's fixes:** the pre-fix `/api/sync`
   stamped `analyzed_at` *before* running the analysis and had no per-game error isolation,
   so a mid-batch exception left games marked analysed with no blunders *and* games with
   blunders but a stale `0` — the exact state above. That path has been fixed in
   `backend/api.py:341+`, but the underlying "one flag guards an append-only table"
   fragility remains.
4. **Downstream damage:** `/api/dashboard`'s `total_blunders` and
   `AVG(cpl_loss)` are inflated, `get_game_gaps` returns the same mistake twice (and
   `all_blunders` / `earlier_mistake` pick the duplicate), and the spaced-repetition
   scheduler gets one `drill_reviews` row per duplicate.
5. **Fix direction:** add `CREATE UNIQUE INDEX IF NOT EXISTS ux_blunders_game_ply
   ON blunders(game_id, ply, played_move)` and switch the insert to
   `INSERT ... ON CONFLICT(game_id, ply, played_move) DO UPDATE SET ...`, which also makes
   a lowered `blunder_threshold_cp` idempotent.
