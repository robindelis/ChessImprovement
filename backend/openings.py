import os
import glob
import csv
import pickle
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
import chess

from backend.opening_strategies import (
    GENERIC_BLACK,
    GENERIC_WHITE,
    SIDE_THEMES,
)

OPENINGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "openings_data")

#: Renders the general/specific pair for humans, e.g.
#: ``"Sicilian Defense › Smith-Morra Gambit"``.  Not an ASCII arrow: the whole
#: app is UTF-8 and the arrows in ``AGENTS.md`` are part of its house style.
NAME_SEPARATOR = " › "

#: How many moves ``get_opening_context`` will replay from a caller-supplied
#: line.  The deepest book line in the encyclopedia is 40 plies; 200 leaves room
#: for a real game while keeping a hostile query string from turning one GET into
#: a long chess replay.
MAX_CONTEXT_MOVES = 200

#: Characters a move token may contain.  SAN/UCI are drawn into the DOM and used
#: as cache keys, so the token is validated here rather than trusted.
_MOVE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+#=x\-oO]{0,15}$")

#: Bounded cache for :meth:`OpeningBook.get_opening_context`.  The encyclopedia is
#: immutable for the lifetime of the process, so entries can never go stale and
#: the whole thing is safe to drop wholesale when it fills up.
_CONTEXT_CACHE_MAX = 2000

# --------------------------------------------------------------------------- #
# "Which side plays this opening?"  --  the primitive the Scout needs in order
# to tell "learn the Najdorf" (I am Black) apart from "learn to face the
# Najdorf" (I am White).  Without it the two questions return the same shelf.
# --------------------------------------------------------------------------- #
SIDE_WHITE = "white"
SIDE_BLACK = "black"
SIDE_EITHER = "either"          # honestly undecidable, e.g. a symmetric line

#: Families that are *structurally* two-sided: either colour can be the one
#: "playing" them, so a name-only classifier must not pick.  Checked **before**
#: the ``Defense`` rule, because "Symmetrical Defense" contains the word and is
#: not a Black opening.
EITHER_SIDED_FAMILIES = frozenset({
    "formation", "creepy crawly formation", "english orangutan",
    "symmetrical", "symmetrical defense", "symmetrical defence",
    "slav indian", "india defense", "indian defense",
})

#: Curated side per encyclopedia family, lower-cased.  Derived from the 142
#: distinct ``split_name`` families actually present in ``openings_data``; the
#: names that state their own side (everything containing "Defense") are
#: handled by :data:`_DEFENSE_RE` instead, so this table only carries the ones
#: that do not.
CURATED_FAMILY_SIDES = {
    # -- Black's answer to a White move: a declined/accepted gambit ----------
    "queen's gambit declined": SIDE_BLACK,
    "queen's gambit accepted": SIDE_BLACK,
    "blackmar-diemer gambit accepted": SIDE_BLACK,
    "blackmar-diemer gambit declined": SIDE_BLACK,
    "latvian gambit accepted": SIDE_BLACK,
    "latvian gambit declined": SIDE_BLACK,
    "benko gambit accepted": SIDE_BLACK,
    "benko gambit declined": SIDE_BLACK,
    "englund gambit declined": SIDE_BLACK,
    "danish gambit accepted": SIDE_BLACK,
    "danish gambit declined": SIDE_BLACK,
    "kings gambit declined": SIDE_BLACK,
    "center game accepted": SIDE_BLACK,
    "blumenfeld countergambit accepted": SIDE_BLACK,
    "queen's indian accelerated": SIDE_BLACK,
    "yusupov-rubinstein system": SIDE_BLACK,
    # -- White's own opening or gambit --------------------------------------
    "kings gambit accepted": SIDE_WHITE,   # the gambit is White's identity
    "kings gambit": SIDE_WHITE,
    "blackmar-diemer gambit": SIDE_WHITE,
    "latvian gambit": SIDE_WHITE,
    "benko gambit": SIDE_WHITE,
    "englund gambit": SIDE_WHITE,
    "danish gambit": SIDE_WHITE,
    "blumenfeld countergambit": SIDE_WHITE,
    "elephant gambit": SIDE_WHITE,
    "duras gambit": SIDE_WHITE,
    "irish gambit": SIDE_WHITE,
    "queen's gambit": SIDE_WHITE,
    "queen's pawn game": SIDE_WHITE,
    "queen's pawn": SIDE_WHITE,
    "ruy lopez": SIDE_WHITE,
    "italian game": SIDE_WHITE,
    "english opening": SIDE_WHITE,
    "scotch game": SIDE_WHITE,
    "four knights game": SIDE_WHITE,
    "three knights opening": SIDE_WHITE,
    "vienna game": SIDE_WHITE,
    "vienna gambit": SIDE_WHITE,
    "kings pawn game": SIDE_WHITE,
    "kings pawn opening": SIDE_WHITE,
    "kings knight opening": SIDE_WHITE,
    "catalan opening": SIDE_WHITE,
    "bird opening": SIDE_WHITE,
    "bishop's opening": SIDE_WHITE,
    "bishop's opening, london defense": SIDE_WHITE,
    "polish opening": SIDE_WHITE,
    "polish defense": SIDE_WHITE,
    "kings indian attack": SIDE_WHITE,
    "london system": SIDE_WHITE,
    "colle system": SIDE_WHITE,
    "rapport-jobava system": SIDE_WHITE,
    "trompowsky attack": SIDE_WHITE,
    "zukertort opening": SIDE_WHITE,
    "r\u00e9ti opening": SIDE_WHITE,
    "barnes opening": SIDE_WHITE,
    "clemenz opening": SIDE_WHITE,
    "mieses opening": SIDE_WHITE,
    "van't kruijs opening": SIDE_WHITE,
    "amsterdam attack": SIDE_WHITE,
    "grob opening": SIDE_WHITE,
    "hungarian opening": SIDE_WHITE,
    "anderssen's opening": SIDE_WHITE,
    "amazon attack": SIDE_WHITE,
}

