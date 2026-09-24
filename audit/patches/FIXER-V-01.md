# FIXER-V-01 — "A line should never finish on an opponent's move" (owner ruling; TRAINER-09)

- **Fixed files:** `backend/repertoire.py`, `frontend/repertoire_trainer.js`
- **New tests:** `tests/test_line_end_semantics.py` (19 tests, 5 classes)
- **New probe helpers:** `audit/scripts/FIXER-V-01_make_prefix.py` (builds the pre-fix snapshot),
  `audit/scripts/FIXER-V-01_prefix_runner.py` (runs tests against it)
- **Existing tests REWRITTEN (LOUD, 2 tests + 1 setup):**
  `tests/test_trainer_hint_and_line_completion.py` (2 tests rewritten, 1 added) and
  `tests/test_repertoire.py` (1 **setup** only). Details in §2 — **the project owner overruled
  documented pedagogy**.
- **Interpreter:** `/home/robin/miniconda3/bin/python3` (base conda env).
- **Production DB:** never opened for write, never checkpointed; inspected read-only through
  `tests/probe_guard.open_production_readonly()`. `ps -eo pid,args | grep -c "[s]tockfish-linux"`
  is **0** after every run. Port 38281 was never contacted.
- **Diff size:** `backend/repertoire.py` **+118 / −16**, `frontend/repertoire_trainer.js`
  **+75 / −9** against the pre-fix snapshot
  (`diff -u /tmp/opencode/fixerv/repertoire_prefix.py backend/repertoire.py`, and the same for the
  trainer). Hunk-by-hunk map in §5.

**Full suite: `Ran 595 tests … OK` (exit 0).** Was `Ran 575 … OK`; +20 tests
(19 new in `tests/test_line_end_semantics.py`, +1 in
`tests/test_trainer_hint_and_line_completion.py`).

---

## 0. The overruled behaviour

The dispatch named `tests/test_pedagogical_trainer.py::test_opponent_branch_prioritization_for_continuing_lines`
as the test that "now asserts the wrong thing". **It is actually in
`tests/test_trainer_hint_and_line_completion.py`** — that file was verified and is where both
overruled tests live. (I re-verified with `grep -rn "test_opponent_branch_prioritization_for_continuing_lines"`.)

**The project owner has overruled the documented pedagogy.** FIXER-T declined to fix TRAINER-09
because `test_opponent_branch_prioritization_for_continuing_lines` explicitly asserted and
*named* the opposite rule ("the engine must prioritize the reply that continues the drill", over
10 iterations, with `assertNotIn("c5", chosen_replies)`), reasoning that "a drill that ends on a
move the user has no answer for teaches nothing". That is no longer the product's intent. The
owner's ruling is now: **"A line should never finish on an opponent's move."**

A second test in the same file asserted the same thing from the other side:
`test_line_completion_when_opponent_reply_has_no_user_followup` required
`opponent_reply == {e8g8, "O-O"}` **and** `is_line_end is True` — i.e. the board is moved onto the
opponent's castling move and the position is then called a completed line. **Both are rewritten
below**, loudly, and replaced with tests that assert the new rule.

---

## 1. The distinction rule, and how it is implemented

The whole decision is stated **once**, in a pure function, and everything else derives from it.

`backend/repertoire.py:69` — `classify_line_position(reached_by_user_move, has_prepared_reply)`:

```python
if has_prepared_reply:   return LINE_POSITION_CONTINUES          # the opponent moved, you have an answer
if reached_by_user_move: return LINE_POSITION_COMPLETED        # YOUR move is the end of the repertoire
return LINE_POSITION_AWAITING_REPLY                            # THE BUG: opponent moved, nothing prepared
```

| position reached by | prepared reply there? | classification | `is_line_end` |
| :--- | :--- | :--- | :--- |
| the **user's own** prepared move | no (opponent's turn) | `completed_line` | `True` |
| an **opponent's** move | **no** | `awaiting_user_reply` | **`False`** — a gap, never an end |
| an opponent's move | yes | `continues` | `False` |

