# FIXER-N-01 — `backend/db.py`: `blunders.best_pv` (SYNC-01), the blunder natural key (SYNC-15), the missing index set (CODE-02-08/10), the every-boot cache rewrite (CODE-02-18)

**Owner:** FIXER-N (exclusive ownership of `backend/db.py`)
**Base:** the version FIXER-P0 landed (byte-identical copy kept at `audit/scripts/FIXER-N_01_prefix_db.py`)
**Files touched:** `backend/db.py`, `AGENTS.md` §5 (one row), `tests/test_schema_integrity.py` (new), `audit/scripts/FIXER-N_01_evidence.py` (new), `audit/scripts/FIXER-N_01_prefix_db.py` (new)
**Net diff vs FIXER-P0's version:** 607 → 1013 lines in `backend/db.py` (494 changed lines, overwhelmingly comments/docstrings and the new index catalogue)

> **FIXER-P0's non-destructive-migration guarantee still holds, and is now re-asserted by tests in my own file.**
> `init_db()` is still additive and idempotent, still never DELETEs / relabels / prunes a `repertoire_moves` row, and the orphan purge is still opt-in only behind `init_db(purge_orphans=True)` / `CHESS_PURGE_ORPHANS=1`. Proven by:
> * `tests/test_migration_safety.py` — **9 tests, OK** (unchanged, not edited).
> * `tests/test_schema_integrity.py::TestBootNeverDestroysTheRepertoire` (new, 3 tests) — seeds orphan-shaped repertoire moves + a study, boots three times, and asserts the rows are **byte-identical**, that no forbidden study is invented, and that the default boot never prints the destructive banner. It exists because my new indexes, the `best_pv` `ALTER`, the de-duplication and the cache migration all run in the **same** `BEGIN IMMEDIATE` transaction as FIXER-P0's migration.

---

## 1. SYNC-01 — CRITICAL — `blunders.best_pv` was never created

### Root cause
`backend/db.py`'s `CREATE TABLE blunders` had no `best_pv`, and no `ALTER TABLE … ADD COLUMN` migration existed for it either. `backend/analyzer.py` INSERTs that column unconditionally, so on any database produced by the current `init_db()` **every** blunder write raised `OperationalError: table blunders has no column named best_pv`, `POST /api/repertoire/sync_games` returned 500, and the blunder / drill / gap / dashboard feature was dead. The column existed only in the user's pre-migration database, which is why 153 blunders are readable there. The test suite was blind to it because `tests/fixtures.py` seeds `blunders` with its own INSERT that omits `best_pv` — and `tests/test_analyzer_correctness.py` even carried a *workaround* (`if "best_pv" not in present: ALTER TABLE blunders ADD COLUMN best_pv TEXT`) with the comment “`best_pv` is missing from `db.py`'s schema”. That workaround is now dead code (the `if` is always false).

### Change
1. **`backend/db.py:510-538` — `CREATE TABLE IF NOT EXISTS blunders`** now declares `best_pv TEXT` as its **last** column:
   ```sql
       opening_games INTEGER DEFAULT NULL,
       created_at INTEGER,
       best_pv TEXT,
       FOREIGN KEY(game_id) REFERENCES games(id)
   ```
   Declared last on purpose: `ALTER TABLE … ADD COLUMN` appends, so a fresh database and a migrated one then have the *same* `PRAGMA table_info` order (asserted by `test_legacy_column_order_matches_a_fresh_schema`).
2. **`backend/db.py:552-556` — the migration path**, for a database that already exists:
   ```python
   _add_column_if_missing(cursor, "blunders", "opening_games", "INTEGER DEFAULT NULL")
   _add_column_if_missing(cursor, "blunders", "best_pv", "TEXT")
   ```
   with the new helper at **`backend/db.py:197-219`**. It reads `PRAGMA table_info` first and only issues the `ALTER` when the column is genuinely absent, so it is idempotent *by construction* instead of by swallowing an exception. This also replaced the five pre-existing `try: ALTER … except Exception: pass` blocks (`blunders.opening_games` and the four `repertoire_moves` columns) — a real “database is locked” used to be swallowed there, letting the app boot half-migrated (CODE-02-18 escalation 3). A genuine failure now propagates and `init_db()` rolls the whole migration back, which is what AGENTS.md §9.9 (“all-or-nothing”) already demanded. **No behaviour change other than that: a duplicate-column name is still a no-op.**

