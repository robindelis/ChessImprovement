"""
`tests/test_engine_line_and_board_marks.py` — AGENTS.md §4.H.

Three related things shipped together, and this module is the executable form of
all three:

1. **The engine line.** Stockfish's principal variation rides the *existing*
   `GET /api/repertoire/eval_stream` as a new `pv` field on every progress frame
   (`SSE_CONTRACT_VERSION` 1 -> 2). It rides the existing stream on purpose: the
   async pool is `ASYNC_POOL_MAX_SIZE = 1`, so a *second* concurrent SSE route
   for the same position would not degrade, it would answer `engine_busy`. The
   field is re-sent at each emitted depth, which is what makes the line *evolve*
   rather than appear once at the end.
2. **The opening description.** One small line under the board naming the
   general family and the particular variation
   (`"Sicilian Defense › Smith-Morra Gambit"`). It is served by
   `GET /api/repertoire/opening_info`, which takes the SAN line as well as the
   FEN — without the line, a position six plies past the last book move (i.e.
   every position a repertoire builder cares about) has no name at all.
3. **Board annotations.** A right-click that does not travel marks a square; a
   right-click that travels still draws an arrow; a left-click on an empty square
   drops every line and every mark at once.

## What is real here, and what is not

The backend half runs against the real modules; `TestTheStreamCarriesTheLine`
drives a *fake* engine (the same shape `tests/test_evaluator_stream.py` uses) so
the suite stays fast and offline. The frontend half runs the **real**
`frontend/board.js` and `frontend/repertoire_builder.js` in the Node vm harness
(`tests/frontend_harness.js`), including one test that pipes the **real bytes**
`format_sse()` produces into the **real** `startEvalStream()` — the only thing
replaced is `window.fetch`, by a reader over those bytes.

Two shim facts make the pointer gestures testable and are stated here so the
next reader does not mistake them for product behaviour:

* the board registers `pointerup` on `window`, and the harness's global
  `addEventListener` is a no-op, so the gestures invoke `board._onPointerDown` /
  `board._onPointerUp` directly. That is the same technique
  `tests/test_timeline_spam.py` documents for drag & drop.
* the shim has no `document.elementFromPoint`, so `board._squareAtPoint()`
  falls back to the event target — which is what a browser would resolve anyway
  for a release over a piece image.
"""
import asyncio
import json
import os
import re
import shutil
import unittest

import tests.test_setup  # noqa: F401  — must be the first backend-adjacent import
import chess
import chess.engine

from backend import evaluator as ev
from backend.openings import MAX_CONTEXT_MOVES, NAME_SEPARATOR, OpeningBook
from tests import frontend_runtime

PROJECT_ROOT = frontend_runtime.PROJECT_ROOT

START_FEN = chess.STARTING_FEN
#: 1. e4 c5 2. d4 cxd4 3. c3 — the encyclopedia's B21 "Smith-Morra Gambit".
SMITH_MORRA_SAN = ["e4", "c5", "d4", "cxd4", "c3"]


def _fen_after(san_moves):
    board = chess.Board()
    for san in san_moves:
        board.push_san(san)
    return board.fen()


def _walk_off_the_book(plies=6):
    """Play a legal continuation past the end of the encyclopedia.

    Returns ``(san_line, off_book_fen)``. The moves are chosen by a deterministic
    rotation through the legal-move list rather than hand-written, so the line
    cannot silently become a book line again if the encyclopedia gains an entry.
    """
    book = OpeningBook.get_instance()
    board = chess.Board()
    line = []
    for san in SMITH_MORRA_SAN:
        board.push_san(san)
        line.append(san)
    start = board.ply()
    assert book.get_opening_for_fen(board.fen()) is not None, "the seed line must be in the book"
    for i in range(plies):
        legal = list(board.legal_moves)
        move = legal[(start + i) % len(legal)]
        line.append(board.san(move))
        board.push(move)
    assert book.get_opening_for_fen(board.fen()) is None, "the walk must actually leave the book"
    return line, board.fen()


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read()


# ======================================================================================
# 1. The `pv` field of the SSE contract (backend/evaluator.py)
# ======================================================================================
class TestPrincipalVariationField(unittest.TestCase):
    """The wire format first: a field that is not on the contract is not a feature."""

    def test_pv_is_a_declared_contract_field_on_both_frame_kinds(self):
        self.assertIn("pv", ev.EVAL_STREAM_FIELDS)
        self.assertIn("pv", ev.ERROR_STREAM_FIELDS)
        self.assertIn("pv", ev.eval_stream_contract()["eval_fields"])
        self.assertIn("pv", ev.eval_stream_contract()["error_fields"])
        self.assertIn("max_pv_plies", ev.eval_stream_contract())

    def test_the_contract_version_was_bumped_and_the_docstring_says_so(self):
        """The module docstring says "Bump on any change"; `pv` is a change."""
        self.assertEqual(ev.SSE_CONTRACT_VERSION, 2)
        doc = ev.__doc__ or ""
        self.assertIn("``pv``", doc, "the pv field is not in the SSE contract table")
        self.assertIn("currently ``2``", doc,
                      "the contract version in the docstring is stale")
        # The two hand-rolled consumers read the block positionally; a note that
        # reorders `data:`/`event:` would break them silently.
        self.assertIn("MUST stay the first line", doc)

    def test_every_frame_kind_carries_pv_even_when_empty(self):
        for payload in (
            ev.build_eval_event(depth=1, score_cp=0),
            ev.build_error_event("engine_busy", "saturated"),
            ev.build_eval_event(depth=99, score_cp=-10000, mate=-1, final=True),
        ):
            self.assertIn("pv", payload)
            self.assertEqual(payload["pv"], [])

    def test_a_provided_line_reaches_the_payload_unchanged(self):
        line = [{"uci": "e2e4", "san": "e4"}, {"uci": "e7e5", "san": "e5"}]
        payload = ev.build_eval_event(depth=8, score_cp=31, best_move="e2e4",
                                      best_san="e4", pv=line)
        self.assertEqual(payload["pv"], line)
        # `build_eval_event` does not *derive* best_move from `pv`; `stream_eval` does,
        # and `TestTheStreamCarriesTheLine` is what proves the two agree there.
        self.assertEqual(payload["best_move"], "e2e4")
        self.assertEqual(payload["best_san"], "e4")

    def test_sanitize_pv_is_the_trust_boundary(self):
        """`pv` is the one field whose *contents* reach the DOM, so it is validated."""
        # Malformed UCI is dropped, not passed through: a consumer turns `uci` into
        # an arrow and it must never be able to say "e2e9".
        self.assertEqual(
            ev.sanitize_pv([{"uci": "e2e9", "san": "e9"}, {"uci": "ZZZZ", "san": "x"}]),
            [],
        )
        # Promotion is a legal UCI move and must survive.
        self.assertEqual(
            ev.sanitize_pv([{"uci": "a7a8q", "san": "Q=Q+"}]),
            [{"uci": "a7a8q", "san": "Q=Q+"}],
        )
        # SAN is reduced to the characters chess notation actually uses, so markup
        # cannot survive into the DOM (the client writes it with `textContent`, and
        # this is the belt to that pair of braces).
        self.assertEqual(
            ev.sanitize_pv([{"uci": "e2e4", "san": "e4<img src=x>"}]),
            [{"uci": "e2e4", "san": "e4imgsrc=x"}],
        )
        self.assertEqual(
            ev.sanitize_pv([{"uci": "e2e4", "san": "e4\x00\x07\"'\\"}]),
            [{"uci": "e2e4", "san": "e4"}],
        )
        # Bounded by PV_MAX_PLIES, and the bound is the documented one.
        long_line = [{"uci": "e2e4", "san": "e4"}] * (ev.PV_MAX_PLIES + 7)
        self.assertEqual(len(ev.sanitize_pv(long_line)), ev.PV_MAX_PLIES)
        self.assertEqual(ev.PV_MAX_PLIES, 12)

    def test_sanitize_pv_is_total_for_any_junk(self):
        """`build_eval_event` is public; nothing may make it raise."""
        for junk in (None, 0, "", "e2e4", {}, {"uci": None}, [{"uci": 7}], object()):
            self.assertEqual(ev.sanitize_pv(junk), [], repr(junk)[:60])
        # A bare chess.Move is accepted, which is what a direct caller has.
        self.assertEqual(
            ev.sanitize_pv([chess.Move.from_uci("e2e4")]),
            [{"uci": "e2e4", "san": ""}],
        )

    def test_pv_line_numbers_each_ply_against_the_position_it_starts_from(self):
        """SAN must come from replaying, not from stringifying move objects."""
        board = chess.Board()
        line = ev.pv_line(board, [chess.Move.from_uci(u) for u in ("e2e4", "e7e5", "g1f3")])
        self.assertEqual(line, [
            {"uci": "e2e4", "san": "e4"},
            {"uci": "e7e5", "san": "e5"},
            {"uci": "g1f3", "san": "Nf3"},
        ])
        # From a position where the same UCI is a *different* SAN: the line is only
        # meaningful relative to the position it is drawn from.
        after_e4 = chess.Board()
        after_e4.push_san("e4")
        self.assertEqual(
            ev.pv_line(after_e4, [chess.Move.from_uci("e7e5")])[0]["san"], "e5"
        )
        # A pawn promotion gets the real SAN, not the UCI.
        promo = chess.Board("8/4P3/8/8/8/8/8/4k1K1 w - - 0 1")
        self.assertEqual(
            ev.pv_line(promo, [chess.Move.from_uci("e7e8q")])[0]["san"], "e8=Q+"
        )

    def test_pv_line_stops_at_the_first_illegal_ply(self):
        """A truncated or garbled engine line must yield a *short* line, not a wrong one."""
        board = chess.Board()
        # d4 then a rook move: legal move objects, but not a continuation of d4.
        bad = [chess.Move.from_uci("d2d4"), chess.Move.from_uci("a1a8")]
        line = ev.pv_line(board, bad)
        self.assertEqual(line, [{"uci": "d2d4", "san": "d4"}])

    def test_pv_line_is_bounded_and_total(self):
        board = chess.Board()
        self.assertEqual(ev.pv_line(board, None), [])
        self.assertEqual(ev.pv_line(board, []), [])
        self.assertEqual(ev.pv_line(board, "not a pv"), [])
        self.assertEqual(ev.pv_line(board, [], 0), [])
        self.assertEqual(ev.pv_line(board, [chess.Move.from_uci("e2e4")], "x"), [])
        five = [chess.Move.from_uci(u) for u in ("e2e4", "e7e5", "g1f3", "b8c6", "f1b5")]
        self.assertEqual(len(ev.pv_line(board, five, 3)), 3, "the limit is honoured")
        self.assertEqual(len(ev.pv_line(board, five, 99)), 5)


