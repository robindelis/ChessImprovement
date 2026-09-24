import os
import requests
import json
import time
from typing import List, Dict, Optional, Tuple
from backend.db import get_setting, set_setting

# Outbound network budget for every Lichess call (audit SCOUT-10 / ABUSE-01).
LICHESS_TIMEOUT = 20
ACCOUNT_TIMEOUT = 10
EXPLORER_TIMEOUT = 8
# Audit ABUSE-01/scope item 11: `fetch_user_games` streamed the whole NDJSON
# body with no ceiling, so a multi-megabyte response was accumulated in memory.
MAX_NDJSON_GAMES = 5000
MAX_NDJSON_LINE_BYTES = 1_000_000


class LichessFetchError(RuntimeError):
    """Raised instead of silently returning ``[]`` when a Lichess call fails.

    Audit ABUSE-01/scope item 11: every error used to collapse into an empty
    list, so a caller could not tell "this account has no games" from "the
    request failed / the token was rejected". Pass ``raise_on_error=True`` (or
    use :meth:`fetch_user_games_result`) to get the real outcome.
    """


def redact(text: str, token: Optional[str] = None) -> str:
    """Strips a bearer token from any string that is about to be logged."""
    out = str(text)
    for secret in (token,):
        if secret and len(secret) > 6:
            out = out.replace(secret, "lip_***REDACTED***")
    # Defensive: strip anything that looks like a Lichess token.
    import re as _re
    return _re.sub(r"lip_[A-Za-z0-9]{8,}", "lip_***REDACTED***", out)


DEFAULT_PERF_TYPES = "bullet,blitz,rapid,classical,correspondence,ultraBullet"


class LichessClient:
    def __init__(self, token: Optional[str] = None):
        self.token = token or self._resolve_token()
        self.base_url = "https://lichess.org"
        self.explorer_url = "https://explorer.lichess.org"
        self.headers = {
            "User-Agent": "ChessImprovementApp/1.0 (Robin Chess Improvement Assistant)"
        }
        if self.token:
            self.headers["Authorization"] = f"Bearer {self.token}"

    def _resolve_token(self) -> Optional[str]:
        """Resolves the Lichess token. The value is NEVER logged, returned by an
        endpoint, written to a subprocess argv, or echoed in an error message.

        Audit ABUSE-01: the token is a full-scope bearer credential for the
        user's Lichess account. It is read from the `settings` table first and
        from `.env` second; the fallback path is kept for compatibility but
        every message that could carry it goes through :func:`redact`.
        """
        # 1. Check DB setting
        db_token = get_setting("lichess_token")
        if db_token:
            return db_token

        # 2. Check .env file in project root
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
            except OSError:
                return None
            if "lip_" in content:
                for line in content.splitlines():
                    line = line.strip()
                    if line.startswith("lip_"):
                        return line
                    if "=" in line:
                        val = line.split("=", 1)[1].strip("\"' ")
                        if val.startswith("lip_"):
                            return val
        return None

    def get_account_profile(self) -> Optional[Dict]:
        """Fetch current logged-in user info using the API token."""
        if not self.token:
            return None
        try:
            url = f"{self.base_url}/api/account"
            resp = requests.get(url, headers=self.headers, timeout=ACCOUNT_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                # Cache username in settings
                if "username" in data:
                    set_setting("lichess_username", data["username"])
                return data
            elif resp.status_code == 401:
                print("Lichess token invalid or expired.")
        except Exception as e:
            print(f"Error fetching Lichess account: {redact(e, self.token)}")
        return None

    def fetch_user_games(self, username: str, max_games: Optional[int] = None, since: Optional[int] = None, perf_type: str = DEFAULT_PERF_TYPES, raise_on_error: bool = False) -> List[Dict]:
        """Fetch games played by username, streaming ndjson. If max_games is None or 0, fetches all games.

        Audit ABUSE-01/scope item 11: the read is now bounded
        (``MAX_NDJSON_GAMES`` games, ``MAX_NDJSON_LINE_BYTES`` per line) and a
        failure is no longer indistinguishable from an empty account: it is
        reported on stderr and, with ``raise_on_error=True``, raised as
        :class:`LichessFetchError`.
        """
        games, error = self.fetch_user_games_result(
            username, max_games=max_games, since=since, perf_type=perf_type
        )
        if error and raise_on_error:
            raise LichessFetchError(error)
        return games

    def fetch_user_games_result(
        self,
        username: str,
        max_games: Optional[int] = None,
        since: Optional[int] = None,
        perf_type: str = DEFAULT_PERF_TYPES
    ) -> Tuple[List[Dict], str]:
        """Same as :meth:`fetch_user_games` but returns ``(games, error)``.

        ``error`` is empty on success, so a caller can tell "no games" from
        "request failed" without parsing a log line.
        """
        url = f"{self.base_url}/api/games/user/{username}"
        params = {
            "evals": "true",
            "opening": "true",
            "moves": "true"
        }
        if perf_type:
            params["perfType"] = perf_type
        if max_games and max_games > 0:
            params["max"] = max_games
        if since and since > 0:
            params["since"] = since

        headers = dict(self.headers)
        headers["Accept"] = "application/x-ndjson"

        games: List[Dict] = []
        resp = None
        try:
            resp = requests.get(url, params=params, headers=headers, stream=True, timeout=LICHESS_TIMEOUT)
            if resp.status_code == 429:
                print("Lichess rate limit hit (429). Waiting 3 seconds...")
                time.sleep(3)
                resp = requests.get(url, params=params, headers=headers, stream=True, timeout=LICHESS_TIMEOUT)

            if resp.status_code != 200:
                # The token is in the Authorization header, never in this message.
                detail = redact(f"status {resp.status_code} - {resp.text[:150]}", self.token)
                msg = f"Failed to fetch games: {detail}"
                print(msg)
                return games, msg

            truncated = False
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if len(line) > MAX_NDJSON_LINE_BYTES:
                    continue
                try:
                    g = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(g, dict):
                    games.append(g)
                if len(games) >= MAX_NDJSON_GAMES:
                    truncated = True
                    break
            if truncated:
                msg = f"Stopped after {MAX_NDJSON_GAMES} games (response cap)."
                print(msg)
                return games, msg
        except Exception as e:
            msg = redact(f"Exception during games fetch: {e}", self.token)
            print(msg)
            return games, msg
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass

        return games, ""

    def get_opening_explorer_stats(self, fen: str, play: str = "") -> Optional[Dict]:
        """Query Lichess Masters Opening Explorer for master moves and win percentages."""
        url = f"{self.explorer_url}/masters"
        params = {"fen": fen}
        if play:
            params["play"] = play

        try:
            resp = requests.get(url, params=params, headers=self.headers, timeout=EXPLORER_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 401:
                # Fallback to lichess community database if masters requires higher scope
                url_lic = f"{self.explorer_url}/lichess"
                resp_lic = requests.get(url_lic, params=params, headers=self.headers, timeout=EXPLORER_TIMEOUT)
                if resp_lic.status_code == 200:
                    return resp_lic.json()
        except Exception as e:
            print(f"Opening explorer error: {redact(e, self.token)}")
        return None
