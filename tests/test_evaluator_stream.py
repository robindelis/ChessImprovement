"""Regression tests for the Stockfish SSE evaluation stream and engine pools.

Covers the three audit findings assigned to FIXER-C:
  * CODE-02-23 -- payload key drift: the stream must expose BOTH ``score_cp``
    (repertoire_builder.js) and ``eval`` (scout.js), plus an explicit ``depth``
    and a ``mate``/``is_mate`` flag.
  * CODE-02-16 -- untyped ``{"error": ...}`` frames silently froze the eval bar;
    errors must be a typed, *terminal* event.
  * CODE-02-09 -- one Stockfish process per request; the concurrency cap must be
    real and enforced, with reuse and guaranteed cleanup.

No test in this file spawns a real Stockfish process: ``chess.engine.popen_uci``
and ``chess.engine.SimpleEngine.popen_uci`` are replaced with counted stubs.
"""
import asyncio
import os
import sys
import subprocess
import threading
import unittest

import tests.test_setup  # noqa: F401  -- MUST be first: isolated temp SQLite DB

import chess
import chess.engine

from backend.evaluator import (
    AsyncEngineEvaluator,
    AsyncEnginePool,
    EngineBusy,
    STOCKFISH_PATH,
    SyncEnginePool,
    build_error_event,
    build_eval_event,
    centipawn_to_height,
    eval_stream_contract,
    format_sse,
    sanitize_error,
)
from backend.analyzer import GameAnalyzer


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
class FakeTransport:
    def __init__(self, pid=4242, alive=True):
        self._pid = pid
        self._alive = alive

    def is_closing(self):
        return not self._alive

    def get_returncode(self):
        return None if self._alive else 1

    def get_pid(self):
        return self._pid

    def kill(self):
        self._alive = False


class _FakeAnalysis:
    """Mirror of ``chess.engine.AnalysisResult``: awaitable, *sync* context manager,
    async iterator.  One snapshot of the info list per analysis, so a search
    always terminates."""

    def __init__(self, engine, infos):
        self._engine = engine
        self._infos = list(infos)

    def __await__(self):
        async def _identity():
            return self
        return _identity().__await__()

    def __enter__(self):
        if self._engine.fail_with is not None:
            raise self._engine.fail_with
        return self

    def __exit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)  # real suspension point (cancellation can land here)
        if self._engine.fail_with is not None:
            raise self._engine.fail_with
        if not self._infos:
            raise StopAsyncIteration
        return self._infos.pop(0)


class FakeAsyncEngine:
    def __init__(self, info_factory, fail_with=None, pid=9001):
        self._info_factory = info_factory
        self.fail_with = fail_with
        self.transport = FakeTransport(pid=pid)
        self.configured = None
        self.quit_called = False
        self.close_called = False
        self.analysis_count = 0

    def infos(self):
        return self._info_factory()

    async def configure(self, options):
        self.configured = dict(options)

    async def ping(self):
        if not self.transport.is_closing():
            return True
        raise chess.engine.EngineTerminatedError("engine is dead")

    def analysis(self, board, limit):
        self.analysis_count += 1
        return _FakeAnalysis(self, self.infos())

    async def quit(self):
        self.quit_called = True
        self.transport.kill()

    async def close(self):
        self.close_called = True
        self.transport.kill()


class FakeSyncEngine:
    def __init__(self, pid=8001):
        self.transport = FakeTransport(pid=pid)
        self.configured = None
        self.quit_called = False
        self.close_called = False

    def configure(self, options):
        self.configured = dict(options)
        return "ok"

    def ping(self):
        if self.transport.is_closing():
            raise chess.engine.EngineTerminatedError("engine is dead")
        return None

    def analyse(self, board, limit):
        return {"score": chess.engine.PovScore(chess.engine.Cp(20), chess.WHITE), "pv": []}

    def quit(self):
        self.quit_called = True
        self.transport.kill()

    def close(self):
        self.close_called = True
        self.transport.kill()


def cp_infos(pairs, pv_move="e2e4"):
    """``[(depth, centipawns), ...]`` -> a fresh list of engine info dicts."""
    def factory():
        board = chess.Board()
        move = chess.Move.from_uci(pv_move)
        return [
            {
                "depth": depth,
                "score": chess.engine.PovScore(chess.engine.Cp(cp), chess.WHITE),
                "pv": [move],
                "seldepth": depth,
            }
            for depth, cp in pairs
        ]
    return factory


def mate_infos(pairs, mate_distance=3):
    def factory():
        board = chess.Board()
        move = chess.Move.from_uci("e2e4")
        return [
            {
                "depth": depth,
                "score": chess.engine.PovScore(chess.engine.Mate(mate_distance), chess.WHITE),
                "pv": [move],
            }
            for depth, _ in pairs
        ]
    return factory


