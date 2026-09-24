# FIXER-K-01 — `backend/analyzer.py`: mate-aware evaluation text, idempotent blunder writes, per-game atomicity

**Owner:** FIXER-K · **Files touched:** `backend/analyzer.py` (owned),
`tests/test_analyzer_correctness.py` (new), `audit/scripts/FIXER-K-01_*.py` (new).
**Not touched:** `backend/db.py`, `backend/repertoire.py`, `backend/api.py`,
`backend/evaluator.py`, `backend/scout.py`, `backend/openings.py`,
`backend/lichess_client.py`, `frontend/**`, `AGENTS.md`, any existing test.

**Findings fixed:** `SYNC-06` (MEDIUM, wrong-result, user-visible),
`SYNC-15` (MEDIUM, data-loss — the write-path half; the schema half is handed off),
per-game atomicity (finding 4 of this task). `ABUSE-06` verified, not changed.

> FIXER-C's engine-pool refactor of `analyzer.py` (`_get_engine` / `close`, audit
> CODE-02-09) is preserved byte-for-byte and is *not* re-litigated here. The
> `git diff` for the file therefore contains both patches; the hunks below are
> only mine.

---

## 1. Root cause per finding

### SYNC-06 — a mate score is a *type*, not a number, and it was being compared as one

`backend/analyzer.py` destroyed the engine's score type at two places and then
tried to recover it from the scalar:

```python
# analyzer.py:162 (old)  -- the type is thrown away here
score_before = info_before["score"].pov(board.turn).score(mate_score=10000)
# analyzer.py:177 (old)
score_after  = info_after["score"].pov(not board.turn).score(mate_score=10000)
# analyzer.py:180 (old)  -- and the information is *reconstructed* from the scalar
cpl_loss = score_before - score_after
# analyzer.py:100-103 (old)  -- a magnitude test, the actual defect
if score_after <= -9000:
    why_bad = "Eval loss: Checkmate in 1."
else:
    why_bad = f"Eval loss: -{cpl_loss} centipawns."
```

Three concrete consequences, all reproduced in §4:

1. `Score.score(mate_score=10000)` returns `10000 - mate`, so **every** mate
   distance became a different near-sentinel: the same real mate in 1 stored
   `-9999` in one row and `9995` in another. The distance was unrecoverable.
2. The explanation branched on `score_after <= -9000`, a *magnitude* test. A
   completely lost-but-playable `-95.00` evaluation was announced to the user as
   **"Checkmate in 1"**, and a real **mate in 2** was announced as
   **"Checkmate in 1"** as well — the same string for three different facts.
3. `cpl_loss = before - after` on two saturated sentinels produced `19994`, which
   permanently owned the top of `ORDER BY cpl_loss DESC` (the `#1 BIGGEST
   BLUNDER` slot) for what is a one-move slip.

**Unit fact established with the real binary** (this is the part the report got
subtly wrong, so it is pinned by a test): `chess.engine.Score.mate()` is
documented as "number of plies", but the value that actually arrives is in
**moves** — the engine's own UCI unit. Verified in
`audit/scripts/FIXER-K-01_materepro.py` §A:

| position | real engine | `pov(WHITE).mate()` | plies to mate |
| :--- | :--- | :--- | :--- |
| after `1.f3 e5 2.g4`, black to move | `...Qh4#` | **-1** | 1 |
| `6RK/6PP/5n2/8/2n5/8/8/7k b`, black to move | `1...Ne5 2...Ng6#` | **-2** | 3 |

So `-1` is "mated on the opponent's very next move" and `-2` is "mated two
moves later". `abs(mate)` is directly the natural-language "checkmate in N" —
**no ply arithmetic** (an earlier draft of this patch divided by two and was
wrong for every mate distance).

### SYNC-15 — the write path had no natural key

```python
# analyzer.py:242-257 (old)  -- plain INSERT, no conflict clause, no key
cursor.execute("""INSERT INTO blunders ( game_id, move_number, ply, color, ... ) VALUES (?, ...)""", (...))
```

and `backend/db.py` declares no unique index on `blunders`
(`PRAGMA index_list(blunders)` → `[]` on a fresh database). Idempotency was
delegated entirely to the caller's single mutable flag `games.analyzed_at`, so
every reachable state where that flag is `0` while rows exist (a legacy
pre-migration database, a mid-batch crash, the `INSERT OR REPLACE` path in
`repertoire.analyze_user_games`, a failed `analyzed_at` UPDATE after a
successful blunder write) duplicates the whole corpus on the next sync.

The schema half belongs to `db.py` and is **handed off** (§7.1). The write-path
half is fixed here so the module is correct **with or without** that index: it
looks the row up on the real natural key `(game_id, ply, played_move)` and
UPDATEs it if present, and it collapses any rows that already duplicate the key.

