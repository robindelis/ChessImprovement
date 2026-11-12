"""
End-to-end tests for the Lichess Live Play, OAuth2 PKCE, Stockfish 19 Full-Game
Analysis, Opening Trap Coaching, Guided Repertoire Sequence Builder, and 4-Tab
Platform Frontend Contracts.
"""
import json
import os
import re
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import tests.test_setup  # noqa: F401 -- MUST BE FIRST: isolates SQLite DB

import chess
import chess.engine
from fastapi.testclient import TestClient

import backend.api as api_mod
import backend.game_analysis as ga_mod
from backend.db import closing_db, get_setting, init_db, set_setting
from backend.live_play import LivePlayManager
from backend.repertoire import RepertoireManager
from tests.frontend_runtime import node_available, run_scenario

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend")


class _StubEngine:
    """Fast deterministic Stockfish stub modelling the Fried Liver / Two Knights trap."""

    def __init__(self):
        self.calls = []

    def analyse(self, board: chess.Board, limit: chess.engine.Limit, multipv: int = 1):
        self.calls.append((board.fen(), limit.depth, multipv))
        fen4 = " ".join(board.fen().split()[:4])

        # Position before 4...Bc5?? (after 1.e4 e5 2.Nf3 Nc6 3.Bc4 Nf6 4.Ng5)
        trap_before = "r1bqkb1r/pppp1ppp/2n2n2/4p1N1/2B1P3/8/PPPP1PPP/RNBQK2R b KQkq -"
        # Position after 4...Bc5?? (allowing 5.Nxf7!)
        trap_after = "r1bqk2r/pppp1ppp/2n2n2/2b1p1N1/2B1P3/8/PPPP1PPP/RNBQK2R w KQkq -"
        # Position after 5.Nxf7
        after_nxf7 = "r1bqk2r/pppp1Npp/2n2n2/2b1p3/2B1P3/8/PPPP1PPP/RNBQK2R b KQkq -"

        if fen4 == trap_before:
            d5 = chess.Move.from_uci("d7d5")
            exd5 = chess.Move.from_uci("e4d5")
            na5 = chess.Move.from_uci("c6a5")
            bc5 = chess.Move.from_uci("f8c5")
            nxf7 = chess.Move.from_uci("g5f7")
            line1 = {"depth": limit.depth or 19, "multipv": 1, "score": chess.engine.PovScore(chess.engine.Cp(-15), chess.BLACK), "pv": [d5, exd5, na5]}
            line2 = {"depth": limit.depth or 19, "multipv": 2, "score": chess.engine.PovScore(chess.engine.Cp(-95), chess.BLACK), "pv": [chess.Move.from_uci("f6e4")]}
            line3 = {"depth": limit.depth or 19, "multipv": 3, "score": chess.engine.PovScore(chess.engine.Cp(-360), chess.BLACK), "pv": [bc5, nxf7]}
            return [line1, line2, line3][:multipv] if multipv > 1 else line1

        if fen4 == trap_after:
            nxf7 = chess.Move.from_uci("g5f7")
            qe7 = chess.Move.from_uci("d8e7")
            nxh8 = chess.Move.from_uci("f7h8")
            info = {"depth": limit.depth or 19, "multipv": 1, "score": chess.engine.PovScore(chess.engine.Cp(380), chess.WHITE), "pv": [nxf7, qe7, nxh8]}
            return [info] if multipv > 1 else info

        if fen4 == after_nxf7:
            qe7 = chess.Move.from_uci("d8e7")
            info = {"depth": limit.depth or 19, "multipv": 1, "score": chess.engine.PovScore(chess.engine.Cp(-380), chess.BLACK), "pv": [qe7]}
            return [info] if multipv > 1 else info

        legal = list(board.legal_moves)
        pv = [legal[0]] if legal else []
        info = {"depth": limit.depth or 19, "multipv": 1, "score": chess.engine.PovScore(chess.engine.Cp(20), chess.WHITE), "pv": pv}
        if multipv > 1:
            out = []
            for rank, mv in enumerate(legal[:multipv], 1):
                out.append({
                    "depth": limit.depth or 19,
                    "multipv": rank,
                    "score": chess.engine.PovScore(chess.engine.Cp(20 - rank * 15), chess.WHITE),
                    "pv": [mv],
                })
            return out
        return info