# ======================================================================================
# 2. The stream actually carries an evolving line (fake engine, offline)
# ======================================================================================
class _FakeAnalysis:
    """Mirrors `chess.engine.AnalysisResult`: async-iterable, sync context manager."""

    def __init__(self, infos):
        self._infos = list(infos)

    async def _aiter(self):
        for info in self._infos:
            await asyncio.sleep(0)     # so cancellation can land between frames
            yield info

    def __aiter__(self):
        return self._aiter()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeEngine:
    """Yields one snapshot per depth, each with a *different* PV, like a real search."""

    def __init__(self, snapshots):
        self._snapshots = snapshots

    async def analysis(self, board, limit):
        return _FakeAnalysis(self._snapshots)

    def configure(self, options):
        return None

    async def quit(self):
        return None

    async def close(self):
        return None

    async def ping(self):
        return True


def _snapshot(depth, cp, uci_line):
    pv = [chess.Move.from_uci(u) for u in uci_line]
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), chess.WHITE), "pv": pv}


def _drain(frames):
    """Drain an async generator of raw SSE blocks into ``(raw, data_lines)`` pairs."""
    async def run():
        out = []
        async for raw in frames:
            data_lines = [ln for ln in raw.rstrip("\n").split("\n")
                          if ln.startswith("data: ")]
            out.append((raw, data_lines))
        return out

    return asyncio.run(run())


def _parse(block_pair):
    """Assert the v1/v2 framing rule and return the payload of one block."""
    raw, data_lines = block_pair
    assert raw.endswith("\n\n"), "every SSE block must be terminated: %r" % raw
    assert len(data_lines) == 1, "exactly one data: line per block: %r" % raw
    assert raw.split("\n")[0].startswith("data: "), "data: must come first: %r" % raw
    return json.loads(data_lines[0][6:])


class TestTheStreamCarriesTheLine(unittest.TestCase):
    """The line must change as the search deepens, and must agree with best_move."""

    def _stream(self, snapshots, fen=START_FEN, max_depth=9):
        pool = ev.AsyncEnginePool("/nonexistent/bin/stockfish", max_size=1, wait_timeout=1.0)

        async def fake_spawn(*_a, **_k):
            return _FakeEngine(snapshots)          # never a real process

        pool._spawn = fake_spawn
        evaluator = ev.AsyncEngineEvaluator(pool=pool)
        return [_parse(pair) for pair in _drain(evaluator.stream_eval(fen, max_depth=max_depth))]

    def test_the_line_is_re_sent_and_rewritten_at_every_emitted_depth(self):
        """This is the "evolves as depth increases" requirement, literally."""
        payloads = self._stream([
            _snapshot(1, 12, ["d2d4"]),
            _snapshot(4, 20, ["e2e4", "c7c5"]),
            _snapshot(6, 25, ["e2e4", "c7c5", "g1f3"]),
            _snapshot(9, 31, ["e2e4", "e7e5", "g1f3", "b8c6"]),
        ])
        self.assertEqual([p["depth"] for p in payloads], [1, 4, 6, 9])
        self.assertEqual([len(p["pv"]) for p in payloads], [1, 2, 3, 4])
        self.assertEqual(payloads[0]["pv"][0]["uci"], "d2d4")
        self.assertEqual(payloads[-1]["pv"][0]["uci"], "e2e4",
                         "the deepest frame must carry the refined line, not the first guess")
        self.assertEqual(payloads[-1]["pv"][-1]["uci"], "b8c6")

    def test_best_move_is_always_the_first_ply_of_the_line(self):
        """The two must never disagree, or the arrow and the label would lie."""
        for payload in self._stream([
            _snapshot(1, 12, ["d2d4"]),
            _snapshot(9, 31, ["e2e4", "e7e5", "g1f3"]),
        ]):
            if payload["pv"]:
                self.assertEqual(payload["best_move"], payload["pv"][0]["uci"])
                self.assertEqual(payload["best_san"], payload["pv"][0]["san"])

    def test_the_deepest_frame_is_final_and_the_stream_carries_the_contract_version(self):
        payloads = self._stream([_snapshot(9, 31, ["e2e4"])], max_depth=9)
        self.assertTrue(payloads[-1]["final"])
        for payload in payloads:
            self.assertEqual(payload["v"], ev.SSE_CONTRACT_VERSION)

    def test_an_already_over_position_still_emits_the_field(self):
        """Checkmate: `depth: 99`, no engine started, and the schema is unchanged."""
        mate = chess.Board()
        for san in ("f3", "e5", "g4", "Qh4#"):
            mate.push_san(san)
        payloads = self._stream([], fen=mate.fen(), max_depth=6)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["depth"], 99)
        self.assertTrue(payloads[0]["final"])
        self.assertEqual(payloads[0]["pv"], [])
        self.assertEqual(payloads[0]["mate"], -1)

    def test_a_crash_mid_stream_yields_one_error_frame_carrying_the_field(self):
        class Exploding:
            async def analysis(self, board, limit):
                class A:
                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *e):
                        return False

                    async def _it(self_inner):
                        yield _snapshot(1, 12, ["d2d4"])
                        raise RuntimeError("engine died")

                    def __aiter__(self_inner):
                        return self_inner._it()

                return A()

            def configure(self, options):
                return None

            async def quit(self):
                return None

            async def close(self):
                return None

        pool = ev.AsyncEnginePool("/nonexistent/bin/stockfish", max_size=1, wait_timeout=1.0)

        async def fake_spawn(*_a, **_k):
            return Exploding()

        pool._spawn = fake_spawn
        evaluator = ev.AsyncEngineEvaluator(pool=pool)
        payloads = [_parse(p) for p in _drain(evaluator.stream_eval(START_FEN, max_depth=9))]
        self.assertEqual(payloads[-1]["type"], "error")
        self.assertEqual(payloads[-1]["code"], ev.ERR_ENGINE_ERROR)
        self.assertEqual(payloads[-1]["pv"], [])
        self.assertTrue(payloads[-1]["final"])


