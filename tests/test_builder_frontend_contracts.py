"""
tests/test_builder_frontend_contracts.py

Regression tests for the frontend contracts owned by FIXER-F
(`frontend/repertoire_builder.js`, `frontend/board.js`, `frontend/index.html`,
`frontend/style.css`).

These are **static source-scanning tests**: there is no DOM in a Python process, so
every assertion here compares a literal selector/id/class/attribute found in the JS or
CSS against `index.html`. That is exactly the class of bug that produced CODE3-01 (an
inline `display:none` that no JS `visibility` toggle could beat) and CODE3-03
(`#blunderBannerAlert` referenced by 200 lines of JS but absent from the HTML).

Behaviour that is genuinely pure logic (arrow validation, the eval-cache LRU, the
write latch, the overlap generation guard) is additionally executed for real by
`audit/scripts/FIXER-F_logic_probe.js`, which is invoked from
`TestPureLogicNodeProbe` when a `node` binary is available.

AUDIT FIXER-W-01 adds `TestCoverageGapKinds` (§4.G): the Builder must render
`gap_kind: "unanswered_branch"` and `gap_kind: "missing_branch"` **distinctly**, and that is
asserted against the REAL shipped `frontend/repertoire_builder.js` executed by
`tests/frontend_harness.js` (the same harness `tests/test_line_end_semantics.py` uses), not
against a static scan.

To prove a check is meaningful, run it against the pre-fix sources:

    FRONTEND_DIR=/path/to/pre-fix/frontend python3 -m unittest tests.test_builder_frontend_contracts
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.environ.get("FRONTEND_DIR") or os.path.join(REPO_ROOT, "frontend")

JS_FILES = ("repertoire_builder.js", "board.js")

import functools


@functools.lru_cache(maxsize=None)
def _read(name):
    with open(os.path.join(FRONTEND_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


def _html():
    return _read("index.html")


def _js(name):
    return _read(name)


def _css():
    return _read("style.css")


def _html_ids():
    return set(re.findall(r'\bid="([^"]+)"', _html()))


def _ids_created_in_js():
    """Ids that the JS creates at runtime, so a literal lookup for them is legitimate."""
    blob = "\n".join(
        _read(f) for f in
        ("repertoire_builder.js", "board.js", "app.js", "scout.js", "repertoire_trainer.js")
        if os.path.exists(os.path.join(FRONTEND_DIR, f))
    )
    created = set(re.findall(r"""\.id\s*=\s*['"]([^'"]+)['"]""", blob))
    created |= set(re.findall(r"""\bid\s*=\s*['"]([^'"]+)['"]""", blob))
    return created


def _literal_lookups(source):
    """Every literal getElementById('X') / querySelector('#X') target in a source file."""
    ids = re.findall(r"""getElementById\(\s*['"]([^'"]+)['"]\s*\)""", source)
    sels = re.findall(r"""querySelector(?:All)?\(\s*['"]#([A-Za-z0-9_-]+)['"]\s*\)""", source)
    return ids + sels


def _tag_with_id(html, element_id):
    """Return the start tag of the element carrying `id`, or None."""
    m = re.search(r"<[^>]*\bid=\"" + re.escape(element_id) + r"\"[^>]*>", html)
    return m.group(0) if m else None


class TestDomSelectorContracts(unittest.TestCase):
    """CODE3-01 / CODE3-03 / CODE3-17: every literal DOM lookup must resolve."""

    def test_every_literal_dom_lookup_resolves_in_index_html(self):
        created = _ids_created_in_js()
        known = _html_ids() | created
        missing = {}
        for fname in JS_FILES:
            source = _js(fname)
            unresolved = sorted({i for i in _literal_lookups(source) if i not in known})
            if unresolved:
                missing[fname] = unresolved
        self.assertEqual(
            missing, {},
            "getElementById/querySelector targets that exist in no element of index.html "
            "and are never created at runtime (this is how CODE3-01 and CODE3-03 became "
            "invisible bugs): %r" % (missing,)
        )

    def test_game_deviations_container_exists_and_is_wired(self):
        """Repertoire Deviations: #trainerDeviationsView exists in trainer and is driven by JS."""
        self.assertIn("trainerDeviationsView", _html_ids(),
                      "index.html must contain the game deviations view container")
        self.assertIn("trainDeviationsBadge", _html_ids(),
                      "index.html must contain the deviations count badge")
        self.assertIn("trainerDeviationsList", _html_ids(),
                      "index.html must contain the deviations list container")

        source = _js("repertoire_trainer.js")
        self.assertIn("loadGameDeviations", source)
        self.assertIn("drillDeviation", source)

    def test_gap_list_containers_exist_and_are_rendered(self):
        """Coverage gaps must be rendered in the Repertoire Builder sidebar."""
        for element_id in (
            "repCoverageGapsList", "repGapsContainer", "repCoverageGapsCount",
            "btnTabRepCoverageGaps", "repGapsHeaderActions", "repCoverageGapsMeta",
        ):
            self.assertIn(element_id, _html_ids(), f"index.html is missing #{element_id}")
        source = _js("repertoire_builder.js")
        self.assertIn("renderGapLists()", source)
        self.assertIn("this.treeData?.gaps", source)
        self.assertIn("this.addressGap(gap)", source,
                      "addressGap() must have a real caller")

    def test_coverage_gaps_tab_is_wired(self):
        """Coverage gaps tab must be reachable and wired in builder."""
        source = _js("repertoire_builder.js")
        self.assertIn("btnTabRepCoverageGaps", source)
        self.assertIn("repCoverageGapsCount", source)

    def test_dead_alias_and_dead_dom_nodes_removed(self):
        """CODE3-17: the zero-caller `showNotice` alias and two dead DOM nodes."""
        self.assertNotRegex(_js("repertoire_builder.js"), r"\n    showNotice\(",
                            "showNotice() is a dead alias with zero callers")
        for dead in ("repQuickAddNotice", "lblMyStatsHeader"):
            self.assertNotIn(dead, _html(), f"#{dead} is a dead DOM node")
            self.assertNotIn(dead, "".join(_js(f) for f in JS_FILES))

    def test_no_unmatchable_selectors_remain(self):
        """CODE3-17: the 8 previously-unmatchable selectors are either bound or removed."""
        source = _js("repertoire_builder.js")
        known = _html_ids() | _ids_created_in_js()
        for gone in ("repOpeningThresholdSelect", "repOpeningThresholdLabel",
                     "repGapThresholdDisplay", "lblMyStatsHeader", "repQuickAddNotice"):
            self.assertNotIn(gone, source, f"'{gone}' lookup should have been removed")
        # The opening-cutoff dropdown documented in AGENTS.md 4.B.3 must now exist and bind.
        self.assertIn("repOpeningThreshold", _html_ids())
        self.assertIn("getElementById('repOpeningThreshold')", source)
        # The full-resync button is a real control again.
        self.assertIn("btnResyncGames", _html_ids())
        self.assertIn("getElementById('btnResyncGames')", source)


class TestZeroClsContracts(unittest.TestCase):
    """AGENTS.md section 8: visibility/opacity + fixed width, not display toggling."""

    def test_conditional_controls_do_not_use_display(self):
        for element_id in ("btnRepDeleteCurrentMove", "btnRepToggleImportant",
                           "syncBlunderPill"):
            tag = _tag_with_id(_html(), element_id)
            self.assertIsNotNone(tag, f"#{element_id} missing from index.html")
            self.assertNotIn("display:none", tag.replace(" ", ""),
                             f"#{element_id} must not be bootstrapped with display:none "
                             f"(AGENTS.md 8.4) - an inline display:none cannot be beaten by "
                             f"a JS style.visibility toggle (CODE3-01)")
        source = _js("repertoire_builder.js")
        for element_id in ("btnRepDeleteCurrentMove", "btnRepToggleImportant"):
            self.assertRegex(
                source, r"getElementById\('%s'\)" % element_id)
        # The reveal path must be visibility + disabled, never display.
        self.assertRegex(source, r"btnDelete\.style\.visibility = repMove \? 'visible' : 'hidden';")
        self.assertRegex(source, r"btnDelete\.disabled = !repMove;")
        self.assertNotRegex(source, r"btnDelete\.style\.display")
        self.assertRegex(source, r"blunderPill\.style\.visibility = 'visible';")
        self.assertRegex(source, r"blunderPill\.style\.visibility = 'hidden';")

    def test_conditional_controls_have_fixed_widths(self):
        css = _css()
        for selector in ("#btnRepDeleteCurrentMove", "#btnRepToggleImportant",
                         "#btnRepTogglePatchedGaps", ".btn-review-game",
                         ".btn-quick-add-opening"):
            m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
            self.assertIsNotNone(m, f"style.css has no rule for {selector}")
            self.assertIn("min-width", m.group(1), f"{selector} needs a fixed min-width (8.4)")

    def test_game_card_add_opening_button_is_restored_after_failure(self):
        """CODE3-11/12: never permanently disabled, latch released in finally."""
        source = _js("repertoire_builder.js")
        m = re.search(r"const btnAdd = item\.querySelector\('\.btn-quick-add-opening'\);"
                      r"([\s\S]*?)\n {20}\}\n", source)
        self.assertIsNotNone(m, "the Add Opening handler must be present")
        block = m.group(1)
        self.assertIn("btnAdd.disabled = false", block,
                      "the button must be re-enabled when the import fails (CODE3-11)")
        self.assertIn("finally", block, "the latch must be released in a finally (CODE3-12)")

    def test_sync_progress_uses_toast_not_in_flow_banner(self):
        """CODE3-10 / AGENDS.md 8.1: no ephemeral in-flow notice in the games list."""
        source = _js("repertoire_builder.js")
        self.assertNotIn("repGamesInlineProgress", source,
                         "the in-flow sync banner is gone; it caused two layout shifts")
        self.assertNotIn("gamesList.prepend(", source)
        self.assertNotIn("repGamesInlineBar", source)
        self.assertIn("this.notify(", source)
        self.assertIn("window.showToast", source)

    def test_eval_bar_label_is_clamped_and_flip_aware(self):
        """CODE3-20 / CODE3-21."""
        css = _css()
        m = re.search(r"\.eval-bar-label\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m)
        for prop in ("white-space: nowrap", "overflow: hidden", "text-overflow: ellipsis"):
            self.assertIn(prop, m.group(1), f".eval-bar-label needs {prop} (8.2)")
        # The label sits on the fill; when the fill flips, the label must flip with it.
        self.assertRegex(css, r"\.eval-bar-wrapper\.flipped \.eval-bar-label\.white-lead\s*\{[^}]*top:")
        self.assertRegex(css, r"\.eval-bar-wrapper\.flipped \.eval-bar-label\.black-lead\s*\{[^}]*bottom:")

    def test_overlap_strip_has_constant_height_and_never_wraps(self):
        """CODE3-20 / AGENTS.md 8.5."""
        m = re.search(r"\.scout-line-overlap-strip\s*\{([^}]*)\}", _css())
        self.assertIsNotNone(m)
        block = m.group(1)
        self.assertIn("flex-wrap: nowrap", block)
        self.assertIn("height: 28px", block)
        self.assertIn("overflow: hidden", block)
        self.assertNotIn("flex-wrap: wrap", block)
        badge = re.search(r"\.scout-overlap-badge\s*\{([^}]*)\}", _css())
        self.assertIn("white-space: nowrap", badge.group(1))

    def test_blunder_banner_has_constant_height(self):
        block = re.search(r"\.blunder-banner-alert\s*\{([^}]*)\}", _css()).group(1)
        self.assertIn("height: 46px", block)
        self.assertIn("min-height: 46px", block)
        self.assertIn("overflow: hidden", block)
        self.assertIn("display: flex", block)

    def test_dynamic_gap_text_is_clamped(self):
        """AGENTS.md 8.2 inside the gap list markup."""
        source = _js("repertoire_builder.js")
        block = re.search(r"\n    _blunderGapCardHtml\([\s\S]*?\n    \}\n", source)
        self.assertIsNotNone(block)
        self.assertIn("white-space:nowrap; overflow:hidden; text-overflow:ellipsis", block.group(0))
        self.assertIn("min-width:110px", block.group(0), "the gap action button needs a fixed width")
        # AUDIT FIXER-W-01: this used to be `re.search(r"btn-address-coverage-gap[\s\S]{0,220}")`,
        # i.e. "the first 220 characters after the first mention of the class". The coverage
        # row's markup was extracted into `_coverageGapCardHtml()`, so the first mention is now
        # the `querySelectorAll('.btn-address-coverage-gap')` wiring and the assertion was
        # measuring a coincidence of file layout. It is now anchored on the *markup* — every
        # `class="… btn-address-coverage-gap …"` attribute must be followed by a fixed button
        # width — which is the property 8.2/8.4 actually cares about, for every occurrence.
        found = 0
        for m in re.finditer(r'class="[^"]*btn-address-coverage-gap[^"]*"', source):
            found += 1
            window = source[m.start():m.start() + 320]
            self.assertIn("min-width:110px", window,
                          "a coverage-gap action button has no fixed width (AGENTS.md 8.4): %r"
                          % window[:160])
        self.assertGreaterEqual(found, 1, "the coverage-gap action button markup is gone")
        # Every dynamic line in the coverage row is clamped, and both kinds share the row.
        cov = re.search(r"\n    _coverageGapCardHtml\([\s\S]*?\n    \}\n", source)
        self.assertIsNotNone(cov, "_coverageGapCardHtml() is missing")
        self.assertGreaterEqual(
            cov.group(0).count("text-overflow:ellipsis"), 2,
            "each line of the coverage row that varies with the data must be clamped (8.2)")
        self.assertIn("title=\"${this._escapeAttr(head)}\"", cov.group(0),
                      "the clamped coverage row needs the untruncated text in its title (8.2)")


class TestSyncAbortContracts(unittest.TestCase):
    """CODE3-04: independent cancel handles for the two sync streams."""

    def test_two_independent_abort_handles(self):
        source = _js("repertoire_builder.js")
        self.assertIn("this.resyncAbortController", source)
        self.assertIn("this.recentSyncAbortController", source)
        self.assertNotIn("this.syncAbortController = new AbortController()", source)

    def test_finally_uses_identity_check(self):
        source = _js("repertoire_builder.js")
        for field in ("resyncAbortController", "recentSyncAbortController"):
            self.assertRegex(
                source,
                r"if \(this\.%s === abortCtrl\) \{\s*this\.%s = null;" % (field, field),
                f"{field} must only be cleared by the stream that owns it")

    def test_cancel_sync_aborts_both(self):
        source = _js("repertoire_builder.js")
        block = re.search(r"cancelSync\(\)\s*\{[\s\S]*?\n    \}\n", source).group(0)
        self.assertIn("this.resyncAbortController.abort()", block)
        self.assertIn("this.recentSyncAbortController.abort()", block)

    def test_recent_games_requests_are_sequenced(self):
        """CODE3-05."""
        source = _js("repertoire_builder.js")
        self.assertIn("this.recentGamesSeq", source)
        self.assertGreaterEqual(source.count("if (seq !== this.recentGamesSeq) return;"), 2)
        self.assertIn("this.recentGamesAbort.abort()", source)
        self.assertIn("this.recentCountAbort.abort()", source)
        # The tab badge must be fetched with the same filters as the list.
        self.assertIn("_recentGamesUrl(1)", source)
        self.assertIn("_recentGamesUrl(40)", source)


class TestRaceGuardContracts(unittest.TestCase):
    """CODE3-06/07/08/12/13: request identity and lifecycle management."""

    def test_candidate_staleness_guard_includes_colour(self):
        source = _js("repertoire_builder.js")
        self.assertRegex(
            source,
            r"const isStale = \(\) => \(\s*this\.activeColor !== reqColor \|\|"
            r"\s*this\.candidateGen !== reqGen\s*\)")
        self.assertRegex(source, r"loadRepertoire\([\s\S]*?candidateAbortController\.abort\(\)")

    def test_overlap_checks_are_sequenced_with_stable_line_ids(self):
        source = _js("repertoire_builder.js")
        self.assertIn("_beginOverlapBatch", source)
        self.assertIn("_isStaleOverlap", source)
        self.assertRegex(source, r"id: String\(l\.leafNode \? l\.leafNode\.id : ''\)")
        self.assertNotRegex(source, r"id: String\(idx\)")
        self.assertIn("signal: abortCtrl.signal", source)

    def test_eval_cache_is_bounded_and_normalised(self):
        source = _js("repertoire_builder.js")
        self.assertIn("this.evalCacheMax = 400", source)
        self.assertIn("_evalCachePut(", source)
        self.assertRegex(source, r"while \(this\.evalCache\.size > max\)")
        self.assertRegex(source, r"const cacheKey = this\.normalizeFen\(fen\);")
        self.assertNotRegex(source, r"this\.evalCache\.set\(fen, data\)")

    def test_write_actions_have_in_flight_latch(self):
        source = _js("repertoire_builder.js")
        self.assertIn("beginWrite(", source)
        self.assertIn("endWrite(", source)
        for fn in ("async saveMove(", "async removeBranch(", "async toggleCurrentMoveImportant(",
                   "async importToRepertoire(", "async importAllLinesToRepertoire("):
            block = re.search(re.escape(fn) + r"[\s\S]*?\n    \}\n", source)
            self.assertIsNotNone(block, fn)
            self.assertIn("beginWrite(", block.group(0), f"{fn} needs an in-flight latch")
            self.assertIn("finally", block.group(0), f"{fn} must release the latch in finally")
        # importGameOpening is latched by its (per-game) click handler instead.
        handler = re.search(r"const btnAdd = item\.querySelector\('\.btn-quick-add-opening'\);"
                            r"[\s\S]*?\n {20}\}\n", source)
        self.assertIsNotNone(handler)
        self.assertIn("beginWrite(", handler.group(0))
        self.assertIn("importGameOpening(g.id, g.user_color)", handler.group(0))
        # ...and the candidate-table buttons latch themselves.
        self.assertIn("withButtonLatch", source)

    def test_prompt_study_selection_is_guarded(self):
        source = _js("repertoire_builder.js")
        calls = len(re.findall(r"await window\.promptStudySelection\(", source))
        guards = len(re.findall(r"typeof window\.promptStudySelection !== 'function'", source))
        self.assertGreaterEqual(guards, calls,
                                "every promptStudySelection call needs a typeof guard (CODE3-09)")


class TestBoardArrowAndAnimationContracts(unittest.TestCase):
    """CODE3-13/14/15/16 + CODE3-02 ordering."""

    def test_arrows_are_validated_at_the_render_boundary(self):
        source = _js("board.js")
        self.assertIn("_isValidArrow(", source)
        self.assertIn("_isValidSquare(", source)
        self.assertRegex(source, r"setArrows\(arrows\)\s*\{[\s\S]*?\.filter\(a => this\._isValidArrow\(a\)\)")
        self.assertRegex(source, r"_getSquareCenterPercent\(sq\)\s*\{[\s\S]*?this\._isValidSquare\(sq\)")

    def test_marker_ids_are_namespaced_per_instance(self):
        source = _js("board.js")
        self.assertIn("this._markerPrefix", source)
        self.assertNotIn("`arrow-marker-${i}`", source)

    def test_cancelled_animations_still_deliver_oncomplete(self):
        source = _js("board.js")
        block = re.search(r"animateMove\(from, to, onComplete = null\)\s*\{[\s\S]*?\n    \}\n", source)
        self.assertIsNotNone(block)
        body = block.group(0)
        self.assertIn("let settled = false;", body)
        self.assertRegex(body, r"anim\.oncancel = \(\) => \{[\s\S]*?finishOnce\(\);")
        self.assertIn("finishOnce", body)

    def test_board_has_a_teardown(self):
        source = _js("board.js")
        self.assertRegex(source, r"destroy\(\)\s*\{[\s\S]*removeEventListener\('pointermove'")
        self.assertRegex(source, r"destroy\(\)\s*\{[\s\S]*removeEventListener\('pointerup'")
        self.assertNotIn("_onSquareClick", source)

    def test_navigation_does_not_defer_loadfen_into_an_animation_callback(self):
        """CODE3-02: model first, arrows last - and never via a deferred callback."""
        source = _js("repertoire_builder.js")
        self.assertNotRegex(source, r"animateMove\([\s\S]{0,300}loadFEN\(")
        block = re.search(r"async updatePosition\([\s\S]*?\n    \}\n", source)
        self.assertIsNotNone(block)
        body = block.group(0)
        self.assertLess(body.index("this.board.loadFEN(curr.fen)"),
                        body.index("this.renderMoveToolsPanel(curr)"))
        jump = re.search(r"jumpToStep\(index\)\s*\{[\s\S]*?\n    \}\n", source)
        self.assertIn("this.updatePosition({ animateFromTo: [from, to] });", jump.group(0))
        self.assertIn("animateGhostMove", _js("board.js"))
        # The counter must be derived from what setArrows actually kept.
        self.assertIn("const kept = this.board.setArrows(parsed);", source)

    def test_overlap_badge_renderer_is_not_duplicated(self):
        """CODE3-18 cluster 1: one canonical renderer, used by both call sites."""
        source = _js("repertoire_builder.js")
        self.assertIn("_overlapBadgesHtml(", source)
        self.assertIn("_importButtonState(", source)

        # Strip the canonical helpers, then assert no second copy of the *label* decision tree
        # survives anywhere else (the two inline copies that had already diverged).
        stripped = source
        for helper in ("_overlapBadgeClass", "_overlapBadgeLabel", "_importButtonState",
                       "_overlapBadgesHtml"):
            m = re.search(r"\n    " + helper + r"\([\s\S]*?\n    \}\n", stripped)
            self.assertIsNotNone(m, helper)
            stripped = stripped.replace(m.group(0), "\n")
        for literal in ("★ White New", "⚡ Black:", "📥 Extend White", "✓ In White (",
                        "★ Black New", "⚡ White:"):
            self.assertNotIn(literal, stripped,
                             f"a second, already-diverged copy of the overlap badge renderer "
                             f"survives: {literal!r}")


class TestErrorSurfacingContracts(unittest.TestCase):
    """CODE3-22: no silent failures, no null dereference on Reset Defaults."""

    def test_reset_defaults_handles_a_null_body(self):
        source = _js("repertoire_builder.js")
        block = re.search(r"async seedDefaults\(\)\s*\{[\s\S]*?\n    \}\n", source)
        self.assertIsNotNone(block)
        body = block.group(0)
        self.assertIn("res.ok", body)
        self.assertRegex(body, r"data && data\.status")
        self.assertNotRegex(body, r"if \(data\.status")
        # AGENTS.md 9.3/9.4: must NOT send force=true (it would overwrite the user's lines).
        self.assertNotIn("/api/repertoire/seed?force=true", source)
        self.assertRegex(source, r"fetch\('/api/repertoire/seed', \{ method: 'POST' \}\)")

    def test_failed_writes_toast_the_user(self):
        source = _js("repertoire_builder.js")
        for fn in ("async saveMove(", "async removeBranch(", "async toggleCurrentMoveImportant(",
                   "async saveMoveComment(", "async onArrowsChange(",
                   "async removeTreeLineFromRepertoire(", "async refreshTree()",
                   "async loadStudies()"):
            block = re.search(re.escape(fn) + r"[\s\S]*?\n    \}\n", source)
            self.assertIsNotNone(block, fn)
            self.assertIn("this.notify(", block.group(0),
                          f"{fn} swallows failures with console.error only (CODE3-22)")

    def test_response_status_is_checked_before_use(self):
        source = _js("repertoire_builder.js")
        body = re.search(r"async refreshTree\(\)\s*\{[\s\S]*?\n    \}\n", source).group(0)
        self.assertIn("if (!res.ok)", body)
        self.assertLess(body.index("if (!res.ok)"), body.index("this.treeData = data"))





class TestCoverageGapKinds(unittest.TestCase):
    """AUDIT FIXER-W-01 / AGENTS.md 4.G — the two coverage-gap kinds must not read alike.

    `get_repertoire_tree` reports two genuinely different problems in one list, tagged by
    `gap_kind` (FIXER-V):

    * `missing_branch`     — a frequent opponent reply the repertoire does **not** hold;
    * `unanswered_branch`  — a reply the repertoire **does** hold but has no prepared answer
      for, so the line would finish on the opponent's move — the state the owner ruled must
      never be presented as a completed line (4.G).

    The Builder used to render both with the same "unprepared opponent moves" wording and the
    same button, so a move the user already had was reported to them as missing. The contract
    is asserted by **running the real shipped `frontend/repertoire_builder.js`** through
    `tests/frontend_harness.js`; the harness is the same one FIXER-V used, and the test proves
    provenance by comparing the SHA-256 the harness executed against the file on disk.
    """

    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node not installed; the real frontend cannot be executed")
        import tests.frontend_runtime as frontend_runtime
        missing = frontend_runtime.missing_frontend_sources()
        if missing:
            raise AssertionError(
                "Real frontend sources missing or empty, refusing to test a mock: %s" % (missing,))
        cls.frontend_runtime = frontend_runtime
        cls.result = frontend_runtime.run_scenario(_SCENARIO)
        cls.rendered = cls.result.get("rendered") or {}

    def test_the_harness_actually_executed_the_real_builder_source(self):
        provenance = self.frontend_runtime.harness_provenance(self.result)
        expected = self.frontend_runtime.frontend_sha256("frontend/repertoire_builder.js")
        self.assertIn("frontend/repertoire_builder.js", provenance,
                      "the harness did not load the shipped builder: %r" % sorted(provenance))
        self.assertEqual(
            provenance["frontend/repertoire_builder.js"], expected,
            "the harness executed a DIFFERENT repertoire_builder.js than the one on disk, so "
            "these results say nothing about the shipped file")

    def test_the_two_kinds_render_differently(self):
        missing = self.rendered.get("missing", "")
        unanswered = self.rendered.get("unanswered", "")
        self.assertTrue(missing and unanswered, "the scenario rendered nothing: %r" % self.rendered)
        self.assertNotEqual(
            missing, unanswered,
            "an `unanswered_branch` and a `missing_branch` render identically, so the user is "
            "told a move they already have is missing")
        # The badge is the part that survives truncation, so it must differ.
        self.assertIn("Not in repertoire", missing)
        self.assertIn("No prepared reply", unanswered)
        # 4.G: the unanswered copy names the fix, in the same terms the Trainer uses.
        self.assertIn("Repertoire Builder", unanswered)
        self.assertNotIn("Repertoire Builder", missing)
        self.assertIn("Add your reply", unanswered)
        self.assertIn("Add this reply", missing)
        # The two kinds are visually distinguishable, not only verbally.
        self.assertIn("gap-item--unanswered", unanswered)
        self.assertIn("gap-item--missing", missing)
        # And the copy must not claim the repertoire is missing a move it holds.
        self.assertIn("You have this reply", unanswered)
        self.assertIn("Not in your repertoire", missing)

    def test_both_kinds_carry_a_clamped_line_and_a_dynamic_title(self):
        """AGENTS.md 8.2: every varying line is clamped and the full text is in `title`."""
        for kind in ("missing", "unanswered"):
            html = self.rendered.get(kind, "")
            self.assertTrue(html, "the %s row is empty" % kind)
            self.assertIn("title=", html, "%s row has no hover title" % kind)
            self.assertGreaterEqual(
                html.count("text-overflow:ellipsis"), 2,
                "%s row must clamp every line that varies with the data (8.2)" % kind)
            self.assertIn("min-width:110px", html,
                          "%s row's action button needs a fixed width (8.4)" % kind)

    def test_the_summary_line_counts_the_two_kinds_separately(self):
        mixed = self.rendered.get("meta_mixed", "")
        only_missing = self.rendered.get("meta_missing_only", "")
        self.assertIn("no prepared reply", mixed, mixed)
        self.assertIn("not in your repertoire", mixed, mixed)
        # The pre-4.G wording conflated them.
        self.assertNotIn("unprepared opponent move", mixed, mixed)
        self.assertNotEqual(
            only_missing, mixed,
            "the summary did not react to the presence of an unanswered branch")
        self.assertNotIn("no prepared reply", only_missing, only_missing)

    def test_a_legacy_payload_without_gap_kind_still_renders(self):
        """A tree payload from before `gap_kind` existed still carries `in_repertoire`, so the
        two cases stay distinguishable instead of collapsing into the generic wording."""
        rows = self.rendered.get("legacy") or {}
        self.assertEqual(sorted(rows), ["false", "true"],
                         "the legacy scenario rendered nothing: %r" % sorted(rows))
        self.assertIn("No prepared reply", rows.get("true", ""))
        self.assertIn("Not in repertoire", rows.get("false", ""))
        self.assertNotEqual(rows.get("true"), rows.get("false"))

    def test_an_unknown_gap_kind_is_not_labelled_unanswered(self):
        """A NEW kind must not silently inherit the "you have no answer" wording."""
        rows = self.rendered.get("unknown") or {}
        self.assertIn("Not in repertoire", rows.get("brand_new", ""),
                      "an unrecognised gap_kind was labelled as an unanswered branch")
        self.assertNotIn("No prepared reply", rows.get("brand_new", ""))


# The scenario executed by `TestCoverageGapKinds`. It is kept inline (rather than in
# audit/scripts/) so the contract and the assertions that read it stay in one file, and it
# only uses the harness's existing public surface: the real singleton
# `window.repertoireBuilder`, the real `renderGapLists()`, and the real DOM ids from
# `index.html` that the harness has to be told about (exactly as FIXER-F's scenario does).
_SCENARIO = r"""
const b = window.repertoireBuilder;
['repCoverageGapsList', 'repCoverageGapsMeta', 'repGapsList', 'repGapsCount', 'repGapsMeta',
 'repPatchedGapsCount', 'btnRepTogglePatchedGaps', 'btnRepNextBlunder'
].forEach(function (id) {
    if (!document.getElementById(id)) document.byId.set(id, document.createElement('div'));
});

function gap(over) {
    return Object.assign({
        parent_fen: 'rnbqkbnr/pp2pppp/2p5/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq d6',
        opening_name: 'Sicilian Defence', eco: 'B50',
        opponent_san: 'd5', opponent_uci: 'd7d5',
        popularity_pct: 12.5, win_rate: 51.0, games: 12345,
        in_repertoire: false, gap_kind: 'missing_branch'
    }, over || {});
}

function render(gaps) {
    b.showPatchedGaps = false;
    b.openingGamesThreshold = 30;
    b.gapThreshold = 5.0;
    b.treeData = { gaps: gaps, game_gaps: { gaps: [], patched_count: 0, total_games_analyzed: 0 } };
    b.renderGapLists();
    return {
        html: document.getElementById('repCoverageGapsList').innerHTML,
        meta: document.getElementById('repCoverageGapsMeta').textContent
    };
}

const unanswered = render([gap({ gap_kind: 'unanswered_branch', in_repertoire: true })]);
const missing = render([gap({ gap_kind: 'missing_branch', opponent_san: 'Nc6', opponent_uci: 'b8c6' })]);
const mixed = render([gap({ gap_kind: 'unanswered_branch', in_repertoire: true }),
                      gap({ gap_kind: 'missing_branch', opponent_san: 'Nc6', opponent_uci: 'b8c6' })]);
const legacyTrue = render([gap({ gap_kind: undefined, in_repertoire: true })]);
const legacyFalse = render([gap({ gap_kind: undefined, in_repertoire: false })]);
const unknown = render([gap({ gap_kind: 'brand_new_kind', in_repertoire: true })]);
const empty = render([]);

return {
    rendered: {
        unanswered: unanswered.html,
        missing: missing.html,
        meta_mixed: mixed.meta,
        meta_missing_only: missing.meta,
        legacy: { 'true': legacyTrue.html, 'false': legacyFalse.html },
        unknown: { brand_new: unknown.html },
        empty: empty.html
    }
};
"""


if __name__ == "__main__":
    unittest.main()

