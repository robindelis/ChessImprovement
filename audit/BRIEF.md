# AUDIT BRIEF — Chess Opening Improvement Platform

## Mission
Full audit of the codebase. 5 **attacker** agents hunt functional bugs by exercising the app.
5 **code** agents audit code quality statically. Orchestrator dispatches a dedicated **fixer**
agent per confirmed bug. Fixers' patches are then handed back to the reporting agent for
**creative re-exploitation**.

## Repo
`/home/robin/Projects/ChessImprovement` — Python 3 FastAPI + `python-chess` backend, SQLite
(WAL) DB, Stockfish 19 UCI engine, vanilla-JS frontend, Electron shell.

Read `AGENTS.md` at repo root first — it is the authoritative architecture doc.

## Hard rules (violating these is itself a bug)
1. **NEVER touch production data.** `chess_improvement.db` holds user `RobiDeli`'s real games,
   blunders and repertoire. Do not open/modify it.
2. **Every probe/test script MUST** `import tests.test_setup` as the **first** backend-adjacent
   import. It points `CHESS_DB_PATH` at a throwaway temp DB and registers `atexit` cleanup.
   ```python
   import tests.test_setup  # FIRST
   from backend.api import app
   from fastapi.testclient import TestClient
   client = TestClient(app, raise_server_exceptions=False)
   ```
3. **NEVER** hit `http://127.0.0.1:38281` over HTTP. The live server owns the production DB.
   Always use `TestClient(app)`.
4. **NEVER** run `POST /api/repertoire/seed?force=true`, `POST /api/repertoire/clear`, or any
   Lichess `/api/sync` against production. On the temp DB it's fine.
5. **NEVER** start a real YouTube/yt-dlp crawl or a real multi-minute Stockfish analysis in a
   probe unless it is essential and bounded (mock or monkeypatch it instead).
6. Do not edit files outside your assigned scope unless a fix is required to demonstrate a bug.
7. Use `/home/robin/miniconda3/bin/python3` — no other interpreter has the deps.

## Where to put things
- Probe/exploit scripts: `audit/scripts/<your-id>_*.py` (or `.js` if you must, though Python is
  preferred for logic probes). Make them **re-runnable** and **idempotent**.
- Findings: `audit/reports/<finding-id>.md`
- Fixer patches: `audit/patches/<finding-id>.md`

## Finding ID scheme
`<DOMAIN>-<n>`, domains: `BUILDER`, `TRAINER`, `SYNC`, `SCOUT`, `ABUSE` (attacker agents);
`CODE`, `PERF`, `DEAD`, `DUP`, `SEC` (code agents). Zero-padded: `TRAINER-01`.

## Reporting contract (MANDATORY)
When you find a bug, **write a finding file** to `audit/reports/<id>.md` containing EXACTLY:

```markdown
# <ID> — <one-line title>
- **Severity:** critical | high | medium | low
- **Type:** crash | data-loss | wrong-result | security | race | ui-regression | spec-violation
- **File:** path/to/file.py
- **Line(s):** 123-140
- **Component:** <short component name>
- **Repro script:** `audit/scripts/<id>_repro.py`
- **Detected by:** <your agent id>

## Expected
What a correct implementation does.

## Actual
What actually happens. Paste real observed output/errors.

## Root cause
Precise explanation, with the offending code quoted.

## Exploit / escalation
How you reached it, and any *adjacent* weaker variants of the same root cause
(list up to 3 concrete variants an attacker could also try).
```

Then **continue hunting** — do not stop at the first bug. Prioritise breadth of coverage over
depth on any single finding.

## Re-exploitation contract (when a patch lands)
The orchestrator will hand you back: `[RETEST] <ID>`. Re-read the patched code and:
1. Verify the original repro now behaves correctly.
2. Try **creative harder variants** of the same root cause: different endpoints, different color,
   different repertoire, deeper plies, unicode/oversized input, concurrent requests, replayed
   state, empty DB, extreme numbers, reversed roles, etc.
3. Report **only NEW distinct bugs** as new finding files, or write
   `audit/reports/<ID>-RETEST.md` stating the patch holds and listing what you tried.

## Prior art
`tests/attack_*.py`, `tests/attacker_*.py`, `tests/reattack_probe.py`, `tests/repro_bugs.py`,
`tests/verify_patches.py` are **earlier** audit rounds. Read them for context on what was already
found — but **verify independently**; they may be stale, may themselves be buggy, and may already
be fixed. Do not re-report a genuinely fixed issue.
