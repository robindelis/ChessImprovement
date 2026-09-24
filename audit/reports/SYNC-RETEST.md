# SYNC-RETEST — re-exploitation round for ATK-3 (sync / blunders)

**Agent:** ATK-3 (RETEST) — territory unchanged: `backend/lichess_client.py`,
`backend/analyzer.py`, `backend/db.py`, and the game-ingestion / blunder / dashboard paths in
`backend/api.py` + `backend/repertoire.py`.

**Isolation:** every script imports `tests.test_setup` first via `audit/scripts/atk3_lib.py`.
`chess_improvement.db` was never opened (only ever named, for the path assertion in
`atk3_lib`). Port 38281 was never contacted — all traffic through `TestClient(app)`. No real
Lichess and no real Stockfish: `atk3_lib.restore_network()` installs a tripwire so any probe
that reaches for `requests` outside a stub raises, and the analyzer's engine was replaced by
`GameAnalyzer.analyze_game_openings` stand-ins. The only engine touched was `python-chess`'s
`Score`/`PovScore` classes, which need no process.

**Snapshot discipline.** `backend/db.py` was being edited live by FIXER-N throughout this
round and changed content at least three times under me (21:08, 21:14, …). Every claim below
about `db.py` is therefore tied to a stated observation, and the two `db.py` claims (SYNC-01,
SYNC-15) were re-verified last, with the fix present.

---

## 1. Which fixes hold

