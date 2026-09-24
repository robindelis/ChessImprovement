# CODE-1 — Static audit of `backend/repertoire.py` (2715 lines)

- **Severity:** (see per-finding)
- **Type:** (see per-finding)
- **File:** backend/repertoire.py
- **Line(s):** various (each finding cites its own)
- **Component:** Repertoire Manager (tree, threat scoring, gaps, explorer cache, studies, trainer, seeding)
- **Repro script:** `audit/scripts/CODE-01_confirm.py`, `CODE-01_confirm2.py`, `CODE-01_confirm3.py`, `CODE-01_confirm4.py`, `CODE-01_confirm5.py` (all `import tests.test_setup` first, temp DB only, no HTTP to :38281)
- **Detected by:** CODE-1

## Summary

| ID | Severity | Type | Line | Title |
|----|----------|------|------|-------|
| CODE-1 | critical | data-loss | 521-552 | Re-adding an existing move deletes the row then re-inserts it → wipes notes, arrows, importance **and all spaced-repetition state** |
| CODE-2 | high | data-loss | 1865-1910 | `seed_default_repertoires(force=True)` destroys the user's prepared lines; guard is a TOCTOU `COUNT(*)`; success path returns `None` |
| CODE-3 | high | wrong-result | 2514-2520 | `LIMIT 1` with no `ORDER BY` over the 4-token/3-token/`LIKE` predicate → trainer validates (and increments counters on) the wrong row |
| CODE-4 | high | wrong-result | 1248, 1270 | `get_repertoire_tree` never forwards `color` → a **black** repertoire is scored with the **white** player's personal stats |
| CODE-5 | high | wrong-result / perf | 1247-1278 | `get_candidate_moves_2000` called **twice** per opponent position (2N explorer fetches + up to 16 extra Stockfish analyses per request) |
| CODE-6 | high | perf / data-loss | 1304-1321, 172-197 | `get_game_gaps` back-fills `opening_games` with one connection + one commit per row and a live HTTP call per FEN; failures silently store `0` and the blunder is then filtered out forever |
| CODE-7 | medium | duplication | 936, 1174, 1214, 2108 | 4 divergent copies of "index `repertoire_moves` by `parent_fen`" |
| CODE-8 | medium | duplication | 1469, 1561, 1714, 1822 | 4 copies of the `games` upsert; only 2 preserve `analyzed_at`; `played_at` unit drifts ms ↔ s |
| CODE-9 | medium | data-loss | 1714-1730 | `analyze_user_games` stamps `analyzed_at` **before** analyzing and has no per-game `try/except` → one failure marks a game analyzed forever and aborts the rest of the library |
| CODE-10 | medium | duplication | 1958, 2236, 2411, 2568 | Mastery classification written 4× (3 variants) |
| CODE-11 | medium | perf | 2108-2205, 2234-2238 | `queue.pop(0)` BFS + per-edge list copies + a duplicated 28-line metadata block; 5 whole-graph scans used only for `len()` |
| CODE-12 | medium | perf | 1642-1662 | `import_game_opening` opens/commits one connection per ply and always returns `status: success` |
| CODE-13 | medium | dead code | 1673, 1760 | `max_ply` parameter of both analyze entry points is never read; docstring says 15, reality is the `max_opening_ply` setting (24) |
| CODE-14 | medium | dead code / waste | 233-239, 202/273/369, 370 | Unreachable `eval_convention` sign-flip branch, 3× `chess.Board(fen)` per call, full re-scan of the user-stats dict |
| CODE-15 | medium | latent bug | 1056-1063 | Line-overlap scanner pushes **illegal** moves onto the board (python-chess allows it) → silent desync of the rest of the scan |
| CODE-16 | medium | wrong-result | 1971-1977 vs 2095, 2514-2520 | Study filter means "exact name" in `get_trainer_progress` but `LIKE %…%` in the graph/evaluator; the prepared-move lookup ignores the filter entirely |
| CODE-17 | medium | data-loss | 788-793 | `delete_study(delete_moves=True)` deletes by the `study_name` **column** only, with no child cascade → shared moves destroyed, tree orphaned |
| CODE-18 | medium | perf / contract | 100-102, 148-170 | Per-colour stats cache guarded by one **shared** timestamp (stale data served as fresh); full rebuild costs ~32 µs/ply (≈8 s for 3000 games) |

Additional (non-blocking) observations are listed at the end, plus `## Unproven suspicions`.

---

# CODE-1 — Re-adding an existing repertoire move wipes its notes, arrows, importance and spaced-repetition state
- **Severity:** critical
- **Type:** data-loss
- **File:** backend/repertoire.py
- **Line(s):** 478-552 (deletion at 521-525, metadata read at 528-537, upsert at 540-552)
- **Component:** Repertoire tree / Trainer (SRS)
- **Repro script:** `audit/scripts/CODE-01_confirm.py` (section 1 and 2)
- **Detected by:** CODE-1

## Expected
`add_repertoire_move` is the single write path for a repertoire node and is called on every import
(`import_game_opening`, `scout.import_line`) and by `POST /api/repertoire/move`. Re-adding a move that
already exists must be idempotent: the row, its `id`, its user metadata and its training history must be
preserved (`UNIQUE(repertoire, parent_fen, move_uci)` already makes the write an upsert).

## Actual
The "enforce one user move per position" block runs **before** the metadata is read back, and for the
already-present-and-identical case it *deletes the row*:

```python
521:            elif existing_user_move:
522:                cursor.execute("""
523:                DELETE FROM repertoire_moves
524:                WHERE repertoire = ? AND parent_fen = ? AND is_user_move = 1
525:                """, (repertoire, norm_parent))
526:
527:        # Check for existing metadata if not explicitly provided
528:        cursor.execute("""
529:        SELECT is_important, comment, arrows, study_name FROM repertoire_moves
530:        WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?
531:        """, (repertoire, norm_parent, move_uci))
532:        existing_meta = cursor.fetchone()
```

so `existing_meta` is always `None` in that branch, and the following `INSERT OR REPLACE`
(`repertoire.py:540-552`) names only 16 columns — every column it omits (`times_practiced`,
`times_correct`, `last_practiced`, `next_review`, `interval_days`, `created_at`) is reset to its
`DEFAULT`. Observed:

```
add  : success
[before re-add] id=1 is_important=1 comment='my precious note' arrows='[{"from":"e2","to":"e4"}]'
                 study_name='Anti French' times_practiced=2 times_correct=2 interval_days=3.0
re-add status: success -> returned is_important: False comment: '' arrows: ''
[after  re-add] id=2 is_important=0 comment='' arrows='' study_name='Anti French'
                 times_practiced=0 times_correct=0 last_practiced=0 next_review=0 interval_days=1.0
trainer progress after re-add: {'total_moves': 1, 'mastered_count': 0, 'learning_count': 0, 'unlearned_count': 1}
```

Re-importing a single already-imported game does the same (`import_game_opening` calls
`add_repertoire_move` for every ply, `repertoire.py:1653`):

```
[before import #2] id=3 is_important=1 comment='keep me' times_practiced=2 times_correct=2 interval_days=3.0
import #2: {'status': 'success', 'imported_moves': 5, ...}
[after  import #2] id=8 is_important=0 comment='' times_practiced=0 times_correct=0 interval_days=1.0
```

