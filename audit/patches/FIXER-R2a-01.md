# FIXER-R2a-01 — `backend/repertoire.py`: non-destructive imports, study-membership consistency, pooled engine

- **Fixed file:** `backend/repertoire.py` (sole owned file)
- **New tests:** `tests/test_repertoire_data_integrity.py` (26 tests, one per finding/behaviour)
- **New probe helper:** `audit/scripts/FIXER-R2a-01_prefix_runner.py` (runs the new tests against the
  pre-fix module)
- **Existing tests amended (LOUD, 3 edits, see §5):** `tests/test_repertoire_write_safety.py` (1 line),
  `tests/test_api_security.py` (1 precondition), `tests/test_scout_pipeline.py` (1 `setUp`)
- **Interpreter:** `/home/robin/miniconda3/bin/python3` (base conda env).
- **Production DB:** never opened. Every probe/test imports `tests.test_setup` first; no port-38281
  request; the Stockfish binary is never started (the pool is driven with a fake UCI engine) and
  `ps -eo pid,args | grep -c "[s]tockfish-linux"` is **0** after every run.
- Diff size: `backend/repertoire.py` **+593 / −164** against the pre-fix snapshot
  (`diff -u /tmp/opencode/fixer_r2a/repertoire_prefix.py backend/repertoire.py`); 9 behavioural hunks,
  4 new helpers, 2 new module constants. Hunk-by-hunk map in §4.

---

## 0. Baseline vs. pre-fix snapshot

There is no commit of the pre-fix file (the whole audit runs on an uncommitted working tree, and
`git show HEAD:backend/repertoire.py` is a *much older* 1280-line module that predates
`import_game_opening`, `check_line_overlap`, `delete_study`, …). The pre-fix snapshot used for
fail-before/pass-after evidence is therefore the file **as FIXER-A left it** — FIXER-A's write path was
the last edit to this file before this wave — captured at
`/tmp/opencode/fixer_r2a/repertoire_prefix.py` before the first FIXER-R2a edit:

```
$ grep -c "replace_existing\|_prune_orphaned_children\|ON CONFLICT(repertoire" /tmp/opencode/fixer_r2a/repertoire_prefix.py
0   1   1        # no replace_existing, has FIXER-A's helper + upsert
$ grep -n "eng = self._get_engine()"  …/repertoire_prefix.py
332:            eng = self._get_engine()
```

---

## 1. Per-finding root cause → change → evidence

### SCOUT-02 (critical, data-loss) / SYNC-12 (medium) — a divergent import deleted the prepared move **and** its subtree

**Root cause.** The "one user move per position" rule was implemented as *delete + recursive prune*,
and it ran for **every** caller: the Scout whole-line import, `import_game_opening`, and even
`update_move_metadata`. There was no way for a caller to say "I am only adding"; a single click on
"Line → White" or "📥 Add Opening" silently destroyed a memorised line.

**Change — `backend/repertoire.py:523-682` (new `replace_existing` flag, default safe) + `:40`
(`REPLACE_PRUNE_MAX_DEPTH = 1`).**

```diff
-    def add_repertoire_move(..., study_name: Optional[str] = None) -> Dict:
+    def add_repertoire_move(..., study_name: Optional[str] = None,
+                            replace_existing: bool = False) -> Dict:

         if is_user_move:
-            cursor.execute("""SELECT fen, move_uci FROM repertoire_moves
-                              WHERE repertoire = ? AND parent_fen = ? AND is_user_move = 1""", …)
+            cursor.execute("""SELECT id, fen, move_uci, move_san, study_name FROM repertoire_moves
+                              WHERE repertoire = ? AND parent_fen = ? AND is_user_move = 1
+                              ORDER BY id ASC""", …)
             existing_user_move = cursor.fetchone()
             if existing_user_move and existing_user_move["move_uci"] != move_uci:
+                if not replace_existing:
+                    # SCOUT-02: report the conflict and touch nothing. No DELETE, no prune.
+                    conn.close()
+                    return {"status": "conflict", "code": "prepared_move_conflict",
+                            "message": (… "Nothing was changed; re-send with
+                                         replace_existing=true to confirm."),
+                            "existing_id": …, "existing_uci": …, "existing_san": …,
+                            "attempted_uci": …, "attempted_san": …,
+                            "requires_confirmation": True,
+                            "deleted_moves": 0, "pruned_moves": 0, …}
+                # Explicit consent: remove the prepared move AND its membership rows, then let
+                # the shared helper prune the children (which removes *their* membership rows).
                 studies_before = self._studies_with_membership(cursor)
                 cursor.execute("""DELETE FROM repertoire_moves
                                   WHERE repertoire = ? AND parent_fen = ? AND is_user_move = 1""", …)
-                # Recursively delete orphaned children of the replaced move
-                … 42 lines of inlined 3-token LIKE cascade, repertoire_moves only …
+                cursor.execute("""DELETE FROM study_moves
+                                   WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?""", …)
+                self._prune_orphaned_children(cursor, repertoire, [existing_user_move["fen"]],
+                                              max_depth=REPLACE_PRUNE_MAX_DEPTH)
+                self._drop_emptied_studies(cursor, studies_before)
```

* **Never delete-then-insert on conflict**: the conflicting write path now `DELETE`s only when
  `replace_existing=True`, and it deletes the *membership rows first* (BUILDER-01) and then reuses
  the shared helper.