class Patches:
    """Context manager replacing the two real process-spawn sites with counters."""

    def __init__(self, async_factory, sync_factory=None):
        self._async_factory = async_factory
        self._sync_factory = sync_factory
        self.async_spawns = 0
        self.sync_spawns = 0
        self._saved = []

    def __enter__(self):
        patches = self

        async def fake_popen_uci(command, *a, **kw):
            patches.async_spawns += 1
            return (None, patches._async_factory())

        def fake_sync_popen_uci(command, *a, **kw):
            patches.sync_spawns += 1
            return patches._sync_factory()

        self._saved = [
            (chess.engine, "popen_uci", chess.engine.popen_uci),
            (chess.engine.SimpleEngine, "popen_uci", chess.engine.SimpleEngine.popen_uci),
        ]
        chess.engine.popen_uci = fake_popen_uci
        chess.engine.SimpleEngine.popen_uci = fake_sync_popen_uci
        return self

    def __exit__(self, *exc):
        for obj, name, original in self._saved:
            setattr(obj, name, original)
        return False


def parse_frame(frame: str):
    """Parse one rendered SSE block back into (event_name, payload)."""
    assert frame.endswith("\n\n"), f"SSE block must end with a blank line: {frame!r}"
    lines = frame.strip("\n").split("\n")
    data_lines = [l[6:] for l in lines if l.startswith("data: ")]
    event_lines = [l[7:] for l in lines if l.startswith("event: ")]
    assert len(data_lines) == 1, f"exactly one data line expected, got {frame!r}"
    import json
    return (event_lines[0] if event_lines else None), json.loads(data_lines[0])


def run(coro):
    return asyncio.run(coro)


def collect(stream):
    async def _go():
        return [frame async for frame in stream]
    return run(_go())