class _StubPool:
    def __init__(self, engine: _StubEngine):
        self._engine = engine

    @contextmanager
    def engine(self, timeout=None):
        yield self._engine


class TestFrontendPlatformContracts(unittest.TestCase):
    """Static + Node VM verification of the 4-workspace platform frontend."""

    def test_all_four_tabs_and_literal_dom_ids_resolve(self):
        with open(os.path.join(FRONTEND_DIR, "index.html"), "r", encoding="utf-8") as fh:
            html = fh.read()

        html_ids = set(re.findall(r'\bid="([^"]+)"', html))
        for required_view in ("viewPlay", "viewAnalysis", "viewRepertoire", "viewRepTrainer"):
            self.assertIn(required_view, html_ids, f"Missing workspace section #{required_view}")

        js_files = ("live_play.js", "game_analysis.js", "app.js")
        js_contents = {}
        for f in js_files:
            with open(os.path.join(FRONTEND_DIR, f), "r", encoding="utf-8") as fh:
                js_contents[f] = fh.read()
        all_js = "\n".join(js_contents.values())
        created_ids = set(re.findall(r"""\.id\s*=\s*['"]([^'"]+)['"]""", all_js))
        created_ids |= set(re.findall(r"""\bid\s*=\s*['"]([^'"]+)['"]""", all_js))
        known = html_ids | created_ids

        missing = {}
        for fname, src in js_contents.items():
            lookups = re.findall(r"""getElementById\(\s*['"]([^'"]+)['"]\s*\)""", src)
            unresolved = sorted({i for i in lookups if i not in known})
            if unresolved:
                missing[fname] = unresolved

        self.assertEqual(missing, {}, f"Unresolved DOM IDs in platform JS: {missing}")

    @unittest.skipUnless(node_available(), "Node.js required for real frontend VM test")
    def test_fair_play_ui_lock_blocks_repertoire_and_analysis_tabs(self):
        res = run_scenario("""
            window.app.showTab('repertoire');
            const beforeLock = window.app.currentTab;
            window.app.setFairPlayLock(true, 'liveGame99');
            window.app.showTab('play');
            window.app.showTab('repertoire');
            const blockedTab = window.app.currentTab;
            window.app.setFairPlayLock(false, null);
            window.app.showTab('analysis');
            const unlockedTab = window.app.currentTab;
            return { beforeLock, blockedTab, unlockedTab };
        """)
        self.assertEqual(res["beforeLock"], "repertoire")
        self.assertEqual(res["blockedTab"], "play")
        self.assertEqual(res["unlockedTab"], "analysis")