* **Prune depth cap** (`_prune_orphaned_children(..., max_depth=1)`, `:878-935`): level 1 = the direct
  children of the replaced move; the cascade cannot walk further into transposed lines. The two
  *explicit* delete endpoints (`delete_repertoire_branch`, `delete_study(delete_moves=True)`) pass
  `max_depth=None` (unbounded) because removing the whole branch is what the user asked for.
* **Import paths inherit the safety without any change in `scout.py`**: `ContentScout.
  import_line_to_repertoire` calls `rm.add_repertoire_move(...)` with no `replace_existing`, so a
  diverging ply now comes back as `status: "conflict"` and lands in the response's `errors` list
  (which the Scout layer already surfaces) instead of deleting the line.

**Verified by** `tests/test_repertoire_data_integrity.py::TestDivergentImportIsNotDestructive::
test_divergent_import_preserves_the_prepared_move_and_its_subtree` and
`::test_divergent_import_reports_the_conflict_and_demands_explicit_consent` (pre-fix:
`AssertionError: Lists differ: ['d2d4', 'd7d5', 'c2c4', 'e7e6', 'b1c3', 'f8b4'] != ['e2e4', 'e7e6',
'd2d4', 'd7d5', 'e4e5', 'c7c5', 'g1f3']` — the whole Anti-French replaced by the video line; and
`AssertionError: 'success' != 'conflict'`).

Original repro, re-run (`audit/scripts/atk4_02_divergence_data_loss_repro.py`):

```
   imported_count  = 5
   errors          = ['A move is already prepared at this position for white: e5. Importing Nc3
                       would replace it. Nothing was changed; re-send with replace_existing=true
                       to confirm.']
  3. e5 still present?         True
  3. e5 child  (4. c5) present? True
VERDICT: no destruction observed            (pre-fix: "USER MOVE + SUBTREE DESTROYED")
```

### BUILDER-01 (critical, data-loss) — the replace-prune left `study_moves` behind, so the next boot resurrected the branch

**Root cause.** `add_repertoire_move` carried an **inlined copy** of the cascade that deleted only
`repertoire_moves`; `delete_repertoire_branch` / `delete_study` used the shared
`_prune_orphaned_children`, which deletes both tables. `init_db()`'s "restore any moves from
`study_moves` that are missing in `repertoire_moves`" therefore re-inserted the entire discarded
branch — as `is_user_move = 1`, because the migration derives the flag from the side to move.

**Change** — the replace path now (a) deletes the replaced move's `study_moves` row explicitly and
(b) prunes through the shared helper, so membership rows go with the moves. Nothing else was needed:
`_prune_orphaned_children` already deleted both tables, and the cap only limits depth, not the
cleanup.

**Verified by** `::test_replacement_prune_removes_membership_so_init_db_cannot_resurrect_the_branch`
— it runs the real `init_db()` (the boot migration) after an explicit replacement and asserts the
tree is *byte-identical* before and after, with exactly one user move at the root. Pre-fix:
`AssertionError: Lists differ: [{'study_id': 76, 'repertoire': 'white', …}] != []` (orphaned
membership) and BUILDER-01's own restart step resurrected three rows.

Original repro (`audit/scripts/atk1_b01_invariant.py`):

```
POST /api/repertoire/move -> 200 {'status': 'conflict', 'code': 'prepared_move_conflict', …}
dangling study_moves (rows whose repertoire_moves row is gone): []
--- tree[white] after restart      (init_db -> migrate_studies_and_cleanup_blunders)
USER moves now at the 3rd-move parent position: {'move_uci': 'e4e5', 'move_san': 'e5', 'is_user_move': 1}
```

### BUILDER-02 (high, data-loss) — `POST /api/repertoire/move_meta` *inserted* moves

**Root cause.** `update_move_metadata` fell through to `add_repertoire_move()` when the lookup missed,
so starring or annotating a move the user had merely played on the board created it — and, through
the destructive one-user-move rule, deleted the prepared move and its subtree.

**Change — `backend/repertoire.py:1149-1206`.** The miss is now refused, nothing is written, and the
lookup is deterministic:

```diff
-        cursor.execute("""SELECT * FROM repertoire_moves
-            WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?) AND move_uci = ?""", …)
+        cursor.execute("""SELECT * FROM repertoire_moves
+            WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?) AND move_uci = ?
+            ORDER BY (parent_fen = ?) DESC, is_important DESC, id ASC
+            LIMIT 1""", (…, norm_parent))
         row = cursor.fetchone()
         if not row:
             conn.close()
-            # If move doesn't exist yet in repertoire, add it
-            return self.add_repertoire_move(…, is_user_move=is_user, is_important=…, comment=…, …)
+            return {"status": "error", "code": "move_not_in_tree",
+                    "message": f"{move_uci} is not in your {repertoire} repertoire at that position, "
+                               f"so no metadata was changed. Add the move to the repertoire first.", …}
```

`api.py` maps `status == "error"` to `HTTPException(400)`, so the caller gets a **4xx** and nothing is
inserted.

**Verified by** `::TestAnnotationIsNeverAStructuralWrite::test_move_meta_on_a_move_that_is_not_in_the_tree_refuses_and_writes_nothing`
(pre-fix: `AssertionError: 200 not greater than or equal to 400`) and
`::test_move_meta_still_updates_an_existing_move` (the happy path is unchanged).
Original repro `audit/scripts/atk1_b04_silent_writes.py` part A now prints
`-> 400 {'status': None, …}` and the prepared line (`2.Nf3 3.Bb5 4.Ba4`) is intact.

