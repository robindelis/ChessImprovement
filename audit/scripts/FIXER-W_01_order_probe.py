"""FIXER-W-01 — order-dependence probe for `tests/test_api.py`.

Runs an arbitrary ordered list of test modules in ONE process and prints, for
each module, the state of the shared surfaces that `tests/test_api.py`'s
`test_account_endpoint` and `test_sync_malformed_games` depend on:

  * the `settings` table (`lichess_username`, `lichess_token`),
  * whether a module replaced `backend.api.LichessClient`,
  * the `RepertoireManager` / `AsyncEngineEvaluator` singletons.

Usage:
    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-W_01_order_probe.py <module> [<module> ...]

Idempotent and re-runnable. Never touches production: `import tests.test_setup`
is the FIRST import and redirects `CHESS_DB_PATH` at a throwaway temp DB.
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST: isolated temp DB

import backend.db as db_mod
from backend.db import get_db


def settings_state():
    conn = get_db()
    try:
        rows = conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    finally:
        conn.close()
    return {r["key"]: r["value"] for r in rows}


def token_present():
    env = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), ".env")
    if not os.path.exists(env):
        return False
    return "lip_" in open(env, encoding="utf-8").read()


def main(argv):
    modules = argv[1:] or ["tests.test_api"]
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    print("DB          : %s" % db_mod.get_db_path())
    print(".env token  : %s" % token_present())
    print("baseline settings: %s" % settings_state())
    print("")
    for name in modules:
        suite.addTests(loader.loadTestsFromName(name))
        buf = io.StringIO()
        runner = unittest.TextTestRunner(stream=buf, verbosity=0)
        res = runner.run(suite)
        import backend.api as api_mod
        import backend.lichess_client as lc
        print("after %-45s ran=%d fail=%d err=%d  settings_keys=%s  "
              "api.LichessClient=%s  manager=%r  evaluator=%r" % (
                  name, res.testsRun, len(res.failures), len(res.errors),
                  sorted(settings_state()),
                  getattr(api_mod, "LichessClient", None),
                  getattr(api_mod.RepertoireManager, "_instance", "<class>")
                  if hasattr(api_mod, "RepertoireManager") else None,
                  None,
              ))
        suite = unittest.TestSuite()
    print("")
    print("final settings: %s" % settings_state())
    print("backend.api.LichessClient is still: %r" % (api_mod.LichessClient,))
    print("backend.lichess_client.LichessClient is still: %r" % (lc.LichessClient,))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