## Root cause
Delete-then-insert is used where an `UPDATE` (or omitting the columns from the insert list and reading
`existing_meta` *before* the delete) is required. Two independent losses stack: (a) the row is deleted
before `existing_meta` is read, and (b) `INSERT OR REPLACE` with an explicit column list resets all
omitted columns to the schema defaults (`backend/db.py:100-137`).

## Exploit / escalation
1. `POST /api/repertoire/import_game_opening` (`api.py:426`) on a game already imported → all notes,
   arrows and mastery for that line are gone; the UI button is at `frontend/repertoire_builder.js:1407`.
2. `backend/scout.py:786` imports a whole line/tree through the same function → one re-import of a
   scouted video wipes the annotations of every user move in it.
3. `update_move_metadata` (`repertoire.py:836`) delegates to `add_repertoire_move` when the row is
   missing, so a metadata write on a *just-pruned* node silently creates a fresh node with `is_important=0`.
4. Because the `id` changes, any in-flight trainer session keyed on move ids (`recent_ids` /
   `lapses` query params, `api.py:902-916`) silently loses its position.

---

# CODE-2 — `seed_default_repertoires(force=True)` wipes the user's prepared repertoire; the guard is TOCTOU and the success path returns `None`
- **Severity:** high
- **Type:** data-loss
- **File:** backend/repertoire.py
- **Line(s):** 1865-1910
- **Component:** Default seeding / `POST /api/repertoire/seed`
- **Repro script:** `audit/scripts/CODE-01_confirm5.py`
- **Detected by:** CODE-1

## Expected
Per `AGENTS.md` §9.3/§9.4, seeding must never prune a user-prepared line; the `force=False` guard should
make that guarantee, and the function should report what it did on every path.

## Actual
`force=True` deletes **every** prepared user move that shares a parent position with a seed line, and
the whole seed is unrecoverable:

```
[BEFORE force-seed (Anti-French 1.e4 e6 2.d4 d5 3.e5 c5 4.f3)] 7 rows
    1 e2e4 (u1) parent= rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR  anti-french prep
    5 e4e5 (u1) parent= rnbqkbnr/ppp2ppp/4p3/3p4/3PP3/8/PPP2PPP/RNBQK…
rows deleted by force=True: [('e2e4','anti-french prep'), ('e7e6',''), ('d2d4',''),
                            ('d7d5',''), ('e4e5',''), ('c7c5',''), ('g1f3','')]
seed_default_repertoires(force=False) -> {'status': 'skipped', …}   # guard does work without force
```

`3.e5` is replaced by the seed's `3.Nc3` (`repertoire.py:1884`) and the old subtree is cascade-pruned by
`add_repertoire_move`. Two further defects in the same function:

```python
1865:    def seed_default_repertoires(self, force: bool = False):
1869:        cursor.execute("SELECT COUNT(*) FROM repertoire_moves")
1870:        count = cursor.fetchone()[0]
1871:        conn.close()                      # <-- guard connection closed here
1872:        if count > 0 and not force:
1873:            return {"status": "skipped", …}
```
* the guard is a read-then-act with **no transaction and no lock**, so two concurrent
  `POST /api/repertoire/seed` calls (the endpoint is a sync `def`, i.e. Starlette threadpool,
  `api.py:965-970`) both observe `count == 0` and both seed; a seed racing with the user's
  `POST /api/repertoire/move` can prune the move the user just added;
* the function has **no `return` on the seeding path** (falls off the end of the method, line 1910) while
  the guard path returns a dict — `POST /api/repertoire/seed` therefore answers `200 null` on success and
  `200 {"status": "skipped"}` when it protected the data (confirmed:
  `seeding path returns: None` / `guard path returns: {'status': 'skipped', …}`);
* the 82 `add_repertoire_move` calls are 82 independent connections + commits, so an interruption leaves
  a half-seeded tree with the user's line already deleted (no rollback possible).

## Root cause
The seeding guard is advisory only; the destructive behaviour lives in `add_repertoire_move`'s
"replace the existing user move and prune its children" logic, which has no notion of "seed" vs "user".

## Exploit / escalation
1. `curl -X POST '127.0.0.1:38281/api/repertoire/seed?force=true'` (no auth, no confirmation in the
   endpoint) destroys the whole repertoire including comments/arrows/mastery.
2. Any future UI that adds a "load example repertoire" button wired to this endpoint becomes a
   one-click data-loss button.
3. Two simultaneous seed calls (or a seed + an add) defeat the `COUNT(*)` guard entirely.

---

# CODE-3 — `LIMIT 1` without `ORDER BY` over the 4-token/3-token/`LIKE` predicate makes the trainer validate against — and mutate — the wrong move
- **Severity:** high
- **Type:** wrong-result
- **File:** backend/repertoire.py
- **Line(s):** 2514-2520 (also 2393-2397, 2652-2661, 820-823, 594-609)
- **Component:** Trainer (`POST /api/repertoire/train/attempt`)
- **Repro script:** `audit/scripts/CODE-01_confirm4.py`
- **Detected by:** CODE-1

## Expected
The prepared-move lookup for a position returns *the* move the user prepared for that position, or
reports "not in tree". It must never return a move that belongs to a different position.

## Actual
`normalize_fen` keeps the en-passant token (`repertoire.py:23-29`, asserted by
`tests/test_repertoire.py:157-161`), so the same chess position can be stored under two different
4-token keys, while the lookup predicate deliberately ignores the 4th token:

```python
2514:            cursor.execute("""
2515:            SELECT rm.*, COALESCE(rs.name, rm.study_name) as effective_study FROM repertoire_moves rm
2516:            LEFT JOIN study_moves sm ON …
2517:            LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
2518:            WHERE rm.repertoire = ? AND (rm.parent_fen = ? OR rm.parent_fen = ? OR rm.parent_fen LIKE ?) AND rm.is_user_move = 1
2519:            LIMIT 1
2520:            """, (repertoire, norm_fen, norm_3, f"{norm_3} %"))
```

Two rows match and `LIMIT 1` picks the first by rowid:

```
P_EP   : rnbqkbnr/ppp2ppp/4p3/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq d6 | exd6 e.p. legal: True
P_NOEP : rnbqkbnr/ppp2ppp/4p3/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq -   | exd6 e.p. legal: False
identical 3-token prefix: True
rows in the white repertoire:
   id=1 parent_fen=…w KQkq -   move_uci=b1c3  'belongs to the OTHER position'
   id=2 parent_fen=…w KQkq d6  move_uci=g1f3  'correct prep'
EXACT SQL from repertoire.py:2514-2520:
  rows matched: [{'id': 1, 'move_uci': 'b1c3', 'move_san': 'Nc3'}]   <-- LIMIT 1 silently picks the first
  total rows the OR/LIKE predicate actually matches: 2
```

Both FENs are valid, legal positions (`1.e4 e6 2.e5 d5` vs. the same placement reached with a `d6-d5`
single push) and the repertoire legitimately contains both because of CODE-1's insertion path. The user
who plays the correct `Nf3` is told *"Not in your repertoire. Your prepared move is Nc3"*, and the
not-match branch (`repertoire.py:2558-2565`) commits `times_practiced = times_practiced + 1` **on row
id 1**, i.e. on the unrelated position's move.