### Per-game atomicity — the batch had no transaction boundary and no `finally`

```python
# analyzer.py:375-407 (old)
conn = get_db(); cursor = conn.cursor()
for b in blunders:
    cursor.execute("INSERT ..."); blunder_id = cursor.lastrowid
    cursor.execute("INSERT OR IGNORE INTO drill_reviews ...")
conn.commit()
conn.close()          # never reached if anything above raises
```

Two defects: (a) an exception mid-batch leaves the connection **open**, and an
open write connection holds the WAL write lock, so the *next* write in the
process fails with `sqlite3.OperationalError: database is locked` after the
30 s busy timeout (reproduced pre-fix, §4.2); (b) there is no single place a
caller can use as "this game is fully persisted".

---

## 2. Changes

### 2.1 `backend/analyzer.py:5-80` — module constants, natural key, column map

```diff
+import threading
 from typing import Any, Dict, List, Optional, Sequence, Tuple
 from backend.db import get_db, get_setting
-from backend.evaluator import get_sync_engine_pool
+from backend.evaluator import MATE_SCORE_CP, get_sync_engine_pool

+#: `MATE_SCORE_CP` (imported from `backend.evaluator`) is the saturated
+#: centipawn stand-in written to `blunders.eval_before` / `eval_after` when the
+#: engine returned a **mate** score.  Sharing the evaluator's constant keeps the
+#: SSE eval bar and the blunder table on one sentinel, and a mate is never
+#: stored as `10000 - mate` (which produced a different number for every mate
+#: distance -- 9995, 9998, 9999, 9901, ...).
+
+#: A single move cannot lose more than "one mate" worth of evaluation, so
+#: `cpl_loss` is clamped to this.  Pre-fix a mate-in-5 -> mate-in-1 collapse
+#: stored 19994 and permanently owned the top of `ORDER BY cpl_loss DESC`
+#: (audit SYNC-06, "ranking pollution").
+MAX_CPL_LOSS = MATE_SCORE_CP
+
+#: Natural key of a blunder row (audit SYNC-15). ...
+BLUNDER_NATURAL_KEY: Tuple[str, str, str] = ("game_id", "ply", "played_move")
+
+#: Written only when the schema actually has them (hand-off to FIXER-P0:
+#: `ALTER TABLE blunders ADD COLUMN eval_before_mate INTEGER DEFAULT NULL`
+#:  and the same for `eval_after_mate`). ...
+_OPTIONAL_MATE_COLUMNS: Tuple[str, str] = ("eval_before_mate", "eval_after_mate")
+
+_BLUNDER_COLUMNS: Tuple[Tuple[str, str], ...] = ( ... 25 entries ... )
+_SAVE_LOCK = threading.RLock()
+_OPTIONAL_BLUNDER_KEYS = frozenset({"best_pv", "opening_games"})
```

`_BLUNDER_COLUMNS` is the single source of truth for both the INSERT and the
UPDATE, so the two statements cannot drift. `_OPTIONAL_BLUNDER_KEYS` preserves
the pre-fix `.get()` behaviour for `best_pv` / `opening_games`; every other key
is **required**, which is what makes a malformed row fail loudly (and roll back)
instead of writing a half-row.

### 2.2 `backend/analyzer.py:83-149` — the mate field is the only mate signal

```python
def normalize_score(score, color=None) -> Tuple[int, Optional[int]]:
    """Split an engine score into ``(centipawns, mate_distance)``. ..."""
    if color is not None and hasattr(score, "pov"):
        score = score.pov(color)
    mate = score.mate() if hasattr(score, "mate") else None
    if mate is None:
        raw = score.score() if hasattr(score, "score") else None
        if raw is None:
            return MATE_SCORE_CP, 0          # MateGiven
        return int(raw), None
    return (MATE_SCORE_CP if mate >= 0 else -MATE_SCORE_CP), int(mate)


def mate_distance_moves(mate: Optional[int]) -> int:
    if mate is None:
        return 0
    return max(1, abs(int(mate)))


def describe_score_mate(mate: Optional[int]) -> str:
    if mate is None:
        return ""                            # <- no mate, no claim, ever
    moves = mate_distance_moves(mate)
    if mate < 0:
        return f"allows checkmate in {moves}"
    if mate == 0:
        return "checkmate is already on the board"
    return f"reaches a forced mate in {moves} for you"


def centipawn_loss(cp_before: int, cp_after: int) -> int:
    return max(0, min(int(cp_before) - int(cp_after), MAX_CPL_LOSS))
```

`describe_score_mate` contains **no arithmetic on a centipawn magnitude** — it
cannot be reached with a centipawn value at all, because `None` is the only
non-mate input and it returns `""` immediately. The old `score_after <= -9000`
test is gone; there is no threshold left in the module to get wrong.

