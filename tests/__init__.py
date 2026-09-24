"""
`tests/__init__.py` — the tests package.

Importing this package runs `tests.test_setup`, which redirects `CHESS_DB_PATH`
at a throwaway temp database seeded with the synthetic fixture in
`tests/fixtures.py`. Every module in this package is expected to

    import tests.test_setup  # FIRST, before any backend import

as its first backend-adjacent import, and `tests/test_isolation_guard.py`
enforces that statically and dynamically.
"""
from tests.test_setup import setup_test_db

setup_test_db()
