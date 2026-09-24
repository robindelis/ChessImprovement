# ABUSE-02 — The production SQLite database (307 games, repertoire) is committed in git history
- **Severity:** high
- **Type:** security
- **File:** `chess_improvement.db` (repo root)
- **Line(s):** whole file (1 687 552 bytes, `SQLite format 3`)
- **Component:** repo hygiene / privacy
- **Repro script:** `audit/scripts/atk5_secrets_repro.py`
- **Detected by:** ATK-5

## Expected
`chess_improvement.db` is the user's private study data: Lichess game history,
opponent usernames and ratings, every detected opening blunder with SAN/FEN, and
the hand-built repertoire with study names and personal notes
(`AGENTS.md` §5, §9 "CRITICAL PRODUCTION DATA INVARIANT"). It must be
gitignored and must never appear in a repository.

## Actual
At commit `81f4ed6` the live database was committed. Observed:

```
$ git ls-files | grep -E '^\.env$|\.db$'
.env
chess_improvement.db          # (pre-fix; both since staged for deletion)

$ git show HEAD:chess_improvement.db | wc -c
1687552
```

`atk5_secrets_repro.py` confirms the blob is a real SQLite file and extracts its
size without ever opening the live file:

```
  Committed chess_improvement.db contains b'lip_' at byte: -1
  Committed DB size: 1687552 bytes;  is SQLite file: True
```

The blob is present in the current (unmodified) history, so `git show
81f4ed6:chess_improvement.db` still hands a full copy of the user's private
chess archive to anyone with repository access, even after the file is removed
from the index. The content was measured indirectly through the API on the
isolated temp DB (which the harness copies from production): 307 games, 153
blunders, 10 000+ blunder rows referenced by id — e.g. opponent `renen12345`
(1855), opening `Philidor Defense: Steinitz Variation`.

## Root cause
There was no `*.db` entry in `.gitignore` (the pre-fix `.gitignore` contained
only `node_modules/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`,
`audit/reports/*.tmp`), and the DB is created in the repository root by
`backend/db.py:7`:

```python
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chess_improvement.db")
```

A concurrent fixer has since added `*.db`, `*.db-wal`, `*.db-shm` to
`.gitignore` and staged the file for deletion — that stops future commits but
leaves the 1.6 MB personal archive readable in history.

## Exploit / escalation
1. **Privacy:** `git show 81f4ed6:chess_improvement.db` from any clone yields the
   user's full game list, opponents' usernames and ratings, and the personal
   repertoire/study names. Lichess game IDs in `games.id` are public URLs
   (`https://lichess.org/<id>`), so the archive is directly re-playable and
   correlated with the account.
2. **Adjacent — write amplification:** because the DB is a tracked file, any
   developer who runs the app (`start.py` calls `init_db()`, which
   `CREATE TABLE`s, `ALTER TABLE`s and runs
   `migrate_studies_and_cleanup_blunders`, a routine that **`DELETE`s orphan
   `repertoire_moves` rows**) dirties the tracked file, so `git status`/`git add
   -A` sweeps production data into the next commit. `.gitignore` now prevents
   this, but the blob is still in history.
3. **Adjacent — combined with ABUSE-01:** the same clone yields both the
   Lichess bearer token *and* the private archive, i.e. full account access plus
   a complete dossier of the account holder's chess activity.

**Required fix:** `git filter-repo --path chess_improvement.db --invert-paths`
(or re-init) and confirm the blob is unreachable; keep `*.db*` ignored.