**How "the user has prepared nothing more" is told apart from "the opponent moved and the user has
not prepared a reply":** by *which move produced the position*, not by what is missing.
`reached_by_user_move` is `opponent_reply is None` — i.e. the board is resting on the position the
user's **own** move produced (it is their turn, and there is nothing prepared there because there
is nothing prepared anywhere further). The other case is an `is_user_move = 0` row in
`repertoire_moves`: the repertoire literally holds the opponent's move, and the position it leads
to has **no** `is_user_move = 1` row. That is a *hole in the repertoire*, not the end of a line.

The rule is enforced **twice**, so the guarantee does not depend on either half staying correct:

1. **By selection** — `evaluate_trainer_move` never chooses an opponent reply without a prepared
   follow-up, so the board is never moved onto an unanswered branch at all.
2. **By classification** — even if a payload somehow carried both an `opponent_reply` and no
   `next_expected_uci`, `classify_line_position` returns `awaiting_user_reply`, and the
   `LINE_POSITION_AWAITING_REPLY` arm (`:4442`) forces `is_line_ended = False` and drops the
   opponent move. That arm is unreachable today and is deliberately kept as a second line of
   defence, because the guarantee the owner asked for is that such a position is *never reported*
   as a line end.

---

## 2. The test rewrite (LOUD) — the owner overruled the documented pedagogy

`AGENTS.md` §6.4: a test that asserts buggy behaviour may be deleted or rewritten, but that must
be said loudly. **Both of the following tests asserted the overruled behaviour and are rewritten,
not weakened.** Each rewrite is marked in place with a `AUDIT FIXER-V / TRAINER-09 — REWRITTEN`
docstring naming what it used to assert.

### 2.1 `test_line_completion_when_opponent_reply_has_no_user_followup`
→ **`test_unanswered_opponent_branch_is_not_played_and_not_called_a_line_end`**

*Was asserting:* `opponent_reply == {e8g8, "O-O"}` **and** `is_line_end is True` — the line
"finishing" on the opponent's castling move.
*Now asserts:* `opponent_reply is None` (the unanswered branch is not offered), the two
`next_expected_* is None` assertions are **kept** (still the correct values, still guard the
wrong-hint bug), `is_line_end is True` **only** because the line stops on the user's own `O-O`,
and `line_end_reason == "unanswered_opponent_branches"` with `unanswered_branches == 1` and
`unanswered_branch_sans == ["O-O"]`.
*Net:* one assertion removed (the overrule), six added.

### 2.2 `test_opponent_branch_prioritization_for_continuing_lines`
→ **`test_an_opponent_branch_with_no_reply_is_never_offered_as_a_drill_move`**

*Was asserting:* over 10 iterations, `assertNotIn("c5", chosen_replies)` — i.e. the dead end is
merely **deprioritised** and stays reachable.
*Now asserts:* the dead end is **impossible**. The fixture is strengthened so the old weighting
would have picked it ~99 % of the time (`1... c5` gets `popularity_pct = 100.0`, the live
`1... e6` gets `1.0`). Over 10 weighted draws: `chosen_replies == {"e6"}` exactly, plus
`next_expected_san == "d4"`, `line_position == "continues"`, `line_end_reason is None` on every
iteration.
*Net:* the original assertion is *strengthened* (set equality instead of two memberships) and four
assertions added. Nothing loosened.

### 2.3 `test_a_position_reached_by_an_opponent_move_is_never_a_completed_line` (added)
The unit test of the rule itself, so the classifier cannot rot:
`classify_line_position(True, False) == "completed_line"`,
`(False, False) == "awaiting_user_reply"` and the two are **not equal**,
`(False, True) == "continues"`.

### 2.4 `tests/test_repertoire.py::test_trainer_null_popularity_pct` — **SETUP ONLY**
This is the only pre-existing test my patch broke, and it is **not** an assertion of the overruled
pedagogy: its subject is that a `NULL popularity_pct` must not break the trainer's weighting. Its
fixture happened to give the user a single opponent branch with **no prepared answer**, and
asserted the trainer returned it as `opponent_reply` — i.e. it depended on the dead-end path
incidentally.

*Fix:* the fixture now states its real precondition — the branch has a prepared follow-up
(`... b KQkq - 3 3` / `d7d5`), which is what the weighting is actually reached for. The `NULL`
`popularity_pct` under test is still on the **opponent** row, the row whose weight is read.
*Assertions went from 2 to 7* (added `opponent_uci == "f1c4"`, `next_expected_uci == "d7d5"`,
`line_position == "continues"`, `is_line_end is False`, `line_end_reason is None`). Marked in
place with `AUDIT FIXER-V (SETUP ONLY — no assertion was removed or loosened)`.

