"""FIXER-L ablation proof for the ABUSE-03 origin boundary.

The orchestrator's objection (correctly) was that a 403 produced by the *Host*
rule looks identical, from the outside, to a 403 produced by the *Origin* rule
-- so a green suite can be green for the wrong reason. This script removes one
defence at a time from the live `backend/api.py` module (module-level functions
are swapped at runtime; the file on disk is never touched) and asserts that
`tests/test_api_security.py` **notices**. A check that survives the removal of
the defence it claims to cover is reported as a gap.

It closes with a direct side-by-side contrast against the pre-fix snapshot
module, so the original ABUSE-03 claim (an arbitrary `Origin` is reflected with
credentials) is re-measured rather than assumed.

Ablations
---------
  A. remove ONLY the Origin check   (Host / Sec-Fetch-Site / preflight kept)
  B. remove ONLY the Host check     (Origin check kept)
  C. remove ONLY the Sec-Fetch-Site check
  D. pre-fix snapshot vs patched module: who reflects a foreign Origin?

Control checks (positive: they must pass whenever the boundary is intact, and
are therefore NOT expected to notice an ablation) are listed separately so a
mechanism-agnostic property test is never mistaken for a coverage gap.

Usage:
    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-L_ablation.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tests.test_setup  # noqa: E402  (must follow the sys.path insert)

import tests.test_api_security as checks  # noqa: E402
import backend.api as api  # noqa: E402

#: Checks whose entire claim is "a cross-origin request is refused".
ORIGIN_REFUSAL_CHECKS = [
    "check_hostile_origin_cannot_mutate",
    "check_cross_origin_preflight_is_refused",
    "check_mutating_request_without_origin_is_allowed",
    "check_hostile_origin_refused_because_of_the_origin",
]
#: Mechanism-agnostic property: `Access-Control-Allow-Origin` is never emitted
#: for a foreign origin. It holds even with the Origin check disabled, because
#: the module has no reflection path at all -- that is a *stronger* property,
#: not a gap.
PROPERTY_REFUSAL_CHECKS = [
    "check_no_cors_reflection_on_any_route",
    "check_fetch_metadata_blocks_cross_site",
]
HOST_CHECKS = ["check_dns_rebinding_is_blunted"]
#: Positive checks. They must pass with the boundary intact; they are not
#: expected to notice an ablation and are excluded from the gap accounting.
POSITIVE_CHECKS = [
    "check_local_origin_still_works",
    "check_security_headers_present",
]

failures = []


def run(names, verbose=False):
    """Return the names that still PASSED. A name in the result = a coverage gap."""
    by_name = {c.__name__: c for c in checks.CHECKS}
    survived = []
    for name in names:
        try:
            checks.purge_markers()
            by_name[name](api)
        except Exception as exc:
            if verbose:
                print(f"        (noticed)  {name}: {type(exc).__name__}: {str(exc)[:150]}")
        else:
            survived.append(name)
        finally:
            checks.purge_markers()
    return survived


def report(label, expected, extra=()):
    """`expected` = the checks that MUST notice. Anything else surviving is a gap."""
    survived = run(list(expected) + list(extra))
    missed = [n for n in survived if n in expected]
    if missed:
        failures.append(f"{label}: {', '.join(missed)}")
        print(f"[GAP ] {label}: these still passed -> {', '.join(missed)}")
    else:
        print(f"[ok  ] {label}: all {len(expected)} expected check(s) noticed")
    for name in survived:
        if name in extra:
            print(f"       (property/positive, unaffected by design) {name}")


def _load_prefix():
    path = os.path.join(HERE, "api_prefix_snapshot_FIXER-L.py")
    spec = importlib.util.spec_from_file_location("api_prefix_snapshot_FIXER_L", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main():
    # ---------------------------------------------------------------- A ---- #
    original_origin = api.is_allowed_local_origin
    api.is_allowed_local_origin = lambda origin, host_header="": True
    try:
        report("A. origin check removed, host/sec-fetch/preflight kept",
               ORIGIN_REFUSAL_CHECKS, extra=PROPERTY_REFUSAL_CHECKS + POSITIVE_CHECKS)
    finally:
        api.is_allowed_local_origin = original_origin

    # ---------------------------------------------------------------- B ---- #
    original_host = api.is_allowed_request_host
    api.is_allowed_request_host = lambda host_header: True
    try:
        survived = run(HOST_CHECKS + ORIGIN_REFUSAL_CHECKS)
        if "check_dns_rebinding_is_blunted" in survived:
            failures.append("B: check_dns_rebinding_is_blunted did not notice")
            print("[GAP ] B: check_dns_rebinding_is_blunted still passed")
        else:
            print("[ok  ] B. host check removed -> check_dns_rebinding_is_blunted noticed")
        collateral = [n for n in survived if n in ORIGIN_REFUSAL_CHECKS]
        if collateral:
            print(f"       (note) origin checks that depend on the host rule too: {collateral}")
    finally:
        api.is_allowed_request_host = original_host

    # ---------------------------------------------------------------- C ---- #
    original_sites = api.ALLOWED_FETCH_SITES
    api.ALLOWED_FETCH_SITES = frozenset(
        set(original_sites) | {"cross-site", "same-site", "none", "same-origin"}
    )
    try:
        survived = run(PROPERTY_REFUSAL_CHECKS)
        if "check_fetch_metadata_blocks_cross_site" in survived:
            failures.append("C: check_fetch_metadata_blocks_cross_site did not notice")
            print("[GAP ] C: check_fetch_metadata_blocks_cross_site still passed")
        else:
            print("[ok  ] C. Sec-Fetch-Site check removed -> the check noticed")
        for name in survived:
            print(f"       (property, unaffected by design) {name}")
    finally:
        api.ALLOWED_FETCH_SITES = original_sites

    # ---------------------------------------------------------------- D ---- #
    print("[....] D. pre-fix snapshot vs patched module: who reflects a foreign Origin?")
    prefix = _load_prefix()
    from fastapi.testclient import TestClient
    probe = {"Host": "127.0.0.1:38281", "Origin": "http://attacker.example"}
    rows = []
    for label, module in (("pre-fix ", prefix), ("patched ", api)):
        client = TestClient(module.app, raise_server_exceptions=False)
        response = client.get("/api/dashboard", headers=probe)
        rows.append((label, response.status_code,
                     response.headers.get("access-control-allow-origin"),
                     response.headers.get("access-control-allow-credentials")))
        del client
    for label, status, acao, acac in rows:
        print(f"       {label} GET /api/dashboard -> {status} "
              f"ACAO={acao!r} ACAC={acac!r}")
    reflected = [r for r in rows if r[2] and r[2] != "null"]
    if len(reflected) == 1 and reflected[0][0].strip() == "pre-fix":
        print("[ok  ] D. only the pre-fix module reflects a foreign Origin")
    else:
        failures.append("D: reflection contrast is not pre-fix-only")
        print("[GAP ] D: unexpected reflection contrast")

    # ---------------------------------------------------------- control ---- #
    print()
    all_names = ORIGIN_REFUSAL_CHECKS + PROPERTY_REFUSAL_CHECKS + HOST_CHECKS + POSITIVE_CHECKS
    survived_control = run(all_names, verbose=True)
    if len(survived_control) != len(all_names):
        broken = [n for n in all_names if n not in survived_control]
        failures.append(f"control: {', '.join(broken)} failed with every defence restored")
        print(f"[FAIL] control: {broken} failed with every defence restored")
    else:
        print(f"[ok  ] control: all {len(all_names)} checks pass with every defence restored")

    print()
    if failures:
        print("ABLATION GAPS FOUND:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("No ablation gaps: each of the Origin, Host and Sec-Fetch-Site rules is "
          "covered by a check that fails when that rule is removed, and the "
          "pre-fix module demonstrably reflected a foreign Origin where the "
          "patched one refuses it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
