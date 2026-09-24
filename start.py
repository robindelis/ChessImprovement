#!/usr/bin/env python3
"""
Chess Opening Improvement Platform - Application Launcher
"""
import os
import sys
import uvicorn
from backend.db import init_db, get_setting, set_setting
from backend.lichess_client import LichessClient

DEFAULT_PORT = 38281

def setup(port: int = DEFAULT_PORT):
    print("=" * 60)
    print("  ♔ Chess Opening Improvement Platform ♚  ")
    print("=" * 60)

    # Initialize SQLite database
    init_db()
    print("✓ SQLite database initialized.")

    # Check local Stockfish engine
    sf_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "bin", "stockfish", "stockfish-linux-x86-64-universal"
    )
    if os.path.exists(sf_path) and os.access(sf_path, os.X_OK):
        print(f"✓ Stockfish 19 UCI engine ready: {sf_path}")
    else:
        print(f"⚠ Stockfish not found at {sf_path}")

    # Check Lichess connection (non-blocking)
    client = LichessClient()
    if client.token:
        def _check_lichess_bg():
            try:
                profile = client.get_account_profile()
                if profile and "username" in profile:
                    username = profile["username"]
                    ratings = [f"{k.capitalize()}: {v['rating']}" for k, v in profile.get("perfs", {}).items() if "rating" in v][:3]
                    print(f"✓ Connected to Lichess: {username} ({', '.join(ratings)})")
                else:
                    print("⚠ Lichess token configured but could not verify profile.")
            except Exception:
                pass
        import threading
        threading.Thread(target=_check_lichess_bg, daemon=True).start()
    else:
        print("ℹ No Lichess token detected. You can add one in Settings or .env.")

    print(f"\nStarting Web Application on http://127.0.0.1:{port}")
    print("Press Ctrl+C to stop.\n")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Start Chess Opening Improvement Server")
    parser.add_argument("--port", "-p", type=int, default=int(os.environ.get("CHESS_PORT", os.environ.get("PORT", DEFAULT_PORT))), help=f"Port to bind to (default: {DEFAULT_PORT})")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    args = parser.parse_args()

    setup(port=args.port)
    uvicorn.run("backend.api:app", host=args.host, port=args.port, log_level="info")
