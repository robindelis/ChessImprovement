# CODE-4 — finding index (frontend static audit)

**Agent:** CODE-4 · **Scope:** `frontend/scout.js`, `frontend/repertoire_trainer.js`,
`frontend/app.js`, `frontend/audio.js` (cross-checked read-only against
`frontend/index.html`, `frontend/style.css`, `frontend/board.js` and
`backend/repertoire.py` / `backend/scout.py` / `backend/api.py`).

> **Namespace note for the orchestrator:** this agent used the bare
> `CODE-NN.md` form mandated by BRIEF.md §"Finding ID scheme". Other code
> agents used `CODE-<agent>-<n>.md` (`CODE-02-NN.md`, `CODE3-NN.md`,
> `CODE-05-NN.md`) and `CODE-1.md`. There is **no file-level collision** — every
> file below carries `- **Detected by:** CODE-4` in its header — but `CODE-05.md`
> … `CODE-11.md` here are *frontend* findings and are unrelated to the
> `CODE-05-NN.md` / `CODE-11-NN.md` files from other agents.

## Findings

| ID | Sev | Type | File:Line | Title |
|---|---|---|---|---|
| [SEC-01](SEC-01.md) | critical | security | `scout.js:463-483` | Stored XSS via unescaped YouTube title/channel in the results grid |
| [SEC-02](SEC-02.md) | critical | security | `scout.js:1253-1310`, `1542-1562` | XSS via line name (PGN `Event`/YouTube title) and study name in the line cards |
| [CODE-01](CODE-01.md) | critical | data-loss | `scout.js:1100,1145,1460,1841` | Board-flip orientation used as the repertoire key → wrong-tree writes |
| [CODE-02](CODE-02.md) | high | wrong-result | `scout.js:112-126,1907-1912` | `deleteLine()` orphans `currentNode`; the next played move is lost |
| [CODE-03](CODE-03.md) | high | wrong-result | `scout.js:147-164` | `_buildPgn()` emits invalid PGN (`"e5 2. d4"`) for black-first lines |
| [CODE-04](CODE-04.md) | high | spec-violation | `scout.js:1651-1692,1731-1771` | Import reports success on total failure; replacement warning never shown |
| [CODE-05](CODE-05.md) | high | race | `scout.js:395-414`, `repertoire_trainer.js:212-513` | No generation counter: stale search/progress responses overwrite newer ones |
| [CODE-06](CODE-06.md) | high | race | `scout.js:1165-1592` | `treeOverlapMap` keyed by positional index → badges on the wrong lines |
| [CODE-07](CODE-07.md) | high | race | `repertoire_trainer.js:701-804,863-872` | Uncleared 550 ms/800 ms timers mutate the *next* drill's board |
| [CODE-08](CODE-08.md) | high | wrong-result | `app.js:124-157` | `saveSettings()` claims success without checking `res.ok` |
| [CODE-09](CODE-09.md) | high | wrong-result | `scout.js:1837-1905`, `api.py:885-892` | `DELETE /api/repertoire/move` always returns `ok` → false "Removed" |
| [CODE-10](CODE-10.md) | high | spec-violation | `scout.js:525-664` | YouTube↔board sync does not exist; `#scoutCurrentTimeDisplay`/`#scoutLiveIndicator` dead |
| [CODE-11](CODE-11.md) | high | wrong-result | `scout.js:630-664` | No YT-API retry → chapter seeking dead for the whole session |
| [CODE-12](CODE-12.md) | high | ui-regression | `repertoire_trainer.js:480-489,752-761` | §8.1/§8.2: `#trainNoteBox` display-toggled in flow |
| [CODE-13](CODE-13.md) | high | spec-violation | `repertoire_trainer.js:360-410,473-477` | §8.4: `#trainFocusBanner` / `#trainImportantBadge` display-toggled |
| [CODE-14](CODE-14.md) | medium | ui-regression | `scout.js:1007-1058,1457-1464` | §8.2/§8.4: quick-rep toolbar re-flows (no `min-width`) |
| [CODE-15](CODE-15.md) | medium | ui-regression | `scout.js` (7 sites) | Seven `getElementById` ids absent from `index.html` |
| [CODE-16](CODE-16.md) | medium | wrong-result | `scout.js:238-245,416-461` | **All three** non-`all` filter buttons are no-ops; `loadRecent()` dead |
| [CODE-17](CODE-17.md) | medium | wrong-result | `scout.js:46-67,755-793` | FEN-format split, dead "transposition" branch, `nextStep()` follows `children[0]` |
| [CODE-18](CODE-18.md) | medium | crash | `app.js:190-431` | `promptStudySelection` has no re-entrancy guard → silent import abort |
| [CODE-19](CODE-19.md) | medium | spec-violation | `scout.js:1201-1244,1542-1591` | Three divergent badge renderers; text mutates under the user |
| [CODE-20](CODE-20.md) | low | crash | `audio.js` (all) | No master bus, no try/catch, ignored `resume()`; success chime on deletes |
| [CODE-21](CODE-21.md) | medium | race | `scout.js:944-1522` | 4 un-cancellable fetches per board move; overlap results applied to a changed tree |
| [CODE-22](CODE-22.md) | low | ui-regression | `scout.js:321-343` | Keydown guard omits `&lt;select&gt;`; swallows Ctrl/Cmd+Z |
| [DUP-01](DUP-01.md) | medium | ui-regression | `scout.js:4-165` / `repertoire_builder.js:3-171` | `SessionLineTree` forked with behavioural drift; ~690 duplicated lines |
| [DEAD-01](DEAD-01.md) | medium | ui-regression | `scout.js`, `app.js`, `repertoire_trainer.js` | Dead methods/locals, duplicated `window.showToast`, `feedback-box.mistake` has no CSS |

