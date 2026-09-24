# FIXER-W-01 — final cleanup wave: deterministic trainer lookups, honest gap rendering, order-independence, §4E

- **Fixed files:** `backend/repertoire.py`, `frontend/repertoire_builder.js`, `frontend/style.css`,
  `tests/test_repertoire.py`, `tests/test_builder_frontend_contracts.py`,
  `tests/test_builder_xss_contracts.py`, `tests/test_docs_contract.py`,
  `tests/test_layout_contracts.py`, `tests/test_repertoire_sync_robustness.py`,
  `tests/test_setup.py`, `tests/test_api.py`, `AGENTS.md`
- **New probe / runner scripts:** `audit/scripts/FIXER-W-01_prefix_runner.py` (runs any test
  target against the pre-fix snapshots; `--only` / `--legacy-ledger` / `--order`),
  `audit/scripts/FIXER-W_01_bisect.py` (order-dependence bisector),
  `audit/scripts/FIXER-W_01_order_probe.py` (shared-state inspector)
- **Existing tests restated (LOUD, 2):**
  `tests/test_builder_frontend_contracts.py::test_dynamic_gap_text_is_clamped` and
  `tests/test_builder_xss_contracts.py::test_escape_helpers_exist_and_cover_the_five_mark_characters`.
  Both were **strengthened**, not relaxed — see §2.2.
- **No existing assertion was removed or weakened. No existing test was deleted.**
- **Interpreter:** `/home/robin/miniconda3/bin/python3` (base conda env).
- **Production DB:** never opened for write, never checkpointed. `chess_improvement.db` mtime is
  unchanged at `2026-09-27 10:34:39` and there is no `-wal`/`-shm` residue. Port 38281 was never
  contacted. `ps -eo pid,args | grep -c "[s]tockfish-linux"` is **0** after every run.
- **Staging:** `git add` was **never** run. No `git reset`, no `git add -A`, no `git add .`.

**Full suite: `Ran 615 tests … OK` (exit 0).** Was `Ran 595 … OK`; **+20** tests
(+4 `test_repertoire.py::TestPreparedMoveLookupIsDeterministic`,
+6 `test_builder_frontend_contracts.py::TestCoverageGapKinds`,
+5 `test_repertoire_sync_robustness.py::TestNoProcessGlobalOutlivesItsTest`,
+3 `test_layout_contracts.py::TestEmittedClassesAreStyled`,
+2 `test_docs_contract.py` §4E checks).

**Diff size** (against the pre-fix snapshots in `/tmp/opencode/fixerw/`):
`backend/repertoire.py` **+19 / −3**, `frontend/repertoire_builder.js` **+83 / −17**,
`frontend/style.css` **+57 / −0**, `tests/test_repertoire_sync_robustness.py` **+100 / −1**,
`tests/test_setup.py` **+43 / −0**, `tests/test_api.py` **+41 / −1**.

---

## 0. TL;DR per item

| # | Item | Root cause | Fix | Tests |
| :-- | :--- | :--- | :--- | :-- |
| 1 | trainer's ambiguous follow-up lookup | study-scoped arm had `LIMIT 1` with **no `ORDER BY`**; the fallback arm ordered by `is_important, id` but **omitted the exact-parent preference** | both arms now carry `(parent_fen = ?) DESC, is_important DESC, id ASC` | 4 new, **2 red → green** |
| 2 | Builder ignored `gap_kind` | one template for both kinds, generic "unprepared opponent moves" wording | `_isUnansweredBranch()` + `_coverageGapCardHtml()` + `.gap-item--*` accents | 6 new, **5 red → green** |
| 3 | 2 order-dependent tests in `test_api.py` | a **lost stub ledger** in `tests/test_repertoire_sync_robustness.py` permanently replaced the **process-global `requests.get`**; plus `settings` is not a fixture table so `test_api` inherited a `lichess_username` row and hit the live API | ledger made loss-proof + `reset_fixtures()` restores shared globals + `test_api` declares its own preconditions | 5 new, **2 + 1 red → green**; 31/31 modules GREEN |
| 4 | §4E promised a removed panel | docs drift: CODE-15 removed the containers, FIXER-G removed the writes, a test *enforces* their absence | **decision (b)**: restate §4E to the API payload, name the enforcing test | 2 new, **1 red → green** |
| 5 | ~23 emitted-but-unstyled classes | most are `querySelector` marker hooks on already-styled elements | **3 styled** (`.badge-pill`, `.scout-card-meta`, `.gap-item--*`), 12 reported as hooks/cosmetic with evidence | 3 new, **5 subTest failures red → green** |

---

## 1. Item 1 — `evaluate_trainer_move`'s follow-up lookup (backend)

### 1.1 Root cause

`backend/repertoire.py`, inside `evaluate_trainer_move`'s "which opponent replies have a prepared
user follow-up" loop, had **two** candidate lookups, and **neither** was deterministic:

```python
# ARM A — the study-scoped lookup
cursor.execute("""
SELECT DISTINCT rm.* FROM repertoire_moves rm
LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
WHERE rm.repertoire = ? AND (rm.parent_fen = ? OR rm.parent_fen = ? OR rm.parent_fen LIKE ?) AND rm.is_user_move = 1
  AND (rs.name = ? OR rm.study_name = ?)
LIMIT 1                                       # <-- NO ORDER BY AT ALL
""", (repertoire, opp_fen, opp_fen_3, f"{opp_fen_3} %", clean_study, clean_study))

# ARM B — the unscoped fallback
cursor.execute("""
SELECT * FROM repertoire_moves
WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?) AND is_user_move = 1
ORDER BY is_important DESC, id ASC             # <-- missing the exact-parent preference
LIMIT 1
""", (repertoire, opp_fen, opp_fen_3, f"{opp_fen_3} %"))
```

