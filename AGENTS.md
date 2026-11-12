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
  api.py            # REST API, SSE streams, Fair-Play guard & local-origin boundary
  db.py             # Schema, migrations, closing_db() SQLite factory
  lichess_auth.py   # Lichess OAuth2 PKCE flow & board:play scope inspector
  live_play.py      # Lichess Board API live game, seek, challenge & clock manager
  game_analysis.py  # Full-game Stockfish 19 review (Depth 19 + MultiPV-3) & sequence saver
  repertoire.py     # Repertoire tree CRUD, studies, overlap & PGN import
  explorer.py       # 2000+ Lichess Explorer cache, candidate evals, threat score
  game_sync.py      # Lichess game ingestion, SSE sync streams, blunder gaps
  trainer.py        # FSRS-5 memory model, implicit latency grading, DAG graph
  deviations.py     # Game deviation detection & practice drills
  analyzer.py       # Stockfish blunder persistence & reconciliation
  evaluator.py      # Stockfish async/sync pools & MultiPV SSE eval stream
  utils.py          # FEN normalization & Lichess outcome coercion
frontend/
  index.html             # 4-tab Studio shell (Play | Analysis | Builder | Trainer)
  style.css              # Single Dark Studio stylesheet
  live_play.js           # Live Lichess board, clocks, seeks, challenges & Fair-Play lock
  game_analysis.js       # Full-game Stockfish 19 review, eval graph & guided trap fixer
  repertoire_builder.js  # Repertoire tree, board, eval bar, Explorer, Gaps & Games
  repertoire_trainer.js  # FSRS-5 active-recall trainer & game deviations
  board.js               # Interactive SVG chessboard with promotion chooser
  app.js                 # Workspace router, OAuth PKCE, Settings modal & toasts
tests/
  test_setup.py                       # Temp SQLite DB isolation on import
  test_platform_play_and_analysis.py  # Live Play, OAuth PKCE, Depth-19 analysis & sequence saver
  test_fsrs5_sota_trainer.py          # FSRS-5, latency grading, DAG & UI contract tests
```

## ⚠️ Core Invariants
1. **Test Isolation**: Import `tests.test_setup` first in tests. Never touch `chess_improvement.db`.
2. **Offline Tests**: Mock external HTTP (Lichess).
3. **Threat Score**: Candidate moves ranked by $N \times (L^2 + 0.02)$ where $L = 1 - W/100$.
4. **FEN Normalization**: Use 4 tokens (`placement turn castling ep`) for keys.
5. **No Data Loss**: Never delete or overwrite user moves without explicit consent.
6. **GitHub Sync (Mandatory)**: Always commit and push changes to GitHub (`git push origin main`) when completing tasks.