### 2.3 `backend/analyzer.py:223-256` — the explanation is now unfalsifiable

```diff
         opening_name: str,
-        score_after: int = 0
+        score_after: int = 0,
+        mate_after: Optional[int] = None,
+        mate_before: Optional[int] = None
     ) -> Tuple[str, str, str]:
         ...
         category = "Blunder" if cpl_loss >= 200 else ("Mistake" if cpl_loss >= 100 else "Inaccuracy")
 
-        if score_after <= -9000:
-            why_bad = "Eval loss: Checkmate in 1."
-        else:
-            why_bad = f"Eval loss: -{cpl_loss} centipawns."
+        mate_phrase = describe_score_mate(mate_after)
+        if mate_phrase:
+            why_bad = f"Eval loss: {mate_phrase}."
+        else:
+            why_bad = (f"Eval loss: {cpl_loss} centipawns "
+                       f"({score_after / 100.0:+.2f} pawns, no forced mate).")
+        if mate_before is not None and mate_before > 0:
+            why_bad += (f" You had a forced mate in "
+                        f"{mate_distance_moves(mate_before)} for you before the move.")
         strategic_idea = f"Engine top choice: {board_before.san(best_move)}."
         return why_bad, strategic_idea, category
```

Both new parameters are **keyword-with-default**, so the existing
`tests/test_analyzer.py::test_explanation_generation` (which calls the method
with six positional arguments) is untouched and still passes.

The three required explanations, **before → after**:

| score the engine returned | before | after |
| :--- | :--- | :--- |
| real mate in 1 (`PovScore(Mate(-1))`, user POV) | `Eval loss: Checkmate in 1.` (from the `-9999` magnitude) | `Eval loss: allows checkmate in 1.` |
| real mate in 2 (`PovScore(Mate(-2))`, user POV) | `Eval loss: Checkmate in 1.` **← wrong** | `Eval loss: allows checkmate in 2.` |
| plain `-95.00` evaluation (`Cp(-95)`) | `Eval loss: -95 centipawns.` (no disambiguation) | `Eval loss: 95 centipawns (-0.95 pawns, no forced mate).` |
| plain `-95.00` *pawns* (`Cp(-9500)`) | `Eval loss: Checkmate in 1.` **← false** | `Eval loss: 9500 centipawns (-95.00 pawns, no forced mate).` |

### 2.4 `backend/analyzer.py:300-333` — the analysis loop keeps the score type

```diff
                 info_before = engine.analyse(board, chess.engine.Limit(depth=12))
-                score_before = info_before["score"].pov(board.turn).score(mate_score=10000)
+                score_before, mate_before = normalize_score(info_before["score"], board.turn)
                 ...
                 info_after = engine.analyse(board, chess.engine.Limit(depth=12))
-                score_after = info_after["score"].pov(not board.turn).score(mate_score=10000)
+                score_after, mate_after = normalize_score(info_after["score"], not board.turn)
 
-                # Centipawn loss
-                cpl_loss = score_before - score_after
+                # Centipawn loss.  Bounded on both sides: a mate is stored as the
+                # +/-MATE_SCORE_CP sentinel, so the raw difference of two
+                # sentinels would be 20000 and would mean nothing (audit SYNC-06).
+                cpl_loss = centipawn_loss(score_before, score_after)
```

and `:343-353` passes `mate_after` / `mate_before` into the explanation, and
`:361-366` adds the two mate distances to the row dict:

```python
"eval_before": score_before,
"eval_after": score_after,
# Mate distance in moves from the user's point of view
# (None = not a mate).  Written to the DB only once the
# schema has the columns -- see FIXER-P0 hand-off.
"eval_before_mate": mate_before,
"eval_after_mate": mate_after,
```

### 2.5 `backend/analyzer.py:397-536` — idempotent, atomic, duplicate-collapsing write path

`_upsert_blunder()` (`:405`) — INSERT-or-UPDATE on the natural key:

```python
cursor.execute(
    "SELECT id FROM blunders WHERE game_id = ? AND ply = ? AND played_move = ? ORDER BY id LIMIT 1",
    (b["game_id"], b["ply"], b["played_move"]))
existing = cursor.fetchone()
if existing is not None:
    updatable = [(n, v) for n, v in zip(names, values) if n not in BLUNDER_NATURAL_KEY]
    cursor.execute("UPDATE blunders SET " + ", ".join(f"{n} = ?" for n, _ in updatable)
                   + " WHERE id = ?", [v for _, v in updatable] + [int(existing["id"])])
    return int(existing["id"]), False
```

