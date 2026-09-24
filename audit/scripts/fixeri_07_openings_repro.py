"""FIXER-I — SCOUT-07 evidence: ECO / opening matching.

    PYTHONPATH=. /home/robin/miniconda3/bin/python3 audit/scripts/fixeri_07_openings_repro.py
"""
import tests.test_setup  # FIRST  (BRIEF hard rule 2)

import chess
from backend.openings import OpeningBook

book = OpeningBook.get_instance()


def show(label, item):
    if item is None:
        print(f"  {label:<44} -> None")
    else:
        print(f"  {label:<44} -> {item['eco']} | {item['name']}")


print("=== (a) ECO guide ids must not resolve to another family ===")
for name in ("Sicilian", "French", "Caro-Kann Defense", "e4", "Italian Game",
             "King's Indian Defense", "Najdorf", "Dragon"):
    show(f"find_opening_by_name({name!r})", book.find_opening_by_name(name))

print()
print("=== (b) search ranking ===")
for q in ("Italian Game", "Najdorf", "Dragon", "King's Indian Defense",
          "Sicilian Defense Najdorf Guide", "c", "b20"):
    hits = book.search_openings(q, limit=3)
    print(f"  search_openings({q!r}) -> " + " | ".join(f"{h['eco']} {h['name']}" for h in hits))

print()
print("=== (c) deepest-match identification for real positions ===")
for label, sans in (
    ("Najdorf", ["e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6", "Nc3", "a6"]),
    ("Dragon", ["e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "g6"]),
    ("KID", ["d4", "Nf6", "c4", "g6", "Nc3", "Bg7", "e4", "d6", "Nf3"]),
    ("Ruy Lopez", ["e4", "e5", "Nf3", "Nc6", "Bb5"]),
):
    info = book.identify_game_opening(sans)
    print(f"  {label:<12} -> {info.get('eco')} | {info.get('name')}")

print()
print("=== (d) identify_best_match(title, chapters) ===")
item = book.identify_best_match(
    "Sicilian Defense Najdorf Guide",
    ["Najdorf introduction", "Poisoned pawn"],
)
show("Najdorf video w/ 'Poisoned pawn' chapter", item)
show("Dragon video", book.identify_best_match("Sicilian Dragon Defense Repertoire Guide", []))
show("KID video", book.identify_best_match("King's Indian Defense - Complete Guide", []))