| Original | Status | Creative variant I tried against it | Result |
|---|---|---|---|
| **SYNC-01** `best_pv` never created by the schema | **HOLDS** | (a) a *fresh* `init_db()` on a brand-new file, counting columns and indexes; (b) a hand-built **legacy** DDL (23 columns, no indexes, one existing row) → `init_db()`; (c) a real `save_blunders_to_db()` against the fresh schema | 24 columns, `best_pv` last; legacy DB gains it and keeps its row; real insert succeeds. See `atk3_re01_schema_retest.py` RE01-A/RE01-B. |
| **SYNC-15** no uniqueness on `blunders` | **HOLDS for the write path, INCOMPLETE overall** | (a) a legacy DB *already containing* duplicates → does boot collapse them, and does it keep the copy with the most drill practice? (b) **8 concurrent** `save_blunders_to_db` calls for one game from 8 threads; (c) the same key with `game_id = NULL` ×4; (d) a **re-analysis that no longer detects the row** | (a) collapses 3→1, survivor is the one with `attempts=3`, drill rows merged. (b) 1 row, 0 errors. (c) **4 identical rows — the index does not constrain NULL** → new finding **SYNC-18**. (d) **the stale row survives** → new finding **SYNC-19**. |
| **SYNC-02** draws stored NULL, scored as a loss | **PARTIAL — the write side is untouched** | (a) drove all four `repertoire.py` writers directly with `"winner": null`; (b) read the result back through `get_user_position_stats`, `/api/games/recent` **and** `/api/dashboard`; (c) **`"status": "abandoned"`** (winner null but not a draw); (d) a **garbage `"winner"` token** | (a) all four store `NULL`. (b) engine says 1W/0D/**1L** while the UI says `½-½`. (c) an abandoned game is shown as a **draw**. (d) stored verbatim, read as a **loss** by the engine and a **draw** by the UI. → **SYNC-17** with two new variants. |
| **SYNC-03** one non-dict NDJSON line aborts the batch | **HOLDS at the reader, BROKEN at the writers** | 23 hostile streams: valid-after-invalid, invalid-before-valid, array/string/number/null/true roots, truncated final line, BOM (line 1 and line 2), CRLF, bare CR, NUL byte, 1-element dict, 40-deep nesting, duplicate ids ×2/×3, a 1 MB+ over-long line first **and** last, exactly-at-the-cap, and 5001 games | The reader is now correct on all 23 (non-dict and undecodable lines are skipped, a valid line *after* a bad one is kept, the whole batch survives). But `"moves":[…]`/`"speed":{…}`/`"winner":{…}`/`"createdAt":[…]`, which the reader passes through as a **dict/list**, still raise `sqlite3.ProgrammingError` inside the writer → 500 on `/api/sync`, and a **leaked connection with a live write transaction** in `repertoire.py` → **SYNC-22** (new measurement: a second writer is blocked the full 30 s). BOM on line 1 still silently drops the first game. |
| **SYNC-05 / SYNC-04** `Infinity`/`NaN` poisons `explorer_cache`; 5 malformed shapes → 500 | **NOT FIXED (the claimed api.py boundary does not exist)** | (a) re-ran all 10 payloads through `/api/repertoire/explore`; (b) grepped for every caller of `get_candidate_moves_2000`; (c) drove the payloads through **`/api/repertoire/tree`**, which calls the same writer internally; (d) planted one poisoned row and read it back through `get_explorer_game_count`, `/api/repertoire/tree`, `/api/blunders/{id}` and `analyze_user_games`; (e) planted 5 hand-written `data_json` bodies; (f) a 20 000-char `ratings` cache key | `explore_position_2000` still validates only the FEN; `get_candidate_moves_2000` still does unguarded `data.get("white",0) + …`. 4 shapes 500 on **both** endpoints; `Infinity`/`NaN` still persist for 7 days. **`get_repertoire_tree:1701` is the bypass.** One poisoned row 400s/500s the tree, 422s the blunder review, and makes `analyze_user_games` raise `OverflowError`, and persists a REAL into `blunders.opening_games`. → **SYNC-20** (new). |
| **SYNC-06** mate collapse / "Checkmate in 1" | **HOLDS at every boundary I could construct** | `Cp(±0, -9500, -8999, -9001, -100000, +100000)`, `Mate(1/2/3/50)`, `Mate(-1/-2)`, `Mate(0)`, all read from **both** povs; `pov()` sign flip; the real Fool's-mate terminal FEN from both sides; end-to-end `_generate_tactical_explanation` text; `cpl_loss` over all four sentinel combinations; the "moves not plies" wording at `mate ∈ {-1,0,1,2,3,99}` | `Cp(-9500)` → `""` (no mate). `Mate(1/2/3)` → three distinct strings. `Mate(0)` → "checkmate is already on the board". `Cp(-100000)` → `""`. `cpl_loss` always in `[0, 10000]`, so a mate can no longer own `ORDER BY cpl_loss DESC` forever. "mate in 1" reads as 1, not 0. **One latent trap found** → **SYNC-23**: `normalize_score(x, color=None)` reports a plain `PovScore` as a mate (python-chess 1.11.2's `PovScore` has neither `.mate()` nor `.score()`); not reachable today, so `low`. |
| **SYNC-07/10** unbounded `limit` / `max_games` | out of scope this round (no probe run) | — | — |
| **SYNC-08 / SYNC-09 / SYNC-11 / SYNC-12 / SYNC-13 / SYNC-14** | out of scope this round | — | — |
| **CODE-02-04** `analyzed_at` before analysis | **HOLDS in `POST /api/sync`, BROKEN in `repertoire.py`** | drove `analyze_user_games` with an engine that raises, and drove `POST /api/repertoire/sync_games` with the same | `repertoire.py` still stamps + commits `analyzed_at = int(time.time())` at 2324/2432 and analyses at 2328; the row survives as `analyzed_at=1790449954` and the endpoint 500s the whole library. → **SYNC-16**. |
| **ABUSE-14** scout ReDoS | out of scope (scout domain, ATK-4) | — | — |

---

## 2. New findings (all in `audit/reports/`)

| ID | Severity | File:Line | Title |
|---|---|---|---|
| **SYNC-16** | high | `backend/repertoire.py:2313-2326`, `2421-2439` | `analyze_user_games` stamps + commits `analyzed_at` before analysing; one bad game 500s the whole library and leaks the engine handle |
| **SYNC-17** | high | `backend/repertoire.py:1949, 2041, 2322, 2430` + `185-190` | Residual of SYNC-02: draws still stored NULL and still scored as **losses**; **new** — abandoned games shown as draws, garbage `winner` tokens stored verbatim and read two different ways |
| **SYNC-18** | high | `backend/repertoire.py:2292-2326`, `2396-2434`; `backend/analyzer.py:419-436` | An id-less record grows an identical `games` **and** `blunders` row on every sync — `ux_blunders_natural_key` cannot dedup `NULL` |
| **SYNC-19** | high | `backend/analyzer.py:471-524`, `405-436` | The SYNC-15 upsert made re-analysis idempotent, not correct: blunder rows a re-analysis no longer detects are never removed, and `analyzed_at` guarantees they never will be |
| **SYNC-20** | high | `backend/repertoire.py:1696-1722`, `234`, `1800`, `330-393` | `/api/repertoire/tree` calls `get_candidate_moves_2000` internally, so the api.py explorer guard protects nothing; one poisoned `explorer_cache` row breaks 4 other endpoints |
| **SYNC-21** | high | `backend/repertoire.py:1946, 2038, 2319, 2427`; `backend/api.py:95-110, 170-190, 1084` | One `"createdAt": Infinity` is stored verbatim and makes `GET /api/games/recent` answer **400 for the whole tab**, unrepairably |
| **SYNC-22** | high | `backend/repertoire.py:1940-1957`, `2032-2066`, `2313-2331`, `2421-2434` | An exception mid-batch leaks the connection **with a live write transaction**: every other writer is blocked for the full 30 s timeout |
| **SYNC-23** | low | `backend/analyzer.py:110-118` | `normalize_score(x, color=None)` reports an ordinary `PovScore` as "checkmate is already on the board" |

## 3. Reproducers (all idempotent, re-runnable, isolated)

```
audit/scripts/atk3_re01_schema_retest.py   # SYNC-01, SYNC-15, their new holes
audit/scripts/atk3_re02_writer_retest.py   # SYNC-02 residual, CODE-02-04 residual, NULL-id
audit/scripts/atk3_re03_ndjson_retest.py   # SYNC-03, 23 hostile streams x 4 writers
audit/scripts/atk3_re04_mate_retest.py     # SYNC-06 boundary + wording hunt
audit/scripts/atk3_re05_explorer_retest.py # SYNC-04/05, the tree-route bypass
audit/scripts/atk3_re06_gameslist_retest.py# played_at writers, write-on-GET, lock leak
```

## 4. The single most serious thing still open

**SYNC-19 — a blunder the engine has stopped detecting is never removed from the database, and
`analyzed_at` guarantees it never will be.** It is the only one of the eight that *silently and
indefinitely corrupts the user's study data with no error anywhere*: the gap list keeps ranking
it, the spaced-repetition trainer keeps drilling it, `mastered_count` in `/api/dashboard` counts
it, and the user is told nothing. The SYNC-15 fix made the write idempotent, which is what
created the false confidence that re-analysis is now safe. Every other open finding produces a
visible 4xx/5xx or a visibly wrong number; this one produces a plausible-looking repertoire
built on blunders that no longer exist, and the recovery is a manual `DELETE`.
