# BUILDER-13 — `init_db()` deletes 65% of the repertoire on every boot, and the production repertoire is *currently* empty because of it
- **Severity:** critical
- **Type:** data-loss
- **File:** backend/db.py
- **Line(s):** 322-364 (step 3 of `migrate_studies_and_cleanup_blunders`) and 366-404 (step 4, "Ensure default studies exist if none configured"), invoked unconditionally from `init_db()` (line ~237) which FastAPI calls from the `startup` event (`backend/api.py`)
- **Component:** Repertoire Builder — database migration at boot
- **Repro script:** `audit/scripts/atk1_b13_migration_safety.py`
- **Detected by:** ATTACKER-1 (BUILDER)

## Expected

`init_db()` runs on **every** application start (`start.py` → FastAPI `startup_event` →
`init_db()`). A schema migration must be non-destructive by default: it may create tables, add
columns and backfill derived values, but it must never `DELETE` the user's repertoire, and it
must never fabricate studies — least of all the three default ones that AGENTS.md §9.3
explicitly forbids ("a repertoire only permits one user move per position and seeding generic
lines (like `3. Nc3` in the French) overwrites and prunes the user's prepared lines (`3. e5`)").

## Actual — Part 1: one legitimate fragment is enough

```
inserted fragment -> 200 e8=Q  study_name = ''
study_moves rows (belong to 'Anti French' only): 5
rows before boot: 6 ['e4', 'e5', 'Nf3', 'Nc6', 'Bb5', 'e8=Q']
rows after  boot: 5 ['e4', 'e5', 'Nf3', 'Nc6', 'Bb5']
fragment survived: False <-- GONE, silently, on the next application start
```

`8/4P3/8/8/8/8/k7/4K3 w - -` → `e8=Q` is a perfectly legal `POST /api/repertoire/move`; the row
is accepted with `study_name = ''`, so it gets **no `study_moves` link** — and the purge's only
protection is exactly that link. One restart, one row, gone, no message, no backup.

## Actual — Part 2: the 243 recovered production rows

`recovered/repertoire_moves_from_git_HEAD.json` in this repo carries the note:

> *"Recovered from git HEAD:chess_improvement.db after
> `migrate_studies_and_cleanup_blunders()` erased them on boot. Live schema has since added
> `study_name`."*

Replaying exactly those 243 rows into the throwaway temp DB and calling `init_db()` **once**:

```
rows loaded                      : 243
rows with a study_moves link      : 0
sample of the loaded rows:
   id=11525  white  e8=Q    parent=8/4P3/8/8/8/8/k7/4K3 w - -
   id=11531  white  O-O-O   parent=r3k2r/8/8/8/8/8/8/R3K2R w KQkq -
   id=11532  white  exf6    parent=8/8/8/4Pp2/8/8/8/4K2k w - f6
   id=11804  white  Nc6     parent=rnbqkbnr/pp1ppppp/8/2p5/4P3/1P6/P1PP1PPP/RNBQKBNR b KQkq -
   id=11805  white  d6      parent=rnbqkbnr/pp1ppppp/8/2p5/4P3/1P6/P1PP1PPP/RNBQKBNR b KQkq -
   id=11806  white  e6      parent=rnbqkbnr/pppp1ppp/8/2p5/4P3/1P6/P1PP1PPP/RNBQKBNR b KQkq -

--- one application start (init_db) ---

rows after a single boot         : 86
ROWS PERMANENTLY DELETED         : 157  (65% of the repertoire)

GET /api/repertoire/studies ->
   {'id': 6, 'name': 'Italian Game',          'repertoire': 'white', 'total_moves': 52, 'user_moves': 24}
   {'id': 8, 'name': "King's Indian Defense", 'repertoire': 'black', 'total_moves': 15, 'user_moves': 8}
   {'id': 7, 'name': 'Sicilian Dragon',       'repertoire': 'black', 'total_moves': 19, 'user_moves': 9}
```

**157 of 243 moves destroyed by a single boot**, and the three studies the user never asked for
are fabricated to fill the gap — precisely the `3. Nc3` / `3... Nf3` overwrite that AGENTS.md
§9.3 says must never happen.

## Actual — Part 3: the live production database is already empty

Read-only inspection of `chess_improvement.db` (opened with `mode=ro`; no probe in this audit
ever opened it for writing):

```
   games                307
   blunders             153
   drill_reviews        153
   scouted_content      31
   explorer_cache       444
   repertoire_moves     0     <-- the user's Anti-French / Caro-Kann studies are GONE
   repertoire_studies   0
   study_moves          0
```

The 243 recovered moves are sitting in `recovered/repertoire_moves_from_git_HEAD.json` and
**have not been re-imported**. If they are re-imported while the current `init_db()` is in
place, the next application start will destroy 65% of them again.

## Root cause

```python
# backend/db.py:322-364  (migrate_studies_and_cleanup_blunders, step 3)
# 3. Purge truly isolated orphan moves using 3-token FEN normalization (protecting study moves)
start_3 = ' '.join(start_fen.split()[:3])
for rep in ('white', 'black'):
    ...
    for r in rows:
        if r["study_id"]:
            study_move_ids.add(r["id"])            # the ONLY protection
        p_3 = ' '.join(r["parent_fen"].split()[:3])
        parent_map.setdefault(p_3, []).append(r["id"])
        dest_map[r["id"]] = ' '.join(r["fen"].split()[:3])
    queue, visited, reachable_ids = [start_3], set(), set()
    while queue:                                    # BFS from the start position
        ...
    orphan_ids = all_ids - reachable_ids - study_move_ids
    if orphan_ids:
        cursor.execute(f"DELETE FROM repertoire_moves WHERE id IN ({placeholders})",
                       tuple(orphan_ids))           # <-- unconditional, no dry-run, no backup
```

and step 4 then manufactures the forbidden defaults:

```python
# backend/db.py:366-404
if studies_count == 0:
    if <white moves exist>:
        INSERT "Italian Game"           ... ; link every white move into it
    if <black moves exist>:
        INSERT "Sicilian Dragon" / "King's Indian Defense" ...
        UPDATE repertoire_moves SET study_name = ... WHERE study_name IS NULL OR = ''
```

Three design errors compound:

1. **"Unreachable from the start position" is treated as "garbage".** For this application it
   is a *normal* state: the Scout whole-line importer (AGENTS.md §4.E), the
   `import_line_to_repertoire` endpoint and any line the user starts from a puzzle position
   legitimately create sub-trees whose root is not the start position — as does the recovered
   production data itself (`8/4P3/8/8/8/8/k7/4K3 w - -` → `e8=Q`,
   `r3k2r/8/8/8/8/8/8/R3K2R w KQkq -` → `O-O-O`).
2. **The protection is a join against a relation that those rows do not have.** A row inserted
   without `study_name` gets no `study_moves` row, so the one guard that would have saved it is
   absent by construction.
3. **Reachability is computed on a 3-token FEN key while the stored values are 4-token**, so
   the walk silently merges distinct positions and cannot distinguish "same position" from
   "same first three FEN tokens". A repair that keeps a reachability test but keys it on
   `normalize_fen` (4 tokens) and treats "not reachable" as *reportable, never deletable* is
   required; an unconditional `DELETE` inside a function that runs on every boot is not
   acceptable at any false-positive rate.

## Exploit / escalation

This is not attacker-triggerable — it fires on the **normal** path:

1. **Any imported sub-line.** Scout "📥 Line → White/Black" and
   `POST /api/repertoire/import_line` create branches; every restart re-evaluates them as
   orphans.
2. **`move_meta` auto-insert (BUILDER-02) makes it worse.** Because a metadata write on an
   unprepared position silently *inserts* the move with whatever `study_name` it was given
   (possibly none), it manufactures unprotected rows that the next boot deletes — and, being
   outside any study, it also *prunes* a prepared line first. Data destroyed on write, then
   again on restart.
3. **`clear_repertoire_database` / the seed guard do not help.** `POST /api/repertoire/seed`
   is guarded by `force=true`, but step 4 of the migration is not guarded at all and runs on
   every boot whenever `repertoire_studies` happens to be empty.
4. **Amplifier — the purge and step 4 feed each other.** The purge empties the studies'
   contents, step 4 refills `repertoire_studies`, so on the *next* boot the "no studies" branch
   does not fire — meaning the damage is not repeated forever, but the first boot is enough to
   lose 65% of the work and to relabel what remains into studies the user did not create.
5. **Blast radius beyond the tree.** `times_practiced` / `times_correct` / `next_review` /
   `interval_days` for every deleted move are gone, so the spaced-repetition schedule
   (`get_next_trainer_drill`, `get_trainer_progress`) is silently reset for the whole study.

### Fixer notes (what "fixed" must look like)

* `migrate_studies_and_cleanup_blunders` must be **non-destructive by default**: report
  unreachable rows (log/return them) and require an explicit opt-in (`init_db(purge_orphans=True)`
  or an env flag) plus a loud banner to delete anything.
* Any reachability test must key on `normalize_fen` (4 tokens), must treat a move whose
  `parent_fen` is not any row's `fen` as "unlinked" (reportable) rather than "garbage", and
  must additionally protect every row that has a non-empty `study_name`.
* Step 4 must be removed, or at minimum gated behind the same explicit opt-in, and must never
  create the three studies AGENTS.md §9.3 names.
* Add a regression test: load a repertoire containing an imported fragment and an endgame
  fragment, call `init_db()`, assert the row count is unchanged.
* **Operational follow-up (not a code change):** re-import
  `recovered/repertoire_moves_from_git_HEAD.json` (243 rows) into the production DB *after* the
  fix lands, and restore the `Anti French` / `Caro-Kann Fantasy Variation` study names.
