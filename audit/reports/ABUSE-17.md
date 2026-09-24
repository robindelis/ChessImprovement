# ABUSE-17 — The 2000+ Explorer lookup has no request coalescing and no negative cache: 24 concurrent requests become 24 upstream calls, and an upstream failure is reported to the user as "this position has no master games"
- **Severity:** medium
- **Type:** security (resource amplification against a third party) / race / wrong-result
- **File:** `backend/repertoire.py`
- **Line(s):** 263-320 (`get_candidate_moves_2000`: cache read at 280-292, unconditional fetch at 310-328, conditional write at 388-390); 1698-1703 (`get_tree_coverage`'s **per-call** `cand_cache`, so a burst of `/api/repertoire/tree` multiplies the fan-out); route at `backend/api.py:1813`
- **Component:** Lichess 2000+ Masters Opening Explorer integration
- **Repro script:** `audit/scripts/atk5_24_explorer_stampede.py` (amplification + missing negative cache), `audit/scripts/atk5_25_explorer_failure_lies.py` (the wrong-result half, end-to-end through the API)
- **Detected by:** ATK-5

## Expected

`get_candidate_moves_2000` is a *read-through cache in front of somebody else's free public API*. Two properties
follow, and neither holds today:

1. **Single-flight.** If the position is not cached yet, N concurrent requests for it must produce **one**
   upstream fetch; the other N-1 should wait for it and share the result. Amplifying one logical question into N
   HTTP requests against `explorer.lichess.org` is an unintended request multiplier pointed at a third party
   who did not opt in, and the app is entirely unauthenticated on the loopback interface.
2. **Failures must be distinguishable from facts.** If the explorer is unreachable, times out, or answers
   5xx, the user must not be told that the position has **zero** games in the 2000+ database — and the failure
   must not be re-attempted in full on every subsequent request.

## Actual

### 1. The stampede (`atk5_24_explorer_stampede.py`, isolated temp DB, network stubbed to count and to cost 0.35 s)

```
[A] cold cache, 24 concurrent requests for the SAME position
    24 concurrent requests, one cold position
    outbound explorer calls actually made : 24
    amplification                         : 24x
    per-position call counts              : {'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1': 24}

[B] the same 24 requests AFTER the cache is warm (the control)
    outbound calls with a warm cache      : 0        <- control is clean, so [A] is the app's behaviour
```

The multiplier is not just outbound HTTP: line 375 calls `self._evaluate_candidate_moves(board, candidate_moves)`
on **every** miss, so each redundant fetch also queues up to 8 Stockfish analyses for the top moves — against a
pool capped at `CHESS_ENGINE_ASYNC_MAX=1` (ABUSE-12's `EngineBusy` territory) and `SyncEnginePool` cap 2.

This compounds with the already-queued **CODE-6** hand-off recorded in `audit/STATE.md`: `get_tree_coverage`
de-duplicates with a **per-call** dict (`repertoire.py:1698-1703`), so one `GET /api/repertoire/tree` can spend
one explorer call *per distinct opponent position in the tree* (up to the documented ~40), and a burst of
concurrent tree requests multiplies that again.

### 2. No negative cache — a failure is re-paid in full, forever

```
[C] does a FAILED fetch get cached? (negative caching)
    3 sequential calls on a FAILING position -> 3 outbound calls (a negative cache would make this 1)
    payload returned to the caller          : total_games=0 moves=0
    rows cached for the failing position    : 0  (0 = never remembered)

[C2] same on a *reachable but empty* position (total_games == 0)
    2 sequential calls on a 0-game position -> 1 outbound calls
    rows cached                              : 1        <- the cache logic is fine; only failures leak
```

The write is conditional, the miss is not remembered:

```python
# Cache result only if request was successful and has moves (or genuine zero games)
if req_success and (len(candidate_moves) > 0 or total_games == 0):
```

so every failure is a permanent cache miss. Measured cost of that (`[E]`, 1 s simulated read timeout):

```
    1st request 1.00s   2nd request 1.00s   (upstream attempts so far: 2)
    -> the timeout is paid again on every single request; nothing is remembered.
```

### 3. Wrong result: an upstream failure is indistinguishable from a real "0 games" answer

This is the half that reaches the user. `data` is pre-seeded with zeros and `req_success` stays `False` for any
non-200 or any exception, so every failure path falls through to the *same* zero payload that a genuine empty
position produces. Driven end-to-end through `GET /api/repertoire/explore` (`atk5_25_explorer_failure_lies.py`):

```
[A] HTTP 503 from the explorer, twice
    upstream calls made : 2  (2 requests, 2 calls -> nothing cached)
    response 1 : {"fen":"...","total_games":0,"white_total":0,"draws_total":0,
                  "black_total":0,"user_games_total":12,"moves":[]}
    response 2 : (identical)

[B] a HEALTHY explorer returning a genuine 0-game reply
    response : {"fen":"...","total_games":0,"white_total":0,"draws_total":0,
                "black_total":0,"user_games_total":12,"moves":[]}

    failure keys  : ['black_total','draws_total','fen','moves','total_games','user_games_total','white_total']
    empty    keys : ['black_total','draws_total','fen','moves','total_games','user_games_total','white_total']
    identical key set: True
    total_games  failure=0   genuine-empty=0
    moves        failure=[]  genuine-empty=[]
    error        failure=None  genuine-empty=None      <- there is no error field at all
    cached       failure=None  genuine-empty=None

[C] a rich, healthy position, for contrast
    response : {"total_games":800,"white_total":400,...,"moves":[{"san":"c5","games":600,
               "popularity_pct":75.0,...}]}

[D] a ConnectionError (the most common real failure -- no HTTP status at all)
    upstream attempts : 1
    response : {"total_games":0,"moves":[]}   <- identical to "no master games exist here"
```

So a dropped connection, a read timeout, a 503 and a genuine empty database all produce the **same bytes** on
the wire, and there is no `error` / `degraded` / `stale` field a client could branch on. The only trace of the
failure is a `print` on the server's stdout (`repertoire.py:328`, `Lichess explorer query failed: timed out`),
which the Electron window does not show. Downstream this silently changes the product's headline numbers:
`get_tree_coverage` weights gaps by `popularity_pct` and the same zero payload is what
`/api/repertoire/game_gaps` and the `opening_games` cutoff (§4B.3) read, so **an explorer outage reads as
"this opening is not played by anyone above 2000"** rather than "we could not check".

## Root cause

Three read-through-cache invariants are all missing from one function:

* **No in-flight de-duplication.** `repertoire.py:277-283` reads the cache, `:310` falls through to the fetch,
  `:388` writes. Between the read and the write there is no lock, no per-FEN "someone is already fetching this"
  marker, and no shared promise/condition — so the window is the full duration of the HTTP call. The class only
  has `_instance_lock` and `_seed_lock` (`repertoire.py:90, 98`); there is no lock anywhere near the cache.
* **No negative caching / no failure state.** The single `if req_success and ...` guards the *write* but nothing
  records the failure, so a broken upstream converts every later request into another full-timeout request.
* **A failure sentinel that is also a valid value.** `data = {"white": 0, "draws": 0, "black": 0, "moves": []}`
  (`:320`) is simultaneously "upstream failed", "upstream said 200 with no games", and "upstream said 200 with
  `total_games: 0`". The function returns it in all three cases with no discriminator.

## Exploit / escalation

How it was reached: `GET /api/repertoire/explore?fen=<a legal FEN the cache has never seen>` — the FEN is fully
attacker-chosen, so an attacker picks positions that are guaranteed cold. The origin boundary (ABUSE-03) means
this is a loopback/local client action, not a drive-by from a web page — but the app has **no auth and no
per-caller budget by design**, so anything that can reach the port (Electron's own renderer, a local script, a
`file://` page, anything on the LAN if the user ever forwards the port) can drive it.

Adjacent weaker variants of the same root cause:

1. **Position-walk amplification.** Because the FEN is free-form, a client can walk N *distinct* legal FENs and
   get N× the outbound calls, with no dedup possible even with single-flight. There is no rate limit and no
   per-process outbound budget, so the only ceiling is the third party's patience.
2. **Tree fan-out as a multiplier.** `GET /api/repertoire/tree` (via `get_tree_coverage`, `repertoire.py:1703`)
   spends one call per distinct opponent position. Combined with variant 1's uncached positions this is the
   ~40-call-per-request behaviour already queued as CODE-6 — but note it is *worse* than CODE-6 assumes, because
   the stampede means N concurrent tree requests do not share any of it.
3. **Engine-pool exhaustion as a side effect.** Every redundant miss also enqueues up to 8 Stockfish analyses
   (`:375`). With the sync pool at 2 and the async pool at 1, a modest stampede converts into `EngineBusy` /
   503s for *legitimate* work — i.e. the cheap-to-send request degrades the expensive work.
4. **Silent freeze of the cache.** SYNC-05 (queued) is `Infinity`/`NaN` in one explorer reply poisoning
   `explorer_cache` for 7 days. This finding is the mirror image: a *failed* fetch poisons nothing but is also
   remembered by nobody, so it repeats forever. Both need the same "record the fetch outcome" fix.
5. **`get_explorer_game_count` (`repertoire.py:218`) is the same shape** and is on the `opening_games` /
   `min_games` cutoff path, so the same stampede and the same failure-as-zero lie apply there.

## Suggested fix shape (for the `repertoire.py` owner)

* **Single-flight:** a process-wide `dict[(norm_fen, ratings)] -> threading.Event` (or a `Lock` per key). First
  caller fetches; the rest `wait()` and then read the cache. Must be `finally`-safe or a single hung fetch
  wedges that FEN forever — the same trap ABUSE-12 documents for the shared `SimpleEngine`.
* **Record the failure:** write a short-TTL "unavailable" marker (e.g. 30-60 s) or at least an in-memory
  backoff keyed by FEN, so a broken upstream costs one timeout per window instead of one per request.
* **Disambiguate the sentinel:** return an explicit `error` / `degraded: true` (and keep `total_games` honest)
  when `req_success` is false, and let the UI say "could not reach the 2000+ database" instead of "0 games".
  This is a `frontend/` display change too, so it should be handed to the frontend owner.
* Note for triage: the *per-tree-call* ceiling (CODE-6) and this finding are the same function and should be
  fixed in one pass, sequentially — both are in `repertoire.py`, which is under exclusive single-file ownership.
