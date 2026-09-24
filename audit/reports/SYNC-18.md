# SYNC-18 — a record with no `id` grows an identical `games` row **and** an identical `blunders` row on every sync; the new `ux_blunders_natural_key` index cannot stop it (`NULL != NULL`)
- **Severity:** high
- **Type:** data-loss
- **File:** backend/repertoire.py
- **Line(s):** 2292-2326 (`analyze_user_games`), 2396-2434 (`stream_analyze_user_games`); backend/analyzer.py:419-436 (`_upsert_blunder`); backend/db.py `ux_blunders_natural_key`
- **Component:** game/blunder ingestion
- **Repro script:** `audit/scripts/atk3_re01_schema_retest.py` (section `RE01-D`), `audit/scripts/atk3_re02_writer_retest.py` (sections `RE02-F`, `RE02-G`)
- **Detected by:** ATK-3 (RETEST)

## Expected
Every table that participates in idempotent ingestion has a natural key that actually dedups.
`ux_blunders_natural_key UNIQUE(game_id, ply, played_move)` (added by FIXER-N for SYNC-15) is
supposed to be that guarantee. A record with no `id` is *rejected* by `POST /api/sync`
(`api.py:953-957`, `failed_games`/`errors`), and the two "quick sync" writers
(`repertoire.py:1918`, `:2008`) skip it with `if not game_id: continue`.

## Actual
The two **analysis** writers have no such guard, and SQLite's UNIQUE index does not constrain
`NULL`s (they compare as distinct in every unique index), so the natural key provides *no*
protection for a `NULL` `game_id`:

```python
# backend/repertoire.py:2292-2326  (analyze_user_games)
for g in games:
    game_id = g.get("id")                     # <-- no `if not game_id: continue`
    cursor.execute("SELECT id, analyzed_at FROM games WHERE id = ?", (game_id,))
    existing = cursor.fetchone()              # <-- `id = NULL` matches NOTHING, always
    ...
    cursor.execute("""INSERT OR REPLACE INTO games (id, ...) VALUES (?, ...)""", (game_id, ...))
```
`games.id` is `TEXT PRIMARY KEY` — a SQLite quirk lets a non-`INTEGER` primary key hold `NULL`,
and two `NULL` rows do not conflict, so `INSERT OR REPLACE` inserts a **new** row every time.
The blunders produced for that game inherit `game_id = None`, and
`analyzer._upsert_blunder`'s `SELECT ... WHERE game_id = ? AND ply = ? AND played_move = ?`
also matches nothing, so every sync appends a fresh copy.

Observed (isolated temp DB, three consecutive syncs of one id-less record,
`audit/scripts/atk3_re02_writer_retest.py` RE02-G):

```
      NDJSON record has keys: ['createdAt', 'moves', 'opening', 'players', 'rated', 'speed', 'winner']
      after 3 analyze_user_games: games=3  blunders=3 (identical rows: 3)
   -> BROKEN  3x the same id-less game -> 1 game row, 1 blunder row   blunders=3
      /api/blunders shows: 0 of 3 rows (INNER JOIN games)  | /api/dashboard total_blunders: 3
```
And the same hole with no ingestion path at all — four identical `save_blunders_to_db` calls
(RE01-D):
```
   -> BROKEN  ux index dedups NULL game_id (it cannot: NULL != NULL in SQLite)
               identical rows after 4 saves = 4, index cols=[1, 3, 7, -1]
```

The damage is not just row count:
* `GET /api/blunders` shows **0 of 3** — it `INNER JOIN`s `games ON b.game_id = g.id`, which
  never matches `NULL`. The user has blunders they can never see or drill.
* `GET /api/dashboard` counts **3** (`COUNT(*)` on `blunders`, no join). Dashboard total ≠
  visible list.
* `get_game_gaps` reaches them too: `LEFT JOIN games` yields `NULL` for `played_at`/`speed`, so
  the row is emitted with `"speed": "rapid"` and `"played_at": 0`.
* Every sync makes it worse and nothing — including `init_db()`'s duplicate collapse — ever
  removes it, because the collapse keys on `(game_id, ply, played_move)` and `NULL` matches
  nothing there either.

## Root cause
Two independent gaps that happen to compose:
1. **Missing input validation** in `analyze_user_games` / `stream_analyze_user_games`:
   `POST /api/sync` validates `game_id`, the two quick-sync writers `continue`, these two do not.
2. **A UNIQUE index is not a constraint on `NULL`.** `ux_blunders_natural_key` fixes SYNC-15
   only for rows with a real `game_id`. Any code path that can produce `game_id IS NULL`
   re-opens the duplicate hole the index was added to close, and `analyzer.py`'s
   SELECT-then-INSERT upsert degrades to a plain INSERT there.

## Exploit / escalation
1. **Unbounded growth from a single malformed upstream line.** One record without `id` in a
   500-game NDJSON response adds one `games` row and one `blunders` row **per sync**, for as
   long as the app runs. `/api/games/recent` will eventually return the same empty-looking
   card over and over (every `NULL`-id row has `game_url = "https://lichess.org/None"`), and
   `total_games` climbs.
2. **Silent divergence between the dashboard and every list.** `total_blunders` counts them,
   `/api/blunders` and `/api/dashboard`'s blunder breakdown (which also joins) do not. There is
   no reconciliation anywhere.
3. **The `INSERT OR REPLACE` is what makes it *insert*.** The same shape with a *non*-NULL
   duplicate id is a genuine REPLACE, so the two cases look identical in the code and behave
   oppositely — a fixer who "adds an id guard" fixes the growth but not the null-index hole.
4. **Adjacent:** `analyze_game_openings` stores `"game_id": game_dict.get("id")`, so a caller
   that forgets the key poisons the table directly. `save_blunders_to_db` has no
   `NOT NULL`/presence check on `game_id` either.

## Fix direction
Add `if not game_id: continue` (counted and reported) to both analysis writers; make
`_upsert_blunder` reject a row whose `game_id` is `None` instead of inserting it; and add a
`CREATE UNIQUE INDEX` **plus** a real `NOT NULL` constraint on `blunders.game_id` /
`games.id` so the guarantee stops depending on every caller validating its input.