### 2.5 `AGENTS.md`
§2's table (modules `32 → 33`, tests `575 → 595`, snapshot date), §3's file map, and a **new §4G**
documenting the rule, both consequences and the client presentation. `tests/test_docs_contract.py`
fails otherwise, and a false claim in `AGENTS.md` is a live hazard (AGENTS.md §6.5).

---

## 3. Per-change evidence

### 3.1 `backend/repertoire.py:50-89` — the rule, stated once
New module constants `LINE_END_REPERTOIRE_EXHAUSTED`, `LINE_END_UNANSWERED_BRANCHES`,
`LINE_POSITION_CONTINUES`, `LINE_POSITION_COMPLETED`, `LINE_POSITION_AWAITING_REPLY` and the pure
`classify_line_position()`.

**Verified by** `tests/test_line_end_semantics.py::TestTheRuleItself::test_a_position_reached_by_an_opponent_move_is_never_a_completed_line`
(pre-fix `AssertionError: 'completed_line' != 'awaiting_user_reply'`).

### 3.2 `backend/repertoire.py:4347-4396` — an unanswered opponent branch is not a drill choice
`replies_with_followup` is built as before, but every branch **without** a prepared follow-up is now
counted (`unanswered_branches`, `unanswered_branch_sans`) and **the `else` branch that played a
dead end is gone**. The board is never moved onto a position the user cannot answer.

*Removed (pre-fix behaviour):*
```python
else:
    # Opponent replies exist, but line ends after opponent move (no prepared user response)
    weights = [max(1.0, float(r.get("popularity_pct") or 0.0)) for r in opp_replies]
    opponent_reply = random.choices(opp_replies, weights=weights, k=1)[0]
    next_prep = None; next_expected_uci = None; next_expected_san = None
```

**Verified by** `::TestAnUnansweredBranchIsNotADrill` (4 tests). Pre-fix:
- `test_a_dead_end_branch_is_never_reported_as_a_line_end_api` → `KeyError: 'line_end_reason'`
- `test_a_line_whose_only_branch_is_a_dead_end_stops_on_the_users_own_move` →
  `AssertionError: {'san': 'c5', 'uci': 'c7c5', …} is not None : the board must never be moved onto an unanswered opponent move`
- `test_a_dead_end_branch_is_never_offered_as_the_opponent_move` (25 draws with the dead end weighted
  100:1) → passes pre- and post-fix: the pre-fix *filter* already preferred the continuing branch.
  It is a **"do not regress"** guard for the filter half, and its sibling test covers the half the
  pre-fix filter did **not** provide.

### 3.3 `backend/repertoire.py:4425-4460` — `is_line_end` is derived, not overloaded
`is_line_ended` now comes from `classify_line_position`, and the three cases map to
`is_line_end` + `line_end_reason`. Response gained `line_position`, `line_end_reason`,
`unanswered_branches`, `unanswered_branch_sans` (`:4491-4494`). `api.py:2285` returns the manager's
dict verbatim, so the new fields reach the client with **no** `api.py` change.

**Verified by** `::TestAGenuineEndIsStillAnEnd` (2 tests, one through `TestClient`).
Pre-fix: `KeyError: 'line_position'` / `KeyError: 'line_end_reason'`.

### 3.4 `backend/repertoire.py:2282-2327, 2346-2370, 2383` — the counting maths excludes a dead end
`prepared_replies = [reply["move_uci"] for reply in tree_by_parent.get(pos_fen, [])]` asked only
"is there a repertoire row for this opponent move", so recording `1... c5` and answering nothing to
it counted as **full coverage of `c5`**. Coverage now requires the branch to be **answered**: the
repertoire holds the opponent move *and* a prepared user move at the position it produces (matched
on the 3-token key, for the same reason the trainer and `get_trainer_progress` use one — AGENTS.md
§4F, TRAINER-01). Unanswered branches are also reported in `gaps`, tagged
`gap_kind: "unanswered_branch"` / `in_repertoire: true` (instead of `"missing_branch"`), and the
repertoire-side total is `unanswered_branches`.

