"""
Adversarial Attacker - Round 2: Creative Exploits & Deep Boundary Probes

Probes:
1. Study Creation & Mutation:
   - G1: 15-thread concurrency race on study creation (same name & color)
   - G2.1-G2.3: Zero-width spaces, BOM, tabs/newlines in study names
   - G3: Casing collisions ("Italian Game" vs "italian game" vs "ITALIAN GAME")
   - G4: Embedded null byte in study name ("Caro\x00Kann")
   - G5: Emojis and RTL Unicode in study names
   - G6.1-G6.2: Deleting study while mid-training and evaluating move (delete_moves=False & True)
2. Spaced Repetition & Intervals:
   - H1.1-H1.4: Corrupt interval_days in DB ('NaN', 'Infinity', -500.0, 'corrupt_string')
   - H2.1-H2.2: Corrupt times_practiced and times_correct in DB (negative, tc > tp)
   - H3: 20-thread concurrency race on the same move attempt
   - H4: Cross-study state manipulation (move in Study A evaluated with study_name="Study B")
3. Mastery Progress Calculation:
   - J1: Progress math with tc > tp (no percentage overflow > 100%)
   - J2.1-J2.3: Single move progression (0% -> 100% unlearned -> 100% learning -> mastery logic)
   - J3: Asymmetric multi-study distribution (0 moves vs 50 moves)
   - J4: 3-way split exact 100.0% sum verification
"""

import sys
import os
import gc
import time
import json
import sqlite3
import threading
import concurrent.futures

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# CODE-05-10: first backend-adjacent import must be `tests.test_setup`.
import tests.test_setup
from tests.probe_guard import require_isolated_db

import tests.test_setup
from backend.db import get_db, init_db
from backend.repertoire import RepertoireManager
from backend.api import app
from fastapi.testclient import TestClient
import chess

client = TestClient(app, raise_server_exceptions=False)
rm = RepertoireManager.get_instance()