# ---------------------------------------------------------------------------
# CODE-02-23: payload key contract
# ---------------------------------------------------------------------------
class TestEvalPayloadContract(unittest.TestCase):
    def test_every_frame_exposes_score_cp_and_eval_alias(self):
        """scout.js reads data.eval, repertoire_builder.js reads data.score_cp."""
        with Patches(lambda: FakeAsyncEngine(cp_infos([(4, 30), (6, 55)]))) as p:
            ev = AsyncEngineEvaluator(
                STOCKFISH_PATH, pool=AsyncEnginePool(STOCKFISH_PATH, max_size=1, wait_timeout=1.0)
            )
            frames = collect(ev.stream_eval(chess.STARTING_FEN, max_depth=6))
        self.assertGreaterEqual(len(frames), 2, p.async_spawns)
        for frame in frames:
            name, data = parse_frame(frame)
            self.assertIn("score_cp", data)
            self.assertIn("eval", data, "scout.js reads data.eval -- it must exist")
            self.assertEqual(data["eval"], data["score_cp"], "eval must alias score_cp")
            self.assertEqual(data["type"], "eval")
            self.assertIsInstance(data["depth"], int)
            self.assertIn("mate", data)
            self.assertIsInstance(data["is_mate"], bool)
            self.assertIn("final", data)
            self.assertIn("v", data)
            self.assertEqual(data["v"], eval_stream_contract()["version"])
            self.assertIn(name, ("eval", "eval_final"))
        self.assertEqual(parse_frame(frames[-1])[0], "eval_final")
        self.assertTrue(parse_frame(frames[-1])[1]["final"])

    def test_simulated_scout_eval_bar_reader_now_receives_a_value(self):
        """Replay scout.js `_updateEvalBar(data.eval, data.mate)` on a real frame."""
        with Patches(lambda: FakeAsyncEngine(cp_infos([(4, 220)]))):
            ev = AsyncEngineEvaluator(
                STOCKFISH_PATH, pool=AsyncEnginePool(STOCKFISH_PATH, max_size=1, wait_timeout=1.0)
            )
            frame = collect(ev.stream_eval(chess.STARTING_FEN, max_depth=4))[0]
        _name, data = parse_frame(frame)
        # Exactly what frontend/scout.js:1950 does.
        score_cp, mate = data["eval"], data["mate"]
        self.assertIsNotNone(score_cp, "scout.js would render a frozen +0.0 bar")
        pct = 50 + 50 * (2 / (1 + math_exp_js(-0.004 * score_cp)) - 1)
        self.assertGreater(pct, 50, "white to move and +2.2 must tilt the bar white")
        if mate is None:
            self.assertTrue(5 <= pct <= 95)

    def test_mate_score_is_flagged_not_silently_a_huge_cp_value(self):
        with Patches(lambda: FakeAsyncEngine(mate_infos([(4, 0), (6, 0)], mate_distance=3))):
            ev = AsyncEngineEvaluator(
                STOCKFISH_PATH, pool=AsyncEnginePool(STOCKFISH_PATH, max_size=1, wait_timeout=1.0)
            )
            frames = collect(ev.stream_eval(chess.STARTING_FEN, max_depth=6))
        for frame in frames:
            _name, data = parse_frame(frame)
            self.assertTrue(data["is_mate"])
            self.assertEqual(data["mate"], 3)
            self.assertEqual(data["score_cp"], 10000)  # saturated stand-in
            self.assertEqual(data["eval"], 10000)

    def test_terminal_checkmate_frame_is_typed(self):
        mate_fen = "7k/6Q1/6K1/8/8/8/8/8 b - - 0 1"  # black is checkmated
        with Patches(lambda: FakeAsyncEngine(cp_infos([(4, 0)]))):
            ev = AsyncEngineEvaluator(
                STOCKFISH_PATH, pool=AsyncEnginePool(STOCKFISH_PATH, max_size=1, wait_timeout=1.0)
            )
            frames = collect(ev.stream_eval(mate_fen, max_depth=6))
        self.assertEqual(len(frames), 1)
        name, data = parse_frame(frames[0])
        self.assertEqual(name, "eval_final")
        self.assertTrue(data["final"])
        self.assertTrue(data["is_mate"])
        self.assertEqual(data["mate"], 1, "White delivers mate => positive, White POV")
        self.assertEqual(data["score_cp"], 10000)
        self.assertEqual(data["eval"], 10000)
        self.assertEqual(data["depth"], 99)

    def test_stalemate_frame_is_typed(self):
        stale_fen = "7k/5Q2/6K1/8/8/8/8/8 b - - 0 1"  # black is stalemated
        with Patches(lambda: FakeAsyncEngine(cp_infos([(4, 0)]))):
            ev = AsyncEngineEvaluator(
                STOCKFISH_PATH, pool=AsyncEnginePool(STOCKFISH_PATH, max_size=1, wait_timeout=1.0)
            )
            frames = collect(ev.stream_eval(stale_fen, max_depth=6))
        _name, data = parse_frame(frames[0])
        self.assertEqual(data["score_cp"], 0)
        self.assertEqual(data["eval"], 0)
        self.assertIsNone(data["mate"])
        self.assertFalse(data["is_mate"])
        self.assertTrue(data["final"])
        self.assertEqual(data["depth"], 99)

    def test_cached_replay_keeps_the_same_contract(self):
        with Patches(lambda: FakeAsyncEngine(cp_infos([(4, 30), (6, 55)]))):
            ev = AsyncEngineEvaluator(
                STOCKFISH_PATH, pool=AsyncEnginePool(STOCKFISH_PATH, max_size=1, wait_timeout=1.0)
            )
            first = collect(ev.stream_eval(chess.STARTING_FEN, max_depth=6))
            second = collect(ev.stream_eval(chess.STARTING_FEN, max_depth=6))
        _n, cached = parse_frame(second[0])
        self.assertTrue(cached["cached"])
        self.assertIn("eval", cached)
        self.assertEqual(cached["eval"], cached["score_cp"])
        _n, live = parse_frame(first[0])
        self.assertFalse(live["cached"])

    def test_build_eval_event_saturates_and_keeps_alias_equal(self):
        payload = build_eval_event(depth=9, score_cp=999999, mate=None, final=True)
        self.assertEqual(payload["score_cp"], 10000)
        self.assertEqual(payload["eval"], payload["score_cp"])
        payload = build_eval_event(depth=9, score_cp=None, mate=-4)
        self.assertEqual(payload["score_cp"], -10000)
        self.assertTrue(payload["is_mate"])
        self.assertEqual(payload["mate"], -4)

    def test_sse_framing_puts_data_first_and_terminates_with_blank_line(self):
        """repertoire_builder.js does block.trim().startsWith('data: ')."""
        frame = format_sse(build_eval_event(depth=5, score_cp=10), event="eval")
        self.assertTrue(frame.startswith("data: "), frame)
        self.assertTrue(frame.endswith("\n\n"), repr(frame))
        self.assertIn("\nevent: eval\n\n", frame)
        self.assertNotIn("\n\n\n", frame)

    def test_contract_self_description(self):
        contract = eval_stream_contract()
        for field in ("score_cp", "eval", "depth", "mate", "is_mate", "final", "type"):
            self.assertIn(field, contract["eval_fields"])
        for code in ("invalid_fen", "engine_unavailable", "engine_busy", "engine_error"):
            self.assertIn(code, contract["error_codes"])


def math_exp_js(x):
    import math
    try:
        return math.exp(x)
    except OverflowError:
        return float("inf") if x > 0 else 0.0