This is the CODE-3 class FIXER-R2a §7 deferred. **Why the missing `ORDER BY` was worse than it
looks** — and this is the part worth knowing: it is not "whatever row SQLite happens to find
first". `EXPLAIN QUERY PLAN` for arm A is

```
SEARCH rm USING INDEX ux (repertoire=?)
```

i.e. the `sqlite_autoindex_repertoire_moves_1 (repertoire, parent_fen, move_uci)` index with only
the leading column pinned. So the natural answer is the row with the **lexicographically smallest
`parent_fen`**, and `-` (0x2D) sorts before every file letter (a–h, 0x61–0x68). So whenever one
logical position is stored with the en-passant token written as `-` and also as a real square,
**the twin always wins** — deterministically, and silently. Arm B is the same bug with a different
tie-breaker: `id ASC` prefers the older row, which is the same twin in a legacy import.

Two independent things make the predicate multi-row:

1. the fuzzy `parent_fen LIKE '<3-token> %'` arm matches the **other en-passant spelling** of the
   same logical position, and
2. the `LEFT JOIN study_moves` **multiplies** a row that belongs to several studies (arm A's
   `SELECT DISTINCT` collapses the duplicates, but the *choice* is still arbitrary).

Reachability: python-chess prints `-` for an en-passant square no pawn can legally take, so a
"harmless" double push does **not** produce the shape. It needs a real ep capture — `1. e4 c6
2. e5 d5` leaves White's pawn on e5 and `d6` survives `board.fen()`. That is the fixture the
tests use, and the tests *assert the premise*
(`chess.Board(_R_EP).has_legal_en_passant()`), so the fixture cannot silently stop reproducing
the bug.

### 1.2 The change