**Verified by** `::TestTheCountingMathsExcludesTheDeadEnd` (5 tests). The crisp one is
`test_adding_an_answer_to_the_dead_end_restores_its_coverage` — the same position, the same
explorer payload, with and without the answer:

```
pre-fix : AssertionError: 80.0 != 50.0 within 1 places (30.0 difference)
          : only the ANSWERED branch (1... e6, 50 points) is coverage
post-fix: with_dead_end = 50.0, with_answer = 80.0, difference = 30.0
```

Pre-fix, `1... c5` contributed its 30 points of coverage **in both cases**; post-fix it contributes
exactly 0 until it is answered. `test_a_fully_covered_repertoire_reports_100_percent_and_no_gaps`
is the "do not regress" half (a fully answered repertoire still reports 100 % and no gaps).

### 3.5 `frontend/repertoire_trainer.js:770-830` — `_lineEndPresentation(data)`, a total function
It refuses to render "🎉 Line Complete!" for anything that is not a *declared* end on the user's
own move: `unfinished = reason === 'unanswered_opponent_branches' || answered > 0 ||
restsOnOpponentMove || !declaredEnd`. Being total is deliberate — the guarantee "no false
completion" is then local to this function rather than to whichever branch calls it today, so an
old-shaped or malformed payload can still not produce one.

### 3.6 `frontend/repertoire_trainer.js:970-996` — the honest fallback, no celebration
The branch that fires when the board *has* just moved onto an opponent's move with no answer
(the state the server no longer produces, kept as the honest fallback) previously rendered
`Opponent replied with ${opp.san}. 🎉 Line Complete!` in `feedback-box correct` + a green toast. It
now renders `feedback-box good_alternative` + an **amber** (`#f59e0b`) toast of 3200 ms:
*"Opponent replied {san}. Your repertoire has no prepared answer here — add a reply for it in the
Repertoire Builder to finish this line."*

### 3.7 `frontend/repertoire_trainer.js:1007-1020` — the no-reply path asks the classifier
`const lineEnd = this._lineEndPresentation(data);` then `feedback.className` /
`feedback.textContent` / `showToast` are all taken from it.

#### Why this presentation (requirement 4: "no celebration … and no error")
* **Not a celebration.** "🎉 Line Complete!" asserts the line is finished. It is not; it is missing
  the user's own reply. Celebrating it is the bug the owner overruled.
* **Not an error.** Nothing failed and the user did nothing wrong: they played their prepared move
  correctly. A red `feedback-box error` would blame them for a repertoire they have not finished
  building.
* **Neutral, and it names the fix.** Amber is already the neutral/warning slot in
  `app.js`'s toast palette (AGENTS.md §8.1) and `good_alternative` is already the style used for
  "informative, not an error" feedback in this same file (`:885`). The message says *what* is
  missing and *where* to add it. Reusing the existing `.feedback-box` element and the floating
  toast container keeps §8.1's **0 px layout shift** guarantee intact — no in-flow element is
  inserted or resized.

**Verified by** `::TestTheClientRendersTheNewState` (6 tests), which executes the **real**
`frontend/repertoire_trainer.js` through the existing `tests/frontend_harness.js` (no new harness)
and proves provenance by comparing the SHA-256 the harness executed against the file on disk.
Pre-fix: `RuntimeError: frontend scenario raised: TypeError: trainer._lineEndPresentation is not a function`
— 6/6 red. Post-fix 6/6 green.

---

## 4. Before / after evidence

### 4.1 New regression tests — **after**

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_line_end_semantics
...................
----------------------------------------------------------------------
Ran 19 tests in 1.355s

