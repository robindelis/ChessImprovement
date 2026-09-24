# BUILDER-06 — `GET /api/repertoire/tree` has unbounded latency: one 10 s-timeout explorer call per opponent position, no cap
- **Severity:** medium
- **Type:** crash
- **File:** backend/repertoire.py
- **Line(s):** 1317-1415 (`get_repertoire_tree`), 268-278 (`requests.get(..., timeout=10)` inside `get_candidate_moves_2000`), 175-216 (`get_explorer_game_count`, `timeout=6`)
- **Component:** Repertoire Builder — tree refresh / coverage gauge / gap list
- **Repro script:** `audit/scripts/atk1_b09_tree_cost.py` (part b1)
- **Detected by:** ATTACKER-1 (BUILDER)

## Expected

`GET /api/repertoire/tree` is called by `refreshTree()` on every study change, on every
tab switch, and after **every** `saveMove()` / `removeBranch()`. It should be bounded: a
repertoire tree endpoint that fans out to a third-party HTTP API once per node must have an
overall deadline, a per-call budget, or a "serve gaps from cache only" mode, so that a
degraded/absent network cannot make the Builder look frozen.

## Actual

The gap/coverage loop calls the explorer once per opponent position (the de-duplication via
`cand_cache` is correct), and each call is an independent `requests.get(..., timeout=10)`.
There is no aggregate deadline, no concurrency, and no per-endpoint time budget. Measured
with a stubbed transport that sleeps 0.5 s and then fails (standing in for an unreachable
Lichess API with the real 10 s timeout):

```
   repertoire rows=5    /api/repertoire/tree took   1.7s   (=  2.5 explorer calls x 0.5s)
   repertoire rows=13   /api/repertoire/tree took   3.8s   (=  6.5 explorer calls x 0.5s)
   repertoire rows=24   /api/repertoire/tree took   6.2s   (= 12.0 explorer calls x 0.5s)
```

Strictly linear in the number of opponent positions. With the shipped `timeout=10`, a 24-move
repertoire is **~120 s** per `refreshTree()`; a 200-move repertoire is ~17 minutes. Because
`saveMove()` does

```javascript
await this.refreshTree();
await this.loadCandidateMoves(parentFen, isUserMove);
```

the candidate table is not even requested until the tree call returns, so the whole Builder
appears unresponsive for the duration. The same unbounded pattern exists a few lines further
down: `get_repertoire_tree` → `get_game_gaps` → the `SELECT id, fen_before FROM blunders WHERE
opening_games IS NULL` loop, which issues one `get_explorer_game_count()` (6 s timeout) per
distinct un-populated blunder position.

Additionally, `moves_by_parent` in the response emits **every row twice** — once under its
4-token `parent_fen` key and once under a 3-token alias key — so the payload the frontend
re-downloads on every refresh is doubled for nothing:

```
   repertoire_moves rows      : 20
   moves_by_parent keys       : 40  (4-token 20, 3-token alias 20)
   row objects in the payload : 40  -> every row is emitted TWICE
   response bytes             : 27864        (~1.4 kB per repertoire_moves row)
```

The frontend only ever looks the 3-token key up (`normalizeFen()` returns
`fen.split(' ').slice(0,3).join(' ')`), so the 4-token half of the payload is dead weight for
this client.

## Root cause

```python
# backend/repertoire.py:1376-1378
for pos_fen, op_name, eco in opp_positions:
    if pos_fen not in cand_cache:
        cand_cache[pos_fen] = self.get_candidate_moves_2000(pos_fen, color=repertoire)
```

```python
# backend/repertoire.py:278  (get_candidate_moves_2000)
resp = requests.get(url, params=params, headers=headers, timeout=10)
```

— a sequential loop of blocking 10 s-timeout calls with no deadline, and

```python
# backend/repertoire.py:1341-1346
p3 = " ".join(p.split()[:3])
if p3 != p:
    if p3 not in tree_by_parent:
        tree_by_parent[p3] = []
    if m not in tree_by_parent[p3]:
        tree_by_parent[p3].append(m)
```

— the alias that doubles the payload. The rest of the codebase moved to bounded pools and
bounded waits (`evaluator.SYNC_POOL_WAIT_TIMEOUT`, `ASYNC_POOL_WAIT_TIMEOUT`, the `EngineBusy`
→ 503 mapping); the repertoire explorer path was left with the original unbounded shape.


## Cross-reference / dedup note

The payload-duplication half of this report is **also** reported by `CODE-05-11`
("`GET /api/repertoire/tree` returns every move under two `parent_fen` keys"). That part is
therefore a duplicate; the distinct, unreported content here is the **unbounded latency**:
a sequential fan-out of blocking 10 s-timeout HTTP calls with no aggregate deadline, measured
at 1.7 s / 3.8 s / 6.2 s for 5 / 13 / 24 rows against a 0.5 s stub, which extrapolates to
~2 minutes per `refreshTree()` for a 24-move repertoire with the shipped timeout — and
`refreshTree()` is `await`ed by `saveMove()` before the candidate table is even requested.

## Exploit / escalation

Adjacent weaker variants:

1. **Two concurrent `refreshTree()` calls** multiply the fan-out (and are subject to
   BUILDER-05's engine race at the same time).
2. **`explorer_cache` is only written on success** (`if req_success and (len(candidate_moves) > 0 or total_games == 0)`),
   so during an outage the same positions are re-fetched at full cost on *every* refresh
   instead of being negatively cached.
3. **The payload doubling compounds the latency**: a large repertoire transfers ~2.8 kB per
   move on every refresh, on the same connection that the SSE eval bar is competing for.
