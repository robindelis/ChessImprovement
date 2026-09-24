# AUDIT STATE — final ledger

## Baseline vs now
| | Start | Now |
|---|---|---|
| Test count | 104 | **575** |
| Suite verdict | `OK` | 575 tests, 5 red — **all owned by the in-flight FIXER-T** |
| Files in a fixer | 0 | 3 (`repertoire.py`, `analyzer.py`+`db.py`, `api.py`) |
| Findings | — | **~200** (6 critical, ~50 high) across 15 agents |

## 🔴 INCIDENT-01 — RESOLVED (was: boot migration erased 243 → 0 repertoire moves)
- `migrate_studies_and_cleanup_blunders()` ran a 3-token-FEN reachability walk on **every** boot and
  `DELETE`d every row it called an "orphan" — which is every legitimate side-line fragment and imported
  sub-line. Measured: **157 of 243 rows (65%)** destroyed per launch; the three studies `AGENTS.md` §9.3
  forbids were fabricated to replace them.
- **Fixed by FIXER-P0**: `init_db(purge_orphans=False)`, `CHESS_PURGE_ORPHANS=1` opt-in with a loud
  DESTRUCTIVE banner, `find_unreachable_repertoire_moves()` kept as a **read-only** diagnostic, forbidden
  study injection removed entirely, `BEGIN IMMEDIATE`/`COMMIT` + `ROLLBACK`, and a `SAVEPOINT` around the
  migration. Verified: pre-fix `FAILED (failures=9)` → post-fix `Ran 9 … OK`; 243 rows / 196 user moves
  survive **three boots** byte-for-byte.
- `recovered/repertoire_moves_from_git_HEAD.json` holds the 243-row pre-incident export and is still
  **awaiting a safe re-import** (the destructive path was live on disk until this fix landed).

