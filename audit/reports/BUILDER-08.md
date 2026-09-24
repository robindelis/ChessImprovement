# BUILDER-08 — The Builder's study selector scopes nothing: the tree, the coverage % and the gap list are always whole-repertoire
- **Severity:** medium
- **Type:** wrong-result
- **File:** frontend/repertoire_builder.js (`refreshTree()`, ~line 997) and backend/api.py (`GET /api/repertoire/tree`)
- **Line(s):** frontend: `refreshTree()` request URL; backend: `get_repertoire_tree(color, threshold, min_games, include_patched)` — no study parameter
- **Component:** Repertoire Builder — study dropdown / coverage gauge / gap list
- **Repro script:** `audit/scripts/atk1_b12_study_scope.py`
- **Detected by:** ATTACKER-1 (BUILDER)

## Expected

The Builder renders a `<select id="builderStudySelect">` whose first option is
`"(All Studies)"`, and `onStudyChange()` refreshes the tree when it changes. A study selector
must scope the displayed tree, the coverage percentage and the gap list to the selected study
(or be visibly labelled as a "where do new moves get filed" selector and nothing else). At
minimum, the per-study move counts the dropdown already shows and the global coverage % it
shows next to them must not be presented as if they described the same thing.

## Actual

`refreshTree()` never sends the study, and the endpoint has no study parameter at all:

```
  refreshTree():
    const res = await fetch(`/api/repertoire/tree?color=${this.activeColor}&threshold=${this.gapThreshold}&min_games=${this.openingGamesThreshold}&include_patched=${this.showPatchedGaps}`);
    this.treeData = await res.json();
    this.renderCoverageAndGaps();

  onStudyChange():
    this.currentStudy = studyName ? studyName.trim() : '';
    await this.refreshTree();

  GET /api/repertoire/tree parameters:
    color: Literal["white","black"] = Query("white"), threshold: float = Query(5.0, ge=0.0, le=100.0),
    min_games: int = Query(30, ge=0), include_patched: bool = Query(False)
    -> no study / study_name parameter exists on the endpoint
```

With two disjoint studies built (`Anti French`: `1.e4 e6 2.d4 d5 3.e5 Nf6`;
`Caro-Kann`: `1.e4 c6 2.d4 d5 3.Nc3 dxe4`):

```
GET /api/repertoire/studies ->
   {'id': 1, 'name': 'Anti French', 'total_moves': 6, 'user_moves': 3}
   {'id': 7, 'name': 'Caro-Kann',  'total_moves': 6, 'user_moves': 3}

GET /api/repertoire/tree?color=white (offline) ->
   total_moves : 11   (union of BOTH studies)
   coverage_pct: 100.0
   gaps        : 0 entries, none of them attributed to a study
   per-study move counts shown in the dropdown: [('Anti French', 6), ('Caro-Kann', 6)]
```

So the dropdown advertises 6 moves per study while the gauge next to it is computed over 11
moves spanning both, and no gap row can be attributed to the study the user selected. A user
working through "Caro-Kann" is shown `Anti French` replies as prepared in the candidate table
(the table is driven by `moves_by_parent`, which is whole-repertoire) and cannot tell which
suggested branches belong to the study they are studying.

Meanwhile `this.currentStudy` *is* used for writes — `saveMove()` posts
`study_name: this.currentStudy || undefined` — so selecting a study changes where new moves
are filed while changing nothing about what is displayed. The result is a silent mismatch
between "what I am looking at" and "what I am editing".

## Root cause

The `study_moves` relation and the `study_name` column exist and *are* consulted elsewhere
(`get_studies`, `get_trainer_progress(study_name=...)`,
`evaluate_trainer_move(study_filter=...)`, `build_repertoire_tree_graph(study_filter=...)`),
but `get_repertoire_tree()` was never given a study filter, and the Builder's `refreshTree()`
does not pass one. The dropdown is therefore wired to a variable that only affects writes.
There is no test or assertion anywhere that the study selection changes what the tree shows.

## Exploit / escalation

Adjacent weaker variants:

1. **Per-study counts and the coverage gauge disagree by construction.** `get_studies` reports
   per-study `total_moves`/`user_moves`, `get_repertoire_tree` reports a global
   `coverage_pct`; showing them side by side invites the comparison the code cannot support.
2. **The study filter the *Trainer* does have makes this worse, not better.** A user who
   notices the gap list is unscoped can drill into the Trainer (which is study-scoped) and get
   a different picture of the same repertoire with no explanation of the discrepancy.
3. **Compounding with BUILDER-01/02/03:** because new moves are filed under whatever study is
   selected but displayed globally, a wrong-colour/wrong-study write is invisible in the UI —
   the user has no way to see which study a given prepared move belongs to from the Builder.
