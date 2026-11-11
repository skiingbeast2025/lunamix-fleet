#!/usr/bin/env python3
"""
Comprehensive Unit & Integration Test Suite for HBU2 Automation Tools:
- simulate_hbu_chat.py
- custom_account_coins.py

Tests every function, data flow, generator, network fallback, CSV parser,
and multi-threaded execution path safely using an isolated local mock server.
"""

import os
import sys
import json
import time
import re
import csv
import threading
import http.server
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import simulate_hbu_chat as sim
import custom_account_coins as coins_tool

# Mock interactive input prompts so automated test suite runs headlessly without blocking
sim.input = lambda prompt="": ""
coins_tool.input = lambda prompt="": ""

TOTAL_TESTS = 0
PASSED_TESTS = 0

def run_test(name, fn):
    global TOTAL_TESTS, PASSED_TESTS
    TOTAL_TESTS += 1
    print(f"\n[TEST {TOTAL_TESTS}] {name}...", end=" ", flush=True)
    try:
        fn()
        PASSED_TESTS += 1
        print("\033[92mPASSED\033[0m")
    except Exception as e:
        print(f"\033[91mFAILED\033[0m: {e}")
        import traceback
        traceback.print_exc()

# ==============================================================================
# 1. Generators & Wordlist Tests
# ==============================================================================

def test_wordlist_loading():
    words = sim.get_wordlist()
    assert len(words) >= 2000, f"Expected >= 2000 words, got {len(words)}"
    assert all(w.isalnum() for w in words[:100]), "Words should be alphanumeric"

def test_username_generation():
    pattern = re.compile(r"^[A-Za-z0-9_]{3,20}$")
    # Test random dictionary names
    for _ in range(30):
        name = sim.generate_username(use_random=True)
        assert pattern.match(name), f"Invalid random username: '{name}'"
        assert 3 <= len(name) <= 20, f"Length out of bounds: {len(name)}"

    # Test prefix names
    for _ in range(20):
        name = sim.generate_username(prefix="Tester", use_random=False)
        assert name.startswith("Tester_"), f"Prefix failed: {name}"
        assert pattern.match(name), f"Invalid prefix username: '{name}'"

def test_password_and_student_id():
    for _ in range(20):
        pwd = sim.generate_password(12)
        assert len(pwd) == 12, f"Password length mismatch: {len(pwd)}"
        stu = sim.generate_student_id()
        assert stu.startswith("STU") and len(stu) == 8, f"Invalid student id: {stu}"

# ==============================================================================
# 2. Settings Management & Auto-Healing Tests
# ==============================================================================

def test_settings_auto_healing():
    tmp_settings = os.path.join(BASE_DIR, "tmp_test_settings.json")
    try:
        # Write unsafe delays below 5.0s
        with open(tmp_settings, "w") as f:
            json.dump({"min_delay_seconds": 2.1, "max_delay_seconds": 3.0}, f)
        
        sim.SETTINGS_FILE = tmp_settings
        loaded = sim.load_settings()
        # Ensure it auto-healed to >= 5.2s
        assert loaded["min_delay_seconds"] >= 5.2, f"Failed to clamp min delay: {loaded['min_delay_seconds']}"
        assert loaded["max_delay_seconds"] >= 5.7, f"Failed to clamp max delay: {loaded['max_delay_seconds']}"
    finally:
        sim.SETTINGS_FILE = os.path.join(BASE_DIR, "hbu_sim_settings.json")
        if os.path.exists(tmp_settings):
            os.remove(tmp_settings)

# ==============================================================================
# 3. CSV Engine Integrity & Crash Prevention Tests
# ==============================================================================