class TestOAuthAndLivePlayPlatform(unittest.TestCase):
    """Tests OAuth2 PKCE, Live Board API state/clocks, Fair-Play API Lock, and auto-persistence."""

    def setUp(self):
        init_db()
        self.client = TestClient(api_mod.app, base_url="http://127.0.0.1:38281", raise_server_exceptions=False)
        self.mgr = LivePlayManager.get_instance()
        with self.mgr._lock:
            self.mgr._active_game = None
            self.mgr._seeking = False
            self.mgr._seek_info = None
        set_setting("lichess_username", "RobiDeli")
        set_setting("lichess_token", "lip_testtoken123456")

    def tearDown(self):
        with self.mgr._lock:
            self.mgr._active_game = None
            self.mgr._seeking = False
            self.mgr._seek_info = None

    def test_oauth_pkce_flow_and_local_boundary_guard(self):
        start_res = self.client.post("/api/auth/lichess/start")
        self.assertEqual(start_res.status_code, 200)
        body = start_res.json()
        self.assertIn("https://lichess.org/oauth?", body["auth_url"])
        self.assertIn("board%3Aplay", body["auth_url"])
        state = body["state"]

        # Cross-site fetch on normal endpoints MUST still be rejected with 403
        blocked = self.client.get("/api/settings", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(blocked.status_code, 403)

        # Top-level navigate GET on OAuth callback succeeds with mocked Lichess token endpoint
        mock_token_resp = MagicMock(status_code=200)
        mock_token_resp.json.return_value = {"access_token": "lip_oauth_fresh_token99"}
        mock_acct_resp = MagicMock(status_code=200, content=b'{"username":"RobiDeli"}')
        mock_acct_resp.json.return_value = {"username": "RobiDeli", "perfs": {"rapid": {"rating": 1850}}}
        mock_acct_resp.headers = {"X-OAuth-Scopes": "board:play, challenge:write"}

        with patch("backend.lichess_auth.requests.post", return_value=mock_token_resp), \
             patch("backend.lichess_client.requests.get", return_value=mock_acct_resp), \
             patch("backend.lichess_auth.requests.get", return_value=mock_acct_resp):
            cb_res = self.client.get(
                f"/api/auth/lichess/callback?code=authcode123&state={state}",
                headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"},
            )
            self.assertEqual(cb_res.status_code, 200)
            self.assertIn("Connected to Lichess", cb_res.text)
            self.assertEqual(get_setting("lichess_token"), "lip_oauth_fresh_token99")

            st_res = self.client.get("/api/auth/lichess/status")
            self.assertEqual(st_res.status_code, 200)
            st = st_res.json()
            self.assertTrue(st["connected"])
            self.assertTrue(st["can_play"])

    def test_board_api_seek_validation_live_clocks_fair_play_lock_and_persistence(self):
        # 1. Blitz random pool seek is cleanly rejected per Lichess Board API rules
        blitz_res = self.client.post("/api/play/seek", json={"minutes": 3, "increment": 0, "rated": True, "color": "random"})
        self.assertEqual(blitz_res.status_code, 400)
        self.assertIn("Rapid", blitz_res.json()["detail"])

        # 2. Simulate Lichess `gameFull` frame starting a live 10+0 Rapid game
        game_id = "zzLiveTrap01"
        self.mgr.apply_game_stream_frame(game_id, {
            "type": "gameFull",
            "id": game_id,
            "rated": True,
            "speed": "rapid",
            "createdAt": 1794500000000,
            "white": {"id": "oppplayer", "name": "OppPlayer", "rating": 1820},
            "black": {"id": "robideli", "name": "RobiDeli", "rating": 1800},
            "clock": {"initial": 600000, "increment": 0},
            "initialFen": "startpos",
            "state": {
                "type": "gameState",
                "moves": "e2e4 e7e5 g1f3",
                "wtime": 595000,
                "btime": 598000,
                "winc": 0,
                "binc": 0,
                "status": "started",
            },
        })

        state = self.client.get("/api/play/state").json()
        self.assertTrue(state["fair_play_locked"])
        self.assertEqual(state["game"]["user_color"], "black")
        self.assertEqual(state["game"]["turn"], "black")
        self.assertEqual(state["game"]["wtime_ms"], 595000)
        self.assertEqual(len(state["game"]["moves"]), 3)

        # 3. Fair-Play Lock blocks repertoire tree, explorer, eval stream, trainer, and analysis!
        self.assertEqual(self.client.get("/api/repertoire/tree?color=black").status_code, 409)
        self.assertEqual(self.client.get(f"/api/repertoire/explore?fen={chess.STARTING_FEN}&color=black").status_code, 409)
        self.assertEqual(self.client.get(f"/api/repertoire/eval_stream?fen={chess.STARTING_FEN}").status_code, 409)
        self.assertEqual(self.client.get("/api/repertoire/train/next?color=black").status_code, 409)

        # 4. Submitting our legal move sends UCI to Lichess Board API
        mock_move_ok = MagicMock(status_code=200)
        with patch("backend.live_play.requests.post", return_value=mock_move_ok) as post_mock:
            mv_res = self.client.post("/api/play/move", json={"game_id": game_id, "move_uci": "b8c6"})
            self.assertEqual(mv_res.status_code, 200)
            self.assertTrue(post_mock.call_args[0][0].endswith(f"/api/board/game/{game_id}/move/b8c6"))

        # 5. Game finishes -> Fair-Play Lock lifts AND game is persisted automatically into `games`!
        self.mgr.apply_game_stream_frame(game_id, {
            "type": "gameState",
            "moves": "e2e4 e7e5 g1f3 b8c6 f1c4 g8f6 f3g5 f8c5 g5f7",
            "wtime": 580000,
            "btime": 572000,
            "status": "resign",
            "winner": "white",
        })

        after_state = self.client.get("/api/play/state").json()
        self.assertFalse(after_state["fair_play_locked"])
        self.assertEqual(self.client.get("/api/repertoire/tree?color=black").status_code, 200)

        with closing_db() as conn:
            saved_row = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        self.assertIsNotNone(saved_row)
        self.assertEqual(saved_row["moves_san"], "e4 e5 Nf3 Nc6 Bc4 Nf6 Ng5 Bc5 Nxf7")
        self.assertEqual(saved_row["user_color"], "black")
        self.assertEqual(saved_row["result"], "white")

        # Cleanup fixture game
        with closing_db() as conn:
            conn.execute("DELETE FROM blunders WHERE game_id = ?", (game_id,))
            conn.execute("DELETE FROM games WHERE id = ?", (game_id,))
            conn.commit()


class TestFullGameAnalysisAndTrapToRepertoireFlow(unittest.TestCase):
    """Tests Depth-19 Stockfish review, opening-trap MultiPV guidance, and safe sequence saving."""

    def setUp(self):
        init_db()
        self.client = TestClient(api_mod.app, base_url="http://127.0.0.1:38281", raise_server_exceptions=False)
        self.rm = RepertoireManager.get_instance()
        self.game_id = "zzTrapReview01"

        with closing_db() as conn:
            self._rep_backup = [tuple(r) for r in conn.execute("SELECT * FROM repertoire_moves").fetchall()]
            conn.execute("DELETE FROM repertoire_moves WHERE repertoire = 'black'")
            conn.execute(
                """
                INSERT OR REPLACE INTO games (
                    id, speed, rated, played_at, white_player, black_player,
                    white_rating, black_rating, user_color, result, opening_name, eco, moves_san
                ) VALUES (?, 'rapid', 1, 1794500000000, 'TrapAttacker', 'RobiDeli',
                          1850, 1820, 'black', 'white', 'Two Knights Defense', 'C57',
                          'e4 e5 Nf3 Nc6 Bc4 Nf6 Ng5 Bc5 Nxf7')
                """,
                (self.game_id,),
            )
            conn.commit()
        self.rm.close()

        # Seed Black repertoire up to 3...Nf6 (so 4.Ng5 is an unprepared opponent novelty!)
        b = chess.Board()
        for uci, is_user in [("e2e4", False), ("e7e5", True), ("g1f3", False), ("b8c6", True), ("f1c4", False), ("g8f6", True)]:
            self.rm.add_repertoire_move("black", b.fen(), uci, is_user_move=is_user)
            b.push(chess.Move.from_uci(uci))

    def tearDown(self):
        with closing_db() as conn:
            conn.execute("DELETE FROM drill_reviews WHERE blunder_id IN (SELECT id FROM blunders WHERE game_id = ?)", (self.game_id,))
            conn.execute("DELETE FROM blunders WHERE game_id = ?", (self.game_id,))
            conn.execute("DELETE FROM games WHERE id = ?", (self.game_id,))
            conn.execute("DELETE FROM repertoire_moves")
            for row in self._rep_backup:
                conn.execute(
                    "INSERT OR REPLACE INTO repertoire_moves VALUES (" + ",".join("?" * len(row)) + ")",
                    row,
                )
            conn.commit()
        self.rm.close()

    def test_trap_game_review_at_depth_19_and_guided_sequence_save(self):
        stub_engine = _StubEngine()
        stub_pool = _StubPool(stub_engine)

        with patch.object(ga_mod, "get_sync_engine_pool", return_value=stub_pool):
            res = self.client.get(f"/api/analysis/{self.game_id}?depth=19")
            self.assertEqual(res.status_code, 200)
            payload = res.json()["analysis"]

        self.assertEqual(payload["depth"], 19)
        self.assertEqual(payload["first_novelty_ply"], 7)  # 4.Ng5 left our repertoire!
        self.assertEqual(len(payload["plies"]), 9)

        # Verify 4...Bc5?? (ply 8) is flagged as an opening trap with MultiPV=3 safe moves!
        ply8 = payload["plies"][7]
        self.assertEqual(ply8["san"], "Bc5")
        self.assertEqual(ply8["classification"], "blunder")
        self.assertEqual(ply8["best_san"], "d5")
        self.assertEqual(len(ply8["candidates"]), 3)
        self.assertEqual(ply8["candidates"][0]["san"], "d5")

        moments = payload["critical_moments"]
        self.assertGreaterEqual(len(moments), 1)
        self.assertEqual(moments[0]["kind"], "opening_trap")
        self.assertEqual(moments[0]["best_san"], "d5")

        # User plays out the engine's guided trap refutation: 1.e4 e5 2.Nf3 Nc6 3.Bc4 Nf6 4.Ng5 d5 5.exd5 Na5
        b = chess.Board()
        seq_moves = []
        for uci in ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6", "g5" if False else "f3g5", "d7d5", "e4d5", "c6a5"]:
            mv = chess.Move.from_uci(uci)
            seq_moves.append({"fen_before": b.fen(), "uci": uci, "san": b.san(mv)})
            b.push(mv)

        # Preview shows 6 existing moves + 4 new moves, 0 conflicts
        prev_res = self.client.post("/api/analysis/preview_sequence", json={
            "repertoire": "black",
            "moves": seq_moves,
        })
        self.assertEqual(prev_res.status_code, 200)
        prev = prev_res.json()
        self.assertEqual(prev["existing_count"], 6)
        self.assertEqual(prev["new_count"], 4)
        self.assertEqual(prev["conflict_count"], 0)

        # Save sequence into Black repertoire
        save_res = self.client.post("/api/analysis/save_sequence", json={
            "repertoire": "black",
            "moves": seq_moves,
            "prioritize_for_training": True,
            "note": "Avoid Fried Liver trap with 4...d5!",
        })
        self.assertEqual(save_res.status_code, 200)
        saved = save_res.json()
        self.assertEqual(saved["status"], "success")
        self.assertEqual(saved["added_moves"], 4)

        # Verify No-Data-Loss Invariant: trying to overwrite 4...d5 with 4...Bc5 without replace_conflicts=True returns conflict!
        b_conf = chess.Board()
        conflict_seq = []
        for uci in ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6", "f3g5", "f8c5"]:
            mv = chess.Move.from_uci(uci)
            conflict_seq.append({"fen_before": b_conf.fen(), "uci": uci, "san": b_conf.san(mv)})
            b_conf.push(mv)

        conf_res = self.client.post("/api/analysis/save_sequence", json={
            "repertoire": "black",
            "moves": conflict_seq,
            "replace_conflicts": False,
        })
        self.assertEqual(conf_res.status_code, 200)
        self.assertEqual(conf_res.json()["status"], "conflict")

        # Verify Trainer now serves the newly added Black repertoire line (5 user moves total)!
        drill_res = self.client.get("/api/repertoire/train/next?color=black&mode=adaptive")
        self.assertEqual(drill_res.status_code, 200)
        drill = drill_res.json()
        self.assertIn("parent_fen", drill)
        self.assertIn("expected_uci", drill)
        self.assertEqual(drill["queue_summary"]["total_moves"], 5)


if __name__ == "__main__":
    unittest.main()
