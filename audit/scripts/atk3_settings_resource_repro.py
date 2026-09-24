"""ATK3-14 : /api/settings + Lichess token handling (plaintext, size, leakage).
ATK3-15 : resource bounds — max_games enforcement on the analyze paths, SSE stream lifecycle."""
import os
import sys
import json
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import hdr, wipe_games, q, q1, fake_lichess_games, restore_network, client  # noqa: E402

cl = client()
import backend.lichess_client as lc  # noqa: E402
import backend.db as dbm  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402
rm = RepertoireManager.get_instance()

hdr("ATK3-14a — can the token be read back through any API?")
r = cl.post("/api/settings", json={"lichess_token": "lip_SECRETTOKEN123",
                                    "lichess_username": "RobiDeli"})
print("  POST /api/settings ->", r.status_code, r.text[:120])
print("  GET  /api/settings ->", cl.get("/api/settings").text)
print("  GET  /api/account  ->", cl.get("/api/account").text[:200])
stored = q1("SELECT value FROM settings WHERE key='lichess_token'")["value"]
print("  raw settings table :", stored, "<- stored in PLAINTEXT")
blob = cl.get("/api/settings").text + cl.get("/api/account").text
print("  token echoed by any GET /api/* ?", "lip_SECRETTOKEN123" in blob)

hdr("ATK3-14b — token validation and size")
for payload, label in [({"lichess_token": ""}, "empty string"),
                       ({"lichess_token": "not-a-lichess-token"}, "garbage token"),
                       ({"lichess_token": 12345}, "integer token"),
                       ({"lichess_token": None}, "null token (does not clear)"),
                       ({"lichess_token": "A" * 200000}, "200 KB token")]:
    r = cl.post("/api/settings", json=payload)
    v = q1("SELECT value FROM settings WHERE key='lichess_token'")
    v = v["value"] if v else None
    print(f"  {label:<26} HTTP {r.status_code} -> stored {str(v)[:30]!r}"
          f"{' len=%d' % len(v) if v else ''}")
print("  -> SettingsPayload.lichess_token is a bare Optional[str]: no length cap, no format")
print("     check, and `null` is treated as 'leave unchanged' so there is no way to clear it.")

hdr("ATK3-14c — which hosts receive the Bearer token?")
sent = {}


class Rec:
    def __init__(self, status=401, payload=None):
        self.status_code = status
        self._p = payload or {}

    def json(self):
        return self._p


def rec_get(url, params=None, headers=None, timeout=None, **kw):
    a = (headers or {}).get("Authorization")
    sent.setdefault(url.split("?")[0], set()).add(
        (a[:14] + "...(%d chars)" % len(a)) if a else None)
    return Rec(401)


