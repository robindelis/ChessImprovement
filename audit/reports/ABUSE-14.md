# ABUSE-14 — Quadratic regex in `ContentScout._extract_lines_from_description` (50 KB input = 42.8 s of CPU); currently unreachable dead code
- **Severity:** low
- **Type:** crash (ReDoS / algorithmic complexity)
- **File:** `backend/scout.py`
- **Line(s):** 494 (the pattern), 489-511 (the caller), 278-291 (its only caller)
- **Component:** Opening Scout
- **Repro script:** `audit/scripts/atk5_dos_repro.py` (section F)
- **Detected by:** ATK-5

## Expected
Any regex applied to a remote-controlled string should be linear (or bounded) in
the input length.

## Actual
```python
    def _extract_lines_from_description(self, desc: str) -> List[Dict]:
        """Extracts PGN lines and timestamped variations from video description."""
        if not desc:
            return []
        lines = []
        matches = re.finditer(r'(?:(\d{1,2}:\d{2})\s+)?(?:[Vv]ariation|[Ll]ine)?\s*([A-Za-z0-9\s-]+)?[:\-]?\s*((?:1\.\s*[e4d4c4Nf3][^\n\r]+))', desc)
```

The three optional groups before the mandatory `(?:1\.\s*[e4d4c4Nf3]…)` group can
all match the same characters, and the character class `[A-Za-z0-9\s-]+`
**includes** whitespace, which is also matched by the following `\s*` and by
`[^\n\r]+`. For input with no `1. e4`-like substring, the engine re-tries every
split point from every start offset: O(n²).

### Observed (`atk5_dos_repro.py` section F, single-threaded, wall clock)
```
F) scout regex cost on a crafted 'description'-like string
  regex finditer len=1000         18.90 ms
  regex finditer len=10000      1833.80 ms      (10x input ->  97x time)
  regex finditer len=50000     42836.60 ms      (50 KB -> 42.8 SECONDS of pure CPU)
```

10 KB already costs 1.8 s. Video descriptions of 50 KB are entirely normal on
YouTube, and the test suite itself would need ~6 minutes to reach 200 KB.

### Reachability — important caveat
`_extract_lines_from_description` is called **only** from
`_extract_chess_moves_and_lines` (`scout.py:291`), and
`_extract_chess_moves_and_lines` currently has **no call site anywhere in the
codebase** (verified with `grep -rn "_extract_chess_moves_and_lines"` across the
repo: only its own definition and my/ATK-4's audit scripts). The current
`POST /api/scout/analyze` → `_analyze_youtube_video` path never calls it, so this
is **latent** rather than live. Severity is therefore `low`, not `high`.

## Root cause
Overlapping optional quantifiers in a `re.finditer` scan, applied to an
attacker-chosen string with no length cap and no timeout. The same
`normalize_chess_speech` helper (11 sequential `re.sub`s, `scout.py:51-81`) is
applied per transcript snippet; measured cost was negligible (<5 ms at 100 KB) but
it is the natural place to add a cap too.

## Exploit / escalation
1. **If the dead code is ever wired up** (and the docstring in
   `_analyze_youtube_video` shows transcripts were intended to feed it), a single
   `POST /api/scout/analyze` for a video with a 50 KB text-only description pins a
   server thread for ~43 s. `scout.py` has **no timeout** on the `yt_dlp` call
   either, so a handful of such videos is a trivial worker-starvation DoS.
2. **Adjacent — the same class in the live path:** `_analyze_raw_pgn`
   (`scout.py:657-708`) loops `while True: chess.pgn.read_game(pgn_io)` on a
   fully user-supplied string with no size cap. Measured: a 20 000-move PGN
   (`POST /api/scout/analyze`, ~100 KB) returns 200 in well under a second, but
   nothing bounds it — `pgn_io` is an in-memory `StringIO`, so a 32 MB PGN
   (ABUSE-09 shows such bodies are accepted) is parsed in-process.
3. **Adjacent — `extract_youtube_id`'s regex** (`scout.py:19-21`) is a simple
   alternation with no nested quantifier and is safe; it was measured and is not
   part of this finding.

**Required fix:** rewrite the description pattern with non-overlapping,
mandatory-delimiter groups (or split on `\n` and anchor with `^`), add
`if len(desc) > 20_000: desc = desc[:20_000]`, and — because the function is
currently dead — either delete it or add an explicit regression test with a size
bound.
