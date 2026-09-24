"""
API contract regression tests (audit FIXER-B).

Covers the findings FIXER-B patched in `backend/api.py`:

  * CODE-02-02 — a wrong-method call under `/api/` must be 405, not 404
  * CODE-02-04 — `games.analyzed_at` is stamped only after a successful analysis
  * CODE-02-05 — Lichess `winner: null` is a draw end to end (writer + reader)
  * CODE-02-06 — `games.played_at` has one unit (ms) and `ORDER BY` is correct
  * CODE-02-11 — `POST /api/sync` is truthful about success/failure
  * CODE-02-12 — `/api/repertoire/*` aliases delegate to the canonical
                 `/api/scout/*` handlers (one implementation, identical output)
  * CODE-02-13 — internal `ValueError`s never leak their text to the client,
                 while genuine user-input errors stay informative
  * CODE-05-04 — `POST /api/repertoire/seed` never reports a `null` body as
                 success and requires an explicit opt-in

Every check is exposed as a module-level function in `CHECKS` so that
`audit/scripts/FIXER-B_before_after.py` can run the *same* checks against the
pre-fix snapshot module (`audit/scripts/api_prefix_snapshot.py`) and produce
fail-before / pass-after evidence.
"""
import contextlib
import sqlite3
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import tests.test_setup  # MUST be first: points CHESS_DB_PATH at a temp DB

from backend.db import get_db, get_setting
import chess

# Marker used to isolate the fixture rows from the copied production games.
GAME_PREFIX = "zzfixb_"
STUDY_MARKER = "ZZFIXBOPENINGZZ"
ECO_MARKER = "Z99"
NOW_MS = int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def make_client(module):
    from fastapi.testclient import TestClient
    return TestClient(module.app, raise_server_exceptions=False)


def purge_fixtures():
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM blunders WHERE game_id LIKE ?", (GAME_PREFIX + "%",))
        cursor.execute("DELETE FROM games WHERE id LIKE ?", (GAME_PREFIX + "%",))
        conn.commit()
    finally:
        conn.close()


def insert_game(game_id, **fields):
    row = {
        "id": game_id,
        "speed": "blitz",
        "rated": 1,
        "played_at": NOW_MS,
        "white_player": get_setting("lichess_username") or "RobiDeli",
        "black_player": "UnitOpp",
        "white_rating": 1800,
        "black_rating": 1800,
        "user_color": "white",
        "result": "draw",
        "opening_name": STUDY_MARKER,
        "eco": ECO_MARKER,
        "moves_san": "e4 e5 Nf3 Nc6",
        "analyzed_at": 0,
    }
    row.update(fields)
    conn = get_db()
    try:
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        conn.execute(
            f"INSERT OR REPLACE INTO games ({cols}) VALUES ({marks})",
            tuple(row.values()),
        )
        conn.commit()
    finally:
        conn.close()


def read_game(game_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM games WHERE id = ?", (game_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def count_repertoire_moves():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM repertoire_moves")
        return cur.fetchone()["cnt"]
    finally:
        conn.close()


def delete_repertoire_move(repertoire, move_uci):
    """Remove a fixture move. `parent_fen` is deliberately not part of the key
    because `add_repertoire_move` stores the 3-token normalised FEN, not the
    6-token `chess.STARTING_FEN`."""
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM repertoire_moves WHERE repertoire = ? AND move_uci = ?",
            (repertoire, move_uci),
        )
        conn.commit()
    finally:
        conn.close()


class FakeAnalyzer:
    """Stockfish stand-in: records lifecycle, fails for the ids in `fail_ids`."""

    fail_ids = ()
    blunder_ids = ()
    instances = []

    def __init__(self, *a, **kw):
        self.closed = False
        FakeAnalyzer.instances.append(self)

    def analyze_game_openings(self, g):
        if g.get("id") in FakeAnalyzer.fail_ids:
            raise RuntimeError("Stockfish died: totally_internal_secret_detail")
        if g.get("id") in FakeAnalyzer.blunder_ids:
            return [{"game_id": g.get("id"), "san": "???"}]
        return []

    def save_blunders_to_db(self, blunders):
        return None

    def close(self):
        self.closed = True