## Landed fixers
| Fixer | File(s) | Highlights |
|---|---|---|
| P0 | `db.py` | INCIDENT-01; opt-in purge; rollback; no forbidden studies |
| A | `repertoire.py` | write-path UPSERT preserving notes/arrows/SRS; `force` seed no longer deletes a prepared line; deterministic tie-breaker; black tree uses black stats; explorer fetched once/position; `get_game_gaps` writes nothing |
| B | `api.py` | draws no longer render as losses; `played_at` epoch-ms; `analyzed_at` on success only; 405s; `/api/sync` truthful; seed route verifies its own post-condition |
| C | `evaluator.py` | `score_cp` **and** `eval` on every frame; typed `error` frame with a stable `code` and a sanitised message; bounded engine pools |
| E | `electron/` | nav guards, deny-by-default window-open, CSP response header, preload 7→2 keys, 0 orphan processes across 4 shutdown paths |
| F | builder FE | **restored** the dead blunder-review feature end to end (`#blunderBannerAlert`); 8 findings incl. the shared abort controller and the candidate-table staleness guard |
| G | scout/trainer FE | 2 stored XSS closed (escaped by construction); repertoire derived from **line identity** not board orientation; import confirms before destroying a prepared move; YouTube↔board sync implemented |
| H | `style.css`, `index.html` | §8.1–§8.5 as executable rules; found `text-overflow:ellipsis` was **inert** without `white-space:nowrap` |
| I | `scout.py`, `openings.py`, `lichess_client.py` | dead YouTube pipeline actually wired up; spoken-order extraction; ECO matching (Najdorf was labelled *A45 Trompowsky*); 262.9 s DoS → 0.0000 s; 1600-row timeline spam → 4 |
| J | builder FE, `board.js` | 53-row interpolation audit; remote Lichess username was an XSS sink; `arrows[].color` reached SVG **and the DB**; TRAINER-14 proven unreachable-to-permanently-unanswerable |
| M | scout/trainer/app FE | `scout.js` interpolation audit (no hole — G's retrofit was complete); DUP-01 forks made code-identical; import `already_present`; **BUILDER-14 eval-bar regression fixed** |
| N | `db.py` | `best_pv` created **and** migrated (fresh ≡ legacy); `ux_blunders_natural_key`; **10 indexes — the first in the codebase**; `LIKE` rewrite made once-only |
| K | `analyzer.py` | a real mate-in-2 was reported as "Checkmate in 1" and a plain -95.00 as mate — both fixed via the mate field, not a magnitude threshold; blunder upsert |
| D | `tests/`, `AGENTS.md` | 4 fake tests deleted/rewritten against the **real** frontend with a SHA-256 provenance guard; suite no longer clones production; fail-closed `probe_guard`; §5 schema regenerated from the live DB and now **machine-checked** |
| U | `api.py` | the 422 handler was itself a 500 (5 bytes 500'd 10/50 route×content-type combos → 0); write-on-read removed; single-flight keyed on the canonical 4-token FEN; `Retry-After` now derived so it cannot drift |

## Re-attack round — what it caught
Resumed three original attackers on their own fixed bugs. Two of three found real holes:
- **BUILDER-14 (critical, a regression created by FIXER-C)** — the two-line SSE frame vs a one-line
  `JSON.parse(trimmed.slice(6))` left the Builder's eval bar at `+0.0` for every real frame, **and
  `tests/test_evaluator_stream.py` asserted the broken format** while its own parser helper was line-wise,
  so the suite certified the regression. Found by ATK-4, patched by FIXER-M.
- **ABUSE-16 (medium)** — FIXER-L's own new validation handler 500'd on 5 bytes across 20 POST routes, and
  its nesting guard was gated on a `Content-Type` nothing enforces. *Why the suite was green:* every
  hostile-body test declared `application/json`; the one form-urlencoded case was stopped at the size gate.
  "The gap sits between two individually-correct tests."
- **SYNC-19 (high, most serious open)** — re-analysis was idempotent but not *correct*: a blunder the engine
  no longer detects was never deleted, so the gap list, the trainer and `mastered_count` all kept it.

## Verified-correct (do not "fix")
`AGENTS.md` §4A threat formula incl. both strict constraints · `eval_before >= -200` filter · `min_games`
29/30/31 boundary · §4D patched-blunder auto-promotion · **no SQL injection** (two independent attackers) ·
**no path traversal** (27 URL + 9 raw-ASGI payloads) · **no SSRF** · FIXER-L's local-origin boundary
(attacked with trailing dots, `userinfo@`, IPv6, `Origin: null`, duplicate `Origin`, non-numeric ports,
`Sec-Fetch-Site` lies — no bypass) · `tests/test_timeline_spam.py` now runs the **real** frontend.

## Process failures I own
1. **A fixer's `git reset` re-tracked 420 MB** and crashed opencode every ~2 min. My brief never banned it.
2. **FIXER-P0 overwrote FIXER-N's concurrent `db.py`** using `cp` in a shared tree; detected and recovered
   from a `.pyc`, and the on-disk file is the correct merge.
3. **Four code agents clobbered each other's reports** — I specified ID schemes but no shared namespace.
4. **Two fixers "resolved" a doc-drift finding by deleting the spec** (AGENTS.md §8/§9 were stripped
   mid-run). Self-recovered; now `tests/test_docs_contract.py` fails if the doc is wrong.
5. My own verification script reused a cursor inside its own iteration loop and briefly reported the
   production DB as gutted. It was not — my script was wrong.

## Outstanding for the user
- **Rotate the Lichess token** `lip_…` — committed at `81f4ed6`; untracked now, still in history.
- **Re-import the 243 recovered moves** when ready (safe now that INCIDENT-01 is fixed).

## In flight
- **FIXER-S** — `analyzer.py` + `db.py`: blunder identity under the new UNIQUE key (SYNC-18), re-analysis
  convergence/staleness (SYNC-19), `PovScore` trap (SYNC-23), `eval_*_mate` columns.
- **FIXER-T** — `repertoire.py`: SYNC-16/17/20/21/22, ABUSE-17, and the trainer selection/scheduling
  cluster (TRAINER-01/02/04/06/07/08/09/15). **Owns all 5 currently-red tests.**