`_collapse_duplicate_rows()` (`:439`) — merges rows that already duplicate the
key, **keeping the copy with the most drill practice** so collapsing can never
destroy the user's spaced-repetition progress:

```sql
SELECT b.id, b.ply, b.played_move, COALESCE(d.attempts,0), COALESCE(d.successes,0)
FROM blunders b LEFT JOIN drill_reviews d ON d.blunder_id = b.id
WHERE b.game_id = ?
ORDER BY b.ply, b.played_move, attempts DESC, successes DESC, b.id
```

`save_blunders_to_db()` (`:471`) — one transaction, lock, `finally: close()`,
and a count summary instead of `None`:

```python
summary = {"inserted": 0, "updated": 0, "duplicates_removed": 0, "drills_created": 0}
if not blunders:
    return summary
with _SAVE_LOCK:
    conn = get_db()
    try:
        ...upsert every row, create its drill row, collapse duplicates...
        conn.commit()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        raise
    finally:
        try: conn.close()
        except Exception: pass
return summary
```

`analyze_and_store()` (`:526`) — the one call a route should make:

```python
blunders = self.analyze_game_openings(game_dict)
saved = self.save_blunders_to_db(blunders)
return blunders, saved
```

Returning the `summary` dict is additive: the only production caller
(`api.py:986`) ignores the return value.

---

## 3. Test evidence — before vs after

`tests/test_analyzer_correctness.py`, **28 tests, no network, no long analysis**.
The three real-engine tests use the shared pool at depth 8 on two tiny endgames
and close the pool in `tearDownClass`; everything else uses `chess.engine.Cp` /
`chess.engine.Mate`, so it is deterministic and instant. The write tests run
against a **private scratch database** created by `init_db()` in `setUpClass`
(see §7.2 for why that is necessary).

The **before** run uses `audit/scripts/FIXER-K-01_before_runner.py`, which
restores `git show HEAD:backend/analyzer.py` verbatim, installs it as
`backend.analyzer`, and adds a **legacy shim** supplying the new API names with
exactly the pre-fix behaviour (`normalize_score` = `(pov.score(mate_score=10000), None)`,
`describe_score_mate` = `""`, `centipawn_loss` = unbounded `before - after`, the
pre-fix `_generate_tactical_explanation` body wrapped to accept and ignore the
new mate keywords, `analyze_and_store` = the two old calls). Everything under
test is pre-fix code.

### 3.1 The required before/after headline

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-K-01_before_runner.py
AssertionError: 2 != 3 : ('Eval loss: Checkmate in 1.', 'Eval loss: Checkmate in 1.',
                         'Eval loss: -95 centipawns.')
AssertionError: 9995 != 10000
AssertionError: -9999 != -10000
AssertionError: 6 != 1 : [{'id': 14, 'ply': 1, ...}, {'id': 15, 'ply': 3, ...},
                          {'id': 16, 'ply': 1, ...}, {'id': 17, 'ply': 3, ...},
                          {'id': 18, 'ply': 1, ...}, {'id': 19, 'ply': 3, ...}]
AssertionError: 2 != 1 : [{'id': 20, 'cpl_loss': 300, ...}, {'id': 21, 'cpl_loss': 450, ...}]
sqlite3.OperationalError: database is locked
BEFORE: ran 28 tests, failures=22 errors=4
```

The first line **is** SYNC-06: mate in 1 and mate in 2 produced the *same*
string, and the `-95.00` case produced a third string that says nothing about
whether a mate exists. The `6 != 1` line **is** SYNC-15: three saves of the same
two blunders produced six rows.

### 3.2 Full before/after

```
BEFORE  (git HEAD analyzer.py + legacy shim)
  tests.test_analyzer_correctness .....  28 tests
  failures=22  errors=4   (1 test passes: two blunders at the same ply but
                           different moves are not duplicates -- correct then too)

AFTER   (this patch)
  $ /home/robin/miniconda3/bin/python3 -m unittest tests.test_analyzer_correctness
  Ran 28 tests in 1.6s
  OK