def reset_fake_analyzer(fail_ids=(), blunder_ids=()):
    FakeAnalyzer.fail_ids = tuple(fail_ids)
    FakeAnalyzer.blunder_ids = tuple(blunder_ids)
    FakeAnalyzer.instances = []


#: Deterministic account used by the sync checks. The route resolves the
#: username from the `lichess_username` setting or, failing that, from
#: `get_account_profile()`; both are patched so no check ever touches the network
#: or depends on whatever the shared temp DB happens to contain.
UNIT_USER = "RobiDeli"


@contextlib.contextmanager
def patched_sync(module, games):
    """Patch the whole `/api/sync` upstream so it is hermetic."""
    with patch.object(module, "GameAnalyzer", FakeAnalyzer):
        with patch.object(module.LichessClient, "fetch_user_games", return_value=games):
            with patch.object(module.LichessClient, "get_account_profile",
                              return_value={"username": UNIT_USER}):
                yield


def lichess_game(game_id, **over):
    g = {
        "id": game_id,
        "speed": "blitz",
        "rated": True,
        "createdAt": NOW_MS,
        "players": {
            "white": {"user": {"name": UNIT_USER}, "rating": 1900},
            "black": {"user": {"name": "UnitOpp"}, "rating": 1900},
        },
        "opening": {"name": STUDY_MARKER, "eco": ECO_MARKER},
        "moves": "e4 e5 Nf3 Nc6 Bc4 Bc5",
    }
    g.update(over)
    return g


def recent_by_marker(client, limit=100):
    res = client.get("/api/games/recent", params={"limit": limit, "search": ECO_MARKER})
    assert res.status_code == 200, f"GET /api/games/recent -> {res.status_code} {res.text[:200]}"
    return res.json()["games"]


# --------------------------------------------------------------------------- #
# CODE-02-02 : wrong-method status codes under /api/
# --------------------------------------------------------------------------- #
def check_wrong_method_is_405(module):
    client = make_client(module)
    wrong_verb_cases = [
        ("GET", "/api/sync"),
        ("PUT", "/api/sync"),
        ("GET", "/api/repertoire/seed"),
        ("GET", "/api/repertoire/clear"),
        ("DELETE", "/api/repertoire/studies"),
        ("DELETE", "/api/settings"),
        ("POST", "/api/blunders"),
        ("PUT", "/api/games/recent"),
        ("POST", "/api/repertoire/tree"),
        ("GET", "/api/repertoire/train/attempt"),
        ("DELETE", "/api/scout/favorite"),
    ]
    for method, path in wrong_verb_cases:
        res = client.request(method, path)
        assert res.status_code == 405, (
            f"{method} {path} -> {res.status_code} (expected 405). "
            f"Body: {res.text[:120]}"
        )

    # A genuinely unknown path must still be 404, not 405.
    for path in ("/api/does_not_exist", "/api/nope/nope"):
        res = client.get(path)
        assert res.status_code == 404, f"GET {path} -> {res.status_code} (expected 404)"


def check_static_mount_still_serves_frontend(module):
    client = make_client(module)
    res = client.get("/")
    assert res.status_code == 200, f"GET / -> {res.status_code}; the StaticFiles mount regressed"
    assert "text/html" in res.headers.get("content-type", ""), res.headers.get("content-type")
    res = client.get("/index.html")
    assert res.status_code == 200, f"GET /index.html -> {res.status_code}"


