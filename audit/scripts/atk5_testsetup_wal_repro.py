#!/usr/bin/env python3
"""
ATK5-12b repro: tests/test_setup.py takes a READ-WRITE handle on the production
database in order to copy it. sqlite3.connect(path) is read-write; the read-only
form is sqlite3.connect("file:<path>?mode=ro", uri=True).

Proof is done on a synthetic WAL database in /tmp -- the production
chess_improvement.db is NEVER opened by this script.
"""
import os
import sqlite3
import time

D = "/tmp/opencode/atk5_wal2.db"
for f in (D, D + "-wal", D + "-shm", "/tmp/opencode/atk5_wal2_copy.db"):
    if os.path.exists(f):
        os.unlink(f)

# 1. build a WAL database exactly like the app's
s = sqlite3.connect(D)
s.execute("PRAGMA journal_mode=WAL")
s.execute("CREATE TABLE t(x)")
s.execute("INSERT INTO t VALUES (1)")
s.commit()
print("WAL mode:", s.execute("PRAGMA journal_mode").fetchone()[0])

# 2. a writer stays connected (this is the live uvicorn server, PID 225248)
print("writer holds the db. side files:",
      {os.path.basename(f): os.path.exists(f) for f in (D, D + "-wal", D + "-shm")})

# 3. test_setup's exact idiom
src = sqlite3.connect(D)                       # <-- READ-WRITE
print("src.isolation_level:", src.isolation_level)
print("can this handle WRITE? ->", end=" ")
try:
    src.execute("INSERT INTO t VALUES (999)")   # accepted => not read-only
    src.rollback()
    print("YES  (plain sqlite3.connect() is read-write; nothing stops a test from")
    print("      writing to, or checkpointing, the user's live database)")
except sqlite3.OperationalError as e:
    print("NO ->", e)

# read-only would look like this:
ro = sqlite3.connect(f"file:{D}?mode=ro", uri=True)
try:
    ro.execute("INSERT INTO t VALUES (1)")
    print("read-only handle accepted a write (unexpected)")
except sqlite3.OperationalError as e:
    print("read-only handle rejects writes:", e)
ro.close()

# 4. closing the last read-write connection checkpoints + removes the WAL
mtime_before = os.path.getmtime(D)
src.close()
s.close()
print("main db mtime before last close:", mtime_before)
print("after last read-write close, -wal/-shm removed:",
      not os.path.exists(D + "-wal"), not os.path.exists(D + "-shm"),
      " (a plain read of the user's DB therefore rewrites their main DB file)")
print("main db mtime after :", os.path.getmtime(D), "changed:", os.path.getmtime(D) != mtime_before)
