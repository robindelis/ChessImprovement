# SCOUT-13 — The destructive-replacement warning is computed by the server and then thrown away; plus §8.4 layout-shift regressions
- **Severity:** medium
- **Type:** ui-regression
- **File:** frontend/scout.js, frontend/index.html
- **Line(s):** scout.js:1395-1472 (`checkActiveLineOverlap`, `banner` is always `null`), 1458-1464, 1669-1670, 1246-1304, 549, 1258-1279; index.html:775 (`btnScoutRemoveFromRep`), 729 (`scoutActiveLineOverlap`)
- **Component:** Opening Scout — overlap strip / tree cards
- **Repro script:** static (grep evidence quoted below) + `audit/scripts/atk4_11_import_contract_repro.py` for the warning that is discarded
- **Detected by:** ATTACKER-4 (SCOUT)

## Expected
`AGENTS.md` §4E: a divergence "warns that the new move replaces the prepared move". §8.4:
conditionally-available actions inside toolbars must not toggle `display` if that shifts
neighbours. §8.2: clamped dynamic text must carry a `title`.

## Actual

**(1) The replacement warning is never displayed.** `checkActiveLineOverlap` fetches
`/api/scout/check_line` for both colours and gets back a message that spells out the
destructive replacement:

```
/api/scout/check_line says:
   divergence = {'ply': 5, 'type': 'user_move_alternative', 'existing_san': 'e5', 'new_san': 'Nc3'}
   message    = First 4 plies already in White repertoire. At move 5, Nc3 replaces your prepared e5 for White!
```

but the only elements it updates are `strip` (badges built from `data.status` /
`matched_plies` / `new_plies` — **not** the message) and `banner`:

```js
const banner = document.getElementById('scoutActiveLineOverlapBanner');
...
if (banner) { banner.style.display = 'none'; banner.innerHTML = ''; }
```

`#scoutActiveLineOverlapBanner` **does not exist** in `frontend/index.html` (grep returns only
`#scoutActiveLineOverlap`, `repActiveLineOverlap`, `scoutQuickAddNotice`), so `banner` is
always `null` and the warning is unreachable. The only place the message surfaces at all is a
`title=` tooltip on the *tree* badges — i.e. only after the user has already scrolled to the
Explored Lines panel, and only for lines, not for the line on the board.

This is the UI half of SCOUT-02: the server detects the destruction, the client drops it, and
`importToRepertoire` then shows a green "✓ Added to White Repertoire".

**(2) §8.4 violations — `display` toggling inside fixed button rows.**

| Location | Code | Effect |
|---|---|---|
| scout.js:1463 | `btnRemoveRep.style.display = inRep ? 'inline-flex' : 'none'` | `#btnScoutRemoveFromRep` is `display:none` in index.html:775 and pops in/out of the quick-rep button row ~120 ms after **every** board move |
| scout.js:1670 / 1750 | `btnTrain.style.display = 'inline-flex'` | "Train this opening" appears in the banner only after a successful import |
| scout.js:549 | `ecoEl.style.display = study.eco ? 'inline-block' : 'none'` | ECO badge in the studio top bar |
| scout.js:1246-1304 | `${showDelWhite ? '<button …>🗑️ White</button>' : ''}` | the `🗑️ White/Black` buttons are **inserted/removed** from `.scout-var-actions` (which is `flex-wrap: wrap`, style.css:2652) when the async overlap data arrives → the row re-wraps |

**(3) §8.2 violations — clamped text without a `title`.** The line-name span
(scout.js:1258-1260) and the study-name span (1277-1279) both set
`white-space:nowrap; overflow:hidden; text-overflow:ellipsis` but never set `el.title`, so a
long opening/study name is silently truncated with no way to read it in full. `renderResults`
does set `title="${item.title}"`, so the pattern exists elsewhere in the same file.

**Checks that came back clean (§8.5 satisfied):** `#scoutActiveLineOverlap` is
`display:flex` with `height:28px` (style.css:2714-2720) and the ply-0 placeholder
`Repertoire: Starting Position` is present both in index.html:729 and in
`checkActiveLineOverlap`; `.scout-var-pgn` is `nowrap/ellipsis`; and
`_showQuickAddNotice` correctly routes to `window.showToast` (§8.1). The dead
`#scoutQuickAddNotice` div (`display:none !important`) and the `#scoutImportAlert` lookup
(scout.js:1595, no such element) are leftovers, not live in-flow notices.

## Root cause
The overlap UI was written against DOM ids that were never added to `index.html`, and the
button visibility rules predate the §8 zero-CLS policy.

## Exploit / escalation
1. A user who plays a move that replaces a prepared one gets a green success toast and a
   neutral "★ White New" badge — the only signal that anything was overwritten is a tooltip on
   a different panel.
2. Every board move triggers a layout shift of the quick-rep button row and (once the overlap
   response lands) of each tree card's action row — the exact jitter §8 was written to
   eliminate.
