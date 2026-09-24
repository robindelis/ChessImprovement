# SCOUT-99 — Unproven suspicions & attack angles that came back clean (ATTACKER-4)

Everything below was actually executed against the temp DB with `yt_dlp` faked. Items marked
**CLEAN** are recorded so the next fixer/re-attacker does not spend time on them; items marked
**UNPROVEN** are real code smells I could not turn into a user-reachable failure.

## UNPROVEN

### U1 — `check_line_overlap`'s 3-token FEN index is en-passant-blind
`moves_by_parent_3` is keyed on `" ".join(parent_fen.split()[:3])`, i.e. placement + side to
move + castling rights, dropping the ep square. python-chess only emits an ep square when an
en-passant capture is actually available, so the *same physical position* can legitimately be
stored with `ep = d6` and with `ep = -`:
`1.e4 a6 2.e5 d5` → `… w KQkq d6` vs a transposition ending `…d6-d5` → `… w KQkq -`.
A crafted `fen_before` therefore gets a false `already_present` from the fallback bucket.

**Why it is unproven:** to reach the false match the *whole* prefix of the queried line must
match exactly, and if every preceding ply is identical then the ep square at the divergence
point is also identical — so a false positive cannot arise from a line the UI would ever
produce. Only a hand-crafted payload (or a stored orphan graft, see SCOUT-11 §5) can trigger
it, and such a payload can already write anything directly. Impact: badge text only.
Probe: `audit/scripts/atk4_14_cache_and_index_repro.py` §3.

### U2 — Orphan prune / `delete_repertoire_branch` collateral deletion via transpositions
`DELETE … WHERE parent_fen = ? OR parent_fen = <3-token> OR parent_fen LIKE '<3-token> %'`
looks like it would delete transposed siblings. I could **not** build a failing case: every
prune step is preceded by
`SELECT COUNT(*) … WHERE fen = ? OR fen = ? OR fen LIKE ?` and a non-zero count
(`# Transposition: this position is still reached by another branch`) aborts the cascade, so
any genuinely transposed branch protects itself. The heuristic is over-protective (it can leave
orphans behind) rather than destructive. Both `add_repertoire_move` and
`delete_repertoire_branch` share `_prune_orphaned_children`, so a fix must consider both.

### U3 — ReDoS / catastrophic backtracking in `_extract_lines_from_description`
`r'(?:(\d{1,2}:\d{2})\s+)?(?:[Vv]ariation|[Ll]ine)?\s*([A-Za-z0-9\s-]+)?[:\-]?\s*((?:1\.\s*[e4d4c4Nf3][^\n\r]+))'`
has an optional group followed by an optional greedy `[A-Za-z0-9\s-]+` and a required literal —
the classic shape for backtracking. I did not construct a pathological input (and the function
is dead code, see SCOUT-06), so this stays a suspicion.

### U4 — `pgn_<md5[:10]>` cache-key collisions
`_analyze_raw_pgn` derives `source_id = "pgn_" + md5(pgn_text)[:10]` — 40 bits. Two different
PGNs would have to collide to overwrite each other's `scouted_content` row (birthday bound
≈ 2²⁰ distinct PGNs). Not practically exploitable; noted because the cache has no other
integrity check.

### U5 — Lichess study fetch is an unauthenticated outbound request
`analyze_source("https://lichess.org/study/XXXXXXXX")` makes the server issue
`requests.get("https://lichess.org/study/XXXXXXXX.pgn", timeout=10)`. The id is
`[a-zA-Z0-9]{8}` taken by an **unanchored** regex, so `https://lichess.org/study/../../x` style
inputs resolve to whatever 8 alphanumerics appear first — but `pgn_url` is always rebuilt
against `lichess.org`, so this is *not* an SSRF. The real issue is abuse/amplification (any
POST body can force an outbound fetch), already covered by SCOUT-08/SCOUT-10.

## CLEAN (verified, no bug)

- **SQL injection** — `'; DROP TABLE scouted_content; --`, `' OR '1'='1`, a `UNION SELECT`
  string and `../../../../etc/passwd` all posted as `source_id` to `/api/scout/favorite`:
  every query is parameterised, each returns `{"is_favorite": false}` and the table is intact.
  `get_scouted_content`, `list_recent_scouted`, `toggle_favorite_scouted` all bind parameters.
  (probe: `atk4_14_cache_and_index_repro.py` §1)