### BUILDER-03 (high) / SYNC-12 — `import_game_opening` ignored `games.user_color` and overwrote prepared lines

**Root cause.** `is_user_turn` was computed from the *requested* repertoire colour and the ply parity;
`games.user_color` was selected and never used, so a game the user played as **Black** installed the
*opponent's* moves as the user's prepared White moves.

**Change — `backend/repertoire.py:2077-2262` (`_position_is_reachable` + rewritten
`import_game_opening`).**

* the stored `user_color` is now mandatory and must equal the requested repertoire, otherwise
  `ValueError` (the API turns that into 400) and **nothing** is written;
* `is_user_turn` is derived from the side to move of the game the user actually played
  (`(board.turn == chess.WHITE) == (repertoire == "white")`) instead of the ply index;
* every ply is added with `replace_existing=False`; the first conflict is recorded and the walk
  **stops** (the remaining plies hang off a position the repertoire does not have);
* the response now states what happened: `status` ∈ `success | conflict | incomplete | error`, plus
  `conflicts[]`, `abandoned_at_ply`, `stopped_at_ply`, `skipped_unreachable[]`, `user_color`;
* `_position_is_reachable()` refuses to graft onto a position nothing in the tree leads to
  (SCOUT-11(5)).

**Verified by** `::TestGameOpeningImport::test_a_black_game_cannot_be_imported_into_the_white_repertoire`
(pre-fix: `AssertionError: 200 not greater than or equal to 400`),
`::test_importing_the_right_colour_never_replaces_a_prepared_line` (`status == "conflict"`,
`abandoned_at_ply == 5`), `::test_import_of_the_users_own_continuation_still_works` (7/7 plies
imported, no conflict), `::test_a_game_with_no_recorded_colour_is_refused`,
`::test_import_refuses_to_graft_onto_an_unreachable_position` and
`::test_the_reachability_guard_accepts_the_starting_position_and_stored_positions`.
Original repro `audit/scripts/atk1_b04_silent_writes.py` parts B/C:

```
POST /api/repertoire/import_game_opening -> 200 {'status': 'conflict', … 'abandoned_at_ply': 3,
   'conflicts': [{'ply': 3, 'san': 'Nc3', 'existing_san': 'Nf3', …}]}
dangling study_moves after the import: 0
--- tree[white] after restart   -> unchanged, 2.Nf3 / 3.Bb5 / 4.Ba4 intact
```

### SCOUT-03 (high, data-loss) — overlapping import hijacked `study_name`; dangling `study_moves`; ghost studies

**Root cause.** `study_name` was written straight from the caller on every upsert, so re-importing a
line under a new study *re-labelled* moves the user had curated; `study_moves` was write-only
(`INSERT OR IGNORE`, never reconciled); and a study that lost its last move stayed in the dropdown
counting moves it did not own.

**Change — `backend/repertoire.py:691-786` and `:839-876`.**

* the *membership* is read from `study_moves` (`member_studies`) and is authoritative; `study_name`
  is a **display cache** that a different study name can no longer overwrite:

```diff
+        cursor.execute("""SELECT s.name FROM study_moves sm JOIN repertoire_studies s ON s.id = sm.study_id
+                           WHERE sm.repertoire = ? AND sm.parent_fen = ? AND sm.move_uci = ?""", …)
+        member_studies = sorted(…)
+        cached_study = (existing_meta["study_name"] if existing_meta else "") or ""
+        requested_study = (study_name or "").strip()
-        final_study = (study_name.strip() if study_name and study_name.strip() else (existing_meta["study_name"] …))
+        if requested_study:
+            if not cached_study or cached_study == requested_study or not member_studies:
+                final_study = requested_study
+            else:
+                final_study = cached_study          # never let a second study hijack the label
+        else:
+            final_study = cached_study
-        if final_study:                                  # <- keyed off the display cache
+        link_study = requested_study or final_study        # <- keyed off the REQUEST
+        if link_study:                                    #    so a second study is ADDED
```

* `_studies_with_membership()` (`:839`) / `_drop_emptied_studies()` (`:851`): a study that *had* moves
  and lost its last one (through `delete_repertoire_branch` or the explicit replace) is deleted; a
  study the user created and has not filled yet is untouched.
* `delete_repertoire_branch` and the replace path both call them, so import **and** delete keep the
  two tables consistent, which is what FIXER-A's `delete_study` (exclusive-move delete + survivor
  re-point) assumes.

**Verified by** `::TestStudyMembershipIntegrity` (4 tests): the label is not hijacked and the move
belongs to **both** studies; no dangling membership after a branch delete; an emptied study does not
survive; a deliberately empty study does. Pre-fix: `AssertionError: 'Scouted Video' != 'Anti French
Defense'`. Original repro `audit/scripts/atk4_03_study_hijack_repro.py`: every `study_moves` link now
reports `exists=True`, no `study_name` was rewritten, and `'Anti French'` keeps `total_moves=5`.

### BUILDER-05 (high, race) / ABUSE-06 (high, resource) — one unsynchronised `SimpleEngine` driven from many threads

**Root cause.** `_get_engine()` handed out a single process-wide `SimpleEngine`, and
`get_candidate_moves_2000` drove it with a blocking `analyse()` from whatever anyio worker thread
served the request. `SimpleEngine` has no internal mutual exclusion, and `_get_engine()` never
health-checked an already-created engine, so one interleaving wedged the protocol for the process
lifetime. `RepertoireManager.get_instance()` was also an unsynchronised check-then-set.

