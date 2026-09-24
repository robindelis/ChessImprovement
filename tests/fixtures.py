"""
`tests/fixtures.py` — the suite's synthetic, self-contained test dataset.

Audit finding CODE-05-07 (MEDIUM-HIGH): `tests/test_setup.py` used to *clone the
production database* into the temp DB and `tests/test_repertoire.py` hard-coded
`assertGreaterEqual(get_total_games_count(), 280)`. The suite's result therefore
depended on how many rated games user `RobiDeli` happened to have played, and a
contributor without `chess_improvement.db` got a red suite out of the box.

This module replaces that coupling with a **synthetic, deterministic dataset**:

* `SYNTHETIC_GAMES` — 12 invented games (ids `synG01`..`synG12`) built from real
  opening lines so every FEN the backend derives is a legal chess position.
* `SYNTHETIC_BLUNDERS` — opening mistakes attached to those games with explicit
  `cpl_loss`, `eval_before`, `opening_games` and `opening_name` values, including
  the negative cases the code is supposed to filter out (an already-lost
  position, and a position below the 2000+ opening cutoff).
* `SYNTHETIC_EXPLORER_CACHE` — cached Lichess-2000+ explorer payloads for every
  FEN the suite queries, so `get_candidate_moves_2000()` and
  `get_explorer_game_count()` are served from the cache and never touch the
  network. The payload for black-to-move positions deliberately declares
  `eval_convention: "stm"` and side-to-move-perspective scores, so the suite
  genuinely exercises the white-POV conversion instead of trusting a value that
  was already correct.

Everything is derived from literals in this file. Nothing here reads
`chess_improvement.db`. `tests/test_setup.py` builds the schema with
`backend.db.init_db()` (the only legitimate way to obtain the production schema)
and then seeds these rows into the throwaway temp database.
"""
import json
import time

import chess

# --------------------------------------------------------------------------- #
# Constants other tests assert against (never hard-code these numbers again)
# --------------------------------------------------------------------------- #

SYNTHETIC_USERNAME = "SyntheticTester"
SYNTHETIC_GAME_COUNT = 12
SYNTHETIC_WHITE_GAME_COUNT = 6
SYNTHETIC_BLACK_GAME_COUNT = 6

#: The starting position the tests build their repertoires from.
START_FEN = chess.STARTING_FEN

#: The two positions the suite asks the 2000+ explorer about.
FEN_AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
FEN_ITALIAN_Bc5 = "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3"
FEN_WHITE_CENTRE_ADVANTAGE = "rnbqkbnr/pp2pppp/2p5/8/3PP3/8/PPP3PP/RNBQKBNR b KQkq - 0 4"

#: A position with an illegal pawn on the 8th rank — used to prove the
#: evaluator refuses it instead of killing the shared engine process.
FEN_ILLEGAL_PAWN_ON_BACKRANK = "P7/8/8/8/8/8/8/k6K w - - 0 1"


# --------------------------------------------------------------------------- #
# Synthetic games
# --------------------------------------------------------------------------- #