def test_csv_engine_robustness():
    test_csv = os.path.join(BASE_DIR, "tmp_test_accounts.csv")
    if os.path.exists(test_csv):
        os.remove(test_csv)

    try:
        # 1. Append accounts
        acc1 = {"username": "User_Alpha", "password": "Pass1!", "student_id": "STU11111", "coins": 0}
        acc2 = {"username": "User_Beta", "password": "Pass2!", "student_id": "STU22222", "coins": 100}
        sim.append_single_account_to_csv(test_csv, acc1)
        sim.append_single_account_to_csv(test_csv, acc2)

        # 2. Read accounts
        loaded = sim.load_accounts_from_csv(test_csv)
        assert len(loaded) == 2, f"Expected 2 accounts, got {len(loaded)}"
        assert loaded[0]["username"] == "User_Alpha"
        assert loaded[1]["username"] == "User_Beta"

        # 3. Manually inject a dirty line with extra unmapped columns (simulating version drift)
        with open(test_csv, "a", encoding="utf-8") as f:
            f.write("User_Gamma,Pass3!,STU33333,500,2026-11-04 22:00:00,ExtraCol1,ExtraCol2\n")

        # 4. Update CSV coins across all users - must NOT crash with ValueError: None
        updates = {"User_Alpha": 99999, "User_Gamma": 88888}
        success = sim.update_csv_coins(test_csv, updates)
        assert success, "update_csv_coins returned False"

        # Verify update applied cleanly
        reloaded = sim.load_accounts_from_csv(test_csv)
        alpha = next(a for a in reloaded if a["username"] == "User_Alpha")
        gamma = next(a for a in reloaded if a["username"] == "User_Gamma")
        assert alpha["coins"] == "99999", f"Coin update mismatch: {alpha['coins']}"
        assert gamma["coins"] == "88888", f"Coin update mismatch: {gamma['coins']}"

        # 5. Test custom_account_coins.py CSV updater
        coins_tool.BASE_DIR = BASE_DIR
        ok = coins_tool.update_or_append_csv(test_csv, "User_Delta", "Pass4!", 77777)
        assert ok, "update_or_append_csv failed"
        reloaded2 = sim.load_accounts_from_csv(test_csv)
        assert len(reloaded2) == 4, f"Expected 4 accounts after custom append, got {len(reloaded2)}"
        delta = next(a for a in reloaded2 if a["username"] == "User_Delta")
        assert delta["coins"] == "77777"
    finally:
        if os.path.exists(test_csv):
            os.remove(test_csv)

# ==============================================================================
# 4. Local Mock Server for Network & Concurrency Tests
# ==============================================================================

MOCK_PORT = 8919
server_requests = []

class ComprehensiveMockHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
        server_requests.append(("POST", self.path, body))

        if "/rpc/hbu_signup" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"success": true}')
        elif "/send-message" in self.path:
            # Check for simulated mute trigger
            if body.get("message") == "TRIGGER_MUTE":
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "You are muted for 5 minutes."}')
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
        elif "/rpc/hbu_send" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
        elif "/rpc/hbu_spin" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"prize": 100, "coins": 100}')
        elif "/rpc/hbu_set_coins" in self.path:
            auth = self.headers.get("Authorization", "")
            if "anon_test_key" in auth:
                self.send_response(404)
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success": true}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_PATCH(self):
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
        server_requests.append(("PATCH", self.path, body))

        auth = self.headers.get("Authorization", "")
        # Simulate RLS failure if anon key is used
        if "anon_test_key" in auth:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"code": "42501", "message": "permission denied for table accounts"}')
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

    def log_message(self, format, *args):
        pass

mock_server = None

def start_mock_server():
    global mock_server
    mock_server = http.server.HTTPServer(("127.0.0.1", MOCK_PORT), ComprehensiveMockHandler)
    t = threading.Thread(target=mock_server.serve_forever, daemon=True)
    t.start()

def stop_mock_server():
    global mock_server
    if mock_server:
        mock_server.shutdown()

# ==============================================================================
# 5. Network API Tests
# ==============================================================================

def test_api_signup():
    rest_url = f"http://127.0.0.1:{MOCK_PORT}/rest/v1"
    ok, msg = sim.signup_user(rest_url, "test_key", "UnitTester", "Pass123!", "STU99999")
    assert ok, f"Signup failed: {msg}"
    assert any(r[1].endswith("/rpc/hbu_signup") and r[2]["p_username"] == "UnitTester" for r in server_requests)

