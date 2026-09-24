"""ATK3-08 : untrusted Lichess-Explorer JSON -> crashes, Infinity cache poisoning, XSS data."""
import os
import sys
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import (hdr, wipe_games, wipe_explorer, q, q1, fake_explorer,  # noqa: E402
                      restore_network, client)
import backend.repertoire as rep  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402

cl = client()
rm = RepertoireManager.get_instance()
rep.RepertoireManager._get_engine = lambda self: None
START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def probe(name, payload, fen=START):
    wipe_explorer()
    fake_explorer(payload)
    try:
        r = cl.get("/api/repertoire/explore", params={"fen": fen})
        out = r.text
        code = r.status_code
    except Exception as e:
        code, out = "EXC", f"{type(e).__name__}: {e}"
    finally:
        restore_network()
    cached = q1("SELECT data_json FROM explorer_cache LIMIT 1")
    print(f"\n  --- {name}")
    print(f"      HTTP {code}  body[:180]={out[:180]}")
    if cached:
        print(f"      explorer_cache row persisted: {cached['data_json'][:160]}")
    return code, out


hdr("ATK3-08 — hostile explorer payloads against GET /api/repertoire/explore")
probe("moves is a list of lists (wrong shape)", {"white": 1, "draws": 1, "black": 1,
                                                "moves": [[1, 2], "x", None]})
probe("counts are strings", {"white": "12", "draws": "3", "black": "9",
                             "moves": [{"uci": "e2e4", "san": "e4", "white": "12",
                                        "draws": "3", "black": "9"}]})
probe("counts are null", {"white": None, "draws": None, "black": None,
                          "moves": [{"uci": "e2e4", "san": "e4", "white": None,
                                     "draws": None, "black": None}]})
probe("payload root is a list", [1, 2, 3])
probe("Infinity counts (JSON allows it)", json.loads('{"white": Infinity, "draws": 0,'
                                                    ' "black": 0, "moves": [{"uci":"e2e4",'
                                                    '"san":"e4","white":Infinity,'
                                                    '"draws":0,"black":0}]}'))
probe("NaN counts", json.loads('{"white": NaN, "draws": 0, "black": 0,'
                               ' "moves": [{"uci":"e2e4","san":"e4","white":NaN,'
                               '"draws":0,"black":0}]}'))
probe("absurd counts 1e15", {"white": 10 ** 15, "draws": 0, "black": 0,
                             "moves": [{"uci": "e2e4", "san": "e4", "white": 10 ** 15,
                                        "draws": 0, "black": 0}]})
probe("negative counts", {"white": -50, "draws": -50, "black": -50,
                          "moves": [{"uci": "e2e4", "san": "e4", "white": -50,
                                     "draws": -50, "black": -50}]})
probe("HTML payload in san/uci (frontend innerHTML)", {
    "white": 10, "draws": 1, "black": 10,
    "moves": [{"uci": "e2e4", "san": '<img src=x onerror=alert(1)>', "white": 10,
               "draws": 1, "black": 10}]})
probe("moves dict (not a list)", {"white": 1, "draws": 0, "black": 0,
                                  "moves": {"a": 1}})
probe("no white/draws/black keys at all", {"moves": [{"uci": "e2e4", "san": "e4",
                                                     "white": 1, "draws": 0, "black": 1}]})
probe("deeply nested junk", {"white": 1, "draws": 0, "black": 0,
                             "moves": [{"uci": "e2e4", "san": "e4", "white": 1, "draws": 0,
                                        "black": 0, "extra": {"a": {"b": {"c": [1] * 5}}}}]})
probe("10 MB response", {"white": 10, "draws": 0, "black": 0,
                         "moves": [{"uci": f"a{i%8}b{i%8}", "san": "e4", "white": 10,
                                    "draws": 0, "black": 0,
                                    "pad": "A" * 120000} for i in range(80)]})

hdr("ATK3-08b — Infinity poison is PERSISTENT (7-day cache) and 500s forever")
wipe_explorer()
fake_explorer(json.loads('{"white": Infinity, "draws": 0, "black": 0,'
                         ' "moves": [{"uci":"e2e4","san":"e4","white":Infinity,'
                         '"draws":0,"black":0}]}'))
r1 = cl.get("/api/repertoire/explore", params={"fen": START})
restore_network()
print("  1st request (network):", r1.status_code, r1.text[:100])
row = q1("SELECT fen, data_json, updated_at FROM explorer_cache")
print("  cached row:", (row["data_json"][:120] if row else None))
r2 = cl.get("/api/repertoire/explore", params={"fen": START})
print("  2nd request (served from cache, no network at all):", r2.status_code, r2.text[:120])
print("  -> the position is permanently broken until the cache row is deleted manually")

hdr("ATK3-08c — SQL injection through the explorer 'san'/'uci' field")
wipe_explorer()
fake_explorer({"white": 1, "draws": 0, "black": 1,
               "moves": [{"uci": "e2e4'; DROP TABLE games; --", "san": "e4", "white": 1,
                          "draws": 0, "black": 1}]})
r = cl.get("/api/repertoire/explore", params={"fen": START})
restore_network()
print("  HTTP", r.status_code)
print("  games table still exists:", q1("SELECT COUNT(*) c FROM games") is not None)
print("  cache row:", (q1("SELECT data_json FROM explorer_cache") or {}).get("data_json", "")[:150])

hdr("ATK3-08d — what the frontend does with the payload (innerHTML sink)")
wipe_explorer()
fake_explorer({"white": 100, "draws": 0, "black": 0,
               "moves": [{"uci": "e2e4", "san": "e4", "white": 100, "draws": 0, "black": 0}]})
r = cl.get("/api/repertoire/explore", params={"fen": START})
restore_network()
mv = r.json()["moves"][0]
print("  API returns move object:", json.dumps(mv))
print("  repertoire_builder.js:2233 interpolates m.san, m.popularity_pct, m.white_win_pct,")
print("  m.draw_pct, m.black_win_pct straight into tr.innerHTML with no escaping.")
print("  e.g. style=\"width:${m.white_win_pct}%;\"  <- a string count would break out of the style")