# (game_id, user_color, speed, result, opening_name, eco, [SAN moves])
_SYNTHETIC_GAME_SPECS = [
    ("synG01", "white", "blitz", "1-0", "Italian Game", "C50",
     ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"]),
    ("synG02", "white", "blitz", "0-1", "Italian Game", "C50",
     ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"]),
    ("synG03", "white", "rapid", "1/2-1/2", "Scotch Game", "C44",
     ["e4", "e5", "Nf3", "Nc6", "d4", "exd4", "Nxd4"]),
    ("synG04", "white", "blitz", "1-0", "Ruy Lopez", "C60",
     ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"]),
    ("synG05", "white", "rapid", "0-1", "Vienna Game", "C25",
     ["e4", "e5", "Nc3", "Nf6"]),
    ("synG06", "white", "blitz", "1-0", "Petrov's Defence", "C42",
     ["e4", "e5", "Nf3", "Nf6"]),
    ("synG07", "black", "blitz", "0-1", "Sicilian Defence", "B20",
     ["e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6", "Nc3", "a6"]),
    ("synG08", "black", "blitz", "1-0", "Sicilian Defence", "B20",
     ["e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6"]),
    ("synG09", "black", "rapid", "0-1", "Caro-Kann Defence", "B10",
     ["e4", "c6", "d4", "d5", "f3"]),
    ("synG10", "black", "blitz", "1-0", "French Defence", "C00",
     ["e4", "e6", "d4", "d5", "Nc3", "Bb4"]),
    ("synG11", "black", "rapid", "1/2-1/2", "King's Indian Defence", "E60",
     ["d4", "Nf6", "c4", "g6", "Nc3", "Bg7"]),
    ("synG12", "black", "blitz", "0-1", "English Opening", "A10",
     ["c4", "e5", "Nc3", "Nf6"]),
]

#: Deterministic base timestamp (2023-11-14T22:13:20Z) so `played_at` never
#: depends on when the suite runs.
SYNTHETIC_EPOCH = 1700000000


def synthetic_games():
    """Yield `dict` rows for the `games` table (no `id` collisions with tests)."""
    for index, (gid, color, speed, result, opening, eco, sans) in enumerate(_SYNTHETIC_GAME_SPECS):
        white_player = SYNTHETIC_USERNAME if color == "white" else "SyntheticOpponent"
        black_player = "SyntheticOpponent" if color == "white" else SYNTHETIC_USERNAME
        yield {
            "id": gid,
            "speed": speed,
            "rated": 1,
            "played_at": SYNTHETIC_EPOCH + index * 3600,
            "white_player": white_player,
            "black_player": black_player,
            "white_rating": 2100 + index,
            "black_rating": 2080 + index,
            "user_color": color,
            "result": result,
            "opening_name": opening,
            "eco": eco,
            "moves_san": " ".join(sans),
            "analyzed_at": SYNTHETIC_EPOCH + index * 3600 + 60,
        }


# --------------------------------------------------------------------------- #
# Synthetic blunders
# --------------------------------------------------------------------------- #

# (game_id, san_index_of_the_mistake, played_san, best_san, eval_before,
#  eval_after, cpl_loss, opening_games, opening_name, eco, category)
#
# Deliberate negative cases:
#   * `synB07` has eval_before = -450  -> must be filtered out (already lost).
#   * `synB08` has opening_games = 11  -> must be filtered out by min_games=30.
_SYNTHETIC_BLUNDER_SPECS = [
    # White mistakes, descending cpl_loss so ordering is observable.
    ("synG01", 4, "d3", "d4", 40, -190, 230, 5120, "Italian Game", "C50", "development"),
    ("synG03", 5, "Nxd4", "Be7", 25, -140, 165, 3310, "Scotch Game", "C44", "development"),
    ("synG02", 4, "d3", "Bc4", 10, -95, 105, 980, "Italian Game", "C50", "development"),
    # Black mistakes.
    ("synG07", 5, "cxd4", "Nc6", 30, -120, 150, 1490, "Sicilian Defence", "B20", "development"),
    ("synG10", 5, "Bb4", "Bd7", 15, -70, 85, 620, "French Defence", "C00", "development"),
    # --- must be filtered: the position is already decisively lost ---------
    ("synG12", 4, "d4", "g3", -450, -820, 370, 2200, "English Opening", "A10", "already-lost"),
    # --- must be filtered: below the 2000+ opening cutoff -------------------
    ("synG11", 5, "Bg7", "d5", 20, -60, 80, 11, "King's Indian Defence", "E60", "rare"),
]


def _blunder_rows():
    """Materialise blunder rows, deriving FENs by replaying each synthetic game."""
    moves_by_game = {g["id"]: g["moves_san"].split() for g in synthetic_games()}
    now = int(SYNTHETIC_EPOCH)
    for order, spec in enumerate(_SYNTHETIC_BLUNDER_SPECS, start=1):
        (gid, san_index, played_san, best_san, eval_before, eval_after,
         cpl_loss, opening_games, opening_name, eco, category) = spec
        board = chess.Board()
        for san in moves_by_game[gid][:san_index]:
            board.push_san(san)
        fen_before = board.fen()
        for san, label in ((played_san, "played_san"), (best_san, "best_san")):
            try:
                board.parse_san(san)
            except Exception as exc:
                raise AssertionError(
                    "tests/fixtures.py blunder spec %r is invalid: %s=%r is not "
                    "legal in %s (%s). Fix the fixture, do not weaken the test."
                    % (gid, label, san, fen_before, exc)
                ) from exc
        played = board.parse_san(played_san)
        best = board.parse_san(best_san)
        color = "white" if board.turn == chess.WHITE else "black"
        move_number = board.fullmove_number
        ply = san_index + 1
        board.push(played)
        fen_after = board.fen()
        yield {
            "synthetic_key": "synB%02d" % order,
            "game_id": gid,
            "move_number": move_number,
            "ply": ply,
            "color": color,
            "fen_before": fen_before,
            "fen_after": fen_after,
            "played_move": played.uci(),
            "played_san": played_san,
            "best_move": best.uci(),
            "best_san": best_san,
            "eval_before": eval_before,
            "eval_after": eval_after,
            "cpl_loss": cpl_loss,
            "opening_name": opening_name,
            "eco": eco,
            "category": category,
            "is_book_move": 0,
            "refutation_pv": best_san,
            "explanation": "Synthetic fixture blunder: %s lets %s in." % (played_san, best_san),
            "strategic_idea": "Castle and connect the rooks before committing the centre.",
            "opening_games": opening_games,
            "created_at": now + order,
        }


def synthetic_blunders():
    """Yield `dict` rows for the `blunders` table (no `id` values: autoincrement)."""
    return _blunder_rows()


def blunder_by_key(key):
    """Look up one synthetic blunder by its stable key, e.g. ``"synB01"``."""
    for row in _blunder_rows():
        if row["synthetic_key"] == key:
            return dict(row)
    raise KeyError(key)


#: Blunder keys that `get_game_gaps()` must return for min_games=30.
#: synB02 is Black's 3...Nxd4 in the Scotch Game; colours follow from the
#: replayed move lists above.
VISIBLE_WHITE_BLUNDERS = ("synB01", "synB03")
VISIBLE_BLACK_BLUNDERS = ("synB02", "synB04", "synB05")
#: Blunder keys that must NEVER appear (negative cases).
FILTERED_BLUNDERS = ("synB06", "synB07")


def blunder_ids_by_key():
    """Map stable blunder key -> row, so tests can look blunders up by name."""
    return {row["synthetic_key"]: row for row in _blunder_rows()}


# --------------------------------------------------------------------------- #
# Synthetic 2000+ explorer cache
# --------------------------------------------------------------------------- #

def _explorer_payload(fen, moves, total_games, eval_convention="white"):
    """Build an `explorer_cache.data_json` payload in the shape the app writes.

    Important: `get_candidate_moves_2000()` caches the **already formatted**
    candidate list, not the raw Lichess reply. A cache hit is returned verbatim
    (only `my_stats` is merged in), so a fixture that stores the raw
    `{white, draws, black}` shape produces responses with no `popularity_pct`
    and no `win_rate` — i.e. a fixture that silently tests a different code path
    than production. `tests/test_isolation_guard.py` asserts every seeded move
    carries the full formatted key set.
    """
    formatted = []
    for san, white, draws, black, average_rating, san_eval in moves:
        games = white + draws + black
        formatted.append({
            "san": san,
            "uci": None,           # filled in by _fill_uci (needs a board)
            "games": games,
            "popularity_pct": round((games / total_games * 100), 1) if total_games else 0.0,
            "white_win_pct": round((white / games * 100), 1) if games else 0.0,
            "draw_pct": round((draws / games * 100), 1) if games else 0.0,
            "black_win_pct": round((black / games * 100), 1) if games else 0.0,
            "win_rate": round((white / games * 100), 1) if games else 0.0,
            "average_rating": average_rating,
            "eval": san_eval,
        })
    return {
        "fen": fen,
        "total_games": total_games,
        "white_total": total_games // 2,
        "draws_total": total_games // 4,
        "black_total": total_games - (total_games // 2) - (total_games // 4),
        "moves": formatted,
        "eval_convention": eval_convention,
    }


def _fill_uci(payload, fen):
    board = chess.Board(fen)
    for move in payload["moves"]:
        move["uci"] = board.parse_san(move["san"]).uci()
    return payload


def norm4(fen):
    """The cache-key normalisation: the first 4 FEN tokens.

    `backend.repertoire.get_candidate_moves_2000()` and
    `get_explorer_game_count()` look `explorer_cache` up by
    `normalize_fen(fen)`, i.e. pieces + side to move + castling + en passant.
    Seeding the cache with a full 6-token FEN therefore produces a silent cache
    MISS and a live network call — which is exactly how a "hermetic" fixture
    turns into a flaky one. `tests/test_isolation_guard.py` asserts that
    `norm4()` here agrees with `backend.repertoire.normalize_fen()`.
    """
    return " ".join(str(fen).strip().split()[:4])


def synthetic_explorer_payload_for(fen, max_moves=12, total_games=5000):
    """A deterministic explorer payload for ANY legal position.

    `get_candidate_moves_2000()` falls through to the live Lichess explorer on a
    cache miss. That makes any test which walks a repertoire tree a network
    test: it is slow, rate-limited, and (as observed) flaky — a tree metric such
    as `coverage_pct` silently became 0.0 because the HTTP call came back
    differently than the day before.

    This generator removes the network from the equation while still driving the
    real code path: it enumerates the position's legal moves and gives each a
    deterministic, strictly decreasing weight. Every legal reply is present with
    a non-zero `popularity_pct`, so a metric that asks "is the move I prepared
    among the database's replies?" has a well-defined answer.
    """
    board = chess.Board(fen)
    ordered = sorted(board.legal_moves, key=lambda m: (board.san(m), m.uci()))[:max_moves]
    moves = []
    for index, move in enumerate(ordered):
        wins = max(20, 1200 - index * 90)
        draws = max(5, 200 - index * 15)
        losses = max(10, 400 - index * 25)
        moves.append((board.san(move), wins, draws, losses, 2200 - index * 5, 0))
    return _fill_uci(_explorer_payload(fen, moves, total_games=total_games), fen)


def seed_explorer_for_positions(conn, fens, now=None):
    """Insert deterministic `explorer_cache` rows for `fens`. Idempotent.

    Call this from a test that is about to exercise a code path which would
    otherwise reach the live explorer, so the test is hermetic instead of
    network-dependent.
    """
    import time as _time
    now = int(now or _time.time())
    ratings = "2000,2200,2500"
    written = 0
    for fen in fens:
        try:
            payload = synthetic_explorer_payload_for(fen)
        except Exception:
            continue  # not a legal position: nothing sensible to seed
        conn.execute(
            "INSERT OR REPLACE INTO explorer_cache (fen, ratings, data_json, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (norm4(fen), ratings, json.dumps(payload), now),
        )
        written += 1
    conn.commit()
    return written


def opponent_positions(conn, repertoire):
    """Parent FENs in `repertoire` where the OPPONENT is to move.

    These are exactly the positions `get_repertoire_tree()` asks the 2000+
    explorer about when it computes `coverage_pct` and the gap list, so they are
    the set a hermetic test has to pre-seed.
    """
    rows = conn.execute(
        "SELECT DISTINCT parent_fen FROM repertoire_moves WHERE repertoire = ?",
        (repertoire,),
    ).fetchall()
    fens = []
    for row in rows:
        parent = row["parent_fen"] if not isinstance(row, tuple) else row[0]
        parts = str(parent).split()
        if len(parts) >= 2 and parts[1] == "w":
            fens.append(parent)
    return fens


def synthetic_explorer_rows():
    """Yield `(fen4, ratings, payload_dict)` triples for the `explorer_cache` table.

    `ratings` matches the backend's default (`"2000,2200,2500"`), and `fen4` is
    the 4-token `normalize_fen()` cache key (see `norm4()`). Both must match or
    every lookup silently falls through to the live Lichess explorer.

    Two synthetic blunders can share a position, so rows are de-duplicated by
    `(fen, ratings)` keeping the largest `total_games` (the one the 2000+
    database would report for that position). `tests/test_isolation_guard.py`
    asserts the keys are unique and backend-normalised.
    """
    ratings = "2000,2200,2500"
    merged = {}

    def offer(fen4, payload):
        existing = merged.get(fen4)
        if existing is None or payload["total_games"] > existing["total_games"]:
            merged[fen4] = payload

    # After 1. e4 — the position the suite explores most.
    offer(norm4(FEN_AFTER_E4), _fill_uci(_explorer_payload(
        FEN_AFTER_E4,
        [("e5", 1200, 300, 500, 2200, 40),
         ("c5", 700, 120, 380, 2100, 20),
         ("e6", 500, 90, 210, 2150, 15),
         ("c6", 300, 60, 140, 2050, 5)],
        total_games=3700,
    ), FEN_AFTER_E4))

    # The Italian position: 3. Bc4 Bc5 — Bc5 and Nf6 must both be present.
    offer(norm4(FEN_ITALIAN_Bc5), _fill_uci(_explorer_payload(
        FEN_ITALIAN_Bc5,
        [("Nf6", 1500, 400, 600, 2250, 55),
         ("Bc5", 900, 200, 350, 2150, 30),
         ("Be7", 400, 80, 150, 2100, 10)],
        total_games=4600,
    ), FEN_ITALIAN_Bc5))

    # The payload below is stored in SIDE-TO-MOVE (Black) perspective on purpose:
    # the backend must convert it to the documented white-POV convention, and the
    # suite asserts the sign is flipped for both `color="white"` and
    # `color="black"`. Without this the eval-convention assertions were
    # conditional on a network-supplied value and therefore never ran.
    offer(norm4(FEN_WHITE_CENTRE_ADVANTAGE), _fill_uci(_explorer_payload(
        FEN_WHITE_CENTRE_ADVANTAGE,
        [("Nbd7", 300, 60, 900, 2050, -30),
         ("Nf6", 200, 40, 700, 2080, -20),
         ("Be6", 150, 30, 500, 2100, -45)],
        total_games=2600,
        eval_convention="stm",
    ), FEN_WHITE_CENTRE_ADVANTAGE))

    # One `explorer_cache` row per synthetic blunder position, so
    # `get_explorer_game_count()` is a pure cache hit and never calls the network.
    for row in _blunder_rows():
        fen = row["fen_before"]
        offer(norm4(fen), _fill_uci(_explorer_payload(
            fen,
            [(row["best_san"], row["opening_games"], 10, 10, 2200, 10)],
            total_games=row["opening_games"],
        ), fen))

    for fen4, payload in merged.items():
        yield fen4, ratings, payload


# --------------------------------------------------------------------------- #
# Synthetic drill reviews (spaced repetition)
# --------------------------------------------------------------------------- #

def synthetic_drill_reviews(blunder_ids_by_key):
    """Yield `drill_reviews` rows keyed by the ids `seed_synthetic()` inserted."""
    now = SYNTHETIC_EPOCH
    plan = [
        # (blunder key, attempts, successes, status, interval_days, next_review_date)
        ("synB01", 6, 5, "learning", 3.0, now + 3 * 86400),
        ("synB02", 9, 8, "mastered", 21.0, now + 21 * 86400),
        ("synB03", 1, 0, "new", 1.0, now),
        ("synB04", 4, 2, "learning", 2.5, now + 2 * 86400),
    ]
    for key, attempts, successes, status, interval, next_review in plan:
        blunder_id = blunder_ids_by_key.get(key)
        if blunder_id is None:
            continue
        yield {
            "blunder_id": blunder_id,
            "attempts": attempts,
            "successes": successes,
            "last_attempt_date": now - 86400,
            "next_review_date": next_review,
            "interval_days": interval,
            "ease_factor": 2.5,
            "status": status,
        }


# --------------------------------------------------------------------------- #
# Synthetic scouted content
# --------------------------------------------------------------------------- #

def synthetic_scouted_content():
    """Yield one `scouted_content` row (an ECO-guide study, not user data)."""
    return [{
        "source_id": "syn-guide-c50",
        "source_type": "eco_guide",
        "title": "Synthetic Italian Game Guide",
        "channel": "synthetic",
        "opening_name": "Italian Game",
        "eco": "C50",
        "thumbnail_url": "",
        "duration": 615,
        "data_json": json.dumps({
            "moves": ["e4", "e5", "Nf3", "Nc6", "Bc4"],
            "synthetic": True,
        }),
        "scouted_at": SYNTHETIC_EPOCH,
        "is_favorite": 0,
    }]


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #

#: Tables the fixture owns. `test_setup` truncates exactly these before seeding,
#: so a test that mutates them cannot leak into another test's baseline.
FIXTURE_TABLES = (
    "games",
    "blunders",
    "drill_reviews",
    "explorer_cache",
    "scouted_content",
    "repertoire_moves",
    "repertoire_studies",
    "study_moves",
)


def truncate_fixture_tables(conn):
    """Delete every fixture-owned row. Idempotent."""
    for table in FIXTURE_TABLES:
        conn.execute("DELETE FROM %s" % table)


def seed_synthetic(conn, now=None):
    """Populate `conn` (an open connection to the TEMP db) with the fixture.

    Returns a small summary dict so tests can assert against fixture constants
    rather than hard-coded numbers. Idempotent: it truncates first.
    """
    now = int(now or time.time())
    truncate_fixture_tables(conn)

    conn.executemany(
        """INSERT INTO games (
               id, speed, rated, played_at, white_player, black_player,
               white_rating, black_rating, user_color, result,
               opening_name, eco, moves_san, analyzed_at
           ) VALUES (
               :id, :speed, :rated, :played_at, :white_player, :black_player,
               :white_rating, :black_rating, :user_color, :result,
               :opening_name, :eco, :moves_san, :analyzed_at
           )""",
        list(synthetic_games()),
    )

    blunder_rows = list(synthetic_blunders())
    ids_by_key = {}
    for row in blunder_rows:
        cursor = conn.execute(
            """INSERT INTO blunders (
                   game_id, move_number, ply, color, fen_before, fen_after,
                   played_move, played_san, best_move, best_san,
                   eval_before, eval_after, cpl_loss, opening_name, eco,
                   is_book_move, refutation_pv, explanation, strategic_idea,
                   category, opening_games, created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["game_id"], row["move_number"], row["ply"], row["color"],
             row["fen_before"], row["fen_after"], row["played_move"],
             row["played_san"], row["best_move"], row["best_san"],
             row["eval_before"], row["eval_after"], row["cpl_loss"],
             row["opening_name"], row["eco"], row["is_book_move"],
             row["refutation_pv"], row["explanation"], row["strategic_idea"],
             row["category"], row["opening_games"], row["created_at"]),
        )
        ids_by_key[row["synthetic_key"]] = cursor.lastrowid

    conn.executemany(
        """INSERT INTO drill_reviews (
               blunder_id, attempts, successes, last_attempt_date,
               next_review_date, interval_days, ease_factor, status
           ) VALUES (?,?,?,?,?,?,?,?)""",
        [tuple(row.values()) for row in synthetic_drill_reviews(ids_by_key)],
    )

    conn.executemany(
        """INSERT OR REPLACE INTO explorer_cache (fen, ratings, data_json, updated_at)
           VALUES (?, ?, ?, ?)""",
        [(fen, ratings, json.dumps(payload), now)
         for fen, ratings, payload in synthetic_explorer_rows()],
    )

    conn.executemany(
        """INSERT OR REPLACE INTO scouted_content (
               source_id, source_type, title, channel, opening_name, eco,
               thumbnail_url, duration, data_json, scouted_at, is_favorite
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [tuple(row.values()) for row in synthetic_scouted_content()],
    )

    conn.commit()
    return {
        "games": SYNTHETIC_GAME_COUNT,
        "white_games": SYNTHETIC_WHITE_GAME_COUNT,
        "black_games": SYNTHETIC_BLACK_GAME_COUNT,
        "blunders": len(blunder_rows),
        "blunder_ids": ids_by_key,
    }