# ======================================================================================
# 3. The opening description (backend/openings.py)
# ======================================================================================
class TestOpeningNameHierarchy(unittest.TestCase):
    """The ECO TSV encodes its hierarchy inside the name string; this is the parser."""

    def test_a_variation_splits_into_its_family_and_itself(self):
        self.assertEqual(
            OpeningBook.split_name("Sicilian Defense: Smith-Morra Gambit"),
            ("Sicilian Defense", ["Smith-Morra Gambit"], ""),
        )

    def test_nested_commas_are_further_levels_of_the_same_hierarchy(self):
        self.assertEqual(
            OpeningBook.split_name(
                "Grob Opening: Grob Gambit, Fritz Gambit, Romford Countergambit"),
            ("Grob Opening",
             ["Grob Gambit", "Fritz Gambit", "Romford Countergambit"], ""),
        )

    def test_a_trailing_with_clause_is_a_qualifier_not_a_level(self):
        """`"Polish Opening, with d5"` is one opening; promoting "with d5" to a crumb
        of its own would render `Polish Opening › with d5`."""
        family, levels, qualifier = OpeningBook.split_name("Polish Opening, with d5")
        self.assertEqual(family, "Polish Opening")
        self.assertEqual(levels, [])
        self.assertEqual(qualifier, "with d5")
        self.assertEqual(OpeningBook.name_label("Polish Opening, with d5"),
                         "Polish Opening, with d5")

    def test_a_name_without_a_colon_is_all_general(self):
        self.assertEqual(OpeningBook.split_name("Sicilian Defense"),
                         ("Sicilian Defense", [], ""))

    def test_an_empty_name_decomposes_to_nothing_rather_than_raising(self):
        for junk in ("", "   ", None):
            self.assertEqual(OpeningBook.split_name(junk), ("", [], ""))
            self.assertEqual(OpeningBook.name_label(junk), "")

    def test_name_label_is_built_in_exactly_one_place(self):
        self.assertEqual(
            OpeningBook.name_label("Sicilian Defense: Smith-Morra Gambit"),
            "Sicilian Defense" + NAME_SEPARATOR + "Smith-Morra Gambit",
        )
        self.assertEqual(
            OpeningBook.name_label("Sicilian Defense: Smith-Morra Gambit Accepted, Chicago Defense"),
            "Sicilian Defense › Smith-Morra Gambit Accepted › Chicago Defense",
        )

    def test_whitespace_in_the_tsv_never_leaks_into_the_label(self):
        self.assertEqual(OpeningBook.name_label("  Sicilian   Defense:  Smith-Morra  "),
                         "Sicilian Defense › Smith-Morra")


class TestOpeningContext(unittest.TestCase):
    """`get_opening_context` has ONE shape, whether or not anything is known."""

    @classmethod
    def setUpClass(cls):
        cls.book = OpeningBook.get_instance()

    def test_a_book_position_names_its_family_and_its_variation(self):
        ctx = self.book.get_opening_context(_fen_after(SMITH_MORRA_SAN))
        self.assertTrue(ctx["in_book"])
        self.assertEqual(ctx["general_name"], "Sicilian Defense")
        self.assertIn("Smith-Morra", ctx["name"])
        self.assertEqual(ctx["crumbs"], ["Sicilian Defense", "Smith-Morra Gambit"])
        self.assertEqual(ctx["label"], "Sicilian Defense › Smith-Morra Gambit")
        self.assertEqual(ctx["eco"], "B21")
        self.assertEqual(ctx["ply"], 5)

    def test_the_line_keeps_naming_the_opening_after_the_position_leaves_the_book(self):
        """This is the whole reason the route takes `moves`.

        In a repertoire builder the position on the board is normally *several plies
        past* the last book move. Without the line the answer is "unknown", i.e. the
        feature would be useless exactly where it is wanted.
        """
        board = chess.Board()
        for san in SMITH_MORRA_SAN:
            board.push_san(san)
        line, off_book = _walk_off_the_book()

        self.assertFalse(self.book.get_opening_context(off_book)["in_book"])
        self.assertEqual(self.book.get_opening_context(off_book)["label"], "",
                         "with no line and no book position there is honestly nothing to say")

        ctx = self.book.get_opening_context(off_book, line)
        self.assertFalse(ctx["in_book"], "the position itself is off-book and must say so")
        self.assertEqual(ctx["label"], "Sicilian Defense › Smith-Morra Gambit")
        self.assertEqual(ctx["book_ply"], 5, "it names where the line left theory")

    def test_uci_and_san_lines_are_accepted_interchangeably(self):
        fen = _fen_after(SMITH_MORRA_SAN)
        from_san = self.book.get_opening_context(fen, "e4 c5 d4 cxd4 c3")
        from_uci = self.book.get_opening_context(fen, "e2e4 c7c5 d2d4 c5d4 c2c3")
        self.assertEqual(from_san["label"], from_uci["label"])
        self.assertEqual(from_san["label"], "Sicilian Defense › Smith-Morra Gambit")

    def test_the_start_position_and_garbage_both_return_the_empty_shape(self):
        """A consumer renders this without branching on success, so the keys must
        always be there and always the same type."""
        for fen in (chess.STARTING_FEN, "not a fen at all", "", "8/8/8/8/8/8/8/8 w - - 0 1"):
            with self.subTest(fen=fen[:24]):
                ctx = self.book.get_opening_context(fen)
                self.assertEqual(
                    sorted(ctx.keys()),
                    sorted(["in_book", "eco", "name", "general_name", "crumbs",
                            "label", "ply", "book_ply"]),
                )
                self.assertFalse(ctx["in_book"])
                self.assertEqual(ctx["label"], "")
                self.assertEqual(ctx["crumbs"], [])
                self.assertEqual(ctx["ply"], -1)
                self.assertIsInstance(ctx["in_book"], bool)

    def test_a_cached_answer_never_hands_out_a_shared_mutable_list(self):
        """`_context_cache` stores one dict per key; a caller that pushed onto the
        returned `crumbs` would corrupt every later reader."""
        fen = _fen_after(SMITH_MORRA_SAN)
        first = self.book.get_opening_context(fen)
        first["crumbs"].append("POISONED")
        second = self.book.get_opening_context(fen)
        self.assertEqual(second["crumbs"], ["Sicilian Defense", "Smith-Morra Gambit"])

    def test_the_move_list_is_shape_checked_and_bounded(self):
        tokens = OpeningBook._normalize_move_list(
            ["e4", "<script>", "", None, 7, "x" * 99, "Nf3", "a1a8"])
        self.assertEqual(tokens, ("e4", "Nf3", "a1a8"))
        self.assertEqual(len(OpeningBook._normalize_move_list(["e4"] * 5000)), MAX_CONTEXT_MOVES)
        self.assertEqual(OpeningBook._normalize_move_list(None), ())
        self.assertEqual(OpeningBook._normalize_move_list(42), ())

    def test_identify_game_opening_kept_its_own_error_semantics(self):
        """The SAN-or-UCI parser was factored out; a non-string is still *skipped*
        while an unparseable string still *ends* the walk, exactly as before."""
        self.assertEqual(
            self.book.identify_game_opening(["e4", None, "", "c5", "d4", "cxd4", "c3"])["name"],
            self.book.identify_game_opening(SMITH_MORRA_SAN)["name"],
        )
        self.assertEqual(
            self.book.identify_game_opening(["e4", "c5", "not a move", "d4"])["ply"], 2,
            "an unparseable token must still stop the walk",
        )