### SYNC-01 proof — red on a fresh pre-fix DB, green after
`audit/scripts/FIXER-N_01_evidence.py` (re-runnable, idempotent) builds a genuinely fresh database with either the pre-fix or the fixed `db.py` and then attempts the analyzer's own INSERT shape.

```
$ .../FIXER-N_01_evidence.py before          # audit/scripts/FIXER-N_01_prefix_db.py
[1] FRESH init_db()
    blunders columns (23): [... 'strategic_idea', 'category', 'opening_games', 'created_at']
    'best_pv' present : False
    indexes on blunders: []
    analyzer-shaped INSERT with best_pv: OperationalError: table blunders has no column named best_pv
    duplicate INSERT attempt 1: ACCEPTED
    duplicate INSERT attempt 2: ACCEPTED

$ .../FIXER-N_01_evidence.py after           # backend/db.py
[1] FRESH init_db()
    blunders columns (24): [... 'category', 'opening_games', 'created_at', 'best_pv']
    'best_pv' present : True
    indexes on blunders: [0, 1, 2, 3, 4]
    analyzer-shaped INSERT with best_pv: OK
    duplicate INSERT attempt 1: refused (UNIQUE constraint failed: blunders.game_id, blunders.ply, blunders.played_move)
```

The same proof inside the regression suite (the test that would have caught SYNC-01):
`tests/test_schema_integrity.py::TestFreshSchemaIsWritable::test_fresh_init_db_creates_best_pv`.

---

## 2. SYNC-15 — no uniqueness on `blunders`, so re-analysis duplicated rows

### Which key is the natural key? (the question the brief asked me to settle)
Not `(game_id, ply, color)`: `color` is a function of the ply parity and the game's `user_color`, so it adds no identity and would leave duplicates that differ only in `color` admissible.

`(game_id, ply, played_move)` — the same triple as `backend/analyzer.py`'s `BLUNDER_NATURAL_KEY`, which its upsert (`SELECT … WHERE game_id=? AND ply=? AND played_move=?`) and its duplicate-collapse both key on. `played_move` belongs in the key for two concrete reasons:

* the writer is a SELECT-then-INSERT upsert on exactly that triple, so a UNIQUE index on the triple is what lets the two agree;
* `played_move` is only *nominally* dependent on `(game_id, ply)` — it depends on the game's current `moves_san`, and `games` is written with `INSERT OR REPLACE` (`repertoire.py:1619, 1711, 1864, 1972`). A re-import that changed a game's move at a fixed ply would, under a UNIQUE index on the pair alone, make the analyzer's `INSERT` raise `IntegrityError` and roll the whole batch back — a new failure mode introduced into a file I do not own. With the triple, that case simply becomes a new row, which the app already handles.

Measured on the real production database (read-only, `mode=ro&immutable=1`): **0** duplicate groups on `(game_id, ply)`, **0** on `(game_id, ply, played_move)`, 0 NULL `game_id`, 0 NULL `ply`. So the index can be created on the user's database without deleting anything at all.

### Change
**`backend/db.py:670-690`** (inside the boot body, one transaction):

```python
    if not _create_index_if_missing(cursor, "ux_blunders_natural_key", "blunders",
                                    "game_id, ply, played_move", unique=True):
        removed, merged = _collapse_duplicate_blunders(cursor)
        print("!" * 78)  … loud banner …
        if not _create_index_if_missing(cursor, "ux_blunders_natural_key", "blunders",
                                        "game_id, ply, played_move", unique=True):
            raise RuntimeError("could not create ux_blunders_natural_key even after "
                               "collapsing duplicates; the database was left untouched by init_db()")
```

i.e. the exact SQL is

```sql
CREATE UNIQUE INDEX IF NOT EXISTS ux_blunders_natural_key
    ON blunders (game_id, ply, played_move);
```

* The de-duplication is **only reachable when SQLite refused that statement**, which can only happen when duplicate rows exist. On a clean database the boot path executes **no `DELETE` at all** — so it is idempotent by construction, not by convention.
* **`_collapse_duplicate_blunders` (`backend/db.py:257-347`)** — deterministic, and it never touches a row whose natural key is unique:
  * survivor of a group = most drill practice (`COALESCE(attempts) DESC, COALESCE(successes) DESC, id ASC`) — the same rule `analyzer.py::_collapse_duplicate_rows` uses, so the two agree;
  * a discarded copy's `drill_reviews` row is re-pointed at the survivor, or **merged** into the survivor's row when there is one: `attempts` and `successes` are summed, the *earlier* `last_attempt_date` / `next_review_date` win (a card that is still owed practice stays owed), the larger `interval_days` is kept, and `status='mastered'` is never downgraded. Practice is not silently dropped.
  * the key comparison uses `IS` so a NULL component is matched NULL-safely and cannot slip past the UNIQUE index.