# ---------------------------------------------------------------------------
# CODE-02-16: typed, terminal error events
# ---------------------------------------------------------------------------
class TestTypedErrorEvents(unittest.TestCase):
    def test_invalid_fen_emits_typed_terminal_event(self):
        ev = AsyncEngineEvaluator(
            STOCKFISH_PATH, pool=AsyncEnginePool(STOCKFISH_PATH, max_size=1, wait_timeout=1.0)
        )
        frames = collect(ev.stream_eval("P7/8/8/8/8/8/8/k6K w - - 0 1", max_depth=8))
        self.assertEqual(len(frames), 1, "an error must terminate the stream")
        name, data = parse_frame(frames[0])
        self.assertEqual(name, "error")
        self.assertEqual(data["type"], "error")
        self.assertEqual(data["code"], "invalid_fen")
        self.assertTrue(data["final"], "client must be able to stop waiting")
        self.assertIn("error", data)

    def test_engine_crash_emits_typed_terminal_event_and_retires_the_engine(self):
        boom = chess.engine.EngineTerminatedError("engine died mid-search")

        def factory():
            return FakeAsyncEngine(cp_infos([(4, 30)]), fail_with=boom)

        with Patches(factory) as p:
            pool = AsyncEnginePool(STOCKFISH_PATH, max_size=1, wait_timeout=1.0)
            ev = AsyncEngineEvaluator(STOCKFISH_PATH, pool=pool)
            frames = collect(ev.stream_eval(chess.STARTING_FEN, max_depth=8))
        self.assertEqual(p.async_spawns, 1)
        name, data = parse_frame(frames[-1])
        self.assertEqual(name, "error")
        self.assertEqual(data["type"], "error")
        self.assertEqual(data["code"], "engine_error")
        self.assertTrue(data["final"])
        self.assertEqual(pool.in_use, 0, "the engine slot must be handed back")
        self.assertLessEqual(pool.peak_in_use, pool.max_size)
        # The engine is not silently handed to the next caller.
        self.assertEqual(len(pool._free), 0)

    def test_error_message_never_leaks_a_filesystem_path(self):
        leaky = FileNotFoundError(f"Stockfish binary not found at {STOCKFISH_PATH}")
        ev = AsyncEngineEvaluator(
            "/nonexistent/bin/stockfish",
            pool=AsyncEnginePool("/nonexistent/bin/stockfish", max_size=1, wait_timeout=1.0),
        )
        frames = collect(ev.stream_eval(chess.STARTING_FEN, max_depth=4))
        self.assertEqual(len(frames), 1)
        _name, data = parse_frame(frames[0])
        self.assertEqual(data["code"], "engine_unavailable")
        self.assertTrue(data["final"])
        self.assertNotIn(STOCKFISH_PATH, data["error"])
        self.assertNotIn("/nonexistent", data["error"])
        self.assertNotIn("/", data["error"], "path redaction must leave no slashes")
        self.assertNotIn("\n", data["error"], "error text must not break SSE framing")
        self.assertTrue(data["error"])
        self.assertNotIn("/home", sanitize_error(leaky))

    def test_error_frames_carry_the_full_field_set(self):
        payload = build_error_event("engine_busy", "pool saturated", fen="abc")
        for field in ("v", "type", "fen", "depth", "score_cp", "eval", "mate",
                      "is_mate", "best_move", "best_san", "final", "cached",
                      "code", "error"):
            self.assertIn(field, payload, field)
        self.assertTrue(payload["final"])
        self.assertIsNone(payload["score_cp"])