**Change — `backend/repertoire.py:16-24, 88-140, 370-372, 462-522` (the pool lease).**

```diff
+from backend.evaluator import EngineBusy, EngineUnavailable, get_sync_engine_pool
-    _instance = None
+    _instance = None
+    _instance_lock = threading.Lock()
         if cls._instance is None:
-            cls._instance = cls()
+            with cls._instance_lock:
+                if cls._instance is None:
+                    cls._instance = cls()
-    def _get_engine(self):                      # removed entirely
-        if self._engine is None and os.path.exists(STOCKFISH_PATH): …
-            self._engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) …
+    @staticmethod
+    def _engine_pool():
+        return get_sync_engine_pool(STOCKFISH_PATH)
             # 4. Fast Stockfish Evaluation of top moves (up to 8 moves)
-            eng = self._get_engine()
-            if eng:
-                for cand in candidate_moves[:8]: … eng.analyse(test_b, Limit(depth=10)) …
+            self._evaluate_candidate_moves(board, candidate_moves)
```

`_evaluate_candidate_moves()` takes **one** lease for the whole batch, `pool.release()`es it in a
`finally`, `pool.discard()`es it if anything escapes the per-candidate guard, and turns
`EngineBusy` / `EngineUnavailable` / `FileNotFoundError` into "no eval for this row" instead of an
exception. The manager no longer owns an engine, so `close()` now only drops caches (the pool owns
the processes and the `atexit` net). This is the *same* mechanism `backend/analyzer.py` uses — no
second lifecycle was invented.

**Verified by** `::TestEngineIsLeasedNotShared` (3 tests, real `SyncEnginePool` + fake UCI engine that
records per-engine concurrency): `test_concurrent_explorer_calls_never_drive_one_engine_from_two_threads`
runs two threads over two uncached positions and asserts every engine's `max_in_flight == 1`.
Pre-fix, with the identical harness: **`AssertionError: 2 != 1 : one Stockfish process was driven by
two threads at once`** — the exact ATK-1 signal. `test_the_explorer_leases_from_the_shared_bounded_pool_and_hands_the_slot_back`
asserts `pool.in_use == 0` after the request (no leaked slot) and `pool.spawned >= 1`;
`test_a_missing_engine_degrades_the_candidate_table_instead_of_raising` patches the pool to raise
`EngineBusy` and asserts the candidate table is still returned with `eval: None`.

> The original repro `audit/scripts/atk1_b05_engine_race.py` can no longer run as written — it calls
> `rm._get_engine()`, which this patch removes. That `AttributeError` is itself evidence that the
> manager no longer owns an engine; the equivalent measurement is the new test above.

### SCOUT-11(4)/(5) (medium) — full-tree re-read per line, no batch ordering, unreachable grafting

**Change — `backend/repertoire.py:1275-1345` (cached index) and `:1551-1650` (batch order +
`batch_conflicts`).**

* `_load_overlap_index(repertoire)` builds `{parent_fen: [...]}` / `{3-token: [...]}` once and reuses
  it for `OVERLAP_INDEX_TTL_SECONDS` (5 s); **every** write path calls `_invalidate_overlap_index()`,
  so the TTL is only a net against a writer that bypasses this class.
* `check_lines_overlap` shares that one index for the whole batch, returns results in **DFS batch
  order** (the line that starts a variation comes before the lines that continue it) and adds
  `batch_conflicts` per line: the other lines *in the same batch* that prepare a different user move
  at the same position. A one-move-per-position repertoire cannot hold all of them, so a bulk import
  that ignores the field will still destroy the siblings it just wrote — the field is what lets the
  Scout layer order and filter its "📥 All" batch (scout.py owns the caller).
* `import_game_opening` refuses to write onto a position nothing in the tree reaches
  (`_position_is_reachable`, `:2077`).

**Verified by** `::TestOverlapCheck` (3 tests): one index build for an 8-line batch (pre-fix:
`TypeError`/8 builds), DFS order `['parent', 'child']` (pre-fix: `['child', 'parent']`), both sides of
a sibling conflict reported, and the cache invalidated by a write.

### FIXER-A escalation 4 — the ambiguous 3-token/`LIKE` lookups (CODE-3 class)

All three sites FIXER-A left open now carry the same deterministic tie-breaker
(`exact 4-token match first, then most important, then lowest id`):

| site | line | change |
| --- | --- | --- |
| `delete_repertoire_branch` | `:805-820` | `ORDER BY (parent_fen = ?) DESC, is_important DESC, id ASC LIMIT 1`, and the delete is now `WHERE id = ?` so the fuzzy predicate cannot fan out |
| `update_move_metadata` | `:1181-1186` | same `ORDER BY … LIMIT 1` |
| `get_next_trainer_drill.last_move` | `:3070-3079` | same, against the 4-token `norm_p4` (the old comparison used a 6-token `p_fen` against the 4-token `fen` column, so it could never match exactly) |

