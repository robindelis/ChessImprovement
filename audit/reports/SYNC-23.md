# SYNC-23 — `normalize_score(score, color=None)` reports an ordinary `PovScore` as "checkmate is already on the board": python-chess's `PovScore` has neither `.mate()` nor `.score()`, so the `MateGiven` fallback swallows every non-mate
- **Severity:** low
- **Type:** wrong-result
- **File:** backend/analyzer.py
- **Line(s):** 83-119 (`normalize_score`), specifically 110-118
- **Component:** blunder analysis / eval normalisation
- **Repro script:** `audit/scripts/atk3_re04_mate_retest.py` (sections `RE04-B`, `RE04-G`)
- **Detected by:** ATK-3 (RETEST) — SYNC-06 re-test

## Expected
`normalize_score` is a *total* function on the engine's score types. python-chess 1.11.2 (the
pinned version here) has four score classes:

```
Cp           attrs: is_mate, mate, score, wdl
Mate         attrs: is_mate, mate, score, wdl
PovScore     attrs: is_mate, pov, white, black, wdl      <-- NO .mate(), NO .score()
```
`SimpleEngine.analyse()` returns a **`PovScore`** (relative to the side to move). `pov(colour)`
converts it to `Cp`/`Mate`/`MateGiven`, which is why the real call sites are correct. Anything
that skips that conversion must not be mistaken for a mate.

## Actual
```python
# backend/analyzer.py:110-118
if color is not None and hasattr(score, "pov"):
    score = score.pov(color)                        # <-- skipped when color is None
mate = score.mate() if hasattr(score, "mate") else None
if mate is None:
    raw = score.score() if hasattr(score, "score") else None
    if raw is None:
        # `MateGiven` -- python-chess folds "already won" into mate 0.
        return MATE_SCORE_CP, 0                     # <-- swallows EVERYTHING else
    return int(raw), None
```
For a `PovScore` and `color=None`, `hasattr(score, "mate")` and `hasattr(score, "score")` are
both `False`, so `raw is None` and the function returns `(+10000, 0)` — which
`describe_score_mate(0)` renders as **"checkmate is already on the board"**. Measured:

```
   python-chess 1.11.2: PovScore attrs = ['black', 'is_mate', 'pov', 'white', 'wdl']
   normalize_score(PovScore(Cp(+30)), color=None) -> cp=10000 mate=0
       'checkmate is already on the board'
```
A position the engine scored at **+0.30** is reported as a delivered checkmate.

## Reachability today
**Not reachable from `analyze_game_openings`** — both call sites pass a colour
(`analyzer.py:313` `board.turn`, `:328` `not board.turn`), so `pov()` always runs. That is why
this is `low` and not `high`. It is a live trap for the next caller: the docstring's contract
(`color: Optional[...] = None`) advertises the colour as optional, and a caller who copies the
signature gets a silent, maximally-confident lie — the exact class of bug SYNC-06 was about.

## Root cause
`hasattr`-based duck typing is being used where a type test is required, and the
"cannot tell, so assume the worst" fallback is applied to a whole class of scores instead of
to the one type it was written for. `MateGiven` is a *distinct* class in python-chess
(`chess.engine.MateGiven`), so it can be identified exactly rather than inferred from a
missing attribute.

## Exploit / escalation
1. **`normalize_score` is module-level and undocumented as "colour required".** It is
   importable and looks safe; `evaluator.py` has its own separate `MATE_SCORE_CP` handling, so
   a future refactor that unifies the two is the natural place this bites.
2. **The fallback also swallows a plain `int`/`None`/any duck type**, so a future
   `engine.analyse` result shape (or a test double that returns a bare dict) silently becomes
   "mate on the board" instead of raising.
3. **Adjacent, and the reason to fix it now:** `describe_score_mate(0)`'s
   "checkmate is already on the board" is currently unreachable from any real position (a
   `mate 0` after the user's move gives `cpl_loss = 0` so no row is written, and a `mate 0`
   *before* the move is impossible mid-game). The `MateGiven` branch is the only live producer
   of that string, and it is exactly the branch that misfires on `PovScore`.

## Fix direction
Make the colour mandatory (`color: chess.Color`) or normalise explicitly:
```python
if isinstance(score, chess.engine.PovScore):
    score = score.pov(color if color is not None else score.turn)
if isinstance(score, chess.engine.MateGiven):
    return MATE_SCORE_CP, 0
mate = score.mate()   # now safe: Cp / Mate only
```
and raise (not guess) for anything else.
