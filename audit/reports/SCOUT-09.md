# SCOUT-09 — PGN import mangles multi-game blobs, names every header-less study "?", and only keeps game 1 in the timeline
- **Severity:** medium
- **Type:** wrong-result
- **File:** backend/scout.py
- **Line(s):** 644-742 (`_analyze_raw_pgn`), esp. 657-661 (game loop + title), 706-708, 718-723
- **Component:** `POST /api/scout/analyze` — raw PGN study builder
- **Repro script:** `audit/scripts/atk4_08_pgn_robustness_repro.py`
- **Detected by:** ATTACKER-4 (SCOUT)

## Expected
`AGENTS.md` §4E advertises *bulk tree import* (`📥 All → White`). A multi-game PGN blob should
become N clean variations, each fully populated, each reachable.

## Actual

**(a) Header-less multi-game blobs are glued into one nonsense game.**

```
headerless 3-game blob '1. e4 e5 *\n1. d4 d5 *\n1. c4 e5 *'
   -> variations: [('?', '1. e4 e5 2. d4 d5 3. c4')]      <-- ONE variation, 3 openings fused
5000 headerless games  55000B -> vars=1 tl=2                <-- 4999 games silently dropped
```

python-chess's `read_game` needs a header/blank-line boundary to start a new game; without one
it keeps consuming movetext, and the resulting "line" is a position sequence that jumps
between unrelated openings.

**(b) `full_timeline` only ever contains the first game.**

```
--- multi-game blob: variations vs full_timeline
    variations: [(1, 'G1', '1. e4 e5'), (2, 'G2', '1. d4 d5'), (3, 'G3', '1. c4 e5')]
    full_timeline (only game 1): ['e4', 'e5']
```

```python
if game_num == 1:
    timeline.extend(moves)
```

Consequently `data['duration'] = len(timeline) * 4` and every chapter's
`end_time = len(v['moves']) * 4` are computed from game 1 only.

**(c) All chapters of a multi-game study start at 00:00.**

```
chapters: [('G1', 0, 8), ('G2', 0, 8), ('G3', 0, 8)]
```

`'chapters': [{'title': v['name'], 'start_time': 0, 'end_time': len(v['moves']) * 4, ...}]`
— the Scout chapter dropdown offers three identical `00:00` entries and
`seekVideoTo(0)` is a no-op.

**(d) Every PGN without an `[Event]` header becomes the study named `?`.**

```python
g_title = game.headers.get("Event") or game.headers.get("White", "") + " vs " + game.headers.get("Black", "") or f"Chapter {game_num}"
```

`+` binds tighter than `or`, and python-chess pre-populates `White`/`Black` with `'?'`, so
`f"Chapter {game_num}"` is unreachable. Observed:

```
no headers   -> title='?'            variation names=['?']
White only   -> title='?'            variation names=['?']
Event empty  -> title='? vs ?'       variation names=['? vs ?']
```

`title` becomes `activeStudy.title`, which the frontend uses as the **default study name** when
saving (`scout.js:1624` `defaultStudy = (this.activeStudy?.title || …)`), and
`opening_name` is used by `trainThisOpening()` — so the user ends up with a repertoire study
literally called `?` and the Trainer's opening filter set to `?`.

**(e) Robustness checks that came back CLEAN** (recorded so they are not re-chased): no
infinite loop, no unhandled exception and no 5xx for any of — unterminated `{` comment,
unterminated header quote, stray `}}}`, embedded NUL bytes, garbage tokens, an 8-queen FEN,
a 200 000-char token, `1. e4` followed by 20 000 `e5`s, 5000 games. Everything either yields a
legal variation or is dropped; illegal SAN is caught by python-chess (logged to stdout).

## Root cause
The multi-game loop assumes python-chess will yield a fresh `Game` per text block; there is no
separator insertion, no per-game sanity check, and the title fallback chain is written with
the wrong precedence.

## Exploit / escalation
1. A PGN exported from Lichess/chess.com **without** `[Event]` headers (very common for
   "repertoire" exports) yields a single fused line — the user imports `1. e4 e5 2. d4 d5 3. c4`
   into their White repertoire.
2. `/api/scout/analyze` on a Lichess study (`_analyze_lichess_study`) always goes through this
   same path, so multi-chapter studies inherit all of (a)–(d).
3. The `[Event "…"]` header is stored verbatim in `scouted_content.title` /
   `.opening_name` and is returned as `variations[0].name`, so it is a second, PGN-only route
   into the innerHTML sinks of SCOUT-01.
