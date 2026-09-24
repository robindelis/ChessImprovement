# FIXER-A-01 — `backend/repertoire.py`: write safety, deterministic lookups, read-only GETs

- **Fixed file:** `backend/repertoire.py` (sole owned file)
- **New tests:** `tests/test_repertoire_write_safety.py` (8 tests, one per finding)
- **New probes:** `audit/scripts/FIXA-01_verify_fixes.py`, `audit/scripts/FIXA-01_prefix_runner.py`
- **Test file I had to amend (LOUD):** `tests/test_studies.py` — 2 tests asserted the *data-loss* behaviour
  and now state their preconditions. Details in §2 "Tests I changed, and why".
- **Interpreter:** `/home/robin/miniconda3/bin/python3` (base env), SQLite 3.53.2, Python 3.14.6.
- **Production DB:** never opened. Every probe/test imports `tests.test_setup` first.

Diff size: `backend/repertoire.py` **+341 / −108** (7 hunks of behaviour, 1 new helper), full patch below.

---

## 1. Per-finding root cause → change → evidence

### CODE-1 (critical, data-loss) — re-adding a move wiped notes, arrows, importance and all SRS state

**Root cause.** Two losses stacked in `add_repertoire_move`:
1. the "one user move per position" block had an `elif existing_user_move:` branch that `DELETE`d the
   row even when the incoming move **was** the move already prepared for that position, and it ran
   *before* `existing_meta` was read, so the metadata read always answered `None`;
2. the write itself was `INSERT OR REPLACE`, which deletes the conflicting row first and therefore
   resets every column absent from the INSERT list to its schema `DEFAULT`
   (`is_important`, `comment`, `arrows`, `times_practiced`, `times_correct`, `last_practiced`,
   `next_review`, `interval_days`, `created_at`) — and the row got a new `id`.

Because `UNIQUE(repertoire, parent_fen, move_uci)` already makes the write an upsert, this was a
self-inflicted data-loss bug on every write path (`POST /api/repertoire/move`,
`import_game_opening`, scout whole-line import, `force=True` seeding).

**Change** — `backend/repertoire.py:536-538` (drop the destructive `elif`) and
`backend/repertoire.py:552-583` (`INSERT … ON CONFLICT(repertoire, parent_fen, move_uci) DO UPDATE`):

```diff
-            elif existing_user_move:
-                cursor.execute("""
-                DELETE FROM repertoire_moves
-                WHERE repertoire = ? AND parent_fen = ? AND is_user_move = 1
-                """, (repertoire, norm_parent))
+            # NOTE (CODE-1): when the move already prepared for this position IS the move being
+            # added, nothing is deleted. The row (and its id, notes, arrows, importance and
+            # spaced-repetition state) is updated in place by the upsert below.
@@
-        INSERT OR REPLACE INTO repertoire_moves (
+        INSERT INTO repertoire_moves (
             repertoire, parent_fen, fen, move_uci, move_san,
             is_user_move, popularity_pct, win_rate, eval_score,
             opening_name, eco, is_important, comment, arrows, study_name, created_at
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
+        ON CONFLICT(repertoire, parent_fen, move_uci) DO UPDATE SET
+            fen = excluded.fen, move_san = excluded.move_san, is_user_move = excluded.is_user_move,
+            popularity_pct = excluded.popularity_pct, win_rate = excluded.win_rate,
+            eval_score = excluded.eval_score, opening_name = excluded.opening_name, eco = excluded.eco,
+            is_important = excluded.is_important, comment = excluded.comment,
+            arrows = excluded.arrows, study_name = excluded.study_name
         """, (…
```

**Field policy (documented in the new docstring, `backend/repertoire.py:447-462`):**

| Column | Policy | Why |
| --- | --- | --- |
| `fen`, `move_san`, `is_user_move`, `popularity_pct`, `win_rate`, `eval_score`, `opening_name`, `eco` | **always refreshed** | derived from the position / request; recomputing is idempotent |
| `is_important`, `comment`, `arrows` | **preserved when the caller omits the argument**; an explicitly supplied value wins (including an explicit `""`, so the UI can still clear a note) | user-authored; a non-empty note must never be dropped *silently*. The pre-existing `existing_meta` merge at `:547-550` already implemented "explicit wins, else keep" — the bug was that `existing_meta` was read *after* the delete |
| `study_name` | preserved when absent/blank (unchanged semantics: a blank `study_name` can never clear a study) | a study is a user grouping, not a derived value |
| `times_practiced`, `times_correct`, `last_practiced`, `next_review`, `interval_days`, `id`, `created_at` | **never written by this function** | spaced-repetition state is owned by `evaluate_trainer_move` only |

**One-user-move-per-position invariant:** unchanged. A *different* user move at the same position
still `DELETE`s the previous row and recursively prunes its now-orphaned subtree
(`:499-535`, untouched). The invariant is now only enforced where it should be: when the move
actually changes. Covered by the tail of the new test (adding `d2d4` still leaves exactly one user
move at the start position) and by the existing `tests/test_repertoire.py::test_delete_repertoire_branch`.

**Verified by** `tests/test_repertoire_write_safety.py::test_readding_existing_move_preserves_notes_arrows_importance_and_srs`
(pre-fix: `AssertionError: 105 != 103`) and `audit/scripts/FIXA-01_verify_fixes.py` `[CODE-1]`
(pre-fix 7/7 failing checks → post-fix all pass). CODE-1's own probe, before → after:

```
  re-add status: success -> returned is_important: True comment: 'my precious note' arrows: '[{"from":"e2","to":"e4"}]' study: 'Anti French'
  [after  re-add] {'id': 1, 'is_important': 1, 'comment': 'my precious note', …, 'times_practiced': 2, 'times_correct': 2, 'interval_days': 3.0}
  trainer progress after re-add: {'total_moves': 1, 'mastered_count': 1, …}
```
(pre-fix: `id=2, is_important=0, comment='', times_practiced=0, mastered_count: 0`)
`import_game_opening` twice in a row: `id=3 … times_practiced: 2` before **and** after
(pre-fix: `id 3 → 8`, all metadata zeroed).

---

### CODE-2 (high, data-loss) — `force=True` destroyed the user's prepared line, guard was TOCTOU, success path returned `None`

**Root cause.** The seed's only guard was a `COUNT(*)` read in its own connection, closed before
anything was written; the destruction itself lived in `add_repertoire_move`'s
"replace the existing user move and prune its children" logic, which has no notion of *seed* vs
*user*; and the function fell off the end of the method, so `POST /api/repertoire/seed` answered
`200 null`.

**Change** — `backend/repertoire.py:2014-2137` (whole method rewritten).

1. **Atomic guard.** The count now runs inside an explicit `BEGIN IMMEDIATE … COMMIT` on a
   dedicated connection, and the whole seed is serialised by a new process-wide
   `self._seed_lock = threading.Lock()` (`__init__`, `backend/repertoire.py:68-74`). A concurrent
   writer can no longer slip between the `COUNT` and the seed, and two concurrent seed calls can
   no longer both pass the guard.
2. **Never delete a user-prepared line.** Seeding is now two-phase:
   *Phase 1* (read-only, one connection) walks every example line and, for every **user** ply,
   looks up the move already prepared for that position:
   * *different* UCI → the whole line is **abandoned** at that point and the reason is recorded in
     the result (`conflicts`, `lines_skipped`); nothing is written for that line, so `3.Nc3` can
     never replace the user's `3.e5`;
   * *same* UCI → the write is **skipped entirely** (no metadata rewrite, no `study_name`
     re-labelling, no SRS reset — the upsert of CODE-1 would have preserved them anyway, but not
     writing at all also preserves `study_moves` membership), and the walk **continues** so the
     line's opponent replies are still contributed.
   Opponent-reply plies are additive (many replies per position is legal) and are always written.
   *Phase 2* performs only the plies proven conflict-free.
   This satisfies `AGENTS.md` §9.7 ("DO NOT seed default lines … seeding a generic line overwrites
   and prunes the user's prepared line") and §9.8 (the `force=False` guard is unchanged and now
   also reports `existing_moves`).
3. **Real result object on every path** — `{"status": "success", "message", "moves_added",
   "lines_total", "lines_skipped", "user_moves_preserved", "conflicts"}` on the seeding path,
   `{"status": "skipped", …, "existing_moves": N}` on the guard path, `{"status": "error", …}` if
   the guard transaction itself fails. `seed_default_repertoires` is now typed `-> Dict[str, Any]`.

**Verified by** `tests/test_repertoire_write_safety.py::test_force_seed_never_deletes_a_user_prepared_line_and_returns_a_result`
(pre-fix: `AssertionError: None is not true : seed_default_repertoires must return a truthy result object`)
and CODE-1's own probe `audit/scripts/CODE-01_confirm5.py`, before → after:

```
before: rows deleted by force=True: [('e2e4','anti-french prep'), ('e7e6',''), ('d2d4',''),
                                     ('d7d5',''), ('e4e5',''), ('c7c5',''), ('g1f3','')]
after : rows deleted by force=True: []
```
End-to-end through the HTTP layer (`TestClient`, temp DB, user's Anti-French pre-seeded):
```
POST /api/repertoire/seed?force=true -> 200 {'status': 'ok', 'message': 'Seeded 73 move(s) from 5/6 example line(s)',
   'user_moves_preserved': 5, 'conflicts': ['white: French Defense abandoned at … (user prepared e4e5, seed wanted b1c3)'], …}
POST /api/repertoire/seed              -> 409 {'status': 'skipped', …, 'force_required': True}
```
(pre-fix: `200 null`.)

---

### CODE-3 (high, wrong-result) — `LIMIT 1` with no `ORDER BY` mutated the wrong row

**Root cause.** `normalize_fen` keeps the 4th (en-passant) token, so the same position can be
stored under two 4-token keys; the "one user move per position" guard uses strict 4-token equality
and therefore happily stores both, while the prepared-move lookup used a deliberately liberal
`OR parent_fen = <3-token> OR parent_fen LIKE '<3-token> %'` predicate with a bare `LIMIT 1`. SQLite
answers with whichever row comes first by rowid, and the `LEFT JOIN study_moves` multiplies a row
that belongs to several studies. The trainer then validated the wrong move and, on the not-match
path (`repertoire.py:2585-2608`), committed `times_practiced = times_practiced + 1` **on that other
position's row**.

**Change** — `backend/repertoire.py:2737-2753`: the lookup is now deterministic.

```diff
-            WHERE rm.repertoire = ? AND (rm.parent_fen = ? OR rm.parent_fen = ? OR rm.parent_fen LIKE ?) AND rm.is_user_move = 1
-            LIMIT 1
-            """, (repertoire, norm_fen, norm_3, f"{norm_3} %"))
+            WHERE rm.repertoire = ? AND (rm.parent_fen = ? OR rm.parent_fen = ? OR rm.parent_fen LIKE ?) AND rm.is_user_move = 1
+            ORDER BY (rm.parent_fen = ?) DESC, rm.is_important DESC, rm.id ASC
+            LIMIT 1
+            """, (repertoire, norm_fen, norm_3, f"{norm_3} %", norm_fen))
```

Tie-break order: **exact 4-token parent first** (so the fuzzy 3-token/`LIKE` branch can never
shadow a real match), then the most important move, then the lowest `id` (so a row duplicated by
the `study_moves` join always resolves to the same row). The 3-token/`LIKE` fallback is kept, not
removed, because the trainer graph is keyed by the 3-token form and some callers hand in a FEN
whose 4th token differs from the stored one — removing it would turn "wrong answer" into "not in
your tree".

**Verified by** `tests/test_repertoire_write_safety.py::test_ambiguous_prepared_move_lookup_prefers_the_exact_parent`.
Pre-fix output:
```
AssertionError: False is not true : exact-parent prepared move was not honoured:
 {'correct': False, 'prepared_id': 1, 'expected_san': 'Nc3', …}
AssertionError: Lists differ: right row 2 → left row 0     (the mistake incremented the other position)
```
Post-fix: `correct: True`, `prepared_id` == the exact row, and after a deliberate mistake the
correct row is at `times_practiced=2` while the other position stays at `0`.

Note: `audit/scripts/CODE-01_confirm4.py` prints **no** delta — it re-executes the *old SQL string*
that is hard-coded in the probe, not the live query. The live behaviour is covered by the new test.

---

### CODE-4 (high, wrong-result) — a black repertoire was scored with the white player's stats

**Root cause.** Both `get_candidate_moves_2000(pos_fen)` call sites in `get_repertoire_tree` dropped
the colour, so the default `color="white"` was always used, and `get_candidate_moves_2000`'s step 5
builds `my_stats` from `get_user_position_stats(color)`.

**Change** — `backend/repertoire.py:1370-1381`: `color=repertoire` is now threaded through
(the endpoint at `api.py:809-817` already receives `color` and forwards it to
`get_repertoire_tree`).

**Verified by** `tests/test_repertoire_write_safety.py::test_black_repertoire_tree_uses_the_black_players_personal_stats`,
which spies on both `get_candidate_moves_2000` and `get_user_position_stats` while a black
repertoire is walked (with a real white game and a real black game in the fixture so the two
indices genuinely differ). Pre-fix:
`AssertionError: Lists differ: ['white'] != ['black']` for both spies. Post-fix: `['black']` for both.

---

### CODE-5 (high, perf) — the explorer was fetched twice per opponent position

**Root cause.** Two independent loops over `opp_positions`; the first one's `cand_data` was thrown
away and the whole (HTTP + up to 8 synchronous Stockfish `depth=10` analyses) lookup was paid again
for the coverage metric. `prepared_replies`/`prepared` were the same list computed twice.

**Change** — `backend/repertoire.py:1370-1399`: the coverage accumulation was folded into the single
gap loop and the result is memoised per position in a local `cand_cache`, so each distinct
opponent position costs exactly one `get_candidate_moves_2000` call.

**Verified by** `tests/test_repertoire_write_safety.py::test_explorer_is_fetched_once_per_opponent_position`
(counts the calls, asserts no FEN is requested twice and that the set of FENs equals the set of
opponent positions the function itself computes). Pre-fix:
`AssertionError: 4 != 2 : the explorer was fetched more than once for the same position`.
`audit/scripts/CODE-01_confirm2.py` section A: `repertoire rows: 6 -> candidate-moves calls: 6` →
**`… calls: 3`** (3 positions × 2 loops → 3).

---

### CODE-6 (high, perf + data-loss) — `get_game_gaps` wrote from a GET and buried failures as `0`

**Root cause.** The read endpoint lazily back-filled `blunders.opening_games`, opening a fresh
connection **and committing** for every row, doing one live explorer request per FEN, and
persisting whatever `get_explorer_game_count` returned — which is `0` for *every* failure mode
(429/timeout/exception) and is indistinguishable from a genuinely unpopular position. After one
blip, `opening_games IS NULL` was never true again, so the back-fill could not be re-triggered and
the blunder stayed filtered out of `/api/repertoire/game_gaps` forever.

**Change** — `backend/repertoire.py:1419-1483`. The endpoint now performs **no writes at all**:

* the never-looked-up positions are resolved **in memory only** into `row_opening_games`, using one
  connection, one FEN→count map, and a hard cap of `max_explorer_lookups` (default **25**) distinct
  FENs per request so a GET cannot be turned into a multi-minute request;
* the SQL predicate now also selects `b.opening_games IS NULL` rows and the `min_games` cut-off is
  applied in Python against the resolved value, so a row whose count is only known in memory is
  still reported and ranked;
* rows past the cap stay unresolved and are simply retried on the next request.

Supporting change — `get_explorer_game_count` (`backend/repertoire.py:175-221`) now memoises its
answer in-process, because `explorer_cache` is only ever *read* by it: a positive count for a day,
a `0` for only **5 minutes** (so a timeout can never become a permanent answer). Nothing is written
to any table from a GET. This is now documented in `AGENTS.md` §4.B.4.

**Verified by** `tests/test_repertoire_write_safety.py::test_get_game_gaps_performs_no_writes_and_persists_no_sentinel`,
which installs a `sqlite3.Connection.set_trace_callback` on every connection the module opens and
asserts that **zero** `INSERT/UPDATE/DELETE/REPLACE/CREATE/DROP/ALTER` statements were executed, and
that the two fixture blunders still hold `opening_games IS NULL` afterwards. Pre-fix:
```
AssertionError: Lists differ: ['UPDATE blunders SET opening_games = 0 WHERE id = 900001',
                                'UPDATE blunders SET opening_games = 0 WHERE id = 900002'] != []
```
`audit/scripts/CODE-01_confirm3.py` section H: `25 NULL rows -> get_db() called 27 times` →
**`… called 1 times`**.

---

### CODE-17 (medium, data-loss) — `delete_study(delete_moves=True)` deleted by a free-text column

**Root cause.** Study membership is modelled twice (`repertoire_moves.study_name` denormalised copy +
the `study_moves` join, and `PRAGMA foreign_keys` is OFF so the declared `ON DELETE CASCADE` is
inert). The delete path used the weaker of the two: `DELETE FROM repertoire_moves WHERE study_name = ?`
destroyed a move that another study still claimed, missed moves linked only through `study_moves`,
and had **no** child cascade, orphaning every continuation.

**Change** — `backend/repertoire.py:835-923` (`delete_study` rewritten) + one new helper
`_prune_orphaned_children` (`backend/repertoire.py:672-724`) extracted from
`delete_repertoire_branch`, which now calls it too:

* membership is read from `study_moves` (the real relation);
* a move is deleted **only** when it is exclusively this study's — i.e. no other `study_moves` row
  references it *and* its `study_name` column is empty or equals this study — so a shared move is
  only unlinked, never destroyed;
* the children of a deleted move are pruned with the same helper `delete_repertoire_branch` uses
  (so `study_moves` links go with them), grouped by repertoire, inside the caller's transaction;
* a surviving move whose `study_name` still names the deleted study is **re-pointed** at another
  study that still claims it, or cleared — no dangling study reference (this also closes
  CODE-16's escalation variant 2);
* the response gains `deleted_moves` / `kept_moves` so the caller can see what happened.

**Verified by** `tests/test_repertoire_write_safety.py::test_delete_study_keeps_moves_shared_with_another_study`
(pre-fix: `AssertionError: Lists differ: ['e7e5'] != ['e2e4', 'e7e5']` — the shared move was
destroyed) and `::test_delete_study_prunes_the_children_of_the_moves_it_owns`
(pre-fix: `KeyError: 'deleted_moves'`; and pre-fix leaves no `study_moves` behind only because the
name-based delete happened to remove the children too, without the reachability guard).

---

## 2. Tests I changed, and why (LOUD)

`tests/test_studies.py` — two tests were asserting the *data-loss* behaviour this patch removes.
Neither was weakened: each now states the precondition it was silently getting from the old wipe.

1. **`test_multi_study_trainer_mixing`** — it force-seeded and then assumed the seed owned the root
   position. In the shared-suite state, `tests/test_scout.py` leaves `1.d4` (Queen's Gambit Declined,
   `is_important=1`) at the root of the white repertoire, so with the non-destructive seed the
   Italian line is *abandoned* and `evaluate_trainer_move(START, "e2e4")` is correctly "not in your
   repertoire" (`AssertionError: False is not true` at the pre-existing `assertTrue(res_italian["correct"])`).
   Fix: the test now prepares `1.e4` itself, which is what it was implicitly assuming.
2. **`test_trainer_drill_from_move_one`** — the drill scheduler serves the *frontier* (the deepest
   untrained move). The test only ever saw a move-one drill because `test_study_crud` re-added
   `1.e4`, and pre-CODE-1 that delete+re-insert silently reset the move's SRS counters to 0. With
   the counters preserved, the drill legitimately moves deeper. Fix: the test now resets those
   counters explicitly (`UPDATE repertoire_moves SET times_practiced = 0, … WHERE parent_fen = ? AND
   move_uci = 'e2e4'`) and asserts the same three things as before. The test is now *more*
   deterministic than before.

Both edits are commented in place with `AUDIT FIXER-A / CODE-1` / `CODE-2` so the reason travels with
the code. No assertion was removed or loosened.

---

## 3. Before / after evidence

### New regression tests — post-fix

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_repertoire_write_safety -v
test_ambiguous_prepared_move_lookup_prefers_the_exact_parent ... ok
test_delete_study_keeps_moves_shared_with_another_study ... ok
test_delete_study_prunes_the_children_of_the_moves_it_owns ... ok
test_explorer_is_fetched_once_per_opponent_position ... ok
test_force_seed_never_deletes_a_user_prepared_line_and_returns_a_result ... ok
test_get_game_gaps_performs_no_writes_and_persists_no_sentinel ... ok
test_readding_existing_move_preserves_notes_arrows_importance_and_srs ... ok
test_black_repertoire_tree_uses_the_black_players_personal_stats ... ok
----------------------------------------------------------------------
Ran 8 tests in 2.193s

OK
```

### The same tests against the PRE-FIX module — all 8 fail

`audit/scripts/FIXA-01_prefix_runner.py` loads `/tmp/opencode/fixera/repertoire_prefix.py` (a byte
copy of `backend/repertoire.py` taken before the first edit) as `backend.repertoire`, *then* imports
`tests.test_setup`, so the temp-DB contract is unchanged:

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXA-01_prefix_runner.py tests.test_repertoire_write_safety
test_ambiguous_prepared_move_lookup_prefers_the_exact_parent ... FAIL
test_black_repertoire_tree_uses_the_black_players_personal_stats ... FAIL
test_delete_study_keeps_moves_shared_with_another_study ... FAIL
test_delete_study_prunes_the_children_of_the_moves_it_owns ... ERROR
test_explorer_is_fetched_once_per_opponent_position ... FAIL
test_force_seed_never_deletes_a_user_prepared_line_and_returns_a_result ... FAIL
test_get_game_gaps_performs_no_writes_and_persists_no_sentinel ... FAIL
test_readding_existing_move_preserves_notes_arrows_importance_and_srs ... FAIL
AssertionError: False is not true : exact-parent prepared move was not honoured: {'correct': False, 'prepared_id': 1, 'expected_san': 'Nc3', …}
AssertionError: Lists differ: ['white'] != ['black']
AssertionError: Lists differ: ['e7e5'] != ['e2e4', 'e7e5']
AssertionError: 4 != 2 : the explorer was fetched more than once for the same position: […]
AssertionError: None is not true : seed_default_repertoires must return a truthy result object
AssertionError: Lists differ: ['UPDATE blunders SET opening_games = 0 WHERE id = 900001', 'UPDATE blunders SET opening_games = 0 WHERE id = 900002'] != []
AssertionError: 105 != 103
Ran 8 tests in 1.580s

FAILED (failures=7, errors=1)
pre-fix module: /tmp/opencode/fixera/repertoire_prefix.py
pre-fix failures: 7 errors: 1
```

### Consolidated probe — 20 failing checks before, 0 after

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXA-01_verify_fixes.py
### running against the CURRENT backend/repertoire.py ###
[CODE-1] … all PASS   [CODE-2] … all PASS   [CODE-3] … all PASS
[CODE-4 + CODE-5] … all PASS   [CODE-6] … all PASS   [CODE-17] … all PASS
ALL CHECKS PASSED  (0 failing check(s))

$ FIXA_PREFIX_COPY=/tmp/opencode/fixera/repertoire_prefix.py … audit/scripts/FIXA-01_verify_fixes.py
### running against PRE-FIX module ###
FAILURES  (20 failing check(s))
```

### CODE-1's own probes, re-run before → after

| probe | before | after |
| --- | --- | --- |
| `CODE-01_confirm.py` §1 re-add | `id=2 … times_practiced: 0`, `mastered_count: 0` | `id=1 … times_practiced: 2`, `mastered_count: 1` |
| `CODE-01_confirm.py` §2 re-import | `id 3 → 8`, metadata zeroed | `id=3`, metadata + SRS intact |
| `CODE-01_confirm2.py` A | `candidate-moves calls: 6` | `candidate-moves calls: 3` |
| `CODE-01_confirm2.py` E | `seeding path returns: None` | `{'status': 'success', 'moves_added': 82, …}` |
| `CODE-01_confirm3.py` H | `get_db() called 27 times` | `get_db() called 1 times` |
| `CODE-01_confirm3.py` K | 3× user-move `DELETE` | 2× |
| `CODE-01_confirm5.py` | `rows deleted by force=True: [7 user rows]` | `rows deleted by force=True: []` |
| `CODE-01_confirm4.py` | unchanged — the probe re-runs the *old* hard-coded SQL string | (live behaviour is covered by the new test) |

`CODE-01_confirm2.py` sections B/C still print their original *static* narrative text ("the `color`
argument is never forwarded") — the probe's prose, not a live measurement; the live assertions are
the new tests.

### Full suite

```
$ /home/robin/miniconda3/bin/python3 -m unittest discover tests
Ran 426 tests in 23.245s

FAILED (failures=2)
FAIL: test_node_contract_harness (test_scout_frontend_contracts.TestScoutFrontendContractsUnderNode.test_node_contract_harness)
FAIL: test_import_reports_imported_count_and_errors (test_scout_frontend_contracts.TestScoutImportDeleteContracts.test_import_reports_imported_count_and_errors)
```
The suite is shared and other fixers were adding tests/doc updates while this patch landed, so the
count moved from the 104 I started with (an intermediate run also showed 3 failures, one of them
`test_docs_contract.test_every_path_in_the_file_map_exists` while the AGENTS.md file map was being
edited mid-run; it has since gone green). The **2 remaining failures are not in my file and are not
caused by this patch** — they fail *identically* with the pre-fix module:

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXA-01_prefix_runner.py tests.test_scout_frontend_contracts
FAIL: test_node_contract_harness (tests.test_scout_frontend_contracts.TestScoutFrontendContractsUnderNode.test_node_contract_harness)
FAIL: test_import_reports_imported_count_and_errors (tests.test_scout_frontend_contracts.TestScoutImportDeleteContracts.test_import_reports_imported_count_and_errors)
Ran 38 tests in 2.651s

FAILED (failures=2)
```
Both assert `frontend/scout.js` source strings (owner: the frontend/scout contract fixer).

Every module that exercises `backend/repertoire.py` is green:

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_repertoire_write_safety \
    tests.test_repertoire tests.test_studies tests.test_scout tests.test_pedagogical_trainer \
    tests.test_user_workflows tests.test_user_behavior_probe \
    tests.test_trainer_hint_and_line_completion tests.test_api tests.test_api_contract \
    tests.test_isolation_guard
Ran 119 tests in 10.718s

OK
```

---

## 4. Full diff

```diff
--- a/backend/repertoire.py
+++ b/backend/repertoire.py
@@ -4,6 +4,7 @@
 import sqlite3
+import threading
 import requests
@@ -67,6 +68,8 @@ class RepertoireManager:
         self._user_stats_cache = {}
         self._user_stats_time = 0
+        self._explorer_count_memo = {}
+        self._seed_lock = threading.Lock()
@@ -170,7 +173,14 @@     def get_explorer_game_count(self, fen, ratings="2000,2200,2500") -> int:
-        """Fast lookup of total 2000+ games played in a position, checking cache first."""
+        """Fast lookup of total 2000+ games played in a position, checking cache first.
+
+        Audit CODE-6: `explorer_cache` is only ever *read* here (the writer is
+        `get_candidate_moves_2000`), so an uncached FEN used to cost a live HTTP request on every
+        single call. Answers are therefore memoised in-process: a positive count for a day, and a
+        `0` for only 5 minutes - a `0` is indistinguishable from a timeout/429 and must never turn a
+        transient network failure into a permanent answer.
+        """
@@ -184,17 +194,28 @@
+        memo_key = (norm_fen, ratings)
+        memo = self._explorer_count_memo.get(memo_key)
+        if memo:
+            age = time.time() - memo[1]
+            if age < (86400 if memo[0] > 0 else 300):
+                return memo[0]
 
         url = "https://explorer.lichess.org/lichess"
         params = {"fen": fen, "ratings": ratings, "speeds": "blitz,rapid,classical"}
+        total_g = 0
         try:
             resp = requests.get(url, params=params, headers=dict(self.lichess_client.headers), timeout=6)
             if resp.status_code == 200:
                 data = resp.json()
                 total_g = data.get("white", 0) + data.get("draws", 0) + data.get("black", 0)
-                return total_g
         except Exception:
             pass
-        return 0
+
+        if len(self._explorer_count_memo) > 512:
+            self._explorer_count_memo.clear()
+        self._explorer_count_memo[memo_key] = (int(total_g), time.time())
+        return total_g
@@ -423,7 +444,22 @@     def add_repertoire_move(…) -> Dict:
-        """Adds a move to the repertoire tree. If user's turn, replaces any existing user move."""
+        """Adds a move to the repertoire tree. If user's turn, replaces any *different* existing user move.
+
+        Re-adding a move that is ALREADY in the tree is an idempotent UPSERT (audit CODE-1), never a
+        delete-then-reinsert. Field policy for the upsert:
+          * Refreshed unconditionally (derived data, safe to recompute): ``fen``, ``move_san``,
+            ``is_user_move``, ``popularity_pct``, ``win_rate``, ``eval_score``, ``opening_name``, ``eco``.
+          * User-authored, preserved when the caller does not send them: ``is_important``,
+            ``comment``, ``arrows``, ``study_name``. An explicitly supplied value (including an
+            explicit empty string) still wins, so the UI can clear a note on purpose.
+          * Never written by this function: the spaced-repetition state (``times_practiced``,
+            ``times_correct``, ``last_practiced``, ``next_review``, ``interval_days``), the row
+            ``id`` and ``created_at``.
+        """
@@ -518,11 +554,9 @@             (inside `if is_user_move:`)
-            elif existing_user_move:
-                cursor.execute("""
-                DELETE FROM repertoire_moves
-                WHERE repertoire = ? AND parent_fen = ? AND is_user_move = 1
-                """, (repertoire, norm_parent))
+            # NOTE (CODE-1): when the move already prepared for this position IS the move being
+            # added, nothing is deleted. The row (and its id, notes, arrows, importance and
+            # spaced-repetition state) is updated in place by the upsert below.
@@ -537,12 +571,30 @@
-        INSERT OR REPLACE INTO repertoire_moves (
+        # CODE-1: upsert instead of INSERT OR REPLACE. …
+        INSERT INTO repertoire_moves (
             repertoire, parent_fen, fen, move_uci, move_san,
             is_user_move, popularity_pct, win_rate, eval_score,
             opening_name, eco, is_important, comment, arrows, study_name, created_at
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
+        ON CONFLICT(repertoire, parent_fen, move_uci) DO UPDATE SET
+            fen = excluded.fen, move_san = excluded.move_san, is_user_move = excluded.is_user_move,
+            popularity_pct = excluded.popularity_pct, win_rate = excluded.win_rate,
+            eval_score = excluded.eval_score, opening_name = excluded.opening_name, eco = excluded.eco,
+            is_important = excluded.is_important, comment = excluded.comment,
+            arrows = excluded.arrows, study_name = excluded.study_name
         """, (
@@ -613,9 +665,18 @@     def delete_repertoire_branch(…):
         # Recursively delete children
+        self._prune_orphaned_children(cursor, repertoire, [child_fen])
+
+        conn.commit()
+        conn.close()
+
+    def _prune_orphaned_children(self, cursor, repertoire: str, start_fens: List[str]):
+        """Recursively deletes the given positions' children while they are no longer reachable from any
+        remaining move of the repertoire. Shared with `delete_study` (audit CODE-17) so a study deletion
+        cannot leave dangling children behind. Must run inside the caller's transaction."""
         start_norm = normalize_fen(chess.STARTING_FEN)
         start_3 = " ".join(start_norm.split()[:3])
-        to_delete = [child_fen]
+        to_delete = [f for f in (start_fens or []) if f]
@@ -652,9 +713,6 @@
-        conn.commit()
-        conn.close()
-
     def clear_repertoire_database(…)
@@ -775,26 +833,91 @@     def delete_study(self, study_id, delete_moves=False):
-        """Deletes a study session, optionally deleting its unique moves."""
+        """Deletes a study session, optionally deleting the moves that belong *exclusively* to it.
+
+        Audit CODE-17: … membership is now read from `study_moves` (the real relation), shared moves
+        are kept and merely unlinked, and the children of a deleted move are pruned with the same
+        helper `delete_repertoire_branch` uses. The `study_name` column of every surviving move is
+        re-pointed at another study that still claims it (or cleared) …
+        """
             study = dict(row)
             study_name = study["name"]
+            # Moves linked to THIS study through the study_moves relation.
+            cursor.execute("SELECT sm.repertoire, sm.parent_fen, sm.move_uci FROM study_moves sm WHERE sm.study_id = ?", (study_id,))
+            linked = [(r["repertoire"], r["parent_fen"], r["move_uci"]) for r in cursor.fetchall()]
             deleted_moves = 0
             kept_moves = 0
             orphaned_fens: Dict[str, List[str]] = defaultdict(list)
             for (rep, p_fen, uci) in linked:
                 … SELECT fen, study_name … / other studies still claiming it …
-                cursor.execute("DELETE FROM repertoire_moves WHERE study_name = ? AND repertoire = ?", (study["name"], study["repertoire"]))
+                if delete_moves and move_row is not None and not other_studies and (
+                    not column_owner or column_owner == study_name):
+                    if move_row["fen"]:
+                        orphaned_fens[rep].append(move_row["fen"])
+                    cursor.execute("DELETE FROM repertoire_moves WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?", (rep, p_fen, uci))
+                    deleted_moves += 1
+                    continue
+                kept_moves += 1
+                if move_row is not None and column_owner == study_name:
+                    cursor.execute("UPDATE repertoire_moves SET study_name = ? WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?",
+                                   (other_studies[0] if other_studies else "", rep, p_fen, uci))
+            for rep, fens in orphaned_fens.items():
+                self._prune_orphaned_children(cursor, rep, fens)
             return {"status": "success", "message": …, "deleted_moves": deleted_moves, "kept_moves": kept_moves}
@@ -1244,13 +1367,27 @@     def get_repertoire_tree(…):
+        # CODE-4/CODE-5: fetch the 2000+ explorer **once per opponent position** and thread the
+        # repertoire colour through …
+        cand_cache: Dict[str, Dict] = {}
         total_opp_weight = 0.0
         covered_opp_weight = 0.0
         for pos_fen, op_name, eco in opp_positions:
-            cand_data = self.get_candidate_moves_2000(pos_fen)
+            if pos_fen not in cand_cache:
+                cand_cache[pos_fen] = self.get_candidate_moves_2000(pos_fen, color=repertoire)
+            cand_moves = cand_cache[pos_fen].get("moves", [])
             prepared_replies = [reply["move_uci"] for reply in tree_by_parent.get(pos_fen, [])]
             for opp_m in cand_moves:
                 pop_pct = float(opp_m.get("popularity_pct") or 0.0)
+                # Coverage metric (was a second, identical explorer loop)
+                total_opp_weight += pop_pct
+                if opp_m.get("uci") in prepared_replies:
+                    covered_opp_weight += pop_pct
                 if pop_pct >= gap_threshold and opp_m.get("uci") not in prepared_replies:
                     gaps.append({…})
-
-        # Calculate coverage metric
-        for pos_fen, _, _ in opp_positions:
-            cand_data = self.get_candidate_moves_2000(pos_fen)      # identical call, result #2
@@ -1292,8 +1416,25 @@     def get_game_gaps(self, color, min_games=30, max_move=None, include_patched=False):
+        include_patched: bool = False,
+        max_explorer_lookups: int = 25
+    ) -> Dict:
+        """… Audit CODE-6: this is a READ endpoint and it now performs **no writes at all**. …"""
-        missing_rows = cursor.fetchall()
-        if missing_rows:
-            conn.close() … c_conn = get_db() … UPDATE blunders SET opening_games = ? … c_conn.commit() …
+        fen_cache: Dict[str, int] = {}
+        row_opening_games: Dict[int, int] = {}
+        for r in cursor.fetchall():
+            f = r["fen_before"]
+            if f not in fen_cache:
+                if len(fen_cache) >= max(0, int(max_explorer_lookups or 0)):
+                    break
+                fen_cache[f] = self.get_explorer_game_count(f)
+            row_opening_games[r["id"]] = fen_cache[f]
-        WHERE b.color = ? AND b.eval_before >= -200 AND (b.opening_games >= ? OR ? = 0)
+        WHERE b.color = ? AND b.eval_before >= -200
+          AND (? = 0 OR b.opening_games >= ? OR b.opening_games IS NULL)
           AND (? IS NULL OR b.move_number <= ?)
+        # Apply the min_games cut-off to the in-memory counts, without writing anything back.
+        resolved_rows = []
+        for r in rows:
+            if r.get("opening_games") is None:
+                if r["id"] not in row_opening_games:
+                    continue
+                r["opening_games"] = row_opening_games[r["id"]]
+            if min_games and int(r["opening_games"] or 0) < min_games:
+                continue
+            resolved_rows.append(r)
+        rows = resolved_rows
@@ -1862,52 +2011,127 @@     def seed_default_repertoires(self, force=False):
-        conn = get_db() … SELECT COUNT(*) … conn.close()   (TOCTOU, no return on success)
+        with self._seed_lock:
+            conn = get_db()
+            try:
+                conn.execute("BEGIN IMMEDIATE")
+                count = conn.execute("SELECT COUNT(*) FROM repertoire_moves").fetchone()[0]
+                conn.commit()
+            except Exception as exc:
+                try: conn.rollback()
+                except Exception: pass
+                return {"status": "error", "message": f"Could not inspect the repertoire: {exc}"}
+            finally:
+                conn.close()
+            if count > 0 and not force:
+                return {"status": "skipped", …, "existing_moves": count}
+            … white_lines / black_lines (study name now part of the tuple) …
+            # Phase 1 (read-only): decide, for every ply of every line, whether it may be written.
+            …  existing user move with a different UCI -> abandon the line, record the conflict;
+                existing user move with the same UCI      -> leave the row completely alone, continue;
+                no user move                              -> plan the write …
+            # Phase 2 (writes): only the plies that are provably free of a user-prepared move.
+            return {"status": "success", "message": …, "moves_added": seeded, "lines_total": lines_total,
+                    "lines_skipped": lines_skipped, "user_moves_preserved": len(preserved),
+                    "conflicts": conflicts}
@@ -2510,14 +2734,23 @@     def evaluate_trainer_move(…):
-            LIMIT 1
-            """, (repertoire, norm_fen, norm_3, f"{norm_3} %"))
+            ORDER BY (rm.parent_fen = ?) DESC, rm.is_important DESC, rm.id ASC
+            LIMIT 1
+            """, (repertoire, norm_fen, norm_3, f"{norm_3} %", norm_fen))
```

(`tests/test_studies.py` diff — the two precondition edits of §2 — and the new
`tests/test_repertoire_write_safety.py` are described above; the test file is added, not modified.)

---

## 5. Residual risks / what I deliberately did not do

1. **`max_explorer_lookups = 25`** is a heuristic bound on live HTTP requests per
   `get_game_gaps` call. With more than 25 never-looked-up distinct positions, the surplus is
   reported on the *next* page load rather than in this one (nothing is lost, nothing is written).
   A better long-term design is a persisted cache write behind an explicit endpoint; that needs
   `backend/api.py`, which is not my file.
2. **Failed probes are still indistinguishable from a real `0` *within a single request*** — a
   position whose explorer call times out is treated as "below `min_games`" for that request. It is
   retried on the next request (5-minute memo) instead of being frozen into the row, which is the
   data-loss half of CODE-6; the "is it really zero?" half would need a sentinel the schema does not
   have (`blunders.opening_games` is `INTEGER DEFAULT NULL`, and I may not add a column from here).
3. **`force=True` seeding can still add opponent replies and new user moves at positions the user
   has never prepared** (that is what "seed the example repertoire" means). It can no longer
   *replace or prune* anything the user prepared. If the intent is "never write anything into a
   non-empty repertoire", the endpoint should stop honouring `force=true` — a product decision, not
   a bug fix.
4. **The 3-token/`LIKE` fuzzy predicate is still used** by `delete_repertoire_branch` (`:664`),
   `update_move_metadata` (`:869`) and `get_next_trainer_drill`'s `last_move` lookup, so the same
   en-passant ambiguity CODE-3 described can still *delete* or *re-annotate* a node of a
   near-identical position. I fixed only the trainer's prepared-move lookup, which is what the
   finding and the mandated regression test cover; the other three sites need the same
   `ORDER BY (parent_fen = ?) DESC` treatment (or the same 4-token-only equality) in a follow-up.
5. **Concurrent `add_repertoire_move` can still create two user moves for one position** (CODE-1's
   unproven suspicion 2). The upsert removes the *destructive* branch but the
   `SELECT … → DELETE … → INSERT` sequence still has no unique index on
   `(repertoire, parent_fen, is_user_move)` and no `BEGIN IMMEDIATE`; the `ON CONFLICT` clause only
   covers `(repertoire, parent_fen, move_uci)`. Fixing it properly needs a schema/index change in
   `backend/db.py`, owned by another agent.
6. **`add_repertoire_move`'s replace-path prune still leaves dangling `study_moves` rows** (its
   inline prune loop is a near-duplicate of `_prune_orphaned_children` that does not delete the
   join rows). I extracted the helper for `delete_study` but deliberately did **not** re-point the
   replace path at it, to keep this patch surgical — it inflates `get_studies`' `total_moves`
   counter and leaves a study pointing at a move that no longer exists.
7. **`seed_default_repertoires` still opens one connection per line for the plan phase** and one per
   `add_repertoire_move` call in the write phase (82 max). I did not introduce a batch writer; the
   destructive behaviour that made this worth fixing is gone.

## 6. Also noticed (not fixed)

* **CODE-11** `get_next_trainer_drill` builds five whole-graph scans purely to take `len()`, and
  `random.shuffle(unseen_studies)` makes drill selection non-reproducible — this is why
  `test_trainer_drill_from_move_one` is order-dependent at all and needed the explicit SRS reset in
  §2.2.
* **CODE-7/8/9/10/12/13/14/15/16/18** — untouched (out of scope for this dispatch).
* `import_game_opening` still opens/commits one connection per ply and reports
  `status: "success"` even when nothing was imported; `max_ply` is still unbounded from the API.
* `get_explorer_game_count`'s `ratings` parameter is still never passed by any caller, while
  `get_candidate_moves_2000` writes the cache under whatever `ratings` the client supplied
  (`api.py:800`) — the read and the write can still use different cache rows for one position.
* `delete_study` still has no `except` (a driver-level error propagates as a 500); it is now
  all-or-nothing because the deletes and the study row share one transaction.