- **Prototype pollution** — `{"__proto__": {...}}`, `{"constructor": {"prototype": {...}}}` and
  `arrows: '{"__proto__":{"evil":1}}'` in the import payload are all accepted harmlessly.
  Python dicts cannot pollute, and the frontend only does `JSON.parse` → `board.setArrows()`,
  which uses `createElementNS` + `setAttribute` (no HTML parsing). `board._renderArrows` is
  therefore **not** an XSS sink even with a hostile `arr.color`.
- **Unicode/homoglyph move tokens** — `ｅ４` (full-width), `e\u200b4` (zero-width space),
  `e−4` (U+2212) and `һ4` (Cyrillic) all extract **0** moves; Python's `\b`/`IGNORECASE` do not
  fold them. The timeline-spam gap (SCOUT-05) is exploitable with plain ASCII only.
- **`openings.get_opening_for_fen`** on `""`, `"garbage"`, `"8/8/8/8/8/8/8/8 w - -"`, a 5000-char
  string, a truncated FEN and the literal `"None"` all return `None` without raising. Only
  `None` (a real `None`, not the string) raises `AttributeError`, and no caller can produce it.
- **`openings.identify_game_opening`** stops cleanly at the first illegal SAN, skips non-string
  entries (`[None, 42, "e4"]` → ply 1) and handles a 600-element list.
- **Malformed PGN robustness** — unterminated `{` comment, unterminated header quote, stray
  `}}}`, embedded NUL bytes, garbage tokens, an 8-queen FEN, a 200 000-char token,
  `1. e4` + 20 000 × `e5`, and 5000 games: **no infinite loop, no unhandled exception, no 5xx**.
  Every case is either a legal variation or cleanly dropped (10-second alarm per case).
- **Session tree mechanics** (node harness on the real classes) — 10 × `stepBack()` at ply 0 is
  a safe no-op; stepping back and replaying a different move forks correctly (2 leaves, one
  child under root, two replies under `1.e4`); `_buildPgn` renders castling correctly
  (`1. e4 Nc6 2. Nf3 d5 3. O-O O-O-O`) and strips stray move numbers (`"12...e4"` → `1. e4`).
- **Import legality guards** — an 8-queen FEN is rejected (`Illegal move`), an opponent reply
  after `1.e4` is stored with `is_user_move = 0`, a black line imported into the black
  repertoire flags Black's plies as user moves, and a bare string inside `moves` produces a
  clean pydantic 422 rather than a 500.
- **§8.5 zero-CLS** — `#scoutActiveLineOverlap` is `display:flex; height:28px` and the ply-0
  `Repertoire: Starting Position` placeholder exists in both `index.html` and
  `checkActiveLineOverlap`. §8.1 is respected: all notices go through `window.showToast`.
- **Frontend perf of the tree itself** — 64 leaves → `getAllLines()` ≈ 0 ms, a single 400-ply
  line ≈ 1 ms. The real cost is the *network* payload (10 KB for 64 leaves, re-POSTed to
  `/api/scout/check_tree` on every board move) — folded into SCOUT-10 rather than claimed as a
  JS hot spot.

## Cross-domain note (blocker, not mine)

`tests/test_setup.py` + `tests/fixtures.py` were rewritten by a concurrent code agent during
this audit (CODE-05-07/10). As of 18:19–18:27 the fixture seeder raises on several synthetic
blunder/explorer specs, e.g.

```
RuntimeError: test isolation ABORTED: seeding the synthetic fixture failed:
  chess.IllegalMoveError: illegal san: 'O-O' in r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3
RuntimeError: test isolation ABORTED: seeding the synthetic fixture failed:
  AssertionError("tests/fixtures.py blunder spec 'synG06' is invalid: played_san='a6' is not legal …")
```

Because `tests/__init__.py` calls `setup_test_db()` at import time, this hard-fails **every**
test and probe in the repo (`python3 -m unittest tests.test_scout` cannot even start).
`audit/scripts/atk4_mocks.py` keeps the mandated `import tests.test_setup` as its first
backend-adjacent import and, only if that raises, falls back to its own isolated temp DB so the
SCOUT repros remain runnable. The production `chess_improvement.db` was never opened.