Note the inconsistency that makes this reachable: the "one user move per position" guard uses strict
4-token equality (`repertoire.py:479-482`) and therefore did **not** prune the duplicate:

```
EXACT SQL from repertoire.py:479-482 (the 'ONE user move per position' guard):
  rows matched: [{'move_uci': 'g1f3'}]  <-- 4-token equality only
```

## Root cause
A "be liberal in what you accept" 3-token/`LIKE` fallback was bolted onto every position lookup
(5 sites) without adding a deterministic tie-breaker, while the invariant that creates duplicates
(`add_repertoire_move`) uses the stricter 4-token form. `fen LIKE '<3tok> %'` only ever matches rows
whose 4th token is an en-passant square (all other 4-token rows fail the equality tests), so the fuzzy
branch is simultaneously *useless for the common case* and *dangerous for the rare one*.

## Exploit / escalation
1. Import two games that transpose into the same position with/without a live ep square, then train —
   the trainer accepts the wrong move and corrupts the other position's SRS counters.
2. `delete_repertoire_branch` (`repertoire.py:594-609`) and `update_move_metadata`
   (`repertoire.py:820-823`) use the same predicate, so the same collision can **delete** or **re-annotate**
   a node belonging to a different position.
3. `get_next_trainer_drill`'s `last_move` lookup (`repertoire.py:2393-2397`) returns the animation arrow of
   a move from a different position, so the trainer UI shows a nonsensical "previous move".

---

# CODE-4 — `get_repertoire_tree` scores a black repertoire with the white player's personal stats
- **Severity:** high
- **Type:** wrong-result
- **File:** backend/repertoire.py
- **Line(s):** 1247-1278
- **Component:** Repertoire Builder — gaps & coverage (`GET /api/repertoire/tree`)
- **Repro script:** `audit/scripts/CODE-01_confirm2.py` (section C)
- **Detected by:** CODE-1

## Expected
`get_repertoire_tree(repertoire="black")` should attach the black player's personal stats/threat scores
to the opponent replies it lists, exactly as `GET /api/repertoire/explore?color=black` does
(`api.py:798-806`).

## Actual
Neither call site forwards the colour, so `get_candidate_moves_2000`'s default `color="white"`
(`repertoire.py:199`) is always used:

```python
1247:        for pos_fen, op_name, eco in opp_positions:
1248:            cand_data = self.get_candidate_moves_2000(pos_fen)      # <-- no color=
…
1269:        for pos_fen, _, _ in opp_positions:
1270:            cand_data = self.get_candidate_moves_2000(pos_fen)      # <-- no color=
```

```
repertoire rows: 6 -> candidate-moves calls: 6
 ('rnbqkbnr/pppppppp/8/8/4P3/8/', 'white')
 ('rnbqkbnr/pppp1ppp/8/4p3/4P3/', 'white')
 ('r1bqkbnr/pppp1ppp/2n5/4p3/2B', 'white')      <- every call, color='white'
```

Because `get_user_position_stats(color)` is what produces `my_stats`/`threat_score`
(`repertoire.py:345`), the black repertoire's gap table ranks *white* games' threat scores, and the
`user_games_total` field returned inside that payload is the count of **all** games
(`get_total_games_count`, `repertoire.py:408/1422`) regardless of colour.

## Root cause
The colour argument is dropped at the call sites instead of being threaded through; the endpoint
(`api.py:809-817`) does receive `color` and simply never forwards it.

## Exploit / escalation
1. Open the Builder with the black tab active (`frontend/repertoire_builder.js:899` sends
   `?color=black`) and read a gap ranking built from the opponent-of-the-user's games.
2. `coverage_pct` (line 1279) inherits the same wrong `popularity_pct` weights only, so coverage is
   unaffected, but the *gap ordering* the user acts on is.
3. Any future call that renders `my_stats` from `/api/repertoire/tree` will show the wrong W/D/L record.

---

# CODE-5 — `get_repertoire_tree` fetches the explorer twice per opponent position (2N HTTP/engine round-trips per page load)
- **Severity:** high
- **Type:** wrong-result (perf cliff)
- **File:** backend/repertoire.py
- **Line(s):** 1247-1278
- **Component:** Repertoire Builder — gap analysis
- **Repro script:** `audit/scripts/CODE-01_confirm2.py` (section A)
- **Detected by:** CODE-1

## Expected
One explorer lookup per opponent position, reused for both the gap list and the coverage metric.

## Actual
The two loops are independent and each calls the (expensive, engine-backed) fetcher; the first loop's
result is thrown away:

```python
1247:        for pos_fen, op_name, eco in opp_positions:
1248:            cand_data = self.get_candidate_moves_2000(pos_fen)
1249:            cand_moves = cand_data.get("moves", [])
1250:            prepared_replies = [reply["move_uci"] for reply in tree_by_parent.get(pos_fen, [])]
…
1269:        for pos_fen, _, _ in opp_positions:
1270:            cand_data = self.get_candidate_moves_2000(pos_fen)     # identical call, result #2
```

```
repertoire rows: 6 -> candidate-moves calls: 6     (3 positions x 2)
 ('rnbqkbnr/pppppppp/8/8/4P3/8/', 'white')
 ('rnbqkbnr/pppppppp/8/8/4P3/8/', 'white')        <- same FEN twice
```

Each cache miss costs a `requests.get(..., timeout=10)` **plus up to 8 synchronous Stockfish
`depth=10` analyses** (`repertoire.py:310-323`). With the documented 7-day cache
(`repertoire.py:222`) a cold tree of, say, 40 opponent positions performs 80 HTTP requests and up to
640 engine analyses in one request; the endpoint is a sync `def` (`api.py:808`), so it also occupies a
Starlette threadpool slot for the whole duration. `prepared_replies` at line 1250 and `prepared` at
line 1272 are the same list computed twice as well.

## Root cause
Straightforward copy-paste of the position loop; no memoisation of the explorer result inside the
request.

## Exploit / escalation
1. Open `/api/repertoire/tree` for a repertoire with many branches on a cold cache — multi-minute
   response, threadpool exhaustion (`atk5_dos_repro.py` territory).
2. Two browser tabs opening the builder simultaneously double the load again and share one
   non-thread-safe `chess.engine.SimpleEngine` (`repertoire.py:77-85`, `self._engine` on the singleton).

---

# CODE-6 — `get_game_gaps` writes to the DB from a GET, one connection+commit per row, one live HTTP call per FEN, and permanently buries failures as `0`
- **Severity:** high
- **Type:** perf / data-loss
- **File:** backend/repertoire.py
- **Line(s):** 1304-1321 (loop), 172-197 (`get_explorer_game_count`), 1325-1332 (filter)
- **Component:** Blunder gaps (`GET /api/repertoire/game_gaps`)
- **Repro script:** `audit/scripts/CODE-01_confirm3.py` (section H)
- **Detected by:** CODE-1

## Expected
Reading gaps should not mutate the database, and a transient network failure must not permanently
remove a user's blunders from the report.

## Actual
The function lazily *writes* to `blunders` from inside a read endpoint, with a fresh connection and a
fresh commit for every row:

```python
1305:        cursor.execute("SELECT id, fen_before FROM blunders WHERE opening_games IS NULL")
1307:        if missing_rows:
1308:            conn.close()
1309:            fen_cache = {}
1310:            for r in missing_rows:
1311:                f = r["fen_before"]
1312:                if f not in fen_cache:
1313:                    fen_cache[f] = self.get_explorer_game_count(f)     # live HTTP, timeout=6
1315:                c_conn = get_db()
1316:                c_cursor = c_conn.cursor()
1317:                c_cursor.execute("UPDATE blunders SET opening_games = ? WHERE id = ?", (c_cnt, r["id"]))
1318:                c_conn.commit()
1319:                c_conn.close()
```

```
25 NULL rows -> get_db() called 27 times, explorer lookups 1, 3.1 ms
after the backfill, opening_games is 0 for every row, so the >= min_games filter hides them forever:
 min_games=30 -> 0 gaps | min_games=0 -> 25 gaps (silent data loss)
```

`get_explorer_game_count` **reads** `explorer_cache` but never writes it:

```python
172:    def get_explorer_game_count(self, fen: str, ratings: str = "2000,2200,2500") -> int:
177:        cursor.execute("SELECT data_json FROM explorer_cache WHERE fen = ? AND ratings = ?", …)
189:        try:
190:            resp = requests.get(url, params=params, …, timeout=6)
191:            if resp.status_code == 200:
…
195:        except Exception:
196:            pass
197:        return 0
```

so every uncached FEN costs a live request on *every* page load, and any exception/timeout is stored as
`opening_games = 0` — after which `WHERE b.opening_games >= ?` (`repertoire.py:1329`) excludes the row
permanently, with no marker distinguishing "really unpopular" from "we failed to ask".

## Root cause
Read path and write path are fused; the cache it consults is never populated by the writer; and the
failure value (`0`) is indistinguishable from a legitimate `0`.

## Exploit / escalation
1. Blocker Lichess (429/timeout) once → every back-filled blunder gets `0` → `/api/repertoire/game_gaps`
   reports "no opening blunders" forever, and the user has no way to re-trigger the backfill
   (`opening_games IS NULL` is now false).
2. Fresh DB with 200 blunders: 200 sequential HTTP calls (up to 6 s each ≈ 20 min) inside one GET.
3. `max_games`/`min_games=0` variants are the only escape hatch; the UI's 30/50/100 dropdown
   (`AGENTS.md` §4.B) cannot recover the rows.

---

# CODE-7 — Four divergent copies of "index `repertoire_moves` by `parent_fen`"
- **Severity:** medium
- **Type:** duplication
- **File:** backend/repertoire.py
- **Line(s):** 936-947, 1174-1185, 1214-1225, 2108-2114
- **Component:** Line-overlap / tree / trainer graph
- **Repro script:** `audit/scripts/CODE-01_confirm2.py` (section F), `CODE-01_confirm4.py`
- **Detected by:** CODE-1

## Expected
One helper, e.g. `_index_moves_by_parent(rows) -> (exact, three_token)`, used by all four callers, with
one documented key convention.

## Actual
Four hand-rolled index builders, each slightly different:

* `check_line_overlap` (936-947) and `check_lines_overlap` (1174-1185) are **byte-identical duplicates**
  of each other (same SQL, same loop) — the only difference is the caller.
* `get_repertoire_tree` (1214-1225) merges both key spaces into **one** dict and de-duplicates with a
  linear scan over full row dicts:
  ```python
  1220:            p3 = " ".join(p.split()[:3])
  1221:            if p3 != p:
  1222:                if p3 not in tree_by_parent:
  1223:                    tree_by_parent[p3] = []
  1224:                if m not in tree_by_parent[p3]:      # O(n) full-dict comparisons
  1225:                    tree_by_parent[p3].append(m)
  ```
  This is O(n²) dict comparisons and it is also what makes the JSON payload contain every row twice
  (once under its 4-token key, once under its 3-token key) — for a 2000-move repertoire, 4000 entries.
* `build_repertoire_tree_graph` (2108-2114) keys **only** by the 3-token form.

## Root cause
No shared index abstraction; the key convention (`parent_fen` vs `p3` vs both) is decided per call site.

## Exploit / escalation
1. `check_lines_overlap` can be implemented as `[check_line_overlap(l) for l in lines]` over one index —
   today a 50-line import repeats the full-table index build 50 times if a caller ever changes it.
2. The mixed-key dict returned to the frontend (`repertoire_builder.js:1488,1916` only ever looks up the
   3-token key) means the 4-token keys and the O(n²) dedup are pure waste.
3. Any change to the key convention (e.g. adding the ep token) has to be made in four places and one
   will be missed — which is exactly how CODE-3 happened.

---

# CODE-8 — Four copies of the `games` upsert; only two preserve `analyzed_at`, and `played_at` units diverge
- **Severity:** medium
- **Type:** duplication
- **File:** backend/repertoire.py
- **Line(s):** 1469-1483, 1561-1575, 1714-1727, 1822-1835
- **Component:** Lichess sync / analysis
- **Repro script:** `audit/scripts/CODE-01_confirm.py` (section 3), `CODE-01_confirm3.py` (section K)
- **Detected by:** CODE-1

## Expected
One `_upsert_game(g)` helper. `played_at` in one unit. `analyzed_at` preserved by every writer.

## Actual
`INSERT OR REPLACE INTO games` appears 4×; `sync_recent_games`/`stream_sync_recent_games` protect the
analysis flag with a correlated subquery, the two analyze paths do not:

```
occurrences of 'INSERT OR REPLACE INTO games' in repertoire.py: 4
occurrences of "COALESCE((SELECT analyzed_at FROM games WHERE id = ?), 0)": 2
occurrences of 'g.get("createdAt", int(time.time() * 1000))': 2      # milliseconds
occurrences of 'g.get("createdAt", int(time.time()))': 2              # seconds  (same column!)
```

`played_at` therefore holds epoch-milliseconds when written by the sync paths (lines 1476, 1568) and
epoch-seconds when written by the analyze paths (lines 1721, 1829) whenever Lichess omits `createdAt`.
(The `COALESCE` subquery itself is correct — verified: an existing `analyzed_at=12345` survives the
sync upsert — so this is duplication/drift, not a lost flag.)

## Root cause
Copy-paste of a 15-line statement block; the `createdAt` fallback was written twice with different units.

## Exploit / escalation
1. Any date sorting/filtering over `games.played_at` is wrong for whichever subset was written by the
   other path (a factor of 1000).
2. A future column added to `games` must be added in four places; missing one silently nulls it
   (e.g. `opening_name` is filled from the local `OpeningBook` in the sync paths but written as
   `opening_data.get("name","")` in the analyze paths — `repertoire.py:1725` vs `1465`).

---

# CODE-9 — `analyze_user_games` stamps `analyzed_at` before analyzing and has no per-game error handling
- **Severity:** medium
- **Type:** data-loss
- **File:** backend/repertoire.py
- **Line(s):** 1714-1730 (compare the streaming twin at 1822-1851)
- **Component:** Blunder analysis (`POST /api/repertoire/sync_games`)
- **Repro script:** `audit/scripts/CODE-01_confirm.py` (read-only proof of the statement order)
- **Detected by:** CODE-1

