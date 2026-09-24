# SCOUT-02 — Whole-line import silently deletes the user's prepared user move *and* its whole subtree
- **Severity:** critical
- **Type:** data-loss
- **File:** backend/scout.py (`import_line_to_repertoire`), backend/repertoire.py (`add_repertoire_move`), frontend/scout.js (`importToRepertoire`, `importAllLinesToRepertoire`)
- **Line(s):** backend/scout.py:754-835 (esp. 762 and 768-804); backend/repertoire.py:477-525; frontend/scout.js:1594-1772
- **Component:** Opening Scout → whole-line repertoire import
- **Repro script:** `audit/scripts/atk4_02_divergence_data_loss_repro.py`
- **Detected by:** ATTACKER-4 (SCOUT)

## Expected
`AGENTS.md` §4E: *"If a user move diverges at a turn where a move is already prepared, warns
that the new move replaces the prepared move."* A destructive, irreversible replacement of the
user's memorised line must require an explicit confirmation and must not be reachable from a
single button press / single API call.

## Actual
`import_line_to_repertoire` **computes** the overlap report and then **ignores it** — it writes
every move in the list unconditionally:

```python
overlap = rm.check_line_overlap(repertoire=repertoire, moves=moves)   # line 762
...
for m in moves:                                                       # line 768
    ...
    res = rm.add_repertoire_move(..., is_user_move=is_user_move, ...)  # line 786
```

`add_repertoire_move` then does exactly what `AGENTS.md` §9.3 warns about — delete the
prepared move and recursively orphan-prune its descendants (`repertoire.py:484-520`).

Observed run (the user's real Anti-French preparation from §9.3, then a scouted line that
diverges at 3rd move):

```
seed import -> {"imported_count": 9, "overlap_status": "new", ...}
  e2e4 e4 is_user_move=1 study='Anti French'
  e7e6 e6 is_user_move=0 study='Anti French'
  d2d4 d4 is_user_move=1 study='Anti French'
  d7d5 d5 is_user_move=0 study='Anti French'
  e4e5 e5 is_user_move=1 study='Anti French'      <-- user's memorised move
  c7c5 c5 is_user_move=0 study='Anti French'
  b1c3 Nc3 is_user_move=1 study='Anti French'
  b8c6 Nc6 is_user_move=0 study='Anti French'
  g1f3 Nf3 is_user_move=1 study='Anti French'

/api/scout/check_line says:
   status     = partially_present
   divergence = {'ply': 5, 'type': 'user_move_alternative', 'existing_san': 'e5', 'new_san': 'Nc3'}
   message    = First 4 plies already in White repertoire. At move 5, Nc3 replaces your prepared e5 for White!

/api/scout/import_repertoire response:
   status          = 'success'
   overlap_status  = 'partially_present'
   imported_count  = 6
   message         = '✓ First 4 plies already in White repertoire. At move 5, Nc3 replaces your prepared e5 for White!'
   errors          = []

  --- repertoire after divergent import: 6 move(s)
      e2e4 e4  is_user_move=1
      e7e6 e6  is_user_move=0
      d2d4 d4  is_user_move=1
      d7d5 d5  is_user_move=0
      b1c3 Nc3 is_user_move=1      <-- replacement
      g8f6 Nf6 is_user_move=0

  3. e5 still present?         False
  3. e5 child  (4. c5) present? False
VERDICT: USER MOVE + SUBTREE DESTROYED
```

The frontend makes this a **single unconfirmed click**. `importToRepertoire` (scout.js:1662-1687)
only inspects `data.status === 'success'`:

```js
if (data.status === 'success') {
    const isAlreadyPresent = !!data.already_present;
    const successMsg = isAlreadyPresent ? `✓ In ${repCap} Repertoire (${studyName})`
                                       : `✓ Added to ${repCap} Repertoire (${studyName})`;
    this._showQuickAddNotice(successMsg, ...);
```

`data.overlap_status`, `data.overlap_details.divergence_info` and the destructive part of
`data.message` are never read, and there is no `confirm()` — unlike
`removeCurrentLineFromRepertoire` (scout.js:1844) and `removeTreeLineFromRepertoire`
(scout.js:1878) which *do* confirm. The green toast claims success.

`importAllLinesToRepertoire` (scout.js:1695) is worse: it feeds `getAllUniqueMoves()` (the whole
tree, in DFS order) into the same destructive import, so one "📥 All → White" click can
overwrite every prepared user move in the study.

## Root cause
The overlap report is advisory only. There is no `force`/`confirm_replace` flag threaded from
the UI into `import_line_to_repertoire`, and `add_repertoire_move`'s "one user move per position"
enforcement is destructive by design with no undo and no soft-delete.

## Exploit / escalation
1. `POST /api/scout/import_repertoire` (or the aliased `POST /api/repertoire/import_line`) with
   any `moves` list whose user-move plies differ from the prepared ones wipes those lines.
2. Clicking "📥 All → White" once destroys *every* prepared user move that differs anywhere in
   the session tree — the deletion is silent and unconfirmed.
3. Because the pruning cascade is recursive and uses a 3-token FEN `LIKE` match, collateral
   damage can extend to *transposed* lines that were never part of the imported line (see
   SCOUT-05).
4. Re-importing the original line does not restore the deleted moves' `is_important` flags,
   `comment`, `arrows` or `study_name` — those columns were deleted with the row.
