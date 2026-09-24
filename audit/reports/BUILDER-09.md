# BUILDER-09 — Study names are normalised by `create_study` but not by `add_repertoire_move`/`update_move_metadata`, so one study name becomes two phantom studies
- **Severity:** low
- **Type:** wrong-result
- **File:** backend/repertoire.py
- **Line(s):** 790-798 (`create_study`: `name = re.sub(r'[\s\u200b\u200c\u200d\ufeff]+', ' ', name or "").strip()`) vs. 537 / 569 / 981 / 992 (`final_study = (study_name.strip() ...)`, `study_name.strip()`) and 781-788 (`get_study_by_name`: `name.strip()`)
- **Component:** Repertoire Builder — study dropdown
- **Repro script:** `audit/scripts/atk1_b09_study_names.py`
- **Detected by:** ATTACKER-1 (BUILDER)

## Expected

A study is identified by its name (`repertoire_studies` has `UNIQUE(name, repertoire)`, and
`study_moves`/`repertoire_moves.study_name` are plain text). Every write path that can create a
study must therefore apply **the same** normalisation, otherwise the same logical name creates
two rows and the user's moves are split across studies that look identical in the UI.

## Actual

```
create_study('Anti  French') -> 'Anti French'          (internal whitespace collapsed)

add(..., study_name="Anti  French") via POST /api/repertoire/move
study rows now:
   {'id': 1, 'name': 'Anti French',  'repertoire': 'white'}
   {'id': 2, 'name': 'Anti  French', 'repertoire': 'white'}

GET /api/repertoire/studies ->
   'Anti  French' total_moves = 1
   'Anti French'  total_moves = 0
```

The study created through the dialog is empty; the moves went into the phantom. Adding a
zero-width joiner and a U+202E RIGHT-TO-LEFT OVERRIDE behaves the same way:

```
after a zero-width + RLO study_name via POST /api/repertoire/move:
   'Anti French'
   'Anti  French'
create_study with the same zero-width/RLO name -> success 'Anti French\u202e'

final study list (note the visually identical entries):
   1 'Anti French'
   2 'Anti  French'
   3 'Anti French \u202e'   (U+202E RIGHT-TO-LEFT OVERRIDE, renders reversed)
```

In the Builder's `<select id="builderStudySelect">` the three options are rendered with
`opt.textContent = \`${s.name} (${s.total_moves} moves)\`` — HTML collapses the double space, so
options 1 and 2 are visually indistinguishable, and option 3 renders with its tail reversed.

## Root cause

Three different normalisations for the same identifier:

```python
# create_study -- collapses runs of whitespace and strips zero-width chars
name = re.sub(r'[\s\u200b\u200c\u200d\ufeff]+', ' ', name or "").strip()

# add_repertoire_move -- only strips the ends
final_study = (study_name.strip() if study_name and study_name.strip()
               else (existing_meta["study_name"] ...))
...
cursor.execute("INSERT OR IGNORE INTO repertoire_studies (name, repertoire, ...) VALUES (?, ?, ...)",
               (final_study, repertoire, ...))          # <- creates the phantom row

# update_move_metadata -- only strips the ends
if study_name is not None and study_name.strip():
    updates.append("study_name = ?"); params.append(study_name.strip())
```

There is no canonical `normalize_study_name()`, and `get_study_by_name()` (used by
`create_study`'s duplicate path) also only `.strip()`s, so the duplicate detection does not
catch the divergence either.

## Exploit / escalation

Adjacent weaker variants:

1. **Bidi/zero-width names are storable at all.** `repertoire_studies.name` is echoed into the
   study `<select>` and into every `study_moves`-derived report, so a name containing
   U+202E/U+2066-U+2069 can make one study's move list render as another study's.
2. **Non-breaking space (U+00A0)** is matched by `\s` in `create_study` but survives
   `.strip()`-only paths — the most likely real-world trigger, since names pasted from web
   pages or video titles routinely contain NBSPs.
3. **The reverse direction is also broken:** there is no way to *un*-assign a move from a study
   (`study_name=""` is ignored by `if study_name is not None and study_name.strip()`), so a
   mis-filed move can only be fixed by deleting and re-adding it — which is BUILDER-02's
   destructive path.