## Expected
A game is marked analyzed only after its blunders are persisted, and a failure on one game does not
abort the remaining library.

## Actual
```python
1726:                g.get("moves", ""), int(time.time())     # analyzed_at = now
1727:            ))
1728:            conn.commit()
1729:
1730:            blunders = analyzer.analyze_game_openings(g)  # <-- unprotected; may raise
```
The streaming variant wraps the same call in `try/except` (`repertoire.py:1839-1851`); the batch variant
does not. If `analyze_game_openings` raises (engine crash, malformed PGN, network hiccup in the
`get_explorer_game_count` precompute at line 1735 which *is* wrapped), then:
* the game stays `analyzed_at != 0`, so the incremental skip at `repertoire.py:1705` will never retry it —
  its blunders are lost forever;
* the exception leaves the `for` loop, so every remaining game in the library is never analyzed;
* `analyzer.close()` / `conn.close()` at 1742-1743 never run (leaked engine subprocess + connection).

## Root cause
Ordering bug plus a missing `try/finally`; the streaming path shows the author knew the right shape.

## Exploit / escalation
1. Kill the Stockfish process mid-run (`bin/stockfish/…` deleted/renamed) → the whole batch stops after
   the first unanalyzed game and that game is permanently marked done.
2. Trigger it via `POST /api/repertoire/sync_games` (no auth) to make blunder analysis permanently
   incomplete while `/api/repertoire/game_gaps` still reports `total_games_analyzed` as if fine.

---

# CODE-10 — Mastery classification written four times (three different variants)
- **Severity:** medium
- **Type:** duplication
- **File:** backend/repertoire.py
- **Line(s):** 1958-1969, 2236-2238, 2411-2418, 2568, 2607
- **Component:** Trainer progress / drill selection / evaluation
- **Repro script:** n/a (static; all four sites quoted)
- **Detected by:** CODE-1

## Expected
One `@staticmethod classify(times_practiced, times_correct) -> "mastered"|"learning"|"unlearned"`.

