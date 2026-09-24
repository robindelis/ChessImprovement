# TRAINER-01 — Two transposing studies put two different prepared moves at one position, creating a drill that can never be graded correct
- **Severity:** high
- **Type:** wrong-result
- **File:** backend/repertoire.py
- **Line(s):** 477-490 (`add_repertoire_move` single-user-move guard), 23-34 (`normalize_fen`), 2512-2521 (`evaluate_trainer_move` prepared lookup)
- **Component:** Repertoire trainer — position identity
- **Repro script:** `audit/scripts/atk2_03_transposition.py`
- **Detected by:** ATTACKER-2 (TRAINER)

## Expected
`AGENTS.md` §5 states `repertoire_moves` "enforces 1 move per position" and
`add_repertoire_move`'s docstring says "If user's turn, replaces any existing user move".
Every trainer lookup identifies a position by the **3-token prefix**
(placement + turn + castling). The uniqueness check must use the same identity,
otherwise the invariant it enforces does not exist.

## Actual
`normalize_fen()` keeps the **en-passant token** (4 tokens), but the replace/delete guard
compares `parent_fen` **exactly**:

```python
norm_parent = normalize_fen(parent_fen)          # "… w KQkq d6"  (ep square kept!)
...
if is_user_move:
    cursor.execute("""SELECT fen, move_uci FROM repertoire_moves
                     WHERE repertoire = ? AND parent_fen = ? AND is_user_move = 1""",
                   (repertoire, norm_parent))     # <-- exact 4-token match
    existing_user_move = cursor.fetchone()
    if existing_user_move and existing_user_move["move_uci"] != move_uci:
        ... DELETE ...
```

Two move orders that transpose into the same position differ only in the ep token, so the
guard does not fire and **both** prepared moves are stored. `evaluate_trainer_move` then
resolves the position with `... AND rm.is_user_move = 1 LIMIT 1` (no `ORDER BY`), i.e. always
the lowest rowid, so the grader is pinned to one of the two moves no matter which study or
which FEN the client sends.

Observed:

```
P parent of 4th move: rnbqkbnr/1pp1pppp/p4n2/3pP3/3P4/8/PPP2PPP/RNBQKBNR w KQkq - 1 4
Q parent of 4th move: rnbqkbnr/1pp1pppp/p4n2/3pP3/3P4/8/PPP2PPP/RNBQKBNR w KQkq d6 0 4
same 3-token prefix  : True

RESULT: prepared user moves stored at ONE logical position
  id=7   Nf3   pop=1.0   study=StudyP  parent_fen=… w KQkq -
  id=14  Bb5+  pop=90.0  study=StudyQ  parent_fen=… w KQkq d6

CONSEQUENCE 1: the grader always resolves to the FIRST row (LIMIT 1)
  client is StudyQ, plays its prepared move f1b5 -> correct=False
  message: 'Not in your repertoire. Your prepared move is Nf3.'
  counters: Bb5 row tp=0 tc=0 | Nf3 row tp=1 tc=0  (user never played Nf3)
  client is StudyP, plays g1f3 -> correct=True

CONSEQUENCE 2: train/next serves Bb5, but Bb5 can never be graded correct
  drills served at the transposed position: {'Bb5+': 8}
  replay 1: user plays the drilled move f1b5 -> correct=False (Not in your repertoire. Your prepared move is Nf3.)
  replay 2: user plays the drilled move f1b5 -> correct=False (…)
  replay 3: user plays the drilled move f1b5 -> correct=False (…)
  Bb5 ends at times_correct=0 / times_practiced=0 -> permanently 'unlearned'

CONSEQUENCE 3: progress counts the same position twice
  {'total_moves': 7, 'mastered_count': 5, 'learning_count': 1, 'unlearned_count': 1}
  (StudyP has 4 user moves + StudyQ has 4 user moves, but only 7 distinct positions)
```

## Root cause
Position identity is inconsistent:
* `add_repertoire_move` (write path) → exact 4-token `parent_fen` incl. en-passant.
* `build_repertoire_tree_graph`, `evaluate_trainer_move`, `delete_repertoire_branch`,
  `update_move_metadata` (read/grade path) → 3-token prefix, i.e. *logical* position.

`UNIQUE(repertoire, parent_fen, move_uci)` in `db.py:134` is likewise 4-token, so the schema
does not stop it either. `get_trainer_progress` dedupes on the raw `parent_fen`
(`repertoire.py:1944 key = (r["parent_fen"], r["move_uci"])`), so the duplicate is also counted
twice in every percentage.

## Exploit / escalation
0. Related but distinct from `BUILDER-01` (which reaches two-user-moves-per-position through
   dangling `study_moves` rows on replacement). This report's root cause is the *position
   identity mismatch* (ep token) and it fires on a **fresh, empty** tree — no replacement or
   legacy state required.
1. **Any two transposing lines** do it — e.g. a user with "Anti French" (`1.e4 e6 2.d4 d5 3.e5`) and a
   Scandinavian/other study that reaches the same position by another move order, or a study
   reached once with `…d5` (ep square set) and once with `…cxd5`.
2. **Answer the drill for study A, get graded for study B**: the frontend sends
   `study_name`, but the prepared-move query has *no* study predicate, so a client can never
   make the grader agree with the drill it was served.
3. **Corrupt another card**: every wrong attempt increments `times_practiced` of the *other*
   row (see the counter line above), so repeatedly failing on Bb5 slowly buries Nf3's
   SM-2 statistics and drags Nf3 toward a 3650-day interval.