# --------------------------------------------------------------------------- #
# CODE-02-05 : draws render as draws
# --------------------------------------------------------------------------- #
def check_draw_rendering(module):
    purge_fixtures()
    user = get_setting("lichess_username") or "RobiDeli"
    # result=NULL is exactly what a real sync wrote for a drawn game
    # (Lichess exports "winner": null).
    insert_game(GAME_PREFIX + "draw_null", result=None, user_color="white")
    insert_game(GAME_PREFIX + "draw_lit", result="draw", user_color="white")
    insert_game(GAME_PREFIX + "win_white", result="white", user_color="white")
    insert_game(GAME_PREFIX + "loss_white", result="black", user_color="white")
    # user was Black: white winning == a loss for the user, score 1-0
    insert_game(GAME_PREFIX + "loss_black", result="white", user_color="black")
    insert_game(GAME_PREFIX + "draw_black", result=None, user_color="black")
    insert_game(GAME_PREFIX + "win_black", result="black", user_color="black")
    try:
        client = make_client(module)
        games = {g["id"]: g for g in recent_by_marker(client)}
        expected = {
            GAME_PREFIX + "draw_null": ("draw", "½-½"),
            GAME_PREFIX + "draw_lit": ("draw", "½-½"),
            GAME_PREFIX + "win_white": ("win", "1-0"),
            GAME_PREFIX + "loss_white": ("loss", "0-1"),
            GAME_PREFIX + "loss_black": ("loss", "1-0"),
            GAME_PREFIX + "draw_black": ("draw", "½-½"),
            GAME_PREFIX + "win_black": ("win", "0-1"),
        }
        for gid, (exp_result, exp_score) in expected.items():
            assert gid in games, f"{gid} missing from /api/games/recent"
            got = games[gid]
            assert got["result"] == exp_result, (
                f"{gid}: result={got['result']!r} expected {exp_result!r} "
                f"(score_text={got['score_text']!r})"
            )
            assert got["score_text"] == exp_score, (
                f"{gid}: score_text={got['score_text']!r} expected {exp_score!r}"
            )
        # The frontend contract is a closed set of three values.
        for g in games.values():
            assert g["result"] in ("win", "loss", "draw"), f"unexpected result {g['result']!r}"
    finally:
        purge_fixtures()


def check_dashboard_groups_draws(module):
    purge_fixtures()
    insert_game(GAME_PREFIX + "dash_draw", result=None, user_color="white")
    try:
        client = make_client(module)
        res = client.get("/api/dashboard")
        assert res.status_code == 200, res.status_code
        rows = res.json()["game_results"]
        keys = {(r["result"], r["user_color"]) for r in rows}
        assert None not in {r["result"] for r in rows}, (
            f"/api/dashboard still groups a NULL result into its own bucket: {sorted(map(str, keys))}"
        )
        assert all(r["result"] in ("white", "black", "draw") for r in rows), sorted(map(str, keys))
        assert ("draw", "white") in keys, f"the drawn fixture is missing from the draw bucket: {sorted(map(str, keys))}"
    finally:
        purge_fixtures()


def check_sync_writer_stores_draw_token(module):
    """A Lichess `winner: null` game must be stored as the token 'draw', not NULL."""
    purge_fixtures()
    reset_fake_analyzer()
    gid = GAME_PREFIX + "sync_draw"
    games = [lichess_game(gid, winner=None)]
    with patched_sync(module, games):
        client = make_client(module)
        res = client.post("/api/sync", params={"max_games": 5})
        assert res.status_code == 200, f"POST /api/sync -> {res.status_code} {res.text[:200]}"
    row = read_game(gid)
    assert row is not None, "the drawn game was not stored"
    assert row["result"] == "draw", f"stored result={row['result']!r}, expected the token 'draw' (not NULL)"
    try:
        client = make_client(module)
        games_out = {g["id"]: g for g in recent_by_marker(client)}
        assert gid in games_out, f"{gid} not returned by /api/games/recent"
        assert games_out[gid]["result"] == "draw", games_out[gid]
        assert games_out[gid]["score_text"] == "½-½", games_out[gid]
    finally:
        purge_fixtures()