```

### 3.3 What fails before, by finding

| test | finding | pre-fix result |
| :--- | :--- | :--- |
| `test_real_mate_in_1_is_explained_as_checkmate_in_1` | SYNC-06 | FAIL `None != -1` (no mate field) |
| `test_real_mate_in_2_is_explained_as_checkmate_in_2` | SYNC-06 | FAIL `None != -2` |
| `test_minus_95_cp_is_never_called_a_checkmate` | SYNC-06 | FAIL `'-0.95' not found in 'Eval loss: -95 centipawns.'` |
| `test_the_three_required_cases_are_correct_and_mutually_different` | SYNC-06 | FAIL `2 != 3` |
| `test_a_huge_centipawn_score_is_not_a_checkmate` | SYNC-06 | FAIL (cp -9000 → "Checkmate in 1") |
| `test_mate_distance_comes_from_the_field_not_the_magnitude` | SYNC-06 | FAIL (all 5 distances → one string) |
| `test_normalize_score_saturates_and_preserves_the_mate_field` | SYNC-06 | FAIL `(9999, None) != (10000, 1)` |
| `test_centipawn_loss_is_bounded` | SYNC-06 | FAIL `-420 != 0` / unbounded |
| `test_walking_into_a_mate_in_1` (end-to-end) | SYNC-06 | FAIL `eval_after == -9999` |
| `test_walking_into_a_mate_in_2_is_not_reported_as_mate_in_1` | SYNC-06 | ERROR (no `eval_after_mate` at all) |
| `test_huge_cp_blunder_is_never_described_as_a_checkmate` | SYNC-06 | ERROR |
| `test_losing_a_forced_mate_is_bounded_and_kept` | SYNC-06 | FAIL (`cpl_loss` 10100) |
| `test_repeated_analysis_converges_on_one_row` | SYNC-15 | FAIL `6 != 1` |
| `test_resaving_updates_instead_of_appending` | SYNC-15 | FAIL `2 != 1` |
| `test_preexisting_duplicate_rows_are_collapsed` | SYNC-15 | FAIL `3 != 1` |
| `test_concurrent_saves_of_one_game_converge_on_one_row` | SYNC-15 | FAIL `6 != 1` |
| `test_analyze_and_store_is_atomic_and_idempotent` | SYNC-15 | FAIL |
| `test_a_failure_mid_batch_rolls_the_whole_batch_back` | atomicity | ERROR `database is locked` |
| `test_the_connection_is_closed_even_when_the_batch_fails` | atomicity | ERROR `database is locked` |
| `test_mate_columns_are_written_when_present_and_skipped_when_absent` | SYNC-06 | FAIL (columns never written) |
| 2 × `TestRealEngineMateScores` (real Stockfish) | SYNC-06 | FAIL — the real mate in 2 stored `-9998` and would be labelled "Checkmate in 1": `(-9998, None) != (-10000, -2)`. The third real-engine test (a plain `+27 cp` score) passes pre-fix by accident, because the old threshold was `-9000` |

The 3 real-engine tests run against the *actual* binary in both runs: the
pre-fix `GameAnalyzer` gets an engine via its own uncached `popen_uci`, the
post-fix one via the pool, and both are closed in `tearDownClass`.

### 3.4 Isolated module + neighbours

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_analyzer_correctness
Ran 28 tests in 1.613s   OK
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_analyzer
Ran 2 tests in 1.452s    OK          (FIXER-C's / the original file, untouched)
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_evaluator_stream
Ran 29 tests in 2.578s   OK          (FIXER-C's suite, still green)
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_isolation_guard
Ran 17 tests in 3.307s   OK          (my new file satisfies the static guards)
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_api tests.test_api_contract tests.test_repertoire
Ran 47 tests in 3.732s   OK
```

---

## 4. Full suite

```
$ /home/robin/miniconda3/bin/python3 -m unittest discover tests
Ran 427 tests in 24.7s
FAILED (failures=2)

FAIL: test_hostile_values_produce_no_executable_node (test_builder_xss_contracts...)
FAIL: test_hostile_arrows_never_block_the_drill (test_builder_xss_contracts...)
```

The baseline for this task was `Ran 104 ... OK`; other agents are mid-flight, so
the count and the failing set move between runs (I observed 425 → 427 during this
task alone). **Both failures are in a file I do not own** and neither is related
to `analyzer.py`: they assert on `frontend/repertoire_builder.js` /
`frontend/scout.js` source text (`if (data.status !== 'success')`), i.e.
frontend work in flight.

An earlier run of the same command also showed
`test_docs_contract::test_documented_test_count_is_current` failing, because §2
of `AGENTS.md` documented 426 collected tests; its owner has since relaxed that
check to a ±5 % tolerance and it now passes, so the current standing is the two
frontend failures above. I introduced no new failure and edited no test outside
my new file.

---

## 5. `ps` resource evidence (real Stockfish)

`audit/scripts/FIXER-K-01_engine_probe.py`. Engines are attributed **by PID and
PPID**, so another agent's concurrently running suites can never be mistaken for
mine (`ps -eo pid,ppid,args`, PPID == this process). 8 sessions per phase, one
short opening line, the pipeline's own depth.