# ---------------------------------------------------------------------------
# CODE-02-09: bounded engine pool
# ---------------------------------------------------------------------------
class TestAsyncEnginePoolCap(unittest.TestCase):
    def test_third_concurrent_stream_is_rejected_instead_of_queueing(self):
        """max_size=1 + a stuck reader => bounded, typed rejection (no hang)."""
        import time as _time

        async def scenario():
            pool = AsyncEnginePool(STOCKFISH_PATH, max_size=1, wait_timeout=0.01)
            with Patches(lambda: FakeAsyncEngine(cp_infos([(4, 10)]))):
                ev = AsyncEngineEvaluator(STOCKFISH_PATH, pool=pool)
                held = await pool.acquire()          # slot 1 taken by a "stuck" reader
                started = _time.monotonic()
                frames = [f async for f in ev.stream_eval(chess.STARTING_FEN, max_depth=6)]
                elapsed = _time.monotonic() - started
                pool.release_slot(held)
            return frames, pool, elapsed

        frames, pool, elapsed = run(scenario())
        self.assertLess(elapsed, 3.0, "the wait must be bounded, not unbounded")
        self.assertEqual(len(frames), 1)
        name, data = parse_frame(frames[0])
        self.assertEqual(name, "error")
        self.assertEqual(data["code"], "engine_busy")
        self.assertTrue(data["final"])
        self.assertEqual(pool.peak_in_use, 1)
        self.assertEqual(pool.in_use, 0)

    def test_cap_holds_under_real_concurrency(self):
        """12 concurrent streams, max_size=2 => at most 2 engines ever alive."""

        async def scenario():
            pool = AsyncEnginePool(STOCKFISH_PATH, max_size=2, wait_timeout=5.0)
            with Patches(lambda: FakeAsyncEngine(cp_infos([(4, 10), (6, 20)]))) as p:
                ev = AsyncEngineEvaluator(STOCKFISH_PATH, pool=pool)

                async def one(i):
                    return [f async for f in ev.stream_eval(chess.STARTING_FEN, max_depth=6)]

                results = await asyncio.gather(*[one(i) for i in range(12)])
            return pool, p, results

        pool, p, results = run(scenario())
        self.assertEqual(len(results), 12)
        ok = 0
        for frames in results:
            if frames:
                name, data = parse_frame(frames[-1])
                if data.get("type") == "eval":
                    ok += 1
                elif data.get("code") == "engine_busy":
                    self.assertTrue(data["final"])
                else:
                    self.fail(f"unexpected terminal frame: {data}")
        self.assertEqual(ok, 12, "no request should be dropped at max_size=2/timeout=5")
        self.assertLessEqual(pool.peak_in_use, 2)
        self.assertLessEqual(p.async_spawns, 2, "process count must be capped")
        self.assertEqual(pool.in_use, 0)

    def test_engine_is_reused_not_respawned(self):
        with Patches(lambda: FakeAsyncEngine(cp_infos([(4, 10), (6, 20)]))) as p:
            pool = AsyncEnginePool(STOCKFISH_PATH, max_size=1, wait_timeout=1.0)
            ev = AsyncEngineEvaluator(STOCKFISH_PATH, pool=pool)
            for _ in range(5):
                collect(ev.stream_eval(chess.STARTING_FEN, max_depth=6))
            self.assertEqual(p.async_spawns, 1, "a healthy engine must be recycled")
            self.assertEqual(pool.in_use, 0)
            self.assertEqual(pool.live, 1)

    def test_disconnect_releases_the_slot_and_retires_the_engine(self):
        """Client vanishing mid-search must not leak a slot or a searching engine."""

        async def scenario():
            pool = AsyncEnginePool(STOCKFISH_PATH, max_size=1, wait_timeout=0.3)
            with Patches(lambda: FakeAsyncEngine(cp_infos([(4, 10), (6, 20), (9, 30)]))) as p:
                ev = AsyncEngineEvaluator(STOCKFISH_PATH, pool=pool)
                gen = ev.stream_eval(chess.STARTING_FEN, max_depth=12)
                first = await gen.__anext__()
                pending = asyncio.create_task(gen.__anext__())
                await asyncio.sleep(0)
                pending.cancel()
                try:
                    await pending
                except asyncio.CancelledError:
                    pass
                try:
                    await gen.aclose()
                except Exception:
                    pass
                after = pool.in_use
                free_after_cancel = len(pool._free)
                retired_after_cancel = len(pool._retired)
                # Next request must succeed and must NOT reuse a mid-search engine.
                frames = [f async for f in ev.stream_eval(chess.STARTING_FEN, max_depth=6)]
            return first, after, free_after_cancel, retired_after_cancel, pool, p, frames

        first, in_use, free, retired, pool, p, frames = run(scenario())
        self.assertTrue(first.startswith("data: "))
        self.assertEqual(in_use, 0, "engine slot leaked on client disconnect")
        self.assertEqual(free, 0, "a mid-search engine must be retired, not recycled")
        self.assertEqual(retired, 1)
        self.assertTrue(frames, "the pool must still serve requests after a disconnect")
        self.assertEqual(parse_frame(frames[-1])[1]["type"], "eval")

    def test_abandoned_generator_releases_the_slot(self):
        """Generator dropped without aclose() must still free the semaphore slot."""

        async def scenario():
            pool = AsyncEnginePool(STOCKFISH_PATH, max_size=1, wait_timeout=0.3)
            with Patches(lambda: FakeAsyncEngine(cp_infos([(4, 10), (6, 20), (9, 30)]))):
                ev = AsyncEngineEvaluator(STOCKFISH_PATH, pool=pool)
                gen = ev.stream_eval(chess.STARTING_FEN, max_depth=12)
                await gen.__anext__()
                del gen
                import gc
                gc.collect()
                await asyncio.sleep(0)
                return pool

        pool = run(scenario())
        self.assertEqual(pool.in_use, 0)
        self.assertEqual(pool._sem._value, 1, "semaphore slot leaked")


