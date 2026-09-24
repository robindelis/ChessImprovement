# ABUSE-01 — Lichess API token is committed in git history and was never rotated
- **Severity:** critical
- **Type:** security
- **File:** `.env` (repo root) / `backend/lichess_client.py`
- **Line(s):** `.env` (whole file, 25 bytes); loader at `backend/lichess_client.py:19-39`
- **Component:** secret management
- **Repro script:** `audit/scripts/atk5_secrets_repro.py`
- **Detected by:** ATK-5

## Expected
A Lichess personal API token (`lip_…`) is a bearer credential: it grants full
account scope on the user's Lichess account. It must (a) never enter git,
(b) be gitignored, (c) be rotatable, and (d) be redacted from every log,
response, traceback and subprocess argument list.

## Actual
The token lives in a plain `.env` at the repo root. At commit `81f4ed6`
("Initial commit") that file — containing the raw token — was committed, and it
is the only commit in the history. Removing the file from the working tree and
adding it to `.gitignore` (both already done by a concurrent fixer) does **not**
remove the blob from history and does **not** invalidate the token.

Observed (`atk5_secrets_repro.py`, run 2026-09-26):

```
1) GIT HYGIENE
  Committed .env blob contains a live 'lip_' token: True
  Commits touching .env   : 81f4ed6 Initial commit
```

```
$ git show 81f4ed6:.env
lip_***REDACTED***
```

`atk5_secrets_repro.py` also verified the *positive* controls, which all hold:
the token is **not** returned by `/api/settings`, `/api/account`, `/api/dashboard`,
`/api/games/recent`, `/api/blunders`, `/api/train/paths`, `/api/repertoire/tree`,
`/api/repertoire/train/openings`, `/api/repertoire/train/progress` or
`/api/scout/recent`; `POST /api/settings` does not echo it back; the
`@app.exception_handler(ValueError)` body never contained it; and the Stockfish
`argv` does not carry it (`chess.engine.popen_uci` uses a list argv, no shell,
and `STOCKFISH_PATH` is a module constant).

## Root cause
`backend/lichess_client.py:19-39` reads the token from the DB `settings` table
first and then falls back to the plaintext `.env` file:

```python
    def _resolve_token(self) -> Optional[str]:
        db_token = get_setting("lichess_token")
        if db_token:
            return db_token
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if "lip_" in content:
                    ...
                        if line.startswith("lip_"):
                            return line
```

The repo had no `.gitignore` entry for `.env` at commit `81f4ed6`, so the file
was tracked. There is no secret scanning, no `.env.example`, and no rotation
path in the app or in `AGENTS.md`.

## Exploit / escalation
1. **Trivial:** `git show 81f4ed6:.env` in any clone of this repo yields a live
   Lichess bearer token. With it an attacker can, via `https://lichess.org/api/...`
   with `Authorization: Bearer lip_…`, read and modify the account (export games,
   change settings, follow/block users, and — depending on granted scopes — send
   challenges/ messages). The victim is a single named user (`RobiDeli`).
2. **Adjacent variant — the same secret also lands in the committed database.**
   `atk5_secrets_repro.py` checks the committed `chess_improvement.db` blob for
   `b"lip_"`: at `81f4ed6` the token is *not* in that blob, but `POST
   /api/settings` writes any token the user pastes into the `settings` table in
   plaintext, so the moment a user saves the token through the UI the same
   repository will leak it a second time via the DB blob unless `*.db` is
   ignored (it now is).
3. **Adjacent variant — `AGENTS.md` §7/§9 documents the token location in prose**
   ("Engine Binary", "Lichess token") and instructs every future agent/agent-run
   to keep the file present, so the secret keeps being re-materialised on every
   machine that clones the repo.

**Required fix:** rotate the token at `lichess.org/account/oauth/token`, rewrite
history (`git filter-repo --path .env --invert-paths`) or re-init the repository,
add `.env.example`, and stop falling back to a plaintext file (prefer the
`settings` table / OS keyring).
