# SYNC-10 — `max_games` is unbounded on the Stockfish-analysis endpoints, and `0`/negative/absent all mean "analyse the entire game history"
- **Severity:** medium
- **Type:** wrong-result
- **File:** backend/api.py
- **Line(s):** 1343 (`POST /api/repertoire/sync_games`), 1350 (`GET /api/repertoire/sync_games_stream`)
- **Component:** sync/analysis resource bounds
- **Repro script:** `audit/scripts/atk3_settings_resource_repro.py` (section ATK3-15a), `audit/scripts/atk3_reverify_matrix.py` (section `SYNC-15`)
- **Detected by:** ATTACKER-3 (SYNC)

## Expected
Consistent with every other bound in the file (`Query(15, ge=1, le=50)` for `/api/sync`,
`Query(20, ge=1, le=50)` for `/api/games/sync_recent`, `Query(30, ge=1, le=100)` for the
recent-games stream), the analysis endpoints cap `max_games`.

## Actual
```python
# backend/api.py:1343
def sync_repertoire_games(max_games: Optional[int] = Query(None)):
# backend/api.py:1350
    max_games: Optional[int] = Query(None, ge=1)
```
and in `backend/lichess_client.py:69-70`:
```python
if max_games and max_games > 0:
    params["max"] = max_games
```
so `None` (the default!), `0` and any negative value silently mean **"no `max` parameter at
all"** → Lichess streams the complete history.

Observed (the value actually forwarded to `LichessClient.fetch_user_games`):

```
  SYNC-15 max_games bounds
  max_games=1          HTTP 200 forwarded=1
  max_games=0          HTTP 200 forwarded=0
  max_games=-5         HTTP 200 forwarded=-5
  max_games=999999999  HTTP 200 forwarded=999999999
  no parameter at all    HTTP 200 forwarded= None
  [LIVE ] SYNC-15: 0/negative/absent max_games all mean 'download and analyse the entire history'
```

`/api/repertoire/sync_games_stream` is the worse of the two because it is the endpoint the
UI's progress bar uses, and its `ge=1` only rejects `0`/negatives *when they are passed
explicitly* — omitting the parameter still yields `None`.

## Root cause
Inconsistent bounding of the same knob across five endpoints, plus a
"falsy means unlimited" convention in `fetch_user_games` that is never validated.

## Exploit / escalation
1. **Unauthenticated CPU burn:** a single `POST /api/repertoire/sync_games` (no auth; CORS is
   `allow_origins=["*"]`) with no parameter queues a depth-12 Stockfish analysis
   (2–3 engine calls per user ply, `max_opening_ply` plies) for every game the account has
   ever played. For a few hundred games that is hours of pinned CPU.
2. **SSE variant:** the same through `GET /api/repertoire/sync_games_stream`, which holds a
   worker thread for the whole run (see SYNC-11).
3. **Related:** the same `get_setting("lichess_username") or "RobiDeli"` fallback means an
   unconfigured instance downloads *that* account's games instead of erroring.
