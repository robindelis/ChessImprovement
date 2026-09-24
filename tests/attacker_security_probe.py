"""
Adversarial Security & Robustness Probe Suite for Repertoire Trainer.

Exhaustively attacks:
1. Malicious/corrupt/fuzzed FEN inputs
2. Injection attempts in opening/study parameters (SQLi, path traversal, shell chars, unicode)
3. Manipulated UCI move inputs (underpromotions, pseudo-legal, wrong turn, malformed)
4. Race conditions, rapid consecutive submissions, and spaced repetition math/overflows
5. Exploitation of study filtering, deletions, and cross-repertoire leakage
6. Repertoire progress calculations with 0 moves and multi-study sharing
"""

import sys
import os
import gc

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# CODE-05-10: first backend-adjacent import must be `tests.test_setup`.
import tests.test_setup
from tests.probe_guard import require_isolated_db

import time
import json
import sqlite3
import threading
import concurrent.futures

# Use isolated test DB
import tests.test_setup
from backend.db import get_db, init_db
from backend.repertoire import RepertoireManager
from backend.api import app
from fastapi.testclient import TestClient
import chess

client = TestClient(app, raise_server_exceptions=False)
rm = RepertoireManager.get_instance()

class AttackProbeRunner:
    def __init__(self):
        self.results = []
        self.vulnerabilities = []

    def log_probe(self, probe_id, name, prediction, action, result, status, details=""):
        entry = {
            "probe_id": probe_id,
            "name": name,
            "prediction": prediction,
            "action": action,
            "result": result,
            "status": status,
            "details": details
        }
        self.results.append(entry)
        if status in ("VULNERABILITY", "UNHANDLED_500", "DATA_CORRUPTION", "BEHAVIOR_MISMATCH"):
            self.vulnerabilities.append(entry)

        status_symbol = "❌ [FAIL]" if status != "PASS" else "✅ [PASS]"
        print(f"\n{status_symbol} {probe_id}: {name}")
        print(f"   PREDICTED: {prediction}")
        print(f"   ACTION:    {action}")
        print(f"   OUTCOME:   {status} - {result}")
        if details:
            print(f"   DETAILS:   {details}")

    def reset_repertoire(self):
        gc.collect()
        conn = get_db()
        conn.execute("DELETE FROM repertoire_moves")
        conn.execute("DELETE FROM repertoire_studies")
        conn.execute("DELETE FROM study_moves")
        conn.commit()
        conn.close()
        gc.collect()

    # =========================================================================
    # CATEGORY A: Malicious / Corrupt / Fuzzed FEN Inputs
    # =========================================================================
    def run_category_a(self):
        print("\n" + "="*80)
        print(" CATEGORY A: MALICIOUS / CORRUPT / FUZZED FEN INPUTS")
        print("="*80)

        # Probe A1: Empty string FEN
        pred = "HTTP 400 with detail 'Invalid FEN string' (is_valid_fen fails)"
        res = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": "",
            "attempted_uci": "e2e4"
        })
        status = "PASS" if res.status_code == 400 else "UNHANDLED_500" if res.status_code == 500 else "BEHAVIOR_MISMATCH"
        self.log_probe("A1", "Empty string FEN", pred, "POST /attempt with current_fen=''", f"Status {res.status_code}: {res.text}", status)

        # Probe A2: Structurally malformed FEN (missing parts, wrong rank counts)
        fuzzed_fens = [
            ("A2.1", "not-a-chess-fen", "Completely invalid string"),
            ("A2.2", "8/8/8/8/8/8/8 w - - 0 1", "Missing 8th rank"),
            ("A2.3", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR", "Only 1 token (missing active color and castling)"),
            ("A2.4", "rnbqkbnr/pppppppp/9/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "Invalid rank length '9'"),
            ("A2.5", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR x KQkq - 0 1", "Invalid turn 'x'")
        ]
        for sub_id, f_fen, desc in fuzzed_fens:
            pred = "HTTP 400 with 'Invalid FEN string'"
            res = client.post("/api/repertoire/train/attempt", json={
                "repertoire": "white",
                "current_fen": f_fen,
                "attempted_uci": "e2e4"
            })
            status = "PASS" if res.status_code == 400 else "UNHANDLED_500" if res.status_code == 500 else "BEHAVIOR_MISMATCH"
            self.log_probe(sub_id, f"Malformed FEN: {desc}", pred, f"POST /attempt with current_fen='{f_fen}'", f"Status {res.status_code}: {res.text}", status)

        # Probe A3: Board-illegal FEN (kings adjacent, no kings, multiple kings, pawns on 1st/8th)
        illegal_fens = [
            ("A3.1", "8/8/8/8/8/8/8/8 w - - 0 1", "Empty board (no kings)"),
            ("A3.2", "4k3/4K3/8/8/8/8/8/8 w - - 0 1", "Kings adjacent (in check illegal)"),
            ("A3.3", "PPPPPPPP/8/8/8/8/8/8/pppppppp w - - 0 1", "Pawns on 1st and 8th ranks"),
            ("A3.4", "k7/8/8/8/8/8/8/8 w - - 0 1", "Single black king, missing white king")
        ]
        for sub_id, f_fen, desc in illegal_fens:
            pred = "HTTP 400 with 'Invalid FEN string' (board.is_valid() returns False)"
            res = client.post("/api/repertoire/train/attempt", json={
                "repertoire": "white",
                "current_fen": f_fen,
                "attempted_uci": "e2e4"
            })
            status = "PASS" if res.status_code == 400 else "UNHANDLED_500" if res.status_code == 500 else "BEHAVIOR_MISMATCH"
            self.log_probe(sub_id, f"Board-illegal FEN: {desc}", pred, f"POST /attempt with current_fen='{f_fen}'", f"Status {res.status_code}: {res.text}", status)

        # Probe A4: Huge / buffer overflow FEN strings (10,000+ characters)
        huge_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1" + "A" * 10000
        pred = "HTTP 400 with 'Invalid FEN string' without memory crash or 500"
        res = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": huge_fen,
            "attempted_uci": "e2e4"
        })
        status = "PASS" if res.status_code == 400 else "UNHANDLED_500" if res.status_code == 500 else "BEHAVIOR_MISMATCH"
        self.log_probe("A4", "Huge FEN (10k chars)", pred, "POST /attempt with 10k appended characters", f"Status {res.status_code}", status)

        # Probe A5: FEN with SQL injection payload
        sqli_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'; DROP TABLE repertoire_moves; --"
        pred = "HTTP 400 with 'Invalid FEN string'; no SQL execution"
        res = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": sqli_fen,
            "attempted_uci": "e2e4"
        })
        status = "PASS" if res.status_code == 400 else "UNHANDLED_500" if res.status_code == 500 else "BEHAVIOR_MISMATCH"
        self.log_probe("A5", "SQLi in FEN", pred, f"POST /attempt with SQLi FEN", f"Status {res.status_code}", status)

        # Probe A6: FEN with Unicode / Null bytes
        null_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR\x00 w KQkq - 0 1"
        pred = "HTTP 400 (validation failure) or 422, no unhandled 500"
        res = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": null_fen,
            "attempted_uci": "e2e4"
        })
        status = "PASS" if res.status_code in (400, 422) else "UNHANDLED_500" if res.status_code == 500 else "BEHAVIOR_MISMATCH"
        self.log_probe("A6", "Null byte in FEN", pred, "POST /attempt with embedded \\x00 in FEN", f"Status {res.status_code}", status)

    # =========================================================================
    # CATEGORY B: Injection Attempts in Study & Opening Parameters
    # =========================================================================
    def run_category_b(self):
        print("\n" + "="*80)
        print(" CATEGORY B: INJECTION ATTEMPTS IN STUDY & OPENING PARAMETERS")
        print("="*80)

        # Setup baseline Italian Game
        self.reset_repertoire()
        rm.seed_default_repertoires()

        sqli_payloads = [
            ("B1.1", "' OR '1'='1", "Classic boolean bypass"),
            ("B1.2", "'; DROP TABLE repertoire_moves; --", "Piggybacked DROP statement"),
            ("B1.3", "' UNION SELECT 1, 'hacked', 'white', '', 0, 0 --", "Union-based injection"),
            ("B1.4", "admin'--", "Comment truncation")
        ]

        for sub_id, payload, desc in sqli_payloads:
            # Test in /train/next
            pred = "Safe parameterization: HTTP 404 position not found, no SQL error or 500"
            res = client.get(f"/api/repertoire/train/next?color=white&opening={payload}")
            status = "PASS" if res.status_code == 404 else "UNHANDLED_500" if res.status_code == 500 else "BEHAVIOR_MISMATCH"
            self.log_probe(f"{sub_id}_next", f"SQLi in /train/next opening ({desc})", pred, f"opening={payload}", f"Status {res.status_code}", status)

            # Test in /train/progress
            pred = "Safe parameterization: HTTP 200 with 0 moves or empty stats, no SQL error"
            res2 = client.get(f"/api/repertoire/train/progress?color=white&study={payload}")
            status2 = "PASS" if res2.status_code == 200 else "UNHANDLED_500" if res2.status_code == 500 else "BEHAVIOR_MISMATCH"
            self.log_probe(f"{sub_id}_prog", f"SQLi in /train/progress study ({desc})", pred, f"study={payload}", f"Status {res2.status_code}", status2)

        # Probe B2: Path traversal payloads
        path_payloads = ["../../../../etc/passwd", "..\\..\\..\\windows\\system32", "/var/log/syslog"]
        for p in path_payloads:
            pred = "Safe parameterization: 404 on /train/next"
            res = client.get(f"/api/repertoire/train/next?color=white&opening={p}")
            status = "PASS" if res.status_code == 404 else "UNHANDLED_500" if res.status_code == 500 else "BEHAVIOR_MISMATCH"
            self.log_probe("B2", "Path traversal in opening parameter", pred, f"opening={p}", f"Status {res.status_code}", status)

        # Probe B3: Shell execution characters
        shell_payloads = ["$(whoami)", "; id", "`ping -c 1 localhost`", "| cat /etc/passwd"]
        for s in shell_payloads:
            pred = "Safe string handling: 404 on /train/next, no command execution"
            res = client.get(f"/api/repertoire/train/next?color=white&opening={s}")
            status = "PASS" if res.status_code == 404 else "UNHANDLED_500" if res.status_code == 500 else "BEHAVIOR_MISMATCH"
            self.log_probe("B3", "Shell chars in opening parameter", pred, f"opening={s}", f"Status {res.status_code}", status)

        # Probe B4: Special characters, Unicode, XSS
        unicode_payloads = [
            ("<script>alert('XSS')</script>", "XSS script tags"),
            ("\u202eRTL_OVERRIDE", "Right-to-left override"),
            ("👑🔥♟️_CHESS_EMOJI", "Multi-byte emojis"),
            ("A" * 5000, "5000 char long study name")
        ]
        for u_pay, desc in unicode_payloads:
            pred = "Handled gracefully (404 not found or safe response), no crash"
            res = client.get(f"/api/repertoire/train/next?color=white&opening={u_pay}")
            status = "PASS" if res.status_code == 404 else "UNHANDLED_500" if res.status_code == 500 else "BEHAVIOR_MISMATCH"
            self.log_probe("B4", f"Special/Unicode: {desc}", pred, f"opening={desc}", f"Status {res.status_code}", status)

        # Probe B5: Duplicate Study Creation Vulnerability Probe!
        pred = "HTTP 200 with status='exists' or HTTP 400 gracefully indicating study already exists; NO unhandled 500"
        create_payload = {"name": "UniqueSecurityStudyTest", "repertoire": "white", "description": "Test study"}
        # First creation: should succeed
        res1 = client.post("/api/repertoire/studies", json=create_payload)
        # Second creation: duplicate name for same repertoire
        res2 = client.post("/api/repertoire/studies", json=create_payload)
        if res2.status_code == 500:
            status = "VULNERABILITY"
            details = "CRITICAL BUG: POST /api/repertoire/studies triggers unhandled 500 Internal Server Error due to NameError: name 'sqlite3' is not defined in backend/repertoire.py line 766."
        elif res2.status_code in (200, 400):
            status = "PASS"
            details = f"Gracefully handled with status {res2.status_code}"
        else:
            status = "BEHAVIOR_MISMATCH"
            details = f"Unexpected status code {res2.status_code}"
        self.log_probe("B5", "Duplicate Study Creation (NameError Probe)", pred, "POST /api/repertoire/studies twice with same name", f"Status {res2.status_code}: {res2.text}", status, details)
        gc.collect()

        # Probe B6: Study creation validation
        # B6.1: Empty name
        pred = "HTTP 400 with detail 'Study name cannot be empty'"
        res_empty_st = client.post("/api/repertoire/studies", json={"name": "", "repertoire": "white"})
        status_b61 = "PASS" if res_empty_st.status_code == 400 else "BEHAVIOR_MISMATCH"
        self.log_probe("B6.1", "Study creation with empty name", pred, "name=''", f"Status {res_empty_st.status_code}", status_b61)

        # B6.2: Whitespace-only name
        pred = "HTTP 400 with detail 'Study name cannot be empty'"
        res_ws_st = client.post("/api/repertoire/studies", json={"name": "   ", "repertoire": "white"})
        status_b62 = "PASS" if res_ws_st.status_code == 400 else "BEHAVIOR_MISMATCH"
        self.log_probe("B6.2", "Study creation with whitespace name", pred, "name='   '", f"Status {res_ws_st.status_code}", status_b62)

        # B6.3: Invalid repertoire color
        pred = "HTTP 422 Unprocessable Entity (Literal['white', 'black'] validation)"
        res_inv_rep = client.post("/api/repertoire/studies", json={"name": "Valid Name", "repertoire": "blue"})
        status_b63 = "PASS" if res_inv_rep.status_code == 422 else "BEHAVIOR_MISMATCH"
        self.log_probe("B6.3", "Study creation with invalid repertoire", pred, "repertoire='blue'", f"Status {res_inv_rep.status_code}", status_b63)

        # Probe B7: Study deletion edge cases
        # B7.1: Negative study_id
        pred = "HTTP 404 with detail 'Study not found'"
        res_del_neg = client.delete("/api/repertoire/studies/-1")
        status_b71 = "PASS" if res_del_neg.status_code == 404 else "BEHAVIOR_MISMATCH"
        self.log_probe("B7.1", "Study deletion with negative id (-1)", pred, "DELETE /studies/-1", f"Status {res_del_neg.status_code}", status_b71)

        # B7.2: Zero study_id
        pred = "HTTP 404 with detail 'Study not found'"
        res_del_zero = client.delete("/api/repertoire/studies/0")
        status_b72 = "PASS" if res_del_zero.status_code == 404 else "BEHAVIOR_MISMATCH"
        self.log_probe("B7.2", "Study deletion with zero id (0)", pred, "DELETE /studies/0", f"Status {res_del_zero.status_code}", status_b72)

        # B7.3: String study_id
        pred = "HTTP 422 Unprocessable Entity (int path param)"
        res_del_str = client.delete("/api/repertoire/studies/invalid_string")
        status_b73 = "PASS" if res_del_str.status_code == 422 else "BEHAVIOR_MISMATCH"
        self.log_probe("B7.3", "Study deletion with string id", pred, "DELETE /studies/invalid_string", f"Status {res_del_str.status_code}", status_b73)

    # =========================================================================
    # CATEGORY C: Manipulated UCI Move Inputs
    # =========================================================================
    def run_category_c(self):
        print("\n" + "="*80)
        print(" CATEGORY C: MANIPULATED UCI MOVE INPUTS")
        print("="*80)
        gc.collect()

        # Baseline: Italian Game seeded
        # 1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5
        starting_fen = chess.STARTING_FEN

        # Probe C1: Underpromotions
        # Setup promotion position: White pawn on e7, black king on a8, white king on h1
        promo_fen = "k7/4P3/8/8/8/8/8/7K w - - 0 1"
        rm.add_repertoire_move("white", promo_fen, "e7e8n", is_user_move=True)
        pred = "Underpromotion e7e8n recognized as legal and correct match"
        res_under = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": promo_fen,
            "attempted_uci": "e7e8n"
        })
        data_under = res_under.json() if res_under.status_code == 200 else {}
        status_under = "PASS" if data_under.get("correct") is True else "BEHAVIOR_MISMATCH"
        self.log_probe("C1.1", "Underpromotion match (e7e8n)", pred, "POST /attempt with e7e8n", f"correct={data_under.get('correct')}", status_under)

        # What if user plays e7e8q when e7e8n was expected?
        pred = "Legal move on board, but incorrect according to repertoire (correct=False)"
        res_queen = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": promo_fen,
            "attempted_uci": "e7e8q"
        })
        data_queen = res_queen.json() if res_queen.status_code == 200 else {}
        status_queen = "PASS" if data_queen.get("correct") is False and "expected_uci" in data_queen else "BEHAVIOR_MISMATCH"
        self.log_probe("C1.2", "Promotion mismatch (e7e8q vs e7e8n)", pred, "POST /attempt with e7e8q", f"correct={data_queen.get('correct')}", status_queen)

        # Probe C2: Non-pawn moves with fake promotion suffix (e.g. e1g1q on castling, b1c3n)
        # Position where White can castle:
        castle_fen = "rnbqk2r/pppp1ppp/5n2/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4"
        rm.add_repertoire_move("white", castle_fen, "e1g1", is_user_move=True)
        pred = "Fake promotion character normalized or parsed safely without crash"
        res_fake = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": castle_fen,
            "attempted_uci": "e1g1q"
        })
        status_fake = "PASS" if res_fake.status_code == 200 else "UNHANDLED_500" if res_fake.status_code == 500 else "BEHAVIOR_MISMATCH"
        self.log_probe("C2", "Non-pawn with promo suffix (e1g1q)", pred, "POST /attempt with e1g1q", f"Status {res_fake.status_code}: {res_fake.text}", status_fake)

        # Probe C3: Pseudo-legal move (piece moving through pieces: e1e3 on starting position)
        pred = "correct=False with message indicating illegal move, no 500"
        res_pseudo = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": starting_fen,
            "attempted_uci": "e1e3"
        })
        data_pseudo = res_pseudo.json() if res_pseudo.status_code == 200 else {}
        status_pseudo = "PASS" if data_pseudo.get("correct") is False and "Illegal move" in data_pseudo.get("message", "") else "BEHAVIOR_MISMATCH"
        self.log_probe("C3", "Pseudo-legal move (e1e3 through pawns)", pred, "POST /attempt with e1e3", f"correct={data_pseudo.get('correct')}, msg={data_pseudo.get('message')}", status_pseudo)

        # Probe C4: King moving into check
        # C4.1: Missing king board FEN
        missing_king_fen = "4r3/8/8/8/8/8/8/4K3 w - - 0 1"
        pred = "HTTP 400 with 'Invalid FEN string' (missing king)"
        res_check_missing = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": missing_king_fen,
            "attempted_uci": "e1e2"
        })
        status_c41 = "PASS" if res_check_missing.status_code == 400 else "BEHAVIOR_MISMATCH"
        self.log_probe("C4.1", "Move with missing king FEN", pred, "POST /attempt with missing king", f"Status {res_check_missing.status_code}", status_c41)

        # C4.2: Legal position with check (White king on e1, Black king on h1, Black rook on e8)
        check_fen = "4r3/8/8/8/8/8/8/4K2k w - - 0 1"
        pred = "correct=False with message indicating illegal move (king cannot remain in check)"
        res_check = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": check_fen,
            "attempted_uci": "e1e2"
        })
        data_check = res_check.json() if res_check.status_code == 200 else {}
        status_c42 = "PASS" if data_check.get("correct") is False and "Illegal move" in data_check.get("message", "") else "BEHAVIOR_MISMATCH"
        self.log_probe("C4.2", "King moving into check (e1e2)", pred, "POST /attempt with e1e2", f"correct={data_check.get('correct')}, msg={data_check.get('message')}", status_c42)

        # Probe C5: Move played for wrong color (Black pawn move on White's turn)
        pred = "correct=False with message indicating illegal move"
        res_turn = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": starting_fen,
            "attempted_uci": "e7e5" # Black pawn move on White's turn
        })
        data_turn = res_turn.json() if res_turn.status_code == 200 else {}
        status_turn = "PASS" if data_turn.get("correct") is False and "Illegal move" in data_turn.get("message", "") else "BEHAVIOR_MISMATCH"
        self.log_probe("C5", "Wrong color move (e7e5 on White turn)", pred, "POST /attempt with e7e5", f"correct={data_turn.get('correct')}, msg={data_turn.get('message')}", status_turn)

        # Probe C6: Malformed / odd-length UCI strings
        malformed_moves = [
            ("C6.1", "e2e4aa", "6-character UCI string"),
            ("C6.2", "e2e", "3-character UCI string"),
            ("C6.3", "e", "1-character UCI string"),
            ("C6.4", "", "Empty string UCI"),
            ("C6.5", "0000", "Null move 0000"),
            ("C6.6", "z9z9", "Out of board square"),
            ("C6.7", "e2 e4", "Space separated UCI"),
            ("C6.8", "e2/e4", "Slash separated UCI")
        ]
        for sub_id, u_str, desc in malformed_moves:
            pred = "correct=False with 'Invalid move format' or 'Illegal move', no 500"
            res_m = client.post("/api/repertoire/train/attempt", json={
                "repertoire": "white",
                "current_fen": starting_fen,
                "attempted_uci": u_str
            })
            data_m = res_m.json() if res_m.status_code == 200 else {}
            status_m = "PASS" if res_m.status_code == 200 and data_m.get("correct") is False else "UNHANDLED_500" if res_m.status_code == 500 else "BEHAVIOR_MISMATCH"
            self.log_probe(sub_id, f"Malformed UCI: {desc}", pred, f"attempted_uci='{u_str}'", f"Status {res_m.status_code}, correct={data_m.get('correct')}", status_m)

        # Probe C7: Schema null/invalid fields in /train/attempt
        # C7.1: null attempted_uci
        pred = "HTTP 422 Unprocessable Entity (missing/null string)"
        res_c71 = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": starting_fen,
            "attempted_uci": None
        })
        status_c71 = "PASS" if res_c71.status_code == 422 else "BEHAVIOR_MISMATCH"
        self.log_probe("C7.1", "Null attempted_uci in attempt payload", pred, "attempted_uci=null", f"Status {res_c71.status_code}", status_c71)

        # C7.2: null current_fen
        pred = "HTTP 422 Unprocessable Entity (missing/null string)"
        res_c72 = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": None,
            "attempted_uci": "e2e4"
        })
        status_c72 = "PASS" if res_c72.status_code == 422 else "BEHAVIOR_MISMATCH"
        self.log_probe("C7.2", "Null current_fen in attempt payload", pred, "current_fen=null", f"Status {res_c72.status_code}", status_c72)

        # C7.3: invalid repertoire color
        pred = "HTTP 422 Unprocessable Entity (Literal['white', 'black'])"
        res_c73 = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "red",
            "current_fen": starting_fen,
            "attempted_uci": "e2e4"
        })
        status_c73 = "PASS" if res_c73.status_code == 422 else "BEHAVIOR_MISMATCH"
        self.log_probe("C7.3", "Invalid repertoire 'red' in attempt payload", pred, "repertoire='red'", f"Status {res_c73.status_code}", status_c73)

    # =========================================================================
    # CATEGORY D: Race Conditions, Spaced Repetition Math, & Overflow
    # =========================================================================
    def run_category_d(self):
        print("\n" + "="*80)
        print(" CATEGORY D: SPACED REPETITION MATH, EXPONENTIAL OVERFLOW, & CONCURRENCY")
        print("="*80)

        self.reset_repertoire()
        rm.seed_default_repertoires()
        starting_fen = chess.STARTING_FEN

        # Probe D1: Exponential Interval Growth & SQLite Integer Overflow (Vulnerability Probe!)
        pred = "Interval clamped to safe maximum (e.g. 3650.0 days) so next_review_ts never overflows signed 64-bit int; NO 500"
        overflow_triggered = False
        last_error = ""
        last_interval = 0
        for i in range(1, 65):
            res_rep = client.post("/api/repertoire/train/attempt", json={
                "repertoire": "white",
                "current_fen": starting_fen,
                "attempted_uci": "e2e4"
            })
            if res_rep.status_code != 200:
                overflow_triggered = True
                last_error = f"Step {i}: Status {res_rep.status_code} - {res_rep.text}"
                break
            else:
                last_interval = res_rep.json().get("times_correct", i)

        if overflow_triggered:
            status = "VULNERABILITY"
            details = f"CRITICAL CRASH: Uncapped exponential interval growth (new_interval = round(prev_interval * 2.2, 1)) caused integer overflow in SQLite INTEGER storage (OverflowError / 500). Error: {last_error}"
        else:
            # Check interval value in DB
            conn = get_db()
            row = conn.execute("SELECT interval_days, next_review FROM repertoire_moves WHERE move_uci='e2e4'").fetchone()
            conn.close()
            status = "PASS"
            details = f"64 reviews completed. Final interval: {row['interval_days']}, next_review: {row['next_review']}"
        self.log_probe("D1", "Exponential Spaced Repetition Overflow", pred, "65 consecutive correct reviews of 1. e4", f"Overflow={overflow_triggered}", status, details)

        # Probe D2: Rapid alternating correct / incorrect submissions
        pred = "times_practiced monotonically increases; interval resets to 1.0 on incorrect"
        res_wrong = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": starting_fen,
            "attempted_uci": "d2d4" # wrong move
        })
        conn = get_db()
        r_after = conn.execute("SELECT times_practiced, interval_days FROM repertoire_moves WHERE move_uci='e2e4'").fetchone()
        conn.close()
        status_d2 = "PASS" if r_after and r_after["interval_days"] == 1.0 else "BEHAVIOR_MISMATCH"
        self.log_probe("D2", "Interval reset on blunder/mistake", pred, "POST wrong move d2d4", f"interval_days={r_after['interval_days'] if r_after else None}", status_d2)

        # Probe D3: Negative intervals or tampered values in DB
        conn = get_db()
        conn.execute("UPDATE repertoire_moves SET interval_days = -999.0, times_practiced = -5 WHERE move_uci = 'e2e4'")
        conn.commit()
        conn.close()
        pred = "evaluate_trainer_move clamps negative interval to >= 1.0, times_practiced handles gracefully"
        res_neg = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": starting_fen,
            "attempted_uci": "e2e4"
        })
        conn = get_db()
        r_clamped = conn.execute("SELECT interval_days, times_practiced FROM repertoire_moves WHERE move_uci='e2e4'").fetchone()
        conn.close()
        status_d3 = "PASS" if res_neg.status_code == 200 and r_clamped["interval_days"] >= 1.0 else "BEHAVIOR_MISMATCH"
        self.log_probe("D3", "Negative interval in DB handling", pred, "tampered interval_days=-999.0 in DB", f"new interval_days={r_clamped['interval_days'] if r_clamped else None}", status_d3)

        # Probe D4: Corrupt times_correct > times_practiced in DB
        conn = get_db()
        conn.execute("UPDATE repertoire_moves SET times_correct = 50, times_practiced = 2 WHERE move_uci = 'e2e4'")
        conn.commit()
        conn.close()
        pred = "get_trainer_progress handles without ZeroDivisionError or negative counts"
        res_prog = client.get("/api/repertoire/train/progress?color=white")
        data_prog = res_prog.json() if res_prog.status_code == 200 else {}
        status_d4 = "PASS" if res_prog.status_code == 200 and data_prog.get("unlearned_count", 0) >= 0 else "BEHAVIOR_MISMATCH"
        self.log_probe("D4", "Corrupt times_correct > times_practiced", pred, "times_correct=50, times_practiced=2", f"Status {res_prog.status_code}, unlearned={data_prog.get('unlearned_count')}", status_d4)

        # Probe D5: Concurrent rapid submissions
        pred = "10 concurrent requests handled safely without SQLite database locks (WAL mode) or 500 errors"
        def send_attempt():
            return client.post("/api/repertoire/train/attempt", json={
                "repertoire": "white",
                "current_fen": starting_fen,
                "attempted_uci": "e2e4"
            }).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(send_attempt) for _ in range(10)]
            codes = [f.result() for f in futures]

        status_d5 = "PASS" if all(c == 200 for c in codes) else "UNHANDLED_500" if any(c == 500 for c in codes) else "BEHAVIOR_MISMATCH"
        self.log_probe("D5", "Concurrent rapid submissions (10 threads)", pred, "10 concurrent POST /attempt", f"Result codes: {codes}", status_d5)

        # Probe D6: times_practiced near 64-bit integer limit
        conn = get_db()
        conn.execute("UPDATE repertoire_moves SET times_practiced = 9223372036854775806 WHERE move_uci = 'e2e4'")
        conn.commit()
        conn.close()
        pred = "Gracefully increments or bounds checks without crashing"
        res_d6 = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": starting_fen,
            "attempted_uci": "e2e4"
        })
        status_d6 = "PASS" if res_d6.status_code == 200 else "UNHANDLED_500" if res_d6.status_code == 500 else "BEHAVIOR_MISMATCH"
        self.log_probe("D6", "times_practiced near 64-bit max", pred, "times_practiced=9223372036854775806", f"Status {res_d6.status_code}", status_d6)
        gc.collect()

    # =========================================================================
    # CATEGORY E: Study Filtering, Deletions, & Cross-Repertoire Leakage
    # =========================================================================
    def run_category_e(self):
        print("\n" + "="*80)
        print(" CATEGORY E: STUDY FILTERING, DELETIONS, & CROSS-REPERTOIRE LEAKAGE")
        print("="*80)

        self.reset_repertoire()
        rm.seed_default_repertoires()

        # Probe E1: Study deletion with delete_moves=True & shared moves
        # Create Study A (Italian line) and Study B (Scotch line, both starting with e2e4)
        rm.create_study("Study A", "white")
        rm.create_study("Study B", "white")
        # e2e4 is added to Study A and Study B
        conn = get_db()
        st_a = conn.execute("SELECT id FROM repertoire_studies WHERE name='Study A'").fetchone()["id"]
        st_b = conn.execute("SELECT id FROM repertoire_studies WHERE name='Study B'").fetchone()["id"]
        norm_start = chess.STARTING_FEN.split()
        norm_start_fen = " ".join(norm_start[:4])
        conn.execute("INSERT OR REPLACE INTO study_moves VALUES (?, 'white', ?, 'e2e4')", (st_a, norm_start_fen))
        conn.execute("INSERT OR REPLACE INTO study_moves VALUES (?, 'white', ?, 'e2e4')", (st_b, norm_start_fen))
        conn.commit()
        conn.close()

        # Delete Study A with delete_moves=True
        pred = "Study A deleted; study_moves cleaned up"
        del_res = client.delete(f"/api/repertoire/studies/{st_a}?delete_moves=true")
        conn = get_db()
        sm_rows = conn.execute("SELECT * FROM study_moves WHERE study_id = ?", (st_a,)).fetchall()
        conn.close()
        status_e1 = "PASS" if del_res.status_code == 200 and len(sm_rows) == 0 else "BEHAVIOR_MISMATCH"
        self.log_probe("E1", "Study deletion with delete_moves=True", pred, "DELETE Study A", f"Status {del_res.status_code}, orphaned study_moves={len(sm_rows)}", status_e1)

        # Probe E2: Study deletion with delete_moves=False
        pred = "Study B deleted; moves in repertoire_moves remain intact"
        del_b = client.delete(f"/api/repertoire/studies/{st_b}?delete_moves=false")
        conn = get_db()
        m_b = conn.execute("SELECT COUNT(*) as cnt FROM repertoire_moves WHERE move_uci = 'e2e4'").fetchone()["cnt"]
        conn.close()
        status_e2 = "PASS" if del_b.status_code == 200 and m_b > 0 else "BEHAVIOR_MISMATCH"
        self.log_probe("E2", "Study deletion with delete_moves=False", pred, "DELETE Study B", f"Status {del_b.status_code}, moves count={m_b}", status_e2)

        # Probe E3: Cross-repertoire study leakage
        # Create Black study "Black Dragon Defense"
        rm.create_study("Black Dragon Defense", "black")
        pred = "Requesting Black study while color=white returns HTTP 404 or 0-move stats; NEVER leaks Black moves as White moves"
        res_leak_next = client.get("/api/repertoire/train/next?color=white&opening=Black Dragon Defense")
        res_leak_prog = client.get("/api/repertoire/train/progress?color=white&study=Black Dragon Defense")
        
        leak_prog_data = res_leak_prog.json() if res_leak_prog.status_code == 200 else {}
        total_leak_moves = leak_prog_data.get("total_moves", -1)
        
        status_e3 = "PASS" if res_leak_next.status_code == 404 and total_leak_moves == 0 else "BEHAVIOR_MISMATCH"
        self.log_probe("E3", "Cross-repertoire study leakage", pred, "color=white with Black Dragon Defense", f"/next={res_leak_next.status_code}, total_moves={total_leak_moves}", status_e3)

        # Probe E4: Non-existent study names
        pred = "/train/next returns 404, /train/progress returns 200 with total_moves=0"
        res_non_next = client.get("/api/repertoire/train/next?color=white&opening=NonExistentStudyXYZ")
        res_non_prog = client.get("/api/repertoire/train/progress?color=white&study=NonExistentStudyXYZ")
        prog_data = res_non_prog.json() if res_non_prog.status_code == 200 else {}
        status_e4 = "PASS" if res_non_next.status_code == 404 and prog_data.get("total_moves") == 0 else "BEHAVIOR_MISMATCH"
        self.log_probe("E4", "Non-existent study query", pred, "opening=NonExistentStudyXYZ", f"/next={res_non_next.status_code}, /prog total_moves={prog_data.get('total_moves')}", status_e4)

    # =========================================================================
    # CATEGORY F: Repertoire Progress Calculations with Edge Cases
    # =========================================================================
    def run_category_f(self):
        print("\n" + "="*80)
        print(" CATEGORY F: REPERTOIRE PROGRESS EDGE CASES & MULTI-STUDY SHARING")
        print("="*80)

        # Probe F1: Repertoire progress with 0 moves
        self.reset_repertoire()
        pred = "Empty repertoire: total_moves=0, mastered_pct=0.0, learning_pct=0.0, unlearned_pct=0.0 (or consistent); NO division by zero"
        res_empty = client.get("/api/repertoire/train/progress?color=white")
        data_empty = res_empty.json() if res_empty.status_code == 200 else {}
        
        # Check unlearned_pct: is it 100.0% despite 0 moves?
        tot = data_empty.get("total_moves")
        unl_pct = data_empty.get("unlearned_pct")
        if tot == 0 and unl_pct == 100.0:
            status_f1 = "BEHAVIOR_MISMATCH"
            details = f"LOGICAL CONTRADICTION: When total_moves is 0, backend reports unlearned_pct=100.0% (line 1955: '100.0 if total == 0 else 0.0'). A repertoire with 0 moves should not report 100% unlearned progress!"
        elif tot == 0 and unl_pct == 0.0:
            status_f1 = "PASS"
            details = "Correctly reports 0.0% across all metrics when total_moves=0"
        else:
            status_f1 = "BEHAVIOR_MISMATCH"
            details = f"Unexpected values: tot={tot}, unl_pct={unl_pct}"
        self.log_probe("F1", "Progress calculation with 0 moves", pred, "GET /train/progress on empty repertoire", f"tot={tot}, unl_pct={unl_pct}%", status_f1, details)

        # Probe F2: Multi-study move sharing deduplication bug (Data Discard Probe!)
        # Create Study 1 and Study 2 for White
        rm.create_study("Study Alpha", "white")
        rm.create_study("Study Beta", "white")
        
        # Add 1. e4 as user move
        rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True, study_name="Study Alpha")
        
        # Associate 1. e4 with Study Beta as well via study_moves
        conn = get_db()
        st_alpha = conn.execute("SELECT id FROM repertoire_studies WHERE name='Study Alpha'").fetchone()["id"]
        st_beta = conn.execute("SELECT id FROM repertoire_studies WHERE name='Study Beta'").fetchone()["id"]
        norm_start_fen = " ".join(chess.STARTING_FEN.split()[:4])
        conn.execute("INSERT OR REPLACE INTO study_moves VALUES (?, 'white', ?, 'e2e4')", (st_alpha, norm_start_fen))
        conn.execute("INSERT OR REPLACE INTO study_moves VALUES (?, 'white', ?, 'e2e4')", (st_beta, norm_start_fen))
        conn.commit()
        conn.close()

        pred = "Both Study Alpha and Study Beta must show 1 user move in their progress breakdown"
        res_multi = client.get("/api/repertoire/train/progress?color=white")
        data_multi = res_multi.json() if res_multi.status_code == 200 else {}
        studies_list = data_multi.get("studies", [])
        alpha_moves = next((s["total_moves"] for s in studies_list if s["name"] == "Study Alpha"), 0)
        beta_moves = next((s["total_moves"] for s in studies_list if s["name"] == "Study Beta"), 0)

        # Also check direct study query:
        res_beta_direct = client.get("/api/repertoire/train/progress?color=white&study=Study Beta")
        beta_direct_moves = res_beta_direct.json().get("total_moves", 0) if res_beta_direct.status_code == 200 else 0

        if beta_moves == 0 or beta_direct_moves == 0:
            status_f2 = "DATA_CORRUPTION"
            details = f"DATA LOSS / STATE CORRUPTION: In get_trainer_progress (repertoire.py lines 1918-1925), user moves are deduplicated by (parent_fen, move_uci) keeping ONLY the first study row! Study Beta's association with 1. e4 was completely discarded: Alpha={alpha_moves}, Beta in list={beta_moves}, Beta direct={beta_direct_moves}."
        else:
            status_f2 = "PASS"
            details = f"Both studies correctly reflect shared move: Alpha={alpha_moves}, Beta={beta_moves}"
        self.log_probe("F2", "Multi-Study Move Sharing Deduplication", pred, "Associate 1. e4 with Study Alpha & Study Beta", f"Alpha={alpha_moves}, Beta={beta_moves}, BetaDirect={beta_direct_moves}", status_f2, details)

    def print_summary(self):
        print("\n" + "="*80)
        print(" ATTACK PROBE SUITE EXECUTION SUMMARY")
        print("="*80)
        total = len(self.results)
        passes = sum(1 for r in self.results if r["status"] == "PASS")
        fails = len(self.vulnerabilities)

        print(f"Total Probes Executed: {total}")
        print(f"Passed:                {passes}")
        print(f"Vulnerabilities/Fails: {fails}")

        if self.vulnerabilities:
            print("\n🚨 VULNERABILITIES & BUGS IDENTIFIED:")
            for v in self.vulnerabilities:
                print(f"\n[{v['probe_id']}] {v['name']} ({v['status']})")
                print(f"  Action:   {v['action']}")
                print(f"  Outcome:  {v['result']}")
                print(f"  Details:  {v['details']}")
        else:
            print("\n✨ All probes passed! No vulnerabilities detected.")

if __name__ == "__main__":
    runner = AttackProbeRunner()
    runner.run_category_a()
    runner.run_category_b()
    runner.run_category_c()
    runner.run_category_d()
    runner.run_category_e()
    runner.run_category_f()
    runner.print_summary()
