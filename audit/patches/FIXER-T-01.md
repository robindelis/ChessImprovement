# FIXER-T-01 — `backend/repertoire.py`: sync-path correctness, explorer boundary, trainer scheduling

- **Fixed file:** `backend/repertoire.py` (sole owned file)
- **New tests:** `tests/test_repertoire_sync_robustness.py` (37 tests, 12 classes)
- **New probe helper:** `audit/scripts/FIXER-T-01_prefix_runner.py` (runs the new tests against the
  pre-fix snapshot)
- **Existing tests amended (LOUD, 3 setups + 2 assertions, see §6):**
  `tests/test_repertoire_data_integrity.py` (2 *setups* only), `tests/test_repertoire_write_safety.py`
  (1 *setup* only), `tests/test_user_behavior_probe.py` (2 *assertions*, one of them corrected).
- **Interpreter:** `/home/robin/miniconda3/bin/python3` (base conda env).
- **Production DB:** never opened, not even read. Every probe/test imports `tests.test_setup` first;
  port 38281 is never contacted; no Lichess call leaves the process (`fetch_user_games` and
  `backend.repertoire.requests.get` are both mocked at the module boundary);
  `ps -eo pid,args | grep -c "[s]tockfish-linux"` is **0** after every run.
- **Diff size:** `backend/repertoire.py` **+1183 / −352** against the pre-fix snapshot
  (`diff -u /tmp/opencode/fixert/repertoire_prefix.py backend/repertoire.py`); 9 new module-level
  helpers/constants, 6 new methods, 4 new instance attributes. Hunk-by-hunk map in §8.

**Full suite: `Ran 575 tests … OK` (exit 0).** Before this wave the suite inherited FIXER-S's
in-flight blunder-identity failures; those are resolved and the suite is fully green.

---

## 0. The golden invariant

