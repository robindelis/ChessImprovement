# BUILDER-11 — No length or shape limits on `comment`, `arrows`, `study_name` or study `description`: multi-megabyte values are stored and re-served on every read
- **Severity:** low
- **Type:** security
- **File:** backend/repertoire.py
- **Line(s):** 431-443 (`add_repertoire_move` signature — all free-form `Optional[str]`), 922-931 (`update_move_metadata`), 790-751 (`create_study`), and the pydantic models in backend/api.py (`RepertoireMovePayload`, `UpdateMoveMetaPayload`, `StudyCreatePayload`)
- **Component:** Repertoire Builder — notes / arrows / study names
- **Repro script:** `audit/scripts/atk1_b04_hostile.py` (sections A, B, D)
- **Detected by:** ATTACKER-1 (BUILDER)

## Expected

Free-text user fields should be bounded and, for structured fields, shape-checked:

* `comment` — a note; a few kB is generous.
* `arrows` — a JSON *array* of `{from,to,color}` objects; the board renderer creates two SVG
  nodes per entry, so a cap is a hard requirement, and `JSON.parse`/`Array.isArray` should be
  validated server-side rather than in the browser.
* `study_name` / study `description` — rendered into a `<select>` / list, so a cap keeps the
  studies endpoint small.

## Actual

```
A. /api/repertoire/move  (POST)
  [comment 2MB]        -> 200 {'status':'success', ..., 'comment':'AAAA…'}          (stored)
  [arrows junk]        -> 200 {'status':'success', ..., 'arrows':']]]{{{'}
  [study_name 1MB]     -> 200 {'status':'success', ..., 'study_name':'SSSS…'}

B. /api/repertoire/move_meta (POST)
  [arrows 1MB on existing] -> 200 …  (stored; DB shows alen = 1000000)

D. /api/repertoire/studies
  [create name 1MB]     -> 200 {'status':'success','study':{'name':'NNNN…'}}
  [create desc 1MB]     -> 200 {'status':'success','study':{…,'description':'DDDD…'}}
  [create name RTL-override] -> 200 {'name': 'Anti‮French‬'}      (bidi override stored verbatim)
```

`GET /api/repertoire/studies` then re-serves every one of those values, and
`GET /api/repertoire/tree` re-serves every stored `comment`/`arrows` for every move
(see BUILDER-06: ~1.4 kB per `repertoire_moves` row is *baseline*, and that baseline is
duplicated by the 3-token alias key). The pydantic models constrain the numbers
(`popularity_pct: Field(0.0, ge=0.0, le=100.0, allow_inf_nan=False)`) but not the strings:

```python
# backend/api.py
class RepertoireMovePayload(BaseModel):
    ...
    comment: Optional[str] = None
    arrows: Optional[str] = None
    study_name: Optional[str] = None
class StudyCreatePayload(BaseModel):
    name: str
    repertoire: Literal['white', 'black']
    description: Optional[str] = ""
```

`arrows` in particular is only validated in the browser:
`board.setArrows()` does `Array.isArray(arrows) ? arrows.filter(a => this._isValidArrow(a)) : []`,
so `"]]]{{{"` is stored happily and the count element ends up reading `0 arrows` after a
`JSON.parse` failure — the stored value is simply lost, with no error at write time.

## Root cause

The write path treats every user string as opaque text and stores it verbatim
(`final_comment = comment.strip()`, `final_arrows = arrows.strip()`, `final_study =
study_name.strip()`); there is no `Field(..., max_length=...)` on the pydantic models, no
length check in the manager, and no JSON validation for `arrows`. The same values are then
echoed by `add_repertoire_move`'s response, by `update_move_metadata`'s full-row
`"move": updated_row`, and by `get_studies`/`get_repertoire_tree`.


## Cross-reference / dedup note

`ABUSE-09` ("No request-size or field-size cap anywhere: 32 MB bodies accepted and 4 MB
secrets/study names persisted into SQLite") covers the generic missing-cap problem and is the
better place to fix the field-length validation. The distinct content here is (a) the
**client-side amplification** of the unvalidated `arrows` field (a 1 MB array of valid-looking
arrows becomes hundreds of thousands of SVG nodes in `board._renderArrows()`), and (b) the
**explicit negative result** that no stored XSS is reachable from these fields — every sink in
`repertoire_builder.js` uses `textContent` / `.value` / `JSON.parse` + `setAttribute`, so a
fixer should not spend budget looking for one.

## Exploit / escalation

Adjacent weaker variants:

1. **Self-inflicted DoS is the realistic case, not an external attacker.** This is a local
   single-user app, so the exposure is a fat-fingered paste; the impact is a
   `repertoire_moves` table (and every tree read) that carries megabytes per row.
2. **`arrows` is a memory/CPU amplifier on the client.** A 1 MB JSON array of valid-looking
   arrows passes `_isValidArrow`, and `_renderArrows()` then creates 2 SVG nodes per entry
   (`marker` + `line`) plus a `defs` child, i.e. hundreds of thousands of DOM nodes, freezing
   the tab.
3. **Bidi/override characters in `study_name` are stored and rendered into the study
   `<select>`** (see BUILDER-09 variant 1), and into the Trainer drill titles.
4. **No XSS was found** — worth recording explicitly, since it is the obvious worry: every
   rendering sink in `repertoire_builder.js` that touches these fields uses `textContent`
   (`opt.textContent = …`, `commentEl.value = …`, `chip.textContent = …`) or `JSON.parse` +
   `setAttribute` (`board.setArrows` → `line.setAttribute('stroke', arr.color)`), and the
   server-generated `move_san` values are produced by `chess.Board.san()`. A stored
   `<img src=x onerror=alert(1)>` comment round-trips untouched but is never parsed as HTML.