## Repro / scan scripts

All are read-only, idempotent, and never touch the production DB or port 38281.
Python scripts `import tests.test_setup` as the first backend-adjacent import and
use `TestClient` only.

| Script | Proves |
|---|---|
| `audit/scripts/CODE4_orientation_repro.py` | CODE-01 — `move_meta` inserts a White node into the **Black** repertoire |
| `audit/scripts/CODE4_import_status_repro.py` | CODE-04 — `status:'success'` with `imported_count:0`; prepared `Nf3` **destroyed** by a divergent import |
| `audit/scripts/CODE4_delete_status_repro.py` | CODE-09 — `DELETE` returns `ok` while deleting nothing |
| `audit/scripts/CODE4_tree_repro.js` | CODE-02, CODE-03 — orphaned `currentNode`; `"e5 2. d4"` |
| `audit/scripts/CODE4_race_repro.js` | CODE-05, CODE-06, CODE-18 — stale `search()` render; positional badge drift; `InvalidStateError` |
| `audit/scripts/CODE4_trainer_timer_repro.js` | CODE-07 — stale 550 ms/800 ms timers corrupt the next drill |
| `audit/scripts/CODE4_yt_fallback_repro.js` | CODE-11 — chapter seek dead after the fallback path |
| `audit/scripts/CODE4_fen_mismatch_repro.js` | CODE-17 — duplicate unique-moves; `nextStep()` wrong branch |
| `audit/scripts/CODE4_filter_repro.js` | CODE-16 — 3/4 filters unmatched |
| `audit/scripts/CODE4_keyboard_repro.js` | CODE-22 — `&lt;select&gt;` not guarded; `Cmd+Z` swallowed |
| `audit/scripts/CODE4_badge_drift_repro.js` | CODE-19 — 3 badge renderings, 2 divergent |
| `audit/scripts/CODE4_prompt_reentrancy_repro.js` | CODE-18 — concurrent `showModal()` rejects |
| `audit/scripts/CODE4_dead_selector_scan.sh` | CODE-15, DEAD-01, CODE-10 — id/selector drift both directions |
| `audit/scripts/CODE4_cls_scan.sh` | CODE-12/13/14 — §8.1–§8.5 evidence (JS + CSS) |
| `audit/scripts/CODE4_dup_scan.sh` | DUP-01 — forked classes, ~690 duplicated lines, forked endpoints |
| `audit/scripts/CODE4_audio_scan.sh` | CODE-20 — direct-to-destination voices, no limiter, no try/catch |

## Unproven suspicions

See [CODE-4-UNPROVEN.md](CODE-4-UNPROVEN.md).