lc.requests = type("R", (), {"get": staticmethod(rec_get)})
cl.get("/api/account")
import backend.repertoire as rep  # noqa: E402
RepReq = lc.requests
rep.requests = RepReq
rm.get_candidate_moves_2000("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
for u, hs in sent.items():
    print(f"    {u:<50} Authorization={hs}")
print("  -> the Lichess API token (issued for lichess.org) is attached to")
print("     explorer.lichess.org as well, because LichessClient.headers is reused for the")
print("     opening-explorer queries in repertoire.get_candidate_moves_2000().")
restore_network()

hdr("ATK3-15a — is max_games actually bounded on the analyze paths?")
import backend.analyzer as an  # noqa: E402
seen = []
an.GameAnalyzer.analyze_game_openings = lambda self, g: (seen.append(g["id"]) or [])
an.GameAnalyzer.close = lambda self: None
passed = {}


def record_fetch(username, max_games=None, since=None, perf_type="x"):
    passed["max_games"] = max_games
    passed["username"] = username
    return [{"id": f"m{i}", "speed": "blitz", "rated": True,
             "createdAt": 1700000000000, "winner": "white",
             "opening": {"name": "X", "eco": "A00"},
             "players": {"white": {"user": {"name": "RobiDeli"}},
                         "black": {"user": {"name": "R"}}},
             "moves": "e4 e5"} for i in range(5)]


lc.LichessClient.fetch_user_games = staticmethod(record_fetch)
wipe_games()
for mg in ("1", "50", "100000", "null", "0", "-5", "999999999"):
    seen.clear()
    r = cl.post(f"/api/repertoire/sync_games?max_games={mg}")
    body = r.json() if r.status_code == 200 else {}
    print(f"  /api/repertoire/sync_games?max_games={mg:<10} HTTP {r.status_code} "
          f"-> max_games forwarded to Lichess: {passed.get('max_games')!r} "
          f"analyzed={len(seen)}")
print("  -> max_games has NO upper bound; the endpoint signature is")
print("       POST /api/repertoire/sync_games(max_games: Optional[int] = Query(None))")
print("     and fetch_user_games only adds `max=` when the value is > 0, so None/0/negative")
print("     means 'download and Stockfish-analyse the ENTIRE game history'.")
print("  (username fallback when nothing is configured:)",
      repr(dbm.get_setting("lichess_username") or "RobiDeli"))

print("\n  /api/repertoire/sync_games_stream?max_games=...")
for mg in ("null", "1", "100000"):
    passed.clear()
    with cl.stream("GET", f"/api/repertoire/sync_games_stream?max_games={mg}") as resp:
        n = sum(1 for x in resp.iter_lines() if x)
    print(f"    max_games={mg:<8} HTTP {resp.status_code} events={n} "
          f"forwarded={passed.get('max_games')!r}")

hdr("ATK3-15b — SSE client disconnect mid-stream (generator driven directly)")
import asyncio  # noqa: E402
import threading  # noqa: E402
state = {"started": False, "finished": False, "polls": 0}


class FakeRequest:
    """`is_disconnected()` returns True from the very first poll onwards."""

    async def is_disconnected(self):
        state["polls"] += 1
        return True


def blocking_fetch(username, max_games=None, since=None, perf_type="x"):
    state["started"] = True
    # simulates requests' 60s read timeout that nothing can interrupt
    for _ in range(20):
        time.sleep(0.05)
        if state.get("cancel"):
            state["finished"] = True
            return []
    state["finished"] = True
    return []


lc.LichessClient.fetch_user_games = staticmethod(blocking_fetch)
rmx = RepertoireManager.get_instance()


async def drive():
    """FakeRequest.is_disconnected() is True from the first call on: the client is gone."""
    gen = rmx.stream_sync_recent_games(max_games=5, request=FakeRequest())
    events = []
    t = time.time()
    async for ev in gen:
        events.append((round(time.time() - t, 2), ev.strip()[:70]))
    return events


t0 = time.time()
evs = asyncio.run(drive())
print("  the client is 'disconnected' for the entire stream; timeline:")
for t, ev in evs:
    print(f"    +{t:>5.2f}s  {ev}")
print("  is_disconnected() polled", state["polls"], "times | total %.2fs"
      % (time.time() - t0))
print("  -> the disconnect is only observed INSIDE the per-game loop, i.e. AFTER")
print("     `await asyncio.to_thread(fetch_user_games, ...)` has already run to completion.")
print("     The download cannot be cancelled, so an abandoned stream still holds a worker")
print("     thread for the whole 60s timeout (or 3s sleep + 30s retry on HTTP 429),")
print("     and if the fetch DOES return games the loop breaks only after the first row")
print("     has been written to an uncommitted transaction.")
print("  events emitted after the disconnect point:",
      [e for e in evs if "processing" in e[1] or "completed" in e[1]] or "none")

hdr("ATK3-15c — the 429 retry path makes an abandoned stream block even longer")
src = open("backend/lichess_client.py", encoding="utf-8").read()
print("  fetch_user_games retry logic:")
for i, line in enumerate(src.splitlines(), 1):
    if "429" in line or "time.sleep" in line or "timeout=" in line:
        print(f"    {i}: {line.rstrip()}")
print("  -> a 429 costs 3s + a second request with a 30s timeout that is never given up on.")
print("  -> the first response object is never closed on the retry path either (resp is")
print("     overwritten, so the first stream's file descriptor is only reclaimed by GC).")
