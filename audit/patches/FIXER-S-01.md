# FIXER-S-01 — blunder identity (SYNC-18), re-analysis convergence (SYNC-19), a fail-closed `normalize_score` (SYNC-23)

**Owner:** FIXER-S · **Files owned and touched:** `backend/analyzer.py`, `backend/db.py`.
**New:** `tests/test_blunder_identity.py` (30 tests), `audit/scripts/FIXER-S-01_before_runner.py`,
`audit/scripts/FIXER-S-01_evidence.py`, `audit/scripts/FIXER-S-01_prefix_analyzer.py`,
`audit/scripts/FIXER-S-01_prefix_db.py`.
**Also touched (assertions preserved, fixtures only):** `tests/test_analyzer_correctness.py` (one
fixture), `tests/test_api_security.py` (one fixture), `AGENTS.md` §2/§3/§5.
**Not touched:** `backend/api.py`, `backend/repertoire.py`, `backend/evaluator.py`, any existing
test's *assertions*, the production database.

> **Baseline for this task:** `Ran 507 … FAILED (failures=1, errors=1)` — the two reds the brief
> named. **Result:** `Ran 575 … FAILED (failures=3, errors=2)`, and an A/B against a tree with my
> two files reverted gives **30 → 5** with an **empty** "introduced by this patch" set (§7). Every
> one of my 22 module reds is gone; the 5 survivors are byte-identical pre-fix and post-fix and
> live in files I do not own (§8).

---

## 1. Root cause per finding

### SYNC-18 (HIGH) — a UNIQUE index is not a constraint on `NULL`, and a row with no identity has no meaning

Two independent holes that compose:

**(a) `blunders`.** `analyze_game_openings` took `game_id = game_dict.get("id")` and put it straight
into the row. With `id` absent, every write produced `game_id = NULL`, and
`ux_blunders_natural_key UNIQUE(game_id, ply, played_move)` cannot dedup that: **SQLite compares
NULLs as distinct inside a UNIQUE index**. `_upsert_blunder`'s `SELECT … WHERE game_id = ?` also
matched nothing, so the upsert degraded to a plain `INSERT`. Measured pre-fix (`before` run,
`TestGamesIdentityIsConstrainedByTheSchema`): **3 identical `INSERT OR REPLACE`s of an id-less game
→ 3 rows.**

Such a row is also *worthless*: every read JOINs `blunders` to `games`
(`api.py:1350` `/api/blunders`, `api.py:1533` `/api/blunders/{id}`), so it is shown by no list,
while `/api/dashboard`'s bare `COUNT(*) FROM blunders` counts it. That is ATK-3's measurement:
**0 of 3 visible, 3 on the dashboard.**

**(b) `games`.** `games.id` is `TEXT PRIMARY KEY`, and a SQLite quirk lets a *non-INTEGER* primary
key hold `NULL`; two `NULL` rows do not conflict, so `INSERT OR REPLACE INTO games (id, …) VALUES
(NULL, …)` — the exact statement `repertoire.analyze_user_games` issues for an id-less record —
**inserted** rather than replaced, once per sync, forever.

### SYNC-19 (HIGH) — the SYNC-15 upsert made re-analysis *idempotent* but not *correct*

`save_blunders_to_db` upserted on the natural key and collapsed duplicates, but there was **no
statement anywhere that removed a row for a game the batch covered**. Its own docstring said so:

```python
for game_id in sorted({b.get("game_id") for b in blunders}, …):
    summary["duplicates_removed"] += self._collapse_duplicate_rows(cursor, game_id)
# NOTE: no `DELETE FROM blunders WHERE game_id = ? AND … NOT IN (<batch>)`
```

So lowering `max_opening_ply`, raising `blunder_threshold_cp`, or a shallower engine verdict all
froze the old findings in place, and `games.analyzed_at > 0` guaranteed they would never be
recomputed. `_collapse_duplicate_rows` gave false coverage: it only collapses rows that *duplicate
an existing natural key* — exactly the duplicates the schema now forbids — so in the fixed schema
it is dead code and it does nothing about a surplus of **distinct** rows.

Pre-fix (`before` run): a game analysed with a mistake, then re-analysed with a clean engine, still
had `1` stored row.

### SYNC-23 (LOW) — `hasattr` duck-typing, plus a fallback written for one class and applied to all

