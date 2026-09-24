# SYNC-09 — the dashboard groups blunders by opening name but reports one arbitrary ECO per group
- **Severity:** low
- **Type:** wrong-result
- **File:** backend/api.py
- **Line(s):** 691-694
- **Component:** `/api/dashboard`
- **Repro script:** `audit/scripts/atk3_api_bounds_repro.py` (section ATK3-11e), `audit/scripts/atk3_reverify_matrix.py` (section `SYNC-10`)
- **Detected by:** ATTACKER-3 (SYNC)

## Expected
Either the ECO is part of the grouping key, or it is aggregated (`MIN(eco)`, `GROUP_CONCAT`),
so the reported value is deterministic and meaningful.

## Actual
```sql
SELECT opening_name, eco, color, COUNT(*) as blunder_count, AVG(cpl_loss) as avg_loss
FROM blunders
GROUP BY opening_name, color
ORDER BY blunder_count DESC
LIMIT 10
```
`eco` is neither grouped nor aggregated. SQLite returns the value from an arbitrary row of
the group. Observed with two rows that share `opening_name='Sicilian Defense'` but carry
`eco='B20'` and `eco='B90'`:

```
  repertoire_blunders: [
   {"opening_name": "Sicilian Defense", "eco": "B20", "color": "white",
    "blunder_count": 2, "avg_loss": 700.0},
   ...
  ]
  [LIVE ] SYNC-10: one ECO reported for a group whose rows carry ['B20','B90']
```

The same query shape is used for the "which openings am I worst at" panel, so the ECO label
shown next to a count can be flat wrong (it will change if SQLite picks a different row
after a VACUUM, an index rebuild or a different SQLite version).

## Root cause
Classic bare-column-in-a-`GROUP BY` query. It is legal in SQLite (and MySQL with
`ONLY_FULL_GROUP_BY` disabled) and silently non-deterministic.

## Exploit / escalation
1. **Adjacent bare columns:** the same query already aggregates `AVG(cpl_loss)` and
   `COUNT(*)`; adding `opening_name` to the `GROUP BY` is a one-word fix and makes the ECO
   exact.
2. **Related truncation:** `LIMIT 10` with no `ORDER BY` tie-break means two openings with
   the same `blunder_count` are shown in arbitrary order between requests — a visible
   flicker in the dashboard.
3. **`recent_blunders` uses `ORDER BY b.created_at DESC LIMIT 5`** where `created_at` is
   `int(time.time())`, i.e. second resolution, so all blunders created in the same sync
   burst tie and the "recent" list is effectively random.