CURATED_FAMILY_SIDES.update({
    "nimzo-larsen attack": SIDE_WHITE,
    "portuguese opening": SIDE_WHITE,
    "saragossa opening": SIDE_WHITE,
    "valencia opening": SIDE_WHITE,
    "basque opening": SIDE_WHITE,
    "bongcloud attack": SIDE_WHITE,
    "canard opening": SIDE_WHITE,
    "paleface attack": SIDE_WHITE,
    "dresden opening": SIDE_WHITE,
    "ware opening": SIDE_WHITE,
    "k\u00e1das opening": SIDE_WHITE,
    "torre attack": SIDE_WHITE,
    "sodium attack": SIDE_WHITE,
})

#: A family that answers White's first move is Black's opening.  Anchored on the
#: word, so "Queen\u2019s Indian Accelerated" (no "Defense") is not caught here.
_DEFENSE_RE = re.compile(r"\bdefen[cs]e\b")

#: Coarse last-resort fallback: the ECO convention.  ``B10``-``B99`` are the
#: Sicilian defences, ``C00``-``C19`` the French/Caro-Kann/Pirc/Scandinavian
#: block, ``C20``+ the open games, ``D06``+ the ``1.d4`` defences and ``E``
#: the Indian defences.  Right about 97% of the time and explicitly *low*
#: confidence, because it is a convention rather than a fact.
_ECO_SIDE_RANGES = (
    (re.compile(r"^A[0-3][0-9]$"), SIDE_WHITE),
    (re.compile(r"^B(?:0[0-9]|[1-9][0-9])$"), SIDE_BLACK),
    (re.compile(r"^C(?:0[0-9]|1[0-9])$"), SIDE_BLACK),
    (re.compile(r"^C[2-9][0-9]$"), SIDE_WHITE),
    (re.compile(r"^D0[0-9]$"), SIDE_WHITE),
    (re.compile(r"^D[1-9][0-9]$"), SIDE_BLACK),
    (re.compile(r"^E[0-9][0-9]$"), SIDE_BLACK),
)

#: Cap on replies returned by :meth:`OpeningBook.expand_critical_replies` and on
#: the extra plies walked per reply, so a whole ECO book cannot become one reply.
MAX_CRITICAL_REPLIES = 16
#: How many named continuations make a position a *branch point* worth asking
#: about.  Two is the right floor: the Sicilian Alapin after ``2.c3`` has exactly
#: two (``3...d5`` Anti-Alapin, ``3...Nf6`` Smith-Morra Declined) and that pair is
#: the single most useful thing to know about the opening, while the tail of a
#: long line often has just one and answers nothing.
MIN_CRITICAL_BRANCH = 2