**Verified by** `::test_delete_branch_uses_a_deterministic_tie_breaker` (pre-fix: `AssertionError: []
!= [121]` — the delete removed the twin position's row as collateral) and
`::test_update_move_metadata_picks_the_exact_parent_position` (pre-fix: the note landed on the other
position).

---

## 2. The golden invariant, asserted after **every** operation

`tests/test_repertoire_data_integrity.py::RepertoireDataIntegrityBase.assert_integrity()` is called
after every single operation in the module (26 tests, ~60 operations) and asserts all four halves:

1. **at most one `is_user_move = 1` row per `(repertoire, parent_fen)`** (SQL `GROUP BY … HAVING n>1`);
2. **no orphaned `study_moves` row** (a `LEFT JOIN` against `repertoire_moves` finds none) — i.e.
   `init_db()` can never resurrect anything;
3. **no ghost study** (every `repertoire_studies` row owns at least one move);
4. **no side-effect deletion** — `test_the_invariant_and_no_side_effect_deletion_hold_through_a_full_sequence`
   prepares the Anti-French, then runs seven unrelated operations (refused divergent import, refused
   annotation, overlapping import with another study name, refused import of a Black game, deleting a
   branch that is not there, deleting an empty study, `seed_default_repertoires(force=True)`) and
   asserts after **each** that `1.e4`, `3.e5` and `4.f3` are still there, plus that all seven baseline
   moves survive.

`test_replacement_prune_removes_membership_so_init_db_cannot_resurrect_the_branch` additionally
re-runs the real `init_db()` and asserts the tree is byte-identical across the simulated restart.

---

## 3. Before / after evidence

### New regression tests — **before** (pre-fix module)

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-R2a-01_prefix_runner.py tests.test_repertoire_data_integrity
test_move_meta_on_a_move_that_is_not_in_the_tree_refuses_and_writes_nothing ... FAIL
test_update_move_metadata_picks_the_exact_parent_position ... FAIL
test_divergent_import_preserves_the_prepared_move_and_its_subtree ... FAIL
test_divergent_import_reports_the_conflict_and_demands_explicit_consent ... FAIL
test_explicit_consent_replaces_the_prepared_move ... ERROR
test_replacement_prune_is_depth_capped ... ERROR
test_replacement_prune_removes_membership_so_init_db_cannot_resurrect_the_branch ... ERROR
test_a_missing_engine_degrades_the_candidate_table_instead_of_raising ... ERROR
test_concurrent_explorer_calls_never_drive_one_engine_from_two_threads ... FAIL
test_the_explorer_leases_from_the_shared_bounded_pool_and_hands_the_slot_back ... FAIL
test_a_black_game_cannot_be_imported_into_the_white_repertoire ... FAIL
test_a_game_with_no_recorded_colour_is_refused ... FAIL
test_import_of_the_users_own_continuation_still_works ... ERROR
test_import_refuses_to_graft_onto_an_unreachable_position ... ERROR
test_importing_the_right_colour_never_replaces_a_prepared_line ... ERROR
test_the_reachability_guard_accepts_the_starting_position_and_stored_positions ... ERROR
test_the_invariant_and_no_side_effect_deletion_hold_through_a_full_sequence ... FAIL
test_a_written_move_invalidates_the_cached_index ... ok
test_the_batch_check_builds_the_index_once_not_once_per_line ... ERROR
test_the_batch_check_reports_sibling_conflicts_and_orders_parents_first ... FAIL
test_a_deliberately_empty_study_is_not_removed ... ERROR
test_a_study_that_loses_its_last_move_does_not_survive_as_a_ghost ... FAIL
test_delete_branch_uses_a_deterministic_tie_breaker ... FAIL
test_deleting_a_branch_leaves_no_dangling_membership_and_no_ghost_study ... ok
test_overlapping_import_does_not_hijack_the_study_a_move_is_filed_under ... FAIL
test_move_meta_still_updates_an_existing_move ... ERROR
test_import_of_the_users_own_continuation_still_works ... ERROR

AssertionError: 200 not greater than or equal to 400 : annotating a move that is not in the tree must be refused
AssertionError: 'only for the ep twin' != ''
AssertionError: Lists differ: ['d2d4', 'd7d5', 'c2c4', 'e7e6', 'b1c3', 'f8b4'] != ['e2e4', 'e7e6', 'd2d4', 'd7d5', 'e4e5', 'c7c5', 'g1f3']
AssertionError: 'success' != 'conflict'
AssertionError: 2 != 1 : one Stockfish process was driven by two threads at once - the UCI protocol desynchronises and the engine wedges (audit BUILDER-05)
AssertionError: 200 not greater than or equal to 400 : a game played as Black must not import into the White repertoire
AssertionError: Lists differ: [{'study_id': 76, 'repertoire': 'white', …}] != []      <- BUILDER-01 orphan membership
AssertionError: [] != [121]                                                          <- delete hit the twin row
AssertionError: 'Scouted Video' != 'Anti French Defense'                               <- SCOUT-03 hijack
AssertionError: Lists differ: ['child', 'parent'] != ['parent', 'child']               <- no DFS order
AssertionError: 0 not greater than or equal to 1                                      <- no pool lease
Ran 26 tests in 1.556s
FAILED (failures=11, errors=13)      ->  24 of 26 fail before the patch
```

(The two that already passed — `test_a_written_move_invalidates_the_cached_index` and
`test_deleting_a_branch_leaves_no_dangling_membership_and_no_ghost_study` — are "don't regress"
tests for behaviour that was already correct on those paths.)

### New regression tests — **after**

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_repertoire_data_integrity
Ran 26 tests in 2.714s

OK
```

### The original attackers' probes, re-run

| probe | before | after |
| --- | --- | --- |
| `atk4_02_divergence_data_loss_repro.py` (SCOUT-02) | `VERDICT: USER MOVE + SUBTREE DESTROYED` | `VERDICT: no destruction observed` + conflict in `errors` |
| `atk4_03_study_hijack_repro.py` (SCOUT-03) | `study_name` relabelled to 'Video Import'; 1 dangling link; 'Anti French' claims 5 moves, owns 0 | labels untouched; every link `exists=True`; both studies own their moves |
| `atk1_b01_invariant.py` (BUILDER-01) | replace succeeded, 3 dangling `study_moves`, **3 rows resurrected** by `init_db()` | replace refused (409-style conflict), 0 dangling links, tree identical after the simulated restart |
| `atk1_b04_silent_writes.py` A (BUILDER-02) | `move_meta` → 200, 4 moves of a study destroyed | `move_meta` → 400, tree unchanged |
| `atk1_b04_silent_writes.py` B/C (BUILDER-03/SYNC-12) | 200 `success`, 5 prepared moves replaced, resurrected on restart | 200 `status=conflict` with `conflicts[]`, nothing replaced, 0 dangling links |
| `atk1_b10_fuzz_invariants.py` (260 actions) | **147** violations: 146 × dangling `study_moves`, 1 × I1' | **1** reported violation — and that one is a **bug in the probe**: line 207 calls `check("AFTER RESTART")("I1' …", dups)` *unconditionally*, and `check()` always records. The probe's own numbers say `after restart: 30 moves, 0 positions with 2+ user moves`. Real violations: 0. |
| `fixeri_scout02_03_repro.py` (FIXER-I hand-off) | 7 rows → 0 after the conflicting import; study_name hijacked | 7 rows unchanged; `study_name` preserved, both studies linked |

### Full test suite

```
$ /home/robin/miniconda3/bin/python3 -m unittest discover tests
Ran 507 tests in 28.277s

FAILED (failures=1, errors=1)
FAIL: test_all_security_checks (test_api_security.TestApiSecurity.test_all_security_checks)
      -> check_dashboard_eco_is_deterministic: IntegrityError: UNIQUE constraint failed:
         blunders.game_id, blunders.ply, blunders.played_move
ERROR: test_preexisting_duplicate_rows_are_collapsed (test_analyzer_correctness…)
      -> sqlite3.IntegrityError: UNIQUE constraint failed: blunders.game_id, blunders.ply, blunders.played_move
```

**Both remaining failures are FIXER-N's in-flight `ux_blunders_natural_key` UNIQUE index, not this
patch.** They fail *identically* with the pre-fix module, and neither touches `repertoire.py`:

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-R2a-01_prefix_runner.py \
      tests.test_analyzer_correctness tests.test_api_security
ERROR: test_preexisting_duplicate_rows_are_collapsed
FAIL:  test_all_security_checks
Ran 29 tests … FAILED (failures=1, errors=1)
```

The two `test_scout_frontend_contracts` failures this dispatch inherited as "known" are gone (FIXER-M
fixed them). Everything that exercises `backend/repertoire.py` is green:

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_repertoire_data_integrity \
    tests.test_repertoire tests.test_repertoire_write_safety tests.test_studies tests.test_scout \
    tests.test_scout_pipeline tests.test_pedagogical_trainer tests.test_user_workflows \
    tests.test_user_behavior_probe tests.test_trainer_hint_and_line_completion tests.test_api \
    tests.test_api_contract tests.test_api_security tests.test_isolation_guard tests.test_docs_contract
Ran 206 tests in 14.926s

FAILED (failures=1)      # only the FIXER-N blunders-index check above
```

`tests/test_docs_contract.py` and `tests/test_isolation_guard.py` pass (the new test module is
`git add`ed and listed in `AGENTS.md` §3).

### Engine hygiene

`ps -eo pid,args | grep -c "[s]tockfish-linux"` = **0** before and after every run of the new tests,
the whole suite, and all probes. Nothing is leaked: the lease is released in a `finally` and
`tests/test_setup.py` closes the shared pool between modules.

---

## 4. Full diff (`backend/repertoire.py`, +593 / −164)

Reproduce with `diff -u /tmp/opencode/fixer_r2a/repertoire_prefix.py backend/repertoire.py`. The
behavioural hunks, in file order:

| lines | change |
| --- | --- |
| 16-24 | import `EngineBusy`, `EngineUnavailable`, `get_sync_engine_pool` from `backend.evaluator` |
| 33-46 | new `REPLACE_PRUNE_MAX_DEPTH = 1`, `OVERLAP_INDEX_TTL_SECONDS = 5.0` |
| 88-115 | `_instance_lock` + double-checked `get_instance()` (ABUSE-06) |
| 118-137 | `_engine_pool()`, cache-only `close()`, `_invalidate_overlap_index()` |
| 370-372 | eval loop → `self._evaluate_candidate_moves(board, candidate_moves)` |
| 462-522 | new `_evaluate_candidate_moves()` — one bounded lease per batch, `release`/`discard` in `finally`, graceful degradation |
| 523-682 | `add_repertoire_move(..., replace_existing=False)`: conflict return, consent path, capped prune through the shared helper, emptied-study cleanup |
| 691-786 | study membership is authoritative (`member_studies`); `study_name` is a display cache; the link is keyed off the *requested* study; index invalidation; `studies[]` in the response |
| 784-836 | `delete_repertoire_branch`: deterministic target row, `DELETE … WHERE id = ?`, emptied-study cleanup, index invalidation |
| 839-876 | new `_studies_with_membership()` / `_drop_emptied_studies()` |
| 878-935 | `_prune_orphaned_children(..., max_depth=None)`: BFS with depth, returns the number of removed rows |
| 1149-1206 | `update_move_metadata`: refuse a missing move (4xx), deterministic lookup, display-cache policy, `studies[]`, index invalidation |
| 1275-1345 | `check_line_overlap` → cached `_load_overlap_index()` |
| 1551-1650 | `check_lines_overlap`: shared index, DFS batch order, `batch_conflicts` |
| 2077-2262 | new `_position_is_reachable()`; rewritten `import_game_opening` (user_color, conflict stop, reachability, honest status) |
| 3070-3079 | `get_next_trainer_drill.last_move`: deterministic tie-breaker against the 4-token key |
| 2555-2565 | `seed_default_repertoires` phase 2 passes `replace_existing=False` explicitly (defence in depth: a planning bug can no longer destroy a line) |

Tests:

* **new** `tests/test_repertoire_data_integrity.py` (26 tests) — added, not modified;
* `tests/test_repertoire_write_safety.py` — 1 line (see §5.1);
* `tests/test_api_security.py` — 1 precondition added to one check (see §5.2);
* `tests/test_scout_pipeline.py` — one `setUp` added to `TestScoutImportContract` (see §5.3).

---

## 5. Existing tests I changed, and why (LOUD)

No assertion was removed, loosened or skipped.

### 5.1 `tests/test_repertoire_write_safety.py` (FIXER-A's own test) — 1 call gained the consent flag

```diff
-        replaced = rm.add_repertoire_move("white", chess.STARTING_FEN, "d2d4", is_user_move=True)
+        # AUDIT FIXER-R2a / SCOUT-02: the replacement is now *explicit*. …
+        replaced = rm.add_repertoire_move("white", chess.STARTING_FEN, "d2d4", is_user_move=True,
+                                          replace_existing=True)
         self.assertEqual(replaced["status"], "success")
```

Pre-fix this call relied on the **destructive default** to reach its assertion ("after replacing the
prepared move the position still holds exactly ONE user move, and it is the new one"). That default
is the bug SCOUT-02/BUILDER-01/BUILDER-02 are about, so the test now *states* the consent. The
assertions, and the one-user-move invariant they check, are unchanged. Without this edit the test
fails with `AssertionError: 'conflict' != 'success'`.

### 5.2 `tests/test_api_security.py::check_mutating_request_without_origin_is_allowed` — precondition stated

`POST /api/repertoire/move_meta` no longer creates a move that is not in the tree, so the check's
probe move (`1.e4` at the start position) may legitimately be absent and the endpoint answers 400.
The check's *subject* is the Origin boundary, so the probe now creates the move first — with a
**no-Origin** `POST /api/repertoire/move`, which is the very shape under test:

```diff
+    # AUDIT FIXER-R2a / BUILDER-02: … so it answers 4xx for a move that is not there. State the
+    # precondition instead of relying on the old destructive side effect …
+    seeded = client.post("/api/repertoire/move", json={… "move_uci": "e2e4", "is_user_move": True})
+    assert seeded.status_code == 200, f"no-Origin write refused: {seeded.text[:160]}"
     with _ApiLogCapture() as logs:
         response = client.post("/api/repertoire/move_meta", …)
     assert response.status_code == 200, …
```

No assertion was changed; the check is *stronger* (it now also proves the write endpoint answers 200
without an `Origin`).

### 5.3 `tests/test_scout_pipeline.py::TestScoutImportContract` — per-test isolation added

These five tests shared one repertoire tree *and* the leftovers of the previous test module, and two
of them only passed because an import silently **deleted** the prepared move the earlier test had
left behind (`test_partial_import_reports_only_the_new_moves` imported `1.e4` into a tree that
already had `1.d4` from `tests/test_scout.py` and still reported `partially_present`). With a
non-destructive import that pollution is visible, so the class now starts from an empty tree — the
same amendment pattern FIXER-A used in `test_studies.py`:

```python
    def setUp(self):
        """AUDIT FIXER-R2a: start every import-contract test from an empty repertoire tree. …"""
        conn = get_db()
        conn.execute("DELETE FROM study_moves")
        conn.execute("DELETE FROM repertoire_studies")
        conn.execute("DELETE FROM repertoire_moves")
        conn.commit()
        conn.close()
```

No assertion was changed; the same contract is now asserted from a known starting state.

---

## 6. Residual risks

1. **`POST /api/repertoire/move` now reports a conflict instead of replacing.** The Builder's
   "make this my move here" / "Address Next Blunder" flow needs the consent flag, which lives in
   `api.py:1832` (not my file). One line for the route owner:
   `rm.add_repertoire_move(..., replace_existing=payload.confirm_replace)`. Until then the UI keeps
   working and simply cannot replace a prepared move in one click — it must delete the branch first
   (`DELETE /api/repertoire/move`, which is unaffected) or the frontend must add a confirm step. This
   is the intended product change (AGENTS.md §4E wants a warning + consent), but it *is* a behaviour
   change for that button.
2. **The depth-capped replace prune leaves the discarded line's deeper rows on disk** as an
   unreachable branch (level ≥ 2). Nothing is lost, the one-user-move-per-position invariant still
   holds, and `init_db()` cannot resurrect anything; but the trainer will still offer those moves
   (`build_repertoire_tree_graph` gives disconnected user moves `ply_depth: 99`). The user can remove
   them with the explicit delete endpoint, or `CHESS_PURGE_ORPHANS=1` sweeps them
   (`backend/db.py`, FIXER-N's). Raising `REPLACE_PRUNE_MAX_DEPTH` trades this for a wider blast
   radius through transpositions.
3. **The Scout import loop still writes the plies *after* a conflict** (it lives in `scout.py`). Each
   such ply is now either additive (an opponent branch at a new position) or refused, so nothing is
   destroyed, but the response can contain a mixture of "imported" and "conflicting" plies. Making
   the Scout loop abandon the rest of the line is scout.py's call (FIXER-M) — `batch_conflicts` and the
   per-ply conflict messages are what it needs.
4. **`_position_is_reachable` is only a guard for callers that can hand in a non-starting position.**
   `import_game_opening` always walks from the start position, so for that path the guard can only
   fire if a position in the walk has no move leading to it *and* the start position was already
   bypassed. The Scout import accepts an arbitrary `fen_before` per move and is therefore the real
   reachable-grafting surface; gating it is scout.py's decision.
5. **`get_instance()` double-checked locking** relies on the GIL for the `_instance` read; it is the
   same pattern the rest of the codebase uses and is sufficient on CPython.
6. **The overlap index is per-process with a 5 s TTL.** A writer that bypasses this class (a manual
   `sqlite3` write, another process) can leave the index stale for up to 5 s; the previous code read
   the table on every call and could not go stale. Every write path *in this module* invalidates it,
   and `tests/test_repertoire_data_integrity.py::test_a_written_move_invalidates_the_cached_index`
   covers the invalidation.
7. **A busy engine pool degrades the eval column silently** (rows carry `eval: None`), which is the
   same user-visible state the desync produced. It is now bounded and self-healing rather than
   permanent, and `print()` records it server-side; a route owner could map `EngineBusy` to a 503 or
   surface it in the response.
8. **The concurrency fix does not serialise the two *pools*.** `get_sync_engine_pool()` caps
   synchronous engines at `CHESS_ENGINE_MAX_PROCS` (2) and the async SSE pool at 1, so an explorer
   burst and an eval stream can still hold 3 Stockfish processes. That is FIXER-C's design and the
   documented cap.

---

## 7. Deliberately deferred

**To R2b (external-data robustness):**

* `get_explorer_game_count` still ignores its `ratings` argument on the read path while
  `get_candidate_moves_2000` writes the cache row under whatever `ratings` the client sent
  (`api.py:800`) — one position can therefore be read and written under different cache rows;
* `get_candidate_moves_2000` still writes `explorer_cache` from a GET, and the `eval_convention`
  migration rewrites rows in place;
* `get_repertoire_tree` still pays one live explorer request per opponent position per call
  (unbounded, `max_explorer_lookups` only caps `get_game_gaps`);
* a failed probe is still indistinguishable from a real `0` *within one request* (FIXER-A §5.2);
* `import_game_opening` still parses `moves_san` with `board.parse_san` and `break`s on the first
  failure, so a `+`/`#` suffix truncates the import without an error, and `max_ply` is still
  unbounded from the API layer.

**To R2c (trainer scheduling):**

* `get_next_trainer_drill` still builds five whole-graph scans to take `len()`, and
  `random.shuffle(unseen_studies)` makes drill selection non-reproducible;
* `evaluate_trainer_move` still has two `LIMIT 1` lookups without the tie-breaker
  (`is_user_move = 1` study-filtered follow-up at `:2879-2886` and the unfiltered fallback at
  `:2889-2895`) — the same CODE-3 class, still ambiguous across en-passant twins;
* `build_repertoire_tree_graph` still treats the denormalised `study_name` as a fallback study
  (`:2372`, `:2411`) — harmless now that the column is only a display cache, but the fallback could
  name a study the move is no longer a member of;
* `get_game_gaps`'s `rep_moves = {parent_fen: move_uci}` dict would still pick an arbitrary one of
  two rows if the invariant were ever broken by a writer outside this module; a `UNIQUE(repertoire,
  parent_fen) WHERE is_user_move = 1` partial index (schema change, FIXER-N) would make it
  impossible rather than merely forbidden.

**Not my file, listed for the owners:**

* `backend/scout.py` — `import_line_to_repertoire` should stop at the first conflict and use
  `batch_conflicts`; `backend/api.py` — the `replace_existing` pass-through and a 409/4xx mapping for
  `status == "conflict"`; `frontend/repertoire_builder.js` / `frontend/scout.js` — show the conflict
  and ask before replacing, and stop showing the green "✓ Added" toast when `status` is
  `conflict`/`incomplete`; `backend/db.py` — the partial unique index above.

---

## 8. Also noticed (not fixed)

* `check_line_overlap` still resolves a SAN-only move against the walker's board rather than the
  caller's `fen_before` when both disagree (pre-existing, `_evaluate_line_against_index`).
* `add_repertoire_move` returns `studies[]` computed *before* its own membership insert, so it lists
  the new study only because `final_study` is appended explicitly — a cosmetic asymmetry.
* `delete_study` still has no `except`; a driver-level error propagates as a 500 (all-or-nothing,
  because the deletes share one transaction). FIXER-A's note, unchanged.
* `_drop_emptied_studies` skips a study that some surviving row still *labels* via `study_name`,
  which is the conservative choice: a stale label keeps a ghost alive rather than orphaning a row.
* The frontend's `POST /api/repertoire/move` call has no `replace_existing` field yet, so from the
  browser the replacement path is currently unreachable — the flag is implemented, tested and wired
  on the manager, and needs one keyword in `api.py` to be reachable.