class TestSyncEnginePoolCap(unittest.TestCase):
    def test_cap_is_enforced_and_saturation_raises_engine_busy(self):
        with Patches(lambda: FakeAsyncEngine(cp_infos([])), sync_factory=FakeSyncEngine) as p:
            pool = SyncEnginePool(STOCKFISH_PATH, max_size=2, wait_timeout=0.01)
            e1 = pool.acquire()
            e2 = pool.acquire()
            with self.assertRaises(EngineBusy) as ctx:
                pool.acquire()
            self.assertIn("saturated", str(ctx.exception))
            self.assertEqual(p.sync_spawns, 2, "the cap must limit process spawns")
            self.assertEqual(pool.peak_in_use, 2)
            pool.release(e1)
            e3 = pool.acquire()
            self.assertEqual(p.sync_spawns, 2, "released engines must be recycled")
            pool.release(e2)
            pool.release(e3)
            pool.close_all()
            self.assertTrue(e1.quit_called and e2.quit_called and e3.quit_called)

    def test_cap_holds_under_threaded_concurrency(self):
        """Replaces the old 'N concurrent requests == N processes' behaviour."""
        with Patches(lambda: FakeAsyncEngine(cp_infos([])), sync_factory=FakeSyncEngine) as p:
            pool = SyncEnginePool(STOCKFISH_PATH, max_size=2, wait_timeout=10.0)
            errors = []
            barrier = threading.Barrier(12)

            def worker():
                try:
                    barrier.wait(timeout=10)
                    for _ in range(6):
                        with pool.engine() as engine:
                            engine.ping()
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
            self.assertEqual(errors, [])
            self.assertLessEqual(pool.peak_in_use, 2)
            self.assertLessEqual(p.sync_spawns, 2, f"spawned {p.sync_spawns} engines")
            pool.close_all()

    def test_missing_binary_keeps_the_legacy_filenotfounderror_contract(self):
        pool = SyncEnginePool("/nonexistent/stockfish", max_size=1, wait_timeout=0.3)
        with self.assertRaises(FileNotFoundError):
            pool.acquire()
        self.assertEqual(pool.in_use, 0)
        self.assertEqual(pool._sem._value, 1, "failed checkout must not leak the slot")

    def test_dead_engine_is_replaced_on_checkout(self):
        with Patches(lambda: FakeAsyncEngine(cp_infos([])), sync_factory=FakeSyncEngine) as p:
            pool = SyncEnginePool(STOCKFISH_PATH, max_size=1, wait_timeout=0.5)
            first = pool.acquire()
            first.transport.kill()  # simulate kill -9
            pool.release(first)
            second = pool.acquire()
            self.assertIsNot(second, first)
            self.assertEqual(p.sync_spawns, 2)
            pool.release(second)
            pool.close_all()

    def test_context_manager_discards_on_error(self):
        with Patches(lambda: FakeAsyncEngine(cp_infos([])), sync_factory=FakeSyncEngine) as p:
            pool = SyncEnginePool(STOCKFISH_PATH, max_size=1, wait_timeout=0.5)
            captured = {}
            with self.assertRaises(RuntimeError):
                with pool.engine() as engine:
                    captured["engine"] = engine
                    raise RuntimeError("boom")
            self.assertEqual(pool.in_use, 0)
            self.assertTrue(captured["engine"].quit_called)
            self.assertEqual(p.sync_spawns, 1)


class TestGameAnalyzerUsesThePool(unittest.TestCase):
    def test_repeated_analyzers_share_one_process(self):
        """GameAnalyzer used to spawn a fresh Stockfish per instance."""
        with Patches(lambda: FakeAsyncEngine(cp_infos([])), sync_factory=FakeSyncEngine) as p:
            from backend import evaluator as evaluator_mod
            pool = SyncEnginePool(STOCKFISH_PATH, max_size=2, wait_timeout=1.0)
            original = evaluator_mod._sync_pool
            evaluator_mod._sync_pool = pool
            try:
                seen = []
                for _ in range(5):
                    analyzer = GameAnalyzer()
                    seen.append(analyzer._get_engine())
                    analyzer.close()
                self.assertEqual(len({id(e) for e in seen}), 1, "engine must be recycled")
                self.assertEqual(p.sync_spawns, 1)
                self.assertEqual(pool.in_use, 0)
            finally:
                evaluator_mod._sync_pool = original
                pool.close_all()

    def test_close_is_idempotent_and_reusable(self):
        with Patches(lambda: FakeAsyncEngine(cp_infos([])), sync_factory=FakeSyncEngine):
            from backend import evaluator as evaluator_mod
            pool = SyncEnginePool(STOCKFISH_PATH, max_size=2, wait_timeout=1.0)
            original = evaluator_mod._sync_pool
            evaluator_mod._sync_pool = pool
            try:
                analyzer = GameAnalyzer()
                analyzer._get_engine()
                analyzer.close()
                analyzer.close()  # must not raise
                self.assertIsNotNone(analyzer._get_engine())
                analyzer.close()
                self.assertEqual(pool.in_use, 0)
            finally:
                evaluator_mod._sync_pool = original
                pool.close_all()


