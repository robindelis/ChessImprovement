# SYNC-11 — an SSE client that walks away mid-sync is never observed until after the download finishes
- **Severity:** medium
- **Type:** race
- **File:** backend/repertoire.py
- **Line(s):** 1645-1690 (`stream_sync_recent_games`), 1904-1946 (`stream_analyze_user_games`)
- **Component:** SSE game sync
- **Repro script:** `audit/scripts/atk3_settings_resource_repro.py` (section ATK3-15b)
- **Detected by:** ATTACKER-3 (SYNC)

## Expected
`request.is_disconnected()` is polled before (and during) the blocking download, so
abandoned streams stop promptly and release their worker thread.

## Actual
`is_disconnected()` is only checked **inside the per-game loop**, i.e. after
`await asyncio.to_thread(self.lichess_client.fetch_user_games, ...)` has already returned.
`asyncio.to_thread` is not cancellable, and `fetch_user_games` uses a 60 s read timeout
(plus a `time.sleep(3)` + a 30 s retry on HTTP 429).

Observed (generator driven directly with a request whose `is_disconnected()` is `True` from
the first call):

```
  the client is 'disconnected' for the entire stream; timeline:
    + 0.00s  data: {"stage": "connecting", ...
    + 0.04s  data: {"stage": "fetching", "message": "Downloading latest games for R
    + 1.08s  data: {"stage": "completed", "message": "No recent games found on Lich
  is_disconnected() polled 0 times | total 1.08s
  -> the disconnect is only observed INSIDE the per-game loop
```

The stub's blocking fetch stood in for 1.04 s; with the real client that is up to 60 s
(93 s if Lichess answers 429). The 429 path also overwrites `resp` without closing the first
streaming response, so its file descriptor is only reclaimed by GC:

```
  fetch_user_games retry logic:
    79:  resp = requests.get(..., stream=True, timeout=60)
    80:  if resp.status_code == 429:
    82:      time.sleep(3)
    83:      resp = requests.get(..., stream=True, timeout=30)   # first resp never closed
```

## Root cause
Ordering inside the generator: the disconnect probe is placed after the only cancellable-
looking await, and `requests` (a blocking library) is run through `to_thread`, which
swallows cancellation for the duration of the call.

## Exploit / escalation
1. **Resource pinning:** open N abandoned sync streams → N pinned anyio worker threads for
   up to 60 s each, with no work done and no client to receive it. The default threadpool
   bound is small, so this also stalls ordinary API requests.
2. **Partial-write hazard:** if the fetch *does* return games, the loop `break`s on the
   first iteration *after* the first `INSERT` into an uncommitted transaction, and the
   `finally: conn.close()` rolls the whole batch back — the user who re-clicked sync gets a
   different result than the one that "ran".
3. **Same shape in `stream_analyze_user_games`** (line 1942), where the loop body is even
   more expensive (a full Stockfish analysis per game via `to_thread`).
4. **Adjacent:** `backend/evaluator.py` *does* poll `is_disconnected()` before acquiring the
   engine lock and between depths — the same discipline is simply missing here.