```
BASELINE: stockfish processes parented by THIS process: 0
         sync pool: max_size=2 spawned=0 live=0 in_use=0

A) 8 CONCURRENT analyses (POST /api/sync shape)  sessions=8  peak_my_children=2  wall=12.5s  errors=none
   pool after A: spawned=2 peak_in_use=2 live=2 in_use=0
B) 8 SEQUENTIAL analyses (reuse check)         sessions=8  peak_my_children=2  wall= 0.34s errors=none
   pool after B: spawned=2 (+0 for 8 more sessions) live=2
C) 8 CONCURRENT _get_engine() checkouts         sessions=8  peak_my_children=2  wall= 0.01s errors=none
   (phase C is the api.py:1678 drill-submit shape)

VERDICT  cap=2 honoured: A=2 B=2 C=2  -> PASS

engine PIDs tracked by this process BEFORE close_all(): [1327302, 1327386]
engine PIDs still parented by this process AFTER close_all(): none
=> ORPHANS LEFT BEHIND: none
=> live=0 in_use=0
=> after kill_stray_child_engines(): none
RESULT: PASS
```

**ATK-5 said 8 cold-start requests → 8 processes, 7 orphaned at ~250 MB.**
Phase A is the same 8 cold starts through the same code path: **2 processes,
0 orphans**, and the pool is 1 slot per concurrent user by construction
(`BoundedSemaphore` held for the whole search). Phase B proves the second half
of the fix — the other 6 sessions reused the same 2 processes (`+0 spawned`).

**I left nothing behind.** Before/after inventory around all of my real-engine
work (mine are PPID-attributed; the others belong to other agents' concurrently
running `unittest discover tests`):

```
mine: 0 stockfish children before, 0 after (probe prints its own inventory; the
      only run that ever leaked one — a probe that raised before close() — was
      reaped by evaluator.kill_stray_child_engines(), verified: the PID
      disappeared from `ps` immediately after that process exited)
host: whatever other agents' suites hold; I killed nothing of theirs
```

### 5.1 `repertoire.py:80-88` — measured, NOT fixed (FIXER-A's file)

`RepertoireManager._get_engine()` is still an uncached, unsynchronised
`chess.engine.SimpleEngine` on a singleton. Measured with
`audit/scripts/FIXER-K-01_repertoire_engine_probe.py` (this python-chess
version, real binary, 4 threads):

```
phase B: singleton engine pre-created -> ok
PHASE B (shared engine, concurrent analyse) THREADS=4 wall=0.00s completed=0 still_running=0
results=[]
errors=['CancelledError()', 'CancelledError()',
        'AssertionError(<CommandState.NEW: 1>)', 'AssertionError(<CommandState.NEW: 1>)']
   … plus python-chess itself raising, in its own transport callback:
   chess/engine.py:1273 self.finished.set_result(None) -> asyncio.exceptions.InvalidStateError
PHASE B2 (reuse the shared engine single-threaded): WEDGED -- no answer in 10s
PHASE A (cold concurrent checkout) THREADS=4 wall=0.51s completed=4 peak_my_children=5
engine children BEFORE close(): [1439360, 1440888, 1440890, 1440894, 1440895]
close() returned: True
engine children AFTER  close(): [1439360, 1440888, 1440890, 1440894]   <= orphans the race created
VERDICT A  spawn bound for a singleton (expect <=1): peak=5 -> OVERSPAWN / UNSYNCHRONISED
after kill_stray_child_engines(): none  <= only FIXER-C's exit net cleans these up
OVERALL: FAIL
```

Three separate facts, all on `repertoire.py`:

1. **4 concurrent callers of a singleton spawned 5 processes**, and
   `close()` left **4 orphans** — the identical defect FIXER-C fixed in
   `analyzer.py`, still live here.
2. **Two threads driving the same engine corrupt it**: every thread fails and
   python-chess's own callback raises `InvalidStateError`.
3. **After that, the engine is permanently wedged** — a *single-threaded*
   `analyse()` on the same object never returns (B2, 10 s cap). This is ATK-1,
   reproduced on the current tree. (A wedged `quit()` also hangs, which is why
   the probe caps `close()` in its own thread and ends with `os._exit`.)

The one-line fix is FIXER-C's pool, exactly as in `analyzer.py`:
`get_sync_engine_pool(STOCKFISH_PATH).acquire()` + `release()` in
`RepertoireManager.close()`. I did not touch the file.

---

## 6. Per-game atomicity — verification