# --------------------------------------------------------------------------- #
# CODE-02-06 : played_at unit + sort order
# --------------------------------------------------------------------------- #
def check_played_at_sort_order(module):
    purge_fixtures()
    # `older_ms` is one second EARLIER in wall-clock terms than `newer_s`, but
    # its raw column value is 1000x larger, so a raw `ORDER BY played_at DESC`
    # puts it first.
    newer_s = 1700000001          # seconds-valued
    older_ms = 1700000000 * 1000  # milliseconds-valued
    insert_game(GAME_PREFIX + "older_ms", played_at=older_ms)
    insert_game(GAME_PREFIX + "newer_s", played_at=newer_s)
    insert_game(GAME_PREFIX + "oldest_ms", played_at=older_ms - 60_000)
    try:
        client = make_client(module)
        games = recent_by_marker(client)
        ids = [g["id"] for g in games]
        for gid in (GAME_PREFIX + "older_ms", GAME_PREFIX + "newer_s", GAME_PREFIX + "oldest_ms"):
            assert gid in ids, f"{gid} missing from /api/games/recent (got {ids})"
        order = [i for i in ids if i.startswith(GAME_PREFIX)]
        assert order == [
            GAME_PREFIX + "newer_s",
            GAME_PREFIX + "older_ms",
            GAME_PREFIX + "oldest_ms",
        ], f"recent games are not sorted newest-first by wall clock: {order}"
        # The API must expose one unit, and it must be milliseconds.
        for g in games:
            assert g["played_at"] >= 1_000_000_000_000, (
                f"{g['id']}: played_at={g['played_at']} is not epoch milliseconds"
            )
        # FIXER-U / ABUSE-18: the read route used to run
        # `migrate_played_at_units()` itself, i.e. a table-wide UPDATE on a GET.
        # It no longer does, so the first two blocks above are now a STRONGER
        # claim than before: the sort order and the millisecond contract hold
        # with a seconds-valued row still sitting in the column, with no write at
        # all. The stored column is brought to the canonical unit by the caller
        # that actually owns a migration -- `startup_event()`.
        module.startup_event()
        for gid, want in (
            (GAME_PREFIX + "older_ms", older_ms),
            (GAME_PREFIX + "newer_s", newer_s * 1000),
            (GAME_PREFIX + "oldest_ms", older_ms - 60_000),
        ):
            row = read_game(gid)
            assert row["played_at"] == want, (
                f"{gid}: stored played_at={row['played_at']} expected {want} (ms)"
            )
    finally:
        purge_fixtures()


def check_played_at_migration_is_idempotent(module):
    purge_fixtures()
    insert_game(GAME_PREFIX + "mig_once", played_at=1700000001)
    try:
        # FIXER-U / ABUSE-18: the migration is triggered through
        # `startup_event()`, its sole legitimate caller, instead of through a
        # read route. The assertion is unchanged -- one pass normalises, a
        # second pass does not multiply the value again.
        module.startup_event()
        first = read_game(GAME_PREFIX + "mig_once")["played_at"]
        module.startup_event()
        second = read_game(GAME_PREFIX + "mig_once")["played_at"]
        assert first == second == 1700000001 * 1000, (first, second)
    finally:
        purge_fixtures()


# --------------------------------------------------------------------------- #
# CODE-02-04 : analyzed_at only on success + per-game error isolation
# --------------------------------------------------------------------------- #
def check_analyzed_at_only_on_success(module):
    purge_fixtures()
    good = GAME_PREFIX + "an_good"
    bad = GAME_PREFIX + "an_bad"
    reset_fake_analyzer(fail_ids=(bad,))
    games = [lichess_game(good), lichess_game(bad)]
    try:
        with patched_sync(module, games):
            client = make_client(module)
            res = client.post("/api/sync", params={"max_games": 5})
            assert res.status_code == 200, f"POST /api/sync -> {res.status_code} {res.text[:300]}"
        body = res.json()
        assert body.get("status") == "partial", (
            f"one game failed, so status must be 'partial', got {body.get('status')!r}"
        )
        assert body.get("failed_games") == 1, body
        assert body.get("synced_games") == 1, (
            f"the healthy game must still be synced (error isolation): {body}"
        )
        good_row = read_game(good)
        bad_row = read_game(bad)
        assert good_row["analyzed_at"] and good_row["analyzed_at"] > 0, (
            "the successfully analysed game must be stamped analyzed_at"
        )
        assert not bad_row["analyzed_at"], (
            f"the FAILED game was stamped analyzed_at={bad_row['analyzed_at']!r}; "
            "a crashed analysis must leave the game pending so it is retried"
        )
        # The internal engine error must not be reflected to the client.
        assert "totally_internal_secret_detail" not in res.text, res.text[:400]
        # The Stockfish handle must be released even though a game failed.
        assert FakeAnalyzer.instances and all(
            getattr(a, "closed", False) for a in FakeAnalyzer.instances
        ), "the analyzer was not closed on the error path (leaked Stockfish process)"

        # Retry: the failed game must be picked up again, not suppressed forever.
        reset_fake_analyzer()
        with patched_sync(module, games):
            client = make_client(module)
            res2 = client.post("/api/sync", params={"max_games": 5})
        assert res2.status_code == 200, res2.text[:300]
        retried = read_game(bad)
        assert retried["analyzed_at"] and retried["analyzed_at"] > 0, (
            "the previously failed game was skipped on the next sync instead of retried"
        )
        assert read_game(good)["analyzed_at"], "the already-analyzed game lost its flag"
    finally:
        purge_fixtures()