## Actual
```python
1958:        def categorize_move(m):
1961:            if tc >= 2 and tc == tp:  return "mastered"
1965:            elif tc > 0:              return "learning"
1968:            else:                     return "unlearned"
```
```python
2236:        mastered_nodes = [n for n in graph.values() if (n["move"].get("times_correct") or 0) >= 2 and n["move"].get("times_correct") == n["move"].get("times_practiced")]
2237:        learning_nodes = [n for n in graph.values() if (n["move"].get("times_correct") or 0) > 0 and not ((…) >= 2 and … == …)]
2238:        unlearned_nodes = [n for n in graph.values() if (n["move"].get("times_correct") or 0) == 0]
```
```python
2413:        if tc >= 2 and tc == tp:  mastery = "mastered"
2415:        elif tc > 0:              mastery = "learning"
2417:        else:                     mastery = "unlearned"
```
```python
2607:            mastery = "mastered" if (new_tc >= 2 and new_tc == new_tp) else "learning"   # no "unlearned" arm
2568:                mastery = "learning" if new_tc > 0 else "unlearned"
```
The five lists built at 2234-2238 exist only to produce `len()` values in `queue_summary`; the
`learning_nodes` predicate is a double negative that is exactly `categorize_move(m) == "learning"`.
`get_trainer_progress` and `get_next_trainer_drill` can therefore disagree about the same move
(they don't today, but nothing keeps them in sync), and `unlearned_pct` at line 1987 is computed as
`100 - mastered - learning` rather than from the classifier.

## Root cause
No shared vocabulary for the SRS states; each consumer re-derives it from the two counters.

## Exploit / escalation
1. Change the mastery rule (e.g. require `tc >= 3`) in one place and the trainer UI, the progress bars
   and the drill scheduler silently disagree.
2. `queue_summary["unlearned_count"]` (line 2238) counts `tc == 0` while `mastered + learning +
   unlearned != total_moves` is possible for a corrupted row (`tc > tp`), which is the case
   `tests/attacker_round2_creative_probe.py:304` already probes.

---

# CODE-11 — Trainer graph BFS is quadratic and the per-node metadata block is duplicated verbatim
- **Severity:** medium
- **Type:** perf
- **File:** backend/repertoire.py
- **Line(s):** 2108-2133, 2135-2150 vs 2176-2203, 2234-2238
- **Component:** `build_repertoire_tree_graph` / `get_next_trainer_drill`
- **Repro script:** `audit/scripts/CODE-01_confirm2.py` (section F)
- **Detected by:** CODE-1

## Expected
`collections.deque` for the queue, one shared node-metadata builder, and counts accumulated in a single
pass.

## Actual
```python
2128:        while queue:
2129:            curr_fen3, depth, path_san, path_moves = queue.pop(0)     # O(n) pop
2130:            for m in by_parent.get(curr_fen3, []):
2132:                new_path_san = path_san + [m["move_san"]]             # copy per edge
2133:                new_path_moves = path_moves + [m]                     # copy per edge
```
and the 15-line block computing `is_due` / `urgency` / `studies` / `effective_study`
(`2135-2150`) is copy-pasted for the "disconnected moves" fallback (`2178-2203`) with two behavioural
differences (`ply_depth = 99`, `ancestors = []`, `prerequisites_met = True`, `is_frontier = (tp == 0)`
without the `prereqs_met` conjunct). Then `get_next_trainer_drill` walks the whole graph five more
times (2234-2238) purely to take `len()`.

## Root cause
Breadth-first search written with list semantics and no node-metadata helper.

## Exploit / escalation
1. `get_next_trainer_drill` is called on every trainer request (`api.py:917`); cost is
   `O(E·depth)` list copies + `O(V)` scans ×5, on top of the two `SELECT DISTINCT rm.*` queries with
   `LEFT JOIN study_moves` (which multiply rows when a move belongs to several studies).
2. `SELECT DISTINCT rm.*, COALESCE(rs.name, rm.study_name)` (lines 2090/2099) cannot dedupe when a move
   belongs to two studies, because the coalesced column differs — so `all_moves` contains duplicates
   and the fallback loop at 2176 can process the same `id` twice.

---

# CODE-12 — `import_game_opening` opens and commits a connection per ply and always reports success
- **Severity:** medium
- **Type:** perf / contract
- **File:** backend/repertoire.py
- **Line(s):** 1642-1671
- **Component:** `POST /api/repertoire/import_game_opening`
- **Repro script:** `audit/scripts/CODE-01_confirm3.py` (section J)
- **Detected by:** CODE-1

## Expected
One connection/transaction for the whole import, and an `error` status when nothing could be imported.

## Actual
```python
1653:            res = self.add_repertoire_move(              # opens+commits its own connection
1654:                repertoire=repertoire, parent_fen=p_fen, move_uci=uci, is_user_move=is_user_turn)
1659:            if res.get("status") == "success":
1660:                imported += 1
…
1664:        return {
1665:            "status": "success",                            # <-- unconditional
1666:            "imported_moves": imported,
```
```
import result: 7 moves -> 8 separate DB connections opened
```
Each `add_repertoire_move` also runs an `OpeningBook.get_opening_for_fen` lookup and, when replacing a
user move, up to 3 queries **per level** of the recursive prune (`repertoire.py:505-520`). Errors are
counted as "not imported" and still reported as `status: success`, and
`import_game_opening` raises `ValueError` for bad input (line 1611/1620) while every sibling returns an
error dict — the two conventions are mixed inside one file.

## Root cause
The bulk import reuses the single-move write path instead of a batch writer.

## Exploit / escalation
1. A 16-ply import with a mid-line user-move replacement performs dozens of connections and commits,
   each a separate WAL fsync.
2. `max_ply` is unbounded (`target_ply = min(max_ply, len(san_tokens))`), so
   `POST /api/repertoire/import_game_opening {"max_ply": 200}` on a long game turns into 200
   connections plus 200 `OpeningBook` lookups in one request.

---

# CODE-13 — `max_ply` is a dead parameter in both analyze entry points (and the docstring is wrong)
- **Severity:** medium
- **Type:** dead code
- **File:** backend/repertoire.py
- **Line(s):** 1673, 1674, 1760
- **Component:** Analysis pipeline
- **Repro script:** grep (below)
- **Detected by:** CODE-1

## Expected
Either the parameter is forwarded to the analyzer, or it is removed.

## Actual
```python
1673:    def analyze_user_games(self, max_games: Optional[int] = None, max_ply: int = 30) -> Dict:
1674:        """Fetches and analyzes opening moves (up to move 15) for user's games. …
1760:        max_ply: int = 30,
```
```
$ grep -n "max_ply" backend/repertoire.py
1607:    def import_game_opening(self, game_id: str, repertoire: str, max_ply: Optional[int] = None) -> Dict:
1632:        if max_ply is not None and max_ply > 0:      # only import_game_opening uses it
1633:            target_ply = min(max_ply, len(san_tokens))
1673:    def analyze_user_games(..., max_ply: int = 30)   # never read in the body
1760:        max_ply: int = 30,                          # never read in the body
```
Both callers pass it explicitly (`api.py:1017`, `api.py:1027`), and the real limit comes from
`get_setting("max_opening_ply", "24")` inside `GameAnalyzer.analyze_game_openings`
(`backend/analyzer.py:97`). The docstring's "up to move 15" matches neither 24 nor 30.

## Root cause
Parameter added for configurability, never wired to the component that owns the setting.

## Exploit / escalation
1. A caller who sets `max_ply=8` (thinking they are limiting analysis depth) silently gets 24 plies.
2. `AGENTS.md` §4.B's "opening moves (up to move 15)" narrative is derived from this stale docstring.

---

# CODE-14 — Unreachable `eval_convention` branch, three `chess.Board(fen)` builds per explorer call, full re-scan of the user-stats dict
- **Severity:** medium
- **Type:** dead code / waste
- **File:** backend/repertoire.py
- **Line(s):** 230-245 (dead branch), 202 & 273 & 369 (Board), 367-390 (re-scan)
- **Component:** `get_candidate_moves_2000`
- **Repro script:** `audit/scripts/CODE-01_confirm2.py` (section C)
- **Detected by:** CODE-1

## Expected
One `chess.Board` per call; the eval convention is normalised once, at write time.

## Actual
The only writer always stamps `"eval_convention": "white"` (line 332) and `backend/db.py:212-224`
rewrites every legacy cache row to `"white"` at startup, so this branch can never fire:

```python
233:            if cached_payload.get("eval_convention") != "white":
234:                parts = fen.strip().split()
235:                if len(parts) >= 2 and parts[1] == 'b':
236:                    for m in cached_payload.get("moves", []):
237:                        if m.get("eval") is not None:
238:                            m["eval"] = -m["eval"]
239:                cached_payload["eval_convention"] = "white"
```
```
$ grep -rn "eval_convention" backend/
backend/db.py:212, 223                      # migration normalises to "white"
backend/repertoire.py:233, 239, 332         # check / set / write-"white"
```
`board = chess.Board(fen)` is built at line 202 (validation), again at 273 (fresh path) and again at
369 (sidelines); the sidelines block then iterates the **entire** user-stats index
(`for (p_fen, uci), my_st in user_stats.items():`) to find the handful of entries whose `p_fen` equals
`norm_fen` — O(total distinct positions in the library) per explorer call, where a single dict lookup
on a pre-grouped index would do. The doubled `existing_ucis` set is also built for a list that was
already a copy.

## Root cause
Defensive code retained after the invariant was made true elsewhere; per-call state rebuilt instead of
grouped once.

## Exploit / escalation
1. With a 3000-game library the user-stats dict holds tens of thousands of keys; every
   `/api/repertoire/explore` and every `/api/repertoire/tree` position pays a full scan of it
   (CODE-5 doubles this).

---

# CODE-15 — Line-overlap scanner pushes illegal moves onto the board and keeps going
- **Severity:** medium
- **Type:** latent bug
- **File:** backend/repertoire.py
- **Line(s):** 1056-1063
- **Component:** `check_line_overlap` / `check_lines_overlap` (scout import)
- **Repro script:** `audit/scripts/CODE-01_confirm.py` (section 4)
- **Detected by:** CODE-1

## Expected
A move that is not legal in the current position ends the scan (or is reported), never advances the
board.

## Actual
```python
1056:            if move_obj and move_obj in curr_board.legal_moves:
1057:                curr_board.push(move_obj)
1058:            elif uci:
1059:                try:
1060:                    mo = chess.Move.from_uci(uci)
1061:                    curr_board.push(mo)          # <-- pushed without a legality check
1062:                except Exception:
1063:                    pass
```
`python-chess` does **not** validate in `push`:
```
push('e2e5') ACCEPTED -> fen: rnbqkbnr/pppppppp/8/4P3/8/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1
legal_moves now: 19
```
So an attacker- or parser-supplied `uci` that parses but is illegal silently rewinds the board to a
non-chess position, and every later iteration computes `is_user_turn` (995-996) and SAN (984-991) from
it — the overlap verdict and the divergence message are then derived from a fiction. Note the loop
`break`s on an *unparsable* move (129-134 in `get_user_position_stats`) but not here.

## Root cause
`move_obj in legal_moves` guards only the first branch; the fallback branch has no guard, and a `break`
is used nowhere in the loop.

## Exploit / escalation
1. `POST /api/scout/line_overlap` (and `/batch`, `api.py:1072/1105/1081/1114`) with a hand-crafted
   `moves[]` whose 3rd entry is a legal-format but illegal UCI → the remaining plies are classified
   against a bogus position, so `already_present` / `partially_present` can be forged.
2. Variant: pass `fen_before` values that disagree with `uci`; `fen_before` wins for the lookup
   (line 973) while the board advances by `uci`, so the two drift apart with no error.
3. Variant: `san` only (no `uci`) with an illegal SAN → `parse_san` raises, `move_obj` stays `None`,
   nothing is pushed, but `uci` stays `""` and the move is still recorded as "new" (1042-1047).

---

# CODE-16 — The study filter means three different things, and the trainer's "prepared move" lookup ignores it
- **Severity:** medium
- **Type:** wrong-result
- **File:** backend/repertoire.py
- **Line(s):** 1971-1977, 2087-2096, 2533, 2514-2520
- **Component:** Trainer (drill selection vs. evaluation)
- **Repro script:** n/a (static; all sites quoted)
- **Detected by:** CODE-1

## Expected
One definition of "move belongs to study X" (exact name), used by progress, drill selection and answer
checking.

## Actual
* `get_trainer_progress` requires an **exact** name:
  ```python
  1971:        filter_clean = (study_name or "").strip()
  1974:            target_moves = [m for m in all_user_moves
  1976:                           if filter_clean in move_studies_map.get((m["parent_fen"], m["move_uci"]), set())]
  ```
* `build_repertoire_tree_graph` uses a **substring** match on three different columns:
  ```python
  2095:              AND (rs.name = ? OR rs.name LIKE ? OR rm.study_name = ? OR rm.study_name LIKE ? OR rm.opening_name = ? OR rm.opening_name LIKE ?)
  ```
* `evaluate_trainer_move` computes `active_study` (2533) and uses it only to pick the *opponent reply*
  (2613-2621); the `prepared` row it validates against (2514-2520) has **no** study predicate at all.

Consequences: (a) a drill issued for study "Anti French" can be answered with a move that only exists in
another study and be marked correct; (b) `get_trainer_progress(study="French")` reports 0 moves while
`get_next_trainer_drill(opening="French")` happily returns drills, because the graph matched
`opening_name`/`LIKE %French%` — and "Anti French" also matches `%French%`;
(c) `tests/test_user_behavior_probe.py:346-373` exercises exactly the exact-match path, so the
divergence is untested.

## Root cause
`study_name` is a free-text column *and* a join table (`study_moves`) *and* an opening-name fallback;
three code paths each pick a different resolution rule.

## Exploit / escalation
1. Create study "Caro" and "Caro-Kann" → `LIKE '%Caro%'` matches both, so drills interleave between
   studies while the progress bars count only the exact one.
2. `DELETE /api/repertoire/studies/{id}?delete_moves=true` (see CODE-17) then leaves `rm.study_name`
   pointing at a study that no longer exists → the graph falls back to `opening_name`
   (`fallback = … or m.get("opening_name")`), so drills keep being labelled with a stale study.

---

# CODE-17 — `delete_study(delete_moves=True)` deletes by the `study_name` column, ignoring `study_moves`, with no child cascade
- **Severity:** medium
- **Type:** data-loss
- **File:** backend/repertoire.py
- **Line(s):** 777-799 (delete at 788-793)
- **Component:** Studies (`DELETE /api/repertoire/studies/{study_id}`)
- **Repro script:** `audit/scripts/CODE-01_confirm.py` (pattern reused; logic is a straight read)
- **Detected by:** CODE-1

## Expected
`delete_moves=True` removes the moves that belong to that study (via `study_moves`), and the removal is
consistent with the tree (children pruned, as `delete_repertoire_branch` does).

## Actual
```python
788:            if delete_moves:
789:                cursor.execute("""
790:                DELETE FROM repertoire_moves
791:                WHERE study_name = ? AND repertoire = ?
792:                """, (study["name"], study["repertoire"]))
793:
794:            cursor.execute("DELETE FROM study_moves WHERE study_id = ?", (study_id,))
```
Problems:
1. Moves associated with the study **only** through `study_moves` (whose `repertoire_moves.study_name`
   is empty or holds a different study) survive → the study row is gone but the moves remain, now
   orphaned from any study.
2. Moves whose `study_name` column happens to equal the study name but which are *also* linked to a
   second study are destroyed, taking that other study's line with them.
3. There is **no** recursive child prune (contrast `delete_repertoire_branch`, 615-653), so every
   continuation of a deleted move is orphaned; `get_repertoire_tree`'s `moves_by_parent` and
   `build_repertoire_tree_graph`'s BFS will then surface moves whose parents no longer exist
   (the `root_parents` fallback at 2117-2120 and `ply_depth = 99` nodes at 2193).
4. `delete_study` has `try/finally: conn.close()` with **no** `except`, so any driver-level error
   (e.g. a locked DB) propagates to a `500` after the `DELETE FROM repertoire_moves` has already been
   staged; the single `conn.commit()` at line 796 is the only thing keeping the delete all-or-nothing,
   and there is no compensating re-link of the destroyed moves.

## Root cause
Study membership is modelled twice (`repertoire_moves.study_name` and `study_moves`) and the delete
path uses the weaker of the two, without reusing the tree-prune helper.

## Exploit / escalation
1. `DELETE /api/repertoire/studies/2?delete_moves=true` (no auth) after linking one move to two studies
   → the shared move and its subtree are destroyed for the surviving study.
2. Variant: create a study, link a move via `update_move_metadata(study_name=…)` (which writes both the
   column and `study_moves`), then rename nothing and delete with `delete_moves=false` → `study_moves`
   rows go, `repertoire_moves.study_name` still says the deleted study → every graph node reports the
   deleted study as `effective_study` (fallback at line 2148).

---

# CODE-18 — Per-colour stats cache guarded by one shared timestamp; full rebuild is O(all plies)
- **Severity:** medium
- **Type:** perf / contract
- **File:** backend/repertoire.py
- **Line(s):** 68-69, 100-102, 110-170
- **Component:** Personal threat statistics
- **Repro script:** `audit/scripts/CODE-01_confirm2.py` (B), `CODE-01_confirm3.py` (G, G2)
- **Detected by:** CODE-1

## Expected
Each colour's cache entry carries its own timestamp and is rebuilt when *that* entry is older than the
TTL.

## Actual
The cache is per colour but the clock is global:

```python
100:        now = time.time()
101:        if color in self._user_stats_cache and (now - self._user_stats_time < 60):
102:            return self._user_stats_cache[color]
…
168:        self._user_stats_cache[color] = result
169:        self._user_stats_time = now
```
Any request for the other colour refreshes `_user_stats_time` and thereby un-expires the stale entry:
```
get_user_position_stats('white') -> {'SENTINEL': True}   (1-hour-old entry served; 0 SQL executed)
```
Cost of a rebuild (full library walk, `parse_san` per ply, plus a `normalize_fen(board.fen())` string
build per ply at line 125):
```
4400 plies over 200 games -> 140 ms, 19 distinct (fen,uci) keys
=> ~32 us per ply; a 3000-game library (~250k plies) costs ~8.0 s per rebuild
```
The TTL is a magic `60` with no constant, and the cache is invalidated by hand in four places
(686-687, 1489-1490, 1602-1603, 1746-1747, 1860-1861) instead of being derived from `analyzed_at`.

## Root cause
A single `_user_stats_time` attribute shared by two independently-cached keys.

## Exploit / escalation
1. Alternate `?color=white` / `?color=black` on `/api/repertoire/explore` after the TTL expires → every
   request pays the full 8 s rebuild (and blocks a threadpool slot each time).
2. Because staleness is unbounded in one direction, the threat scores shown next to a candidate move can
   be arbitrarily old while the UI implies they are current.

---

## Additional observations (real but non-blocking)

1. **`unpatched_gaps` and `unpatched_count` are the same expression computed twice**, each re-running
   `normalize_fen()` over every row (`repertoire.py:1415-1416`); the loop above already computed the
   same predicate into `is_patched` (1366). Observed: `unpatched_gaps = 25  unpatched_count = 25`.
2. **Response payload blow-up in `get_game_gaps`**: the full `all_blunders` list of a game is embedded
   in *every* gap of that game (`1397`) and the same list object is shared, so 25 gaps of one game each
   carry 25 blunders (observed `gaps entries for game perf0001: 25 | all_blunders: 25 | gaps: 25`).
   With `include_patched=True` the patched items are also emitted twice (1402-1407: appended to `gaps`
   *and* `patched_gaps`).
3. **`total_games_analyzed` is `COUNT(*) FROM games`** (`1358-1359`, reported at 1412) — the name
   promises "analyzed", the value counts every stored game including never-analyzed ones.
4. **`moves_by_parent` is returned with mixed key semantics** (`1214-1225`): 4-token and 3-token keys
   in one dict, every row serialised twice, and the frontend only ever looks up the 3-token form
   (`frontend/repertoire_builder.js:1425-1427, 1488, 1916`). `findMoveInTree`'s first branch
   (`moves_by_parent[parentFen]`) can never hit, because `parentFen` is a 6-token board FEN while the
   stored keys are 4/3-token.
5. **`RepertoireManager.close()` (87-93) is never called** — `grep -rn "rm\.close\|manager\.close"`
   returns nothing, while `_get_engine()` (77-85) spawns a real Stockfish process
   (`chess.engine.SimpleEngine.popen_uci`) that is never quit. The API lifespan only closes
   `AsyncEngineEvaluator` (`api.py:81`).
6. **`get_repertoire_tree` line 1250 vs 1272** compute the same `prepared` list twice, and
   `prepared_replies`/`prepared` use `tree_by_parent.get(pos_fen)` (4-token) while the index also
   contains 3-token keys — the gap/coverage logic therefore only ever sees exact-parent rows.
7. **`get_studies` issues two byte-identical queries** differing only by the `WHERE s.repertoire = ?`
   clause (`690-717`); `COUNT(DISTINCT sm.parent_fen || ':' || sm.move_uci)` is redundant given the
   `study_moves` primary key.
8. **`get_repertoire_openings` line 2452**: `"move_count": s["user_moves"] or s["total_moves"] or 0`
   reports the *opponent-branch* count for a study that contains no user moves at all.
9. **`random.shuffle(unseen_studies)` (2275)** contradicts the comment above it ("followed by seen
   studies ordered by least recently trained") and makes drill selection non-reproducible: the same
   DB + same request can return different studies.
10. **`create_study` double-closes the connection** (`conn.close()` at 768 inside `except` and again in
    `finally` at 774) and the `status: "exists"` branch returns `get_study_by_name`'s row **without**
    the `total_moves`/`user_moves` keys the success branch adds (752-766) — an inconsistent response
    contract for the same endpoint.
11. **Policy constants are hardcoded in several places and can drift**: the explorer bands
    `"2000,2200,2500"` (172, 199, `api.py:798`), the speeds `"blitz,rapid,classical"` (188, 252), the
    `7 * 86400` cache TTL (222), the 60 s stats TTL (101), the `60`-second and `6 s`/`10 s` HTTP
    timeouts (190, 259), `min_games=30` (1196, 1295, `api.py:812/990`) and `gap_threshold=5.0`
    (1196, `api.py:811`) — while the *same* `gap_threshold` also exists as a user setting
    (`db.py:233`, `api.py:178/194`) that `get_repertoire_tree` never reads.
12. **`get_explorer_game_count`'s `ratings` parameter is never passed by any caller** (only the default
    is ever used), while `get_candidate_moves_2000` writes the cache under whatever `ratings` the client
    supplied (`api.py:800` accepts an arbitrary string) — the two features can therefore read and write
    different cache rows for the same position.
13. **`update_move_metadata` builds SQL by f-string** (`864-868`) from a fixed whitelist of four literal
    clauses — not injectable, but the `updates` list is unnecessary since all four columns could be
    written unconditionally.
14. **`clear_repertoire_database` wraps `DELETE FROM sqlite_sequence` in `try/except: pass`**
    (680-683) for a table that always exists when the AUTOINCREMENT tables do — dead defensive code.

## Unproven suspicions

1. **Shared, non-thread-safe Stockfish across requests.** `self._engine` is a single
   `chess.engine.SimpleEngine` on the `RepertoireManager` singleton, used from `get_candidate_moves_2000`
   (311-323), which is reached from **sync** `def` endpoints (`api.py:798-806`, `809-817`) that
   Starlette runs in a threadpool. `SimpleEngine.analyse` is not documented as thread-safe; concurrent
   `/api/repertoire/explore` calls plausibly corrupt the UCI stream. I did not reproduce it
   (needs a real engine + true parallelism); `audit/scripts/atk5_explore_concurrency_repro.py` and
   `atk5_engine_repro.py` from the attacker fleet are the right place to look.
2. **Concurrent `add_repertoire_move` can create two user moves for one position.** The invariant is
   enforced by `SELECT … LIMIT` → `DELETE` → `INSERT` (`479-552`) with no transaction and no unique
   index on `(repertoire, parent_fen, is_user_move)`; `backend/db.py:134` only has
   `UNIQUE(repertoire, parent_fen, move_uci)`, which permits `e2e4` and `d2d4` at the same parent.
   Two concurrent `POST /api/repertoire/move` calls (threadpool) can interleave between the SELECT and
   the INSERT. Not reproduced here (needs thread-level interleaving on a real server).
3. **`chess.Board(m["fen"])` failures silently drop positions** (`1238-1241`): a corrupted `fen` column
   removes that node from `opp_positions`, so its coverage contribution disappears and `coverage_pct`
   silently rises. Plausible only with a corrupted/legacy DB; the db.py migration that re-inserts
   `study_moves` rows writes an **un-normalised** `r["parent_fen"]`
   (`backend/db.py:285-291`, no `normalize_fen`), which is the most likely way such a row would appear.
4. **`get_max_depth` shares one `visited` set across sibling branches** (`1069-1085`), so a transposed
   node's depth is counted along whichever path reached it first; `repertoire_deepest_ply` and the
   "extends N plies deeper" message can therefore under-report. Only affects a UI message, and I could
   not construct a case where it changes `status`.
5. **`normalize_fen` returns `fen.strip()` unchanged for a malformed FEN** (`23-34`): the `except`
   branch returns the raw string, which then flows into `parent_fen`. `add_repertoire_move` validates
   first (433-442) so the write path is safe, but `get_candidate_moves_2000`/`evaluate_trainer_move`
   normalise *before* validating, so a malformed `fen` becomes a cache key / lookup key that silently
   matches nothing.

## Reproduction commands

```bash
/home/robin/miniconda3/bin/python3 audit/scripts/CODE-01_confirm.py    # CODE-1, CODE-12, CODE-15
/home/robin/miniconda3/bin/python3 audit/scripts/CODE-01_confirm2.py   # CODE-4, CODE-5, CODE-18
/home/robin/miniconda3/bin/python3 audit/scripts/CODE-01_confirm3.py   # CODE-6, CODE-12, CODE-18
/home/robin/miniconda3/bin/python3 audit/scripts/CODE-01_confirm4.py   # CODE-3
/home/robin/miniconda3/bin/python3 audit/scripts/CODE-01_confirm5.py   # CODE-2
```
All scripts import `tests.test_setup` before any backend module, run against the throwaway temp DB, and
make no HTTP request to `127.0.0.1:38281`. They are idempotent (they `DELETE` the rows they create).
