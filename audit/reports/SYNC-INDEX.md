# ATTACKER-3 (SYNC) — findings index & live-status matrix

**Agent:** ATTACKER-3 (SYNC) — territory: `backend/lichess_client.py`, `backend/analyzer.py`,
`backend/evaluator.py`, `backend/db.py`, sync/game-ingestion/blunder/dashboard endpoints in
`backend/api.py`, `get_game_gaps` / `get_user_position_stats` / `get_candidate_moves_2000` /
blunder patch detection in `backend/repertoire.py`, and the blunder-review / dashboard /
personal-stats UI in `frontend/repertoire_builder.js`.

**Isolation:** every script imports `tests.test_setup` first (BRIEF rule 2). While I worked,
`tests/test_setup.py` was being rewritten by another agent and its synthetic fixture aborted;
`audit/scripts/atk3_lib.py` then falls back to a private `tempfile.mkstemp` DB, asserted to be
different from `backend.db.DEFAULT_DB_PATH`. `chess_improvement.db` was never opened.
`atk3_lib.restore_network()` installs a **tripwire** so no probe in this directory can reach
lichess.org (verified: `grep -c 'LEAKED TO REAL NETWORK'` matches only the tripwire's own
error text). Port 38281 was never contacted — everything goes through `TestClient(app)`.

## Live findings (all re-verified against the tree as it stood at the end of the run)

| ID | Severity | File:Line | One-line title |
|---|---|---|---|
| SYNC-01 | critical | backend/db.py:118-150 | `blunders.best_pv` is never created by the schema, so the analyzer cannot persist a single blunder |
| SYNC-02 | high | backend/repertoire.py:1626,1718,1871,1979 + :146 | `"winner": null` (a draw) stored as NULL by all four repertoire.py sync paths, then scored as a LOSS by the threat engine |
| SYNC-03 | high | backend/lichess_client.py:89-95 → repertoire.py:1594,1684,1842,1945 | one non-dict NDJSON line aborts the whole batch with a 500 and leaves the write txn open for 30 s |
| SYNC-04 | medium | backend/repertoire.py:218-320 | 5 distinct malformed explorer payloads cause unhandled 500s |
| SYNC-05 | high | backend/repertoire.py:325-341 | `Infinity`/`NaN` from the explorer permanently poisons `explorer_cache` for 7 days |
| SYNC-06 | medium | backend/analyzer.py:100-103,162,177 | mate scores collapse to ±9999 sentinels; every huge loss is explained as "Checkmate in 1" |
| SYNC-07 | medium | backend/api.py:739-748, 791-800 | `/api/blunders` `limit` is unbounded (`LIMIT -1` = no limit); out-of-int64 id → 500 |
| SYNC-08 | medium | backend/api.py:826-830 | every blunder-detail view makes a blocking, un-cached outbound explorer call |
| SYNC-09 | low | backend/api.py:691-694 | dashboard `GROUP BY opening_name, color` reports one arbitrary ECO per group |
| SYNC-10 | medium | backend/api.py:1343,1350 | `max_games` unbounded on the analysis endpoints; `0`/negative/absent = whole history |
| SYNC-11 | medium | backend/repertoire.py:1645-1690, 1904-1946 | SSE disconnect is never observed during the blocking download |
| SYNC-12 | medium | backend/repertoire.py:1754-1820 → 483-520 | one-click "Add Opening" silently replaces + prunes the prepared repertoire line |
| SYNC-13 | medium | frontend/repertoire_builder.js:992-1015,1153,1499 | stored XSS: untrusted game fields interpolated into `innerHTML` |
| SYNC-14 | low | backend/api.py:226-232,324-330 | Lichess token: plaintext, unbounded, unclearable, replayed to `explorer.lichess.org` |
| SYNC-15 | medium | backend/analyzer.py:236-268 + db.py:118-141 | `blunders` has no uniqueness constraint → re-analysis duplicates blunders |

## Verified-correct (no finding)

* **AGENTS.md §4A threat formula** is implemented exactly as specified, including
  `clamp(L,0,1)` and the `+0.02` epsilon. Constraint 1 (30@50 % = 8.10 > 2@0 % = 2.04) and
  Constraint 2 (20@50 % = 5.40 < 10@20 % = 6.60) both hold; `N = 0` does not divide by zero
  because `L` is derived from `W`, not `N`. The *inputs* are corrupted by SYNC-02, not the
  formula.
* **AGENTS.md §4B.1 `eval_before >= -200` filter** is applied and correctly signed
  (`-199` and `-200` kept, `-201` dropped).
* **AGENTS.md §4B.3 opening cutoff** boundary is exact: with `min_games=30`, positions with
  `opening_games` 29 / 30 / 31 behave as 29 excluded, 30 and 31 included. The dropdown values
  10/20/30/50/100 and `min_games=-1` → 422 all behave.
* **AGENTS.md §4D auto-promotion** works server-side: after patching blunder #1,
  `gaps[0]` becomes the #2 blunder and `patched_count`/`unpatched_count` update correctly.
  (The frontend started consuming `is_patched` / `patched_count` / `earlier_mistake` /
  "Show Patched" during this audit round, so §4B.2/§4D are now wired up.)
* **`import_game_opening` move validation** is sound: every imported move is checked against
  `board.legal_moves` and a malformed `moves_san` truncates the import instead of injecting.
* **No SQL injection** through the explorer `san`/`uci` fields or through `/api/games/recent`'s
  `search` parameter (all parameterised; hostile strings are stored as data).
* **`init_db()` migrations** are idempotent (5 consecutive runs → identical schema, 24 columns)
  and, since the CODE-05 patch, run in one explicit transaction with the destructive orphan
  purge moved behind an opt-in flag and the forbidden auto-study names blocked.

## Fixed during this audit round by other agents (do NOT re-dispatch a fixer)

| Was | Now |
|---|---|
| `/api/sync` stamped `analyzed_at` before analysis and had no error isolation → permanent silent blunder loss + leaked engine/connection | `backend/api.py:341+` — per-game `try/except`, `analyzed_at` written after success, `try/finally`, `ON CONFLICT DO UPDATE`, `normalize_game_result` |
| `get_game_gaps` wrote `opening_games` from a GET, one connection + one live explorer call per row, freezing `0` (the "permanently buried blunder" bug) | `backend/repertoire.py:1417+` — read-only, in-memory resolution capped at `max_explorer_lookups=40` per request |
| `get_repertoire_tree` never exposed `max_explorer_lookups`; `db.py` invented "Italian Game"/"Sicilian Dragon" studies and hard-purged orphan moves on every boot | opt-in `init_db(purge_orphans=True)` + `FORBIDDEN_AUTO_STUDY_NAMES` |
| `analyzer` spawned one Stockfish per sync (`SimpleEngine.popen_uci`) | bounded process-wide pool in `backend/evaluator.py` (`get_sync_engine_pool`) |
| games with no `id` were inserted with a NULL primary key → dedup impossible, unbounded row growth | rejected with `failed_games`/`errors` |
| NULL `games.result` rendered as a `0-1` loss in the UI | `normalize_game_result` / `render_game_outcome` in `backend/api.py` |
| `played_at` stored in seconds by one path and milliseconds by another | `normalize_played_at` / `played_at_to_seconds` |
| frontend did not consume `is_patched` / `patched_count` / `earlier_mistake` | now consumed (verified in the current `repertoire_builder.js`) |

## Unproven suspicions

See `audit/reports/SYNC-99-unproven.md`.
