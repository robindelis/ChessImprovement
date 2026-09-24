"""
CODE-2 follow-up #4: does AsyncEngineEvaluator._get_engine()'s liveness probe
work?  `transport.is_closing()` on an asyncio.subprocess.Process.

Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-2_engine.py
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST

import chess.engine


async def main():
    transport, engine = await chess.engine.popen_uci(
        "bin/stockfish/stockfish-linux-x86-64-universal")
    print("type(transport) =", type(transport).__mro__[:4])
    print("has is_closing  =", hasattr(transport, "is_closing"))
    print("has get_returncode =", hasattr(transport, "get_returncode"))
    try:
        r = transport.is_closing()
        print("transport.is_closing() ->", r)
    except Exception as e:
        print("transport.is_closing() -> %s: %s" % (type(e).__name__, e))
    print("protocol.transport is the same object:", engine.transport is transport)
    await engine.quit()
    if hasattr(transport, "close"):
        transport.close()


asyncio.run(main())
print("\nDONE")