```python
# backend/analyzer.py:110-118 (pre-fix)
if color is not None and hasattr(score, "pov"):
    score = score.pov(color)
mate = score.mate() if hasattr(score, "mate") else None
if mate is None:
    raw = score.score() if hasattr(score, "score") else None
    if raw is None:
        return MATE_SCORE_CP, 0      # <- "MateGiven" fallback
```

Measured against the installed python-chess **1.11.2**:

| class | `mate()` | `score()` | `pov()` |
| :--- | :--- | :--- | :--- |
| `Cp` | ✓ | ✓ | ✗ |
| `Mate` | ✓ | ✓ | ✗ |
| `MateGiven` (a *singleton*) | ✓ (`0`) | ✓ (`None`) | ✗ |
| `PovScore` | **✗** | **✗** | ✓ |

`MateGiven.mate()` returns `0`, so the `raw is None` branch is **never reached by `MateGiven` at
all** — it is reached *only* by a `PovScore` with `color=None`. The branch written for one class
was in fact the branch that misfires on the one class it never sees. A plain `PovScore(Cp(+30))`
became `(+10000, mate=0)` and `describe_score_mate(0)` rendered it as
**"checkmate is already on the board"**.

---

## 2. My identity rule (SYNC-18) — and the alternatives I rejected

> **Rule: a `blunders` row is storable only if all three of `game_id`, `ply` and `played_move` are
> present and usable. `ply` and `played_move` are *normalised*; `game_id` is *required*. A finding
> with no game identity is REFUSED, loudly, before any transaction opens.**

**Why refuse rather than synthesise a sentinel `game_id`.** A fabricated id (`''`, `'unknown'`,
`'anonymous'`) makes the row *worse*, not better: it still points at no real game, so it is still
invisible to every reader, but it now **occupies the natural key** `('' , ply, move)` and would
make a genuine future game that somehow received that id collide with it. A refusal loses one
analysis of a record that has no identity — which the user could never have seen or drilled
anyway — and it is loud. Rejecting the "normalise everything to a sentinel" option is also what
the brief asked me to weigh explicitly: for `game_id` there is nothing honest to normalise *to*.

**Why `ply` / `played_move` are derived rather than refused.** They *are* derivable:
`analyze_game_openings` computes `ply` from the loop index and `played_move` from
`move.uci()` while the board is still in front of it. So a caller that passes `" 4 "` or
`" D2D4 "` gets a row that lands on the same natural key as `"4"` / `"d2d4"` — the upsert's lookup
and the value stored are then guaranteed to be the same bytes, whatever the caller did.
`natural_key_of` also normalises what it stores, so `ux_blunders_natural_key` and the Python-side
comparison cannot drift apart.

**Why the *schema* also carries the guarantee, for `games` and not for `blunders`.** The brief
asked for a real `NOT NULL` on both. I did **not** add one: SQLite cannot add a `NOT NULL`
constraint to an existing table without a full table rebuild (`CREATE TABLE … AS SELECT … ,
DROP , RENAME`), which on the boot path is a rewrite of the user's game history — a destructive
migration of exactly the class `init_db()` exists to avoid. Instead:
* `blunders` — the writer is *the only* writer of that table in this repository
  (`grep` confirms: `analyzer.py` only), so the invariant lives in the writer, is enforced before
  the transaction, and raises. A `NOT NULL` constraint would additionally have required
  `_collapse_duplicate_blunders` and the fixtures to be reworked, and would still not have stopped
  `repertoire.analyze_user_games` from writing a `NULL`-id `games` row.
* `games` — has **four** writers across two files I do not own (`api.py:994`, `repertoire.py`
  ×4), and only one of them validates `game_id`. So the guarantee **must** be in the schema:
  ```sql
  CREATE UNIQUE INDEX IF NOT EXISTS ux_games_id_not_null
      ON games (COALESCE(id, ''));
  ```
  This folds every id-less record into one bucket, so the next `INSERT OR REPLACE` with a `NULL`
  id *conflicts* and converges on a single row — which is precisely "make both upserts converge",
  achieved without touching one line of a file another agent is editing right now. A real Lichess
  id is eight base62 characters and can never be `''`, so no legitimate game is folded in.

**Why `_create_games_id_index` refuses to delete anything.** It is the first thing in `init_db()`
that would need a `DELETE` on `games` to succeed, and it is the wrong trade. See §4.