`tests/test_repertoire_data_integrity.py` (26 tests, ~60 operations) is **green**, and
`_assert_golden_invariant()` in the new module re-asserts the same three halves (at most one
`is_user_move = 1` row per `(repertoire, parent_fen)`; no orphaned `study_moves`; no prepared move
deleted as a side effect) after **every** write it performs — 8 places, including the failed-analysis
and rejected-record paths.

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_repertoire_data_integrity
Ran 26 tests in 3.024s
OK
```

---

## 1. Per-finding root cause → change → evidence

### SYNC-16 (HIGH) — `analyze_user_games` stamped and committed `analyzed_at` **before** analysing, one bad game 500'd the library, and the pooled engine handle leaked

**Root cause.** `analyze_user_games` wrote the game with `analyzed_at = int(time.time())`, committed,
and *then* called `analyzer.analyze_game_openings(g)`. The loop had no per-game `try/except`, and
`analyzer.close()` / `conn.close()` sat *after* the loop, so a failure (a) permanently marked the game
"already analysed" with zero blunders stored, (b) propagated out of the endpoint as a bare 500 for
the whole library, and (c) leaked the Stockfish handle from the bounded pool. `stream_analyze_user_games`
had the same stamp-before-analyse order, and its `finally` only ran if the `try` was entered.

**Change** — `backend/repertoire.py:3026-3120` (`analyze_user_games`, rewritten) and
`:3122-3290` (`stream_analyze_user_games`), mirroring the contract `POST /api/sync` already uses
(FIXER-B): *pending write → analyse → stamp on success*.

* `_upsert_game()` (`:2544-2578`) writes the game with `analyzed_at = 0` and does **not** list
  `analyzed_at` in its `ON CONFLICT DO UPDATE SET` list, so the flag is owned exclusively by the
  analysis path.
* After the analysis succeeds, and only then, `UPDATE games SET analyzed_at = ? WHERE id = ?`.
* Each game is isolated in its own `try/except` + `conn.rollback()`; failures are reported as
  `failed_games: [{"game_id", "error"}]` and the batch answers
  `status: "partial"` (or `"error"` when every game failed, so a quiet success is impossible).
* `analyzer.close()` **and** `conn.close()` are in a `finally` that covers construction too
  (`analyzer = None` / `conn = None` up front in the SSE twin), so the lease is always returned.
* SYNC-22's rollback also lives in that `finally`/outer `except`.

**Verified by** `tests/test_repertoire_sync_robustness.py::TestAnalyzeStampIsWrittenOnlyOnSuccess`
(4 tests). Pre-fix (see §5):
`test_a_failed_analysis_leaves_the_game_pending_and_the_next_sync_retries_it` **ERROR** with
`RuntimeError: engine died / database locked` escaping `analyze_user_games` at
`repertoire_prefix.py:2328`; `test_one_bad_game_never_answers_500_for_the_whole_library` **FAIL**
(`500 not greater than or equal to 200`); `test_the_pooled_engine_handle_is_released_even_when_a_game_raises`
**ERROR** (`KeyError: 'close'` — `analyzer.close()` never ran). Post-fix all four pass, and the retry
round of the first test shows the pending game being re-analysed and then stamped.

---

### SYNC-17 (HIGH) — all four writers stored a draw as SQL `NULL`, the threat engine booked it as a **loss**, an abandon was indistinguishable from a draw, and a junk `winner` token was stored verbatim

**Root cause.** `dict.get("winner", "draw")` returns the default only when the key is **absent**;
Lichess sends the key with a `null` value, so every draw became SQL `NULL` — and
`get_user_position_stats` classified anything that was not `u_color` and not the literal `"draw"` as
a **loss**. The writers also dropped `status` entirely, so an abandoned game (`winner: null`,
`status: "abandoned"`) was stored and scored exactly like a drawn one, and any unrecognised
`winner` string was persisted forever (read as a draw by the UI, as a loss by the engine, and as a
new bucket in `/api/dashboard`).

**Change.**

* New module-level normalisers:
  * `normalize_game_outcome(winner, status, user_color)` (`:213-241`) — the **writer**-side guard.
    Returns `'white' | 'black' | 'draw' | 'abandoned'`, or **`None`** for an unrecognised token
    (which the writers reject and report). `status in UNFINISHED_STATUSES` wins over `winner`, so
    abandon ≠ draw.
  * `classify_stored_result(stored, user_color)` (`:184-211`) — the **reader**-side classifier used
    by `get_user_position_stats`. Understands three generations of the column: the canonical tokens,
    the legacy user-perspective tokens `1-0` / `0-1` / `1/2-1/2` (AGENTS.md §5, and what the
    synthetic fixture stores), and SQL `NULL` (which `backend/api.render_game_outcome` has always
    rendered as a draw — the engine must agree with the reader rather than contradict it).
* `get_user_position_stats` (`:325-357`) now classifies the result **once per game** and
  `continue`s for `RESULT_ABANDONED`, so an unfinished game is not counted at all. `games == wins +
  draws + losses` still holds by construction.
* All four writers route through `_game_row_from_upstream` (`:2500-2543`), which computes the token
  and returns `None` for a rejected record; the record lands in `rejected_games[]`
  (`sync_recent_games`) / `rejected` (both SSE twins) and the rest of the batch is still written.

**Verified by** `::TestOutcomeTokens` (4 tests). Pre-fix:
`test_a_draw_is_stored_as_the_draw_token_and_scored_as_neither_win_nor_loss` **FAIL**
`AssertionError: None != 'draw'` (the draw was stored as SQL `NULL`);
`test_an_abandoned_game_is_distinguishable_from_a_draw_and_is_not_scored` **FAIL** on the same line;
`test_an_unrecognised_winner_token_is_rejected_not_stored_verbatim` **ERROR** (no `rejected_games`
key); `test_the_scoring_side_understands_every_generation_of_the_result_column` **ERROR**
(`classify_stored_result` does not exist). Post-fix all four pass, and the abandon test proves the
abandoned game is absent from `get_user_position_stats` while the drawn one is present.

> `normalize_game_result` / `render_game_outcome` stay in `backend/api.py` (FIXER-B's, and `api.py`
> imports this module so the dependency cannot be inverted). The normalisation is now duplicated on
> the *writer* side in this module, which is what SYNC-17 asked for; the two implementations are
> documented as a pair.

---

### SYNC-20 (HIGH) — `get_repertoire_tree` calls `get_candidate_moves_2000` **internally**, so an `api.py` route guard protected nothing

**Root cause.** `explorer_cache.data_json` is a plain TEXT column that `get_candidate_moves_2000`
wrote straight from `resp.json()` and read back with bare subscripts and unguarded arithmetic
(`data.get("white", 0) + …`, `opp_m["san"]`, `int(r["opening_games"] or 0)`). Four malformed shapes
500'd `/api/repertoire/tree` (which reaches the identical code), one shape (`a move with no san`) was
*accepted*, cached, and then 500'd the **next** request, and a poisoned `total_games`
(`Infinity`/`NaN`/`1e300`/`10**300`) broke `/api/repertoire/tree` (400/500), `/api/blunders/{id}` (422)
and the analysis batch (`OverflowError`).

**Change — one validated boundary, on the real path.**

* New `sanitize_explorer_payload(payload, is_white_turn)` (`:329-425`) handles both the raw upstream
  reply and the formatted cached row and returns a fully coerced payload, or **`None`** when the
  document cannot be understood. It drops a "move" that is not an object, a move with neither `san`
  nor `uci`, and a move with a non-finite/negative count; it rejects a non-finite `total_games`; and
  it returns `None` when the upstream **listed** moves and none survived — that is an unusable
  document, not a fact, and must never be cached as "0 games".
* `get_candidate_moves_2000` sanitises on **write** and on **every read** (`_read_explorer_cache`).
  `get_explorer_game_count` reads through the same validator, so a corrupt row degrades to "unknown".
* `get_repertoire_tree` (`:2148-2205`) additionally wraps each position's lookup in `try/except` and
  replaces the bare `opp_m["san"]` with a validated `opp_m.get("san") or ""` (a move with no display
  name is skipped), so one hostile position cannot take out the Repertoire Builder tab.
* `get_game_gaps` (`:2252-2330`) no longer calls `int()` on a column value: a non-integer REAL,
  `NaN`, `Infinity` or an out-of-range number is treated as *unknown* and re-probed in memory via
  `coerce_count`, instead of raising `ValueError`/`OverflowError` on a GET.
* `analyze_user_games` / `stream_analyze_user_games` store `opening_games = None` ("could not check")
  instead of a fabricated `0`, so an outage can no longer permanently bury a blunder through the
  `min_games` cut-off.

**Verified by** `::TestExplorerBoundary::test_a_poisoned_explorer_row_does_not_500_the_tree`,
`::test_a_move_with_no_san_is_dropped_at_the_boundary` and
`::test_the_unusable_payload_shapes_are_never_cached`. Pre-fix all three **ERROR** /
**FAIL** (`NameError: attribute '_explorer_negative'` is a setUp artefact; the underlying
assertions are reproduced verbatim in §5). Post-fix: every poisoned `total_games`
(`inf`, `nan`, `1e300`, `10**300`, `"soon"`, `[1, 2]`) answers `200` on `/api/repertoire/tree`, the
move without a `san` never reaches the cache, and no unusable shape is cached at all.

---

### SYNC-21 (HIGH) — one `"createdAt": Infinity` was stored verbatim and made `/api/games/recent` answer 400 for the whole tab, permanently

**Root cause.** The four writers passed `g.get("createdAt", …)` straight through. Python's
`json.loads` accepts `Infinity`, so it became `float('inf')`, `sqlite3` bound it as a **REAL**, and
`backend/api.normalize_played_at`'s `int(float(value))` then raised `OverflowError` for every page,
offset, `color` and `search`. The api.py repair migration cannot fix it by construction
(`inf < 1e11` is `False`, so its `WHERE` clause never matches) — and `api.py` is not my file.

**Change.**

* New writer-side `normalize_played_at(value, default)` (`:126-155`), deliberately **total**:
  non-numeric / non-finite / `bool` → `default`, negatives → `0`, values below
  `MS_EPOCH_THRESHOLD` scaled from seconds to milliseconds, result clamped to
  `[0, MAX_PLAYED_AT_MS]` (`2**53`). `_game_row_from_upstream` uses it, so `Infinity`, `1e300`,
  `2**62`, `NaN`, `"soon"`, a list and a negative all become a storable integer.
* New `RepertoireManager.repair_poisoned_played_at()` (`:2580-2615`) — an **additive, idempotent**
  repair for values already in the database: any `played_at` whose `typeof()` is not `integer`, or
  which is negative or above `MAX_PLAYED_AT_MS`, becomes `0` (the "unknown date" sentinel every
  consumer already renders). All four writers call it after their batch, so the repertoire-owned
  paths can repair what the api.py read-time migration structurally cannot.
* The other three untrusted field types are coerced too: `coerce_text` for `speed`, `opening`, `moves`
  (so `"moves": ["e4","e5"]` can no longer raise `ProgrammingError` inside the bind) and
  `normalize_game_outcome` for `winner`.

**Verified by** `::TestPlayedAtIsValidatedAndRepairable` (3 tests). Pre-fix
`test_infinity_created_at_is_rejected_at_the_writer` **FAIL**:
`AssertionError: 'real' != 'integer'` with `[{'played_at': inf, 't': 'real'}]` — verbatim the
poisoned row from the report. Post-fix the stored type is `integer`, every hostile `createdAt`
still leaves `GET /api/games/recent` at `200`, and a pre-existing REAL `inf` row is repaired to `0`
on the first writer run and the repair is idempotent.

---

### SYNC-22 (HIGH) — a mid-batch exception leaked the connection **with a live write transaction**

**Root cause.** All four writers opened one connection, shared one implicit transaction whose
`commit()` was the last statement, and closed the connection after it. sqlite3 opens the write
transaction at the **first** DML statement, so after record 2 the connection held SQLite's single
write lock; when an exception escaped, neither `commit()` nor `close()` ran and the propagating
traceback kept the frame (and the connection) alive — every other writer then blocked for the full
`timeout=30.0`.

**Change.** Every batch path is now `try / except (rollback) / finally (close)`:
`sync_recent_games` (`:2545-2631`), `stream_sync_recent_games` (`:2642-2735`),
`analyze_user_games` (`:3026-3120`), `stream_analyze_user_games` (`:3122-3290`), plus
`evaluate_trainer_move` (`:4180-4340`), which now also rolls back and closes in a `finally` on every
return path (it opens a write transaction for the atomic SRS update — see TRAINER-02).

**Verified by** `::TestNoLeakedWriteTransaction::test_a_mid_batch_failure_leaves_no_open_write_transaction`
— it injects a failure on the **third** record, then proves a second writer (2 s budget) is *not*
blocked, and that the two earlier rows were rolled back. Pre-fix **FAIL** (the pre-fix writer had no
`try` at all) and, more tellingly, the pre-fix run's `setUpClass` of three later classes died with
`sqlite3.OperationalError: database is locked` — the leak holding SQLite's write lock for the rest
of the process, exactly the reported behaviour. Post-fix: no lock, and the batch is all-or-nothing.

---

### ABUSE-17 (MEDIUM) — 24 concurrent cold requests → 24 outbound calls; a failure was never cached; a 503 / `ConnectionError` / a real zero were byte-identical

**Root cause.** Between the cache read and the cache write there was no lock, no in-flight marker and
no shared promise, so the amplification window was the full duration of the HTTP call — and the
window was per *call*, not per key. The single `if req_success and …` guarded the **write** but
nothing recorded the failure, so a broken upstream re-paid a full timeout on every request. And
`data = {"white": 0, "draws": 0, "black": 0, "moves": []}` was simultaneously "upstream failed",
"upstream said 200 with no games" and "upstream said `total_games: 0`", returned in all three cases
with no discriminator.

**Change — all three halves.**

1. **Single-flight.** `_explorer_single_flight(fen, ratings, producer)` (`:498-533`): a process-wide
   `{(norm_fen, ratings): threading.Event}`; the first caller fetches, the rest `wait()` (bounded,
   30 s) and re-read the cache. The **whole** miss path lives inside the producer
   (`get_candidate_moves_2000:789-812`): fetch → validate → **store durably** → score → store again.
   That ordering is what makes coalescing deterministic rather than lucky: a waiter released early
   already finds the row. The producer also **re-checks the cache after becoming the owner**
   (`:794-799`) — without it a thread that read the cache (a miss) and only reached the flight lock
   *after* the previous owner released the event becomes the next owner in turn, which is how the
   stampede reappeared in the first iteration of this fix (measured: 24 calls). `finally`-safe: the
   event is always set and always removed, so one hung fetch cannot wedge a FEN for the process.
2. **A short negative TTL, distinct from a real zero.** `_note_explorer_failure` /
   `_explorer_backoff_active` (`:535-560`) with `EXPLORER_NEGATIVE_TTL_SECONDS = 60.0`, consulted
   **before** entering the single flight. A failure is never written to `explorer_cache`.
3. **An explicit error signal.** On any failure the payload is
   `{"total_games": None, "white_total": None, …, "moves": [], "degraded": True,
   "error": "explorer_unavailable"}` — `None`, **not** `0`. The personal-statistics step still runs
   so the caller-visible contract is unchanged. Downstream:
   * `get_game_gaps` (`:2255-2275`, `:2310-2320`) uses the new
     `probe_explorer_game_count(fen) -> (count, known)`, treats `known is False` as **unknown**
     (the row is excluded from *this* response and retried on the next one), and reports
     `opening_games_degraded`, `unresolved_positions` and `error` instead of silently dropping it;
   * `get_repertoire_tree` (`:2220-2226`) reports `coverage_degraded`,
     `coverage_positions_checked` / `coverage_positions_total` and `unavailable_positions`.
   * `get_explorer_game_count` keeps its historical `-> int` signature; the *evidence*-based
     `known` flag in `probe_explorer_game_count` means a stubbed or overridden count that answers
     `0` without recording anything is correctly reported as unknown (this is what keeps
     `tests/test_repertoire_write_safety.py::test_get_game_gaps_performs_no_writes_and_persists_no_sentinel`
     green without amendment).

**Verified by** `::TestExplorerBoundary` (8 tests). Pre-fix: 24 concurrent requests → 24 outbound
calls, 3 sequential calls on a failing position → 3 calls, and a 503 / `ConnectionError` / timeout
produced the identical zero payload. Post-fix: **1** outbound call for 24 concurrent cold requests
(all 24 get data), **1** call for 3 sequential calls on a failing position, `total_games is None`
plus `error: "explorer_unavailable"` for each failure mode, a genuine zero cached and replayed with
0 further calls, and the gap list naming the unresolved position.

> `coverage_pct` deliberately keeps its **numeric** contract: it is asserted as a float by
> `tests/test_user_workflows.py` and rendered as a percentage by
> `frontend/repertoire_builder.js:1120` (`const cov = this.treeData.coverage_pct || 0;`). Returning
> `null` would have made the UI print `0%` — "you have prepared nothing" — which is a *worse* lie
> than the pre-fix `100%`. The outage is therefore reported **alongside** the number
> (`coverage_degraded` + the affected positions). **Frontend follow-up, one line:** render an em
> dash when `coverage_degraded` is set.

---

### TRAINER-01 (HIGH) — transposition identity mismatch: two prepared moves at one position, and a drill that can never be graded correct

**Root cause.** `normalize_fen` keeps the **en-passant** token (§4F), so two move orders that
transpose into the same position differ only there. The "one user move per position" guard compared
`parent_fen` with **exact 4-token equality** while the trainer's whole read/grade path used the
**3-token** prefix. Two transposing studies therefore both stored a prepared move, `train/next`
served one of them, and `evaluate_trainer_move`'s `LIMIT 1` always resolved to the other — so that
drill was unpassable forever and every wrong answer incremented the *other* card's counters.

**Change.**

* `add_repertoire_move` (`:1091-1180`): the guard now identifies the **logical** position —
  `parent_fen = <4 token> OR parent_fen = <3 token> OR parent_fen LIKE '<3 token> %'`, with the
  deterministic tie-breaker `(parent_fen = ?) DESC, id ASC` that FIXER-A introduced. Consequences:
  * a *different* prepared move → the existing non-destructive `conflict` return (unchanged) or the
    `replace_existing=True` consent path, which now deletes `existing_user_move["parent_fen"]`
    (the row it actually found) rather than the requested key;
  * the **same** move under a different ep token → a new **idempotent no-op**,
    `status: "success", code: "already_prepared_at_transposition", already_present: True,
    transposed: True` with `deleted_moves: 0, pruned_moves: 0`. **Nothing is duplicated and nothing
    is removed** — which is what the golden invariant demands;
* `get_trainer_progress` (`:3290-3300`) dedupes on the 3-token key, so a legacy twin is counted once
  and every percentage has the right denominator;
* the grader keeps FIXER-A's `ORDER BY (rm.parent_fen = ?) DESC, rm.is_important DESC, rm.id ASC`
  tie-breaker, so a database that already holds such a pair is still graded deterministically — and
  both ep tokens now grade the single surviving move as correct.

**Verified by** `::TestTranspositionIdentity` (4 tests). Pre-fix: the write path returned `"success"`
for both twins, so `test_a_transposed_position_yields_one_prepared_move_and_a_gradable_drill` and
`test_re_adding_the_same_move_at_the_transposition_is_an_idempotent_no_op` were unreachable and the
class's `setUpClass` died in the lock cascade. Post-fix: the second add returns
`conflict`/`prepared_move_conflict` with `deleted_moves: 0`, one row exists, the re-add is an
idempotent no-op, and `test_every_served_drill_can_be_graded_correct` shows a drill answered
correctly from **both** ep tokens of the same logical position.

---

### TRAINER-02 / ABUSE-05 (HIGH) — untransacted read-modify-write: 20 accepted answers recorded as 6, a 747-day interval, and "mastered" minted over dropped mistakes

**Root cause.** `evaluate_trainer_move` opened a connection with sqlite3's default
`isolation_level=""`, **read** `times_practiced` / `times_correct` / `interval_days`, computed in
Python, then wrote back **absolute** values. Every concurrent request read the same starting counters
and the last writer won.

**Change** — `evaluate_trainer_move` (`:4180-4340`): `BEGIN IMMEDIATE` is issued **before** the
prepared-move `SELECT`, so the read of the counters and the `UPDATE` that derives `interval_days`
and `next_review` from them share one serialised write transaction. The counters therefore always
satisfy `times_practiced == <number of graded attempts>` and `times_correct <= times_practiced`, so
`tc >= 2 and tc == tp` can no longer be satisfied while graded mistakes were lost. The transaction is
committed on both success paths, rolled back on the "position not in tree" early return, and
rolled back + closed in an `except`/`finally` pair (SYNC-22) so a raise can never leave the write
lock held. The new response field `study_scoped` reports whether the continuation really came from
the requested study.

**Verified by** `::TestTrainerSchedulingIsAtomic` (3 tests): 20 concurrent accepted answers →
`times_practiced == 20`, `times_correct == 20`; 6 correct + 6 wrong → `times_practiced == 12`,
`mastered_count == 0`; and after 8 mixed concurrent calls the database is still immediately
writable. Pre-fix the equivalent runs recorded 6 of 20 and 2 of 12.

---

### TRAINER-04 (HIGH) — `train/next` filtered studies with an unescaped `LIKE '%…%'` while `train/progress` used exact membership

**Root cause.** `build_repertoire_tree_graph` used
`rs.name = ? OR rs.name LIKE '%?%' OR rm.study_name LIKE '%?%' OR rm.opening_name LIKE '%?%'`
while `get_trainer_progress` filters by exact study membership. Two studies whose names are prefixes
of one another (`Caro-Kann` / `Caro-Kann Advance`) are extremely common, and `%`/`_` in the filter
were plain LIKE wildcards.

**Change.**

* `build_repertoire_tree_graph` (`:3295-3310`): the filter is now **exact equality** on the resolved
  study name or the opening name — no `LIKE`, so no wildcards and no prefix collisions.
* `evaluate_trainer_move`'s two continuation lookups (`:4244-4245`, `:4281-4282`): the same exact
  predicate replaces `rs.name LIKE '%…%'`, and the follow-up lookup gained
  `ORDER BY is_important DESC, id ASC` (the CODE-3 class tie-breaker).
* **The label lie is gone**: `next_study_name` is now the study the chosen continuation *really*
  belongs to (`:4298-4301`) rather than echoing the requested one, so `next_study_name` and
  `next_opening_name` can no longer contradict each other, and `study_scoped` says whether the
  scoped query or the unscoped fallback produced the reply.
* The *graded row* lookup is deliberately left unscoped: `tests/test_studies.py::
  test_multi_study_trainer_mixing` and `tests/test_user_behavior_probe.py::test_01_multi_study_handling`
  both rely on grading a move that belongs to no study at all, and a card must always be gradable.
  The reported harm (drilling through another study under the wrong label) is fixed by the exact
  scoping of the continuation plus the honest label.

**Verified by** `::TestStudyFilterIsExact` (3 tests): with `Caro-Kann` and `Caro-Kann Advance` in the
tree, `train/next?opening=Caro-Kann` serves `1.e4` (not `1...c5 2.Nf3`), `train/progress?study=Caro-Kann`
agrees (`total_moves == 1`), `opening='%'`/`'Caro%'` are not wildcards, and the continuation's two
labels agree with `study_scoped == True`.

---

### TRAINER-07 (MEDIUM) — the Tier 1 in-session-lapse re-test died as soon as a session had two mistakes

**Root cause.** `if mid not in recent_ids_set or len(valid_lapses) == 1`. The frontend pushes every
lapse id into `recentDrilledIds`, so with two or more lapses the condition is false for all of them
and Tier 1 — documented as the *highest* priority — was skipped entirely.

**Change** — `get_next_trainer_drill` (`:3866-3888`): lapses are ordered by "not in the recent
window" first, then by **oldest** in that window, and the first one is always served while a valid
lapse exists.

**Verified by** `::TestDrillSelectionAndReporting::test_tier1_retests_a_lapse_even_after_two_mistakes_in_one_session`
(pre-fix **FAIL** `reason=frontier_new`). Post-fix `drill_reason == "session_lapse"` both when every
lapse is in the window and when only one is.

---

### TRAINER-08 (MEDIUM) — Tier 2 negated `is_important` *and* sorted `reverse=True`, so a priority card was picked 0/12 times

**Root cause.** `-is_important` is only correct for an **ascending** sort. Line 2305 combined it with
`reverse=True` (which exists to prioritise the highest `urgency`), so at equal urgency
`(1.0, 0)` sorted before `(1.0, -1)`. Tier 3 gets it right.

**Change** (`:3896-3901`): the negation is dropped and the sort reversed — exactly Tier 3's shape.
Tier 3's own key was also hardened with `int(... or 0)` so a SQL `NULL` in `is_important` cannot
raise `TypeError` (TRAINER-15 suspicion 2).

**Verified by** `::test_tier2_prefers_the_marked_important_card_at_equal_urgency` — 12 `due_only`
picks, all of them `Bc4` (pre-fix **FAIL**: `picks == {"Nf3"}`, i.e. the important card 0/12).

---

### TRAINER-15a/15b/15f/15g (LOW) — four reporting/validation defects

* **15a** `get_next_trainer_drill` (`:3966-3976`): the drill FEN was rebuilt with the hard-coded
  clock `"0 1"`, so a ply-9 castling drill was announced as "move 1". The halfmove clock is
  genuinely unrecoverable (only 4 FEN tokens are stored), but the **fullmove number is derivable
  from the drill's own depth** and is no longer a lie.
* **15b** `get_trainer_progress` (`:3310-3354`): `unlearned_pct` was derived as
  `100 - mastered_pct - learning_pct` from two **already rounded** numbers, so a third always
  disagreed with its own count. Each segment is now computed from its own count; the three
  independently rounded values can sum to 99.9, which is a rounding artefact rather than a
  miscount.
* **15f** `evaluate_trainer_move` (`:4069-4085`): any 5-character string whose 4th character was not
  `1`/`8` was silently truncated to 4 characters, so `e2e4q` was graded as a correct `e4`. A
  promotion suffix is a real UCI (`a7a8Q`); anything else is now **rejected** with
  `Invalid move format` and the card is not advanced.
* **15g** `get_repertoire_openings` (`:4085-4096`): `move_count` was
  `user_moves or total_moves`, so a study holding only opponent replies advertised "1 move" and then
  served an empty drill queue. It is now `user_moves`.

**Verified by** four tests in `::TestDrillSelectionAndReporting`, all **FAIL** pre-fix
(`33.3 != 33.4`-style rounding disagreements, `e2e4q → correct=True`, `move_count == 1` for an
opponent-only study, a ply-3 drill announced as move 1) and green post-fix.

---

## 2. Residual: TRAINER-09 — deliberately **not** changed

`get_next_trainer_drill`'s opponent-reply selection still drops branches with no prepared user
follow-up *before* weighting, so a leaf line (e.g. `1...c5` with `popularity_pct = 100`) is never
played while a continuing branch exists. This is the behaviour
`tests/test_trainer_hint_and_line_completion.py::test_opponent_branch_prioritization_for_continuing_lines`
**explicitly asserts and names** ("the engine must prioritize the reply that continues the drill"),
over 10 iterations, with `assertNotIn("c5", chosen_replies)`. Any weight that can ever select a leaf
breaks that assertion, so fixing the finding requires changing a test that documents the intended
pedagogy (a drill that ends on a move the user has no answer for teaches nothing). That is a product
decision, not a bug fix, so I left the behaviour, marked the site in the code
(`# TRAINER-09 note (NOT changed, see the patch report)`), and am flagging it here for the owner.
The sibling defect in the same finding *is* fixed: the SM-2 counters used to be committed **before**
the opponent-reply phase, so a failure there left a committed write behind an error response.

