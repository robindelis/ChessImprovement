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
  api.py            # REST API, SSE streams, local-origin security boundary
  db.py             # Schema, migrations, closing_db() SQLite factory
  repertoire.py     # Repertoire tree CRUD, studies, overlap & PGN import
  explorer.py       # 2000+ Lichess Explorer cache, candidate evals, threat score
  game_sync.py      # Lichess game ingestion, SSE sync streams, blunder gaps
  trainer.py        # FSRS-5 memory model, implicit latency grading, DAG graph
  deviations.py     # Game deviation detection & practice drills
  analyzer.py       # Stockfish blunder detection
  evaluator.py      # Stockfish async/sync pools & SSE eval stream
  utils.py          # FEN normalization & Lichess outcome coercion
frontend/
  index.html             # 2-tab Studio shell (Builder | Trainer + Settings modal)
  style.css              # Single Dark Studio stylesheet
  repertoire_builder.js  # Repertoire tree, board, eval bar, Explorer, Gaps & Games
  repertoire_trainer.js  # FSRS-5 active-recall trainer & game deviations
  board.js               # Interactive SVG chessboard
  app.js                 # Tab switcher, Settings modal, study modal & toasts
tests/
  test_setup.py          # Temp SQLite DB isolation on import
  test_fsrs5_sota_trainer.py # FSRS-5, latency grading, DAG & UI contract tests
```

## ⚠️ Core Invariants
1. **Test Isolation**: Import `tests.test_setup` first in tests. Never touch `chess_improvement.db`.
2. **Offline Tests**: Mock external HTTP (Lichess).
3. **Threat Score**: Candidate moves ranked by $N \times (L^2 + 0.02)$ where $L = 1 - W/100$.
4. **FEN Normalization**: Use 4 tokens (`placement turn castling ep`) for keys.
5. **No Data Loss**: Never delete or overwrite user moves without explicit consent.
6. **GitHub Sync (Mandatory)**: Always commit and push changes to GitHub (`git push origin main`) when completing tasks.