def test_api_send_chat():
    fn_url = f"http://127.0.0.1:{MOCK_PORT}/functions/v1"
    rest_url = f"http://127.0.0.1:{MOCK_PORT}/rest/v1"
    
    # 1. Normal message
    ok, res = sim.send_chat_message(fn_url, rest_url, "test_key", "UnitTester", "Pass123!", "Hello world!")
    assert ok, f"Chat message send failed: {res}"

    # 2. Muted message detection
    ok2, res2 = sim.send_chat_message(fn_url, rest_url, "test_key", "UnitTester", "Pass123!", "TRIGGER_MUTE")
    assert not ok2, "Expected muted failure"
    assert "MUTED" in res2, f"Expected MUTED error tag, got: {res2}"

# ==============================================================================
# 6. Simulation Option Tests (Parallel Engine)
# ==============================================================================

def test_simulation_option_3_create_only():
    mock_csv = os.path.join(BASE_DIR, "tmp_opt3_test.csv")
    if os.path.exists(mock_csv):
        os.remove(mock_csv)
    try:
        settings = {
            "supabase_rest_url": f"http://127.0.0.1:{MOCK_PORT}/rest/v1",
            "supabase_functions_url": f"http://127.0.0.1:{MOCK_PORT}/functions/v1",
            "supabase_anon_key": "test_key",
            "num_accounts": 3,
            "messages_per_user": 1,
            "min_delay_seconds": 5.2,
            "max_delay_seconds": 5.5,
            "use_random_names": True,
            "name_prefix": "Test",
            "csv_filename": "tmp_opt3_test.csv"
        }
        # Run Option 3: create accounts only
        sim.execute_simulation(settings, create_new=True, send_messages=False)
        accounts = sim.load_accounts_from_csv(mock_csv)
        assert len(accounts) == 3, f"Expected 3 created accounts, got {len(accounts)}"
    finally:
        if os.path.exists(mock_csv):
            os.remove(mock_csv)

def test_simulation_option_1_full_parallel():
    mock_csv = os.path.join(BASE_DIR, "tmp_opt1_test.csv")
    if os.path.exists(mock_csv):
        os.remove(mock_csv)
    try:
        settings = {
            "supabase_rest_url": f"http://127.0.0.1:{MOCK_PORT}/rest/v1",
            "supabase_functions_url": f"http://127.0.0.1:{MOCK_PORT}/functions/v1",
            "supabase_anon_key": "test_key",
            "num_accounts": 2,
            "messages_per_user": 2,
            "min_delay_seconds": 5.2,
            "max_delay_seconds": 5.4,
            "use_random_names": True,
            "name_prefix": "Opt1",
            "csv_filename": "tmp_opt1_test.csv"
        }
        sim.execute_simulation(settings, create_new=True, send_messages=True)
        accounts = sim.load_accounts_from_csv(mock_csv)
        assert len(accounts) == 2, f"Expected 2 accounts, got {len(accounts)}"
    finally:
        if os.path.exists(mock_csv):
            os.remove(mock_csv)

def test_simulation_option_2_existing_parallel():
    mock_csv = os.path.join(BASE_DIR, "tmp_opt2_test.csv")
    if os.path.exists(mock_csv):
        os.remove(mock_csv)
    try:
        # Prepopulate CSV
        sim.append_single_account_to_csv(mock_csv, {"username": "Opt2_A", "password": "P1", "student_id": "S1", "coins": 0})
        sim.append_single_account_to_csv(mock_csv, {"username": "Opt2_B", "password": "P2", "student_id": "S2", "coins": 0})
        
        settings = {
            "supabase_rest_url": f"http://127.0.0.1:{MOCK_PORT}/rest/v1",
            "supabase_functions_url": f"http://127.0.0.1:{MOCK_PORT}/functions/v1",
            "supabase_anon_key": "test_key",
            "num_accounts": 0,
            "messages_per_user": 1,
            "min_delay_seconds": 5.2,
            "max_delay_seconds": 5.4,
            "use_random_names": True,
            "name_prefix": "Opt2",
            "csv_filename": "tmp_opt2_test.csv"
        }
        sim.execute_simulation(settings, create_new=False, send_messages=True)
    finally:
        if os.path.exists(mock_csv):
            os.remove(mock_csv)

# ==============================================================================
# 7. Coin Granting & RLS SQL Fallback Tests
# ==============================================================================