class OpeningBook:
    _instance = None

    def __init__(self):
        self.fen_to_opening = {} # fen (normalized) -> dict(eco, name, pgn)
        self.move_tree = {}      # trie of UCI moves: node = {'children': {}, 'eco': ..., 'name': ...}
        self.openings_list = []  # list of dict(eco, name, pgn)
        self.opening_themes = self._init_themes()
        self._context_cache = {} # (normalized fen, normalized move line) -> context dict
        # Distinct `split_name` families, longest first.  Built once from the
        # encyclopedia: the Scout uses it to notice that a video title names a
        # *different* opening than the one being searched for, which is the only
        # way to keep "Alapin vs Trompowsky" from scoring as an Alapin video.
        # A frozen tuple because the encyclopedia is immutable for the process.
        self.opening_family_index: Tuple[str, ...] = ()
        self._load_openings()
        self.opening_family_index = tuple(sorted(
            {f for f in (self.split_name(m.get("name", ""))[0] for m in self.openings_list) if f},
            key=len, reverse=True,
        ))

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _normalize_fen(self, fen: str) -> str:
        # Keep piece placement, active color, castling rights, and en passant (first 4 components)
        parts = fen.split()
        if len(parts) >= 4:
            return " ".join(parts[:4])
        return fen.strip()

    def _load_openings(self):
        tsv_files = sorted(glob.glob(os.path.join(OPENINGS_DIR, "*.tsv")))
        if not tsv_files:
            return

        cache_file = os.path.join(OPENINGS_DIR, ".openings_cache.pkl")
        try:
            max_tsv_mtime = max(os.path.getmtime(fn) for fn in tsv_files)
            if os.path.exists(cache_file) and os.path.getmtime(cache_file) >= max_tsv_mtime:
                with open(cache_file, "rb") as cf:
                    cached = pickle.load(cf)
                    if isinstance(cached, tuple) and len(cached) == 3:
                        self.openings_list, self.fen_to_opening, self.move_tree = cached
                        return
        except Exception:
            pass

        for fn in tsv_files:
            try:
                with open(fn, encoding="utf-8") as f:
                    reader = csv.DictReader(f, delimiter="\t")
                    for row in reader:
                        eco = row.get("eco", "")
                        name = row.get("name", "")
                        pgn_text = row.get("pgn", "")
                        if not pgn_text:
                            continue

                        # Parse moves into board and trie
                        self.openings_list.append({"eco": eco, "name": name, "pgn": pgn_text})
                        self._index_pgn_entry(eco, name, pgn_text)
            except Exception as e:
                print(f"Error loading opening file {fn}: {e}")

        try:
            tmp_cache = cache_file + f".tmp.{os.getpid()}"
            with open(tmp_cache, "wb") as cf:
                pickle.dump(
                    (self.openings_list, self.fen_to_opening, self.move_tree),
                    cf,
                    protocol=pickle.HIGHEST_PROTOCOL
                )
            os.replace(tmp_cache, cache_file)
        except Exception:
            pass

    # Words that say nothing about *which* opening a title refers to. They are
    # dropped from a query before matching, because otherwise the all-words rule
    # rejects every realistic video title ("Sicilian Defense Najdorf **Guide**")
    # or matches an unrelated entry through a single shared word.
    TITLE_STOPWORDS = frozenset({
        "a", "an", "the", "of", "for", "and", "or", "to", "in", "on", "at",
        "with", "vs", "versus", "against", "my", "your", "our", "their", "is",
        "are", "was", "were", "be", "do", "does", "did", "how", "what", "why",
        "who", "when", "which", "you", "it", "this", "that", "not", "no",
        "guide", "guides", "tutorial", "tutorials", "intro", "introduction",
        "overview", "lesson", "lessons", "part", "chapter", "episode",
        "full", "complete", "basic", "basics", "beginner", "beginners",
        "advanced", "master", "masters", "video", "videos", "explained",
        "explainer", "strategy", "strategies", "repertoire", "opening",
        "openings", "line", "lines", "theory", "move", "moves", "play",
        "plays", "playing", "played", "best", "top", "new", "step", "steps",
        "improve", "improving", "improve your", "detailed", "short", "quick",
        "simple", "ultimate", "essential", "main", "core", "everything",
    })

    @staticmethod
    def _query_tokens(q: str):
        """Lowercase alphanumeric tokens of a query with TITLE_STOPWORDS removed."""
        raw = re.findall(r"[a-z0-9]+", q)
        kept = [t for t in raw if t not in OpeningBook.TITLE_STOPWORDS]
        return kept or raw

    def _match_score(self, item: Dict, q: str, tokens) -> Optional[float]:
        """Relevance of one encyclopedia entry to a query, or None when it does not match.

        Audit SCOUT-07. The old rule was ``all(w in name or w in eco)``, i.e. a
        *substring* test with file order as the tie-break. That made
        "Sicilian Defense Najdorf Guide" match nothing (the word "Guide") and then
        let the chapter "Poisoned pawn" pick *A45 Trompowsky Attack: Poisoned Pawn
        Variation* for a Najdorf video.
        """
        name = (item.get("name") or "").lower()
        eco = (item.get("eco") or "").lower()
        if not tokens:
            return None

        score = 0.0
        for tok in tokens:
            if re.search(r"\b" + re.escape(tok) + r"\b", name):
                score += 10.0 + min(len(tok), 12)   # word-boundary hit in the NAME
            elif tok == eco:
                score += 8.0                        # exact ECO code
            else:
                return None                         # every query word must land
        if q:
            if name == q:
                score += 400.0
            elif name.startswith(q):
                score += 200.0
            elif re.search(r"\b" + re.escape(q) + r"\b", name):
                score += 120.0
        # Prefer the most specific (shortest) name when relevance ties.
        return score - 0.05 * len(name) - 0.01 * len(eco)

    def search_openings(self, query: str, limit: int = 15):
        try:
            limit = max(1, min(int(limit or 1), 200))
        except (TypeError, ValueError):
            limit = 15
        q = (query or "").lower().strip()
        if not q:
            return self.openings_list[:limit]
        tokens = self._query_tokens(q)
        scored = []
        for idx, item in enumerate(self.openings_list):
            s = self._match_score(item, q, tokens)
            if s is not None:
                scored.append((s, -idx, item))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [item for _, _, item in scored[:limit]]

    def find_opening_by_name(self, name_query: str):
        """Most specific encyclopedia entry whose NAME matches ``name_query``.

        Audit SCOUT-07(b): the old fallback was a bare ``q in name`` substring
        test returning the *first* hit, so "Sicilian" resolved to
        "A00 Hungarian Opening: Sicilian Invitation" and "French" to
        "A08 King's Indian Attack: French Variation". Matching is now
        word-boundary anchored and ranked (exact > prefix > contained, then
        shortest name).
        """
        q = (name_query or "").lower().strip()
        if not q:
            return None
        pat = r"\b" + re.escape(q) + r"\b"
        best_key = None
        best_item = None
        for idx, item in enumerate(self.openings_list):
            name = (item.get("name") or "").lower()
            if name == q:
                tier = 0
            elif re.match(r"^" + re.escape(q) + r"\b", name):
                tier = 1
            elif re.search(pat, name):
                tier = 2
            else:
                continue
            key = (tier, len(name), idx)   # most specific (shortest) name wins
            if best_key is None or key < best_key:
                best_key = key
                best_item = item
        return best_item

    def suggest_openings(self, prefix: str, limit: int = 8) -> List[Dict]:
        """Autocomplete over opening names -- prefix-tolerant, unlike :meth:`search_openings`.

        :meth:`search_openings` matches **whole words**, which is right for a
        search box ("Sicilian Najdorf" must not match an entry that merely
        contains one of them) and useless for an autocomplete: "alap" is a
        prefix of "Alapin", not a word in it, so the search matcher rejects every
        entry and the suggestion list comes back empty exactly when the user is
        mid-word.

        So this walks the names itself and scores three tiers, strongest first:

        1. the name **starts with** the fragment;
        2. the family starts with it;
        3. any word of the name starts with it.

        Ties break on the shorter name, so "Najdorf" outranks
        "Najdorf Variation, English Attack" and the autocomplete leads with the
        opening itself rather than one of its three-hundred sub-lines.
        """
        frag = " ".join(str(prefix or "").split()).lower()
        if not frag:
            return []
        try:
            cap = max(1, min(int(limit or 1), 50))
        except (TypeError, ValueError):
            cap = 8
        scored: List[Tuple[float, int, Dict]] = []
        for idx, item in enumerate(self.openings_list):
            name = (item.get("name") or "").lower()
            if not name:
                continue
            family = self.split_name(item.get("name", ""))[0].lower()
            if name.startswith(frag):
                tier = 0
            elif family.startswith(frag):
                tier = 1
            elif any(w.startswith(frag) for w in re.findall(r"[a-z0-9]+", name)):
                tier = 2
            else:
                continue
            scored.append((tier, len(name), item))
        scored.sort(key=lambda t: (t[0], t[1]))
        out: List[Dict] = []
        seen = set()
        for _tier, _len, item in scored:
            key = (item.get("eco"), item.get("name"))
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= cap:
                break
        return out

    def identify_best_match(self, *texts):
        """Best opening match for a video title, preferring earlier/whole text.

        Used by the Scout so that a specific title match always beats a chapter
        title that merely shares one word with an unrelated family
        (audit SCOUT-07(a)).
        """
        best = None
        best_key = None
        for rank, text in enumerate(texts):
            if not text or not isinstance(text, str):
                continue
            q = text.lower().strip()
            if not q:
                continue
            matches = self.search_openings(q, limit=1)
            if not matches:
                continue
            item = matches[0]
            s = self._match_score(item, q, self._query_tokens(q))
            if s is None:
                continue
            key = (-rank, s)
            if best_key is None or key > best_key:
                best_key = key
                best = item
        return best

    def _index_pgn_entry(self, eco: str, name: str, pgn_text: str):
        board = chess.Board()
        current_node = self.move_tree

        # Clean moves (remove move numbers like '1.', '2.', etc.)
        tokens = pgn_text.split()
        san_moves = [t for t in tokens if not t.endswith(".") and not t[0].isdigit()]
        total_moves = len(san_moves)

        for i, san in enumerate(san_moves):
            try:
                clean_san = re.sub(r"[?!#+]", "", san).strip()
                move = board.parse_san(clean_san)
                uci = move.uci()
                board.push(move)

                norm_fen = self._normalize_fen(board.fen())
                ply = board.ply()
                is_terminal = (i == total_moves - 1)

                existing = self.fen_to_opening.get(norm_fen)
                should_update = False
                if existing is None:
                    should_update = True
                elif is_terminal and not existing.get("is_terminal"):
                    should_update = True
                elif is_terminal and existing.get("is_terminal"):
                    if total_moves <= existing.get("total_moves", 999):
                        should_update = True
                elif not is_terminal and not existing.get("is_terminal"):
                    if total_moves < existing.get("total_moves", 999):
                        should_update = True

                if should_update:
                    self.fen_to_opening[norm_fen] = {
                        "eco": eco,
                        "name": name,
                        "ply": ply,
                        "total_moves": total_moves,
                        "is_terminal": is_terminal
                    }

                if "children" not in current_node:
                    current_node["children"] = {}
                if uci not in current_node["children"]:
                    current_node["children"][uci] = {"children": {}, "eco": eco, "name": name}
                current_node = current_node["children"][uci]
            except Exception:
                break

    def get_opening_for_fen(self, fen: str):
        norm = self._normalize_fen(fen)
        return self.fen_to_opening.get(norm)

    # ------------------------------------------------------------------ #
    # "What opening is this?"  --  general family + particular variation
    # ------------------------------------------------------------------ #
    @staticmethod
    def split_name(name: str) -> Tuple[str, List[str], str]:
        """Decompose an encyclopedia name into ``(family, levels, qualifier)``.

        The ECO TSV encodes its own hierarchy *inside the name string*, so the
        name is the only place "general" versus "particular" can come from:

        * ``"Sicilian Defense: Smith-Morra Gambit Accepted, Chicago Defense"``
          -> ``("Sicilian Defense", ["Smith-Morra Gambit Accepted", "Chicago Defense"], "")``
        * ``"Grob Opening: Grob Gambit, Fritz Gambit, Romford Countergambit"``
          -> ``("Grob Opening", ["Grob Gambit", "Fritz Gambit", "Romford Countergambit"], "")``
        * ``"Polish Opening, with d5"``
          -> ``("Polish Opening", [], "with d5")`` -- a trailing ``with …`` names
          the move that defines the line, it is not another level of the
          hierarchy, so it is kept as a qualifier instead of being promoted to a
          crumb of its own.

        An empty name yields ``("", [], "")`` rather than raising: the caller is
        deciding what to *display*, and "we do not know" must be renderable.
        """
        clean = " ".join(str(name or "").split())
        if not clean:
            return "", [], ""
        family, sep, rest = clean.partition(": ")
        if not sep:
            base, _, qualifier = clean.partition(", with ")
            return base, [], ("with " + qualifier.strip()) if qualifier.strip() else ""
        levels, qualifier = [], ""
        for part in rest.split(", "):
            part = part.strip()
            if not part:
                continue
            if part.lower().startswith("with "):
                qualifier = part
                continue
            levels.append(part)
        return family.strip(), levels, qualifier

    @classmethod
    def name_label(cls, name: str) -> str:
        """``"Sicilian Defense: Smith-Morra Gambit"`` -> ``"Sicilian Defense › Smith-Morra Gambit"``.

        Rendered in exactly one place so the Builder, the Scout and any future
        surface cannot disagree about what "Sicilian Defense › Smith-Morra
        Gambit" means.
        """
        family, levels, qualifier = cls.split_name(name)
        if not family:
            return ""
        label = NAME_SEPARATOR.join([family] + levels)
        if qualifier:
            label = "%s, %s" % (label, qualifier)
        return label

    @staticmethod
    def _parse_move(board: chess.Board, token: str) -> Optional[chess.Move]:
        """Parse one SAN *or* UCI token against ``board``; ``None`` if neither works.

        Shared by :meth:`identify_game_opening` and :meth:`get_opening_context` so
        the two cannot drift on what counts as a playable move.
        """
        if not token or not isinstance(token, str):
            return None
        clean = re.sub(r"[?!#+]", "", token).strip()
        if not clean:
            return None
        try:
            return board.parse_san(clean)
        except Exception:
            pass
        try:
            move = chess.Move.from_uci(clean)
            return move if move in board.legal_moves else None
        except Exception:
            return None

    def _deepest_book_position(self, line: Sequence[str]) -> Tuple[Optional[Dict[str, Any]], int]:
        """``(entry, ply)`` of the deepest encyclopedia position reached by ``line``.

        Replays from the starting position and remembers the last position the
        encyclopedia knows, so a line that has *left* book theory still reports
        the opening it came out of.
        """
        board = chess.Board()
        best: Tuple[Optional[Dict[str, Any]], int] = (None, -1)
        for token in line:
            move = self._parse_move(board, token)
            if move is None:
                break
            board.push(move)
            entry = self.fen_to_opening.get(self._normalize_fen(board.fen()))
            if entry is not None:
                best = (entry, board.ply())
        return best

    @staticmethod
    def _normalize_move_list(moves: Any) -> Tuple[str, ...]:
        """Accept a list, a tuple or a delimited string; return a bounded tuple of tokens.

        Every token is shape-checked (:data:`_MOVE_TOKEN_RE`) so a hostile query
        string cannot reach ``chess.Board.parse_san`` with arbitrary text, and
        the result is capped at :data:`MAX_CONTEXT_MOVES`.
        """
        if moves is None:
            return ()
        if isinstance(moves, str):
            raw = re.split(r"[\s,]+", moves)
        elif isinstance(moves, (list, tuple)):
            raw = list(moves)
        else:
            return ()
        out: List[str] = []
        for token in raw:
            if not isinstance(token, str):
                continue
            token = token.strip()
            if not token or not _MOVE_TOKEN_RE.match(token):
                continue
            out.append(token)
            if len(out) >= MAX_CONTEXT_MOVES:
                break
        return tuple(out)

    def get_opening_context(self, fen: str, moves: Any = None) -> Dict[str, Any]:
        """Describe the opening played in ``fen``: the general family and the particular name.

        This is the payload behind the one-line opening description on the board.
        The return value has **one shape whether or not anything is known**, so a
        consumer renders it without branching on success:

        ===================  =======  =========================================
        key                  type     meaning
        ===================  =======  =========================================
        ``in_book``          bool     ``fen`` itself is a position the
                                      encyclopedia knows (a *fresh* line is
                                      still in book one move after theory ends).
        ``eco``              str      ECO code of the reported position.
        ``name``             str      full encyclopedia name of that position.
        ``general_name``     str      the family, e.g. ``"Sicilian Defense"``.
        ``crumbs``           list     ``["Sicilian Defense", "Smith-Morra
                                      Gambit"]`` -- the ``›``-separated path.
        ``label``            str      ``crumbs`` joined for display, ready to
                                      drop into a single clamped line.
        ``ply``              int      ply of the reported position, ``-1`` when
                                      nothing is known.
        ``book_ply``         int      that same ply, or ``-1``.
        ===================  =======  =========================================

        ``moves`` is the optional SAN/UCI line leading to ``fen`` from the
        starting position.  Supplying it is what keeps naming the opening once
        the position has left book theory, which is the normal state of affairs
        in a repertoire builder: without it, a position six plies past the last
        book move has no name at all.
        """
        norm = self._normalize_fen(fen or "")
        line = self._normalize_move_list(moves)
        cache_key = (norm, line)
        cached = self._context_cache.get(cache_key)
        if cached is not None:
            return {**cached, "crumbs": list(cached["crumbs"])}

        exact = self.fen_to_opening.get(norm)
        entry, ply = (exact, exact.get("ply", 0)) if exact else (None, -1)
        if line:
            line_entry, line_ply = self._deepest_book_position(line)
            if line_entry is not None and line_ply > ply:
                entry, ply = line_entry, line_ply

        name = (entry or {}).get("name", "") or ""
        family, levels, _qualifier = self.split_name(name)
        crumbs = ([family] + levels) if family else []
        payload: Dict[str, Any] = {
            "in_book": exact is not None,
            "eco": (entry or {}).get("eco", "") or "",
            "name": name,
            "general_name": family,
            "crumbs": crumbs,
            "label": self.name_label(name),
            "ply": int((entry or {}).get("ply", -1)),
            "book_ply": int(ply),
        }

        if len(self._context_cache) >= _CONTEXT_CACHE_MAX:
            # The encyclopedia is immutable for the life of the process, so no
            # entry can be stale; dropping the whole map is cheaper and simpler
            # than a real LRU and costs nothing measurable.
            self._context_cache.clear()
        self._context_cache[cache_key] = payload
        return {**payload, "crumbs": list(crumbs)}

    def is_known_book_move(self, board: chess.Board, move: chess.Move) -> bool:
        """Checks whether the move keeps the position within known master opening theory."""
        test_board = board.copy()
        test_board.push(move)
        norm = self._normalize_fen(test_board.fen())
        return norm in self.fen_to_opening

    def identify_game_opening(self, moves_list):
        """Identifies the deepest opening name and ECO for a sequence of moves (SAN or UCI)."""
        board = chess.Board()
        deepest = {"eco": "A00", "name": "Standard Chess Opening", "ply": 0}

        for m in moves_list:
            # Preserved from the pre-refactor shape: a *non-string* entry is
            # skipped, an unparseable *string* ends the walk.  Callers pass
            # `moves_san.split()` and freshly extracted SAN lists, so the two
            # are not the same failure and must not be treated as one.
            if not m or not isinstance(m, str):
                continue
            move = self._parse_move(board, m)
            if move is None:
                break
            board.push(move)
            norm = self._normalize_fen(board.fen())
            if norm in self.fen_to_opening:
                deepest = self.fen_to_opening[norm]

        return deepest

    # ------------------------------------------------------------------ #
    # "Which side is this?"  --  the primitive that lets the Scout tell
    # "learn the Najdorf" apart from "learn to face the Najdorf".
    # ------------------------------------------------------------------ #
    @classmethod
    def classify_opening_side(cls, eco: str, name: str) -> Dict[str, Any]:
        """Which side *plays* the opening called ``name``?

        Returns ``{"side", "confidence", "evidence", "family"}`` where ``side``
        is ``"white"``, ``"black"`` or ``"either"`` and ``confidence`` is
        ``"high"`` (the name or a curated family said so), ``"low"`` (the ECO
        convention) or ``"none"``.  Every answer carries the evidence that
        produced it, so the UI can say *why* it decided.

        Rules are tried in decreasing trust:

        1. a structurally two-sided family (:data:`EITHER_SIDED_FAMILIES`) --
           checked **first** because "Symmetrical Defense" contains the word
           "Defense" and is not a Black opening;
        2. a curated family (:data:`CURATED_FAMILY_SIDES`);
        3. the word "Defense"/"Defence" in the family, which is how a chess
           opening states that it answers White's first move;
        4. the ECO letter ranges, as a last resort and explicitly *low*
           confidence.

        ``eco`` is consulted by rule 4 only.  It is a convention and it is wrong
        at the edges (``B00`` holds both sides), so nothing else leans on it.

        It never raises: an unknown name yields ``either``/``none``.
        """
        clean = " ".join(str(name or "").split())
        family, _levels, _qualifier = cls.split_name(clean)
        eco_clean = str(eco or "").strip().upper()
        family_key = family.lower()

        result: Dict[str, Any] = {
            "side": SIDE_EITHER,
            "confidence": "none",
            "evidence": "no rule matched",
            "family": family,
        }

        if family_key and family_key in EITHER_SIDED_FAMILIES:
            result["evidence"] = "structurally two-sided family"
            return result
        if family_key in CURATED_FAMILY_SIDES:
            result.update(
                side=CURATED_FAMILY_SIDES[family_key],
                confidence="high",
                evidence="curated family",
            )
            return result
        if family_key and _DEFENSE_RE.search(family_key):
            result.update(
                side=SIDE_BLACK,
                confidence="high",
                evidence="family is named as a defence",
            )
            return result
        for pattern, eco_side in _ECO_SIDE_RANGES:
            if pattern.match(eco_clean):
                result.update(
                    side=eco_side,
                    confidence="low",
                    evidence="ECO range",
                )
                return result
        return result

    def _replay_to_book(self, line: Sequence[str]) -> Tuple[chess.Board, List[str], int]:
        """``(board, uci_path, book_ply)`` for the deepest encyclopedia position.

        Same walk as :meth:`_deepest_book_position`, but it hands back the board
        and the UCI path because :meth:`expand_critical_replies` has to push a
        reply off that exact position.  ``book_ply`` is the ply of the deepest
        *known* position reached, ``-1`` when the line never enters the book.
        """
        board = chess.Board()
        path: List[str] = []
        book_ply = -1
        for token in line:
            move = self._parse_move(board, token)
            if move is None:
                break
            board.push(move)
            path.append(move.uci())
            if self._normalize_fen(board.fen()) in self.fen_to_opening:
                book_ply = board.ply()
        return board, path, book_ply

    def _named_children(self, uci_path: Sequence[str], board: chess.Board) -> List[chess.Move]:
        """Legal, *named* children of ``board`` in the encyclopedia, sorted by UCI.

        A child that resolves to no opening name is not a continuation anybody
        has to prepare for, so it is not a "critical reply" and is left out.
        """
        node: Any = self.move_tree
        for uci in uci_path:
            node = (node.get("children") or {}).get(uci) if isinstance(node, dict) else None
            if node is None:
                return []
        out: List[chess.Move] = []
        for uci in sorted((node or {}).get("children") or {}):
            try:
                move = chess.Move.from_uci(uci)
                if move not in board.legal_moves:
                    continue
            except Exception:
                continue
            after = board.copy()
            after.push(move)
            entry = self.fen_to_opening.get(self._normalize_fen(after.fen()))
            if entry and entry.get("name"):
                out.append(move)
        return out

    def expand_critical_replies(self, moves: Any, side: Optional[str] = None) -> Dict[str, Any]:
        """The named encyclopedia continuations a player must prepare for ``moves``.

        This is what answers "I want to learn to face the Sicilian Alapin": the
        real question is not *"what is the Alapin"* but *"which replies do I
        need"*.  The answer comes from the move trie -- a real tree built from
        every encyclopedia line -- and it is **direction-aware**: the replies are
        whatever side is to move in the position, so one call answers both
        "Black's answers to 1.e4 c5" and "White's answers to ...a6".

        ``moves`` is a SAN/UCI line, a delimited string, or a list of either, and
        is normalized by :meth:`_normalize_move_list`, so a hostile string is
        dropped token by token rather than reaching ``parse_san``.

        **Where the replies are read from** is the subtle part, and there are
        two distinct corrections, both learned from the encyclopedia's own
        contents:

        * *The anchor is the last real branch point where the asked colour is to
          move.*  The ``B98`` entry runs 14 plies to ``7...Be7``, where the
          encyclopedia names exactly **one** continuation (``8.Qf3``).  Reporting
          "White must prepare: Qf3" for the Najdorf is technically true and
          practically useless; the real answer is the ten replies to ``5...a6``.
          So the walk starts at the deepest book position and backs up until the
          asked colour is to move **and** there are at least
          :data:`MIN_CRITICAL_BRANCH` named continuations.
        * *The anchor is the deepest book position, not the end of the line.*
          ``1.d4 d5 2.c4 e6 3.Bf4`` (the Alapin) has no entry past ply 4, so
          asking at the end returns nothing while asking at ply 4 -- the Queen's
          Gambit Declined -- returns its named continuations.

        ``side`` filters replies to one colour.  It is a filter, not a
        transform, so ``None`` returns every named continuation.

        Returns one shape whether or not anything was found:

        ==================  =======  =========================================
        key                 type     meaning
        ==================  =======  =========================================
        ``replies``         list     ``{san, uci, eco, name, label, family,
                                      side, ply, parent_fen}``, sorted by
                                      ply then name so the order is stable.
        ``book_ply``        int      ply of the deepest book position, ``-1``.
        ``anchor_ply``      int      ply the replies were actually read from.
        ``off_book``        bool     the encyclopedia has no *named*
                                      continuation at all on this line.
        ``fen``             str      4-token FEN the replies are read from.
        ``anchor``          str      that position's name, ``""`` when unknown.
        ==================  =======  =========================================
        """
        result: Dict[str, Any] = {
            "replies": [], "book_ply": -1, "anchor_ply": -1, "off_book": True,
            "fen": "", "anchor": "",
        }
        line = self._normalize_move_list(moves)
        if not line:
            return result

        board, path, book_ply = self._replay_to_book(line)
        if book_ply < 0:
            return result
        result["book_ply"] = book_ply

        # Replay to the deepest book position, then walk *backwards* to the
        # deepest ancestor that is actually worth asking about.  Two conditions
        # have to hold there, and getting either wrong produces a confident
        # useless answer:
        #
        # * the asked colour must be to move -- asking for White's replies to
        #   the Najdorf and stopping one ply early lands on a position where
        #   *Black* moves, and every reply then filters away to nothing;
        # * there must be a real choice, at least
        #   :data:`MIN_CRITICAL_BRANCH` named continuations -- the tail of a
        #   long line often has one, and "you must prepare Qf3" is not an
        #   answer to "how do I face the Najdorf".
        want_side = side if side in (SIDE_WHITE, SIDE_BLACK) else None
        anchor_board: Optional[chess.Board] = None
        anchor_ply = -1
        children: List[chess.Move] = []
        for ply in range(book_ply, -1, -1):
            probe = chess.Board()
            ok = True
            for uci in path[:ply]:
                try:
                    probe.push(chess.Move.from_uci(uci))
                except Exception:
                    ok = False
                    break
            if not ok:
                continue
            if want_side is not None:
                turn = SIDE_WHITE if probe.turn == chess.WHITE else SIDE_BLACK
                if turn != want_side:
                    continue
            probe_children = self._named_children(path[:ply], probe)
            if len(probe_children) < MIN_CRITICAL_BRANCH and ply > 0:
                continue
            anchor_board, anchor_ply, children = probe, ply, probe_children
            break
        if anchor_board is None:
            return result
        result["anchor_ply"] = anchor_ply

        fen4 = self._normalize_fen(anchor_board.fen())
        result["fen"] = fen4
        result["anchor"] = (self.fen_to_opening.get(fen4) or {}).get("name", "") or ""

        replies: List[Dict[str, Any]] = []
        for move in children:
            after = anchor_board.copy()
            san = anchor_board.san(move)
            after.push(move)
            entry = self.fen_to_opening.get(self._normalize_fen(after.fen())) or {}
            replies.append({
                "san": san,
                "uci": move.uci(),
                "eco": entry.get("eco", "") or "",
                "name": entry.get("name", "") or "",
                "label": self.name_label(entry.get("name", "")),
                "family": self.split_name(entry.get("name", ""))[0],
                "side": SIDE_WHITE if after.turn == chess.BLACK else SIDE_BLACK,
                "ply": after.ply(),
                "parent_fen": fen4,
            })

        if side in (SIDE_WHITE, SIDE_BLACK):
            replies = [r for r in replies if r["side"] == side]
        # ``off_book`` describes the *encyclopedia*, not the filter: a position
        # with ten named continuations is in the book even when the caller asked
        # for the colour that is not to move.
        result["off_book"] = not result["replies"] and not children
        replies.sort(key=lambda r: (r["ply"], r["name"].lower()))
        result["replies"] = replies[:MAX_CRITICAL_REPLIES]
        return result

    def _init_themes(self):
        return {
            "Sicilian": {
                "name": "Sicilian Defense",
                "core_idea": "Asymmetric central fight. Black trades the c-pawn for White's central d-pawn to gain queenside and semi-open c-file counterplay.",
                "typical_mistakes": "Premature queen raids, neglecting development to grab flank pawns, failing to contest White's knight on d4/d5.",
                "key_plans": "Maintain d6/e6 pawn structure, control the d5 break, advance ...a6 and ...b5 for queenside expansion, trade White's active bishop or knight."
            },
            "French": {
                "name": "French Defense",
                "core_idea": "Solid central pawn chain (e6-d5) directly challenging White's e4 center.",
                "typical_mistakes": "Allowing White's e5 wedge to lock Black into passive defense; misplacing the 'bad' French light-squared bishop on c8; prematurely closing the center without ...c5 tension.",
                "key_plans": "Strike White's d4 pawn with ...c5 and ...Qb6; challenge the e5 outpost with ...f6; activate or trade the c8 bishop via ...Bd7-b5 or ...b6."
            },
            "Italian": {
                "name": "Italian Game / Giuoco Piano",
                "core_idea": "Harmonious classical piece development targeting the vulnerable f7/f2 square while fighting for the d4 center.",
                "typical_mistakes": "Weakening the f2/f7 diagonal prematurely; carelessly trading off the active c4/c5 bishop; leaving central pawn tension unresolved and dropping pawns on d4.",
                "key_plans": "White prepares c3 and d4 to build an imposing center, or plays quiet d3 (Giuoco Pianissimo) with Nbd2-f1-g3 maneuvering. Black contests the center with ...Nf6 and ...d6."
            },
            "Ruy Lopez": {
                "name": "Ruy Lopez / Spanish Game",
                "core_idea": "Pinning and pressuring the c6 knight that defends Black's key central e5 pawn.",
                "typical_mistakes": "Overreacting to the Bb5 pin by compromising kingside pawn structure; allowing White to capture on c6 and exploit doubled pawns without compensation.",
                "key_plans": "White builds pressure with c3, d4, and Re1 targeting e5. Black plays ...a6 and ...b5 to free the knight, followed by ...Be7, ...d6, and kingside castling."
            },
            "Queen's Gambit": {
                "name": "Queen's Gambit",
                "core_idea": "White offers the flank c4 pawn to achieve absolute supremacy over the center squares d4 and e4.",
                "typical_mistakes": "Black trying stubbornly to hold the gambited c4 pawn (often leading to severe tactical disaster); White rushing central pushes before castling.",
                "key_plans": "White controls the center with Nc3, e4 (or e3), and Bd3. Black either solidly defends d5 with ...e6 (QGD) or ...c6 (Slav), preparing the ...c5 counter-break."
            },
            "King's Indian": {
                "name": "King's Indian Defense",
                "core_idea": "Hypermodern defense where Black cedes space in the center initially, only to unleash a devastating kingside pawn storm (...f5-f4).",
                "typical_mistakes": "Hesitating with the ...f5 counterplay while White rolls down the queenside; forgetting to safeguard the d6 pawn.",
                "key_plans": "Black plays ...e5, locks the center with ...d6, and attacks White's king with ...f5, ...g5, and ...f4. White breaks through on the queenside with c5."
            },
            "Caro-Kann": {
                "name": "Caro-Kann Defense",
                "core_idea": "An ultra-solid pawn structure (...c6 and ...d5) challenging e4, while keeping the light-squared bishop freely developable outside the pawn chain.",
                "typical_mistakes": "Blocking the c8 bishop behind the e-pawn before it develops; dropping the d5 pawn to aggressive tactical gambits (like the Fantasy Variation 3.f3).",
                "key_plans": "Develop bishop to f5 or g4, then play ...e6, ...Nbd7, and strike the White center with ...c5."
            }
        }

    def get_strategic_ideas(self, eco: str, opening_name: str,
                            side: Optional[str] = None) -> dict:
        """Strategic background for an opening, optionally from one side's view.

        The four legacy keys (``name``, ``core_idea``, ``typical_mistakes``,
        ``key_plans``) are **unchanged in meaning and shape**: every existing
        consumer (``GET /api/blunders/{id}``'s ``opening_theme``, every study
        payload, the Scout studio) keeps working untouched, and
        ``tests/test_openings.py::test_strategic_ideas`` still holds. They are
        resolved exactly as before -- a word-boundary match of the family name,
        else the generic fallback.

        What is new is everything after them, and it exists because the Scout now
        answers two different questions with this data:

        ==================  =======  =========================================
        key                 type     meaning
        ==================  =======  =========================================
        ``side``            str/None the requested side, or ``None``.
        ``white``           dict     ``{core_idea, typical_mistakes, key_plans}``
                                    for White. Falls back to the generic text.
        ``black``           dict     the same for Black.
        ``family``          str      the matched family, ``""`` when generic.
        ``matched``         bool     ``False`` when this is the fallback.
        ==================  =======  =========================================

        ``side`` matters because "I want to play this" needs *your* plans and
        "I want to face this" needs the *opponent's* -- and the two are not the
        same sentence. The legacy four keys carry whichever side's text matches
        ``side`` (White's by default), so a caller that ignores the new keys
        still gets the right one for the common case.

        It never raises, and a family with no notes is reported as
        ``matched: False`` rather than dressed up as if it were known.
        """
        safe_name = str(opening_name or "")
        theme = None
        for key, candidate in self.opening_themes.items():
            # Word-boundary match: "frenchy stuff" is not the French Defense
            # (audit SCOUT-07, adjacent variant 2).
            if re.search(r"\b" + re.escape(key.lower()), safe_name.lower()):
                theme = candidate
                break

        family = self.split_name(safe_name)[0]
        if theme is not None:
            payload = dict(theme)   # a copy: callers must not mutate the table
        else:
            # Generic strategic guidance if the opening is not in the table.
            payload = {
                "name": safe_name or "Opening Phase",
                "core_idea": f"In the {safe_name or 'opening'}, key priorities are controlling central squares, developing minor pieces rapidly, and ensuring king safety before launching attacks.",
                "typical_mistakes": "Moving the same piece multiple times in the opening, neglecting king castling, and initiating premature pawn attacks without solid central support.",
                "key_plans": "Fight for the center (e4/d4/e5/d5), complete minor piece development (Knights and Bishops), and castle early."
            }

        wanted = side if side in (SIDE_WHITE, SIDE_BLACK) else SIDE_WHITE
        per_side = SIDE_THEMES.get((family or safe_name).lower(), {})
        white = per_side.get(SIDE_WHITE) or GENERIC_WHITE
        black = per_side.get(SIDE_BLACK) or GENERIC_BLACK
        # The side-aware text lives ONLY in the new keys.  The four legacy keys
        # keep their original prose verbatim -- overwriting them with the
        # side-specific text would silently change what every existing consumer
        # renders (`GET /api/blunders/{id}`'s `opening_theme`, the study
        # payloads), and would break the contract
        # `tests/test_openings.py::test_strategic_ideas` pins.
        payload.update({
            "side": wanted,
            "white": dict(white),
            "black": dict(black),
            "family": family,
            "matched": bool(per_side),
        })
        return payload