| question | answer | evidence |
| :--- | :--- | :--- |
| Can the pipeline mark a game analysed unless the analysis completed? | **Not in `api.py`** (`POST /api/sync`, `api.py:984-991`): `analyze_game_openings` → `save_blunders_to_db` → *then* `UPDATE games SET analyzed_at`, inside a per-game `try` with per-game error isolation. **Yes, in `repertoire.py::analyze_user_games` (`repertoire.py:1862-1875`)** — it `INSERT OR REPLACE`s the game with `analyzed_at = int(time.time())` and `conn.commit()`s **before** running the analysis. A crash there leaves the game permanently "analysed" with no blunders, and the `analyzed_at > 0` guard at `:1854` then skips it forever. `analyzer.close()` / `conn.close()` at `:1888-1889` are also not in a `finally`. | read-only inspection; **hand-off §7.3** |
| Can a half-written blunder set survive? | **No, not any more.** One transaction per game (`save_blunders_to_db`), `rollback()` + `close()` in `except`/`finally`. Pre-fix, a mid-batch failure left the connection open and the next write in the process died with `database is locked`. | `test_a_failure_mid_batch_rolls_the_whole_batch_back`, `test_the_connection_is_closed_even_when_the_batch_fails` (§3.1) |
| Is the idempotency fix *reachable*? | Yes: if the blunder write commits and the following `UPDATE games SET analyzed_at` fails, the game keeps `analyzed_at = 0` with rows present — the exact SYNC-15 reachable state. Pre-fix that doubled the rows; now it converges. | `test_analyze_and_store_is_atomic_and_idempotent` |
| Is there a single call a route can trust? | Yes: `GameAnalyzer.analyze_and_store(game_dict)` returns only after every blunder of the game is committed, or raises. | `backend/analyzer.py:526`; **hand-off §7.5** |

---

## 7. Hand-off list

### 7.1 FIXER-P0 — `backend/db.py` (schema, two items)

1. **Uniqueness for the natural key** (SYNC-15's schema half; the write path is
   fixed, the index is not mine):
   ```sql
   CREATE UNIQUE INDEX IF NOT EXISTS ux_blunders_natural
       ON blunders(game_id, ply, played_move);
   ```
   Caveat: a fresh database cannot have duplicates, but the **production**
   database may already contain them (SYNC-15 escalation #1 says a pre-migration
   database doubles its corpus on the next sync). `CREATE UNIQUE INDEX` will
   *fail* on such a file, so the migration must first collapse duplicates —
   keeping the copy with the most `drill_reviews.attempts`/`successes`, exactly
   as `GameAnalyzer._collapse_duplicate_rows` (`analyzer.py:439`) already does,
   or the index creation must be wrapped so a failure is logged rather than
   fatal. **Please do not run it on production without a backup.**
2. **The mate columns.** `analyzer.py` writes them **only if they exist**:
   ```sql
   ALTER TABLE blunders ADD COLUMN eval_before_mate INTEGER DEFAULT NULL;
   ALTER TABLE blunders ADD COLUMN eval_after_mate INTEGER DEFAULT NULL;
   ```
   (sign convention: negative = that side is getting mated, magnitude = moves
   until mate). Once they exist, `analyzer.py` fills them in with no further
   change — `tests/test_analyzer_correctness.py::TestMateColumnsAreForwardCompatible`
   proves both directions (present → written, absent → skipped). If you name
   them differently, tell me and I will rename the constant
   `_OPTIONAL_MATE_COLUMNS`.

### 7.2 FIXER-P0 / FIXER-D — `blunders.best_pv` is missing from the schema (pre-existing, **not mine**)

`backend/db.py:118-144` does not create `best_pv`, and there is no
`ALTER TABLE` for it, but `analyzer.py` has always inserted into it and
`api.py:1752` reads it. On a **fresh** database the whole blunder write path
fails outright:

```
sqlite3.OperationalError: no column named best_pv
```

(Production has the column, added out-of-band — hence the bug is invisible until
someone builds a new profile.) My new tests add it to their private scratch
schema so they can measure the behaviour under test, but the schema must declare
it. `AGENTS.md` §5 documents this as known; the *fix* is still missing.

### 7.3 FIXER-A — `backend/repertoire.py` (measured in §5.1, not edited)

1. `RepertoireManager._get_engine()` (`repertoire.py:80-88`): route it through
   `get_sync_engine_pool(STOCKFISH_PATH).acquire()` and return the lease in
   `RepertoireManager.close()` (`:90-96`), exactly as `analyzer.py:168-186` does.
   Measured: 4 concurrent callers of a singleton → **5 processes, 4 orphans**;
   a shared engine driven from 2 threads is **permanently wedged**.
2. `analyze_user_games` (`:1862-1875`) stamps `analyzed_at` and commits
   **before** analysing, and uses `INSERT OR REPLACE` (which is DELETE+INSERT in
   SQLite and, with `PRAGMA foreign_keys` OFF, silently **orphans the game's
   blunders** on every re-sync). `stream_analyze_user_games` (`:1974+`) has the
   same shape. Both should: insert/upsert with `analyzed_at = 0`, run the
   analysis, then stamp — and wrap `analyzer.close()` / `conn.close()` in
   `finally`.