def check_total_sync_failure_is_not_200(module):
    purge_fixtures()
    gids = (GAME_PREFIX + "all_a", GAME_PREFIX + "all_b")
    reset_fake_analyzer(fail_ids=gids)
    games = [lichess_game(g) for g in gids]
    try:
        with patched_sync(module, games):
            client = make_client(module)
            res = client.post("/api/sync", params={"max_games": 5})
        assert res.status_code >= 500, (
            f"every game failed, so the response must not be 2xx: got {res.status_code} {res.text[:200]}"
        )
        assert "totally_internal_secret_detail" not in res.text, res.text[:400]
        for gid in gids:
            assert not read_game(gid)["analyzed_at"], f"{gid} must stay pending"
    finally:
        purge_fixtures()


# --------------------------------------------------------------------------- #
# CODE-02-11 : /api/sync truthfulness
# --------------------------------------------------------------------------- #
def check_sync_fetch_exception_is_502(module):
    reset_fake_analyzer()

    def boom(*a, **kw):
        raise ConnectionError("TLS handshake to lichess.org failed (internal host detail)")

    with patch.object(module.LichessClient, "fetch_user_games", side_effect=boom):
        with patch.object(module.LichessClient, "get_account_profile",
                          return_value={"username": UNIT_USER}):
            client = make_client(module)
            res = client.post("/api/sync", params={"max_games": 5})
    assert res.status_code == 502, (
        f"an upstream fetch failure must be a non-2xx, got {res.status_code} {res.text[:200]}"
    )
    assert "internal host detail" not in res.text, res.text[:300]


def check_sync_empty_shape_is_stable(module):
    """The empty result must carry the SAME keys as the success result."""
    with patched_sync(module, []):
        client = make_client(module)
        empty = client.post("/api/sync", params={"max_games": 5})
    assert empty.status_code == 200, empty.text[:200]
    body = empty.json()
    assert body.get("status") == "no_games", body
    for key in ("synced_games", "new_blunders", "failed_games", "errors", "count"):
        assert key in body, f"'no_games' body is missing {key!r}: {body}"

    reset_fake_analyzer()
    gid = GAME_PREFIX + "shape_ok"
    with patched_sync(module, [lichess_game(gid)]):
        client = make_client(module)
        ok = client.post("/api/sync", params={"max_games": 5})
    assert ok.status_code == 200, ok.text[:200]
    ok_body = ok.json()
    for key in ("synced_games", "new_blunders", "failed_games", "errors"):
        assert key in ok_body, f"'success' body is missing {key!r}: {ok_body}"
    try:
        missing = set(ok_body) - set(body)
        assert not missing, f"the two /api/sync shapes diverged on {sorted(missing)}"
    finally:
        purge_fixtures()