---

## 3. My staleness policy (SYNC-19) — and the alternatives I rejected

> **Policy: a re-analysis makes the stored blunder set of a game in scope equal the engine's
> current findings. A stale finding is deleted. If the user had actually *practised* it, its drill
> card is RETIRED, not deleted — `status = 'retired'`, `next_review_date = 9223372036854775807`
> ("never due").**

**Scope.** `WHERE b.game_id = ?` — one game, one parameter. A re-analysis can never touch another
game's rows (pinned by `test_reconciliation_never_touches_another_game`). The "keep" set is
compared in Python, never with `NOT IN (…)` — whose `NULL` semantics the brief flagged as a
footgun and which I had just spent SYNC-18 fixing. The reconcile runs inside the **same single
transaction** as the upserts, so a mid-batch failure rolls back the removals too
(`test_a_failed_reconciliation_rolls_the_whole_batch_back`).

**Rejected alternative A — delete the card too, unconditionally.** This is the simplest reading and
it *is* what happens for an untouched card: the `drill_reviews` row is the all-zero `new`
placeholder the writer creates for every finding, holds nothing of the user's, and keeping it would
only inflate `mastered_count`. Deleted. ✓

**Rejected alternative B — keep the card but leave it countable.** Keeping the row but deleting the
blunder leaves an orphan `drill_reviews` row, and `api.py:1241/1244/1611` count
`drill_reviews` **bare** (`status='mastered'`, `next_review_date <= ?`). The phantom would still be
counted — the exact complaint of the finding, unresolved. ✗

**Rejected alternative C — keep the blunder row, mark it stale with a new column.** This is what
the finding's "prefer soft deletion" suggests, and it is the one I argued against hardest, because
**it does not fix the finding.** Every gap-list and dashboard query in `repertoire.py` / `api.py`
would need a new `AND is_stale = 0`, and those are files I do not own; shipping a column that
nothing filters on means the phantom is still ranked forever while the database now claims the
problem is handled. A retirement is only honest if a reader honours it. ✗

