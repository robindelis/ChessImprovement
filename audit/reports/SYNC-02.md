# SYNC-02 — `"winner": null` (a draw) is still stored as SQL NULL by all four repertoire.py sync paths and then scored as a LOSS by the threat engine
- **Severity:** high
- **Type:** wrong-result
- **File:** backend/repertoire.py
- **Line(s):** 1626, 1718, 1871, 1979 (writes) and 146 (scoring)
- **Component:** game ingestion → personal threat statistics
- **Repro script:** `audit/scripts/atk3_winner_threat_repro.py`, `audit/scripts/atk3_reverify_matrix.py` (section `SYNC-02`)
- **Detected by:** ATTACKER-3 (SYNC)

## Expected
Lichess emits `"winner": null` for a draw. A draw must be persisted as a draw and must
feed the AGENTS.md §4A threat formula as `draws`, not as `losses`:
`win_rate = (wins + 0.5*draws)/N`.

## Actual
`POST /api/sync` was fixed to use `normalize_game_result()` (`backend/api.py:438`), and the
*read* path now renders a NULL `games.result` as a draw (`render_game_outcome`). But the
**write** paths in `repertoire.py` were not touched — they still use
`g.get("winner", "draw")`, which returns `None` when the key is present with a null value:

```python
# backend/repertoire.py:1626 (sync_recent_games), 1718 (stream_sync_recent_games),
#                        1871 (analyze_user_games),   1979 (stream_analyze_user_games)
user_color, g.get("winner", "draw"),
```

`get_user_position_stats()` then classifies the NULL as a loss:

```python
# backend/repertoire.py:145-147
if res == u_color:      st["wins"] += 1
elif res == "draw":     st["draws"] += 1
else:                   st["losses"] += 1     # <-- NULL lands here
```

Observed (isolated temp DB, one win + one draw, both `1.e4`):

```
  stored: [{'id': 'd0', 'result': None, 't': 'null'}, {'id': 'w0', 'result': 'white', 't': 'text'}]
  /api/games/recent says: [('w0', 'win', '1-0'), ('d0', 'draw', '½-½')]
  threat engine says    : {'games': 2, 'wins': 1, 'draws': 0, 'losses': 1, 'win_rate': 50.0, 'threat_score': 0.54}
  [LIVE ] SYNC-02: result NULL stored=True; (1W+1D should be draws=1 win_rate=75.0 threat=0.44)
```

The UI shows `½-½` while the threat engine has booked the game as a loss — an internal
contradiction inside one screen. Scaled up (the earlier run with 10 wins + 10 draws):

```
   20 games of 1.e4, 10 wins + 10 draws (stored as NULL):
   stats for (start,e2e4) = {'games': 20, 'wins': 10, 'draws': 0, 'losses': 10, 'win_rate': 50.0, 'threat_score': 5.4}
   expected: 10W 10D 0L  win_rate 60.0  threat = 20*((0.40)^2+0.02) = 3.6
```

A 50 % overstatement of the headline "threat score" for a draw-heavy repertoire.

## Root cause
`dict.get(key, default)` returns `default` only when the key is **absent**; Lichess sends the
key with a `null` value. `g.get("winner", "draw")` therefore yields `None`, and
`normalize_game_result()` exists in `backend/api.py` but is not used by `repertoire.py`.

## Exploit / escalation
1. **Legacy rows:** the same `else: losses += 1` also mis-scores every NULL row already in
   the database from before the read-path fix, so historical data stays wrong until the
   games are re-imported.
2. **Second reader, same bug:** `GET /api/dashboard`'s `game_results` groups by the raw
   `games.result`, so a NULL forms its own bucket and never appears as a draw.
3. **Adjacent variant:** `g.get("winner", "draw")` also accepts an *arbitrary* string from
   the upstream JSON (`"winner": "unknown"`, `"winner": 7`, …) and stores it verbatim,
   because only `None`-vs-string is distinguished downstream.
4. **Related fallback:** `sync_recent_games` and friends use
   `get_setting("lichess_username") or "RobiDeli"`, so a mis-configured instance silently
   ingests a *different* account's games, and every `user_color` derived from the name
   comparison is then wrong.