def check_sync_unreachable_upstream_not_reported_as_no_games(module):
    """Empty list + an unreachable account must not be reported as a quiet success."""
    with patch.object(module, "GameAnalyzer", FakeAnalyzer):
        with patch.object(module.LichessClient, "fetch_user_games", return_value=[]):
            with patch.object(module.LichessClient, "get_account_profile", return_value=None):
                with patch.object(module.LichessClient, "_resolve_token", return_value="lip_fake_token"):
                    client = make_client(module)
                    res = client.post("/api/sync", params={"max_games": 5})
    assert res.status_code >= 400, (
        f"an unreachable Lichess must not be reported as 200 no_games: {res.status_code} {res.text[:200]}"
    )


# --------------------------------------------------------------------------- #
# CODE-02-13 : no internal detail in error bodies
# --------------------------------------------------------------------------- #
SECRET = "no such column: totally_internal_secret_schema_detail"


def check_internal_valueerror_is_not_reflected(module):
    from backend.repertoire import RepertoireManager
    with patch.object(RepertoireManager, "get_repertoire_tree",
                      side_effect=ValueError(SECRET)):
        client = make_client(module)
        res = client.get("/api/repertoire/tree", params={"color": "white"})
    assert SECRET not in res.text, (
        f"internal schema detail leaked to the client: {res.status_code} {res.text[:200]}"
    )
    assert res.status_code == 500, (
        f"an internal ValueError must be a 500, got {res.status_code} {res.text[:200]}"
    )
    assert "Internal server error" in res.text, res.text[:200]


def check_scout_internal_error_is_not_reflected(module):
    from backend.scout import ContentScout
    with patch.object(ContentScout, "analyze_source",
                      side_effect=RuntimeError("yt-dlp exploded in /home/robin/.config/yt-dlp/config")):
        client = make_client(module)
        res = client.post("/api/scout/analyze", json={"source": "https://www.youtube.com/watch?v=abc"})
    assert "/home/robin" not in res.text, res.text[:300]
    assert "yt-dlp exploded" not in res.text, res.text[:300]
    assert res.status_code == 500, f"expected 500, got {res.status_code} {res.text[:200]}"


def check_user_input_errors_stay_informative(module):
    from backend.scout import ContentScout
    client = make_client(module)
    # Invalid FEN -> explicit, useful 400.
    res = client.get("/api/repertoire/explore", params={"fen": "garbage"})
    assert res.status_code == 400, res.status_code
    assert "FEN" in res.text, res.text[:200]
    # ContentScout's own ValueError for a bad source stays a useful 400.
    with patch.object(ContentScout, "analyze_source",
                      side_effect=ValueError("Could not identify content source for: not-a-source")):
        res = client.post("/api/scout/analyze", json={"source": "not-a-source"})
    assert res.status_code == 400, f"expected 400, got {res.status_code} {res.text[:200]}"
    assert "Could not identify content source" in res.text, res.text[:200]
    # Schema violation on a body field is still a 422 with a readable message.
    res = client.post("/api/settings", json={"max_opening_ply": -10})
    assert res.status_code == 422, res.status_code


def check_settings_survive_corrupt_values(module):
    from backend.db import set_setting
    saved = {k: get_setting(k) for k in ("max_opening_ply", "blunder_threshold_cp", "gap_threshold")}
    try:
        set_setting("max_opening_ply", "not-a-number")
        set_setting("blunder_threshold_cp", "")
        set_setting("gap_threshold", "NaN")
        client = make_client(module)
        res = client.get("/api/settings")
        assert res.status_code == 200, f"corrupt settings killed the page: {res.status_code} {res.text[:200]}"
        data = res.json()
        assert data["max_opening_ply"] == 24, data
        assert data["blunder_threshold_cp"] == 150, data
        assert data["gap_threshold"] == 5.0, data
    finally:
        for k, v in saved.items():
            if v is not None:
                set_setting(k, v)