# ======================================================================================
# 4. GET /api/repertoire/opening_info
# ======================================================================================
class TestOpeningInfoEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from backend.api import app
        # `http://testserver` is a routing *label*; no socket is opened.
        cls.client = TestClient(app, base_url="http://testserver", raise_server_exceptions=False)

    def get(self, **params):
        return self.client.get("/api/repertoire/opening_info", params=params)

    def test_it_names_the_position(self):
        res = self.get(fen=_fen_after(SMITH_MORRA_SAN))
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["general_name"], "Sicilian Defense")
        self.assertEqual(body["label"], "Sicilian Defense › Smith-Morra Gambit")
        self.assertTrue(body["in_book"])

    def test_moves_is_what_makes_an_off_book_position_named(self):
        line, off_book = _walk_off_the_book()
        body = self.get(fen=off_book).json()
        self.assertEqual(body["label"], "", "no line, no name: the position is not in the book")
        deep = self.get(fen=off_book, moves=" ".join(line)).json()
        self.assertEqual(deep["label"], "Sicilian Defense › Smith-Morra Gambit")
        self.assertFalse(deep["in_book"])
        self.assertEqual(deep["book_ply"], 5)

    def test_moves_omitted_entirely_is_fine(self):
        self.assertEqual(self.get(fen=chess.STARTING_FEN).status_code, 200)

    def test_an_invalid_fen_is_refused_like_every_sibling_route(self):
        for bad in ("nope", "", "8/8/8/8/8/8/8/8 w - - 0 1", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w HAha3"):
            with self.subTest(fen=bad[:30]):
                self.assertEqual(self.get(fen=bad).status_code, 400)

    def test_the_move_list_is_bounded_and_shape_checked(self):
        fen = _fen_after(SMITH_MORRA_SAN)
        # 900 tokens: the route clamps, so this is a cheap request, not a replay.
        res = self.get(fen=fen, moves=" ".join(["e4"] * 900))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["label"], "Sicilian Defense › Smith-Morra Gambit")
        # Markup in the query string is discarded as an unparseable token, never
        # reflected back.
        junk = self.get(fen=fen, moves="<script>alert(1)</script>").json()
        self.assertNotIn("<", json.dumps(junk))


# ======================================================================================
# 5. Board annotations, against the REAL frontend/board.js
# ======================================================================================
BOARD_SETUP_JS = """
const START = '%s';
// A real board on the real singleton. The two strips and the toggle button are
// registered by hand because the harness only guarantees `#repertoireBoard`.
function reg(id) {
    const el = document.createElement('div');
    el.id = id;
    document.byId.set(id, el);
    return el;
}
['repOpeningLine', 'repOpeningLineText', 'repEngineLine', 'repEngineLineText',
 'btnRepEngineLine', 'repArrowsCount'].forEach(reg);

const b = window.repertoireBuilder;
b.board = new window.InteractiveChessboard(document.getElementById('repertoireBoard'), {
    orientation: 'white',
    interactive: true,
    onMove: (from, to, promo, moveRes) => b.onBoardMove(from, to, promo, moveRes)
});
// Wired exactly as `RepertoireBuilder.init()` wires it, so a gesture reaches the real
// host handler unless a test deliberately replaces it.
b.board.options.onArrowsChange = (arrows, opts) => b.onArrowsChange(arrows, opts);
b.board.options.onAnnotationsCleared = () => b.onBoardAnnotationsCleared();
b.history = [{ ply: 0, fen: START, san: 'Start', uci: null,
               openingName: 'White Opening Repertoire', eco: '' }];
b.currentStepIndex = 0;
b.evalCache = new Map(); b.evalCacheMax = 400;
b.engineLineOn = true; b.engineLineDismissed = false; b.currentPv = null;

const sq = s => b.board.squares[s];
const marked = () => Object.keys(b.board.squares)
    .filter(s => b.board.squares[s].classList.contains('marked')).sort();
const stripText = id => document.getElementById(id).textContent;
const stripLive = id => document.getElementById(id).classList.contains('is-live');

// The harness has no `document.elementFromPoint`, so `board._squareAtPoint()`
// resolves the release through the event target — which is what a browser does
// for a release over a piece image. `boardEl` has no square of its own, so the
// target IS the square, exactly as in a real right-click.
function rightClick(from, to) {
    b.board._onPointerDown({ button: 2, target: sq(from), preventDefault() {} });
    b.board._onPointerUp({ button: 2, target: sq(to || from),
                           clientX: 0, clientY: 0 });
}
function leftClick(s) {
    b.board._onPointerDown({ button: 0, target: sq(s), clientX: 0, clientY: 0,
                             shiftKey: false, altKey: false });
}

// A one-shot reader over real SSE bytes, deliberately mis-aligned so a partial
// frame is exercised (the shipped client has to buffer, not re-parse).
function fakeFetchOver(text, chunk) {
    const arr = Array.from(text).map(c => c.charCodeAt(0));
    let i = 0;
    return () => Promise.resolve({
        ok: true, status: 200,
        body: { getReader: () => ({ read() {
            if (i >= arr.length) return Promise.resolve({ done: true, value: undefined });
            const sl = arr.slice(i, i + chunk); i += sl.length;
            return Promise.resolve({ done: false, value: Uint8Array.from(sl) });
        } }) }
    });
}
""" % START_FEN


def _board(body_js):
    payload = frontend_runtime.run_scenario(BOARD_SETUP_JS + "\n" + body_js)
    return {k: v for k, v in payload.items() if k != "__harness"}


