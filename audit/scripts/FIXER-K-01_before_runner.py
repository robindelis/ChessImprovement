"""FIXER-K-01 -- run the new regression tests against the PRE-FIX analyzer.

The pre-fix `backend/analyzer.py` is restored verbatim from git (`HEAD`, i.e.
before FIXER-C's pool refactor and before this fix) and installed as
`backend.analyzer` in `sys.modules`.  A small **legacy shim** then supplies the
API names the new tests need, with exactly the pre-fix behaviour:

* `normalize_score`      -> `(pov.score(mate_score=10000), None)` -- the old
  scalar stand-in, mate distance thrown away, which is precisely the bug.
* `describe_score_mate`  -> `""` always (the pre-fix code had no mate field).
* `mate_distance_moves`  -> `0` (no mate concept).
* `MATE_SCORE_CP`        -> 10000, `MAX_CPL_LOSS` -> 10000 (the sanity bounds the
  new tests assert the *code* must respect).
* `centipawn_loss`       -> the old unbounded `before - after`.
* `GameAnalyzer._generate_tactical_explanation` -> the pre-fix body, wrapped so it
  tolerates (and ignores) the new `mate_after` / `mate_before` keywords.
* `GameAnalyzer.analyze_and_store` -> `analyze_game_openings` + `save_blunders_to_db`
  (what `api.py` did by hand).

Everything under test is pre-fix code; the shim only makes the tests *expressible*.

    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-K-01_before_runner.py
"""
import importlib.util
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

import tests.test_setup  # noqa: E402  FIRST backend-adjacent import (temp DB only)

PRE_FIX_COPY = "/tmp/opencode/FIXER-K-01_analyzer_prefix.py"


def load_pre_fix_module():
    source = subprocess.check_output(
        ["git", "-C", ROOT, "show", "HEAD:backend/analyzer.py"]).decode()
    with open(PRE_FIX_COPY, "w") as handle:
        handle.write(source)
    spec = importlib.util.spec_from_file_location("backend.analyzer", PRE_FIX_COPY)
    module = importlib.util.module_from_spec(spec)
    sys.modules["backend.analyzer"] = module
    spec.loader.exec_module(module)
    return module


def install_legacy_shim(module):
    """Give the pre-fix module the new API surface, with pre-fix semantics."""
    import chess.engine

    # The pre-fix copy lives outside the repo, so its STOCKFISH_PATH would
    # resolve to /tmp/bin/stockfish/... .  Point it back at the real binary so
    # the real-engine regression tests actually run (path only, no behaviour).
    # `stockfish_path` is a *default argument*, so the default tuple has to be
    # rewritten as well as the module attribute.
    module.STOCKFISH_PATH = os.path.join(
        ROOT, "bin", "stockfish", "stockfish-linux-x86-64-universal")
    module.GameAnalyzer.__init__.__defaults__ = (module.STOCKFISH_PATH,)

    module.MATE_SCORE_CP = 10000
    module.MAX_CPL_LOSS = 10000

    def normalize_score(score, color=None):
        if color is not None and hasattr(score, "pov"):
            score = score.pov(color)
        return int(score.score(mate_score=10000)), None

    def mate_distance_moves(mate):
        return 0

    def describe_score_mate(mate):
        return ""

    def centipawn_loss(cp_before, cp_after):
        return int(cp_before) - int(cp_after)

    module.normalize_score = normalize_score
    module.mate_distance_moves = mate_distance_moves
    module.describe_score_mate = describe_score_mate
    module.centipawn_loss = centipawn_loss

    original_explanation = module.GameAnalyzer._generate_tactical_explanation

    def legacy_explanation(self, board_before, played_move, best_move, refutation_moves,
                           cpl_loss, opening_name, score_after=0,
                           mate_after=None, mate_before=None):
        # The pre-fix body, with the new mate keywords accepted and ignored.
        return original_explanation(self, board_before, played_move, best_move,
                                    refutation_moves, cpl_loss, opening_name,
                                    score_after=score_after)

    module.GameAnalyzer._generate_tactical_explanation = legacy_explanation

    def analyze_and_store(self, game_dict):
        blunders = self.analyze_game_openings(game_dict)
        self.save_blunders_to_db(blunders)
        return blunders, {"inserted": len(blunders), "updated": 0,
                          "duplicates_removed": 0, "drills_created": 0}

    module.GameAnalyzer.analyze_and_store = analyze_and_store


def main():
    module = load_pre_fix_module()
    install_legacy_shim(module)
    print("=" * 78)
    print("PRE-FIX backend/analyzer.py (git HEAD + legacy shim) loaded from %s"
          % PRE_FIX_COPY)
    print("=" * 78)

    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(["tests.test_analyzer_correctness"])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print()
    print("BEFORE: ran %d tests, failures=%d errors=%d"
          % (result.testsRun, len(result.failures), len(result.errors)))
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