OK
```

### 4.2 The same 19 against the **pre-fix** module

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-V-01_prefix_runner.py \
      tests.test_line_end_semantics
...
test_a_completed_line_still_reports_no_unanswered_branches_through_the_api ... ERROR
test_the_users_own_last_prepared_move_is_a_completed_line                   ... ERROR
test_a_dead_end_branch_is_never_offered_as_the_opponent_move                ... ok
test_a_dead_end_branch_is_never_reported_as_a_line_end                     ... ERROR
test_a_dead_end_branch_is_never_reported_as_a_line_end_api                 ... ERROR
test_a_line_whose_only_branch_is_a_dead_end_stops_on_the_users_own_move    ... FAIL
test_a_continuing_line_is_not_rendered_as_an_end                            ... ERROR
test_a_dead_end_is_not_celebrated_and_is_not_an_error                       ... ERROR
test_a_genuine_end_is_still_celebrated                                      ... ERROR
test_a_legacy_payload_that_ends_on_an_opponent_move_is_not_celebrated       ... ERROR
test_the_harness_actually_executed_the_real_trainer_source                  ... ERROR
test_the_source_no_longer_celebrates_an_opponent_move_as_a_completed_line   ... ERROR
test_a_dead_end_branch_is_not_counted_as_covered                            ... FAIL
test_a_dead_end_branch_is_reported_as_a_gap_not_silently_dropped            ... ERROR
test_a_fully_covered_repertoire_reports_100_percent_and_no_gaps             ... ERROR
test_adding_an_answer_to_the_dead_end_restores_its_coverage                ... FAIL
test_progress_counting_only_ever_counted_prepared_user_moves               ... ok
test_the_repertoire_side_dead_end_count_is_reported                         ... ERROR
test_a_position_reached_by_an_opponent_move_is_never_a_completed_line      ... FAIL

KeyError: 'line_end_reason'
KeyError: 'line_position'
KeyError: 'line_end_reason'
KeyError: 'line_position'
RuntimeError: frontend scenario raised: TypeError: trainer._lineEndPresentation is not a function
RuntimeError: frontend scenario raised: TypeError: trainer._lineEndPresentation is not a function
RuntimeError: frontend scenario raised: TypeError: trainer._lineEndPresentation is not a function
RuntimeError: frontend scenario raised: TypeError: trainer._lineEndPresentation is not a function
RuntimeError: frontend scenario raised: TypeError: trainer._lineEndPresentation is not a function
RuntimeError: frontend scenario raised: TypeError: trainer._lineEndPresentation is not a function
KeyError: 'gap_kind'
KeyError: 'unanswered_branches'
KeyError: 'unanswered_branches'
AssertionError: {'san': 'c5', 'uci': 'c7c5', 'fen': 'rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -'} is not None
              : the board must never be moved onto an unanswered opponent move
AssertionError: 0.0 != 50.0 within 1 places (50.0 difference)
AssertionError: 80.0 != 50.0 within 1 places (30.0 difference)
              : only the ANSWERED branch (1... e6, 50 points) is coverage
AssertionError: 'completed_line' != 'awaiting_user_reply'
- completed_line
+ awaiting_user_reply

Ran 19 tests in 1.373s

FAILED (failures=4, errors=13)

pre-fix backend : /tmp/opencode/fixerv/repertoire_prefix.py
pre-fix frontend: /tmp/opencode/fixerv/repertoire_trainer_prefix.js (sha256 on disk: 41eb2203ac466e3186301ba756e2f539f439ace86f06d2b822484cff43e25f33)
real frontend restored: True
pre-fix failures: 4 errors: 13
```

**17 of 19 fail before the patch; all 19 pass after it.** The two that were already green
(`test_a_dead_end_branch_is_never_offered_as_the_opponent_move`,
`test_progress_counting_only_ever_counted_prepared_user_moves`) are **"do not regress"** guards for
behaviour that was already correct: the pre-fix filter already preferred a continuing branch, and
`get_trainer_progress` already counted `is_user_move = 1` rows only.

The prefix runner's frontend swap is fail-safe: the real file is restored in a `finally`, and
`sha256sum frontend/repertoire_trainer.js` after the pre-fix run matches the pre-swap copy
(`4d2f36b4fec69fac73f74eeb0b381dde0abea1ce5041fe37e71a4aeb271b0e5a`).

### 4.3 The two rewritten tests, against the pre-fix module

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-V-01_prefix_runner.py \
      tests.test_trainer_hint_and_line_completion
AssertionError: {'san': 'O-O', 'uci': 'e8g8', 'fen': 'rnbq1rk1/pp2bppp/4p3/3pPn2/3p1P2/P2B1N2/1PP3PP/RNBQ1RK1 w - -'} is not None
              : an opponent branch with no prepared user reply must not be offered as a drill move
AssertionError: 'completed_line' != 'awaiting_user_reply'
Ran 4 tests in 1.132s
FAILED (failures=2, errors=1)
```

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_trainer_hint_and_line_completion
Ran 4 tests in 1.128s
OK
```

