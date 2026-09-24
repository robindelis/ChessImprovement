"""
CODE-05 repro #7: enumerate the REAL schema from backend/db.py's CREATE TABLE
statements and print it, so AGENTS.md §5 can be diffed column-for-column.

Safe: reads only backend/db.py source text; no DB is opened for writing.
Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-05_schema_dump.py
"""
import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = open(os.path.join(PROJECT_ROOT, "backend", "db.py"), encoding="utf-8").read()

for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\s*\)", src, re.S):
    table, body = m.group(1), m.group(2)
    print(f"\n### {table} ###")
    for raw in body.splitlines():
        line = raw.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        print("   " + line)
    # table-level constraints
    tail = body.splitlines()[-1].strip().rstrip(",")
    if re.match(r"(UNIQUE|PRIMARY KEY|FOREIGN KEY|CHECK)", tail):
        print("   " + tail)
