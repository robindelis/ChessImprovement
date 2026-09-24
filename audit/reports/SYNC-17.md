# SYNC-17 — RESIDUAL of SYNC-02: all four `repertoire.py` writers still store a draw as SQL `NULL`, the threat engine still books it as a **LOSS**, and two brand-new variants make the same root cause worse
- **Severity:** high
- **Type:** wrong-result
- **File:** backend/repertoire.py
- **Line(s):** 1949, 2041, 2322, 2430 (writes) and 185-190 (scoring)
- **Component:** game ingestion → personal threat statistics (AGENTS.md §4A) → Repertoire Builder candidate table
- **Repro script:** `audit/scripts/atk3_re02_writer_retest.py` (sections `RE02-A`…`RE02-D`)
- **Detected by:** ATK-3 (RETEST)

## Expected
Lichess emits `"winner": null` for a draw *and* for an unfinished/abandoned game, and the
`status` field is what distinguishes them. Whatever token is stored, the three consumers must
agree:
1. `GET /api/games/recent` (`render_game_outcome`, the UI),
2. `GET /api/dashboard` (`game_results` buckets),
3. `get_user_position_stats` (the threat formula `win_rate = (W + 0.5·D)/N`).

A draw must feed the formula as `draws`, and an unfinished game must not be counted at all.

## Actual
FIXER-B added `normalize_game_result()` / `render_game_outcome()` to **`backend/api.py` only**.
`backend/repertoire.py` still writes the raw value:

```python
# backend/repertoire.py:1949 (sync_recent_games), 2041 (stream_sync_recent_games),
#                        2322 (analyze_user_games),   2430 (stream_analyze_user_games)
user_color, g.get("winner", "draw"),
```
`dict.get(key, default)` returns the default only when the key is **absent**; Lichess sends the
key with a `null` value, so this yields `None` and SQLite stores `NULL`. And the consumer in
`repertoire.py` classifies `NULL` as a loss:

```python
# backend/repertoire.py:185-190
if res == u_color:      st["wins"] += 1
elif res == "draw":     st["draws"] += 1
else:                   st["losses"] += 1     # <-- NULL lands here
```

### Variant A — verified: a draw is a LOSS to the engine and `½-½` on screen
```
      stored: [{'id': 'w1', 'result': 'white', 't': 'text'}, {'id': 'd1', 'result': None, 't': 'null'}]
      threat engine says: {'games': 2, 'wins': 1, 'draws': 0, 'losses': 1, 'win_rate': 50.0, 'threat_score': 0.54}
      /api/games/recent says: {'w1': ('win', '1-0'), 'd1': ('draw', '½-½')}
      /api/dashboard game_results buckets: [{'result': 'draw', ...count: 1}, {'result': 'white', ...count: 1}]
   -> BROKEN 1W + 1D scores as 1W 1D (win_rate 75.0)   got {'games': 2, 'wins': 1, 'draws': 0, 'losses': 1, 'win_rate': 50.0, ...}
   -> BROKEN the UI and the threat engine agree on that game   UI=('draw', '½-½')  engine losses=1
```
All four writers store it. Expected `wins 1, draws 1, losses 0, win_rate 75.0, threat 0.44`.

### Variant B — NEW: an **abandoned** game is displayed as a DRAW
The writers drop `status` entirely. A game that was abandoned on move 3 has `winner: null` and
`status: "abandoned"`; it is neither a win, a loss, nor a draw.
```
      stored: [{'id': 'ab1', 'result': None}]
      /api/games/recent: draw -> an ABANDONED game is shown to the user as a DRAW
   -> BROKEN an abandoned game is not reported as a draw
```
`abandoned` games are extremely common in a real `recent games` list (queued games that never
started), and the same row is scored as a **loss** by the threat engine, so every abandoned
game permanently inflates the user's threat score for the line it was opened with.

### Variant C — NEW: an unrecognised `winner` token is stored verbatim, then read two different ways
```
      stored: [{'id': 'j1', 'result': 'white-wins-by-resignation'}]
        -> threat stats: {'games': 1, 'wins': 0, 'draws': 0, 'losses': 1, 'win_rate': 0.0, 'threat_score': 1.02}
      /api/games/recent renders it as: draw
```
`normalize_game_result` maps anything it does not recognise to `draw`; the threat engine maps
anything it does not recognise to `loss`. One row, two opposite verdicts, and the raw string is
persisted forever (it is also injected into `/api/dashboard`'s `game_results` grouping key, so
the dashboard grows one bucket per distinct junk value).

## Root cause
`normalize_game_result()` lives in `backend/api.py` and is not importable from
`backend/repertoire.py` without an import cycle (`api.py` imports `repertoire`). The
normalisation therefore has exactly one of its two call sites, and the module that owns the
*write* path was never migrated. The scoring side (`repertoire.py:185-190`) has no
`None`-handling at all, so it cannot even see the distinction.

## Exploit / escalation
1. **Historical rows stay wrong.** Every `NULL`/`result` row already in the user's DB — i.e.
   every draw and every abandoned game ever synced through `repertoire.py` — keeps being scored
   as a loss. Re-running `/api/sync` does not help, because `/api/sync` *skips* games whose
   `analyzed_at > 0` and those rows are never rewritten.
2. **Scale of the error.** For a user with 10 wins + 10 draws out of `1.e4`, the threat score
   is 5.4 instead of 3.6 — a **50 % overstatement** of the headline "biggest threat" number
   that drives the Repertoire Builder's candidate ordering
   (`get_candidate_moves_2000` → `moves_with_stats.sort(threat_score DESC)`).
3. **Direction of the error is always wrong.** A draw becomes a loss, so the app
   over-reports risk, never under-reports it. The user "fixes" a line that was never broken
   and leaves a real leak unfixed.
4. **`get_setting("lichess_username") or "RobiDeli"`** (1903, 1974, 2274, 2363) means an
   unconfigured instance ingests a *different* account, and then `user_color` derived from the
   name comparison is wrong too — every `res == u_color` test below mis-fires. This is the
   escalation SYNC-02 listed and it is still unfixed.

## Fix direction
Move `normalize_game_result()` into `backend/repertoire.py` (or `backend/db.py`) and call it
from all four writers; make it return four values (`win`/`loss`/`draw`/`unfinished`) and give
`get_user_position_stats` a fourth branch that *skips* `unfinished` games; drop
`INSERT OR REPLACE` in favour of `ON CONFLICT(id) DO UPDATE`; and back-fill
`games.result = 'draw'` where it is `NULL` in the boot migration (additive, idempotent).