def test_coin_granting_and_sql_export():
    mock_csv = os.path.join(BASE_DIR, "tmp_coins_test.csv")
    sql_out = os.path.join(BASE_DIR, "update_coins.sql")
    if os.path.exists(mock_csv):
        os.remove(mock_csv)
    try:
        sim.append_single_account_to_csv(mock_csv, {"username": "CoinHero", "password": "P1", "student_id": "S1", "coins": 0})
        
        # Test with anon key (triggers RLS simulation)
        settings = {
            "supabase_rest_url": f"http://127.0.0.1:{MOCK_PORT}/rest/v1",
            "supabase_anon_key": "anon_test_key",
            "supabase_service_role_key": "",
            "csv_filename": "tmp_coins_test.csv"
        }
        target = [{"username": "CoinHero", "password": "P1", "coins": 0}]
        
        sim.grant_coins_to_accounts(settings, target, 50000)

        # Ensure update_coins.sql was generated with the query
        assert os.path.isfile(sql_out), "update_coins.sql was not generated!"
        with open(sql_out, "r") as f:
            content = f.read()
        assert "CoinHero" in content, "Target username missing from update_coins.sql"
        assert "50000" in content, "Target coins amount missing from update_coins.sql"
    finally:
        if os.path.exists(mock_csv):
            os.remove(mock_csv)

# ==============================================================================
# 8. Standalone Custom Coins CLI & Mock Tests
# ==============================================================================

def test_standalone_custom_coins_cli():
    # Test --mock
    res = subprocess.run([sys.executable, "custom_coins.py", "--mock"], capture_output=True, text=True)
    assert res.returncode == 0, f"custom_coins.py --mock failed: {res.stderr}"
    assert "Mock Test Passed" in res.stdout, "Mock test output missing confirmation"

    # Test CLI parameters with mock server
    res2 = subprocess.run([
        sys.executable, "custom_coins.py",
        "-u", "DirectCLIUser",
        "-p", "CLIPass123",
        "-c", "60000",
        "--no-csv"
    ], capture_output=True, text=True)
    assert res2.returncode == 0, f"Direct CLI execution failed: {res2.stderr}"

    # Test CLI --spin parameter
    res3 = subprocess.run([
        sys.executable, "custom_coins.py",
        "-u", "DirectSpinUser",
        "-p", "CLIPass123",
        "--spin",
        "--no-csv"
    ], capture_output=True, text=True)
    assert res3.returncode == 0, f"Direct CLI --spin execution failed: {res3.stderr}"

# ==============================================================================
# Main Runner
# ==============================================================================

if __name__ == "__main__":
    start_mock_server()
    print("=" * 65)
    print("      Running Full Test Suite for HBU2 Automation Codebase       ")
    print("=" * 65)

    try:
        run_test("1. Wordlist Dictionary Loader", test_wordlist_loading)
        run_test("2. Username Generator (Random + Prefix)", test_username_generation)
        run_test("3. Password & Student ID Generators", test_password_and_student_id)
        run_test("4. Settings Auto-Healing (5.2s Anti-Mute Guard)", test_settings_auto_healing)
        run_test("5. CSV Engine Robustness (Dirty Columns / Timestamps)", test_csv_engine_robustness)
        run_test("6. Signup RPC Integration (signup_user)", test_api_signup)
        run_test("7. Chat Message Engine (Normal & Mute Detection)", test_api_send_chat)
        run_test("8. Option 3: Account Creation Only", test_simulation_option_3_create_only)
        run_test("9. Option 1: Full Parallel Bot Simulation", test_simulation_option_1_full_parallel)
        run_test("10. Option 2: Parallel Existing Accounts Chat", test_simulation_option_2_existing_parallel)
        run_test("11. Coin Granting & Auto-generation of update_coins.sql", test_coin_granting_and_sql_export)
        run_test("12. Standalone Custom Coins CLI & Mock", test_standalone_custom_coins_cli)

        print("\n" + "=" * 65)
        if PASSED_TESTS == TOTAL_TESTS:
            print(f"\033[92mALL {TOTAL_TESTS}/{TOTAL_TESTS} TESTS PASSED SUCCESSFULLY! ✓\033[0m")
        else:
            print(f"\033[91m{TOTAL_TESTS - PASSED_TESTS} TEST(S) FAILED!\033[0m")
        print("=" * 65)
    finally:
        stop_mock_server()
