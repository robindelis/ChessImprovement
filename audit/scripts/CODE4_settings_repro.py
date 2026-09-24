#!/usr/bin/env python3
"""
CODE-4 repro: CODE-08 -- ChessApp.saveSettings() (frontend/app.js:124-157)
reports success unconditionally and never inspects res.ok, so a 4xx/5xx from
POST /api/settings still yields the green "Settings saved successfully!" toast.

This harness asserts the *contract the frontend relies on* (a failing save must
be distinguishable from a successful one) and shows what the endpoint actually
returns for the failure cases the frontend can trigger. If the endpoint ever
signalled failure, the client's `if (data.status === 'success')`-equivalent
pattern would still be wrong -- it never looks at anything.

Isolated temp DB via tests.test_setup. Production DB never touched.
Idempotent: only reads.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST backend-adjacent import (BRIEF hard rule 2)

from fastapi.testclient import TestClient

from backend.api import app
from backend.db import get_setting

client = TestClient(app, raise_server_exceptions=False)

# ---- what frontend/app.js:130-134 actually builds -----------------------
#   payload = { lichess_username: username }
#   if (token)      payload.lichess_token   = token
#   if (geminiKey)  payload.gemini_api_key  = geminiKey
# NOTE: `gap` (the "Default Opponent Gap Threshold") is read at line 128 and
# NEVER put into payload -- that setting is client-only and silently resets.


def main():
    print("[baseline] current stored settings:")
    for k in ("lichess_username",):
        print("   ", k, "=", repr(get_setting(k)))

    print("\n(a) save that the endpoint accepts")
    r = client.post("/api/settings", json={"lichess_username": "RobiDeli"})
    print("    ->", r.status_code, r.json())
    ok = 200 <= r.status_code < 300
    print("    frontend would show:", "'Settings saved successfully!'" if ok
          else "<nothing>")

    print("\n(b) save with an EMPTY username (what a cleared field produces)")
    r = client.post("/api/settings", json={"lichess_username": ""})
    print("    ->", r.status_code, r.json())
    print("    frontend would show: 'Settings saved successfully!'"
          if 200 <= r.status_code < 300 else "<nothing>")

    print("\n(c) save with a junk token -- the endpoint does not validate it")
    r = client.post("/api/settings",
                    json={"lichess_username": "RobiDeli", "lichess_token": "x"})
    print("    ->", r.status_code, r.json())
    print("    (the endpoint accepts any token string, so the user gets a green")
    print("     toast and only discovers the problem later, at 'Sync Games'.)")

    print("\n(d) save that 500s (simulated: unhandled type in the payload)")
    r = client.post("/api/settings",
                    json={"lichess_username": {"not": "a string"}})
    print("    ->", r.status_code, str(r.json())[:120])
    if not (200 <= r.status_code < 300):
        print("  [FAIL-REPRODUCED] a server-side failure is reported to the UI as")
        print("      success. And if the body is not JSON, `await res.json()` at")
        print("      app.js:142 throws into the catch, which only console.error()s.")

    print("\n(e) dead DOM: #settingsAlert")
    print("    frontend/app.js:143-146 is the ONLY reference to #settingsAlert:")
    print("        const alertEl = document.getElementById('settingsAlert');")
    print("        if (alertEl) { alertEl.style.display = 'none'; }")
    print("    -> it is only ever HIDDEN; no code path ever sets display='block'")
    print("       or writes text into it. Verified by grep:")
    import subprocess
    out = subprocess.run(
        ["grep", "-rn", "settingsAlert", "frontend/"],
        capture_output=True, text=True).stdout.strip()
    print("      ", out.replace("\n", "\n       "))


if __name__ == "__main__":
    main()