* If the index still cannot be created afterwards, `init_db()` **raises**, the surrounding `BEGIN IMMEDIATE` rolls back, and the database is left exactly as it was. Fail closed.

Other tables inserted with `INSERT OR REPLACE` were surveyed (`backend/repertoire.py:359, 585, 1619, 1711, 1864, 1972`, `backend/db.py` settings, `backend/analyzer.py`): `explorer_cache` and `settings` and `repertoire_moves` all have a UNIQUE/PK covering the whole key, and `games.id` is the primary key. `blunders` was the only table with **no** key at all. No other de-duplication was needed.

---

## 3. CODE-02-08 / CODE-02-10 — there was not a single `CREATE INDEX` in the schema

### Root cause
`grep -rn "CREATE INDEX" --include=*.py .` returned zero hits. Every non-primary-key predicate was a scan; `study_moves`' only index is the PRIMARY KEY, which leads with the *unconstrained* `study_id`, so the planner had to scan `study_moves` for every row of `repertoire_moves`.

### Change
**`backend/db.py:84-118`** — one catalogue, applied at `backend/db.py:663-666` with `CREATE INDEX IF NOT EXISTS` (`SCHEMA_INDEXES`) plus one partial index (`SCHEMA_PARTIAL_INDEXES`):

| index | table | columns | predicate it serves |
| :--- | :--- | :--- | :--- |
| `ix_study_moves_move` | `study_moves` | `repertoire, parent_fen, move_uci` | every study-aware read + the boot migration's own join (CODE-02-08) |
| `ix_blunders_game_id` | `blunders` | `game_id` | the declared-but-unenforced FK every blunder read joins through |
| `ix_blunders_cpl_loss` | `blunders` | `cpl_loss DESC, id DESC` | “BIGGEST BLUNDER” ordering |
| `ix_blunders_opening_name` | `blunders` | `opening_name, color, cpl_loss DESC, id DESC` | dashboard group-by + the group's worst row (SYNC-09) |
| `ix_blunders_created_at` | `blunders` | `created_at DESC` | dashboard “recent blunders” |
| `ix_drill_reviews_due` | `drill_reviews` | `next_review_date` | trainer due filter |
| `ix_drill_reviews_status` | `drill_reviews` | `status, next_review_date` | `status='mastered'` + due |
| `ix_explorer_cache_updated_at` | `explorer_cache` | `updated_at` | 7-day TTL sweep |
| `ix_scouted_content_scouted_at` | `scouted_content` | `scouted_at DESC` | “recent scouted content” |
| `ix_games_user_color` | `games` | `user_color, played_at` | the games list filter |
| `ix_explorer_cache_unconverted` | `explorer_cache` | `fen, ratings` **(partial)** | the eval-convention probe (§4) |
| `ux_blunders_natural_key` | `blunders` | `game_id, ply, played_move` **(unique)** | §2 |

**Deliberately not added:** `blunders.eco`. `api.py:1359` filters it with `AND b.eco LIKE 'B90%'`, and SQLite only turns a `LIKE` into an index seek when the indexed column has a `NOCASE` collation, so an index there would be pure write cost. I also dropped a candidate `repertoire_moves(fen)` index after measuring that no query filters on `fen` alone.

### `EXPLAIN QUERY PLAN` — before / after

All plans below are measured on a **byte-faithful copy of the real production database** (`sha256=f0e5ce993e5a74a6a25084cc09304e51`, 2 457 600 bytes, copied with a plain `open(…,'rb')` — no SQLite connection is ever opened against production).

**The `study_moves` join (the one the brief asked for) — the app's hottest study query, `repertoire.py:2151`:**
```
BEFORE                                        AFTER
SEARCH rm USING INDEX                          SEARCH rm USING INDEX
  sqlite_autoindex_repertoire_moves_1            sqlite_autoindex_repertoire_moves_1
  (repertoire=?)                                 (repertoire=?)
SCAN sm LEFT-JOIN                              SEARCH sm USING INDEX ix_study_moves_move
                                                (repertoire=? AND parent_fen=? AND move_uci=?) LEFT-JOIN
```
`SCAN sm LEFT-JOIN` is a full scan of `study_moves` **per row of `repertoire_moves`** — the O(N×M) of CODE-02-08.

