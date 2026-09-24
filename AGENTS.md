# AGENTS.md — Developer Guide

Personalized chess repertoire builder, trainer, and game review app for **`RobiDeli`** with Stockfish 19 engine and Lichess Explorer.

## ⚡ Quick Reference
- **Python**: `/home/robin/miniconda3/bin/python3` (base env only).
- **Port**: **`38281`** (never 8000).
- **Run Server**: `/home/robin/miniconda3/bin/python3 start.py`
- **Run Desktop**: `./run-desktop.sh` or `npm start`
- **Run Tests**: `/home/robin/miniconda3/bin/python3 -m unittest discover tests`

## 🧱 Stack
- **Backend**: FastAPI + Uvicorn + `python-chess`.
- **Database**: SQLite (`chess_improvement.db`, WAL mode per connection).
- **Engine**: Stockfish 19 binary (`bin/stockfish/stockfish-linux-x86-64-universal`).
- **Frontend**: Zero-build vanilla ES6+ JS/HTML/CSS in `frontend/`.
- **Desktop**: Electron 44 in `electron/` (spawns Python backend).

## 📁 Key Files
```
backend/
  api.py            # REST API, SSE streams, CORS
  db.py             # Schema, migrations, SQLite factory
  repertoire.py     # Tree manager, spaced repetition (SRS), studies, threat score
  analyzer.py       # Stockfish blunder detection
  evaluator.py      # Stockfish async pool & SSE eval stream
  deviations.py     # Game deviation detection & practice drills
  scout_discovery.py# Opening scout & video discovery
frontend/
  repertoire_builder.js  # Repertoire tree, board, eval bar, game gaps
  repertoire_trainer.js  # SRS training against opponent replies
  board.js               # Interactive SVG chessboard
  scout.js               # Course video scout & board sync
tests/
  test_setup.py     # Temp SQLite DB isolation on import
```

## ⚠️ Core Invariants
1. **Test Isolation**: Import `tests.test_setup` first in tests. Never touch `chess_improvement.db`.
2. **Offline Tests**: Mock external HTTP (Lichess/YouTube).
3. **Threat Score**: Candidate moves ranked by $N \times (L^2 + 0.02)$ where $L = 1 - W/100$.
4. **FEN Normalization**: Use 4 tokens (`placement turn castling ep`) for keys.
5. **No Data Loss**: Never delete or overwrite user moves without explicit consent.
6. **GitHub Sync (Mandatory)**: Always commit and push changes to GitHub (`git push origin main`) when completing tasks.
