# SCOUT-05 — Timeline builder has no dedup, no timestamp validation and silently drops chapters
- **Severity:** medium
- **Type:** wrong-result
- **File:** backend/scout.py
- **Line(s):** 278-438 (`_extract_chess_moves_and_lines`), 297-329 (chapter walk), 384-426 (reset-to-start branch)
- **Component:** Scout video timeline / variation construction
- **Repro script:** `audit/scripts/atk4_04_timeline_extraction_repro.py` (sections 1-5)
- **Detected by:** ATTACKER-4 (SCOUT)

## Expected
`AGENTS.md` §4E describes a timeline of video-timestamped moves. Prior audit rounds fixed
"timeline spam" in the **builder** (`tests/test_timeline_spam.py` only exercises
`repertoire_builder.js` + `board.js`); the Scout timeline builder has *no* dedup, *no*
timestamp validation and *no* cap.

## Actual

**(a) Spam is unbounded — one repeated sentence produces 1600 timeline rows / 400 variations.**

```
--- 400 snippets x 'e4 e5 Nf3 Nc6'
    timeline entries      : 1600
    illegal/invalid moves : 0
    duplicate timestamps  : 1200   (moves sharing a video timestamp)
    duplicate (fen,uci)   : 1596
    max ply               : 4
    variations produced   : 400
    elapsed               : 0.65s
```

Only 4 distinct `(fen_before, uci)` pairs exist; 1596 rows are exact duplicates. Nothing in the
function compares a candidate move against what has already been extracted, and `variations`
grows by one entry per reset. With a real ~30-minute auto-caption transcript (thousands of
snippets) this is thousands of duplicated rows, all serialised into `data_json` and stored in
`scouted_content`.

**(b) Timestamps are trusted verbatim — negative, non-monotonic and past the video duration.**

```
--- negative / out-of-order / past-duration timestamps (video duration=120)
    min/max timestamp     : -50.0 / 900.0
    non-monotonic ts steps: 1
    variations             : [('Main Line', 900.0, '15:00')]
```

`duration` is a parameter of the function and is never used for validation. A `15:00` seek on a
2-minute video is what the frontend chapter dropdown would issue (`seekVideoTo`,
scout.js:660-664). `format_seconds` clamps negatives to `"00:00"` but the raw `timestamp` /
`start_time` fields still carry `-50.0`.

**(c) Two moves from one snippet get the same timestamp** (`{"start": 33.0, "text": "e4 e5"}`
→ `timestamps: [33.0, 33.0]`), so "jump to the move" navigation cannot distinguish them.

**(d) Chapters are matched with an equality guard, so duplicate/unordered chapters are dropped.**

```python
if t_start >= ch['start_time'] and ch['start_time'] != last_chapter_time:
```

```
--- duplicate / backwards / negative chapters
    chapters -> [('A', '00:55')]     # 4 chapters supplied, 1 variation produced
```

Chapters 2-4 (`start_time` 50.0 duplicate, 5.0 backwards, -100.0 negative) never advance
`current_chapter_idx`, so all their moves are attributed to chapter "A" and the chapter
titles in the UI dropdown do not correspond to the variations.

**(e) The reset-to-start branch silently discards short variations.** When the speaker
"restarts", the pending variation is only flushed `if len(current_variation_moves) >= 3`
(line 392), so 1-2 move variations exist only in `full_timeline` and in no `variations`
entry — the UI shows a timeline it cannot import.

## Root cause
No state machine, no `seen` set, no `0 <= timestamp <= duration` validation, no chapter
sanitisation, and a `< 3` magic threshold on flush.

## Exploit / escalation
1. Bypass attempts that were tried and **did not work** (recorded so the next fixer does not
   chase them): zero-width separators (`e\u200b4`), full-width characters (`ｅ４`),
   U+2212 MINUS SIGN (`e−4`) and Cyrillic homoglyphs (`һ4`) all produce **0** moves — Python's
   `\b`/`IGNORECASE` do not fold them, so the `re.sub` pre-normaliser and the `\be4\b` pattern
   both reject them. The dedup gap is exploitable with *plain ASCII* only.
2. `text_snippet` stores the entire raw caption text on every extracted move, so the spam case
   also multiplies storage by the snippet length.
3. Because there is no cap, one pathological caption file is a cheap memory/CPU amplifier for
   `data_json` (it is stored, then re-served verbatim by `/api/scout/analyze` from cache).

## Reachability caveat
`_extract_chess_moves_and_lines` currently has no call site (see SCOUT-06), so this is latent
today. It becomes live the moment the transcript pipeline is wired back up, which is what
`AGENTS.md` §4E documents as existing behaviour.
