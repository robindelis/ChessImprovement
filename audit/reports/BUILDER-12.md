# BUILDER-12 — `is_valid_fen` accepts chess960/Shredder-FEN castling fields, so chess960 positions can be written into a standard repertoire (and the startup migration then treats them as orphans)
- **Severity:** low
- **Type:** spec-violation
- **File:** backend/api.py
- **Line(s):** 22-33 (`is_valid_fen`), consumed by `POST /api/repertoire/move` (~line 1073) and `backend/repertoire.py` 471-476 (`add_repertoire_move`'s own check)
- **Component:** Repertoire Builder — move validation
- **Repro script:** `audit/scripts/atk1_b04_hostile.py` (section A)
- **Detected by:** ATTACKER-1 (BUILDER)

## Expected

`is_valid_fen` is documented as *"Strictly validates chess FEN string structure and board
legality"* and is the single gate in front of every repertoire write. The repertoire is a
**standard** chess tree: the Builder's board (`frontend/board.js`, `chess.min.js`) only ever
produces KQkq-style castling fields, `chess.Board.fen()` round-trips them, and the
`normalize_fen` 4-token key that indexes the whole tree assumes standard notation. A FEN whose
castling field uses Shredder-FEN letters (`AHah`) is a different game and must be rejected, the
same way a FEN with an impossible castling field (`Zz`) already is.

## Actual

```
A. /api/repertoire/move  (POST)
  [parent_fen 4 fields no ep]           -> 400 {"detail":"Invalid FEN string"}
  [parent_fen non-POSIX castling]      -> 200 {"status":"success","san":"O-O","uci":"e1g1",
                                             "fen":"r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R4RK1 b kq -",
                                             "is_user_move":true, ...}
  [parent_fen 4 tokens garbage castling]-> 400 {"detail":"Invalid FEN string"}
```

`r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w AHah - 0 1` is a Shredder-FEN (chess960) position: the
`AH`/`ah` codes mean "White's king-side rook is the H-file rook". It is accepted, `1.e4`-style
moves are validated against it, and a row is stored whose `fen` is
`r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R4RK1 b kq -` — a *standard* castling field for a position
whose castling semantics were chess960. The `Z`-garbage case is correctly rejected only because
`chess.Board()` refuses to parse it.

## Root cause

The gate delegates entirely to `chess.Board(fen).is_valid()`, and `python-chess` accepts
Shredder-FEN castling notation as a synonym for standard notation:

```python
# backend/api.py
def is_valid_fen(fen: str) -> bool:
    """Strictly validates chess FEN string structure and board legality."""
    if not fen or not isinstance(fen, str):
        return False
    parts = fen.strip().split()
    if len(parts) < 4 or parts[0].count('/') != 7:
        return False
    try:
        board = chess.Board(fen)
        return board.is_valid()
    except Exception:
        return False
```

`add_repertoire_move` repeats the same check (`backend/repertoire.py:471-476`), so both layers
agree, and nothing downstream distinguishes the two notations — the 4-token
`normalize_fen` key, `moves_by_parent`, the coverage/gap scan and the Trainer all assume
standard castling.

## Exploit / escalation

Adjacent weaker variants, in decreasing order of practical reachability:

1. **The startup migration is hostile to such rows.**
   `migrate_studies_and_cleanup_blunders` (backend/db.py) reports
   *"N repertoire move(s) are not reachable from the starting position … Set CHESS_PURGE_ORPHANS=1
   to opt in to the (lossy) orphan purge"*, and `backend/db.py`'s reachability walk starts from
   the standard start FEN — so chess960 rows are unreachable by construction. A user who opted
   into the purge would lose them; a user who did not sees them counted forever as "not
   reachable". (Observed incidentally in the fuzz run: `[migration] 1 repertoire move(s) are
   not reachable …`.)
2. **The same gate is on `POST /api/repertoire/move_meta`, `DELETE /api/repertoire/move` and
   `GET /api/repertoire/eval_stream`**, so the whole Builder surface is consistent in accepting
   chess960 and inconsistent with its own board component.
3. **Unreachable, non-replayable positions in general.** `board.is_valid()` checks material and
   check-legality, *not* reachability. A FEN with a missing pawn (e.g. the e-pawn deleted while
   it is still White's turn) is accepted — I hit this accidentally during this audit when a
   hand-written FEN omitted the e-pawn and the API happily stored `2.Nf3` on top of it — so an
   arbitrary *legal-looking* position can become a repertoire node that no game can ever reach.

## Unproven suspicions

Suspected but **not** demonstrated, listed so a fixer does not waste time unless they want to
chase them:

1. **Stale-eval landing on the wrong position.** `startEvalStream()` has no
   "is this stream still for the current step?" guard, so if an `AbortController` ever fails to
   reject an in-flight `reader.read()` (or if `renderEvalBar` is reached from another path), a
   previous position's score would be painted on the current one. I reasoned through the
   microtask ordering and could not construct a deterministic interleaving — the abort looks
   sound in practice. The genuinely reachable part is the *cache* variant in BUILDER-04
   escalation 3: an aborted stream's partial depth is cached and then suppresses all later
   refreshes of that position.
2. **`update_move_metadata` picks an arbitrary row** when the 3-token `LIKE` matches more than
   one `parent_fen` (no `ORDER BY`/`LIMIT`, `fetchone()`). I could not construct two distinct
   *reachable* positions that share the first 3 FEN tokens (a transposition must differ in the
   en-passant square, and the double-push/single-push pair that would do it cannot produce the
   same piece placement), so this looks unreachable in practice.
3. **Concurrent `AsyncEngineEvaluator` across event loops.** `AsyncEngineEvaluator._instance`
   is process-wide and its `AsyncEnginePool` holds an `asyncio.Semaphore`, which raises
   `RuntimeError: <Semaphore> is bound to a different event loop` on a second loop in the same
   process. I reproduced it under `TestClient` (`eval stream: engine checkout failed`), but the
   production server is a single uvicorn worker with one loop, so I could not show a
   user-visible failure. It *would* bite under `uvicorn --reload`, `--workers > 1` with an
   in-process singleton, or any test that drives two lifespans in one process.
4. **O(n²) index build in `get_repertoire_tree`.** `if m not in tree_by_parent[p3]` is a linear
   `dict.__eq__` scan, but each bucket only holds the children of one position (1-5 rows), so I
   could not make it a practical problem; the *payload* doubling (BUILDER-06) is the real cost.
5. **Transposition-aware pruning.** `_prune_orphaned_children` deliberately keeps a position
   that is still "reached by another branch" via the 3-token `LIKE` test. I did not find a case
   where that keeps a genuinely orphaned subtree, but the check is loose enough (it matches on
   3 FEN tokens, i.e. it ignores the en-passant square) that a reviewer should confirm it.