**The boot migration's own `study_name` backfill probe:**
```
BEFORE  SEARCH sm EXISTS USING AUTOMATIC PARTIAL COVERING INDEX (repertoire=? AND parent_fen=? AND move_uci=?)
AFTER   SEARCH sm EXISTS USING COVERING INDEX ix_study_moves_move (repertoire=? AND parent_fen=? AND move_uci=?)
```
`AUTOMATIC` means SQLite built a throwaway index *for that one statement, every boot*.

**Study membership for a whole colour (`repertoire.py:2297`) and for one position (`repertoire.py:879`):**
```
BEFORE  SCAN sm  /  SEARCH rs USING INTEGER PRIMARY KEY (rowid=?)
AFTER   SEARCH sm USING INDEX ix_study_moves_move (repertoire=?)  /  SEARCH rs …
        SEARCH sm USING INDEX ix_study_moves_move (repertoire=? AND parent_fen=? AND move_uci=?)
```

**Analyzer upsert + duplicate detection:**
```
BEFORE  SCAN blunders                                          AFTER  SEARCH blunders USING COVERING INDEX ux_blunders_natural_key (game_id=? AND ply=? AND played_move=?)
BEFORE  SCAN blunders + USE TEMP B-TREE FOR GROUP BY          AFTER  SCAN blunders USING COVERING INDEX ux_blunders_natural_key
```

**The blunder/games join and the three list queries:**
```
BEFORE  SEARCH g … / SCAN b                                   AFTER  SEARCH g … / SEARCH b USING COVERING INDEX ix_blunders_game_id (game_id=?)
BEFORE  SCAN blunders + USE TEMP B-TREE FOR ORDER BY          AFTER  SCAN blunders USING COVERING INDEX ix_blunders_cpl_loss
BEFORE  SCAN blunders + USE TEMP B-TREE FOR GROUP BY          AFTER  SCAN blunders USING COVERING INDEX ix_blunders_opening_name
BEFORE  SCAN b + USE TEMP B-TREE FOR ORDER BY                 AFTER  SCAN b USING INDEX ix_blunders_created_at
BEFORE  SCAN explorer_cache                                   AFTER  SEARCH explorer_cache USING INDEX ix_explorer_cache_updated_at (updated_at<?)
BEFORE  SCAN scouted_content + TEMP B-TREE                    AFTER  SCAN scouted_content USING COVERING INDEX ix_scouted_content_scouted_at
BEFORE  SCAN games + TEMP B-TREE                              AFTER  SEARCH games USING INDEX ix_games_user_color (user_color=?)
BEFORE  SCAN drill_reviews                                    AFTER  SEARCH drill_reviews USING COVERING INDEX ix_drill_reviews_due (next_review_date<?)
```
Every `USE TEMP B-TREE FOR ORDER BY / GROUP BY` on these paths is gone.

**The repertoire tree walk was already a `SEARCH`** — `sqlite_autoindex_repertoire_moves_1 (repertoire=? AND parent_fen=?)` comes from the table's own `UNIQUE(repertoire, parent_fen, move_uci)` and is unchanged by this patch. I am reporting that honestly rather than claiming a win: the quadratic cost CODE-02-08 measured lived in the **join**, not in the walk. `test_repertoire_tree_walk_is_a_search` now pins it so it cannot silently regress.

---

## 4. CODE-02-18 — the full-table `LIKE '%…%'` rewrite migration on every boot

### Root cause
`init_db()` unconditionally selected every cached row whose payload did not *contain the text* `"eval_convention": "white"`, parsed it, negated the moves of black-to-move positions, and wrote it back — on every launch, twice per launch (`start.py` and the FastAPI `startup` event), with both `except` clauses `pass`ing.

### Change
**`backend/db.py:20-70` (constants) + `349-465` (probe + migration) + `222-246` (index helper):**