**Chosen — retire the card, delete the finding.** `mastered_count` and `due_reviews_count` become
truthful with **no change to a file I do not own**, and the user's own training record
(`attempts`, `successes`, `last_attempt_date`, `interval_days`, `ease_factor`) survives verbatim
and stays queryable. This is what the brief asked for ("retire rather than delete, keeping the
review row"), and it is strictly better than deleting it: the user keeps the evidence that they
drilled this position and that the engine no longer considers it a mistake.

**`is_patched` needs no special case, and I checked rather than assumed.** "Patched" is **not a
column**. `get_game_gaps` (`repertoire.py:2437`) *derives* it by comparing the finding's
`best_move` at its `fen_before` against the user's own repertoire tree. The fix therefore lives in
`repertoire_moves`, which this reconciliation never touches — removing a stale finding cannot lose
a patch.

**Safety property of the orphan.** `blunders.id` is `INTEGER PRIMARY KEY AUTOINCREMENT`, so a
retired card's `blunder_id` can never be silently re-bound to a different finding later. Pinned by
`test_a_retired_card_can_never_be_re_bound_to_another_finding`.

---

## 4. Changes

### `backend/analyzer.py`

| # | change | lines |
| :--- | :--- | :--- |
| 1 | `Iterable`, `Mapping`, `Set` imported | `:8` |
| 2 | `UnidentifiableBlunderError`, `NEVER_REVIEW_DUE = 9223372036854775807`, `RETIRED_DRILL_STATUS = "retired"`, `PRACTISED_DRILL_SQL`, `_ABSOLUTE_SCORE_TYPES` | `:83-140` |
| 3 | `_coerce_ply(value)` | `:145-156` |
| 4 | `natural_key_of(row)` — the identity rule | `:159-197` |
| 5 | `normalize_score` fail-closed (SYNC-23) | `:212-247` |
| 6 | `analyze_game_openings` refuses an id-less game **before** leasing an engine | `:386-403` |
| 7 | `_upsert_blunder(..., key)` writes and looks up the *same* validated key | `:547-590` |
| 8 | `_collapse_duplicate_rows` coerces the stored key the same way | `:614` |
| 9 | **`_reconcile_game_blunders`** (SYNC-19) | `:624-691` |
| 10 | `save_blunders_to_db(blunders, game_ids=None)` — validate-then-write, per-game reconcile, two new summary keys | `:693-790` |
| 11 | `analyze_and_store` passes the game's id as the write scope | `:801-815` |

**The SQL that does the work** (`_reconcile_game_blunders`):

```sql
SELECT b.id AS id, b.ply AS ply, b.played_move AS played_move,
       (COALESCE(d.attempts,0)>0 OR COALESCE(d.successes,0)>0
        OR COALESCE(d.last_attempt_date,0)>0
        OR COALESCE(d.status,'new') NOT IN ('new','')) AS practised
FROM blunders b LEFT JOIN drill_reviews d ON d.blunder_id = b.id
WHERE b.game_id = ? ORDER BY b.id
```
then, per row whose `(ply, played_move)` is not in the batch:
```sql
-- practised:  retire, never destroy the user's record
UPDATE drill_reviews SET status = 'retired', next_review_date = 9223372036854775807
 WHERE blunder_id = ?;
-- untouched:  drop the empty placeholder
DELETE FROM drill_reviews WHERE blunder_id = ?;
-- either way, the phantom leaves the gap list
DELETE FROM blunders WHERE id = ?;
```

### `backend/db.py` — 3 additions, 0 deletions, 0 `DELETE`s

```python
GAMES_ID_NOT_NULL_INDEX = ("ux_games_id_not_null", "games", "COALESCE(id, '')")
```
```python
# in _init_db_body(), right after the index catalogue
_create_games_id_index(cursor)
```
```sql
CREATE UNIQUE INDEX IF NOT EXISTS "ux_games_id_not_null" ON "games" (COALESCE(id, ''))
```

`_create_games_id_index` (`db.py:171-196`) **refuses to delete anything to make room.** If the
index is refused — only possible on a database that already holds ≥2 id-less `games` rows, i.e. one
the pre-fix code has already damaged — it prints a banner naming the row count and the exact
manual remedy (`DELETE FROM games WHERE id IS NULL OR id = '';`) and moves on. The `init_db()`
docstring's safety contract is updated to name it, so the list of "the only DELETEs in the boot
path" stays accurate: it is still the opt-in orphan purge and `_collapse_duplicate_blunders`.
**No new `DELETE` was introduced anywhere in the boot path.** `tests/test_migration_safety.py` (9)
and `tests/test_schema_integrity.py` (25) are green and untouched.

### `AGENTS.md` §5 (2 blocks) and §2/§3 (counts + the new file map entry)

§5 now documents (a) the `ux_games_id_not_null` index and *why* it exists, and (b) the `retired`
drill status with the full policy. Both are additions; no existing claim was weakened.
`test_docs_contract.py` (18) green.

---

## 5. Before / after evidence

`audit/scripts/FIXER-S-01_before_runner.py` copies the tree to a temp directory (never touching the
repo), installs `audit/scripts/FIXER-S-01_prefix_{analyzer,db}.py` verbatim, appends a **shim
supplying only the new public names with pre-fix behaviour** (so reds are behavioural, never
`ImportError`), and runs the module there. This exists because FIXER-P0 and FIXER-N each clobbered
a concurrent agent's work by `cp`-ing over a shared repo path.

### `tests/test_blunder_identity.py` — 30 tests

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-S-01_before_runner.py before
AssertionError: UnidentifiableBlunderError not raised            x5
AssertionError: 'no id' not found in ''
AssertionError: Tuples differ: ('  g1  ', ' 4 ', ' D2D4 ') != ('g1', 4, 'd2d4')
AssertionError: 3 != 1 : an id-less games record accumulated again
AssertionError: 'ux_games_id_not_null' not found in {'ix_games_user_color', 'sqlite_autoindex_games_1'}
AssertionError: Lists differ: [1, 3] != [1]        <- SYNC-19: stale rows survive
AssertionError: Lists differ: [9, 1] != [1]        <- the finding's own repro (ply 9 vs a 4-ply window)
AssertionError: Lists differ: [5, 1] != [1]
AssertionError: 2 unexpectedly found in [2, 3, 4, 5]   <- a retired card rebound to a new finding
AssertionError: ValueError not raised              <- SYNC-23: PovScore still guessed
AssertionError: (<class 'TypeError'>, <class 'ValueError'>) not raised
KeyError: 'stale_removed' / 'stale_retired'        x6
----------------------------------------------------------------------
Ran 30 tests in 1.730s
FAILED (failures=16, errors=6)          <-- 22 of 30 red

$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-S-01_before_runner.py after
----------------------------------------------------------------------
Ran 30 tests in 1.407s
OK
```

**22 of 30 red before, 0 after.** The 8 green-in-both are deliberate invariants, and I am not
claiming them as proof of the fix: `test_the_list_and_the_dashboard_count_the_same_blunders` and
`test_a_blunder_row_always_has_a_resolvable_game` (the fixture never had a NULL key — they pin
*agreement*, which the pre-fix code did not break), `test_a_pov_score_with_a_colour_still_works`,
`test_the_known_score_types_are_still_interpreted`, `test_the_real_call_sites_are_unaffected`
(the two production call sites always passed a colour, so SYNC-23 was never reachable — the
finding says so itself), `test_a_failed_reconciliation_rolls_the_whole_batch_back` (atomicity was
already correct pre-fix) and `test_a_real_game_is_never_folded_into_the_null_bucket`.

### The two reds the brief handed me

Both were **fixtures that construct a state the corrected schema forbids**, exactly as FIXER-N
predicted in their §6. No assertion was weakened; each fixture was corrected to build a
*realisable* state, and both were verified to pass against the pre-fix `db.py` too, proving the
index — not my code — was the sole cause.

* `tests/test_analyzer_correctness.py::test_preexisting_duplicate_rows_are_collapsed` — the test's
  premise is *"a database written BEFORE the UNIQUE index existed"*. It now says so and builds the
  premise the way a real pre-index database has it: `DROP INDEX`, insert the duplicates, run the
  writer, `CREATE UNIQUE INDEX` again. All six original assertions are byte-for-byte unchanged
  (2 rows before, 1 after, `duplicates_removed == 1`, and the **practised** copy with
  `attempts=7` survives).
* `tests/test_api_security.py::check_dashboard_eco_is_deterministic` — it inserted two rows with
  the **identical** natural key `(game_id, 1, 'd2d4')`, i.e. two different blunders for one move
  of one game, which is not a state the domain can produce. The two rows now have distinct plies
  *and* distinct moves (`d2d4` at ply 1, `b1c3` at ply 3). Everything it protects — one dashboard
  group, `blunder_count == 2`, and the reported ECO being the **worst** blunder's (`B90`) — is
  untouched.

Verified against the pre-fix `db.py` (which has no UNIQUE index), same corrected fixtures:
`test_preexisting_duplicate_rows_are_collapsed` → OK, `tests.test_api_security` → OK. So the
fixture correction restores the original semantics rather than papering over a regression.

### Production is untouched, and a real production copy boots clean

`audit/scripts/FIXER-S-01_evidence.py` copies the real 9-table file with a plain `open(…,'rb')`
(SHA-256 printed), boots `init_db()` on the **copy**, and re-digests every row:

```
[1] byte copy of production  sha256=d9497d3908f10fa0  bytes=2457600
[2] BEFORE init_db(): settings=5, games=307, blunders=153, drill_reviews=153,
                       repertoire_moves=0, repertoire_studies=0, study_moves=0,
                       explorer_cache=444, scouted_content=31
[5] AFTER  init_db(): (identical)
[6] row digests changed: NOTHING
[7] user_version after boot: 1
[8] indexes on games: ['ux_games_id_not_null', 'ix_games_user_color', 'sqlite_autoindex_games_1']
[9] games rows with an empty/NULL id: 0
[10] the COALESCE lookup plan: ['SEARCH games USING INDEX ux_games_id_not_null (<expr>=?)']
[12] second boot changed: NOTHING
[13] production fingerprint unchanged by this script: True
```

Production fingerprint `(1790451445361260136, 2457600, d9497d39…)` is **identical to the value
read before any of my work**. Production is never opened by SQLite: the only access is
`probe_guard.fingerprint()` (bytes, no connection) and the byte copy. No probe touched port 38281.

### `ps` — no leaked engines

`ps -eo args | grep -c stockfish` → **0** before, **0** after every full-suite run. My module
never starts Stockfish at all: the engine is a scripted `PovScore`-returning double, so the whole
30-test module is offline and instant.

---

## 6. Full suite

```
$ /home/robin/miniconda3/bin/python3 -m unittest discover tests
Ran 575 tests in 27.689s
FAILED (failures=3, errors=2)
```

**I did not reach a fully green suite, and none of the 5 is mine.** The A/B is exact — the *same
scratch-tree harness*, the *same* full-suite command, my two files reverted vs applied:

| tree | result |
| :--- | :--- |
| **pre-fix** (`analyzer.py` + `db.py` reverted, everything else identical) | `Ran 572 … FAILED (failures=22, errors=8, skipped=8)` — **30 failing tests** |
| **post-fix** (this patch) | `Ran 575 … FAILED (failures=3, errors=2)` — **5 failing tests** |

```
$ comm -13 before.txt after.txt     # failures present ONLY post-fix
(empty)                             # my patch introduced zero regressions
```

The 5 survivors are **byte-identical pre-fix and post-fix**:

| test | file | owner |
| :--- | :--- | :--- |
| `TestEngineIsLeasedNotShared::test_the_explorer_leases_from_the_shared_bounded_pool_and_hands_the_slot_back` | `tests/test_repertoire_data_integrity.py` | FIXER-A — `repertoire.py` explorer engine pool |
| `TestEngineIsLeasedNotShared::test_concurrent_explorer_calls_never_drive_one_engine_from_two_threads` | same | same |
| `RepertoireWriteSafety::test_black_repertoire_tree_uses_the_black_players_personal_stats` | `tests/test_repertoire_write_safety.py` | FIXER-A — `repertoire.py` |
| `TestUserWorkflows::test_workflow_1_white_repertoire_and_sidelines` | `tests/test_user_workflows.py` | FIXER-A — `repertoire.py` |
| `TestUserWorkflows::test_workflow_2_black_repertoire_setup_and_multiple_openings` | same | same |

Isolated pre/post A/B on the engine-pool pair (`audit/scripts/FIXER-S-01_before_runner.py before
tests.test_repertoire_data_integrity.TestEngineIsLeasedNotShared`): the **same 2 failures** in both
runs, with the same plan assertion (`False is not true : tree produced no evaluations: the lease
was not taken`). These are FIXER-A's engine-lease work in `backend/repertoire.py`, which I do not
own and which is being edited concurrently. The three workflow/personal-stats failures are the
same FIXER-A area (`the personal stats index was never built`).

My own modules, isolated and green:

```
tests.test_blunder_identity          Ran 30   OK      (new)
tests.test_analyzer_correctness      Ran 28   OK
tests.test_schema_integrity          Ran 25   OK      (FIXER-N's, untouched, still green)
tests.test_migration_safety          Ran  9   OK      (FIXER-P0's, untouched, still green)
tests.test_docs_contract             Ran 18   OK      (incl. my §5 edit)
tests.test_isolation_guard           Ran 17   OK      (my new file satisfies the static guards)
tests.test_api_security              Ran  1   OK      (the corrected fixture)
```

---

## 7. Hand-off

**FIXER-L — `backend/api.py:1273-1276` (`POST /api/sync`).** One line each, the change FIXER-K
already proposed in their §7.5:
```python
blunders, saved = analyzer.analyze_and_store(g)
new_blunders_count += saved["inserted"] + saved["updated"]
```
`analyze_and_store` passes the game's id as the reconciliation scope, which is what makes
SYNC-19's convergence reachable from this route.

**FIXER-A — `backend/repertoire.py:3084` and `:3238` (`analyze_user_games`,
`stream_analyze_user_games`).** Same one line. Both currently guard on `if blunders:`, so an
analysis that finds **nothing** never calls the writer and cannot converge — measured, §9.

**FIXER-L — `backend/api.py:993-1013` and the two `repertoire.py` writers.** Consider deleting
the games row when the analyzer refuses it, or skipping the record before the `INSERT OR REPLACE`.
With `ux_games_id_not_null` this is now a *converge in place* rather than an *append*, so it is no
longer urgent — but a record with no id has no business in `games` at all.

---

## 8. Also noticed (not fixed)

1. **A NULL-keyed `blunders` row that already exists is not swept.** It belongs to no game, so it
   is outside the per-game reconciliation scope — and removing it would be a table-wide `DELETE`
   with no owner, executed from a write path, which is the exact shape of the destructive
   `init_db()` migration this audit had to undo. The fix is that none can be *created* any more.
   A database that already has one needs a manual
   `DELETE FROM blunders WHERE game_id IS NULL`. Pinned explicitly by
   `test_a_legacy_unidentifiable_row_is_outside_the_per_game_scope` so the boundary is a decision,
   not an accident. **Production has 0** (measured read-only).
2. **`blunders.eval_before_mate` / `eval_after_mate` still do not exist** (FIXER-N §Also-noticed
   #1, FIXER-K §7.1.2). I investigated and **deliberately did not add them**: the natural place
   is the `blunders` `CREATE TABLE` + two `_add_column_if_missing` calls, but
   `tests/test_schema_integrity.py::test_legacy_column_order_matches_a_fresh_schema` asserts
   `fresh_columns[-1] == "best_pv"` *precisely so a fresh and a migrated schema have the same
   `PRAGMA table_info` order*. Appending two `ALTER`ed columns after `best_pv` on the migration
   path but declaring them before it in `CREATE TABLE` would make a migrated legacy schema
   **permanently** diverge from a fresh one, and the only fix is a table rebuild on the boot
   path. That test is one of the 25 the brief told me to keep green, so the columns stay absent;
   the analyzer's opportunistic write remains correct either way
   (`TestMateColumnsAreForwardCompatible` still passes). **Recommendation:** whoever owns that
   assertion decides the ordering, then it is a 4-line change.
3. **The three legacy call sites still say `if blunders:`** (§7). SYNC-19's convergence is
   implemented and tested, but only *reachable* through `analyze_and_store`. Measured in §9.
4. **`_OPTIONAL_MATE_COLUMNS` detection still costs one `PRAGMA` per save call.** Deliberate
   (FIXER-K §8.4): a stale cache would silently drop the mate distance if `db.py` ever adds the
   column mid-process.
5. **`PRACTISED_DRILL_SQL` is interpolated into a `SELECT`.** It is a module constant with no
   interpolation of caller data; the `%s` is a literal, not a format argument.
6. **`normalize_score` now raises on a `PovScore` with no colour.** Unreachable from the two
   production call sites (both pass a colour), and the finding explicitly asked for exactly this
   ("raise (not guess) for anything else"). The `except` path a future caller needs is a two-line
   `try`; the alternative was a confident wrong claim.

## 9. Residual risks

* **The `retired` card is an orphan.** `drill_reviews.blunder_id` points at a deleted
  `blunders.id` (the FK is inert — `AGENTS.md` §5 "PRAGMA foreign_keys is OFF"). It is
  intentionally invisible to every join-driven reader, and `AUTOINCREMENT` makes re-binding
  impossible, but it is inert data. A future "prune retired cards older than N" job would be
  needed if they ever accumulate; at one per stale-practised finding per re-analysis that is very
  slow. The user's *record* is preserved; that was the trade.
* **The reconciliation is a `DELETE` inside the write path**, scoped to one `game_id`. It is the
  first such `DELETE` in `analyzer.py` outside `_collapse_duplicate_rows`, and it is the intended
  semantics of a recomputation. It is **not** on the `init_db()` boot path, so the §6.2 contract
  is untouched.
* **`game_ids` is a destructive argument.** `save_blunders_to_db([], game_ids=[g])` empties `g`'s
  findings. That is the point, and it is why a non-string / empty value raises
  (`test_a_bad_game_id_argument_cannot_wipe_a_game`) — `str(None)` must not become the literal
  `"None"`, which would create a real, invisible game bucket.
* **A game analysed under two different `max_opening_ply` settings oscillates.** Lower the
  threshold and the far findings are removed; raise it and they come back as new rows with fresh
  cards. Correct (the stored set always equals the current computation) but it means the *set* of
  practised cards is not stable across setting changes. The retired cards keep the history.
* **If the engine fails mid-game**, `analyze_game_openings` raises and the writer is never called,
  so the stored set is untouched and the game is retried — the safe direction. It would be unsafe
  if a partial list were ever passed as if it were complete; that is why the scope is an explicit
  `game_ids` argument rather than inferred.
* **The two corrected fixtures are the only edits to another fixer's test files.** Both are
  fixture-only and are justified line by line in §5. I did not touch a single assertion.
* **`ux_games_id_not_null` is a 12th index on `games`.** Negligible at 307 rows; on this user's
  data the write cost is unmeasurable.
