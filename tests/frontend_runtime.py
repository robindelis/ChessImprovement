"""
Python wrapper around `tests/frontend_harness.js`.

Exposes `run_scenario(js_code: str) -> dict` which loads the **real** shipped
`frontend/board.js` and `frontend/repertoire_builder.js` in Node behind a
minimal DOM shim and executes `js_code` against them.

Audit finding CODE-05-05: the previous timeline-spam tests executed a
hand-written JavaScript mock instead of the application, so they passed with
the frontend deleted. Everything that calls `run_scenario` now exercises
production code read from disk on every run.

Node is not a declared test dependency, so the callers skip cleanly when it is
absent rather than erroring out.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS_JS = os.path.join(PROJECT_ROOT, "tests", "frontend_harness.js")

REQUIRED_FRONTEND_FILES = (
    os.path.join(PROJECT_ROOT, "frontend", "index.html"),
    os.path.join(PROJECT_ROOT, "frontend", "audio.js"),
    os.path.join(PROJECT_ROOT, "frontend", "board.js"),
    os.path.join(PROJECT_ROOT, "frontend", "repertoire_builder.js"),
    os.path.join(PROJECT_ROOT, "frontend", "repertoire_trainer.js"),
    os.path.join(PROJECT_ROOT, "frontend", "scout.js"),
    os.path.join(PROJECT_ROOT, "frontend", "app.js"),
    os.path.join(PROJECT_ROOT, "frontend", "vendor", "chess.min.js"),
)


def node_available() -> bool:
    """True when a Node interpreter is on PATH.

    Deliberately does NOT consider the frontend sources: a missing or emptied
    `frontend/board.js` / `frontend/repertoire_builder.js` must make tests FAIL,
    never silently skip.
    """
    return shutil.which("node") is not None


def missing_frontend_sources():
    """Real frontend files the harness needs that are absent or empty."""
    missing = []
    for path in REQUIRED_FRONTEND_FILES:
        if not os.path.isfile(path):
            missing.append(path)
        elif os.path.getsize(path) == 0:
            missing.append(path + " (empty)")
    return missing


import copy
import functools


@functools.lru_cache(maxsize=128)
def _run_scenario_cached(js_code: str, timeout: int = 60) -> str:
    fd, scenario_path = tempfile.mkstemp(prefix="chess_frontend_scenario_", suffix=".js")
    os.close(fd)
    try:
        with open(scenario_path, "w", encoding="utf-8") as fh:
            fh.write(js_code)
        result = subprocess.run(
            ["node", HARNESS_JS, scenario_path],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=timeout,
        )
    finally:
        try:
            os.unlink(scenario_path)
        except OSError:
            pass

    if result.returncode != 0:
        raise RuntimeError(
            "frontend harness exited %d\nSTDOUT:\n%s\nSTDERR:\n%s"
            % (result.returncode, result.stdout, result.stderr)
        )
    stdout = (result.stdout or "").strip()
    if not stdout:
        raise RuntimeError(
            "frontend harness produced no output\nSTDERR:\n%s" % (result.stderr,)
        )
    return stdout


def run_scenario(js_code: str, timeout: int = 60) -> dict:
    """Execute `js_code` against the real frontend modules and return its JSON result.

    Raises RuntimeError if the harness or the scenario fails, so a broken
    frontend can never masquerade as a passing test.
    """
    if not node_available():
        raise RuntimeError(
            "Node.js is unavailable; cannot exercise the shipped frontend code."
        )
    missing = missing_frontend_sources()
    if missing:
        raise RuntimeError(
            "Real frontend sources missing or empty, refusing to test a mock: %s"
            % (missing,)
        )

    stdout = _run_scenario_cached(js_code, timeout)
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "frontend harness produced non-JSON output: %r (%s)" % (stdout[:2000], exc)
        )

    if isinstance(payload, dict) and payload.get("__error"):
        raise RuntimeError("frontend scenario raised: %s" % payload["__error"])
    return copy.deepcopy(payload)


def frontend_source(path_rel: str) -> str:
    """Read a real frontend source file (used by provenance assertions)."""
    with open(os.path.join(PROJECT_ROOT, path_rel), "r", encoding="utf-8") as fh:
        return fh.read()


def frontend_sha256(path_rel: str) -> str:
    """sha256 of a real frontend source file on disk."""
    with open(os.path.join(PROJECT_ROOT, path_rel), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def harness_provenance(result: dict) -> dict:
    """`{relpath: sha256}` of the sources the harness actually executed."""
    harness = result.get("__harness") or {}
    return {src["filename"]: src["sha256"] for src in harness.get("loadedSources", [])}


if __name__ == "__main__":  # pragma: no cover - manual smoke check
    print(json.dumps(run_scenario("return {ok: true, node: typeof window};"), indent=2))
    sys.exit(0)
