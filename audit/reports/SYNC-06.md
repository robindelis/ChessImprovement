# SYNC-06 — mate scores collapse to ±9999 sentinels and every huge loss is explained as "Checkmate in 1"
- **Severity:** medium
- **Type:** wrong-result
- **File:** backend/analyzer.py
- **Line(s):** 100-103, 162, 177, 180
- **Component:** blunder analysis / eval handling
- **Repro script:** `audit/scripts/atk3_analyzer_repro.py` (sections ATK3-09e, ATK3-09h)
- **Detected by:** ATTACKER-3 (SYNC)

## Expected
A mate score keeps its distance (or is stored in a dedicated column) and the generated
explanation reflects the actual engine score.

## Actual
`score(mate_score=10000)` collapses every mate into `±(10000 - plies)`, and the
explanation branches on the *centipawn* value:

```python
# backend/analyzer.py:100-103
if score_after <= -9000:
    why_bad = "Eval loss: Checkmate in 1."
else:
    why_bad = f"Eval loss: -{cpl_loss} centipawns."
```

Observed (scripted engine, real `GameAnalyzer` code path):

```
  ATK3-09e — cpl_loss when the engine returns a MATE (mate_score=10000 sentinel)
  e4: eval_before=9995 eval_after=-9999 cpl_loss=19994 category=Blunder
    explanation: Eval loss: Checkmate in 1.
  'loses a mate': eval_before=-100, eval_after=-10000 -> [('e4', -100, -10000, 9900)]

  ATK3-09h — the explanation text: is 'Checkmate in 1' ever accurate?
  eval_after is plain centipawns, -95.00   -> explanation: 'Eval loss: Checkmate in 1.' | cpl=9500
  eval_after is mate in 1                  -> explanation: 'Eval loss: Checkmate in 1.' | cpl=9999
  eval_after is mate in 30                 -> explanation: 'Eval loss: Checkmate in 1.' | cpl=9970
  eval_after is mate in 99                 -> explanation: 'Eval loss: Checkmate in 1.' | cpl=9901
```

So the *same string* is emitted for a mate in 1, a mate in 99, and a plain **-95.00**
evaluation where no mate exists at all. `cpl_loss` for a one-move slip that loses a forced
mate is 9900–19994, which permanently occupies the top of the
`ORDER BY b.cpl_loss DESC` gap list, and it is indistinguishable from a genuine 99-pawn
collapse in the database.

## Root cause
`mate_score=10000` is used as a scalar stand-in and then compared against a centipawn
threshold, while `blunders` has no `mate_in` / `score_type` column to preserve the
information. `_generate_tactical_explanation` only receives `score_after: int`, so it cannot
tell a mate from a huge centipawn score.

## Exploit / escalation
1. **Ranking pollution:** one walked-in mate permanently occupies `#1 BIGGEST BLUNDER`; the
   genuinely instructive 300 cp mistakes are pushed off the top of the list.
2. **`-9000` boundary:** any evaluation worse than -90 pawns is described as a checkmate, so
   the text is wrong for a completely lost position that is still playable.
3. **Sign confusion:** the collapse uses `pov(board.turn)` before and `pov(not board.turn)`
   after, which is correct, but with the sentinel the resulting `cpl_loss` magnitude carries
   no information about *which side* is mating, so the stored row cannot be interpreted
   without re-running the engine.
4. **Adjacent:** `evaluator.py:171-175` uses the same `mate_score` idea for the eval bar
   (`score_cp = 10000 if mate > 0 else -10000`), so the front-end eval bar shows a full bar
   rather than a "mate in N" badge.
