# SCOUT-07 — Naive substring matching labels a Sicilian Najdorf video "A45 Trompowsky" and resolves ECO guide ids to the wrong opening
- **Severity:** high
- **Type:** wrong-result
- **File:** backend/openings.py, backend/scout.py
- **Line(s):** openings.py:54-79 (`search_openings`, `find_opening_by_name`), 226-238 (`get_strategic_ideas`); scout.py:143-164, 224-239, 587-599
- **Component:** ECO encyclopedia lookup / opening identification
- **Repro script:** `audit/scripts/atk4_07_openings_matching_repro.py`
- **Detected by:** ATTACKER-4 (SCOUT)

## Expected
Opening identification should pick the *most specific / most relevant* match, and a partially
typed ECO-guide id should never resolve to a different opening family.

## Actual

**(a) The video's ECO/opening is hijacked by any shared word in any chapter title.**

```python
def search_openings(self, query, limit=15):
    words = q.split()
    for item in self.openings_list:
        if all(w in item["name"].lower() or w in item["eco"].lower() for w in words):
            results.append(item)
```

`_analyze_youtube_video` tries the title first, and if that yields nothing walks the chapters
and takes the **first chapter that matches anything**:

```python
elif chapters:
    for ch in chapters:
        ch_matches = self.opening_book.search_openings(ch.get('title', ''), limit=1)
        if ch_matches:
            eco = ch_matches[0]['eco']; opening_name = ch_matches[0]['name']; break
```

Observed for a video titled *"Sicilian Defense Najdorf Guide"* with chapters
`["Najdorf introduction", "Poisoned pawn"]`:

```
-> eco      : A45
-> name     : Trompowsky Attack: Poisoned Pawn Variation
-> advice   : In the Trompowsky Attack: Poisoned Pawn Variation, key priorities are ...
-> setting  : {"name": "Trompowsky Attack: Poisoned Pawn Variation", "eco": "A45"}
```

The title fails to match only because of the word "Guide"; the chapter "Poisoned pawn" then
matches the Trompowsky line. The bogus `opening_name` is also written to the
`recent_learned_opening` setting, which is what `/api/repertoire/train/openings` returns and
what `repertoireTrainer.setOpeningFocus()` filters on — so the Trainer then serves drills
for the wrong opening.

**(b) `find_opening_by_name` is a substring test with no ranking** — used by
`_analyze_eco_guide` to resolve hand-typed `eco_*` ids:

```
'Sicilian'  -> ('A00', 'Hungarian Opening: Sicilian Invitation')
'French'    -> ('A08', "King's Indian Attack: French Variation")
'e4'        -> ('E43', 'Nimzo-Indian Defense: St. Petersburg Variation, with Ne4')
```

Through the public API:

```
POST /api/scout/analyze {"source": "eco_B20_Sicilian"}
   -> A00 | Hungarian Opening: Sicilian Invitation   | pgn=1. g3 c5
POST /api/scout/analyze {"source": "eco_B10_French"}
   -> A08 | King's Indian Attack: French Variation   | pgn=1. Nf3 d5 2. g3 c5 3. Bg2 Nc6
POST /api/scout/analyze {"source": "eco_"}
   -> A00 | Amar Opening                             | pgn=1. Nh3
```

The user clicks *📥 Line → White* on that study and `1. g3 c5` lands in their Sicilian
repertoire.

**(c) `search_openings` lets a word match the ECO *code* instead of the name**, and there is
no relevance ordering, so the search shelf shows the wrong "master repertoire line" first:

```
GET /api/scout/search?query=Italian+Game  (eco_guide rows, in order)
  C47 - Four Knights Game: Italian Variation (Master Repertoire Line)
  C47 - Four Knights Game: Italian Variation, Noa Gambit (...)
  C50 - Four Knights Game: Italian Variation (...)
  C50 - Italian Game (...)
```

The single-letter query `c` returns `Amsterdam Attack`, `Barnes Opening: Hammerschlag`, …

**(d) Robustness checks that came back clean** (recorded so they are not re-chased):
`get_opening_for_fen` returns `None` for `""`, `"garbage"`, `"8/8/8/8/8/8/8/8 w - -"`,
a 5000-char string, a truncated FEN and `"None"` — no crash. `identify_game_opening` stops
cleanly at the first illegal move, skips non-string entries, and handles a 600-element list.
Full-width / zero-width / homoglyph opening names simply do not match (no false positive).

## Root cause
Both lookups reduce relevance to "does every whitespace-separated word appear as a substring
of the name or the ECO code", with file order as the tie-break and no notion of
prefix-vs-full-name, word-boundary, or family specificity. `find_opening_by_name` tries
exact match first (good) but then falls back to `q in item["name"].lower()` and returns the
first hit.

## Exploit / escalation
1. A YouTube channel that names its videos after any word shared with an opening family
   ("Poised", "The Gruenfeld", "Bird", "Lasker", "Polish") can steer the app's ECO badge, the
   strategic-advice panel and the Trainer's opening filter.
2. `get_strategic_ideas` matches themes by substring too, so `"frenchy stuff"` yields the
   French Defense advice — cosmetic but it shows the theme matching is theme-blind.
3. Any `eco_<anything>` string is accepted and cached (`ON CONFLICT(source_id) DO UPDATE`),
   so the `scouted_content` table can be filled with arbitrary ids / bogus ECO codes
   (`eco_X00_Zzzz_Nothing` → eco `X00`, PGN `1. e4 e5`) — see SCOUT-11.