`backend/repertoire.py:4366-4400` — both arms now carry **exactly the tie-breaker the rest of the
file already uses** (`add_repertoire_move` `:1181`, `delete_repertoire_branch` `:1380`,
`update_move_metadata` `:1755`, `get_next_trainer_drill.last_move` `:4047`, and
`evaluate_trainer_move`'s own prepared lookup `:4207`):

| lines | change |
| :--- | :--- |
| `4366-4373` → `4366-4385` | arm A gains `ORDER BY (rm.parent_fen = ?) DESC, rm.is_important DESC, rm.id ASC` and the extra `opp_fen` bind; a 14-line comment records the `EXPLAIN` finding and why `-` always won |
| `4377-4382` → `4396-4400` | arm B gains the `(parent_fen = ?) DESC` term it was missing, plus its bind and a comment |

### 1.3 The whole-file audit: **no bare `LIMIT 1` remains**

Every `LIMIT` in `backend/repertoire.py`, mechanically (`audit/scripts/`-free one-liner reproduced
in the patch transcript):

```
1384   SELECT id, parent_fen, fen FROM repertoire_moves   ORDERED
1759   SELECT * FROM repertoire_moves                     ORDERED
4051   SELECT * FROM repertoire_moves                     ORDERED
4213   SELECT rm.*, COALESCE(rs.name, …) …                ORDERED
4383   SELECT DISTINCT rm.* FROM repertoire_moves rm      ORDERED   <- arm A (this patch)
4397   SELECT * FROM repertoire_moves                     ORDERED   <- arm B (this patch)
1433   "SELECT 1 FROM study_moves WHERE study_id = ? LIMIT 1"      EXISTENCE probe
1442   "SELECT 1 FROM repertoire_moves WHERE study_name = ? LIMIT 1"  EXISTENCE probe
2930   "SELECT 1 FROM repertoire_moves WHERE repertoire = ? AND fen = ? LIMIT 1"  EXISTENCE probe
```

The last three project the **constant** `1` and are only used as `is not None` / truthiness
probes, so no row is ever *selected* and a tie-breaker is meaningless there. Six SQL `LIMIT 1`s
over a row-selecting predicate, **all ordered**.

I also swept every `fetchone()` in the file (28 sites) for unordered *row* selection. The only
non-unique-predicate hit left is `backend/repertoire.py:3467-3471`, inside
`seed_default_repertoires`' **read-only planning phase**. It is reported in §7 rather than changed,
because it is not a `LIMIT 1` and its failure mode is a message, not a write.

### 1.4 Evidence — 2 of 4 red before, 4/4 green after

New: `tests/test_repertoire.py::TestPreparedMoveLookupIsDeterministic` (4 tests). The twin rows
are inserted with **raw SQL**, because `add_repertoire_move` enforces one user move per logical
(3-token) position — this shape can only arrive from a legacy import, which is exactly CODE-3's
state. The twin is written first, so it has both the lower `id` **and** the smaller `parent_fen`;
the tests assert both orderings point at the wrong row, so the trap cannot be defeated by a
different query plan.

**AFTER**

```
$ /home/robin/miniconda3/bin/python3 -m unittest \
      tests.test_repertoire.TestPreparedMoveLookupIsDeterministic
....
----------------------------------------------------------------------
Ran 4 tests in 1.126s

OK
```

**BEFORE** (pre-fix `backend/repertoire.py` at `/tmp/opencode/fixerw/repertoire_prefix.py`)

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-W-01_prefix_runner.py \
      tests.test_repertoire.TestPreparedMoveLookupIsDeterministic
The study-scoped arm: `next_expected_uci` must be the exact-parent answer. ... FAIL
The fallback arm already ordered by `is_important DESC, id ASC` but was still ... FAIL
test_only_the_chosen_row_is_graded_and_scheduled ... ok
test_the_grade_is_reported_against_the_chosen_move ... ok

FAIL: test_the_study_scoped_arm_serves_the_exact_parent_row
AssertionError: 'f1c4' != 'd2d4'
- f1c4
+ d2d4
 : the trainer offered 'f1c4', which is the prepared move of the `-`-spelled TWIN of the position the board is on

FAIL: test_the_unscoped_fallback_arm_also_prefers_the_exact_parent
AssertionError: 'f1c4' != 'd2d4'
- f1c4
+ d2d4
 : the unscoped arm answered from the wrong position

Ran 4 tests in 1.105s
FAILED (failures=2)
```

The 2 that are green in both runs are **"do not regress" guards** for the *prepared* lookup that
FIXER-R2a already fixed; they are kept because they pin the observable half of the same CODE-3
class (the wrong row's spaced-repetition counters moving).

**Diff of the fix** (`diff -u /tmp/opencode/fixerw/repertoire_prefix.py backend/repertoire.py`,
`+19 / −3`, both hunks inside `evaluate_trainer_move`).

---

## 2. Item 2 — the Builder must render `gap_kind` (frontend)

### 2.1 Root cause

FIXER-V made `get_repertoire_tree` report two genuinely different problems in one `gaps[]` list,
tagged with `gap_kind` / `in_repertoire` (`backend/repertoire.py:2359-2370`). FIXER-V §7.2
handed the client half over. `renderGapLists()` ignored both keys: one template, the summary line
`"N unprepared opponent moves ≥ X%"`, and one button labelled **"Address this reply →"**. A move
the user **already has in their repertoire** and simply has no answer to was therefore reported to
them with the same words as a move they have never seen — and the button implied the first job.

### 2.2 The change

`frontend/repertoire_builder.js:2470-2505` (summary) and two new methods at `:2507-2572`.

| lines | change |
| :--- | :--- |
| `2470-2495` | the summary counts the kinds separately: `"1 branch with no prepared reply · 2 not in your repertoire ≥ 5%"`; when there is no unanswered branch it falls back to the plain missing-only phrasing |
| `2507-2524` | **new** `_isUnansweredBranch(gap)` — total: `gap_kind` authoritative, pre-4.G payload falls back to the exact `in_repertoire` flag, an **unrecognised** `gap_kind` is treated as `missing_branch` (a new kind must not silently inherit "you have no answer") |
| `2526-2572` | **new** `_coverageGapCardHtml(g)` — the row, split by kind |
| `frontend/style.css:1454-1476` | `.gap-item--unanswered` / `.gap-item--missing` accents, drawn with an **inset** `box-shadow` so the row's border box and content box are byte-identical to the unstyled rule |

The two rows:

| | `missing_branch` | `unanswered_branch` |
| :--- | :--- | :--- |
| badge | `badge-critical` — "Not in repertoire" | **`badge-mistake`** (amber `#fbbf24`) — "No prepared reply" |
| line 3 | "Not in your repertoire. Add this reply, then prepare your answer to it." | "You have this reply but no prepared answer after it — the line would stop on their move. **Add your reply in the Repertoire Builder.**" |
| button | *Add this reply →* | *Add your reply →* |
| accent | red inset bar | amber inset bar |

Consistency with §4G: the wording is FIXER-V's "add a reply in the Repertoire Builder", the colour
is the same amber neutral the Trainer uses for the identical state
(`repertoire_trainer.js::_lineEndPresentation`), and neither is a success nor an error.

**§8 compliance, explicitly:**

* **§8.1** — no in-flow notice is inserted. A gap row is *content*, not a notice; the existing
  `addressGap()` path already toasts. Nothing new is prepended or expanded.
* **§8.2** — three lines, all `white-space:nowrap; overflow:hidden; text-overflow:ellipsis`, and
  the **untruncated** sentence goes in the row's `title="${this._escapeAttr(head)}"`.
* **§8.4** — the action button keeps `min-width:110px; justify-content:center;` and the button
  column gets `flex-shrink:0`, so a longer label cannot push the text column.
* **§8/zero-CLS between the two kinds** — identical structure, identical line count, identical
  button footprint; the *only* difference that could move a sibling is the accent, and that is an
  inset `box-shadow` (paints inside the padding box) rather than a thicker `border-left` (which
  would change the content box by 2 px). This is asserted in
  `tests/test_layout_contracts.py::test_the_two_coverage_gap_kinds_have_distinct_accents`.
* The `.gap-left` column wrapper is new: `.gap-item` is `display:flex; justify-content:space-between`,
  so the pre-fix row's three children were three side-by-side flex columns. A fourth text line
  would have been squeezed. The wrapper is `style="flex:1; min-width:0;"` plus the existing
  `.gap-left` (`display:flex; flex-direction:column; gap:0.2rem`) → [text column, button].

### 2.3 The two restated static contracts (LOUD — both strengthened)

Extracting the row into its own method moved the `btn-address-coverage-gap` markup **after** the
`querySelectorAll('.btn-address-coverage-gap')` wiring, and two existing assertions were measuring
incidents of file layout rather than the property they name. Both are restated over the *general*
form, which is **stricter** than what they replaced:

1. `tests/test_builder_frontend_contracts.py::test_dynamic_gap_text_is_clamped` — was
   `re.search(r"btn-address-coverage-gap[\s\S]{0,220}")`, i.e. *"the first 220 characters after the
   first mention of the class"*, asserting `min-width:110px` is in that window. It now iterates
   **every** `class="… btn-address-coverage-gap …"` attribute in the file and requires a fixed
   width in the following 320 characters, plus three new assertions on `_coverageGapCardHtml`
   (≥ 2 clamped lines, the dynamic `title`, the method exists).
2. `tests/test_builder_xss_contracts.py::test_escape_helpers_exist_and_cover_the_five_mark_characters`
   — was `assertIn("this._escapeAttr(g.parent_fen", source)`, i.e. *one* call site, one local's
   name. It now asserts the property over **every** `data-parent="${…}"` hole
   (`≥ 1` hole, and each one's expression starts with `this._escapeAttr(`), which covers the added
   site and is immune to the next extraction.

No assertion was removed. The SEC-J interpolation lint (`test_class_attributes_are_allowlisted_not_interpolated`,
`test_no_untrusted_interpolation_reaches_markup`) initially rejected the first draft — the lesson
is recorded in the code: the kind badge, the note colour and the button label are **ternaries
between two literals**, never a variable interpolated into a `class=` or `style=` attribute.

### 2.4 Evidence — 5 of 6 red before, 6/6 green after

New: `tests/test_builder_frontend_contracts.py::TestCoverageGapKinds` (6 tests). It executes the
**real shipped** `frontend/repertoire_builder.js` through the existing `tests/frontend_harness.js`
via `tests/frontend_runtime.run_scenario` (no new harness), and one of the six tests proves
provenance by comparing the SHA-256 the harness *executed* against the file on disk.

**AFTER**

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_builder_frontend_contracts
........................................
----------------------------------------------------------------------
Ran 40 tests in 0.309s

OK
```

**BEFORE** (pre-fix `repertoire_builder.js` + `style.css`, staged and verified by the runner)

```
test_the_harness_actually_executed_the_real_builder_source ... ok
test_the_two_kinds_render_differently ... FAIL
test_both_kinds_carry_a_clamped_line_and_a_dynamic_title ... FAIL
test_the_summary_line_counts_the_two_kinds_separately ... FAIL
test_a_legacy_payload_without_gap_kind_still_renders ... FAIL
test_an_unknown_gap_kind_is_not_labelled_unanswered ... FAIL

FAIL: test_the_two_kinds_render_differently
AssertionError: 'Not in repertoire' not found in '<div class="gap-item"> … <span class="badge-critical">12.5%</span> … <button …>Address this reply →</button>'

FAIL: test_the_summary_line_counts_the_two_kinds_separately
AssertionError: 'no prepared reply' not found in '2 unprepared opponent moves ≥ 5%'

FAIL: test_both_kinds_carry_a_clamped_line_and_a_dynamic_title
AssertionError: 'title=' not found in '…' : missing row has no hover title

Ran 6 tests in 0.043s
FAILED (failures=5)
```

The 6th (provenance) is a **"do not regress" guard**: it must be green against *any* tree, or the
other five say nothing.

---

## 3. Item 3 — the order-dependent tests in `tests/test_api.py`

### 3.1 How the bisection was done

`audit/scripts/FIXER-W_01_bisect.py` runs each of the 31 other collected modules followed, **in one
interpreter**, by the two suspect tests, and reports the verdict:

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-W_01_bisect.py
preceding module                                 account / sync verdict
------------------------------------------------------------------------------------------
… 19 modules GREEN …
tests.test_repertoire_data_integrity             GREEN
tests.test_repertoire_sync_robustness            RED
      test_account_endpoint AttributeError: 'list' object has no attribute 'get'
tests.test_repertoire_write_safety               RED
tests.test_repertoire_sync_robustness            … (RED for every module sorting after it)
…
tests.test_user_workflows                        RED
```

**A clean alphabetical cut-off at `test_repertoire_sync_robustness`** — that is the leak, and it
was a single module.

### 3.2 Root cause (two independent leaks, one symptom pair)

**Leak A — a lost stub ledger permanently replaced the process-global `requests.get`.**

`tests/test_repertoire_sync_robustness.py::_SyncTestBase` stubs the explorer with
`self._stub(rep_mod.requests, "get", _get)`. `backend/repertoire.py` and
`backend/lichess_client.py` both `import requests`, so `rep_mod.requests` **is** the `requests`
module — a stub there also answers the Lichess **account probe**, the NDJSON game stream and the
scout's HTTP calls.

Three tests in `TestExplorerBoundary` re-enter `self.setUp()` from inside a `subTest` loop (to
reset the explorer's in-process single-flight state), and `setUp()` re-initialised the ledger
**blindly**:

```python
def setUp(self):
    self._patched = []          # <- discards the previous iteration's entry
def tearDown(self):
    while self._patched:        # <- so this restores a STUB, not the original
        target, name, original = self._patched.pop()
        setattr(target, name, original)
```

Three iterations of the loop ⇒ the first two entries are lost ⇒ `tearDown` installs
`_explorer_returns.<locals>._get` from iteration 2, whose canned response is
`_FakeResponse(200, ["a", "list"])`. That object then answers **every** later module's HTTP call
for the rest of the interpreter. `LichessClient.get_account_profile()` does
`data = resp.json()` → a **list** → `if "username" in data:` is False → returns the list →
`GET /api/account` does `profile.get("perfs", {})` → **`AttributeError: 'list' object has no
attribute 'get'`** → 500 → `ERROR` (the class builds `TestClient(app)` with the default
`raise_server_exceptions=True`, so it is an ERROR and not a FAIL). And `POST /api/sync` gets
`"username" in <list>` → False → **`HTTPException(400, "Lichess username or token not configured.")`**
→ **`AssertionError: 400 != 200`**. Both symptoms, from one leak.

**Leak B — `settings` is not a fixture table, so `test_api` inherited its precondition.**

`tests/fixtures.py::FIXTURE_TABLES` lists eight tables; `settings` is not one, so
`reset_fixtures()` never truncates it. `GET /api/account` and `POST /api/sync` both resolve the
Lichess account, so `test_api` was reaching **lichess.org over HTTPS with the developer's real
token** (probe `audit/scripts/FIXER-W_01_order_probe.py` shows `lichess_username` being written
into the temp DB as `'RobiDeli'`), and `test_sync_malformed_games` only saw a `lichess_username`
row because `test_account_endpoint` sorts first in the class and had just written one. With an
empty `settings` table it answers 400 — a second, independent order dependency, and a
`AGENTS.md` §9.3 violation.

### 3.3 The fix — cause first, then the boundary

1. **Cause** (`tests/test_repertoire_sync_robustness.py:127-150`): `setUp()` now **undoes before it
   re-initialises**, making the reset idempotent and the ledger loss-proof; `tearDown()` and the
   new `_undo_stubs()` share one implementation.
2. **Backstop** (`tests/test_setup.py`): `_PROCESS_GLOBAL_SURFACES` records the pristine
   `requests.{get,post,put,delete,head,patch,request,Session}` at import
   (`_record_process_globals()`) and `_reset_process_globals()` puts them back on every
   `reset_fixtures()` — i.e. at the module boundary, which is where a *cross-module* leak belongs.
3. **The affected module declares its own preconditions** (`tests/test_api.py:16-55`): `setUp`
   writes `lichess_username = SYNTHETIC_USERNAME` and patches
   `LichessClient._resolve_token → None` and `get_account_profile →` a local profile, both
   `addCleanup`-restored, and registers `reset_fixtures()` as a cleanup so this class's own
   `settings` writes cannot leak either. `test_account_endpoint` gained 3 assertions
   (`connected is True`, `username == SYNTHETIC_USERNAME`, `token_present is False`) — 2 → 5
   assertions, nothing removed.

### 3.4 Evidence

**BEFORE** — the two FIXER-V symptoms, reproduced exactly, in one process:

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-W-01_prefix_runner.py \
      --only sync_robustness,test_setup,test_api \
      tests.test_repertoire_sync_robustness \
      tests.test_api.TestAPI.test_account_endpoint \
      tests.test_api.TestAPI.test_sync_malformed_games

test_account_endpoint ... Unhandled AttributeError while serving GET /api/account
AttributeError: 'list' object has no attribute 'get'
ERROR: test_account_endpoint
FAIL: test_sync_malformed_games
AssertionError: 400 != 200
Ran 39 tests in 1.957s
FAILED (failures=1, errors=1)
```

**AFTER** — the same command against the patched tree:

```
$ /home/robin/miniconda3/bin/python3 -m unittest \
      tests.test_repertoire_sync_robustness \
      tests.test_api.TestAPI.test_account_endpoint \
      tests.test_api.TestAPI.test_sync_malformed_games
Ran 44 tests in 2.129s

OK
```

**Order-independence, demonstrated** — the full 31-module bisection, after:

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-W_01_bisect.py
preceding module                                 account / sync verdict
------------------------------------------------------------------------------------------
tests.test_analyzer                              GREEN
…  (31 modules, every one)  …
tests.test_user_workflows                        GREEN
```

`grep -c RED` → **0**. Before the patch the same sweep was RED for the 11 modules sorting after
`test_repertoire_sync_robustness`.

**Each cause proven separately red → green.** New: `tests/test_repertoire_sync_robustness.py::TestNoProcessGlobalOutlivesItsTest`
(5 tests).

*Ledger cause* (`--legacy-ledger` reverts `_SyncTestBase.setUp`/`tearDown` to the pre-fix two
methods, in memory, so the new tests stay in place):

```
$ … --only none --legacy-ledger tests…TestNoProcessGlobalOutlivesItsTest
test_re_entering_set_up_cannot_lose_a_stub ... FAIL
AssertionError: 'fake-1' != 'real'
test_the_explorer_boundary_class_no_longer_leaves_requests_stubbed ... FAIL
AssertionError: <function TestExplorerBoundary._explorer_returns.<locals>._get …> is not <function get …>
    : TestExplorerBoundary left a requests.get stub installed, so every module that sorts
      after it inherits a fake HTTP client
Ran 5 tests in 1.323s
FAILED (failures=2)
```

*Backstop cause* (`--only test_setup` stages the pre-fix `tests/test_setup.py`, imported **after**
the swap so the swap is real):

```
$ … --only test_setup tests…TestNoProcessGlobalOutlivesItsTest
test_a_stub_on_the_shared_requests_module_does_not_outlive_reset_fixtures ... FAIL
AssertionError: <function …_leaked at 0x…> is not <function get at 0x…>
    : reset_fixtures() left a process-global replacement installed; every module that runs
      next inherits it
Ran 5 tests in 1.310s
FAILED (failures=1)
```

Both green after: `Ran 5 tests … OK`. The 3 that are green in both runs are the premise assertion
("the two backends really do share the `requests` module"), the idempotence check, and
`reset_fixtures`' behaviour with a clean global.

---

## 4. Item 4 — §4E: the panel that does not exist

### 4.1 The decision: **(b)**, and why

I did **not** add the container, and I did **not** merely delete the sentence. Option (a) is
closed by three independent facts, not by preference:

1. **It is not my file.** `frontend/index.html` and `frontend/scout.js` are outside the
   FIXER-W ownership list (`backend/repertoire.py`, `frontend/repertoire_builder.js`,
   `frontend/style.css`, `tests/**`, `AGENTS.md`). Adding `#scoutCoreIdea` / `#scoutKeyPlans` /
   `#scoutTypicalMistakes` to the HTML is a change to a file I must not touch.
2. **A live test forbids it.**
   `tests/test_scout_frontend_contracts.py::TestScoutUiContractContracts::test_dead_methods_are_removed`
   asserts `assertNotIn("'scoutCoreIdea'", scout_js)` (and the other two) — FIXER-G removed the
   dead writes and pinned their absence. Option (a) would require relaxing that test, i.e. the
   exact move `AGENTS.md` §6.4 forbids ("delete it **and say so loudly**, then add a correct
   replacement") for a *deliberate* prior decision, not a bug.
3. **The content is not lost — it is an API payload.** `strategic_ideas` is computed on every
   analyse (`backend/scout.py:388, 943, 1136`), asserted by
   `tests/test_scout.py:78-79` (`"core_idea"`), and served twice: as `study.strategic_ideas` from
   `POST /api/scout/analyze` and as `opening_theme` on `GET /api/blunders/{id}`
   (`backend/api.py:1875-1876`). So "stop the backend computing an unused payload" would be
   **wrong**: it is not unused, and removing it would break a test and a documented endpoint for
   no product gain.

Which is truer to the product? The owner of this app is a solo user building a repertoire: the
thematic content is reference material they consult, and the JSON payload is where it lives
today. A third static panel duplicating the Trainer and Builder would be the *less* truthful
option, and it is the one three prior fixers already removed. So §4E is restated to describe the
shipped behaviour and to record the decision with its enforcing test, so the next agent does not
re-open it as a "missing feature".

**Not "fixing a drift by deleting a spec that should be implemented":** the spec that should be
implemented is §4G's line-end rule, and it *is* implemented on both halves (Trainer by FIXER-V,
Builder by this patch). The sentence being corrected describes a *container*, and the codebase's
own test suite declares that container dead.

### 4.2 The change

`AGENTS.md:219-220`:

* The "Embedded Video & Chapters" bullet now says what the panel actually shows: chapter
  timestamps, opening themes, and a per-card source badge (YouTube / Master Guide / ECO — the
  three classes FIXER-H styled at `style.css:3123-3158`).
* A new bullet states where the thematic content **is** delivered (`study.strategic_ideas`,
  `opening_theme`, keys `core_idea` / `key_plans` / `typical_mistakes`,
  `OpeningBook.get_strategic_ideas()`), names the three removed ids, and names the test that
  fails if they return — plus the rule for a future panel (ship container **and** renderer
  together, and relax the test in the same change).

### 4.3 Evidence

New: `tests/test_docs_contract.py` (2 tests, the module whose stated job is "makes AGENTS.md
machine-checked"). One asserts §4E no longer states the removed promise as a shipped feature
(`assertNotRegex(body, r"chapter timestamps,[^.]*\bcore ideas\b")`) and *does* state where the
content lives and which test enforces the removal. The other asserts the **other half** of the
decision against the code: the three ids are absent from `frontend/index.html`, and
`backend/scout.py` still calls `get_strategic_ideas`.

**AFTER**

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_docs_contract
Ran 20 tests in 0.190s

OK
```

**BEFORE** (pre-fix `AGENTS.md`)

```
FAIL: test_section4e_does_not_advertise_the_removed_ideas_panel
AssertionError: Regex matched: 'chapter timestamps, opening themes, core ideas' matches
'chapter timestamps,[^.]*\bcore ideas\b' in '### E. Opening Scout & Interactive Line Explorer …
- **Embedded Video & Chapters**: Embedded YouTube player with chapter timestamps, opening
  themes, core ideas, key plans, and common tactical mistakes. …'
 : §4E still promises the removed 'core ideas' panel as a shipped feature
Ran 2 tests in 0.001s
FAILED (failures=1)
```

The second test is green in both runs: it asserts the code half, which was already true.

---

## 5. Item 5 — emitted-but-unstyled CSS classes (low priority, triage only)

FIXER-H §6.2/§6.3 and FIXER-M §7.3 listed ~23 classes. I re-measured the whole frontend with
their scanner (extended to template-literal class holes) and triaged every hit:

```
frontend/repertoire_builder.js -> ['...', 'btn-add-branch', 'btn-address-blunder',
                        'btn-address-coverage-gap', 'btn-del-rep-session-line',
                        'btn-del-rep-tree-black', 'btn-del-rep-tree-white',
                        'btn-delete-branch', 'btn-delete-move',
                        'btn-import-rep-tree-black', 'btn-import-rep-tree-white',
                        'btn-load-rep-tree-line', 'btn-set-move']
frontend/scout.js             -> ['btn-del-session-line', 'btn-del-tree-black',
                        'btn-del-tree-white', 'btn-import-tree-black',
                        'btn-import-tree-white', 'btn-load-tree-line',
                        'scout-card-meta', 'scout-line-name', 'scout-line-study-name',
                        'scout-line-study-row']
frontend/repertoire_trainer.js -> []
frontend/app.js               -> []
frontend/index.html           -> ['badge-pill', 'filter-pill-group', 'mastery-legend-row',
                        'rep-line-actions-bar', 'scout-header-actions',
                        'scout-header-titles', 'scout-modal-body', 'scout-note-bar',
                        'scout-study-selector-wrap', 'sync-status-text-wrap']
```

### 5.1 Fixed — 2 genuinely visible, plus the 2 the new gap accents needed

| class | evidence it was visible | change |
| :--- | :--- | :--- |
| `.badge-pill` | `index.html:658` is `<span id="scoutResultsCount" class="badge-pill">0</span>` — the **only** one of its three users with **no inline style**, so the Scout result count rendered as bare unstyled text beside a bold section title. The other two (`:834`, `:953`) carry full inline styles, so a base rule is additive for them. Exactly FIXER-H §6.3. | `style.css:3172-3194` — `inline-flex`, fixed `height:1.15rem`, `nowrap`, pill colours |
| `.scout-card-meta` | `scout.js:220` emits `<div class="scout-card-meta">` around the three badges FIXER-H styled — the **one** child of `.scout-card-body` with no inline style, so the badges sat flush against the title with no gap and no reserved height, and gaining/losing an ECO badge changed the card's height (§8.5) | `style.css:3204-3214` — flex row, `gap`, `flex-wrap:nowrap`, `min-height:20px` (the same 20px `.scout-card-overlap-badges` reserves) |
| `.gap-item--unanswered` / `.gap-item--missing` | new markers from item 2; a marker class with no rule would be exactly the gap this item is about | `style.css:1454-1476` — **inset** `box-shadow` accents, so the row's box is unchanged (§8) |

### 5.2 Reported, NOT styled — 12 classes, with the evidence that they are hooks, not styling targets

* **11 `btn-*` marker classes** (`btn-add-branch`, `btn-set-move`, `btn-delete-move`,
  `btn-delete-branch`, `btn-address-blunder`, `btn-address-coverage-gap`,
  `btn-del-rep-session-line`, `btn-del-rep-tree-white/black`,
  `btn-import-rep-tree-white/black`, `btn-load-rep-tree-line`, and the six Scout twins). Every
  one is emitted **as a second class on an element that already carries a styled class and a full
  inline style**, e.g.
  `frontend/repertoire_builder.js:2567`: `<button class="btn-secondary btn-address-coverage-gap"
  style="font-size:0.7rem; padding:0.18rem 0.45rem; min-width:110px; justify-content:center;">`.
  They are `querySelectorAll('.btn-…')` / `querySelector('.btn-set-move')` **handles** — deleting
  them would break the wiring, and styling them would duplicate `btn-secondary` /
  `btn-var-action` / `btn-add-move` / `btn-del-branch` and add dead rules. **Not fixable by
  "removing an emitted-but-unused class"**: they are used, just not as style hooks.
* **`scout-line-study-row`**, **`scout-line-name`**, **`scout-line-study-name`** — each carries a
  *complete* inline style at `scout.js:263, 247, 266` (background, padding, border-radius, clamp).
  The class name is a handle; the element is fully styled. Cosmetic only.
* **`filter-pill-group`**, **`mastery-legend-row`**, **`rep-line-actions-bar`**, **`scout-note-bar`**
  — `index.html:320, 450, 205, 801`, all with complete inline styles.
* **`scout-header-actions`**, **`scout-header-titles`**, **`scout-modal-body`**,
  **`sync-status-text-wrap`** — `index.html:610, 619, 928, 946, 118`. These four DO have no inline
  style, but they are plain `div`s in normal flow with no badge, no border and no conditional
  content; they inherit the parent block layout, so there is nothing to style. `scout-study-selector-wrap`
  is not in `index.html` at all — dead (reported by CODE-15's reverse direction).

**Evidence that the "fixed" three are fixed and cannot rot:**
`tests/test_layout_contracts.py::TestEmittedClassesAreStyled` gains 3 tests (5 red subTest failures
before, all green after) asserting each rule exists, carries the required declarations
(`display` / `white-space` / `font-weight` for the pill; `display` / `gap` / `min-height` and the
shared 20px for the meta row; `inset` + *distinct* declarations for the two accents), and — for
`.badge-pill` — that `#scoutResultsCount` did **not** quietly grow an inline background that would
make the rule a no-op.

```
BEFORE:  FAILED (failures=5)   # .badge-pill, .scout-card-meta, .gap-item--unanswered,
                                # .gap-item--missing, and "styled identically"
AFTER :  Ran 32 tests … OK      (whole module, tests.test_layout_contracts)
```

---

## 6. Full suite

```
$ /home/robin/miniconda3/bin/python3 -m unittest discover tests
----------------------------------------------------------------------
Ran 615 tests in 29.360s

OK
```
(exit code 0)

Golden invariants re-asserted after the trainer writes:
`tests/test_repertoire_data_integrity.py` (`Ran 26 tests … OK`, untouched by this patch — this
patch adds no write path) and `tests/test_line_end_semantics.py` (`Ran 19 tests … OK`).

Engine / production hygiene after every run above:

```
$ ps -eo pid,args | grep -c "[s]tockfish-linux"
0
$ ls -l --time-style=full-iso chess_improvement.db*
-rw-r--r-- 1 robin robin 2547712 2026-09-27 10:34:39.673058434 +0200 chess_improvement.db
```
No `-wal` / `-shm` residue; the mtime predates the first edit of this patch. Production row
counts unchanged from AGENTS.md §9.7 (`repertoire_moves 0`, `repertoire_studies 0`,
`study_moves 0`). No test in this patch starts an engine, and none contacts port 38281.

## 6.1 `AGENTS.md` changes (documentation is part of the build)

| lines | change |
| :--- | :--- |
| `51` | §2 test count `595 → 615` (module count stays 33) |
| `219-220` | §4E — the removed panel replaced by the API payload, the removed ids, and the enforcing test |
| `272-303` | §4G — the **Builder** half (the render contract, the zero-CLS argument, the totality of `_isUnansweredBranch`) |
| `305-321` | **new §4G2** — the deterministic-lookup rule: the one tie-breaker, all six sites, and *why* `-` sorts first |
| `668-696` | §9.2 — item 5 now covers the process-global surfaces, names the concrete leak, and pins the new test; plus the new "**`settings` is deliberately NOT a fixture table**" paragraph |
| `715-721` | §9.3 — "No live network" now states the `tests/test_api.py` hermetic precondition |

`tests/test_docs_contract.py` passes (20 tests), including the two new §4E checks and the
§2/§3/§5 re-derivations.

---

## 7. Residual risks

1. **The `-`-before-a-file-letter ordering is the load-bearing part of item 1.** The tie-breaker is
   what makes it correct, and the tie-breaker is *right regardless of the ordering*; but the
   **tests** depend on the twin sorting first, because that is what made the pre-fix code
   observably wrong. If SQLite's planner changed and the pre-fix code became accidentally right,
   `TestPreparedMoveLookupIsDeterministic` would go green for the wrong reason. The tests assert
   the premise explicitly (`assertLess(_R_NOEP, _R_EP)` and `assertLess(twin, exact)`), so a change
   of plan would surface as a *premise* failure with a message saying which ordering stopped
   holding — not as a silent pass.
2. **`settings` is still not a fixture table**, by design (§9.2 now says so). Any *other* module
   that reads a `settings` row without declaring its precondition is still order-dependent. I
   fixed the one module the audit proved it for; I did **not** add `settings` to
   `FIXTURE_TABLES`, because truncating it would delete `max_opening_ply` /
   `blunder_threshold_cp` / `gap_threshold` (seeded by `init_db()`, which `reset_fixtures()` only
   re-runs when a table is *missing*) and could break modules that rely on a setting persisting.
   That is a bigger blast radius than this brief, and it needs its own before/after.
3. **`_reset_process_globals()` restores a *recorded* value, not "the real function".** If a module
   replaced `requests.get` *and* `reset_fixtures()` had not yet recorded the baseline (i.e. before
   `tests/test_setup.py` finished importing), nothing would be restored. In practice the record
   happens at `tests/test_setup.py` import, which every module imports first, and
   `test_the_stubbed_module_really_is_the_shared_requests_module` pins the premise. It is a
   backstop, not a substitute for the ledger fix — which is why both are tested independently.
4. **The item-2 change alters the visual structure of one list.** The coverage-gap row's text
   lines are now a single column rather than three side-by-side flex items (see §2.2). This is
   strictly more correct, and the *two kinds* are pixel-identical in structure, so there is no
   shift between them — but it is a visible change to `#repCoverageGapsList` and deserves one
   manual Ctrl+R before shipping.
5. **`.badge-pill` is now styled, so `#scoutTreeCountBadge` and `#studyModalColorBadge` get a base
   rule underneath their inline styles.** Inline wins for every property they set, so they are
   unchanged today. If someone later *removes* one of those inline styles, they will inherit this
   pill's geometry rather than rendering bare — the desired direction, but a coupling worth
   knowing.
6. **`_isUnansweredBranch` treats an unknown `gap_kind` as `missing_branch`.** That is the safe
   direction (a new kind must not be told "you have no answer" without a human deciding it), but
   it means adding a third kind server-side without touching the Builder shows the new kind with
   the *missing* wording until the frontend catches up. `test_an_unknown_gap_kind_is_not_labelled_unanswered`
   documents the choice so it cannot drift silently.
7. **`backend/repertoire.py:3467-3471`** (`seed_default_repertoires`' planning probe) still
   `fetchone()`s an `is_user_move = 1` lookup on an exact 4-token `parent_fen` that can return more
   than one row in a legacy DB. It is not a `LIMIT 1` and it only picks which *message* to emit
   (both possible outcomes are safe: preserve, or abandon the line) — but the reported move in a
   "the user prepared X" conflict can be either of two rows. Left alone to keep the diff surgical;
   one `ORDER BY id ASC LIMIT 1` would close it. See §8.

## 8. Also noticed (not fixed)

1. **`backend/repertoire.py:3467-3471`** — see §7.7. `SELECT move_uci FROM repertoire_moves WHERE
   repertoire = ? AND parent_fen = ? AND is_user_move = 1` with no `LIMIT`/`ORDER BY`. Exact
   4-token match, so the only multi-row case is a legacy DB with two prepared moves at the *same*
   4-token parent (possible: the UNIQUE key includes `move_uci`). Behaviour is safe either way —
   the plan entry is either preserved or the line is abandoned — only the *message* can name the
   wrong move. Not the `LIMIT 1` class, so out of scope here.
2. **`settings` truncation is still not done** — §7.2. The one-line change
   (`"settings"` in `tests/fixtures.py::FIXTURE_TABLES`) is *not* safe on its own, because
   `reset_fixtures()` would then leave `max_opening_ply` / `blunder_threshold_cp` /
   `gap_threshold` unset, and every `get_setting(key, default)` caller has to be re-checked. Needs
   its own before/after, not a drive-by.
3. **`tests/test_api.py` still has one real outbound call.** With `_resolve_token → None`,
   `test_repertoire_tree` asks the 2000+ explorer about a position the fixture never seeded and
   gets a deterministic **HTTP 401** (previously a live 200 with the developer's real token). The
   assertion (`200` + key presence) does not depend on it, and the suite's own budget is
   unaffected, but the honest fix is for that test to
   `tests.fixtures.seed_explorer_for_positions()` first. Pre-existing, and pinning it would change
   what the test covers.
4. **`.scout-overlap-banner` remains absent from `style.css` while `#scoutActiveLineOverlap` is
   live** — FIXER-H's judgement call, already pinned by
   `test_dead_overlap_banner_css_is_gone`. Unchanged here.
5. **The `btn-*` marker classes (§5.2) would be cleaner as `data-*` attributes.** They are
   `querySelector` targets on styled elements, so a `data-role="set-move"` would read better than
   a fake class name. That is a ~40-site refactor across three files I do not all own, for zero
   behaviour change. Not done.
6. **Production DB inspection** in this patch was byte-level only (`ls -l` / `fingerprint`-style
   mtime check) — no SQLite handle was opened at all, so §9.1's `open_production_readonly()` was
   not needed. `AGENTS.md` §9.7's row counts are therefore quoted from the previous audit rather
   than re-read; the file's mtime is unchanged, so those counts still hold.