# --------------------------------------------------------------------------- #
# CODE-05-04 : seed route is defensive
# --------------------------------------------------------------------------- #
def check_seed_never_returns_null_body(module):
    from backend.repertoire import RepertoireManager

    def fake_seed_none(self, force=False):
        """Mimics the real bug: no return value, but moves WERE written."""
        conn = get_db()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO repertoire_moves "
                "(repertoire, parent_fen, fen, move_uci, move_san, is_user_move, created_at) "
                "VALUES ('white', ?, ?, 'e2e4', 'e4', 1, 0)",
                (chess.STARTING_FEN, chess.STARTING_FEN),
            )
            conn.commit()
        finally:
            conn.close()
        return None

    # Start from a known-clean slate: a leftover fixture row from another run
    # would make the post-condition ambiguous.
    delete_repertoire_move("white", "e2e4")
    before = count_repertoire_moves()
    try:
        with patch.object(RepertoireManager, "seed_default_repertoires", fake_seed_none):
            client = make_client(module)
            res = client.post("/api/repertoire/seed", params={"force": "true"})
        assert res.status_code == 200, f"expected 200, got {res.status_code} {res.text[:200]}"
        body = res.json()
        assert body is not None, "the seed route returned a JSON null body"
        assert body.get("status") == "ok", (
            f"the route must synthesise a truthful ok from the verified post-condition: {body}"
        )
        assert body.get("seeded_moves", 0) > 0, body
    finally:
        purge_fixtures()
        delete_repertoire_move("white", "e2e4")
        assert count_repertoire_moves() == before, "the seed fixture leaked a repertoire row"


def check_seed_failure_is_not_reported_as_success(module):
    from backend.repertoire import RepertoireManager

    def fake_seed_nothing(self, force=False):
        return None  # the reported bug, but nothing was written

    with patch.object(RepertoireManager, "seed_default_repertoires", fake_seed_nothing):
        client = make_client(module)
        res = client.post("/api/repertoire/seed", params={"force": "true"})
    assert res.status_code >= 400, (
        f"a no-op seed must not be reported as success: {res.status_code} {res.text[:200]}"
    )
    assert res.json() is not None, "the seed route returned a JSON null body"


def check_seed_requires_explicit_opt_in(module):
    """Without force=true a non-empty repertoire is refused, never silently 'ok'."""
    delete_repertoire_move("white", "e2e4")
    try:
        client = make_client(module)
        added = client.post("/api/repertoire/move", json={
            "repertoire": "white", "parent_fen": chess.STARTING_FEN, "move_uci": "e2e4",
        })
        assert added.status_code == 200, f"fixture move not added: {added.status_code} {added.text[:200]}"
        res = client.post("/api/repertoire/seed")
        assert res.status_code >= 400, (
            f"seeding a non-empty repertoire without force must not be a 2xx: {res.status_code} {res.text[:200]}"
        )
        body = res.json()
        assert body is not None, "the seed route returned a JSON null body"
        assert body.get("status") == "skipped", body
        assert body.get("force_required") is True, body
        # Bad boolean input is rejected by schema validation, not coerced.
        res = client.post("/api/repertoire/seed", params={"force": "maybe"})
        assert res.status_code == 422, f"force=maybe must be a 422, got {res.status_code} {res.text[:200]}"
        # The guard must actually be in force: the user's move is still there.
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS c FROM repertoire_moves WHERE move_uci = 'e2e4'")
            assert cur.fetchone()["c"] >= 1, "the guarded seed overwrote the user's own move"
        finally:
            conn.close()
    finally:
        delete_repertoire_move("white", "e2e4")