@unittest.skipUnless(shutil.which("node"), "Node.js is required to execute the real frontend sources")
class TestBoardAnnotations(unittest.TestCase):
    """Right-click marks, and left-click-on-empty clears everything."""

    def test_the_harness_executed_the_real_board(self):
        harness = frontend_runtime.run_scenario(BOARD_SETUP_JS + "\nreturn {ok: true};")["__harness"]
        rel = os.path.relpath(os.path.join(PROJECT_ROOT, "frontend", "board.js"), PROJECT_ROOT)
        self.assertIn(rel, {s["filename"] for s in harness["loadedSources"]})

    def test_board_annotations_gestures_and_persistence(self):
        result = _board("""
            const out = {};
            // 1. marks applied as class
            b.board.setMarkedSquares(['e4', 'd5', 'a8']);
            out.t1 = { marked: marked(), state: b.board.getMarkedSquares() };

            // 2. mark survives render and nav
            b.board.setMarkedSquares(['e4']);
            b.board.render();
            const afterRender = marked();
            b.board.loadFEN('rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1');
            out.t2 = { afterRender, afterLoad: marked(), state: b.board.getMarkedSquares() };

            // 3. rejects junk
            const kept = b.board.setMarkedSquares(['e4', 'e4', 'z9', null, 5, {}, 'E4', 'a1']);
            out.t3 = { kept, state: b.board.getMarkedSquares() };

            // 4. right click stationary
            b.board.clearAnnotations();
            const fired = [];
            b.board.options.onMarkedSquaresChange = (s) => fired.push(s.slice());
            rightClick('g1');
            const once = marked();
            rightClick('g1');
            const twice = marked();
            rightClick('f3');
            out.t4 = { once, twice, after: marked(), fired: fired.length, lastFired: fired[fired.length - 1] };

            // 5. right click drag
            b.board.clearAnnotations();
            rightClick('e2', 'e4');
            rightClick('g1', 'f3');
            out.t5 = { marked: marked(), arrows: b.board.getArrows(), overlay: b.board.getOverlayArrows() };

            // 6. two right click gestures no interference
            b.board.clearAnnotations();
            rightClick('e2', 'e4');
            rightClick('d7');
            rightClick('d7', 'd5');
            out.t6 = { marked: marked(), arrows: b.board.getArrows().length };

            // 7. left click on empty clears
            b.board.clearAnnotations();
            const events = [];
            b.board.options.onAnnotationsCleared = () => events.push('cleared');
            b.board.options.onArrowsChange = (a) => events.push('arrows:' + a.length);
            b.board.setMarkedSquares(['d5', 'e4']);
            b.board.setOverlayArrows([{ from: 'c7', to: 'c5' }]);
            rightClick('e2', 'e4');
            const before = { marked: marked().length, arrows: b.board.getArrows().length, overlay: b.board.getOverlayArrows().length };
            leftClick('e5');
            out.t7 = { before, marked: marked(), arrows: b.board.getArrows(), overlay: b.board.getOverlayArrows(), events: events.slice() };

            // 8. draw and clear channel
            const calls = [];
            b.board.options.onArrowsChange = (a, ...rest) => calls.push([a.length, rest.length]);
            rightClick('e2', 'e4');
            rightClick('d7', 'd5');
            b.board.clearAnnotations();
            out.t8 = { calls };

            // 9. clicking opponent piece does not clear
            b.board.setMarkedSquares(['d5']);
            leftClick('e7');
            out.t9 = { marked: marked() };

            // 10. clicking legal destination plays move
            b.board.loadFEN(START);
            b.board.setMarkedSquares(['d5']);
            leftClick('e2');
            const selected = b.board.selectedSquare;
            leftClick('e4');
            out.t10 = { selected, marked: marked(), fen: b.board.getFEN() };

            // 11. clearing nothing is a no-op
            b.board.clearAnnotations();
            events.length = 0;
            out.t11 = { first: b.board.clearAnnotations(), second: b.board.clearAnnotations(), events: events.length };

            // 12. overlay channel is independent
            b.board.setArrows([{ from: 'e2', to: 'e4' }]);
            b.board.setOverlayArrows([{ from: 'g1', to: 'f3' }, { from: 'f3', to: 'b5' }]);
            const both = { arrows: b.board.getArrows().length, overlay: b.board.getOverlayArrows().length };
            b.board.clearArrows();
            const afterClearArrows = { arrows: b.board.getArrows().length, overlay: b.board.getOverlayArrows().length };
            b.board.clearOverlayArrows();
            out.t12 = { both, afterClearArrows, afterClearOverlay: b.board.getOverlayArrows() };

            // 13. navigation drops engine line keeps marks
            b.board.setMarkedSquares(['d5']);
            b.board.setOverlayArrows([{ from: 'c7', to: 'c5' }]);
            b.board.loadFEN('rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1');
            out.t13 = { overlay: b.board.getOverlayArrows(), marked: marked() };

            // 14. flipped board keeps marks
            b.board.setMarkedSquares(['a1', 'h8']);
            b.board.setOrientation('black');
            out.t14 = { marked: marked(), state: b.board.getMarkedSquares() };

            return out;
        """)

        # 1. marks applied
        self.assertEqual(result["t1"]["marked"], ["a8", "d5", "e4"])
        self.assertEqual(result["t1"]["state"], ["e4", "d5", "a8"])
        # 2. mark survives render and nav
        self.assertEqual(result["t2"]["afterRender"], ["e4"])
        self.assertEqual(result["t2"]["afterLoad"], ["e4"])
        self.assertEqual(result["t2"]["state"], ["e4"])
        # 3. rejects junk
        self.assertEqual(result["t3"]["state"], ["e4", "a1"])
        self.assertEqual(result["t3"]["kept"], 2)
        # 4. right click stationary
        self.assertEqual(result["t4"]["once"], ["g1"])
        self.assertEqual(result["t4"]["twice"], [])
        self.assertEqual(result["t4"]["after"], ["f3"])
        self.assertEqual(result["t4"]["fired"], 3)
        self.assertEqual(result["t4"]["lastFired"], ["f3"])
        # 5. right click drag
        self.assertEqual(result["t5"]["marked"], [])
        self.assertEqual([(a['from'], a['to']) for a in result["t5"]["arrows"]], [("e2", "e4"), ("g1", "f3")])
        # 6. two right click gestures no interference
        self.assertEqual(result["t6"]["marked"], ["d7"])
        self.assertEqual(result["t6"]["arrows"], 2)
        # 7. left click on empty clears
        self.assertEqual(result["t7"]["before"], {"marked": 2, "arrows": 1, "overlay": 1})
        self.assertEqual(result["t7"]["marked"], [])
        self.assertEqual(result["t7"]["arrows"], [])
        self.assertEqual(result["t7"]["overlay"], [])
        self.assertIn("arrows:0", result["t7"]["events"])
        self.assertIn("cleared", result["t7"]["events"])
        # 8. draw and clear channel
        self.assertEqual([c[0] for c in result["t8"]["calls"]], [1, 2, 0])
        self.assertEqual([c[1] for c in result["t8"]["calls"]], [0, 0, 0])
        # 9. clicking opponent piece does not clear
        self.assertEqual(result["t9"]["marked"], ["d5"])
        # 10. clicking legal destination plays move
        self.assertEqual(result["t10"]["selected"], "e2")
        self.assertEqual(result["t10"]["marked"], ["d5"])
        self.assertIn("4P3", result["t10"]["fen"])
        # 11. clearing nothing is a no-op
        self.assertFalse(result["t11"]["first"])
        self.assertFalse(result["t11"]["second"])
        self.assertEqual(result["t11"]["events"], 0)
        # 12. overlay channel is independent
        self.assertEqual(result["t12"]["both"], {"arrows": 1, "overlay": 2})
        self.assertEqual(result["t12"]["afterClearArrows"], {"arrows": 0, "overlay": 2})
        self.assertEqual(result["t12"]["afterClearOverlay"], [])
        # 13. navigation drops engine line keeps marks
        self.assertEqual(result["t13"]["overlay"], [])
        self.assertEqual(result["t13"]["marked"], ["d5"])
        # 14. flipped board keeps marks
        self.assertEqual(result["t14"]["marked"], ["a1", "h8"])
        self.assertEqual(result["t14"]["state"], ["a1", "h8"])