# ---------------------------------------------------------------------------
# AGENTS.md section 4C: centipawn -> height mapping
# ---------------------------------------------------------------------------
class TestCentipawnMapping(unittest.TestCase):
    def test_sign_and_centre(self):
        self.assertAlmostEqual(centipawn_to_height(0), 50.0, places=6)
        self.assertGreater(centipawn_to_height(150), 50.0)
        self.assertLess(centipawn_to_height(-150), 50.0)
        self.assertAlmostEqual(
            centipawn_to_height(200), 50 + 50 * (2 / (1 + math_exp_js(-0.8)) - 1), places=6
        )

    def test_scale_and_saturation(self):
        self.assertAlmostEqual(centipawn_to_height(10000), 100.0, places=3)
        self.assertAlmostEqual(centipawn_to_height(-10000), 0.0, places=3)
        self.assertAlmostEqual(centipawn_to_height(40000), 100.0, places=3)
        self.assertAlmostEqual(centipawn_to_height(-40000), 0.0, places=3)
        # +1.0 pawn must be visibly ahead but not saturated.
        self.assertTrue(50.0 < centipawn_to_height(100) < 70.0)

    def test_malformed_input_never_raises(self):
        for bad in (None, float("nan"), "x", object()):
            value = centipawn_to_height(bad)
            self.assertTrue(0.0 <= value <= 100.0)


