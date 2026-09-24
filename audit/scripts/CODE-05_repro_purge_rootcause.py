"""
CODE-05 repro #10: pin down WHY migrate_studies_and_cleanup_blunders (db.py:322-364)
orphans 155 of the user's own moves on first boot.

Instruments the exact same BFS the migration runs, on a read-only copy of the
git-committed DB, and reports the first place the chain breaks.

Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-05_repro_purge_rootcause.py
"""
import os
import subprocess
import sqlite3
from collections import Counter

PROJECT_ROOT = "/home/robin/Projects/ChessImprovement"
WORK = "/tmp/opencode/code05_initdb3"
os.makedirs(WORK, exist_ok=True)
committed = os.path.join(WORK, "committed.db")
if not os.path.exists(committed):
    with open(committed, "wb") as fh:
        subprocess.run(["git", "-C", PROJECT_ROOT, "show", "HEAD:chess_improvement.db"],
                       stdout=fh, check=True)
    os.chmod(committed, 0o644)

con = sqlite3.connect(f"file:{committed}?mode=ro&immutable=1", uri=True)
con.row_factory = sqlite3.Row

start_fen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
start_3 = ' '.join(start_fen.split()[:3])
print("BFS seed (3-token):", start_3)

for rep in ('white', 'black'):
    rows = con.execute("SELECT id, parent_fen, fen, move_uci FROM repertoire_moves WHERE repertoire=?", (rep,)).fetchall()
    parent_map, dest_map = {}, {}
    for r in rows:
        parent_map.setdefault(' '.join(r["parent_fen"].split()[:3]), []).append(r["id"])
        dest_map[r["id"]] = ' '.join(r["fen"].split()[:3])

    queue, visited, reach = [start_3], set(), set()
    while queue:
        cur = queue.pop(0)
        if cur in visited:
            continue
        visited.add(cur)
        for mid in parent_map.get(cur, []):
            reach.add(mid)
            d = dest_map.get(mid)
            if d and d not in visited:
                queue.append(d)

    total = len(rows)
    orphans = [r for r in rows if r["id"] not in reach]
    print(f"\n=== {rep}: {total} rows, {len(reach)} reachable, {len(orphans)} would be DELETED ===")

    # Which parent_fen do the orphans hang off, and is that parent itself reachable?
    orphan_parents = Counter(' '.join(r["parent_fen"].split()[:3]) for r in orphans)
    dead_parents = [(p, n) for p, n in orphan_parents.items() if p not in visited]
    print(f"  distinct unreachable parent positions: {len(dead_parents)}")
    for p, n in sorted(dead_parents, key=lambda x: -x[1])[:3]:
        print(f"    {p}  ({n} orphan children)")
        # is this position reachable as a DEST of anything?
        src = [i for i, d in dest_map.items() if d == p]
        print(f"      reachable as a `fen` of {len(src)} move(s); those move ids: {src[:6]}")
        if src:
            r = con.execute("SELECT parent_fen, fen, move_uci FROM repertoire_moves WHERE id=?", (src[0],)).fetchone()
            print(f"      e.g. id={src[0]} move={r['move_uci']}")
            print(f"           its parent_fen (3tok) = {' '.join(r['parent_fen'].split()[:3])}")
            print(f"           its parent_fen is in visited? {' '.join(r['parent_fen'].split()[:3]) in visited}")
            print(f"           its fen         (3tok) = {dest_map[src[0]]}")
            print(f"           full fen stored      = {r['fen']!r}")
            print(f"           token counts: parent={len(r['parent_fen'].split())} fen={len(r['fen'].split())}")
con.close()
