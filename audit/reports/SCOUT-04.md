# SCOUT-04 — Move extraction ignores the spoken order, and turns plain English into phantom moves
- **Severity:** high
- **Type:** wrong-result
- **File:** backend/scout.py
- **Line(s):** 51-81 (`normalize_chess_speech`), 331-381 (main matching loop)
- **Component:** Scout video move extraction / speech alignment
- **Repro script:** `audit/scripts/atk4_04_timeline_extraction_repro.py` (section 6c / 6d)
- **Detected by:** ATTACKER-4 (SCOUT)

## Expected
A transcript that says the moves in order must yield that line, and ordinary English must not
be interpreted as a move.

## Actual

**(a) Order is taken from python-chess move generation, not from the text.**

```python
for s in transcript_snippets:
    ...
    matched = True
    while matched:
        matched = False
        for m in list(board.legal_moves):        # <-- generation order, not text order
            san = board.san(m)
            ...
            if found_pattern:                    # any legal SAN anywhere in the snippet
                board.push(m)
                norm_text = re.sub(found_pattern, '', norm_text, count=1, ...)
                matched = True
                break
```

`board.legal_moves` yields piece moves (N/B/R/Q/K) *before* pawn advances, and pawns in
file order. So for a snippet mentioning both `e4` and `Nf3`, `Nf3` wins.

Observed:

```
--- 6c. transcript 'so first e4, and then e5, then Nf3, then Nc6'
    extracted sans        : ['Nf3', 'Nc6', 'e4', 'e5']
    plies                 : [1, 2, 3, 4]
    resulting line PGN    : ['1. Nf3 Nc6 2. e4 e5']
    final FEN             : r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 0 3
    expected (text order) : rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b kq - 1 3
    ORDER CORRECT?        : False
```

The opening `1. Nf3 Nc6 2. e4 e5` is not the line the narrator described, and the reported
`ply`/`move_number`/`fen_before` chain is a completely different game. Every individual move is
*legal* (verified by replay), which is exactly why nothing downstream notices.

**(b) `normalize_chess_speech` rewrites ordinary English into square names.**

```python
t = re.sub(r'\b(be|bee)\s*([1-8])\b', r'b\2', t)
t = re.sub(r'\b(day|the)\s*([1-8])\b', r'd\2', t)
t = re.sub(r'\b(see|sea)\s*([1-8])\b', r'c\2', t)
```

Observed:

```
'I could be 4 moves ahead of him'
    normalized : 'i could b4 moves ahead of him'
    extracted  : ['b4']            <-- 1.b4 played from an English sentence

'he was told to be 3 for 1 in the pawn endgame'
    normalized : 'he was told b3 for 1 in the pawn endgame'
    extracted  : ['b3']            <-- 1.b3 played
```

This is a normal thing for a chess YouTube commentator to say ("we could be 4 moves up",
"it has to be 3 for 1"). The pipeline then seeds the user's session line tree and the
repertoire with phantom moves.

## Root cause
Matching is a *set membership* test (`re.search` over the whole remaining snippet) with the
candidate order supplied by python-chess, and the speech normaliser rewrites homophones
(`be`→`b`, `the`→`d`, `see`→`c`, `day`→`d`) without requiring any chess context.

## Exploit / escalation
1. Any caption containing "be 4"/"be 3"/"the 5"/"see 6" injects a phantom `b4`/`b3`/`d5`/`c6`
   move at whatever ply the extractor happens to be on.
2. Captures are never detected from speech: `normalize_chess_speech` produces `xd4`, but
   `board.san()` emits `exd4`, so the pattern `\bexd4\b` never matches — an asymmetric
   failure that silently drops every capture the narrator describes.
3. Castling is never detected from real speech either: the regex is
   `\bcastles?\s*(?:kingside)?\b`, which does not match the word **"castling"**
   (`normalize_chess_speech("castling kingside") == "castling kingside"`, unchanged) even
   though `tests/test_scout.py::test_normalize_chess_speech` asserts castling support using
   the unnatural word "castles".
4. Move-number digits are matched as moves: `t[0].isdigit()` filtering happens only in
   `openings.py`, not here, so a snippet containing "1. e4" can also match `d4`-style tokens
   from the prose around it.