* **A real version marker.** `PRAGMA user_version`, `SCHEMA_VERSION = 1`, `EXPLORER_EVAL_CONVENTION_VERSION = 1`, stamped by `_stamp_schema_version` (monotonic; it never moves backwards — pinned by `test_user_version_never_moves_backwards`). Deliberately **not** a tenth `settings` row (a migration marker is not app configuration and `GET /api/settings` hands every key to the frontend) and deliberately **not** a tenth table (AGENTS.md §5 says “There are 9 tables” and `test_docs_contract` re-derives that from the live schema). A header integer is no row, no column, no data.
* **The per-boot scan is gone, replaced by an index-backed probe.** The selection predicate moved from a textual substring test to the *value* of the JSON key:
  ```python
  UNCONVERTED_EXPLORER_PREDICATE = (
      "NOT (json_valid(data_json) AND "
      "COALESCE(json_extract(data_json, '$.eval_convention'), '') = 'white')"
  )
  # CREATE INDEX IF NOT EXISTS ix_explorer_cache_unconverted
  #     ON explorer_cache (fen, ratings) WHERE <that predicate>
  ```
  SQLite only uses a partial index when the query's `WHERE` matches the index's, so the predicate string is shared verbatim between `SCHEMA_PARTIAL_INDEXES` and the migration's own `SELECT`. The plan is now
  `SCAN explorer_cache USING INDEX ix_explorer_cache_unconverted` — an index whose size is the number of rows that actually need work, not the size of the cache. Measured on a 5000-row cache: **0.0014 ms vs 0.48 ms per probe (≈340×)**, and the worst case (a cache that is entirely unconverted) is the same order as the pre-fix scan.
  Two safety valves: a SQLite build without JSON1 logs a warning and falls back to the old `LIKE` predicate, then to a plain scan — a missing optional extension can never stop the app from booting.
* **The intended effect on `eval_convention` is unchanged** (same JSON parse, same negation for `parts[1] == 'b'`, same `UPDATE … WHERE fen=? AND ratings=?`, same `d["eval_convention"] = "white"`).
* **It is transactional** — it runs inside the `BEGIN IMMEDIATE` `init_db()` already owns, so a crash cannot half-convert the cache.
* **Per-row failures are loud.** A row that cannot be parsed is now named in a `!!`-banner (FEN, ratings, exception) and the version is left un-stamped so the retry stays loud. `json_valid` is inside the index predicate specifically so a payload that is not valid JSON **cannot be quietly dropped** — the pre-fix code `pass`ed it forever, with no log, on every boot. My own test caught this: the first version of the predicate (`json_valid(...) AND …`) made corrupt rows invisible, and `test_a_corrupt_payload_is_reported_loudly_and_left_alone` failed until the predicate was widened to `NOT (…)`.
* **The stamp does not strand a late arrival.** I deliberately did **not** make the version gate skip the probe outright: a row that turns up unconverted *after* the stamp (legacy import, hand edit, test fixture) is still repaired on the next boot. Every payload the app itself writes already carries `eval_convention: "white"` (`repertoire.py:353`) and the read path re-normalises anything else (`repertoire.py:254`), so the user-visible contract is unaffected either way — but a version-only gate would have silently abandoned such a row forever.
* **A real latent bug fell out of this.** The old gate was *textual*, and the rewrite negates unconditionally, so any already-converted payload the substring failed to match (e.g. written with `separators=(',', ':')`) had its White-perspective scores flipped a second time on the next boot, and again on the boot after that. Pinned by `test_a_compact_json_payload_counts_as_converted` (red pre-fix, green after).

---

## 5. Regression tests — `tests/test_schema_integrity.py` (new, 25 tests)