# ---------------------------------------------------------------------------
# BUILDER-14: the wire format must be parseable by the SHIPPED clients
# ---------------------------------------------------------------------------
class TestShippedClientCanParseTheStream(unittest.TestCase):
    """A contract is only real if the code that consumes it can read it.

    BUILDER-14 (CRITICAL, ui-regression): ``_frame()`` renders every SSE block as
    ``data: {...}`` FOLLOWED BY ``event: <name>``.  ``repertoire_builder.js``
    did ``block.trim().startsWith('data: ')`` and then
    ``JSON.parse(block.trim().slice(6))`` -- a *prefix* test followed by a parse
    that requires the block to be a SINGLE ``data:`` line.  Every real frame
    therefore threw ``SyntaxError`` inside a bare ``catch (e) {}`` and the live
    eval bar never moved: 0 of 917 captured bytes were rendered.

    The pre-existing test ``test_sse_framing_puts_data_first_and_terminates_with_blank_line``
    could not catch it: it asserts the wire format, and this module's
    ``parse_frame()`` helper is line-wise, so it parses the two-line block happily.
    The only sound check is to run the *shipped* parse shape -- and, when Node is
    available, the shipped ``startEvalStream()`` itself -- over the real bytes.
    """

    #: The exact data-extraction shape used by frontend/scout.js:2813-2821 and,
    #: after BUILDER-14, frontend/repertoire_builder.js.  A block is a *sequence*
    #: of `field: value` lines, not one `data:` line, so the data line has to be
    #: picked out of it.
    CLIENT_PARSE_JS = """
        function parseBlocks(text) {
            const out = [], errors = [];
            for (const block of text.split('\\n\\n')) {
                const line = block.split('\\n').find(l => l.startsWith('data: '));
                if (!line) continue;
                try { out.push(JSON.parse(line.slice(6))); }
                catch (e) { errors.push(String(e)); }
            }
            return { frames: out, errors: errors };
        }
        return parseBlocks(TEXT);
    """

    def _parse_with_shipped_client(self, text: str) -> dict:
        import json
        from tests import frontend_runtime
        js = "const TEXT = %s;\n%s" % (json.dumps(text), self.CLIENT_PARSE_JS)
        payload = frontend_runtime.run_scenario(js, timeout=60)
        return {k: v for k, v in payload.items() if k != "__harness"}

    def test_a_block_is_a_sequence_of_lines_with_exactly_one_data_line(self):
        """The shape every client may rely on: one `data:` line, then `event:`.

        Guards the producer against emitting a second `data:` line (a multi-line
        JSON payload would need continuation lines, which neither client joins).
        """
        for payload, event, code in (
            (build_eval_event(depth=7, score_cp=42, final=True), "eval_final", None),
            (build_error_event("engine_unavailable", "Stockfish binary not found at <path>"),
             "error", "engine_unavailable"),
        ):
            frame = format_sse(payload, event=event)
            self.assertTrue(frame.startswith("data: "), frame)
            self.assertIn("\nevent: " + event + "\n\n", frame)
            data_lines = [l for l in frame.strip("\n").split("\n") if l.startswith("data: ")]
            self.assertEqual(len(data_lines), 1, frame)
            result = self._parse_with_shipped_client(frame)
            self.assertEqual(result["errors"], [], "the documented client shape must not raise")
            self.assertEqual(len(result["frames"]), 1, result)
            if code:
                self.assertEqual(result["frames"][0]["code"], code)
                self.assertTrue(result["frames"][0]["final"])

    def test_the_builder_does_not_parse_the_whole_block_as_one_data_line(self):
        """Source-level guard on the exact defect BUILDER-14 was.

        The builder used `JSON.parse(trimmed.slice(6))` on the *whole* trimmed
        block, which only works if `data:` is the block's only line. Asserting on
        the source (rather than on a re-implementation of it) is what makes this
        test able to go red when the shipped parser regresses.
        """
        from tests import frontend_runtime
        src = frontend_runtime.frontend_source("frontend/repertoire_builder.js")
        lines = src.split("\n")
        needle = "JSON.parse(trimmed.slice(6))"
        offenders = [i + 1 for i, l in enumerate(lines) if needle in l]
        self.assertEqual(
            offenders, [],
            "BUILDER-14: repertoire_builder.js:%s parses an entire SSE block as one "
            "`data:` line. Every real frame also carries an `event:` line, so the parse "
            "raises and the frame is dropped inside a bare `catch (e) {}` -- the live eval "
            "bar never moves. Extract the data line out of the block first, the way "
            "frontend/scout.js does." % (offenders or "?"),
        )
        self.assertTrue(
            any("find(l => l.startsWith('data: '))" in l for l in lines),
            "the builder must pick the data line out of the block, the way "
            "frontend/scout.js:2813-2821 does",
        )

    @unittest.skipUnless(
        __import__("shutil").which("node"), "Node.js is required to execute the shipped frontend"
    )
    def test_real_startEvalStream_renders_every_real_frame(self):
        """The end-to-end version: real bytes -> real ``startEvalStream()``.

        Only ``fetch`` is replaced (by a reader over the real bytes the server
        produces); the parsing, the cache write and the render call are the
        shipped ones.  A canary payload proves a 0 cannot be the stub's fault.
        """
        import json
        from tests import frontend_runtime
        real = "".join(
            format_sse(build_eval_event(depth=d, score_cp=d * 7, final=(d == 5)),
                       event="eval_final" if d == 5 else "eval")
            for d in range(1, 6)
        )
        canary = 'data: {"v":1,"type":"eval","depth":1,"score_cp":42,"eval":42,"mate":null}\n\n'
        scenario = """
        return (async () => {
        const FEN = %(fen)s;
        const CANARY = %(canary)s;
        const REAL = %(real)s;
        const b = window.repertoireBuilder;
        b.evalCache = new Map(); b.evalCacheMax = 400;
        b.history = [{ ply: 0, fen: FEN, san: 'Start', uci: null, openingName: 'x', eco: '' }];
        b.currentStepIndex = 0;
        const renders = [], errors = [];
        b.renderEvalBar = (s, m, d) => renders.push([s, m, d]);
        b.showEvalStreamError = (d) => errors.push([d.code, d.error]);
        b._evalCachePut = (k, v) => v;
        const bytes = s => Array.from(s).map(c => c.charCodeAt(0));
        function reader(buf, size) {
            let i = 0;
            return { read() {
                if (i >= buf.length) return Promise.resolve({ done: true, value: undefined });
                const sl = buf.slice(i, i + size); i += sl.length;
                return Promise.resolve({ done: false, value: Uint8Array.from(sl) });
            } };
        }
        async function drive(text, size) {
            renders.length = 0; errors.length = 0;
            window.fetch = () => Promise.resolve(
                { ok: true, status: 200, body: { getReader: () => reader(bytes(text), size) } });
            await b.startEvalStream(FEN);
            return { renders: renders.slice(), errors: errors.slice() };
        }
        const canaryRun = await drive(CANARY, 11);
        const live = await drive(REAL, 17);
        return { canary: canaryRun.renders.length, renders: live.renders, errors: live.errors };
        })();
        """ % {
            "fen": json.dumps("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"),
            "canary": json.dumps(canary),
            "real": json.dumps(real),
        }
        result = {k: v for k, v in frontend_runtime.run_scenario(scenario, timeout=60).items()
                  if k != "__harness"}
        self.assertEqual(result["canary"], 1,
                         "canary: a single-line data: block must render, else the stub is broken")
        self.assertEqual(result["errors"], [], "a real stream contains no error frames here")
        self.assertEqual(len(result["renders"]), 5,
                         "BUILDER-14: the shipped startEvalStream() dropped real frames: %r"
                         % (result["renders"],))
        self.assertEqual([r[2] for r in result["renders"]], [1, 2, 3, 4, 5])


if __name__ == "__main__":
    unittest.main()
