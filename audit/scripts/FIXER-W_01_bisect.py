"""FIXER-W-01 — bisect which module makes `tests/test_api.py` order-dependent.

For every other collected test module M, runs M first and then only the two
order-sensitive tests of `tests/test_api.py`, in ONE process, and reports the
verdict. Hermetic in the sense that it only ever uses the throwaway temp DB
(`import tests.test_setup` is the first import).

Usage: /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-W_01_bisect.py [module ...]
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST

TARGETS = [
    "tests.test_api.TestAPI.test_account_endpoint",
    "tests.test_api.TestAPI.test_sync_malformed_games",
]

MODULES = [
    "tests.test_analyzer",
    "tests.test_analyzer_correctness",
    "tests.test_api_boundary_hardening",
    "tests.test_api_contract",
    "tests.test_api_security",
    "tests.test_blunder_identity",
    "tests.test_builder_frontend_contracts",
    "tests.test_builder_xss_contracts",
    "tests.test_docs_contract",
    "tests.test_electron_shell",
    "tests.test_evaluator_stream",
    "tests.test_frontend_followups",
    "tests.test_isolation_guard",
    "tests.test_layout_contracts",
    "tests.test_line_end_semantics",
    "tests.test_migration_safety",
    "tests.test_openings",
    "tests.test_pedagogical_trainer",
    "tests.test_repertoire",
    "tests.test_repertoire_data_integrity",
    "tests.test_repertoire_sync_robustness",
    "tests.test_repertoire_write_safety",
    "tests.test_schema_integrity",
    "tests.test_scout",
    "tests.test_scout_frontend_contracts",
    "tests.test_scout_pipeline",
    "tests.test_studies",
    "tests.test_timeline_spam",
    "tests.test_trainer_hint_and_line_completion",
    "tests.test_user_behavior_probe",
    "tests.test_user_workflows",
]


def run(names):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for n in names:
        suite.addTests(loader.loadTestsFromName(n))
    buf = io.StringIO()
    res = unittest.TextTestRunner(stream=buf, verbosity=0).run(suite)
    return res


def main(argv):
    modules = argv[1:] or MODULES
    print("%-48s %s" % ("preceding module", "account / sync verdict"))
    print("-" * 90)
    for m in modules:
        try:
            res = run([m] + TARGETS)
            bad = []
            for t, tb in list(res.failures) + list(res.errors):
                bad.append("%s %s" % (t.id().split(".")[-1], tb.strip().splitlines()[-1][:110]))
            print("%-48s %s" % (m, "GREEN" if not bad else "RED"))
            for b in bad:
                print("      %s" % b)
        except Exception as exc:  # a module that cannot even be loaded
            print("%-48s LOAD-ERROR %r" % (m, exc))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
