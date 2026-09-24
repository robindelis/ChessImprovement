"""[ATK3-RETEST] SYNC-03: the NDJSON stream reader, attacked much harder than in
round 1, plus every caller of the stream.

Cases: mixed valid/invalid, a valid line AFTER the bad one, a truncated final
line, a BOM, CRLF, an over-long line, duplicate ids, a non-dict root, a
1-element dict, a deeply nested dict, a `\x00` line, and a stream that raises
mid-body.

    /home/robin/miniconda3/bin/python3 audit/scripts/atk3_re03_ndjson_retest.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import (hdr, q, q1, get_db, wipe_games, FakeResponse, restore_network,  # noqa: E402
                      client, ensure_username)

import backend.analyzer as an  # noqa: E402
import backend.api as api  # noqa: E402
import backend.repertoire as rp  # noqa: E402
import backend.lichess_client as lc  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402

an.GameAnalyzer.analyze_game_openings = lambda self, gd: []
ensure_username("RobiDeli")
RESULTS = []


def verdict(name, ok, detail=""):
    RESULTS.append((name, "HOLDS" if ok else "BROKEN", detail))
    print("   -> %-6s %-56s %s" % ("HOLDS" if ok else "BROKEN", name, detail))


def g(gid, moves="e4 e5 Nf3"):
    return {"id": gid, "speed": "blitz", "rated": True, "createdAt": 1700000000000,
            "winner": "white", "opening": {"name": "Italian Game", "eco": "C50"},
            "players": {"white": {"user": {"name": "RobiDeli"}, "rating": 1800},
                        "black": {"user": {"name": "Rival"}, "rating": 1800}},
            "moves": moves}


def serve(lines, status=200):
    import types

    def _get(url, params=None, headers=None, timeout=None, stream=False, **kw):
        if "/api/games" in url:
            return FakeResponse(lines, status, "")
        raise lc._Exploder(url)
    lc.requests = types.SimpleNamespace(get=_get)


# ---------------------------------------------------------------------------
hdr("RE03-A  the reader itself: which lines survive `fetch_user_games_result`?")
# ---------------------------------------------------------------------------
client_obj = lc.LichessClient.__new__(lc.LichessClient)
client_obj.token = None
client_obj.base_url = "https://lichess.org"
client_obj.explorer_url = "https://explorer.lichess.org"
client_obj.headers = {"User-Agent": "probe"}

CASES = [
    ("plain valid", [json.dumps(g("a1"))]),
    ("valid AFTER an invalid line", [json.dumps(g("a1")), "{not json", json.dumps(g("a2"))]),
    ("invalid BEFORE a valid line", ["{not json", json.dumps(g("a1"))]),
    ("array root", ["[1,2,3]", json.dumps(g("a1"))]),
    ("string root", ['"hello"', json.dumps(g("a1"))]),
    ("number root", ["42", json.dumps(g("a1"))]),
    ("null root", ["null", json.dumps(g("a1"))]),
    ("true root", ["true", json.dumps(g("a1"))]),
    ("truncated final line", [json.dumps(g("a1")), '{"id":"a2","speed":"bl']),
    ("BOM at the start of a line", ["﻿" + json.dumps(g("a1"))]),
    ("BOM only on line 1 of 2", ["﻿" + json.dumps(g("a1")), json.dumps(g("a2"))]),
    ("CRLF terminators", [json.dumps(g("a1")) + "\r", json.dumps(g("a2")) + "\r"]),
    ("bare CR inside the line", [json.dumps(g("a1")).replace(",", ",\r")]),
    ("blank line between two good ones", [json.dumps(g("a1")), "", json.dumps(g("a2"))]),
    ("NUL byte inside the line", ['{"id":"a1","speed":"bl\x00itz"}']),
    ("1-element dict (no id)", [json.dumps({"speed": "blitz"})]),
    ("deeply nested dict", ['{"a":' * 40 + '1' + '}' * 40]),
    ("duplicate id twice in one batch", [json.dumps(g("a1")), json.dumps(g("a1"))]),
    ("three copies of one id", [json.dumps(g("a1"))] * 3),
    ("over-long line (>1 MB) FIRST",
     ["x" * (lc.MAX_NDJSON_LINE_BYTES + 10), json.dumps(g("a1"))]),
    ("over-long line LAST", [json.dumps(g("a1")), "x" * (lc.MAX_NDJSON_LINE_BYTES + 10)]),
    ("exactly MAX_NDJSON_LINE_BYTES",
     [json.dumps(dict(g("big"), note="y" * (lc.MAX_NDJSON_LINE_BYTES - 200)))]),
    ("5000 games + 1 more (response cap)",
     [json.dumps(g("c%04d" % i)) for i in range(lc.MAX_NDJSON_GAMES + 1)]),
]
for label, lines in CASES:
    serve(lines)
    try:
        games, err = client_obj.fetch_user_games_result("RobiDeli", max_games=0)
        ids = [x.get("id") if isinstance(x, dict) else "<%s>" % type(x).__name__ for x in games]
        shown = ids if len(ids) <= 4 else ids[:2] + ["...(%d)" % len(ids)] + ids[-1:]
        print("   %-34s -> %2d dicts %s  err=%r" % (label, len(games), shown, err[:40]))
    except Exception as exc:
        print("   %-34s -> RAISED %r" % (label, exc))
restore_network()

# ---------------------------------------------------------------------------
hdr("RE03-B  same streams, driven through POST /api/sync (per-game isolation?)")
# ---------------------------------------------------------------------------
cl = client()
SYNC_CASES = [
    ("mixed valid/invalid", [json.dumps(g("s1")), "{not json", json.dumps(g("s2"))]),
    ("array root mid-batch", [json.dumps(g("s1")), "[1,2,3]", json.dumps(g("s2"))]),
    ("truncated final line", [json.dumps(g("s1")), '{"id":"s2","sp']),
    ("BOM first line", ["﻿" + json.dumps(g("s1")), json.dumps(g("s2"))]),
    ("CRLF", [json.dumps(g("s1")) + "\r", json.dumps(g("s2")) + "\r"]),
    ("over-long line first", ["z" * (lc.MAX_NDJSON_LINE_BYTES + 5), json.dumps(g("s1"))]),
    ("dict with no id", [json.dumps({"speed": "blitz", "moves": "e4 e5"})]),
    ("'moves' is a list", [json.dumps(dict(g("m1"), moves=["e4", "e5"]))]),
    ("'moves' is a number", [json.dumps(dict(g("m1"), moves=7))]),
    ("'players' is a list", [json.dumps(dict(g("m1"), players=["a", "b"]))]),
    ("'winner' is a dict", [json.dumps(dict(g("m1"), winner={"a": 1}))]),
    ("'createdAt' is 'soon'", [json.dumps(dict(g("m1"), createdAt="soon"))]),
    ("'createdAt' is 1e30", [json.dumps(dict(g("m1"), createdAt=1e30))]),
    ("'createdAt' is -1", [json.dumps(dict(g("m1"), createdAt=-1))]),
    ("'opening' is a string", [json.dumps(dict(g("m1"), opening="Italian"))]),
    ("'speed' is a dict", [json.dumps(dict(g("m1"), speed={"k": 1}))]),
    ("id is an int", [json.dumps(dict(g("m1"), id=12345))]),
    ("id is a 5 MB string", [json.dumps(dict(g("m1"), id="z" * 5_000_000))]),
    ("duplicate id in one batch", [json.dumps(g("s1")), json.dumps(g("s1"))]),
    ("'rated' is the string 'false'", [json.dumps(dict(g("s1"), rated="false"))]),
]
for label, lines in SYNC_CASES:
    wipe_games()
    serve(lines)
    r = cl.post("/api/sync?max_games=50")
    restore_network()
    n = q1("SELECT COUNT(*) c FROM games")["c"]
    try:
        body = r.json()
    except Exception:
        body = {}
    print("   %-34s HTTP %s games=%d status=%r synced=%r failed=%r errors=%s" % (
        label, r.status_code, n, body.get("status"), body.get("synced_games"),
        body.get("failed_games"), str(body.get("errors"))[:70]))
    if r.status_code >= 500:
        verdict("POST /api/sync: %s" % label, False, "HTTP %d" % r.status_code)
wipe_games()

# ---------------------------------------------------------------------------
hdr("RE03-C  same streams, driven through the repertoire.py writers")
# ---------------------------------------------------------------------------
WRITER_CASES = SYNC_CASES + [
    ("stream ends mid-object", [json.dumps(g("s1")), '{"id":"s2"']),
]
for label, lines in WRITER_CASES:
    wipe_games()
    RepertoireManager._instance = None
    serve(lines)
    try:
        out = RepertoireManager.get_instance().sync_recent_games(max_games=50)
        note = "ok %s" % out.get("status")
    except Exception as exc:
        note = "RAISED %r" % (exc,)
    finally:
        restore_network()
    n = q1("SELECT COUNT(*) c FROM games")["c"]
    print("   %-34s %-42s games=%d" % (label, note, n))
    if "RAISED" in note:
        verdict("sync_recent_games: %s" % label, False, note)
wipe_games()

# ---------------------------------------------------------------------------
hdr("RE03-D  the SSE writers (they share the same INSERT, but a different loop)")
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402


def pump(meth, lines):
    serve(lines)
    rm = RepertoireManager.get_instance()
    gen = getattr(rm, meth)(max_games=50, request=None)

    async def run():
        out = []
        async for chunk in gen:
            out.append(json.loads(chunk[6:]) if chunk.startswith("data: ") else chunk)
        return out
    try:
        return asyncio.run(run())
    finally:
        restore_network()


for label, lines in SYNC_CASES:
    wipe_games()
    RepertoireManager._instance = None
    try:
        ev = pump("stream_sync_recent_games", lines)
        stages = [e.get("stage") for e in ev if isinstance(e, dict)]
    except Exception as exc:
        stages = ["RAISED %r" % (exc,)]
    n = q1("SELECT COUNT(*) c FROM games")["c"]
    print("   %-34s games=%d stages=%s" % (label, n, stages[-2:]))
wipe_games()

# ---------------------------------------------------------------------------
hdr("RE03-E  is the write transaction left OPEN when the batch aborts?")
# ---------------------------------------------------------------------------
# 1) reader-level: a stream that raises mid-body after some good lines.
wipe_games()
RepertoireManager._instance = None


class RaisingBody(FakeResponse):
    def iter_lines(self, decode_unicode=False):
        yield json.dumps(g("t1")).encode()
        yield json.dumps(g("t2")).encode()
        raise ConnectionResetError("peer closed the connection mid-body")


serve([])
lc.requests.get = lambda url, **kw: RaisingBody([], 200, "")
rm = RepertoireManager.get_instance()
import backend.repertoire as _rp  # noqa: E402
_orig_fetch = _rp.LichessClient.fetch_user_games_result


def boom_fetch(self, *a, **k):
    games, _ = _orig_fetch(self, *a, **k)
    raise ConnectionResetError("peer closed the connection mid-body")


_rp.LichessClient.fetch_user_games_result = boom_fetch
try:
    print("   sync_recent_games ->", rm.sync_recent_games(max_games=50))
except Exception as exc:
    print("   sync_recent_games RAISED %r" % (exc,))
_rp.LichessClient.fetch_user_games_result = _orig_fetch
restore_network()
print("   games rows after the aborted batch:", q1("SELECT COUNT(*) c FROM games")["c"])

# A *writer* that raises mid-loop: does a second writer block for 30 s?
wipe_games()


def exploding_book(self, fen):
    raise RuntimeError("explorer exploded")


rp.RepertoireManager.opening_book = None
orig_ident = rp.OpeningBook.identify_game_opening


def ident_boom(self, moves):
    raise RuntimeError("OpeningBook exploded")


rp.OpeningBook.identify_game_opening = ident_boom
RepertoireManager._instance = None
serve([json.dumps(dict(g("u1"), opening=None)), json.dumps(g("u2"))])
try:
    print("   sync_recent_games (book raises on game 1) ->",
          RepertoireManager.get_instance().sync_recent_games(max_games=50))
except Exception as exc:
    print("   sync_recent_games RAISED %r" % (exc,))
finally:
    rp.OpeningBook.identify_game_opening = orig_ident
    restore_network()
print("   games rows committed:", q1("SELECT COUNT(*) c FROM games")["c"],
      "(game 1 raised, so the whole batch should roll back)")
t0 = time.time()
wipe_games()
serve([json.dumps(g("u3"))])
out = RepertoireManager.get_instance().sync_recent_games(max_games=5)
print("   next sync after the aborted one: %s in %.2fs" % (out, time.time() - t0))
wipe_games()

# ---------------------------------------------------------------------------
hdr("SUMMARY")
# ---------------------------------------------------------------------------
for name, status, detail in RESULTS:
    print("  %-8s %-56s %s" % (status, name, detail))
print("\n%d/%d hold" % (sum(1 for r in RESULTS if r[1] == "HOLDS"), len(RESULTS)))