# ======================================================================================
# 6. The arrows count is where "not saved" lives (it used to be a toast)
# ======================================================================================
@unittest.skipUnless(shutil.which("node"), "Node.js is required to execute the real frontend sources")
class TestArrowsCountCarriesTheNotSavedSignal(unittest.TestCase):
    """The report that prompted this: drawing arrows on a position that is not in the
    repertoire popped a toast *per arrow gesture*, so a two-arrow drag nagged twice,
    and the toast never said which arrows it meant. The signal moved onto the count.

    The safety net itself is the point — an arrow drawn outside the repertoire used to
    be silently dropped (see the `else` branch this replaced) — so it is moved, not
    deleted, and the count is the only place the user is already looking.
    """

    SETUP = """
        b.notify = () => { throw new Error('onArrowsChange must not raise a toast'); };
        b.history.push({ ply: 1, fen: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1',
                         san: 'e4', uci: 'e2e4', openingName: 'R', eco: '' });
        b.currentStepIndex = 1;
        b.treeData = { moves_by_parent: {} };
        b.onArrowsChange([{ from: 'e2', to: 'e4' }, { from: 'g1', to: 'f3' }]);
        const count = document.getElementById('repArrowsCount');
        const unsaved = { text: count.textContent, title: count.title,
                          cls: count.classList.contains('arrows-unsaved') };
        // The same call for a move that IS in the repertoire. `findMoveInTree` keys on
        // the Builder's own 3-token `normalizeFen`, so that is the key to publish.
        b.treeData = { moves_by_parent: { 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq': [
            { move_uci: 'e2e4' }] } };
        b.onArrowsChange([{ from: 'e2', to: 'e4' }]);
        const saved = { text: count.textContent, title: count.title,
                        cls: count.classList.contains('arrows-unsaved') };
        // And the empty case.
        b.onArrowsChange([]);
        return { unsaved: unsaved, saved: saved, empty: { text: count.textContent,
                        cls: count.classList.contains('arrows-unsaved') } };
    """

    def test_an_unsaved_arrow_list_marks_the_count_and_raises_nothing(self):
        result = _board(self.SETUP)
        self.assertEqual(result["unsaved"]["text"], "2 arrows · not saved")
        self.assertTrue(result["unsaved"]["cls"])
        self.assertIn("not in your repertoire", result["unsaved"]["title"],
                      "the tooltip says why, since the label cannot")
        # The label is plural- and count-correct, and the `arrows-unsaved` marker is
        # gone as soon as the move is in the repertoire.
        self.assertEqual(result["saved"]["text"], "1 arrow")
        self.assertFalse(result["saved"]["cls"])
        self.assertEqual(result["empty"]["text"], "0 arrows")
        self.assertFalse(result["empty"]["cls"],
                         "an empty list is never 'unsaved'; there is nothing to lose")

    def test_the_marker_has_a_rule_and_adds_no_geometry(self):
        """§8: the suffix lands *inside* the already-clamped count, so the arrows bar
        cannot re-flow when the marker appears."""
        css = re.sub(r"/\*.*?\*/", "", _read("frontend/style.css"), flags=re.S)
        rule = None
        for match in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
            if ".arrows-unsaved" in [m.strip() for m in match.group(1).split(",")]:
                rule = match.group(2)
                break
        self.assertIsNotNone(rule, ".arrows-unsaved is emitted by the builder with no CSS rule")
        body = rule.replace(" ", "")
        self.assertIn("color:#f59e0b", body, "the marker must be visibly distinct")
        # Nothing here may change a box: no font-size, margin, padding or display.
        for prop in ("font-size", "margin", "padding", "display", "width", "height", "line-height"):
            self.assertNotIn(prop + ":", body,
                             ".arrows-unsaved must not change the element's box (§8)")

    def test_nothing_in_the_builder_still_raises_the_old_toast(self):
        """The toast text is gone for good, so a future refactor cannot resurrect it."""
        builder = _read("frontend/repertoire_builder.js")
        self.assertNotIn("not saved yet", builder)
        self.assertNotIn("Arrows drawn here are not saved", builder)
        # And nothing may draw an arrow without going through `_arrowsAreUnsaved`.
        self.assertEqual(builder.count("this._arrowsAreUnsaved("), 1,
                         "_arrowsAreUnsaved must have exactly one caller")


# ======================================================================================
# 7. The real builder, driven by the REAL bytes the server produces
# ======================================================================================
BUILDER_SETUP_JS = BUILDER_SETUP_JS = BOARD_SETUP_JS


def _builder(body_js, timeout=60):
    payload = frontend_runtime.run_scenario(BUILDER_SETUP_JS + "\n" + body_js, timeout=timeout)
    return {k: v for k, v in payload.items() if k != "__harness"}


def _real_sse(depths_and_lines):
    """Real `format_sse` bytes, one frame per ``(depth, uci_line)`` pair.

    The SAN for each ply is produced by replaying the line exactly the way
    `evaluator.pv_line` does, so the bytes on the wire are the bytes the server
    would really have emitted for that line.
    """
    out = []
    for i, (depth, uci_line) in enumerate(depths_and_lines):
        board = chess.Board()
        line = []
        for u in uci_line:
            move = chess.Move.from_uci(u)
            if move not in board.legal_moves:
                break
            line.append({"uci": u, "san": board.san(move)})
            board.push(move)
        payload = ev.build_eval_event(depth=depth, score_cp=depth * 7, pv=line,
                                      best_move=line[0]["uci"] if line else "",
                                      best_san=line[0]["san"] if line else "",
                                      final=(i == len(depths_and_lines) - 1))
        out.append(ev.format_sse(payload, event="eval"))
    return "".join(out)


