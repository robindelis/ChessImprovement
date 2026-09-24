# TRAINER-04 — The study filter is a substring/LIKE match in `train/next` but an exact match in `train/progress`: drilling "Caro-Kann" serves "Caro-Kann Advance" moves, mislabelled and never counted
- **Severity:** high
- **Type:** wrong-result
- **File:** backend/repertoire.py
- **Line(s):** 2087-2096 (`build_repertoire_tree_graph` filter), 1971-1977 (`get_trainer_progress` filter), 2528-2533 + 2624-2662 (`evaluate_trainer_move` unscoped fallbacks)
- **Component:** Repertoire trainer — study scoping
- **Repro script:** `audit/scripts/atk2_04_studyfilter_replay.py`
- **Detected by:** ATTACKER-2 (TRAINER)

## Expected
A study filter selects exactly one study. `train/next`, `train/progress` and `train/attempt`
must agree on what "study = X" means, and a drill must never contain a move that is not in
the selected study.

## Actual
Two different matching rules, plus LIKE metacharacters are never escaped.

**(a) `train/next` matches by substring.** `build_repertoire_tree_graph`:

```python
AND (rs.name = ? OR rs.name LIKE ? OR rm.study_name = ? OR rm.study_name LIKE ?
     OR rm.opening_name = ? OR rm.opening_name LIKE ?)
… (repertoire, clean_filter, f"%{clean_filter}%", …)
```

Observed with two studies `Caro-Kann` (3.Nf3) and `Caro-Kann Advance` (3.Nd2):

```
opening=Caro-Kann returned: {('Nd2', 'Caro-Kann')}  <-- 3.Nd2 only exists in 'Advance'
progress?study=Caro-Kann -> filter_study='Caro-Kann' total_moves=2
```

`3.Nd2` is drilled while the header says `Caro-Kann`, and `train/progress` (which filters by
exact set membership, `filter_clean in move_studies_map.get(key)`) never counts it — so
`Caro-Kann` reports 100 % mastered while the session keeps drilling a foreign move.

**(b) LIKE wildcards are injectable.** `%` / `_` in the filter are passed straight into
`LIKE ?`, so the filter is a no-op or a wildcard match:

```
opening='%'    -> HTTP ok, drill san=Nd2 study='%'
opening='_'    -> HTTP ok, drill san=Nd2 study='_'
opening='Caro%' -> HTTP ok, drill san=Nd2 study='Caro%'
attempt with study_name='%' -> study_name='%' reply=c6
```

The drill payload then literally reports `study_name: "%"` (the frontend then writes that into
`#trainOpeningName` and the study `<select>`).

**(c) `train/attempt` ignores the filter for the graded move and falls back to any study for
the continuation.** The prepared-move query has no study predicate at all, and both the
opponent-reply and follow-up lookups degrade to an unscoped query when the filtered one comes
back empty (`repertoire.py:2624-2629` and `2656-2662`).

Observed with `Alpha` = {1.e4} and `Beta` = 1.e4 e5 2.Nf3 Nc6 3.Bb5:

```
drill: id=2 san=e4 study=Alpha opening=Alpha
attempt(study=Alpha) -> study_name=Alpha
   opponent_reply   = e5   <-- belongs to Beta, not Alpha
   next_expected    = g1f3 (Nf3)  is_line_end=False
   next_study_name  = 'Alpha'   next_opening_name = 'Beta'  <-- mutually inconsistent labels
```

A user who selects the single-move study `Alpha` is silently drilled through the whole of
`Beta`, and the two label fields in the same response disagree.

## Root cause
`LIKE '%…%'` (unanchored, unescaped) in the tree builder vs. exact `in` membership in the
progress builder, plus "best-effort" study scoping in `evaluate_trainer_move` whose fallback
branches deliberately drop the filter.

## Exploit / escalation
1. Two studies whose names are prefixes of one another are extremely common
   (`Caro-Kann` / `Caro-Kann Advance`, `Queen's Gambit` / `Queen's Gambit Declined`,
   `Anti French` / `Anti French Classical`) — no crafted input needed.
2. `train/progress?study=` accepts `%`, so the "All Studies" figure can be silently replaced
   by any single study's figure and vice-versa.
3. A study literally named e.g. `50% Endgame` or `Bird's 1-4-6` can never be selected
   exactly, because its own `%`/`_` are treated as wildcards.