| test | what it pins |
| :--- | :--- |
| `test_fresh_init_db_creates_best_pv` | **the SYNC-01 catcher**: fresh schema has `best_pv` and the analyzer's verbatim column list inserts |
| `test_fresh_schema_has_every_column_the_analyzer_writes` | all 23 analyzer columns exist |
| `test_fresh_schema_declares_the_blunder_natural_key_as_unique` | `ux_blunders_natural_key` is UNIQUE on `(game_id, ply, played_move)` |
| `test_fresh_schema_refuses_a_duplicate_blunder` | re-analysis cannot append a second copy |
| `test_fresh_schema_creates_every_declared_index` | no index silently dropped from the catalogue |
| `test_fresh_schema_creates_exactly_the_nine_documented_tables` | the version marker never became a tenth table |
| `test_legacy_database_gains_best_pv_without_losing_a_row` | **legacy `ALTER` path**, built from the verbatim pre-fix DDL, rows + `drill_reviews` byte-identical |
| `test_legacy_column_order_matches_a_fresh_schema` | fresh ≡ migrated |
| `test_legacy_duplicates_are_collapsed_and_practice_is_kept` | **de-dup**: 3 rows → 1, attempts 7+2=9, successes 3+1=4, `mastered` never downgraded, loud banner printed |
| `test_collapse_removes_nothing_but_duplicates_and_is_idempotent` | 3 distinct blunders untouched, only the duplicate removed, survivor = most-practised copy, second boot a no-op |
| `test_a_production_copy_is_preserved_row_for_row` | a **byte copy of `chess_improvement.db`** survives `init_db()` with every row and no orphaned drill row (skips when the file is absent) |
| `test_second_and_third_boot_change_nothing` | **idempotence**: row digest + `sqlite_master` digest + `user_version` all stable across two further boots |
| `test_user_version_never_moves_backwards` | the marker is monotonic |
| `test_side_to_move_payload_is_negated_and_stamped` | intended effect of the cache migration unchanged |
| `test_a_row_that_turns_up_later_is_still_repaired` | the stamp does not strand a late arrival |
| `test_a_compact_json_payload_counts_as_converted` | no second negation (a real latent bug) |
| `test_a_corrupt_payload_is_reported_loudly_and_left_alone` | per-row failure is loud, row left byte-identical |
| `test_the_probe_is_answered_by_the_partial_index` | the `LIKE` full scan is gone |
| `test_orphan_shaped_moves_survive_every_boot` ×3 | **FIXER-P0's guarantee**, re-asserted under the new migrations |
| `test_study_moves_join_uses_an_index_not_a_scan` | **CODE-02-08** |
| `test_study_name_backfill_probe_uses_the_same_index` | no `AUTOMATIC` per-query index at boot |
| `test_blunder_lookups_use_indexes` / `test_repertoire_tree_walk_is_a_search` | the remaining measured plans |

### Red before / green after
```
$ cp audit/scripts/FIXER-N_01_prefix_db.py backend/db.py
$ .../python3 -m unittest tests.test_schema_integrity
Ran 25 tests ... FAILED (failures=16, errors=1)      # 17 of 25 red against FIXER-P0's db.py
$ cp /tmp/opencode/fixer_n_fixed_db.py backend/db.py
$ .../python3 -m unittest tests.test_schema_integrity
Ran 25 tests ... OK
```
The 17 red ones cover: `best_pv` on a fresh DB, the legacy `ALTER`, the natural-key index, duplicate refusal, the index catalogue, the `study_moves` join plan, the backfill probe plan, the blunder plans, the de-duplication (errors because `DROP INDEX ux_blunders_natural_key` does not exist pre-fix), the compact-payload double negation, the loud-failure banner, the partial-index probe, and `user_version` stamping.

The 8 that are green in **both** are deliberate invariants, and I am not going to claim otherwise: the production-copy preservation test, the three FIXER-P0 repertoire-preservation tests, the repertoire tree-walk plan, the 9-tables test, and the late-arrival repair (the pre-fix code repaired on every boot, so it also passes). An idempotence or data-preservation test that was red before the fix would be a false claim about the pre-fix code.

---

## 6. Full-suite result

```
$ /home/robin/miniconda3/bin/python3 -m unittest discover tests
Ran 507 tests in 28.3s
FAILED (failures=1, errors=1)

ERROR: test_preexisting_duplicate_rows_are_collapsed (test_analyzer_correctness.TestIdempotentWritePath)
FAIL:  test_all_security_checks (test_api_security.TestApiSecurity)

$ ... -m unittest tests.test_schema_integrity    -> Ran 25 tests ... OK   (my module, isolated)
$ ... -m unittest tests.test_migration_safety    -> Ran  9 tests ... OK   (FIXER-P0's file, untouched)
$ ... -m unittest tests.test_docs_contract       -> Ran 18 tests ... OK   (incl. my AGENTS.md §5 edit)
$ ... -m unittest tests.test_isolation_guard     -> Ran 17 tests ... OK
```

Down from 12 reds at the start of the wave (the scout / frontend / docs modules were being
fixed in parallel by their owners). **Exactly two reds are attributable to this change**,
and both are fixtures in files I do not own that construct a state the schema now forbids
— §6a below. My new files are staged (`git add tests/test_schema_integrity.py
audit/scripts/FIXER-N_01_*.py`), which is what `test_isolation_guard` requires of a new
test module.

### The two failures my change causes — both are fixtures that construct a state the schema now forbids