### 4.4 The amended `test_repertoire.py` setup, against the pre-fix module

```
$ ... FIXER-V-01_prefix_runner.py tests.test_repertoire
ERROR: test_trainer_null_popularity_pct    # KeyError: 'line_position' — the pre-fix code has no such field
```

### 4.5 Full suite

```
$ /home/robin/miniconda3/bin/python3 -m unittest discover tests
----------------------------------------------------------------------
Ran 595 tests in 27.373s

OK
```
(exit code 0)

### 4.6 The golden invariant

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_repertoire_data_integrity
Ran 26 tests in 2.639s

OK
```

The new module re-asserts all three halves after every trainer write
(`assert_golden_invariant`, `tests/test_line_end_semantics.py:118-131`): at most one
`is_user_move = 1` row per `(repertoire, parent_fen)`, no orphaned `study_moves`, and no prepared
move deleted. This patch adds **no write path at all**, so nothing can disturb it.

### 4.7 Engine & production hygiene

```
$ ps -eo pid,args | grep -c "[s]tockfish-linux"
0
```
after the full suite, the focused run, both pre-fix runs and every probe. No engine is started by
this patch (the coverage tests stub `get_candidate_moves_2000`; `get_game_gaps` is stubbed too).

```
$ ls -l --time-style=full-iso chess_improvement.db
-rw-r--r-- 1 robin robin 2547712 2026-09-27 10:34:39 chess_improvement.db   # no -wal / -shm residue
```
Read-only row counts via `probe_guard.open_production_readonly()` (`mode=ro&immutable=1`):
`games 307`, `blunders 153`, `drill_reviews 153`, `explorer_cache 444`, `scouted_content 31`,
`settings 5`, `repertoire_moves 0`, `repertoire_studies 0`, `study_moves 0` — matching the current
AGENTS.md §9.7 (the repertoire is intentionally empty; the owner cleared it). The file mtime
predates the first edit of this patch.

---

## 5. Change map

`backend/repertoire.py` (+118 / −16):

| lines | change |
| :--- | :--- |
| 50-89 | new constants + `classify_line_position()` — the single definition |
| 2282-2302 | coverage/gap preamble: `user_move_parents3`, `unanswered_branch_count`, `counted_positions` |
| 2318-2327 | `covered_replies` / `unanswered_replies` — the answered-vs-unanswered split, with the 3-token fallback |
| 2346-2370 | coverage counts only an **answered** branch; an unanswered one is a tagged gap |
| 2383 | `unanswered_branches` in the `/api/repertoire/tree` result |
| 4347-4356, 4388-4389 | count `unanswered_branches` / `unanswered_branch_sans` in the trainer |
| 4390-4396 | **the "line ends after an opponent move" branch is deleted** |
| 4425-4460 | `is_line_end` derived from `classify_line_position`; `AWAITING_REPLY` arm as a second line of defence |
| 4491-4494 | new response fields `line_position`, `line_end_reason`, `unanswered_branches`, `unanswered_branch_sans` |

`frontend/repertoire_trainer.js` (+75 / −9):

| lines | change |
| :--- | :--- |
| 770-830 | new `_lineEndPresentation(data)` — a **total** function; amber "Line paused", names the fix |
| 970-996 | the opponent-move fallback no longer says "Line Complete" |
| 1007-1020 | the no-reply path renders through `_lineEndPresentation` |

Tests:

* **new** `tests/test_line_end_semantics.py` (19 tests) — added, not modified;
* `tests/test_trainer_hint_and_line_completion.py` — 2 tests **rewritten** + 1 added (§2);
* `tests/test_repertoire.py` — 1 **setup** + 5 added assertions (§2.4);
* `AGENTS.md` — §2 counts, §3 file map, new §4G.

---

## 6. Two failures in files I do not own — reported, not edited

Running an **arbitrary 18-module subset** (not the canonical discovery order) surfaces two
order-dependent failures in `tests/test_api.py`:

* `test_account_endpoint` (ERROR) and `test_sync_malformed_games` (`AssertionError: 400 != 200`).

They are **pre-existing order dependencies, not caused by this patch**:

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_api          # alone, post-fix
Ran 25 tests in 1.995s
OK

$ ... FIXER-V-01_prefix_runner.py <the same 18-module subset>              # pre-fix
FAIL:  test_sync_malformed_games
ERROR: test_account_endpoint
```

