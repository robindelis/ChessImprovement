"""
CODE-05 repro #9 (detail for CODE-05_repro_initdb_purge): which moves does the
init_db() migration destroy, and which default studies does it inject?

Works only on a /tmp copy of the git-committed DB. Production is never written.
Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-05_repro_initdb_purge_detail.py
"""
import os
import shutil
import subprocess
import sys
import sqlite3

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK = "/tmp/opencode/code05_initdb2"
os.makedirs(WORK, exist_ok=True)

committed = os.path.join(WORK, "committed.db")
if not os.path.exists(committed):
    with open(committed, "wb") as fh:
        subprocess.run(["git", "-C", PROJECT_ROOT, "show", "HEAD:chess_improvement.db"],
                       stdout=fh, check=True)
    os.chmod(committed, 0o644)
victim = os.path.join(WORK, "victim.db")
shutil.copy2(committed, victim)


def rows(p, q):
    con = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
    r = con.execute(q).fetchall()
    con.close()
    return r


before = {(r[0], r[1], r[2]) for r in rows(committed, "select repertoire, parent_fen, move_uci from repertoire_moves")}
print(f"moves before boot: {len(before)}")

os.environ["CHESS_DB_PATH"] = victim
sys.path.insert(0, PROJECT_ROOT)
from backend.db import init_db  # noqa: E402
init_db()

after_rows = rows(victim, "select repertoire, parent_fen, move_uci, study_name, is_user_move from repertoire_moves")
after = {(r[0], r[1], r[2]) for r in after_rows}
print(f"moves after  boot: {len(after)}")
print(f"DESTROYED        : {len(before - after)}")
print(f"ADDED            : {len(after - before)}")

print("\n### studies the migration INJECTED (violates AGENTS.md 9.3) ###")
for r in rows(victim, "select name, repertoire, description from repertoire_studies"):
    print("   ", r)

print("\n### study_name re-labelled onto the surviving moves ###")
from collections import Counter
for name, c in Counter(r[3] for r in after_rows).most_common():
    print(f"    {name!r:<32} {c}")

print("\n### sample of destroyed moves (user's own lines) ###")
for m in sorted(before - after)[:12]:
    con = sqlite3.connect(f"file:{committed}?mode=ro&immutable=1", uri=True)
    nm = con.execute("select move_san, opening_name from repertoire_moves where repertoire=? and parent_fen=? and move_uci=?", m).fetchone()
    con.close()
    print(f"    {m[0]:<6} {' '.join(m[1].split()[:4]):<48} {m[2]:<6} {nm}")

print("\n### how many of the destroyed moves were USER moves? ###")
con = sqlite3.connect(f"file:{committed}?mode=ro&immutable=1", uri=True)
destroyed_user = 0
for m in (before - after):
    r = con.execute("select is_user_move from repertoire_moves where repertoire=? and parent_fen=? and move_uci=?", m).fetchone()
    if r and r[0] == 1:
        destroyed_user += 1
con.close()
print(f"    {destroyed_user} of {len(before - after)} destroyed rows were is_user_move=1 (the user's own prepared moves)")
