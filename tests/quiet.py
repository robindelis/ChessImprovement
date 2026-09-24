"""
`tests/quiet.py` — narrative-output plumbing for the test suite.

Audit finding **CODE-05-13** (LOW): the discovered suite printed 191 lines, 85 of
which (45%) were narrative `[STEP n: ...]` / `==== SCENARIO n ====` scaffolding
left over from the interactive audit rounds. `unittest` does not capture stdout,
so all of it landed in the same stream as the result summary and buried a real
`AssertionError` traceback — a green suite could be misread as red and vice
versa.

Everything narrative now goes through `narrate()` / `step_log()` / `banner()`,
which are **silent by default**. To see the narrative (useful when a probe
reproduces a bug by hand):

```bash
CHESS_TEST_VERBOSE=1 /home/robin/miniconda3/bin/python3 -m unittest tests.test_user_workflows
```

A default run therefore prints progress dots, then `Ran N tests`, then `OK` or a
traceback — and nothing else.
"""
import os
import sys

#: Truthy values accepted for `CHESS_TEST_VERBOSE`.
_TRUTHY = ("1", "true", "yes", "on")


def verbose() -> bool:
    return (os.environ.get("CHESS_TEST_VERBOSE") or "").strip().lower() in _TRUTHY


def narrate(*args, **kwargs):
    """`print`, but silent unless `CHESS_TEST_VERBOSE` is set."""
    if verbose():
        kwargs.setdefault("file", sys.stdout)
        print(*args, **kwargs)


def step_log(step_num, title, details=""):
    """The audit-round `[STEP n: TITLE] details` line, silenced by default."""
    if verbose():
        print("\n  [STEP %s: %s] %s" % (step_num, str(title).upper(), details))


def banner(text=""):
    """A section separator, silenced by default."""
    if verbose():
        print("\n" + "=" * max(8, len(str(text)) + 4))
        if text:
            print(text)
        print("=" * max(8, len(str(text)) + 4))
