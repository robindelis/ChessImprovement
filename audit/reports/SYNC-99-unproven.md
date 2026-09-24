# SYNC-99 — Unproven suspicions (ATTACKER-3, SYNC)

Things I suspected but could **not** prove with a runnable repro inside the time box. Listed
honestly so a follow-up round does not have to re-derive them.

1. **`refreshTree()` has no request-sequencing guard → cross-colour tree race.**
   `frontend/repertoire_builder.js:897-905`:
   ```js
   const res = await fetch(`/api/repertoire/tree?color=${this.activeColor}&...`);
   this.treeData = await res.json();
   this.renderCoverageAndGaps();
   ```
   There is no `AbortController` and no request-id check, unlike `loadCandidateMoves()`
   (`:2091-2114`), which *does* guard with `candidateAbortController` and a
   `curr.fen !== targetFen` re-check. Two rapid `switchColor()` calls can therefore land
   out of order and leave `this.treeData` holding the **other colour's** coverage %,
   gap list and blunder list while `activeColor` says otherwise.
   *Why unproven:* it is a pure client-side race; proving it needs a real browser with
   controllable latency, and I am not allowed to bind a server port for this.

2. **`get_repertoire_tree()` calls `get_candidate_moves_2000()` twice per opponent position**
   (`backend/repertoire.py:1248` gaps loop and `:1270` coverage loop). I measured 3 explorer
   HTTP requests for a 2-position tree and 4 engine `analyse()` calls, i.e. the
   `explorer_cache` written by pass 1 absorbs the duplicate in the *warm* case.
   *Suspected but unproven:* on a **cold** cache the engine work may still be duplicated, and
   the two passes may disagree (pass 1 is uncached and stores per-move `eval`; pass 2 reads
   them back), so `coverage_pct` could be computed from slightly different data than the
   visible explorer table. I could not construct a case where the numbers actually differ.

3. **`/api/repertoire/game_gaps` still performs up to 40 sequential outbound explorer calls
   inside a GET** (`max_explorer_lookups=40`, `backend/repertoire.py:1417+`), each with a 6 s
   timeout in `get_explorer_game_count`. Worst case ≈ 240 s of a blocked request. The cap is
   *not* exposed as a query parameter, so a client cannot shorten it. I did not measure a real
   40-FEN cold request (that would need 40 real network round trips, which the brief forbids),
   so the 240 s figure is arithmetic, not observation.

4. **`games.result` is mid-migration to a score-string convention.** While I was working, the
   new `tests/fixtures.py` started seeding `result` as `'1-0' / '0-1' / '1/2-1/2'`, while
   `backend/api.py:normalize_game_result()` still returns `'white' / 'black' / 'draw'` and
   `repertoire.get_user_position_stats():145-147` still compares against `'white'`/`'black'`/
   `'draw'`. If the score-string convention reaches `backend/repertoire.py` before the
   comparison is migrated, **every** game is booked as a loss and every threat score becomes
   `N * 1.02`. I did not report it as a finding because it is plainly a transient state of
   another agent's in-flight migration, not a committed defect.

5. **`get_user_position_stats()`'s 60-second cache is keyed on nothing but the colour.**
   `backend/repertoire.py:98-102` caches `self._user_stats_cache[color]` for 60 s and
   `self._user_stats_time` is a *single* shared timestamp, so a call for `color="white"`
   refreshes the freshness stamp that also protects the `color="black"` entry (and vice
   versa). The invalidation in `sync_recent_games` / `analyze_user_games` clears the whole
   dict, so I could not construct a user-visible staleness window — but the shared timestamp
   is clearly wrong and will bite as soon as one code path stops clearing both.

6. **`/api/blunders/{id}` exposes `master_stats` verbatim to the client** (unvalidated
   upstream dict). Combined with the CORS `allow_origins=["*"]` this is a data-egress path,
   but I found no *write* primitive that uses it, so I could not escalate it beyond SYNC-08's
   variant list.

7. **`GameAnalyzer` never records an `opening_games` value in the `/api/sync` path** while
   `analyze_user_games` / `stream_analyze_user_games` do. After the `get_game_gaps` fix this
   no longer causes a permanent burial (the count is resolved in memory), but it does mean
   blanders written via `/api/sync` cost 40 extra explorer round trips on the next
   `game_gaps` request. Not a correctness bug any more; noted for the fixer's benefit.