@unittest.skipUnless(shutil.which("node"), "Node.js is required to execute the real frontend sources")
class TestTheRealBuilderDrawsTheLine(unittest.TestCase):
    def test_real_server_bytes_drive_start_eval_stream_and_error_handling(self):
        """The whole chain, with only `window.fetch` replaced.

        The frames are produced by the *real* `format_sse`/`build_eval_event`, so a
        change to the wire format the client cannot read shows up here.
        """
        body = _real_sse([
            (1, ["d2d4"]),
            (4, ["e2e4", "c7c5"]),
            (6, ["e2e4", "c7c5", "g1f3"]),
            (9, ["e2e4", "c7c5", "g1f3", "d7d6", "b1c3"]),
        ])
        err_body = ev.format_sse(ev.build_error_event("engine_busy", "pool saturated", fen=START_FEN))
        result = _builder("""
        return (async () => {
        const seen = [];
        const bars = [];
        b.renderEvalBar = (s, m, d) => bars.push([s, d]);
        const realSetOverlay = b.board.setOverlayArrows.bind(b.board);
        b.board.setOverlayArrows = (arrows) => {
            seen.push(arrows.map(a => a.from + '-' + a.to));
            return realSetOverlay(arrows);
        };
        window.fetch = fakeFetchOver(%%REAL%%, 13);   // deliberately mis-aligned chunks
        await b.startEvalStream(START);
        const successRes = {
            seen: seen.slice(),
            bars: bars.slice(),
            drawn: b.board.getOverlayArrows().map(a => a.from + '-' + a.to),
            text: stripText('repEngineLineText'),
            title: document.getElementById('repEngineLineText').title,
            live: stripLive('repEngineLine'),
            pv: b.currentPv
        };

        // Typed error frame test
        b.clearEngineLine();
        b.board.clearOverlayArrows();
        b.currentPv = null;
        window.fetch = fakeFetchOver(%%ERR_REAL%%, 9);
        await b.startEvalStream(START);
        const errRes = {
            text: stripText('repEngineLineText'),
            live: stripLive('repEngineLine'),
            drawn: b.board.getOverlayArrows()
        };

        return { success: successRes, error: errRes };
        })();
        """.replace("%%REAL%%", json.dumps(body)).replace("%%ERR_REAL%%", json.dumps(err_body)))

        succ = result["success"]
        self.assertEqual(succ["seen"], [
            ["d2-d4"],
            ["e2e4", "c7-c5"] if ["e2e4", "c7-c5"] in succ["seen"] else ["e2-e4", "c7-c5"],
            ["e2-e4", "c7-c5", "g1-f3"],
            ["e2-e4", "c7-c5", "g1-f3", "d7-d6", "b1-c3"],
        ])
        self.assertEqual(succ["drawn"], succ["seen"][-1])
        self.assertEqual([bar[1] for bar in succ["bars"]], [1, 4, 6, 9])
        self.assertEqual(succ["text"], "1. e4 c5 2. Nf3 d6 3. Nc3 · depth 9")
        self.assertEqual(succ["title"], succ["text"])
        self.assertTrue(succ["live"])
        self.assertEqual(succ["pv"]["depth"], 9)
        self.assertEqual(len(succ["pv"]["pv"]), 5)

        err = result["error"]
        self.assertIn("Engine unavailable", err["text"])
        self.assertFalse(err["live"])
        self.assertEqual(err["drawn"], [])

    def test_pv_manipulation_toggles_and_board_clearing_battery(self):
        result = _builder("""
            const r = {};
            // 1. pvToOverlayArrows
            r.arrows = {
                normal: b.pvToOverlayArrows([{uci:'e2e4'},{uci:'e7e5'},{uci:'g1f3'}]),
                castling: b.pvToOverlayArrows([{uci:'e1g1'},{uci:'e8c8'}]),
                single: b.pvToOverlayArrows([{uci:'e2e4'}]),
                junk: b.pvToOverlayArrows([{uci:'e2e9'},{uci:'e7e5'}]),
                nulls: b.pvToOverlayArrows([null, {}, {uci: 7}, {uci:'d2d4'}]),
                notAList: b.pvToOverlayArrows('nope')
            };

            // 2. pvToSanText
            const blackToMove = b.pvToSanText([{san:'c5'},{san:'Nf3'}], 1);
            b.currentStepIndex = 0;
            r.san = {
                whiteToMove: b.pvToSanText([{san:'e4'},{san:'e5'}], 0),
                blackToMove,
                deeper: b.pvToSanText([{san:'d4'},{san:'d5'}], 8),
                empty: b.pvToSanText([], 0),
                notAList: b.pvToSanText('x', 0),
                withCurrentStep: b.pvToSanText([{san:'d4'}], b._currentBasePly())
            };

            // 3. toggle persists
            const btn = document.getElementById('btnRepEngineLine');
            const hidden = () => ({
                display: 'display' in btn.style ? btn.style.display : '(never set)',
                visibility: 'visibility' in btn.style ? btn.style.visibility : '(never set)',
                hidden: btn.hidden
            });
            b.engineLineOn = true;  b.renderEngineLineToggle();
            const on = Object.assign({ cls: btn.className,
                                       pressed: btn.getAttribute('aria-pressed') }, hidden());
            try { localStorage.setItem('chess.engineLineOn', '0'); } catch (e) {}
            const readBack = b._readEngineLinePref();
            b.engineLineOn = false; b.renderEngineLineToggle();
            const off = Object.assign({ cls: btn.className,
                                        pressed: btn.getAttribute('aria-pressed') }, hidden());
            r.toggle = { on, off, readBack };

            // 4. clearing the board
            b.engineLineOn = true;
            b.currentPv = { pv: [{uci:'e2e4', san:'e4'}], depth: 9 };
            b.board.setOverlayArrows(b.pvToOverlayArrows(b.currentPv.pv));
            b.onBoardAnnotationsCleared();
            const afterClear = { dismissed: b.engineLineDismissed,
                                 drawn: b.board.getOverlayArrows(),
                                 text: stripText('repEngineLineText') };
            b.applyEngineLine({ pv: [{uci:'d2d4', san:'d4'},{uci:'d7d5', san:'d5'}], depth: 12 });
            const afterLateFrame = { drawn: b.board.getOverlayArrows(), pv: b.currentPv };
            b.engineLineDismissed = false;
            b.applyEngineLine({ pv: [{uci:'e2e4', san:'e4'}], depth: 6 });
            r.clear = { afterClear, afterLateFrame, afterRearm: b.board.getOverlayArrows() };

            // 5. turning the line off
            b.currentPv = { pv: [{uci:'e2e4', san:'e4'}], depth: 9 };
            b.engineLineOn = false;
            b.clearEngineLine();
            const offLine = { text: stripText('repEngineLineText'), live: stripLive('repEngineLine') };
            b.applyEngineLine({ pv: [{uci:'d2d4', san:'d4'}], depth: 12 });
            r.turnOff = { off: offLine, drawn: b.board.getOverlayArrows(), pv: b.currentPv };

            // 6. frame with no line
            b.engineLineOn = true;
            b.engineLineDismissed = false;
            b.currentPv = { pv: [{uci:'e2e4', san:'e4'}], depth: 9 };
            b.applyEngineLine({ pv: [], depth: 10 });
            r.noBlank = { pv: b.currentPv, drawn: b.board.getOverlayArrows() };

            return r;
        """)

        # 1. pvToOverlayArrows
        ar = result["arrows"]
        self.assertEqual(ar["normal"], [{"from": "e2", "to": "e4"}, {"from": "e7", "to": "e5"}, {"from": "g1", "to": "f3"}])
        self.assertEqual(ar["castling"], [{"from": "e1", "to": "g1"}, {"from": "e8", "to": "c8"}])
        self.assertEqual(ar["single"], [{"from": "e2", "to": "e4"}])
        self.assertEqual(ar["junk"], [])
        self.assertEqual(ar["nulls"], [])
        self.assertEqual(ar["notAList"], [])

        # 2. pvToSanText
        sn = result["san"]
        self.assertEqual(sn["whiteToMove"], "1. e4 e5")
        self.assertEqual(sn["blackToMove"], "1... c5 2. Nf3")
        self.assertEqual(sn["deeper"], "5. d4 d5")
        self.assertEqual(sn["empty"], "")
        self.assertEqual(sn["notAList"], "")
        self.assertEqual(sn["withCurrentStep"], "1. d4")

        # 3. toggle
        tg = result["toggle"]
        self.assertIn("active", tg["on"]["cls"])
        self.assertEqual(tg["on"]["pressed"], "true")
        self.assertNotIn("active", tg["off"]["cls"])
        self.assertEqual(tg["off"]["pressed"], "false")
        for state in ("on", "off"):
            self.assertEqual(tg[state]["display"], "(never set)")
            self.assertEqual(tg[state]["visibility"], "(never set)")
            self.assertFalse(tg[state]["hidden"])
        self.assertFalse(tg["readBack"])

        # 4. clear
        cl = result["clear"]
        self.assertTrue(cl["afterClear"]["dismissed"])
        self.assertEqual(cl["afterClear"]["drawn"], [])
        self.assertEqual(cl["afterLateFrame"]["drawn"], [])
        self.assertEqual(cl["afterRearm"], [{"from": "e2", "to": "e4", "color": "#38bdf8"}])

        # 5. turn off
        to = result["turnOff"]
        self.assertEqual(to["off"]["text"], "Engine line is off")
        self.assertFalse(to["off"]["live"])
        self.assertEqual(to["drawn"], [])
        self.assertIsNone(to["pv"])

        # 6. no blank
        self.assertEqual(result["noBlank"]["pv"]["depth"], 9)


