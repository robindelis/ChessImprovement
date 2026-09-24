"""
FIXER-T helper: run the new regression tests against a PRE-fix snapshot of
`backend/repertoire.py`, proving they fail before the patch and pass after it.

The pre-fix snapshot is `backend/repertoire.py` as FIXER-R2a left it (their
`repertoire.py` was the last edit to this file before this wave), captured at
`/tmp/opencode/fixert/repertoire_prefix.py`.

**Shims.** The pre-fix module has no `RESULT_DRAW` / `RESULT_ABANDONED` /
`MAX_PLAYED_AT_MS` constants and no `probe_explorer_game_count` / `repair_poisoned_played_at`
/ `_upsert_game` / `_game_row_from_upstream` methods, because those are part of the patch. The
runner injects the *missing names only*, each mapping onto the pre-fix behaviour, so the test
module can import and the assertions exercise the pre-fix logic:

| shim | pre-fix behaviour it stands in for |
| --- | --- |
| `RESULT_DRAW` / `RESULT_ABANDONED` | the literal tokens the pre-fix writers never produced |
| `MAX_PLAYED_AT_MS` | no clamp existed |
| `probe_explorer_game_count` | returns `(get_explorer_game_count(...), True)` - i.e. the pre-fix "a failure is a zero" lie |
| `repair_poisoned_played_at` | a no-op, so the "poisoned row is repairable" assertion fails |
| `_upsert_game` | the pre-fix `INSERT OR REPLACE INTO games` statement |
| `_game_row_from_upstream` | returns `None`, so no record can be written by the pre-fix path |

Usage:
    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-T-01_prefix_runner.py \
        tests.test_repertoire_sync_robustness

Never touches chess_improvement.db: `tests.test_setup` points CHESS_DB_PATH at a
throwaway temp database (BRIEF hard rules 1-3). No Lichess call leaves the process: the
tests mock `backend.repertoire.requests.get` and `fetch_user_games`.
"""
import importlib.util
import os
import sqlite3
import sys
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

PREFIX_COPY = os.environ.get(
    "FIXER_T_PREFIX_COPY", "/tmp/opencode/fixert/repertoire_prefix.py"
)

# 1. Load the pre-fix module and register it as `backend.repertoire` BEFORE anything
#    else (including tests.test_setup) imports the real one.
import backend  # noqa: E402

_spec = importlib.util.spec_from_file_location("backend.repertoire", PREFIX_COPY)
_prefix = importlib.util.module_from_spec(_spec)
sys.modules["backend.repertoire"] = _prefix
_spec.loader.exec_module(_prefix)
backend.repertoire = _prefix

# 2. Inject ONLY the names the pre-fix file cannot have (see the module docstring).
if not hasattr(_prefix, "RESULT_DRAW"):
    _prefix.RESULT_DRAW = "draw"
if not hasattr(_prefix, "RESULT_ABANDONED"):
    _prefix.RESULT_ABANDONED = "abandoned"
if not hasattr(_prefix, "MAX_PLAYED_AT_MS"):
    _prefix.MAX_PLAYED_AT_MS = 2 ** 53

if not hasattr(_prefix.RepertoireManager, "probe_explorer_game_count"):
    def _probe(self, fen, ratings="2000,2200,2500"):
        # Pre-fix: a failure and a real zero were indistinguishable, so the count is always
        # reported as "known".
        return (self.get_explorer_game_count(fen, ratings), True)
    _prefix.RepertoireManager.probe_explorer_game_count = _probe

if not hasattr(_prefix.RepertoireManager, "repair_poisoned_played_at"):
    def _repair(self):
        return 0
    _prefix.RepertoireManager.repair_poisoned_played_at = staticmethod(_repair)

if not hasattr(_prefix.RepertoireManager, "_upsert_game"):
    def _upsert(self, cursor, row):
        cursor.execute("""
        INSERT OR REPLACE INTO games (
            id, speed, rated, played_at, white_player, black_player,
            white_rating, black_rating, user_color, result, opening_name, eco, moves_san, analyzed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            row["id"], row.get("speed"), 1, row.get("played_at"), row.get("white_player"),
            row.get("black_player"), row.get("white_rating"), row.get("black_rating"),
            row.get("user_color"), row.get("result"), row.get("opening_name"),
            row.get("eco"), row.get("moves_san"),
        ))
    _prefix.RepertoireManager._upsert_game = staticmethod(_upsert)

if not hasattr(_prefix.RepertoireManager, "_game_row_from_upstream"):
    def _row(self, game, username):
        return None
    _prefix.RepertoireManager._game_row_from_upstream = staticmethod(_row)

# 3. Make the pre-fix module hermetic: the pre-fix explorer has neither a negative cache nor
#    single-flight, so a run against the real explorer.lichess.org would make dozens of outbound
#    calls and take minutes. A stub `requests` object replaces the module reference *on the
#    pre-fix module only*; a test that counts outbound calls still patches
#    `rep_mod.requests.get` on this object, so the counting keeps working.
class _OfflineRequests:
    """Stands in for the `requests` module on the pre-fix module. Never touches the network."""

    class _Resp:
        status_code = 503

        @staticmethod
        def json():
            return {}

    calls = []

    @classmethod
    def get(cls, url, params=None, headers=None, timeout=None, **kw):
        cls.calls.append((url, params))
        return cls._Resp()

    @classmethod
    def post(cls, *a, **kw):
        raise RuntimeError("offline")


_prefix.requests = _OfflineRequests

# 4. Now the isolated temp-DB harness.
import tests.test_setup  # noqa: E402,F401  (FIRST backend-adjacent import by contract)

if __name__ == "__main__":
    targets = sys.argv[1:] or ["tests.test_repertoire_sync_robustness"]
    if targets == ["discover"]:
        suite = unittest.TestLoader().discover("tests", top_level_dir=PROJECT_ROOT)
    else:
        suite = unittest.TestLoader().loadTestsFromNames(targets)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("\npre-fix module:", PREFIX_COPY)
    print("pre-fix failures:", len(result.failures), "errors:", len(result.errors))
    sys.exit(0 if result.wasSuccessful() else 1)