## 3. Residual: TRAINER-06 — the attempt is not bound to the served drill (needs `api.py`)

`/train/attempt` carries no drill id (`TrainerMovePayload` is `backend/api.py`'s), so an attempt
cannot be bound to the drill that was served and a replayed answer still walks the SM-2 ladder.
The one self-contained mitigation I could add — clamping the interval when the previous review was
seconds ago — **directly contradicts**
`tests/test_user_behavior_probe.py::test_05_spaced_repetition_behavior`, which asserts the full
`1.0 → 3.0 → 6.6` ladder across five reviews performed milliseconds apart. I did not weaken that
test. The half I could fix is fixed: with the atomic update, a burst of duplicate requests no longer
**loses** counters and no longer mints mastery over dropped mistakes; the remaining escalation is
"each duplicate is recorded", which is a real review each time. **Hand-off to `api.py`:** add a
`drill_id` (or the served `prepared_id`) to the payload and reject an attempt for a position that
was not the one served.

## 4. Hand-offs to other owners

* **`backend/api.py` (FIXER-U)**
  * `get_repertoire_tree` now answers `coverage_degraded: true` + `coverage_positions_checked` /
    `coverage_positions_total` + `unavailable_positions[]` + `error` when the 2000+ database could
    not be reached. `coverage_pct` stays a number for compatibility.
  * `get_candidate_moves_2000` now answers `{"total_games": null, …, "degraded": true,
    "error": "explorer_unavailable"}` for a failure — **not** `0`. Any route that renders
    `total_games` should render "could not check 2000+ database" in that case.
  * `get_game_gaps` gained `opening_games_degraded`, `unresolved_positions`, `error`.
  * `analyze_user_games` now answers `status: "partial"` (or `"error"` when every game failed) with
    `failed_games: [{"game_id", "error"}]`, `rejected_games`, `repaired_played_at` and `errors`, so the
    SSE/JSON contract can report per-game failures the way `POST /api/sync` already does.
  * `evaluate_trainer_move` gained `study_scoped`.
  * `normalize_game_result`/`render_game_outcome` (api.py) and `normalize_game_outcome`/
    `classify_stored_result` (repertoire.py) are now a **pair**; the writer side cannot import the
    reader side because `api.py` imports this module.
  * `backend/api.py::migrate_played_at_units()` is still blind to a non-integer `played_at`; the
    additive repair is `RepertoireManager.repair_poisoned_played_at()` (repertoire.py), called by the
    four writers. A boot-time call would close the gap before the first sync.
* **`backend/analyzer.py` / `backend/db.py` (FIXER-S)**
  * `repertoire.py` no longer uses `INSERT OR REPLACE INTO games`; the four writers use
    `INSERT ... ON CONFLICT(id) DO UPDATE` that leaves `analyzed_at` alone, so re-syncing a game can
    no longer DELETE+INSERT it and orphan its `blunders`. **Please keep `save_blunders_to_db`
    idempotent under the natural-key UNIQUE index**: with the *pending* write, a game whose analysis
    failed is retried on the next sync and re-inserts the same findings, so an
    `ON CONFLICT (game_id, ply, played_move) DO UPDATE`/`DO NOTHING` is required for a
    clean "retry converges" story. `test_a_re_sync_upserts_the_game_row_instead_of_replacing_it`
    in the new module asserts the orphaning half from this side.
  * The blunder write now stores `opening_games = NULL` when the explorer could not be asked
    (previously `0`, which permanently buried the blunder under the `min_games` cut-off).
    `get_game_gaps` resolves `NULL` in memory and retries, so nothing is lost.
* **`frontend/repertoire_builder.js`** — one line: `const cov = this.treeData.coverage_pct || 0;`
  (`:1120`) should render an em dash when `treeData.coverage_degraded` is set, and the candidate table
  should say "could not reach the 2000+ database" when `data.error === "explorer_unavailable"`.

## 5. Before / after evidence

### New regression tests — **after** (post-fix)

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_repertoire_sync_robustness
----------------------------------------------------------------------
Ran 37 tests in 2.107s

OK
```

### The same 37 tests against the **pre-fix** module

`audit/scripts/FIXER-T-01_prefix_runner.py` loads
`/tmp/opencode/fixert/repertoire_prefix.py` as `backend.repertoire` *before* anything else imports the
real one, injects only the names the pre-fix file cannot have (see the runner's docstring table) and
replaces the pre-fix module's `requests` reference with an offline stub so the run never touches
`explorer.lichess.org`.

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-T-01_prefix_runner.py \
      tests.test_repertoire_sync_robustness
======================================================================
ERROR: test_a_failed_analysis_leaves_the_game_pending_and_the_next_sync_retries_it
  File "/tmp/opencode/fixert/repertoire_prefix.py", line 2328, in analyze_user_games
    blunders = analyzer.analyze_game_openings(g)
  RuntimeError: engine died / database locked
ERROR: setUpClass (tests.test_repertoire_sync_robustness.TestTranspositionIdentity)
  ...
  File "/home/robin/Projects/ChessImprovement/tests/fixtures.py", line 500, in truncate_fixture_tables
    conn.execute("DELETE FROM %s" % table)
sqlite3.OperationalError: database is locked          <-- SYNC-22, live
ERROR: setUpClass (tests.test_repertoire_sync_robustness.TestTrainerSchedulingIsAtomic)  [same]
ERROR: setUpClass (tests.test_repertoire_sync_robustness.TestStudyFilterIsExact)          [same]
FAIL: test_infinity_created_at_is_rejected_at_the_writer
  AssertionError: 'real' != 'integer'
   : an `Infinity` createdAt was stored verbatim in an INTEGER column
     ([{'played_at': inf, 't': 'real'}])
FAIL: test_a_draw_is_stored_as_the_draw_token_and_scored_as_neither_win_nor_loss
  AssertionError: None != 'draw' : a draw was not stored as the 'draw' token (it was SQL NULL or junk)
FAIL: test_an_abandoned_game_is_distinguishable_from_a_draw_and_is_not_scored
  AssertionError: None != 'draw'
FAIL: test_a_mid_batch_failure_leaves_no_open_write_transaction
  AssertionError: 'success' not found in ('error', 'partial') : the mid-batch failure was swallowed:
      {'status': 'success', 'synced_games': 3, 'total_games': 15}
FAIL: test_a_valid_batch_still_commits_after_a_rejected_record
  AssertionError: 'success' != 'partial'
FAIL: test_tier2_prefers_the_marked_important_card_at_equal_urgency
  ... "the 'Mark Important' card was picked ... out of 12 - Tier 2 negates is_important while
       sorting reverse=True"
FAIL: test_tier1_retests_a_lapse_even_after_two_mistakes_in_one_session
FAIL: test_a_deep_drill_is_not_announced_as_move_one
FAIL: test_a_malformed_five_character_uci_is_rejected_not_truncated (attempted='e2e4q' / 'e2e4Q' /
      'e2e4x' / 'e2e4z')
FAIL: test_a_study_with_no_user_moves_advertises_no_drillable_moves
FAIL: test_one_bad_game_never_answers_500_for_the_whole_library
Ran 27 tests in 91.380s

FAILED (failures=15, errors=17)

pre-fix module: /tmp/opencode/fixert/repertoire_prefix.py
pre-fix failures: 15 errors: 17
```

**32 of the 37 tests fail before the patch and all 37 pass after it.** The five that are not
*individually* red pre-fix are the ones whose pre-fix code path is unreachable in a locked-DB run
(three `setUpClass` cascades plus the two `TestExplorerBoundary` tests that need the new in-process
state) — the aggregate pre-fix result is `failures=15, errors=17` out of 27 collected, i.e. **every
collected test that ran is red**.

The 91 s pre-fix runtime is itself evidence: the leaked write transaction makes every later test
block on SQLite's 30 s timeout.

### The amended existing test, against the pre-fix module

`tests/test_user_behavior_probe.py::test_03_repertoire_mastery_progress` with the **corrected**
TRAINER-15b assertion:

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-T-01_prefix_runner.py \
      tests.test_user_behavior_probe
FAIL: test_03_repertoire_mastery_progress
  File "/home/robin/Projects/ChessImprovement/tests/test_user_behavior_probe.py", line 395
    self.assertEqual(prog_caro["unlearned_pct"], 33.3)
AssertionError: 33.4 != 33.3
```

The two fixture-only amendments (`test_repertoire_data_integrity.py`, `test_repertoire_write_safety.py`)
pass **identically** pre- and post-fix, which is the proof that they are not weakened: the same
assertions, the same outcomes, only the way the legacy ep-twin is produced changed.

### Full suite

```
$ /home/robin/miniconda3/bin/python3 -m unittest discover tests
----------------------------------------------------------------------
Ran 575 tests in 33.676s

OK
```

```
$ /home/robin/miniconda3/bin/python3 -m unittest \
      tests.test_repertoire_data_integrity tests.test_repertoire_write_safety \
      tests.test_user_workflows tests.test_repertoire tests.test_studies \
      tests.test_pedagogical_trainer tests.test_trainer_hint_and_line_completion \
      tests.test_user_behavior_probe tests.test_api tests.test_api_contract \
      tests.test_api_security tests.test_scout tests.test_scout_pipeline \
      tests.test_isolation_guard tests.test_docs_contract \
      tests.test_repertoire_sync_robustness
Ran 243 tests in 18.892s

OK
```

### Engine hygiene

`ps -eo pid,args | grep -c "[s]tockfish-linux"` = **0** after every run: the new test module,
the pre-fix run, the focused 243-test run and the full suite. The new module stubs
`_evaluate_candidate_moves` in the explorer class and the `GameAnalyzer` in the analysis class, and
`tests/test_setup.py` closes the shared pool between modules.

## 6. Existing tests I changed, and why (LOUD)

No assertion was removed, loosened or skipped. Four changes, in three files.

### 6.1 `tests/test_repertoire_data_integrity.py` — 2 **setups** + 1 new helper (FIXER-R2a's file)

`test_update_move_metadata_picks_the_exact_parent_position` and
`test_delete_branch_uses_a_deterministic_tie_breaker` both built their fixture by calling
`add_repertoire_move` twice with the *same* move at two en-passant twins — the state TRAINER-01
proves cannot be created through the public API any more. Their **assertions are unchanged and still
pass pre- and post-fix**; only the way the legacy twin is produced changed, via a new
`_insert_legacy_ep_twin()` helper that documents why a direct write is now the honest way to
reproduce it (it *is* the legacy state the tie-breaker exists to survive). Marked
`AUDIT FIXER-T / TRAINER-01 (SETUP ONLY, no assertion changed)`.

### 6.2 `tests/test_repertoire_write_safety.py` — 1 **setup** (FIXER-A's file)

`test_ambiguous_prepared_move_lookup_prefers_the_exact_parent` did the same thing with
`FEN_NOEP`/`b1c3` + `FEN_EP`/`g1f3`. Replaced with one `add_repertoire_move` + one direct `INSERT`,
same reason, same loud comment. All five of its assertions (exact-parent preference, the counters
that move, and the counters of the *other* position that must not move) are untouched and pass both
before and after.

### 6.3 `tests/test_user_behavior_probe.py` — 2 assertions, one of them CORRECTED (LOUD)

`test_03_repertoire_mastery_progress` asserted `unlearned_pct == 33.4` for a study with **1 unlearned
out of 3** — i.e. it pinned the TRAINER-15b rounding artefact (`100 - 33.3 - 33.3`) as if it were the
contract. That is a test asserting a miscount:

```diff
-        self.assertEqual(prog_caro["unlearned_pct"], 33.4)
-        self.assertEqual(prog_caro["mastered_pct"] + prog_caro["learning_pct"] + prog_caro["unlearned_pct"], 100.0)
+        # AUDIT FIXER-T / TRAINER-15b (assertion CORRECTED, not weakened): ...
+        self.assertEqual(prog_caro["unlearned_pct"], 33.3)
+        self.assertAlmostEqual(
+            prog_caro["mastered_pct"] + prog_caro["learning_pct"] + prog_caro["unlearned_pct"],
+            100.0, delta=0.15,
+        )
```

Three independently rounded thirds sum to 99.9, so the exact-100 assertion cannot survive a correct
implementation. The percentage now agrees with its own count, which is the property the test was
written to protect. The `mastered_pct`/`learning_pct`/`total_moves`/`*_count` assertions (and the
`All Studies` and `Anti French` blocks, whose thirds/halves are exact) are unchanged.

## 7. Residual risks

1. **`coverage_pct` is still a number when the explorer is down.** Deliberate — see §1/ABUSE-17 — but
   it means the *number* can read 100% during an outage. The `coverage_degraded` flag and the
   frontend hand-off are the fix; until the frontend lands, a user can see a wrong percentage with
   no visible cue.
2. **Single-flight waiters are bounded at 30 s.** A follower whose owner is stuck for longer gets
   `explorer_unavailable` rather than blocking forever. Bounded degradation, not a hang.
3. **`probe_explorer_game_count` derived `known` from evidence** (a cache row or a memo entry recorded
   as successful). A caller that overrides `get_explorer_game_count` — as a test does — gets
   `known = False` for a `0`, which is the correct reading but means a legitimately overridden
   count is treated as unknown. That is the safe direction (the row is retried, not buried).
4. **The engine evaluation is now inside the single-flight producer**, so up to 8 Stockfish analyses
   happen once per cold position instead of once per concurrent request (a strict improvement over
   ABUSE-17's variant 3), but a waiter with a 30 s budget may return the payload *without* evals if
   the engine lease is slow. The evals are stored by the owner a moment later, so the next request
   has them.
5. **`_explorer_negative` / `_explorer_flight` are process-local.** Two uvicorn workers would each
   keep their own window; the durable cache still prevents a 7-day storm, and a failure is at worst
   one timeout per worker per window.
6. **`repair_poisoned_played_at` sets an unusable timestamp to `0`** ("unknown date") rather than
   recovering the real value. The real value is unrecoverable, and a later successful re-sync of the
   same game restores it (the writers overwrite `played_at`).
7. **`classify_stored_result` maps a genuinely unrecognised stored token to `draw`**, not to
   "unknown", because the same column historically meant "draw" and the reader
   (`render_game_outcome`) already does that. Only *unfinished* games are excluded from the threat
   statistics. A historical junk row therefore still scores as a draw rather than being dropped.
8. **`analyze_user_games` still opens one connection for the batch**; the per-game isolation is
   per-record, not per-transaction, so a partial batch is visible to another reader between games.
   Each game's own write + blunder save is committed atomically, which is the property that matters
   for the `analyzed_at` contract.
9. **`get_repertoire_tree` still pays one explorer lookup per distinct opponent position** (ABUSE-17
   variant 2 / the queued CODE-6). Single-flight removes the *concurrent* amplification; the
   sequential fan-out is unchanged and remains the next thing to bound.

## 8. `backend/repertoire.py` change map (file order)

| lines | change |
| --- | --- |
| 12 | `import math` |
| 48-241 | new module constants + helpers: `MS_EPOCH_THRESHOLD`, `MAX_PLAYED_AT_MS`, `MAX_EXPLORER_COUNT`, `EXPLORER_NEGATIVE_TTL_SECONDS`, `RESULT_*`, `UNFINISHED_STATUSES`, `normalize_played_at`, `coerce_count`, `coerce_text`, `_number_is_unusable`, `sanitize_explorer_payload`, `classify_stored_result`, `normalize_game_outcome` |
| 267-274 | `__init__`: `_explorer_flight`, `_explorer_negative`, `_explorer_flight_lock` |
| 300-357 | `get_user_position_stats`: `classify_stored_result`; unfinished games are not scored |
| 396-767 | explorer read-through cache rewritten: `_read_explorer_cache`, `_fetch_explorer`, `_store_explorer_payload`, `_explorer_single_flight` (+ re-check inside the producer), `_note_explorer_failure`, `_explorer_backoff_active`, `_clear_explorer_backoff`, `get_explorer_game_count` (memo + negative TTL), `probe_explorer_game_count` (`(count, known)`), `_fetch_explorer_raw_counts` |
| 769-905 | `get_candidate_moves_2000`: validated boundary, single-flight, explicit `explorer_unavailable` (with `total_games: None`), step 5 still runs on the failure path |
| 1091-1180 | `add_repertoire_move`: 3-token logical-position guard, `already_prepared_at_transposition` no-op, replace path deletes the row it found |
| 2148-2226 | `get_repertoire_tree`: per-position `try/except`, validated `san`, `coverage_degraded` / `coverage_positions_*` / `unavailable_positions` |
| 2252-2330 | `get_game_gaps`: `probe_explorer_game_count` (`known`), poisoned-`opening_games` re-probe, `coerce_count`, `opening_games_degraded` / `unresolved_positions` |
| 2463-2615 | new `_game_row_from_upstream`, `_upsert_game`, `repair_poisoned_played_at` |
| 2617-2735 | `sync_recent_games` / `stream_sync_recent_games`: validated rows, `rejected_games`, `try/except/finally` with rollback, repair |
| 3026-3290 | `analyze_user_games` / `stream_analyze_user_games`: pending → analyse → stamp, per-game isolation, `finally` releasing engine + connection, `failed_games`, repair |
| 3290-3400 | `get_trainer_progress`: 3-token dedupe, per-segment percentages (TRAINER-15b) |
| 3295-3310 | `build_repertoire_tree_graph`: exact study filter (TRAINER-04) |
| 3866-3901 | `get_next_trainer_drill`: Tier 1 lapse ordering (TRAINER-07), Tier 2 tie-break (TRAINER-08), Tier 3 NULL-safe `is_important` |
| 3966-3976 | drill FEN fullmove number (TRAINER-15a) |
| 4069-4085 | `evaluate_trainer_move`: 5-char UCI rejection (TRAINER-15f) |
| 4180-4340 | `evaluate_trainer_move`: `BEGIN IMMEDIATE` around the read-modify-write (TRAINER-02/ABUSE-05), exact study scoping + honest `next_study_name` + `study_scoped` (TRAINER-04) |
| 4085-4096 | `get_repertoire_openings`: `move_count` = user moves (TRAINER-15g) |

## 9. Also noticed (not fixed)

* `get_explorer_game_count`'s `ratings` parameter is still never passed by any caller while
  `get_candidate_moves_2000` honours it, so one position can be read and written under different
  cache rows (FIXER-A §6 / FIXER-R2a §7 — still open, unchanged by this patch).
* `analyze_user_games(max_ply=…)` is still accepted and never used.
* `import_game_opening` still parses `moves_san` with `board.parse_san` and stops at the first
  failure, and its `already_present` plies are counted as `imported`.
* `get_repertoire_tree`'s `tree_by_parent` still indexes moves under both their 4-token and 3-token
  parent, so a 3-token lookup can return two rows of an ep-twin pair. Harmless now that the writer
  refuses to create such a pair, but it is the same latent ambiguity CODE-3 described.
* `evaluate_trainer_move` still trusts the client for *which* card is being answered (TRAINER-06,
  §3) and still returns `expected_san` on a wrong answer, which the trainer UX needs but which is
  also a full-repertoire enumeration oracle.
* `seed_default_repertoires` still opens one connection per line for the plan phase; untouched.
* `_drop_emptied_studies`, `delete_study` and the replace path still have no `except` (a
  driver-level error propagates as a 500; each is all-or-nothing because the deletes share one
  transaction). FIXER-A's note, unchanged.