### 7.4 FIXER-D / docs owner — `AGENTS.md` §2 test counts (now satisfied)

`tests/test_docs_contract.py::test_documented_test_count_is_current` was red
while §2 documented 426 collected tests and the tree collected 427; the check has
since been relaxed to a ±5 % tolerance and it passes. **§3 already lists
`tests/test_analyzer_correctness.py`**, so no documentation change is needed from
me. No action.

### 7.5 FIXER-L — `backend/api.py` (optional, one line each)

1. `api.py:984-987` can collapse three statements into the atomic one I added:
   ```python
   blunders, saved = analyzer.analyze_and_store(g)
   new_blunders_count += saved["inserted"] + saved["updated"]
   ```
   This also makes `new_blunders` honest: today it counts rows the analyser
   *found*, including rows that were merely updated on a re-analysis.
2. `api.py:218` / `:1676-1721` construct a `GameAnalyzer` per request and call
   the private `analyzer._get_engine()`. That is still correct (the lease is
   returned by `close()`), but `analyze_and_store`/a public `acquire()` would
   remove the reach-through. Your `EngineBusy` → 503 handling at `:993` and
   `:1702` is already in place and my changes do not affect it.
3. If you want the drill-submit message to show the mate distance, read
   `eval_after_mate` from the blunder row (once §7.1 lands) instead of
   formatting `cpl_loss` — `api.py:1673` currently prints
   `Game Move: {san}?! (-{cpl_loss} cp)`, which for a mate row reads
   `(-10000 cp)`.

### 7.6 Frontend owners (informational, no action required)

`explanation` is served by `GET /api/blunders/{id}` and by the drill-submit
payload (`api.py:1767-1768`) and is rendered by
`frontend/repertoire_trainer.js` (the `data.message` line). Its length grew by
one clause (`"... (-0.95 pawns, no forced mate)"`); the trainer renders it in
`.feedback-box`, which is a block element, so `AGENTS.md` §8.2/§8.3 do not
apply. No frontend change is needed for this fix.

---

## 8. Also noticed (not fixed)

1. **`analyze_game_openings` has no per-game engine error handling.** A dead
   engine mid-game raises out of the loop and the whole game's analysis is lost
   (the caller marks it pending and retries next sync, so it is recoverable, not
   lossy). Out of scope for a surgical fix.
2. **`max_opening_ply` counts plies, not moves** (`analyzer.py:113`, default
   `"24"` commented "12 moves") — correct as written, but the default of 24 plies
   reaches further into the game than "opening" suggests. Pre-existing.
3. **`is_book` moves can still be recorded as blunders** — `is_book_move` is
   stored but never used to filter inside the analyzer (it is filtered later in
   `get_game_gaps`). Pre-existing, reported by earlier audits.
4. **`_OPTIONAL_MATE_COLUMNS` detection costs one `PRAGMA` per save call.** It is
   not cached, deliberately: `db.py` may add the column while the process is
   running, and a stale cache would silently drop the mate distance.
5. **`_collapse_duplicate_rows` deletes rows.** It is the only delete this module
   performs, it is scoped to the game being written, and it keeps the most-practised
   copy — but it is a delete in a file whose owner elsewhere is forbidden from
   deleting, so it is called out here explicitly.
6. **`Mate(0)` is ambiguous in python-chess** (it folds "checkmated" and
   `MateGiven` together), so `normalize_score` returns the positive sentinel and
   the text says "checkmate is already on the board". An already-finished
   position cannot reach `analyze_game_openings`, so this is unreachable in
   practice; documented in the docstring and pinned by a test.

## 9. Residual risks

* **`cpl_loss` is now clamped to 10000.** Two different one-move losses can tie
  at the cap (e.g. losing a mate in 1 vs. losing a mate in 30), and
  `ORDER BY cpl_loss DESC` can no longer distinguish them. That is the intended
  trade (the number now means "at least this much"), and the mate distance is
  recoverable from the explanation text and — once §7.1 lands — from
  `eval_after_mate`. If you want a strict ranking, rank on
  `(eval_after_mate IS NULL, cpl_loss DESC)` instead.
* **The write path serialises on a process-wide `RLock`.** A long batch blocks
  other blunders writes in the same process. A batch is one game's plies (≤ ~24
  `analyse` results), so the critical section is milliseconds; it is a
  correctness measure, not a throughput one.
* **Idempotency is per `(game_id, ply, played_move)`.** If a *replay* of the same
  game legitimately contained a different move at the same ply, the new row
  would overwrite the old one rather than duplicate it. For a fixed Lichess game
  id the moves are immutable, so this is the intended semantics.
* **The mate columns are inert until `db.py` declares them** (§7.1). Until then
  the mate distance survives only in the `explanation` string — enough for the
  user, not enough for SQL.
