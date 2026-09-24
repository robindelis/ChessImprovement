# SCOUT-11 — Whole-line import lies about what it did: writes on `already_present`, drops SAN-only moves, and reports success with 0 rows written
- **Severity:** high
- **Type:** wrong-result
- **File:** backend/scout.py (`import_line_to_repertoire`), backend/api.py (payload models)
- **Line(s):** scout.py:754-835 (esp. 768-772, 799-804, 813-821); api.py:96-107
- **Component:** `/api/scout/import_repertoire`, `/api/repertoire/import_line`, `/api/scout/check_line`
- **Repro script:** `audit/scripts/atk4_11_import_contract_repro.py`
- **Detected by:** ATTACKER-4 (SCOUT)

## Expected
`AGENTS.md` §4E: `already_present` *"prevents redundant writes"*. The response fields must agree
with each other and with the database. A move list the check accepts must be importable.

## Actual

**(1) `already_present` still performs the write — and re-labels the study while claiming
"No changes needed".**

```
response: {"already_present": true, "overlap_status": "already_present",
           "imported_count": 4, "new_count": 0,
           "message": "✓ Already present in White repertoire! All 4 plies are already in your
                       opening tree. (No changes needed)"}

re-importing an already_present line with study B -> ✓ ... (No changes needed)
study_name before: [('d4', 'Study A'), ('d5', 'Study A')]
study_name after : [('d4', 'Study B'), ('d5', 'Study B')]  <-- rows rewritten
```

`import_line_to_repertoire` computes `overlap` and then ignores it; the loop at line 768 runs
for every move regardless. `add_repertoire_move` has since been converted to an idempotent
`ON CONFLICT ... DO UPDATE` UPSERT (row `id`/`created_at` are preserved, so the damage is
limited), but it still **writes** every move and still overwrites the caller-supplied
`study_name` on moves the user had already filed under a different study. The response is
internally contradictory: `imported_count: 4` next to `new_count: 0` next to
"(No changes needed)".

**(2) SAN-only move lists are counted by the check and silently dropped by the import.**

```
check_line  -> {"status": "new", "total_plies": 6, "matched_plies": 0, "new_plies": 6}
import      -> {"status": "success", "imported_count": 0, "new_count": 6,
                "message": "✓ Added 0 moves to White repertoire.", "errors": []}
rows before/after: 2 / 2
frontend would show: badge '+6 new' then toast '✓ Added 0 moves to White repertoire.'
```

`_evaluate_line_against_index` has an `elif san:` branch that resolves SAN against a board
(repertoire.py:988-993), but `import_line_to_repertoire` requires `uci`:

```python
parent_fen = m.get('fen_before')
move_uci = m.get('uci')
if not parent_fen or not move_uci:
    continue                      # <-- silent skip, no error recorded
```

The badge promises `+N new`, the click writes nothing, and `errors` stays empty. The same skip
silently drops any move object without `fen_before` — e.g. a *Lichess-study* or *ECO-guide*
variation whose move dicts were built before `fen_before` existed.

**(3) A wholly invalid move list returns `status: "success"`.**

```
moves: [{"fen_before": "not-a-fen", ...}, {"fen_before": "8/8/8/8/8/8/8/8 w - -", ...}, ...]
response: {"status": "success", "imported_count": 0, "errors": ["expected 8 rows ...",
          "Invalid FEN string", "Illegal move: invalid uci: 'e2e9'"],
          "message": "✓ Added 0 moves to White repertoire."}
```

`scout.js` shows the green `✓` toast because it only checks `data.status === 'success'`.
`errors` is truncated to the first 3 (`errors[:3]`) so the user never sees all failures.

**(4) `importAllLinesToRepertoire`'s DFS order destroys sibling branches.** One
"📥 All → White" click posts the whole session tree in `getAllUniqueMoves()` (DFS) order:

```
DFS move order: ['e2e4', 'c7c5', 'g1f3', 'd2d4', 'd7d5', 'c2c4']
response: {"imported_count": 6, "overlap_status": "partially_present",
           "message": "✓ Start of line is already present in White repertoire (1/6 plies exist).
                       5 new moves (c5 Nf3 d4 d5...) will extend this line."}
surviving moves: [('d2d4','d4',1), ('d7d5','d5',0), ('c2c4','c4',1)]
```

`1.d4` replaces `1.e4` and the recursive prune removes the `1.e4 c5 2.Nf3` branch that had
*already been inserted* by the same batch. The overlap message ("1/6 plies exist") is
meaningless for a multi-branch batch. This is the same destruction as SCOUT-02 but reachable
from a single "import everything" button and it silently discards half the explored tree.

**(5) Arbitrary unreachable positions can be grafted into the repertoire tree.**

```
detached mid-game branch -> 1
stored row: parent_fen 'r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq -',
             uci 'c6a5', san 'Na5', is_user_move 0
```

`add_repertoire_move` validates `board.is_valid()` and move legality but never checks that the
parent position is reachable inside the user's own tree, so a crafted payload creates
floating branches (which then confuse the 3-token prune of SCOUT-12).

**Checks that came back clean:** an 8-queen FEN is rejected (`Illegal move`), an opponent reply
after `1.e4` is stored with `is_user_move=0`, and a bare string inside `moves` is a clean 422
from pydantic (`{"type": "dict_type", "loc": ["body","moves",0]}`).

## Root cause
`import_line_to_repertoire` treats the overlap report as cosmetic, silently `continue`s past
unusable moves, and hard-codes `status: 'success'` regardless of `imported_count`.

## Exploit / escalation
1. Clicking "📥 Extend White (+N)" when the payload contains SAN-only moves is a silent no-op
   that still reports success and clears the badge state.
2. Repeated "📥 Line → White" clicks on the same line with different study names reassign study
   membership without the "(No changes needed)" message being anything but a lie.
3. "📥 All → White" on any tree containing two different first moves keeps only the last one.
4. A crafted `moves` array of unreachable FENs lets an attacker (or a buggy scraper) inject
   arbitrary positions as opponent branches that the prune logic will later use as collateral.