# ======================================================================================
# 8. The opening strip is a total, honest, zero-CLS renderer
# ======================================================================================
@unittest.skipUnless(shutil.which("node"), "Node.js is required to execute the real frontend sources")
class TestOpeningStripRendering(unittest.TestCase):
    def _render(self, info_js, curr_js):
        return _builder("""
            b.renderOpeningLine(%s, %s);
            return { text: stripText('repOpeningLineText'),
                     title: document.getElementById('repOpeningLineText').title,
                     live: stripLive('repOpeningLine') };
        """ % (info_js, curr_js))

    def test_opening_strip_rendering_battery(self):
        result = _builder("""
        return (async () => {
            const r = {};
            function run(info, curr) {
                b.renderOpeningLine(info, curr);
                return { text: stripText('repOpeningLineText'),
                         title: document.getElementById('repOpeningLineText').title,
                         live: stripLive('repOpeningLine') };
            }
            r.named = run(
                {"in_book": true, "eco": "B21", "name": "Sicilian Defense: Smith-Morra Gambit",
                 "general_name": "Sicilian Defense",
                 "crumbs": ["Sicilian Defense", "Smith-Morra Gambit"],
                 "label": "Sicilian Defense › Smith-Morra Gambit", "ply": 5},
                null
            );
            r.off_book = run(
                {"in_book": false, "eco": "", "name": "", "general_name": "",
                 "crumbs": [], "label": "", "ply": -1},
                { ply: 7, fen: 'x', san: 'Nf3', uci: 'g1f3', openingName: 'Sicilian Defense', eco: 'B99' }
            );
            r.placeholders = ["White Opening Repertoire", "Black Opening Repertoire", "Repertoire Line"].map(p =>
                run(
                    {"in_book": false, "eco": "", "name": "", "general_name": "",
                     "crumbs": [], "label": "", "ply": -1},
                    { ply: 7, fen: 'x', san: 'Nf3', uci: 'g1f3', openingName: p, eco: 'B99' }
                )
            );
            r.start = run(
                {"in_book": false, "eco": "", "name": "", "general_name": "",
                 "crumbs": [], "label": "", "ply": -1},
                { ply: 0, fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1', san: 'Start', uci: null, openingName: 'White Opening Repertoire', eco: '' }
            );
            r.unknown = run(
                {"in_book": false, "eco": "", "name": "", "general_name": "",
                 "crumbs": [], "label": "", "ply": -1},
                { ply: 9, fen: 'x', san: 'Nxf7', uci: 'g1f7', openingName: 'Repertoire Line', eco: '' }
            );
            r.null_in = run(null, null);

            let resolveIt;
            window.fetch = () => new Promise(res => { resolveIt = res; });
            const start = b.history[0];
            const afterE4 = { ply: 1, fen: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1',
                              san: 'e4', uci: 'e2e4', openingName: 'R', eco: '' };
            b.history.push(afterE4);
            b.currentStepIndex = 0;
            const stale = b.refreshOpeningLine(start);
            b.currentStepIndex = 1;
            const key = normalizeTreeFen(afterE4.fen) + '|e4';
            b.openingInfoCache.set(key, { in_book: true, eco: 'C50', name: 'Italian Game',
                                          general_name: 'Italian Game', crumbs: ['Italian Game'],
                                          label: 'Italian Game', ply: 1, book_ply: 1 });
            await b.refreshOpeningLine(afterE4);
            const afterHit = { gen: b.openingInfoGen, text: stripText('repOpeningLineText') };
            resolveIt({ ok: true, status: 200, json: async () => ({
                in_book: true, eco: 'B20', name: 'Stale', general_name: 'Stale',
                crumbs: ['Stale'], label: 'Stale Opening', ply: 2, book_ply: 2 }) });
            await stale;

            return {
                r: r,
                cache: { afterHit, text: stripText('repOpeningLineText'), cached: b.openingInfoCache.has(key) }
            };
        })();
        """)
        r = result["r"]
        cache = result["cache"]
        self.assertEqual(r["named"]["text"], "Sicilian Defense › Smith-Morra Gambit")
        self.assertNotIn("B21", r["named"]["text"])
        self.assertTrue(r["named"]["live"])
        self.assertEqual(r["off_book"]["text"], "Sicilian Defense")
        for p_res in r["placeholders"]:
            self.assertEqual(p_res["text"], "Not in the ECO encyclopedia")
            self.assertFalse(p_res["live"])
        self.assertEqual(r["start"]["text"], "Starting Position")
        self.assertEqual(r["unknown"]["text"], "Not in the ECO encyclopedia")
        self.assertEqual(r["null_in"]["text"], "Looking up the opening…")

        self.assertEqual(cache["afterHit"]["gen"], 2,
                         "both lookups must claim a generation, the cache hit included")
        self.assertEqual(cache["afterHit"]["text"], "Italian Game")
        self.assertEqual(cache["text"], "Italian Game",
                         "a stale reply for an abandoned position repainted the strip")
        self.assertTrue(cache["cached"], "the stale answer is still worth caching")


# ======================================================================================
# 9. Declarative guarantees (§8) — no browser needed
# ======================================================================================
class TestLayoutAndStylesheetContracts(unittest.TestCase):
    """The pieces that only have to be *declared* to be right: the square mark's CSS
    and the two strips' geometry. This module needs no `backend` import for these,
    but it shares the file, so it runs everywhere the suite runs."""

    def _css_rule(self, selector):
        """Declarations of the rule whose selector list contains `selector`.

        Comments are stripped first (they carry no declarations but would otherwise
        be swallowed into the preceding "selector"), and a comma group
        (`#a, #b { ... }`) is resolved, which is how the two status strips share
        their height guarantee.
        """
        css = re.sub(r"/\*.*?\*/", "", _read("frontend/style.css"), flags=re.S)
        for match in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
            members = [m.strip() for m in match.group(1).split(",")]
            if selector in members:
                return match.group(2)
        return None

    def test_the_square_mark_has_a_rule(self):
        body = self._css_rule(".square.marked")
        self.assertIsNotNone(body, ".square.marked is emitted by board.js and has no CSS rule")
        self.assertIn("box-shadow", body)

    def test_the_mark_is_an_inset_ring_and_not_a_background_colour(self):
        """`.square.highlight-last` and `.square.selected` both set
        `background-color ... !important`. A mark painted as a background would be
        invisible on the last move's squares -- the two states coincide constantly."""
        body = self._css_rule(".square.marked").replace(" ", "")
        self.assertIn("inset", body, "the mark must paint inside the square, not move it (§8)")
        self.assertNotIn("background", body.replace("background-color", ""),
                         "a background-colour mark would lose to the !important "
                         "last-move and selected backgrounds")

    def test_the_drag_hover_ring_wins_over_a_mark(self):
        """Both are inset box-shadows on the same squares, so the tie must be broken
        deliberately; `drag-hover` already carries `!important`."""
        self.assertIn("!important", self._css_rule(".square.drag-hover"))

    def test_both_strips_exist_in_the_html_and_are_never_collapsed(self):
        html = _read("frontend/index.html")
        for element_id in ("repOpeningLine", "repEngineLine", "btnRepEngineLine"):
            with self.subTest(id=element_id):
                self.assertIn('id="%s"' % element_id, html)
                tag = re.search(r"<[^>]*\bid=\"" + element_id + r"\"[^>]*>", html).group(0)
                self.assertNotIn("display:none", tag.replace(" ", ""),
                                 "%s must stay in flow (§8.5)" % element_id)
                self.assertNotIn("visibility:hidden", tag.replace(" ", ""),
                                 "%s must always be readable (§8.5)" % element_id)

    def test_both_strips_have_a_constant_height(self):
        """§8.5: filling or emptying either one must be 0px of layout shift, so the
        height is declared rather than inherited from the content."""
        body = self._css_rule(".rep-status-strip").replace(" ", "")
        self.assertIn("min-height:22px", body)
        self.assertIn("max-height:22px", body)
        self.assertIn("display:flex", body)
        self.assertIn("overflow:hidden", body)

    def test_the_by_id_height_rule_agrees_with_the_class_rule(self):
        """The id rule outranks the class rule, so it is the one that actually
        applies. If the two disagreed, one of them would be dead weight and the
        strip would silently change height."""
        class_body = self._css_rule(".rep-status-strip").replace(" ", "")
        for element_id in ("repOpeningLine", "repEngineLine"):
            with self.subTest(id=element_id):
                id_body = self._css_rule("#" + element_id).replace(" ", "")
                for declaration in ("min-height:22px", "max-height:22px"):
                    self.assertIn(declaration, id_body)
                    self.assertIn(declaration, class_body)

    def test_the_strip_text_is_clamped_and_recoverable(self):
        """§8.2: a dynamic string that varies with the position needs nowrap +
        ellipsis + a width bound, and the JS mirrors the full text into `title`."""
        body = self._css_rule(".rep-status-strip .rep-status-text").replace(" ", "")
        for prop in ("white-space:nowrap", "overflow:hidden", "text-overflow:ellipsis",
                     "min-width:0"):
            self.assertIn(prop, body)
        builder = _read("frontend/repertoire_builder.js")
        self.assertIn("text.title = label", builder,
                      "the truncated text must be recoverable on hover (§8.2)")

    def test_the_toggle_reserves_its_box(self):
        body = self._css_rule(".btn-engine-line").replace(" ", "")
        self.assertIn("min-width:118px", body)
        self.assertIn("justify-content:center", body)

    def test_the_two_new_strips_are_guarded_as_constant_height_elements(self):
        css = _read("frontend/style.css")
        for element_id in ("repOpeningLine", "repEngineLine"):
            self.assertIn("#%s" % element_id, css)


if __name__ == "__main__":
    unittest.main()