class CreativeAttackRunner:
    def __init__(self):
        self.results = []
        self.vulnerabilities = []

    def log(self, probe_id, name, prediction, action, result, status, details=""):
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

        symbol = "❌ [FAIL]" if status != "PASS" else "✅ [PASS]"
        print(f"\n{symbol} {probe_id}: {name}")
        print(f"   PREDICTED: {prediction}")
        print(f"   ACTION:    {action}")
        print(f"   OUTCOME:   {status} - {result}")
        if details:
            print(f"   DETAILS:   {details}")

    def reset_db(self):
        gc.collect()
        conn = get_db()
        conn.execute("DELETE FROM repertoire_moves")
        conn.execute("DELETE FROM repertoire_studies")
        conn.execute("DELETE FROM study_moves")
        conn.commit()
        conn.close()
        gc.collect()

    # =========================================================================
    # SECTION 1: STUDY CREATION & MUTATION CREATIVE PROBES
    # =========================================================================
    def run_section_1_studies(self):
        print("\n" + "="*80)
        print(" SECTION 1: STUDY CREATION & MUTATION CREATIVE PROBES")
        print("="*80)

        # Probe G1: 15-thread concurrency race creating the same study
        self.reset_db()
        study_name = "RaceConditionStudy"
        pred = "1 thread returns 200 'success', 14 return 200 'exists' or 400. Zero 500s, zero SQLite lock crashes."

        def create_study_worker():
            return client.post("/api/repertoire/studies", json={
                "name": study_name,
                "repertoire": "white",
                "description": "Concurrent creation stress test"
            })

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(create_study_worker) for _ in range(15)]
            responses = [f.result() for f in futures]

        status_codes = [r.status_code for r in responses]
        statuses = [r.json().get("status") if r.status_code == 200 else r.status_code for r in responses]
        has_500 = any(c == 500 for c in status_codes)
        success_count = sum(1 for s in statuses if s == "success")
        exists_count = sum(1 for s in statuses if s == "exists")

        status_g1 = "PASS" if not has_500 and (success_count + exists_count == 15) else "VULNERABILITY"
        details_g1 = f"Success={success_count}, Exists={exists_count}, Total={len(status_codes)}, Codes={set(status_codes)}"
        self.log("G1", "15-Thread Concurrent Study Creation Race", pred, "15 simultaneous POST /studies", f"Codes: {set(status_codes)}, 500={has_500}", status_g1, details_g1)

        # Probe G2: Zero-Width Characters & Non-Standard Whitespace
        # G2.1: Zero-Width Space (\u200b)
        pred = "HTTP 400 rejection (invisible name should not create phantom study) OR safe handling without crash"
        res_zws = client.post("/api/repertoire/studies", json={"name": "\u200b", "repertoire": "white"})
        # Check what was returned:
        if res_zws.status_code == 400:
            status_g21 = "PASS"
            det_g21 = "Rejected with HTTP 400"
        elif res_zws.status_code == 200 and res_zws.json().get("status") == "success":
            status_g21 = "BEHAVIOR_MISMATCH"
            det_g21 = "POTENTIAL UX BUG: Zero-width space '\\u200b' accepted as a valid study name! Creates an invisible empty option in the UI study selector."
        else:
            status_g21 = "PASS" if res_zws.status_code != 500 else "UNHANDLED_500"
            det_g21 = f"Status {res_zws.status_code}: {res_zws.text}"
        self.log("G2.1", "Zero-Width Space Study Name (\\u200b)", pred, "name='\\u200b'", f"Status {res_zws.status_code}", status_g21, det_g21)

        # G2.2: BOM (\ufeff)
        res_bom = client.post("/api/repertoire/studies", json={"name": "\ufeff", "repertoire": "white"})
        status_g22 = "PASS" if res_bom.status_code == 400 else "BEHAVIOR_MISMATCH" if res_bom.status_code == 200 else "UNHANDLED_500"
        det_g22 = "BOM '\\ufeff' accepted as study name creating invisible entry" if res_bom.status_code == 200 else f"Status {res_bom.status_code}"
        self.log("G2.2", "Byte Order Mark Study Name (\\ufeff)", pred, "name='\\ufeff'", f"Status {res_bom.status_code}", status_g22, det_g22)

        # G2.3: Whitespace mix (\t\n\r )
        pred = "HTTP 400 (Study name cannot be empty)"
        res_wsmix = client.post("/api/repertoire/studies", json={"name": "\t\n  \r ", "repertoire": "white"})
        status_g23 = "PASS" if res_wsmix.status_code == 400 else "BEHAVIOR_MISMATCH"
        self.log("G2.3", "Tabs, Newlines & Carriage Returns", pred, "name='\\t\\n  \\r '", f"Status {res_wsmix.status_code}", status_g23)

        # Probe G3: Casing Collisions ("Italian Game" vs "italian game" vs "ITALIAN GAME")
        pred = "Handled gracefully without SQLite constraint crash or duplicate clutter"
        self.reset_db()
        r_c1 = client.post("/api/repertoire/studies", json={"name": "Italian Game", "repertoire": "white"})
        r_c2 = client.post("/api/repertoire/studies", json={"name": "italian game", "repertoire": "white"})
        r_c3 = client.post("/api/repertoire/studies", json={"name": "ITALIAN GAME", "repertoire": "white"})
        
        # Check how many studies are returned by get_studies
        studies_list = client.get("/api/repertoire/studies?repertoire=white").json()
        names = [s["name"] for s in studies_list]
        status_g3 = "PASS" if r_c1.status_code == 200 and r_c2.status_code == 200 and r_c3.status_code == 200 else "UNHANDLED_500"
        det_g3 = f"Created distinct casing variations: {names}"
        self.log("G3", "Study Name Casing Variations", pred, "Italian Game vs italian game vs ITALIAN GAME", f"Studies in DB: {len(names)}", status_g3, det_g3)

        # Probe G4: Embedded Null Byte ("Caro\x00Kann")
        pred = "HTTP 400/422 validation failure or safe handling; NO 500 or SQLite null-byte crash"
        res_null = client.post("/api/repertoire/studies", json={"name": "Caro\x00Kann", "repertoire": "white"})
        status_g4 = "PASS" if res_null.status_code in (200, 400, 422) else "UNHANDLED_500"
        self.log("G4", "Embedded Null Byte in Study Name", pred, "name='Caro\\x00Kann'", f"Status {res_null.status_code}", status_g4)

        # Probe G5: Emojis and RTL Unicode
        pred = "UTF-8 preserved accurately without encoding errors or 500s"
        emoji_name = "👑 Sicilian Dragon 🐉"
        rtl_name = "\u202eReversedStudy"
        r_em = client.post("/api/repertoire/studies", json={"name": emoji_name, "repertoire": "black"})
        r_rtl = client.post("/api/repertoire/studies", json={"name": rtl_name, "repertoire": "black"})
        
        # Fetch back
        st_black = client.get("/api/repertoire/studies?repertoire=black").json()
        st_names = [s["name"] for s in st_black]
        status_g5 = "PASS" if emoji_name in st_names and rtl_name in st_names else "BEHAVIOR_MISMATCH"
        self.log("G5", "Emoji & RTL Unicode Study Names", pred, "Unicode study creation", f"Stored names: {st_names}", status_g5)

        # Probe G6: Deleting Study Mid-Training & Evaluating Next Move
        # G6.1: delete_moves=False
        self.reset_db()
        rm.seed_default_repertoires()
        rm.create_study("MidTrainStudy", "white")
        rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True, study_name="MidTrainStudy")
        
        # Delete study while client holds study_name
        conn = get_db()
        st_id = conn.execute("SELECT id FROM repertoire_studies WHERE name='MidTrainStudy'").fetchone()["id"]
        conn.close()
        client.delete(f"/api/repertoire/studies/{st_id}?delete_moves=false")

        # Now client attempts move with study_name="MidTrainStudy"
        pred = "HTTP 200: falls back cleanly to general repertoire moves, correct=True"
        res_mid_eval = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": chess.STARTING_FEN,
            "attempted_uci": "e2e4",
            "study_name": "MidTrainStudy"
        })
        mid_data = res_mid_eval.json() if res_mid_eval.status_code == 200 else {}
        status_g61 = "PASS" if res_mid_eval.status_code == 200 and mid_data.get("correct") is True else "BEHAVIOR_MISMATCH"
        self.log("G6.1", "Move attempt after study deleted (delete_moves=False)", pred, "POST /attempt with deleted study_name", f"Status {res_mid_eval.status_code}, correct={mid_data.get('correct')}", status_g61)

        # G6.2: delete_moves=True
        # Delete Italian Game with delete_moves=True
        conn = get_db()
        it_id = conn.execute("SELECT id FROM repertoire_studies WHERE name='Italian Game'").fetchone()["id"]
        conn.close()
        client.delete(f"/api/repertoire/studies/{it_id}?delete_moves=true")

        # G6.2a: Unique move from Italian Game (3. Bc4: f1c4 after 1. e4 e5 2. Nf3 Nc6)
        fen_italian_unique = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
        pred = "HTTP 200: unique Italian move 3. Bc4 was deleted -> correct=False, is_in_tree=False"
        res_del_unique = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": fen_italian_unique,
            "attempted_uci": "f1c4"
        })
        del_u_data = res_del_unique.json() if res_del_unique.status_code == 200 else {}
        status_g62a = "PASS" if res_del_unique.status_code == 200 and del_u_data.get("correct") is False else "BEHAVIOR_MISMATCH"
        self.log("G6.2a", "Unique move deletion (3. Bc4 deleted with Italian Game)", pred, "POST /attempt with 3. Bc4", f"correct={del_u_data.get('correct')}, msg={del_u_data.get('message')}", status_g62a)

        # G6.2b: Shared root move (1. e4) must remain preserved for other White studies (French & Sicilian)
        pred = "HTTP 200: 1. e4 preserved for other White studies (French Defense/Open Sicilian) -> correct=True"
        res_shared = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": chess.STARTING_FEN,
            "attempted_uci": "e2e4"
        })
        shared_data = res_shared.json() if res_shared.status_code == 200 else {}
        status_g62b = "PASS" if res_shared.status_code == 200 and shared_data.get("correct") is True else "BEHAVIOR_MISMATCH"
        self.log("G6.2b", "Shared root move preservation (1. e4 kept for French/Sicilian)", pred, "POST /attempt with 1. e4", f"correct={shared_data.get('correct')}", status_g62b)

    # =========================================================================
    # SECTION 2: SPACED REPETITION MATH & CORRUPT DB STATE PROBES
    # =========================================================================
    def run_section_2_spaced_rep(self):
        print("\n" + "="*80)
        print(" SECTION 2: SPACED REPETITION MATH & CORRUPT DB STATE PROBES")
        print("="*80)

        self.reset_db()
        rm.seed_default_repertoires()
        starting_fen = chess.STARTING_FEN

        corrupt_intervals = [
            ("H1.1", "NaN", "float('nan') in DB"),
            ("H1.2", "Infinity", "float('inf') in DB"),
            ("H1.3", "-500.0", "Negative float -500.0 in DB"),
            ("H1.4", "corrupt_string_val", "Arbitrary non-numeric string in DB")
        ]

        for sub_id, corrupt_val, desc in corrupt_intervals:
            conn = get_db()
            conn.execute("UPDATE repertoire_moves SET interval_days = ? WHERE move_uci = 'e2e4'", (corrupt_val,))
            conn.commit()
            conn.close()

            pred = "evaluate_trainer_move recovers gracefully: interval normalized to safe float [1.0, 3650.0], NO 500"
            res_c = client.post("/api/repertoire/train/attempt", json={
                "repertoire": "white",
                "current_fen": starting_fen,
                "attempted_uci": "e2e4"
            })
            conn = get_db()
            row_c = conn.execute("SELECT interval_days, next_review FROM repertoire_moves WHERE move_uci = 'e2e4'").fetchone()
            conn.close()

            status_h1 = "PASS" if res_c.status_code == 200 and (1.0 <= float(row_c["interval_days"]) <= 3650.0) else "VULNERABILITY"
            det_h1 = f"DB interval={row_c['interval_days']}, next_review={row_c['next_review']}"
            self.log(sub_id, f"Corrupt interval_days: {desc}", pred, f"interval_days='{corrupt_val}' in DB", f"Status {res_c.status_code}", status_h1, det_h1)

        # Probe H2: Negative times_practiced and times_correct in DB
        conn = get_db()
        conn.execute("UPDATE repertoire_moves SET times_practiced = -10, times_correct = -5 WHERE move_uci = 'e2e4'")
        conn.commit()
        conn.close()

        pred = "Clean handling without crash; verify if negative counts are leaked in API response"
        res_h2 = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": starting_fen,
            "attempted_uci": "e2e4"
        })
        h2_data = res_h2.json() if res_h2.status_code == 200 else {}
        resp_tp = h2_data.get("times_practiced", 0)
        resp_tc = h2_data.get("times_correct", 0)

        # If API returns negative times_practiced, it's a data hygiene issue
        if resp_tp < 0 or resp_tc < 0:
            status_h2 = "BEHAVIOR_MISMATCH"
            det_h2 = f"DATA HYGIENE BUG: Negative counters in DB (-10, -5) were incremented to negative values ({resp_tp}, {resp_tc}) and returned to client without clamping to >= 0."
        else:
            status_h2 = "PASS"
            det_h2 = f"times_practiced={resp_tp}, times_correct={resp_tc}"
        self.log("H2.1", "Negative times_practiced in DB", pred, "times_practiced=-10, times_correct=-5 in DB", f"Returned tp={resp_tp}, tc={resp_tc}", status_h2, det_h2)

        # Probe H2.2: times_correct > times_practiced (e.g. tc=100, tp=5)
        conn = get_db()
        conn.execute("UPDATE repertoire_moves SET times_practiced = 5, times_correct = 100 WHERE move_uci = 'e2e4'")
        conn.commit()
        conn.close()

        pred = "evaluate_trainer_move handles tc > tp gracefully: status='learning', no crash"
        res_h22 = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": starting_fen,
            "attempted_uci": "e2e4"
        })
        h22_data = res_h22.json() if res_h22.status_code == 200 else {}
        status_h22 = "PASS" if res_h22.status_code == 200 and h22_data.get("correct") is True else "BEHAVIOR_MISMATCH"
        self.log("H2.2", "times_correct > times_practiced (tc=100, tp=5)", pred, "tc=100, tp=5 in DB", f"Status {res_h22.status_code}, mastery={h22_data.get('mastery_status')}", status_h22)

        # Probe H3: 20-thread concurrency race on the same move attempt
        self.reset_db()
        rm.seed_default_repertoires()
        pred = "20 concurrent move attempts handled cleanly with 100% 200 OK responses; no SQLite lock crash"

        def attempt_worker():
            return client.post("/api/repertoire/train/attempt", json={
                "repertoire": "white",
                "current_fen": starting_fen,
                "attempted_uci": "e2e4"
            })

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(attempt_worker) for _ in range(20)]
            h3_responses = [f.result() for f in futures]

        h3_codes = [r.status_code for r in h3_responses]
        h3_correct = [r.json().get("correct") for r in h3_responses if r.status_code == 200]
        status_h3 = "PASS" if all(c == 200 for c in h3_codes) and all(h3_correct) else "UNHANDLED_500" if any(c == 500 for c in h3_codes) else "BEHAVIOR_MISMATCH"
        self.log("H3", "20-Thread High Concurrency Move Attempt Race", pred, "20 simultaneous POST /attempt", f"Codes: {set(h3_codes)}, 200s={len(h3_codes)}", status_h3)

        # Probe H4: Cross-Study State Manipulation (Move in Study A evaluated with study_name="Study B")
        # Setup Study A with e2e4 and Study B (empty)
        self.reset_db()
        rm.seed_default_repertoires()
        rm.create_study("Study A", "white")
        rm.create_study("Study B", "white")
        rm.add_repertoire_move("white", starting_fen, "e2e4", is_user_move=True, study_name="Study A")

        pred = "Move evaluation succeeds, opponent reply falls back cleanly; verify returned study_name metadata"
        res_h4 = client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": starting_fen,
            "attempted_uci": "e2e4",
            "study_name": "Study B"
        })
        h4_data = res_h4.json() if res_h4.status_code == 200 else {}
        status_h4 = "PASS" if res_h4.status_code == 200 and h4_data.get("correct") is True else "BEHAVIOR_MISMATCH"
        det_h4 = f"study_name in response: '{h4_data.get('study_name')}', opponent_reply: {bool(h4_data.get('opponent_reply'))}"
        self.log("H4", "Cross-Study State Move Evaluation", pred, "1. e4 (Study A) evaluated with study_name='Study B'", f"Status {res_h4.status_code}", status_h4, det_h4)

    # =========================================================================
    # SECTION 3: MASTERY PROGRESS CALCULATION EXTREMES
    # =========================================================================
    def run_section_3_progress_extremes(self):
        print("\n" + "="*80)
        print(" SECTION 3: MASTERY PROGRESS CALCULATION EXTREMES")
        print("="*80)

        # Probe J1: Progress math with tc > tp (no percentage overflow > 100%)
        self.reset_db()
        rm.create_study("CorruptStudy", "white")
        rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True, study_name="CorruptStudy")
        conn = get_db()
        conn.execute("UPDATE repertoire_moves SET times_practiced = 5, times_correct = 100 WHERE move_uci = 'e2e4'")
        conn.commit()
        conn.close()

        pred = "mastered_pct + learning_pct + unlearned_pct == 100.0%, no individual pct > 100.0%"
        res_j1 = client.get("/api/repertoire/train/progress?color=white")
        j1_data = res_j1.json() if res_j1.status_code == 200 else {}
        m_pct = j1_data.get("mastered_pct", -1)
        l_pct = j1_data.get("learning_pct", -1)
        u_pct = j1_data.get("unlearned_pct", -1)
        total_pct = round(m_pct + l_pct + u_pct, 1)

        status_j1 = "PASS" if res_j1.status_code == 200 and total_pct == 100.0 and max(m_pct, l_pct, u_pct) <= 100.0 else "BEHAVIOR_MISMATCH"
        self.log("J1", "Progress Percentages with tc > tp", pred, "tc=100, tp=5", f"M={m_pct}%, L={l_pct}%, U={u_pct}%, Sum={total_pct}%", status_j1)

        # Probe J2: Single Move Repertoire Progression Logic
        # J2.1: After 1 incorrect attempt
        self.reset_db()
        rm.create_study("SingleMoveStudy", "white")
        rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True, study_name="SingleMoveStudy")
        # Play wrong move
        client.post("/api/repertoire/train/attempt", json={"repertoire": "white", "current_fen": chess.STARTING_FEN, "attempted_uci": "d2d4"})
        res_j21 = client.get("/api/repertoire/train/progress?color=white").json()
        status_j21 = "PASS" if res_j21.get("unlearned_count") == 1 and res_j21.get("unlearned_pct") == 100.0 else "BEHAVIOR_MISMATCH"
        self.log("J2.1", "Single Move After 1 Blunder", "100.0% unlearned", "wrong move d2d4", f"U_pct={res_j21.get('unlearned_pct')}%, U_count={res_j21.get('unlearned_count')}", status_j21)

        # J2.2: After 1 correct attempt
        client.post("/api/repertoire/train/attempt", json={"repertoire": "white", "current_fen": chess.STARTING_FEN, "attempted_uci": "e2e4"})
        res_j22 = client.get("/api/repertoire/train/progress?color=white").json()
        status_j22 = "PASS" if res_j22.get("learning_count") == 1 and res_j22.get("learning_pct") == 100.0 else "BEHAVIOR_MISMATCH"
        self.log("J2.2", "Single Move After 1 Correct Review", "100.0% learning", "correct move e2e4", f"L_pct={res_j22.get('learning_pct')}%, L_count={res_j22.get('learning_count')}", status_j22)

        # J2.3: After 2nd correct attempt (tc=2, tp=3 -> had blunder earlier)
        client.post("/api/repertoire/train/attempt", json={"repertoire": "white", "current_fen": chess.STARTING_FEN, "attempted_uci": "e2e4"})
        res_j23 = client.get("/api/repertoire/train/progress?color=white").json()
        # Chessbook rule: tc >= 2 AND tc == tp. Here tc=2, tp=3 (1 mistake on record). Must REMAIN "learning"!
        status_j23 = "PASS" if res_j23.get("learning_count") == 1 and res_j23.get("mastered_count") == 0 else "BEHAVIOR_MISMATCH"
        det_j23 = f"Mastered={res_j23.get('mastered_count')}, Learning={res_j23.get('learning_count')} (Must not master with blunder on record)"
        self.log("J2.3", "Mastery Strictness with Past Mistake (tc=2, tp=3)", "Remains 'learning'", "tc=2, tp=3", f"Mastered={res_j23.get('mastered_count')}", status_j23, det_j23)

        # Probe J3: Asymmetric Multi-Study Distribution (0 moves vs 50 moves)
        self.reset_db()
        rm.create_study("Empty Study", "white")
        rm.create_study("Active Study", "white")
        # Add 5 moves to Active Study
        b = chess.Board()
        moves_line = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "c2c3", "g8f6", "d2d4", "e5d4"]
        for ply, uci in enumerate(moves_line):
            p_fen = b.fen()
            is_u = (ply % 2 == 0)
            rm.add_repertoire_move("white", p_fen, uci, is_user_move=is_u, study_name="Active Study")
            b.push(chess.Move.from_uci(uci))

        pred = "Empty Study shows total_moves=0 and 0.0% across all metrics; Active Study shows accurate counts; Overall sums to 100%"
        res_j3 = client.get("/api/repertoire/train/progress?color=white")
        j3_data = res_j3.json() if res_j3.status_code == 200 else {}
        studies_j3 = j3_data.get("studies", [])
        empty_st = next((s for s in studies_j3 if s["name"] == "Empty Study"), {})
        active_st = next((s for s in studies_j3 if s["name"] == "Active Study"), {})

        empty_safe = (empty_st.get("total_moves") == 0 and empty_st.get("unlearned_pct") == 0.0 and empty_st.get("mastered_pct") == 0.0)
        active_safe = (active_st.get("total_moves") == 5 and active_st.get("unlearned_count") == 5)
        status_j3 = "PASS" if empty_safe and active_safe else "BEHAVIOR_MISMATCH"
        self.log("J3", "Asymmetric Multi-Study Distribution (0 vs 5 moves)", pred, "Empty Study vs Active Study", f"Empty: tot={empty_st.get('total_moves')}, unl%={empty_st.get('unlearned_pct')} | Active: tot={active_st.get('total_moves')}", status_j3)

        # Probe J4: 3-Way Move Split Percentage Rounding Math
        self.reset_db()
        rm.create_study("SplitStudy", "white")
        # Move 1: Mastered (tc=2, tp=2)
        rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True, study_name="SplitStudy")
        # Move 2: Learning (tc=1, tp=2)
        fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"
        rm.add_repertoire_move("white", fen_after_e4, "e7e5", is_user_move=False, study_name="SplitStudy")
        fen_after_e5 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -"
        rm.add_repertoire_move("white", fen_after_e5, "g1f3", is_user_move=True, study_name="SplitStudy")
        # Move 3: Unlearned (tc=0, tp=1)
        fen_after_nf3 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq -"
        rm.add_repertoire_move("white", fen_after_nf3, "b8c6", is_user_move=False, study_name="SplitStudy")
        fen_after_nc6 = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq -"
        rm.add_repertoire_move("white", fen_after_nc6, "f1c4", is_user_move=True, study_name="SplitStudy")

        conn = get_db()
        conn.execute("UPDATE repertoire_moves SET times_correct = 2, times_practiced = 2 WHERE move_uci = 'e2e4'")
        conn.execute("UPDATE repertoire_moves SET times_correct = 1, times_practiced = 2 WHERE move_uci = 'g1f3'")
        conn.execute("UPDATE repertoire_moves SET times_correct = 0, times_practiced = 1 WHERE move_uci = 'f1c4'")
        conn.commit()
        conn.close()

        pred = "Exactly 3 user moves: 1 mastered, 1 learning, 1 unlearned. Percentages (33.3%, 33.3%, 33.4%) must sum to EXACTLY 100.0%!"
        res_j4 = client.get("/api/repertoire/train/progress?color=white")
        j4_data = res_j4.json() if res_j4.status_code == 200 else {}
        m_4 = j4_data.get("mastered_pct", 0)
        l_4 = j4_data.get("learning_pct", 0)
        u_4 = j4_data.get("unlearned_pct", 0)
        sum_4 = round(m_4 + l_4 + u_4, 1)

        status_j4 = "PASS" if sum_4 == 100.0 and j4_data.get("total_moves") == 3 else "BEHAVIOR_MISMATCH"
        self.log("J4", "3-Way Split Exact 100.0% Sum Verification", pred, "3 moves (1/3 each)", f"M={m_4}%, L={l_4}%, U={u_4}%, Sum={sum_4}%", status_j4)

    def print_summary(self):
        print("\n" + "="*80)
        print(" ROUND 2 CREATIVE PROBE SUITE EXECUTION SUMMARY")
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
    runner = CreativeAttackRunner()
    runner.run_section_1_studies()
    runner.run_section_2_spaced_rep()
    runner.run_section_3_progress_extremes()
    runner.print_summary()