I did **not** edit either file (`tests/**` belongs to FIXER-D; the rule was explicit), and I did **not** weaken any guarantee to make them pass. Both need a one-line fixture change, and neither change weakens what the test asserts:

**(a) `tests/test_api_security.py:667-676`, `check_dashboard_eco_is_deterministic` (SYNC-09)**
```python
        for eco, loss in (("B20", 100), ("B90", 900)):
            cursor.execute("""INSERT INTO blunders (game_id, move_number, ply, ...) VALUES (?,?,?,...)""",
                (game_id, 1, 1, "white", START_FEN, START_FEN, "d2d4", "d4", "g1f3", "Nf3",
                 0, -900, loss, group_name, eco, "[]", 100))
```
Both rows carry the **identical** natural key `(game_id, 1, 'd2d4')` — two different blunders for one move of one game, which is not a state the domain can produce. The second INSERT now raises `UNIQUE constraint failed: blunders.game_id, blunders.ply, blunders.played_move`.
**Suggested fix (preserves the test exactly):** give the two rows distinct plies, e.g. iterate `for ply, eco, loss in ((1, "B20", 100), (3, "B90", 900))` and pass `ply` in the `move_number, ply` slots. The assertions it protects — one dashboard group, `blunder_count == 2`, and the reported ECO being the **worst** blunder's (`B90`) — are unaffected.

**(b) `tests/test_analyzer_correctness.py:553-580`, `test_preexisting_duplicate_rows_are_collapsed`**
It deliberately inserts two rows with an identical natural key and then asserts that `save_blunders_to_db()` collapses them. With the UNIQUE index that state cannot be *created* any more, which is the point of SYNC-15. The equivalent guarantee is now covered at the schema level by `tests/test_schema_integrity.py::TestLegacyDatabaseMigration::test_legacy_duplicates_are_collapsed_and_practice_is_kept` (drop the index, seed the duplicates, boot, assert the collapse and the preserved practice).
**Suggested fix:** have the test seed its duplicates on a database that has not been migrated yet (as my test does), and then assert on the **`init_db()`** collapse instead of the writer's, or keep the writer assertion but drop the index first.

Both are, deliberately, the *only* two reds attributable to me out of 507 tests.

---

## 7. Verification that the user's data is untouched

`audit/scripts/FIXER-N_01_evidence.py after`, section [2]/[5] — on a byte copy of the real
9-table production database:

```
[2] COPY OF PRODUCTION  sha256=f0e5ce993e5a74a6a25084cc09304e51  bytes=2457600
    row counts BEFORE init_db(): blunders 153, drill_reviews 153, explorer_cache 444,
                                    games 307, repertoire_moves 0, repertoire_studies 0,
                                    scouted_content 31, settings 5, study_moves 0
    row counts AFTER  init_db(): (identical)
    counts changed? NO
    content digest before=3d19a1fd6f24e5ea7604cc90b24733ce
    content digest after =3d19a1fd6f24e5ea7604cc90b24733ce
    content identical? True
[4] idempotence: after a SECOND init_db(): counts identical? True
    PRAGMA user_version after the boots: 1
[4b] self-healing: a row written dirty AFTER the version was stamped
     -> convention='white' eval=-30
[5] production fingerprint unchanged by this script? True
```

Production is never opened by SQLite: the only access is `tests/probe_guard.fingerprint()`
(`os.stat` + `open(…,'rb')`) and the same raw byte read used to make the copy, whose
SHA-256 is printed so the copy is provably faithful. No probe touched port 38281.

---

## ⚠️ Operational note — a concurrent prefix swap reverted `backend/db.py` mid-report