# --------------------------------------------------------------------------- #
# CODE-02-12 : aliases delegate to one canonical handler
# --------------------------------------------------------------------------- #
def check_scout_aliases_delegate(module):
    from backend.scout import ContentScout
    calls = []

    def fake_check_line(self, repertoire="white", moves=None):
        calls.append(("check_line", repertoire, tuple(moves or [])))
        return {"status": "ok", "echo": [list(m) for m in (moves or [])]}

    def fake_check_lines(self, repertoire="white", lines=None):
        calls.append(("check_lines", repertoire, len(lines or [])))
        return {"status": "ok", "count": len(lines or [])}

    payload_line = {"repertoire": "white", "moves": [{"uci": "e2e4", "fen": chess.STARTING_FEN}]}
    payload_tree = {"repertoire": "white", "lines": [{"moves": payload_line["moves"], "label": "L1"}]}

    with ExitStack() as stack:
        stack.enter_context(patch.object(ContentScout, "check_line_overlap", fake_check_line))
        stack.enter_context(patch.object(ContentScout, "check_lines_overlap", fake_check_lines))
        client = make_client(module)
        a = client.post("/api/scout/check_line", json=payload_line)
        b = client.post("/api/repertoire/check_line", json=payload_line)
        c = client.post("/api/scout/check_tree", json=payload_tree)
        d = client.post("/api/repertoire/check_tree", json=payload_tree)
    for res in (a, b, c, d):
        assert res.status_code == 200, f"{res.request.url} -> {res.status_code} {res.text[:200]}"
    assert a.json() == b.json(), (
        f"alias /api/repertoire/check_line diverged from /api/scout/check_line:\n"
        f"  canonical={a.json()}\n  alias    ={b.json()}"
    )
    assert c.json() == d.json(), (
        f"alias /api/repertoire/check_tree diverged from /api/scout/check_tree:\n"
        f"  canonical={c.json()}\n  alias    ={d.json()}"
    )
    assert len(calls) == 4, f"expected exactly one canonical call per request, got {calls}"
    # The alias must be a documented thin delegation, not a second implementation.
    alias_doc = getattr(module.repertoire_check_line, "__doc__", "") or ""
    assert "Alias of POST /api/scout/check_line" in alias_doc, alias_doc


# --------------------------------------------------------------------------- #
# unittest wrapper
# --------------------------------------------------------------------------- #
CHECKS = [
    ("CODE-02-02 wrong method under /api/ is 405", check_wrong_method_is_405),
    ("CODE-02-02 static frontend still served", check_static_mount_still_serves_frontend),
    ("CODE-02-05 draws render as draws", check_draw_rendering),
    ("CODE-02-05 dashboard groups NULL results as draws", check_dashboard_groups_draws),
    ("CODE-02-05 sync stores 'draw' not NULL", check_sync_writer_stores_draw_token),
    ("CODE-02-06 played_at sorted by wall clock", check_played_at_sort_order),
    ("CODE-02-06 played_at migration is idempotent", check_played_at_migration_is_idempotent),
    ("CODE-02-04 analyzed_at only on successful analysis", check_analyzed_at_only_on_success),
    ("CODE-02-11 all-games-failed is not 2xx", check_total_sync_failure_is_not_200),
    ("CODE-02-11 fetch exception -> 502", check_sync_fetch_exception_is_502),
    ("CODE-02-11 stable /api/sync response shape", check_sync_empty_shape_is_stable),
    ("CODE-02-11 unreachable upstream is not 'no_games'", check_sync_unreachable_upstream_not_reported_as_no_games),
    ("CODE-02-13 internal ValueError not reflected", check_internal_valueerror_is_not_reflected),
    ("CODE-02-13 scout internal error not reflected", check_scout_internal_error_is_not_reflected),
    ("CODE-02-13 user-input errors stay informative", check_user_input_errors_stay_informative),
    ("CODE-02-13 settings survive corrupt values", check_settings_survive_corrupt_values),
    ("CODE-05-04 seed never returns a null body", check_seed_never_returns_null_body),
    ("CODE-05-04 seed failure is not a success", check_seed_failure_is_not_reported_as_success),
    ("CODE-05-04 seed requires explicit opt-in", check_seed_requires_explicit_opt_in),
    ("CODE-02-12 aliases delegate to canonical handlers", check_scout_aliases_delegate),
]


class TestApiContract(unittest.TestCase):
    """Runs every CHECKS entry against the live (patched) backend.api module."""

    def _run(self, fn):
        import backend.api as api_module
        fn(api_module)

    def test_00_contract_checks(self):
        failures = []
        for name, fn in CHECKS:
            with self.subTest(check=name):
                try:
                    self._run(fn)
                except Exception as exc:  # noqa: BLE001 - report, do not mask
                    failures.append(f"{name}: {exc}")
                    print(f"  FAIL  {name}\n        {exc}")
        self.assertEqual(failures, [], "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