Both fail **identically with the pre-fix module**, both pass in isolation, and both pass in the
canonical `unittest discover tests` run (`Ran 595 … OK`). They depend on `settings`/fixture state
left behind by whichever module runs before `test_api`. Left untouched — `tests/test_api.py` is not
my file and "fixing" it would mean guessing at another owner's intent.

---

## 7. Residual risks

1. **A repertoire that is only a dead end no longer plays a closing opponent move.** The user
   drills `1. e4`, the board stops there, and the UI says "Line paused — 1 opponent branch has no
   prepared reply. Add it in the Repertoire Builder." Previously it played `1... c5` and
   celebrated. This is exactly the ruling, but it is a **visible product change**: a user with a
   one-move repertoire will now see a paused state rather than a celebration after every drill.
   The mitigating path is the Builder's `gaps` list, which now names the branch
   (`gap_kind: "unanswered_branch"`).
2. **`frontend/repertoire_builder.js` does not yet render `gap_kind`.** The gap rows carry the new
   `gap_kind` / `in_repertoire` keys, and the builder will render them with the existing generic
   "missing" wording. The honest wording ("you have this move, you have no answer") needs one
   conditional in `repertoire_builder.js`, which is **not my file** — flagged for its owner. The
   same applies to `coverage_pct`: it will now correctly read *lower* for a repertoire with dead
   ends, and the FIXER-T hand-off about `coverage_degraded` is still open.
3. **`unanswered_branches` on the tree result counts a repertoire-side fact per position, and a
   transposition can make one branch appear twice.** `counted_positions` de-duplicates by
   `(pos_fen, move_uci)`, so a branch reachable through two *different* 3-token spellings of the
   same position is still counted twice. That is a pre-existing property of `tree_by_parent`
   (FIXER-T §9), not introduced here, and the count is informational.
4. **The trainer no longer weights dead ends at all**, so a repertoire where *every* branch at a
   position is a dead end now stops instead of drilling a random one. If a user deliberately kept
   opponent branches only as "bookkeeping" (to see the move, not to answer it), those rows are now
   inert for training and visible as gaps. That is the intended reading of the ruling, but it is a
   workflow change for anyone who used them that way.
5. **`evaluate_trainer_move`'s study-scoped follow-up lookup still has a bare `LIMIT 1` without a
   tie-breaker** (`:4272`), the CODE-3 class FIXER-R2a §7 deferred. Untouched here to keep the diff
   surgical; it is the site a re-exploiter should probe next for this function.
6. **The classification half is a second line of defence, not the primary filter.** If a future
   change re-introduces a dead-end pick, `is_line_end` still cannot be `True` on it — but
   `opponent_reply` would be `None` while the client had already been told a move was coming.
   `tests/test_line_end_semantics.py` asserts the *observable* post-condition (`opponent_reply is
   None`), which is what would catch a regression.

## 8. Also noticed (not fixed)

* `repertoire_tree`'s coverage lookup now also falls back to the **3-token** key
  (`tree_by_parent.get(pos_fen) or tree_by_parent.get(pos_3)`). Without it, a position whose
  stored `parent_fen` and the `fen` of the move reaching it disagree on the en-passant token
  (CODE-3 / TRAINER-01) contributed **nothing** to coverage. This patch therefore also raises
  `coverage_pct` for such transposing pairs — visible in the pre-fix/post-fix delta
  (`0.0 → 50.0` in `test_a_dead_end_branch_is_not_counted_as_covered`, where the pre-fix number was
  `0.0` for this reason rather than the `80.0` the dead-end rule alone implies). The
  dead-end effect is isolated by `test_adding_an_answer_to_the_dead_end_restores_its_coverage`
  (`80.0 → 50.0` with consistent 4-token keys), which is why both tests exist.
* `get_repertoire_tree` still pays one explorer lookup per distinct opponent position
  (FIXER-T §7.9) — unchanged.
* `tests/test_pedagogical_trainer.py` has **no** test named
  `test_opponent_branch_prioritization_for_continuing_lines`; the dispatch's attribution was
  wrong. The real test was found in `tests/test_trainer_hint_and_line_completion.py`.