At **21:08:10** — 13 seconds after this report was first written — `backend/db.py` was
overwritten with a byte-identical copy of `audit/scripts/FIXER-N_01_prefix_db.py`
(FIXER-P0's pre-FIXER-N version, md5 `7eb491a0…`), i.e. the **unfixed** schema was back
in the repo. No script in `audit/scripts/` or `tests/` writes that path
(`FIXER-P0_prefix_proof.py` only writes into a `/tmp` scratch tree), so this was a live
agent session performing the same “swap the pre-fix file in, prove the regression test is
red, swap back” dance the orchestrator asked me to do — the identical incident
`audit/STATE.md` already recorded against FIXER-P0.

I restored it and re-verified (`md5 6acaad41…`, 1013 lines, 34/34 in
`test_schema_integrity` + `test_migration_safety`). **The fixed file is also committed at
`audit/scripts/FIXER-N_01_fixed_db.py`** so a swap can always be undone without a /tmp
snapshot:

```bash
md5sum backend/db.py audit/scripts/FIXER-N_01_fixed_db.py   # must match (6acaad41…)
cp audit/scripts/FIXER-N_01_fixed_db.py backend/db.py       # recovery
```

If you find `backend/db.py` at 607 lines with `CREATE TABLE IF NOT EXISTS blunders` at
line 118, the fix is **not** in the repo. `tests/test_schema_integrity.py` catches that in
under a second: 17 of its 25 tests go red.

## Also noticed (not fixed)

1. **`blunders.eval_before_mate` / `eval_after_mate` do not exist either** — the same class of
   gap as SYNC-01, and `backend/analyzer.py` writes them *opportunistically*
   (`_schema_mate_columns` skips them when absent, with the comment “hand-off to FIXER-P0:
   `ALTER TABLE blunders ADD COLUMN eval_before_mate INTEGER DEFAULT NULL`”). So SYNC-06's
   mate-distance detection is persisted for **no** game: the live table has 24 columns and
   neither mate column, and neither does a fresh `init_db()`. Two `INTEGER DEFAULT NULL`
   columns would fix it, but it is outside the four findings I was given, it widens the
   documented `blunders` shape that `test_docs_contract.py` re-derives from production, and
   FIXER-D owns that doc. **Recommendation: a 2-line follow-up, plus 2 new §5 rows.**
   (Note `tests/test_analyzer_correctness.py` is already structured for it — see the
   `best_pv` workaround in §1.)
2. **The pre-fix `try/except Exception: pass` idiom survives in three places I do not own**:
   `_init_db_body`'s `migrate_studies_and_cleanup_blunders` wrapper, the per-row loop inside
   `migrate_studies_and_cleanup_blunders`, and `find_unreachable_repertoire_moves`'s callers.
   FIXER-P0 deliberately made step 3 read-only, which is why they are harmless today, but
   step 1's per-row `print(f"Error restoring missing study move …")` and step 2's
   `Warning in migrate_studies_and_cleanup_blunders: …` still mean a failed study migration
   is survivable-but-silent. Out of scope; flagging for whoever owns the policy.
3. **The two fixtures in §6 need FIXER-D's hand.** Nothing else in the tree depends on the
   absence of the UNIQUE index.
4. **`explorer_cache` is the table that will grow without bound** (one row per visited
   position per `ratings` string, never pruned). Nothing in `db.py` expires it. Out of scope.

## Residual risks

* **A UNIQUE index is a behavioural change for any writer that does not use `ON CONFLICT`.**
  `backend/analyzer.py` (FIXER-K's) already upserts on the natural key, so it is safe. A
  future writer that inserts blunders with a plain `INSERT` and no conflict clause will now
  raise `IntegrityError` on a re-analysis instead of silently duplicating — the correct
  failure, and the reason the finding's own fix direction also asked to switch the insert to
  `ON CONFLICT … DO UPDATE`.
* **`_collapse_duplicate_blunders` merges spaced-repetition rows.** It only runs on a
  database that already contains duplicates of a natural key, and it never downgrades
  `mastered`. It is nonetheless a `DELETE` on `blunders`/`drill_reviews` inside the boot
  path, which is why it is loud, unreachable on a clean database, and asserted by
  `test_legacy_duplicates_are_collapsed_and_practice_is_kept`.
* **`json_valid`/`json_extract` need SQLite ≥ 3.9 with JSON1** (3.53.2 here; JSON1 is
  built in by default since 3.38). Without it the app still boots and still migrates, via a
  logged fallback to the old `LIKE` predicate — i.e. the pre-fix performance, not a
  regression. `test_the_probe_is_answered_by_the_partial_index` would fail on such a build,
  which is the intended signal.
* **Index write amplification.** Eleven new indexes across the append-heavy tables
  (`explorer_cache`, `blunders`). On this user's data (444 + 153 rows) the cost is
  unmeasurable; at 10⁵ rows it is a few percent of insert time, which is the trade the
  finding asked for.
* **`PRAGMA user_version` is a shared, global header field.** Nothing else in the repository
  uses it, and `_stamp_schema_version` only moves it forward, but a future tool that also
  stamps it would need to share the numbering (documented on `SCHEMA_VERSION`).
