"""Scout discovery: what the user means, and which video answers it.

The Scout's original search was a single string -- ``f"{query} chess opening
guide"`` -- handed to YouTube, with the results rendered in YouTube's own order
and no notion of *which side* any of it was about.  That is why "Alapin"
returned an equal mixture of videos that teach you to **play** the Alapin and
videos that teach you to **face** it, interleaved and unlabelled, with the
most-viewed video first regardless of the question.

This module supplies the three things that were missing.

**1. Intent** (:func:`resolve_intent`) -- *which side do I play* and *do I want
to play this or play against it*.  Resolved from the query text, the named
opening's own side (:meth:`OpeningBook.classify_opening_side`) and a stored
preference, and it always reports the *evidence* so the UI can explain itself.

**2. Perspective** (:func:`classify_perspective`) -- *which side does this video
teach*.  The trap this exists to get right: "CRUSH the Sicilian Defense: The
ALAPIN" is a **White** video, because the Sicilian is Black's opening and the
word "crush" makes the named opening the *opponent's* move.  A classifier that
reads "crush" as "teaches Black" gets the single most-viewed result in the
search exactly backwards.

**3. Ranking** (:func:`score_result`) -- an intent score whose *components are
returned to the client*, so a card can say why it is on the shelf instead of
being an unexplained grid of thumbnails.

Everything here is pure and offline except :func:`discover` and
:func:`enrich_videos`, which are the only two functions that touch the network,
and both funnel every yt-dlp call through a single guarded helper.
"""

import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Tuple

import chess
import yt_dlp

from backend.db import (
    get_scouted_content,
    get_video_meta_rows,
    save_video_meta_rows,
)
from backend.openings import (
    OpeningBook,
    SIDE_BLACK,
    SIDE_EITHER,
    SIDE_WHITE,
)
from backend.scout import BASE_YDL_OPTS, format_seconds

logger = logging.getLogger("backend.scout.discovery")

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
MODE_PLAY = "play"            # I want to play this opening
MODE_AGAINST = "against"      # I want to meet this opening
MODE_EITHER = "either"        # show me both, labelled

SIDES = (SIDE_WHITE, SIDE_BLACK)

#: The user is asking how to **face** something.  Every alternative is a way of
#: saying "my opponent plays this": the *named* opening is therefore the
#: opponent's move, and the teacher plays the other side.  That inversion is the
#: whole reason :func:`classify_perspective` needs the named opening's side.
AGAINST_CUES = re.compile(
    r"\b(?:against|versus|vs\.?)\b"
    r"|\bcounters?(?:ing|play|attack|playback)?\b"
    r"|\bhow\s+(?:to|do\s+i|do\s+you|do\s+we)\s+(?:beat|defend|fight|handle|meet|answer|respond|solve|treat|repel|survive)\b"
    r"|\bbeat(?:ing|s)?\b|\bcrush(?:es|ing)?\b|\bkill(?:s|ing)?\b|\bdestroy(?:s|ing)?\b"
    r"|\bstop(?:s|ping)?\b|\bsurviv(?:e|es|ing)\b|\bavoid(?:s|ing)?\b"
    r"|\banti[-\s]|\brepel(?:s|ling)?\b|\bbrute[-\s]?force\b"
    r"|\bhow\s+(?:black|white)\s+(?:answers?|replies|respond|meets?)\b",
    re.IGNORECASE,
)

#: The user is asking how to **play** it themselves.
PLAY_CUES = re.compile(
    r"\bhow\s+to\s+play\b|\blearn(?:ing|s)?\b|\bstud(?:y|ies|ying)\b"
    r"|\b(?:for|as|with)\s+(?:white|black)\b"
    r"|\b(?:white|black)'s?\s+(?:repertoire|system|ideas?|attack|setup|game)\b"
    r"|\btutorial\b|\bexplained\b|\bintroduction\b|\bbasics\b|\bstrategy\b",
    re.IGNORECASE,
)

#: A video states its own side outright.  Deliberately *specific*: bare
#: ``\bblack\b`` would fire on "Blackburne Shilling Gambit" and on any title
#: that merely names a Black opening without teaching it.
_SIDE_PHRASES = (
    (SIDE_BLACK, re.compile(
        r"\b(?:as|for|with|playing|play)\s+(?:it\s+)?(?:as\s+|when\s+(?:you(?:'re| are)\s+)?)?black\b"
        r"|\bwhen\s+(?:you(?:'re| are)|playing)\s+black\b"
        r"|\bblack(?:'s|s')\s+(?:repertoire|system|ideas?|attack|setup|side|game|line|tactical|pawns?|structure|position)\b"
        r"|\bdefen[cs]e\s+with\s+black\b|\bblack\s+to\s+move\b",
        re.IGNORECASE)),
    (SIDE_WHITE, re.compile(
        r"\b(?:as|for|with|playing|play)\s+(?:it\s+)?(?:as\s+|when\s+(?:you(?:'re| are)\s+)?)?white\b"
        r"|\bwhen\s+(?:you(?:'re| are)|playing)\s+white\b"
        r"|\bwhite(?:'s|s')\s+(?:repertoire|system|ideas?|attack|setup|side|game|line|play|playstyle)\b"
        r"|\battack\s+with\s+white\b|\bwhite\s+to\s+move\b",
        re.IGNORECASE)),
)

_DEFENCE_WORD = re.compile(r"\bdefen[cs]e\b", re.IGNORECASE)

#: A **move** immediately after a relative cue, e.g. the "1.e4" in "the opening
#: grandmasters use against 1.e4".  This is the difference between the two
#: readings of "against":
#:
#: * "how to beat the **Najdorf**"  -- the cue names the *opening*, so it is
#:   the opponent's and the teacher plays the other side;
#: * "the opening used against **1.e4**" -- the cue names a *move*, so the video
#:   is about whoever **answers** that move, which for a defence is Black.
#:
#: Reading the second as the first is how a Giri video teaching the Najdorf got
#: labelled "teaches White".
_MOVE_AFTER_CUE = re.compile(
    r"^\W{0,3}(?:\d+\s*\.{1,3}\s*)?(?:[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=[qrbn])?[+#]?)\b"
)


def _relative_target_is_a_move(text: str, match_end: int) -> bool:
    """Does the relative cue at ``match_end`` point at a move rather than an opening?"""
    tail = text[match_end:match_end + 24]
    return bool(_MOVE_AFTER_CUE.match(tail))

#: Function words that carry no content of their own.  A query template is only
#: usable if at least one of its parts is *not* one of these, so a missing side
#: drops the whole clause instead of leaving a dangling "as".
_TEMPLATE_WORDS = frozenset({
    "how", "to", "the", "a", "an", "of", "for", "with", "as", "and", "or",
    "chess", "opening", "guide", "beat", "against", "versus", "vs",
})

# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #
MAX_QUERY_CHARS = 200
#: YouTube queries fired per discovery.  Measured at 0.7-0.9s each, run in
#: parallel, so four cost about as much wall-clock as the single query the Scout
#: used to send.
MAX_DISCOVER_QUERIES = 4
#: Videos merged into one shelf across all queries, after de-duplication.
MAX_DISCOVER_RESULTS = 24
#: Videos whose chapters/description/date are fetched.  Measured: 6 videos in
#: 3.4s wall with 4 workers, so the second paint is ~3s and never blocks the
#: first one.
MAX_ENRICH_VIDEOS = 8
ENRICH_WORKERS = 4
#: Candidates offered for disambiguation.
MAX_INTENT_CANDIDATES = 8
#: Metadata older than this is refetched: descriptions get edited, chapters get
#: added, and a view count is stale within weeks.
VIDEO_META_TTL_SECONDS = 30 * 24 * 3600
_YEAR_SECONDS = 365 * 24 * 3600
#: Column-safe truncation for the strings this module caches and returns.
MAX_TITLE_CHARS_DISCOVERY = 300
MAX_DESCRIPTION_CHARS_DISCOVERY = 8_000
#: The card shows a 2-line description, so 600 chars is already more than fits.
MAX_CARD_DESCRIPTION_CHARS = 600


# --------------------------------------------------------------------------- #
# Intent
# --------------------------------------------------------------------------- #
def detect_mode(query: str) -> Tuple[str, Optional[str]]:
    """``(mode, evidence)`` for the words in ``query`` alone.

    ``AGAINST`` wins the tie because "how to beat X with 1.d4" contains both
    cue families and means *against* -- the whole phrase is about meeting X.
    """
    text = query or ""
    against = AGAINST_CUES.search(text)
    if against:
        return MODE_AGAINST, against.group(0)
    play = PLAY_CUES.search(text)
    if play:
        return MODE_PLAY, play.group(0)
    return MODE_EITHER, None


def _opposite(side: Optional[str]) -> Optional[str]:
    if side == SIDE_WHITE:
        return SIDE_BLACK
    if side == SIDE_BLACK:
        return SIDE_WHITE
    return None


def _describe_candidate(item: Dict[str, Any]) -> Dict[str, Any]:
    """One disambiguation chip: identity, side, and the line it stands for."""
    name = str(item.get("name") or "")
    eco = str(item.get("eco") or "")
    family, levels, _qualifier = OpeningBook.split_name(name)
    side_info = OpeningBook.classify_opening_side(eco, name)
    pgn = str(item.get("pgn") or "")
    moves = [t for t in pgn.split() if not t.endswith(".") and not t[:1].isdigit()]
    return {
        "eco": eco,
        "name": name,
        "label": OpeningBook.name_label(name) or name,
        # The encyclopedia name contains a colon ("Sicilian Defense: Najdorf
        # Variation") because the TSV encodes its hierarchy there.  Colons are
        # excellent in a UI and terrible in a search box -- YouTube reads
        # "Sicilian Defense: Najdorf Variation for black" as one odd phrase, so
        # the query form is flattened to spaces.
        "search_name": " ".join([family] + list(levels)).strip() or name,
        "family": family,
        "levels": levels,
        "side": side_info["side"],
        "side_confidence": side_info["confidence"],
        "side_evidence": side_info["evidence"],
        "pgn": pgn,
        "move_count": len(moves),
        "opening_moves": moves[:8],
    }


#: Words that describe the *request*, not the opening.  Stripped before the raw
#: query is used as a search name, so "how to beat the Alapin" does not become
#: the query "how to beat the how to beat the Alapin as black".
_REQUEST_WORDS = re.compile(
    r"\b(?:how|to|the|a|an|of|do|i|you|we|me|my|play|playing|learn|study|studying|"
    r"beat|beating|crush|crushing|defend|defending|face|facing|meet|meeting|handle|"
    r"handling|answer|answers|answering|respond|response|counter|countering|kill|"
    r"destroy|stop|avoid|survive|repel|against|versus|vs|repertoire|opening|guide|"
    r"tips|tricks|explained|explanation|ideas?|strategy|for|with|as|and|or|white|black)\b",
    re.IGNORECASE,
)


def _search_name_from_query(query: str) -> str:
    """The opening-ish part of a raw query, with the request words removed."""
    stripped = _REQUEST_WORDS.sub(" ", str(query or ""))
    stripped = " ".join(stripped.split())
    if len(stripped) >= 3:
        return stripped[:MAX_QUERY_CHARS]
    return " ".join(str(query or "").split())[:MAX_QUERY_CHARS]


def _distinctive_words(name: str) -> List[str]:
    """The words in an opening name that actually identify it.

    ``TITLE_STOPWORDS`` already removes "defense"/"variation"/"gambit" from
    matching, so the leftovers are the part worth putting in a search box.
    """
    words = re.findall(r"[a-z0-9]+", str(name or "").lower())
    kept = [w for w in words if w not in OpeningBook.TITLE_STOPWORDS and len(w) > 2]
    # Longest first: "Najdorf" identifies the opening, "sicilian" its family.
    return sorted(set(kept), key=lambda w: (-len(w), w))


def resolve_intent(query: str,
                   side_pref: Optional[str] = None,
                   mode_pref: Optional[str] = None,
                   chosen_eco: Optional[str] = None,
                   eco_limit: int = MAX_INTENT_CANDIDATES) -> Dict[str, Any]:
    """Work out *which side* and *play-or-against* a Scout query is asking for.

    The resolution order is deliberate and each step is reported:

    1. **text** -- "against the Alapin" is unambiguous on its own;
    2. **the named opening's side** -- naming a Black defence means you are
       Black when you want to *play* it and White when you want to *face* it;
    3. **your stored preference** -- the last explicit choice wins over nothing;
    4. **honest ignorance** -- ``side: None`` means "not known", and the caller
       must not substitute a guess.

    ``needs_disambiguation`` is true when the encyclopedia matches span more than
    one *family*, which is the precise condition under which a name is
    ambiguous.  "Najdorf" resolves inside the Sicilian family and needs no
    chips; **"Alapin" resolves to five families** -- Sicilian, French, Slav,
    Dutch and King's Pawn -- and guessing between them is exactly the bug this
    whole feature exists to remove.  Passing ``chosen_eco`` is the user picking
    one of those chips: the chosen entry becomes ``primary`` and
    ``candidate_chosen`` is set, which is what lets :func:`discover` proceed
    instead of asking again.

    The return value always has the same shape, whatever was found:

    ==================  =======  =========================================
    key                 type     meaning
    ==================  =======  =========================================
    ``query``           str      the (bounded) query text.
    ``side``            str      ``"white"``, ``"black"`` or ``None``.
    ``mode``            str      ``"play"``, ``"against"`` or ``"either"``.
    ``side_source``     str      where the side came from.
    ``mode_source``     str      where the mode came from.
    ``reasons``         list     human-readable, shown in the intent bar.
    ``candidates``      list     :func:`_describe_candidate` rows.
    ``primary``         dict/None the best candidate, ``None`` if nothing matched.
    ``families``        list     distinct families among the candidates.
    ``needs_disambiguation``  bool  more than one family matched.
    ``candidate_chosen``  bool  the caller named one explicitly.
    ``in_eco_book``     bool     at least one encyclopedia match.
    ==================  =======  =========================================
    """
    raw = " ".join(str(query or "").split())[:MAX_QUERY_CHARS]
    book = OpeningBook.get_instance()

    reasons: List[str] = []
    candidates: List[Dict[str, Any]] = []
    try:
        matches = book.search_openings(raw, limit=max(1, min(int(eco_limit or 1), 20))) if raw else []
    except Exception as exc:            # the encyclopedia is in-process; still fail soft
        logger.warning("ECO resolution failed for %r: %s", raw, exc)
        matches = []

    seen_labels = set()
    for item in matches or []:
        cand = _describe_candidate(item)
        # The encyclopedia stores one entry per *depth* of a line, so a single
        # opening comes back six times ("Najdorf" -> B90, B94, B95, B96, B98,
        # B99, all named identically).  Six identical chips is not a choice, so
        # candidates collapse to one per distinct label and the **deepest** line
        # wins -- that is the one with the most theory behind it.
        key = cand["label"].lower()
        if not key or key in seen_labels:
            existing = next((c for c in candidates if c["label"].lower() == key), None)
            if existing is not None and cand["move_count"] > existing["move_count"]:
                existing.update(cand)
            continue
        seen_labels.add(key)
        candidates.append(cand)
        if len(candidates) >= MAX_INTENT_CANDIDATES:
            break

    families = []
    for cand in candidates:
        if cand["family"] and cand["family"] not in families:
            families.append(cand["family"])
    needs_disambiguation = len(families) > 1

    # -- mode ------------------------------------------------------------- #
    mode, cue = detect_mode(raw)
    if cue:
        mode_source = "text"
        reasons.append('"%s" reads as: play against it' % cue.strip())
    elif mode_pref in (MODE_PLAY, MODE_AGAINST):
        mode, mode_source = mode_pref, "preference"
    else:
        mode, mode_source = MODE_EITHER, "none"

    # -- primary ---------------------------------------------------------- #
    primary = candidates[0] if candidates else None
    candidate_chosen = False
    wanted = str(chosen_eco or "").strip()
    if wanted:
        for cand in candidates:
            if cand["eco"] == wanted or cand["label"].lower() == wanted.lower():
                primary = cand
                candidate_chosen = True
                break
        if candidate_chosen:
            needs_disambiguation = False
            reasons.append("you chose %s" % (primary.get("label") or primary.get("name")))

    # -- side ------------------------------------------------------------- #
    named_side = primary.get("side") if primary else None
    side: Optional[str] = None
    side_source = "none"

    if side_pref in SIDES:
        side, side_source = side_pref, "preference"
    elif needs_disambiguation:
        # The name is ambiguous, so the top match's side is not *the* side --
        # "Alapin" ranks the Ruy Lopez first, which would silently assert White
        # when almost every Alapin in the book is a Black opening.  The majority
        # side across the matches is the defensible answer, and it is reported
        # as a guess rather than as a fact.
        tally = {SIDE_WHITE: 0, SIDE_BLACK: 0}
        for cand in candidates:
            if cand["side"] in tally:
                tally[cand["side"]] += 1
        if max(tally.values()) > 0:
            side = max(tally, key=lambda k: tally[k])
            side_source = "candidate_majority"
            reasons.append("%d of %d matches are %s openings"
                           % (tally[side], len(candidates), side))
    elif named_side in SIDES:
        if mode == MODE_AGAINST:
            # "I want to face the Najdorf" -> I am not the Najdorf.
            side, side_source = _opposite(named_side), "named_opening"
            reasons.append("%s is a %s opening, so you meet it as %s"
                           % (primary["name"], named_side, side))
        else:
            side, side_source = named_side, "named_opening"
            reasons.append("%s is a %s opening" % (primary["name"], named_side))
    if not candidates and mode == MODE_AGAINST:
        reasons.append("no encyclopedia match, so only your wording is available")

    if needs_disambiguation:
        reasons.append('"%s" names %d different openings -- choose one'
                       % (raw, len(candidates)))

    return {
        "query": raw,
        "side": side,
        "mode": mode,
        "side_source": side_source,
        "mode_source": mode_source,
        "reasons": reasons,
        "candidates": candidates,
        "primary": primary,
        "families": families,
        "needs_disambiguation": needs_disambiguation,
        "candidate_chosen": candidate_chosen,
        "in_eco_book": bool(candidates),
    }


def _eco_guides(intent: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The encyclopedia master lines behind a discovery, in the card shape.

    Each guide carries its own ``strategic_ideas``, resolved for **the side the
    user actually plays**: in *against* mode that is the opponent's side, because
    knowing what you are about to be attacked with is the point. Without it the
    Scout would have to open a study to learn anything about the opening, and the
    one thing a search result cannot do is make the user do extra work.
    """
    book = OpeningBook.get_instance()
    asked = intent.get("side")
    mode = intent.get("mode", MODE_EITHER)
    # Facing the opening means reading the side that plays it.
    wants_opponent = mode == MODE_AGAINST
    candidates = list(intent.get("candidates") or [])
    # The chosen chip leads. Without this the shelf would show the *top* match
    # first, which for an ambiguous name is a different opening entirely -- pick
    # the Sicilian Alapin and get the Ruy Lopez's master line and notes.
    chosen = intent.get("primary") or {}
    chosen_eco = chosen.get("eco")
    if chosen_eco:
        candidates.sort(key=lambda c: 0 if c.get("eco") == chosen_eco else 1)
    guides: List[Dict[str, Any]] = []
    for cand in candidates[:4]:
        view_side = _opposite(asked) if (wants_opponent and asked) else asked
        try:
            ideas = book.get_strategic_ideas(cand["eco"], cand["name"], view_side)
        except Exception as exc:
            logger.warning("Strategic ideas failed for %s: %s", cand["eco"], exc)
            ideas = {}
        guides.append({
            "source_type": "eco_guide",
            "id": "eco_%s_%s" % (cand["eco"], cand["name"].replace(" ", "_")),
            "url": "",
            "title": "%s - %s (Master Repertoire Line)" % (cand["eco"], cand["name"]),
            "label": cand["label"],
            "channel": "ECO Master Encyclopedia",
            "duration": 0,
            "duration_formatted": "Theory Guide",
            "thumbnail": "/assets/pieces/wN.svg",
            "view_count": None,
            "is_analyzed": True,
            "is_favorite": False,
            "eco": cand["eco"],
            "opening_name": cand["name"],
            "side": cand["side"],
            "side_confidence": cand["side_confidence"],
            "pgn": cand["pgn"],
            "move_count": cand["move_count"],
            "strategic_ideas": ideas,
        })
    return guides


# --------------------------------------------------------------------------- #
# Perspective: which side does a video teach?
# --------------------------------------------------------------------------- #
def classify_perspective(title: str,
                         description: str = "",
                         chapters: Optional[Sequence[Any]] = None,
                         named_side: Optional[str] = None,
                         named_name: str = "") -> Dict[str, Any]:
    """Which side does this video **teach**, and how sure are we?

    Returns ``{"side", "mode", "confidence", "evidence"}`` where ``side`` is the
    colour being taught, ``mode`` is ``"play"``/``"against"``/``None`` and
    ``confidence`` is ``"high"``/``"medium"``/``"none"``.

    The order matters, and the first rule is the one that fixes the bug this
    module exists for:

    1. an **explicit** side statement ("play the Alapin **as Black**") wins
       outright -- the creator told us;
    2. a **relative** cue ("crush/beat/against X") is only meaningful once the
       named opening's side is known, and it *inverts* it: you cannot crush the
       Sicilian as Black, because the Sicilian **is** Black's opening;
    3. naming a defence in the title means the video is about the defence, so
       the teacher plays that side -- weaker, hence ``"medium"``;
    4. otherwise the honest answer is ``side: None``, and the UI shows "?"
       rather than inventing a colour.  Several genuinely good videos are only
       classifiable from their chapters, which is what :func:`enrich_videos`
       fetches.

    Never raises; unusable input yields ``{"side": None, "confidence": "none"}``.
    """
    empty = {"side": None, "mode": None, "confidence": "none", "evidence": ""}
    chapter_titles = []
    for ch in chapters or []:
        if isinstance(ch, dict):
            title_text = ch.get("title")
        else:
            title_text = ch
        if isinstance(title_text, str) and title_text.strip():
            chapter_titles.append(title_text.strip())
    text = " ".join(
        [str(title or ""), " ".join(chapter_titles), str(description or "")[:1_000]]
    ).strip()
    if not text:
        return empty

    mode: Optional[str] = None
    relative = AGAINST_CUES.search(text)
    if relative:
        mode = MODE_AGAINST

    for side, pattern in _SIDE_PHRASES:
        found = pattern.search(text)
        if found:
            return {
                "side": side,
                "mode": mode,
                "confidence": "high",
                "evidence": found.group(0).strip()[:80],
            }

    if mode == MODE_AGAINST and named_side in SIDES:
        if _relative_target_is_a_move(text, relative.end()):
            # "against 1.e4" / "a response to 1.d4": the cue names a *move*, so
            # the video is about the side that answers it, not the side that
            # faces the opening.
            return {
                "side": named_side,
                "mode": MODE_PLAY,
                "confidence": "high",
                "evidence": '"%s <move>" -- the side that answers that move'
                            % relative.group(0).strip(),
            }
        return {
            "side": _opposite(named_side),
            "mode": MODE_AGAINST,
            "confidence": "high",
            "evidence": '%s: "%s" means facing the %s'
                        % (named_name or "named opening", relative.group(0), named_side),
        }

    if named_side in SIDES and _DEFENCE_WORD.search(text):
        return {
            "side": named_side,
            "mode": MODE_PLAY,
            "confidence": "medium",
            "evidence": "teaches the %s defence" % named_side,
        }

    if mode == MODE_AGAINST:
        return {"side": None, "mode": MODE_AGAINST, "confidence": "none",
                "evidence": 'mentions "%s" but never says which side' % relative.group(0)}
    return empty


# --------------------------------------------------------------------------- #
# Query fan-out
# --------------------------------------------------------------------------- #
def _clean_query(text: str) -> str:
    """Tidy an assembled query: no dangling preposition, no repeated phrase.

    Two defects this exists to remove, both of which produce a query YouTube
    answers with nothing:

    * a template whose variable was empty leaves its preposition behind, so
      "how to beat the {name} as {side}" with no side becomes "how to beat the
      Alapin as" -- the trailing "as" is a stop-word with nothing after it;
    * a template that names both the opening and its family repeats the family,
      giving "Sicilian Defense Alapin Variation Sicilian Defense for black".

    Only the **tail** is stripped.  Leading function words are load-bearing:
    "how to beat the" is made entirely of them and is precisely what turns a
    name into a *how-do-I-meet-this* query, so trimming the head would silently
    delete the intent and reduce every "against" search to the plain opening
    name -- which is the exact query the Scout sent before this existed.
    """
    tokens = str(text or "").split()
    while tokens and tokens[-1].lower() in _TEMPLATE_WORDS:
        tokens.pop()
    words = [t.lower() for t in tokens]
    if not words:
        return ""
    # Cut at the start of a bigram that already occurred, keeping the first.
    for size in (4, 3, 2):
        seen = set()
        cut = None
        for i in range(len(words) - size + 1):
            gram = tuple(words[i:i + size])
            if gram in seen:
                cut = i
                break
            seen.add(gram)
        if cut:
            tokens = tokens[:cut]
            words = [t.lower() for t in tokens]
    return " ".join(tokens)[:MAX_QUERY_CHARS].strip()


def build_queries(intent: Dict[str, Any],
                  replies: Optional[Sequence[Dict[str, Any]]] = None,
                  max_queries: int = MAX_DISCOVER_QUERIES) -> List[str]:
    """The YouTube queries that could answer ``intent``, best first.

    The Scout used to send exactly one: ``f"{query} chess opening guide"``.
    That string is wrong for half of all questions -- "the Najdorf **chess
    opening guide**" returns videos for people who want the Najdorf *and* people
    who want to beat it, and the API gives no way to tell them apart downstream.

    So the queries are built from the *resolved* intent: the opening's name, its
    family, the side, and the mode.  A "play the Najdorf as Black" intent gets
    ``Sicilian Defense Najdorf Variation`` and ``... for black``; a "face the
    Najdorf as White" intent gets ``how to beat the Sicilian Defense Najdorf
    Variation as white``, which is what actually surfaces the English Attack and
    the Chelyabinsk.

    Duplicates are dropped, order is preserved, and the total is capped so one
    request cannot fan out into a crawl.  Every query is run through
    :func:`_clean_query`, so a template that lost its variable is dropped rather
    than searched for as a phrase ending in a preposition.
    """
    primary = intent.get("primary") or {}
    # Prefer the flattened `search_name`; fall back to the query with its
    # request words stripped.  Never the raw query: "how to beat the Alapin"
    # would otherwise be searched for as "how to beat the how to beat the
    # Alapin as black".
    name = str(primary.get("search_name") or "").strip()
    if not name:
        name = _search_name_from_query(intent.get("query") or "")
    if not name:
        return []
    family = str(primary.get("family") or "")
    eco = str(primary.get("eco") or "")
    side = intent.get("side")
    mode = intent.get("mode", MODE_EITHER)
    side_word = side or ""

    def add(bucket: List[str], *parts: Any) -> None:
        pieces = [str(p).strip() for p in parts if p and str(p).strip()]
        if not pieces:
            return
        candidate = _clean_query(" ".join(pieces))
        # A template is only worth searching when something in it is content.
        # "how to beat the Alapin as" with no side cleans down to "how to beat
        # the Alapin", which *is* a good query, so a single content word is
        # enough -- "Najdorf" on its own finds the Najdorf.
        if not any(w.lower() not in _TEMPLATE_WORDS for w in candidate.split()):
            return
        if candidate.lower() not in {q.lower() for q in bucket}:
            bucket.append(candidate)

    play_q: List[str] = []
    against_q: List[str] = []
    if mode in (MODE_PLAY, MODE_EITHER):
        add(play_q, name)
        add(play_q, name, "chess opening guide", "for", side_word)
        if family and family.lower() not in name.lower():
            add(play_q, eco, family)
    if mode in (MODE_AGAINST, MODE_EITHER):
        add(against_q, name, family, "for", side_word)
        add(against_q, "how to beat the", name, "as", side_word)
        add(against_q, "against the", name, side_word)
    if not primary:
        # Nothing in the encyclopedia, so the user's own phrasing is the best
        # signal available -- and it already contains the intent ("how to beat
        # the Alapin"), which the generated templates would have to reconstruct.
        add(against_q if mode == MODE_AGAINST else play_q,
            _clean_query(intent.get("query") or ""))

    ordered = against_q + play_q if mode == MODE_AGAINST else play_q + against_q

    # Only spend the remaining budget on individual critical replies when they
    # are actually distinct openings -- otherwise the fan-out degenerates into
    # the same line queried under five names.
    for reply in list(replies or []):
        if len(ordered) >= max_queries:
            break
        reply_name = _clean_query(" ".join(
            [str(reply.get("family") or "")] + [
                lvl for lvl in OpeningBook.split_name(str(reply.get("name") or ""))[1]
            ]
        )) or str(reply.get("name") or "").strip()
        if not reply_name or reply_name.lower() == name.lower():
            continue
        add(ordered, reply_name, "for", side_word)

    # Dedupe across both buckets: a mode of `either` puts the same string in
    # each, and a repeated query wastes one of the four we are allowed.
    final: List[str] = []
    for q in ordered:
        if q.lower() not in {seen.lower() for seen in final}:
            final.append(q)
    return final[:max_queries]


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
def _recency_score(upload_date: str) -> float:
    """0.0 for an unknown/absurd date, decaying to 0.0 after ~6 years."""
    if not upload_date or len(upload_date) != 8 or not upload_date.isdigit():
        return 0.0
    try:
        stamp = time.mktime(time.strptime(upload_date, "%Y%m%d"))
    except Exception:
        return 0.0
    age = max(0.0, (time.time() - stamp) / _YEAR_SECONDS)
    return max(0.0, 1.0 - age / 6.0)


def score_result(item: Dict[str, Any], intent: Dict[str, Any]) -> Dict[str, Any]:
    """Rank one result against ``intent``, returning the score *and its reasons*.

    The score is split into ``intent_score`` (does this answer my question?) and
    ``quality_score`` (is it worth watching?), because they answer different
    questions and a 2-minute Chessable video routinely outranks a 53-minute
    Naroditsky course on the first while losing badly on the second.  Both
    components are returned, and the reasons list is what the card renders, so
    an unexplained grid of thumbnails becomes a list that argues its own case.

    Side is the heaviest term on purpose.  A video that teaches the *other*
    side is not a slightly worse answer, it is a different answer.
    """
    reasons: List[str] = []
    score = 0.0
    want_side = intent.get("side")
    want_mode = intent.get("mode", MODE_EITHER)
    primary = intent.get("primary") or {}
    named_side = primary.get("side")
    named_name = str(primary.get("name") or "")

    teach_side = item.get("side_teach") or None
    teach_mode = item.get("mode_teach") or None

    # -- 1. side, the heaviest term ------------------------------------- #
    if want_side in SIDES:
        if teach_side == want_side:
            score += 40
            reasons.append("teaches %s, your side" % want_side)
        elif teach_side is None:
            score += 12
            reasons.append("side not stated")
        else:
            score -= 25
            reasons.append("teaches %s, not %s" % (teach_side, want_side))

    # -- 2. mode -------------------------------------------------------- #
    if want_mode in (MODE_PLAY, MODE_AGAINST) and teach_mode == want_mode:
        score += 35
        reasons.append("play it" if want_mode == MODE_PLAY else "play against it")
    elif want_mode == MODE_AGAINST and teach_mode == MODE_PLAY and teach_side == _opposite(want_side):
        # A video on the named opening itself, taught by the side that owns it.
        # Relevant context, but not the answer to "how do I meet it".
        score += 8
        reasons.append("about the opening itself, not the defence against it")

    # -- 3. is it even about this opening? ------------------------------ #
    title = str(item.get("title") or "")
    haystack = (title + " " + str(item.get("description") or "")).lower()
    family = str(primary.get("family") or "").lower()
    if family and family in haystack:
        score += 20
        reasons.append("names %s" % primary.get("family"))
    hit_words = [w for w in _distinctive_words(named_name) if w in haystack]
    if hit_words:
        score += min(15, 5 * len(hit_words))
        reasons.append("names %s" % hit_words[0])
    other = _other_family_in_text(haystack, family, named_name)
    if other:
        score -= 20
        reasons.append("also about %s" % other)

    if item.get("source_type") == "eco_guide":
        score += 12
        reasons.append("master line in the encyclopedia")

    # -- quality, reported but never mixed into intent ------------------ #
    views = item.get("view_count") or 0
    try:
        views = max(0, int(views))
    except (TypeError, ValueError):
        views = 0
    quality = 0.0
    if views:
        quality += min(20.0, 4.0 * math.log10(views + 1))
    chapters = item.get("chapters") or []
    if len(chapters) >= 4:
        quality += min(15.0, 1.5 * len(chapters))
    quality += 10.0 * _recency_score(str(item.get("upload_date") or ""))
    if item.get("is_analyzed"):
        quality += 8.0
        reasons.append("already extracted — reopens instantly")
    if item.get("is_favorite"):
        quality += 20.0

    if score >= 55:
        bucket = "exact"
    elif score >= 25:
        bucket = "related"
    else:
        bucket = "course"

    return {
        "intent_score": round(score, 2),
        "quality_score": round(quality, 2),
        "bucket": bucket,
        "reasons": reasons[:5],
    }


def _other_family_in_text(haystack: str, own_family: str, own_name: str) -> str:
    """A *different* opening family named in ``haystack``, or ``""``.

    Without this, a title like "Alapin vs Trompowsky" scores as a good Alapin
    video while being mostly about a different opening.  The comparison is
    against the encyclopedia's own families, so it cannot invent a conflict out
    of a shared word like "defense".
    """
    if not haystack:
        return ""
    own = str(own_name or "").lower()
    families = OpeningBook.get_instance().opening_family_index
    for family in families:
        low = family.lower()
        if not low or low in own or low == str(own_family or "").lower():
            continue
        if low in haystack and len(low) > 5:
            return family
    return ""


# --------------------------------------------------------------------------- #
# Coverage: what do I still have to prepare?
#
# This is the half that answers "my needs exactly". A shelf of theory is only
# useful next to the question "which of these do I actually lack?", and the
# honest answer is a *read* of the user's own repertoire, not another search.
# --------------------------------------------------------------------------- #

#: The per-reply states, named after `backend/repertoire.py`'s `gap_kind` values
#: so the Scout and the Builder say the same words about the same position.
#: §4.G ruled that a branch you hold but cannot answer is a gap of the *same
#: kind* as one you do not hold at all -- both finish the line on the opponent's
#: move -- so it is counted as a gap here for exactly that reason.
COVERAGE_PREPARED = "prepared"
COVERAGE_UNANSWERED = "unanswered_branch"    # §4.G: held, but nothing after it
COVERAGE_MISSING = "missing_branch"          # not held at all

#: Overall verdicts.
COVERAGE_COVERED = "covered"
COVERAGE_GAPS = "gaps"
COVERAGE_EMPTY = "empty"
COVERAGE_UNKNOWN = "unknown"


def _fen3(fen: str) -> str:
    """The first **three** FEN tokens: piece placement, side to move, castling.

    Deliberately drops the en-passant square. With a legal en-passant square one
    logical position has two spellings (``… w KQkq d6`` and ``… w KQkq -``), so a
    4-token comparison can call a dead end covered, or a covered line missing.
    `backend/repertoire.py::get_repertoire_tree` makes the same 3-token
    comparison for exactly that reason (its `user_move_parents3`), and the two
    surfaces must agree or one position gets two different verdicts.
    """
    return " ".join(str(fen or "").split()[:3])


def _repertoire_index(conn, repertoire: str) -> Tuple[Dict[str, set], Dict[str, set]]:
    """``({parent3 -> {opponent ucis}}, {parent3 -> {user ucis}})`` for one repertoire.

    Two indexes, because the two roles are genuinely different rows:

    * an **opponent row** (``is_user_move = 0``) is a branch the repertoire
      *holds* -- what the user's opponent is expected to play;
    * a **user row** (``is_user_move = 1``) is a move the user can *play*.

    §4.G's rule is stated in those terms: a held branch is covered only when a
    user move follows it. Reading the wrong index reports every reply as
    "not prepared" even when the branch is held, and the mirror of that reports
    a branch the user cannot answer as covered.

    One pass over the repertoire -- a few hundred rows, so far cheaper than a
    query per reply -- and a pure read (§9's "read endpoints perform no writes").
    """
    cursor = conn.execute(
        "SELECT parent_fen, move_uci, is_user_move FROM repertoire_moves WHERE repertoire = ?",
        (repertoire,),
    )
    branches: Dict[str, set] = {}
    answers: Dict[str, set] = {}
    for parent_fen, uci, is_user in cursor.fetchall():
        target = answers if is_user else branches
        target.setdefault(_fen3(parent_fen), set()).add(uci or "")
    return branches, answers


def _fen_after(parent_fen: str, uci: str) -> str:
    """The 4-token FEN after playing ``uci`` in ``parent_fen``; ``""`` if impossible.

    Built from the *stored* 4-token FEN, which is enough for `python-chess` to
    play on: side to move, castling rights and an en-passant square are all the
    move generator needs, and the halfmove/fullmove counters are irrelevant to it.

    A position that cannot be built is the *expected* outcome for junk input and
    returns ``""`` quietly; anything else is a bug (a `NameError` from a missing
    import once made every reply read as "unanswered"), so it is logged rather
    than swallowed.
    """
    if not parent_fen or not uci:
        return ""
    try:
        board = chess.Board(parent_fen + " 0 1")
    except Exception:
        return ""          # a position python-chess will not parse: junk in, "" out
    try:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            return ""      # not a legal move here: nothing to compute
        board.push(move)
        return " ".join(board.fen().split()[:4])
    except Exception as exc:
        logger.warning("Could not play %r on a known position: %s", uci, exc)
        return ""


def repertoire_coverage(side: Optional[str],
                        replies: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Cross the critical replies against the user's own repertoire.

    The replies come from :meth:`OpeningBook.expand_critical_replies` -- the
    continuations the *opponent* may play -- and this decides, for each one,
    whether the repertoire **holds that branch** and whether the user can
    **answer** it. That is the question a shelf of theory cannot answer for you,
    and it is stated in §4.G's own terms:

    * ``missing_branch`` -- the repertoire does not hold this branch at all;
    * ``unanswered_branch`` -- it holds it, and the user has nothing prepared
      after it, so the line stops on the *opponent's* move;
    * ``prepared`` -- held **and** answerable.

    Both indexes are keyed by the **3-token** parent (see :func:`_fen3`), which is
    what makes this verdict agree with the Builder's for a position that has a
    legal en-passant square.

    Returns one shape whatever happened, including for an empty repertoire:

    ==================  =======  =========================================
    key                 type     meaning
    ==================  =======  =========================================
    ``side``            str      the repertoire that was inspected.
    ``state``           str      ``covered`` / ``gaps`` / ``empty`` /
                               ``unknown`` (:data:`COVERAGE_COVERED` &c.).
    ``total``           int      replies considered.
    ``prepared``        int      replies held **and** answerable.
    ``unanswered``      int      held, but the line stops on their move.
    ``gaps``            int      ``unanswered + missing`` -- the work to do.
    ``rows``            list     the per-reply verdict, in the input order.
    ``summary``         str      one sentence, ready to display.
    ==================  =======  =========================================

    ``side: None`` or no replies yields ``state: "unknown"`` and an honest
    summary -- never ``"covered"``, which would claim a completeness that nobody
    checked.
    """
    result: Dict[str, Any] = {
        "side": side, "state": COVERAGE_UNKNOWN, "total": 0, "prepared": 0,
        "unanswered": 0, "gaps": 0, "rows": [], "summary": "",
    }
    if side not in (SIDE_WHITE, SIDE_BLACK) or not replies:
        result["summary"] = ("No critical replies to check yet." if not replies
                             else "Pick a side to check your repertoire against it.")
        return result

    from backend.db import get_db

    conn = None
    try:
        conn = get_db()
        branches, answers = _repertoire_index(conn, side)
    except Exception as exc:
        logger.warning("Coverage lookup failed: %s", exc)
        result["summary"] = "Could not read your repertoire just now."
        return result

    try:
        rows = []
        for reply in replies:
            parent_fen = str(reply.get("parent_fen") or "")
            uci = str(reply.get("uci") or "")
            fen_after = _fen_after(parent_fen, uci)
            held = uci in branches.get(_fen3(parent_fen), ())
            if not held:
                state = COVERAGE_MISSING
            elif fen_after and _fen3(fen_after) in answers:
                state = COVERAGE_PREPARED
            else:
                # §4.G: holding the reply is not enough. It is now the user's turn
                # there and they have nothing prepared, so the line still stops on
                # the opponent's move -- a gap, never a finished line.
                state = COVERAGE_UNANSWERED
            rows.append({
                "san": reply.get("san", ""),
                "uci": uci,
                "eco": reply.get("eco", ""),
                "label": reply.get("label", ""),
                "family": reply.get("family", ""),
                "ply": reply.get("ply", 0),
                "parent_fen": parent_fen,
                "fen_after": fen_after,
                "state": state,
                "in_repertoire": state in (COVERAGE_PREPARED, COVERAGE_UNANSWERED),
            })

        prepared = sum(1 for r in rows if r["state"] == COVERAGE_PREPARED)
        unanswered = sum(1 for r in rows if r["state"] == COVERAGE_UNANSWERED)
        missing = len(rows) - prepared - unanswered
        gaps = unanswered + missing
        state = (COVERAGE_EMPTY if not rows
                 else COVERAGE_COVERED if not gaps else COVERAGE_GAPS)

        side_label = "White" if side == SIDE_WHITE else "Black"
        if state == COVERAGE_COVERED:
            summary = f"All {len(rows)} critical {side_label} replies are prepared and answered."
        elif state == COVERAGE_EMPTY:
            summary = "No critical replies to check."
        elif not prepared and not unanswered:
            summary = (f"Your repertoire holds none of the {len(rows)} critical "
                       f"{side_label} replies yet — {missing} to prepare.")
        else:
            summary = (f"You have {prepared + unanswered} of {len(rows)} critical "
                       f"{side_label} replies"
                       + (f", but {unanswered} of them stop the line on their move"
                          if unanswered else "")
                       + f" · {gaps} to close.")
        result.update({
            "state": state, "total": len(rows), "prepared": prepared,
            "unanswered": unanswered, "gaps": gaps, "rows": rows, "summary": summary,
        })
        return result
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# The only two functions here that touch the network
# --------------------------------------------------------------------------- #
def _flat_search(query: str, limit: int) -> List[Dict[str, Any]]:
    """One flat YouTube search.  Never raises; an empty list means "no answer".

    ``extract_flat`` is kept on purpose.  It is the difference between a search
    costing ~0.8s and costing a minute, and flat results still carry title,
    channel, duration, view count and -- the useful part -- the short description
    YouTube returns with the search hit.  What they do *not* carry is chapters
    and the upload date, which is exactly what :func:`enrich_videos` goes back
    for.
    """
    if not query:
        return []
    opts = dict(BASE_YDL_OPTS)
    opts.update({"extract_flat": True, "default_search": "ytsearch"})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info("ytsearch%d:%s" % (limit, query), download=False)
    except Exception as exc:
        logger.warning("YouTube search failed for %r: %s", query, exc)
        return []
    entries = (info or {}).get("entries") if isinstance(info, dict) else None
    return [e for e in (entries or []) if isinstance(e, dict)]


def _fetch_video_meta(video_id: str) -> Optional[Dict[str, Any]]:
    """Full metadata for one video: description, chapters, upload date.

    Costs 1-2s and is only ever called for the top of the shelf, in a bounded
    thread pool, and only for videos this process has not seen before.
    """
    if not video_id or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return None
    try:
        with yt_dlp.YoutubeDL(dict(BASE_YDL_OPTS)) as ydl:
            data = ydl.extract_info(
                "https://www.youtube.com/watch?v=%s" % video_id, download=False
            )
    except Exception as exc:
        logger.warning("Metadata fetch failed for %s: %s", video_id, exc)
        return None
    if not isinstance(data, dict):
        return None

    chapters = []
    for ch in (data.get("chapters") or [])[:60]:
        if not isinstance(ch, dict):
            continue
        chapters.append({
            "title": str(ch.get("title") or "")[:MAX_TITLE_CHARS_DISCOVERY],
            "start_time": _as_float(ch.get("start_time"), 0.0),
        })
    try:
        views = int(data.get("view_count") or 0)
    except (TypeError, ValueError):
        views = 0
    try:
        duration = int(data.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    return {
        "video_id": video_id,
        "title": str(data.get("title") or "")[:MAX_TITLE_CHARS_DISCOVERY],
        "channel": str(data.get("uploader") or data.get("channel") or "")[:MAX_TITLE_CHARS_DISCOVERY],
        "duration": max(0, duration),
        "view_count": max(0, views),
        "upload_date": str(data.get("upload_date") or "")[:8],
        "description": str(data.get("description") or "")[:MAX_DESCRIPTION_CHARS_DISCOVERY],
        "chapters": chapters,
    }


def enrich_videos(video_ids: Sequence[str],
                  named_side: Optional[str] = None,
                  named_name: str = "") -> Dict[str, Dict[str, Any]]:
    """Fetch and cache the metadata behind a handful of results.

    Returns ``{video_id: meta}`` for the ids that are not already cached,
    classified with :func:`classify_perspective` against the *named* opening's
    side so "how to beat X" resolves to the side that actually beats it.

    A video that fails to fetch is simply absent from the result: the card keeps
    the metadata the flat search already gave it and the shelf is one item
    poorer, which is a far better failure than a 500.
    """
    ids = [v for v in (video_ids or []) if isinstance(v, str) and v][:MAX_ENRICH_VIDEOS]
    if not ids:
        return {}
    cached = get_video_meta_rows(ids)
    todo = [vid for vid in ids if vid not in cached]
    if todo:
        with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
            fetched = list(pool.map(_fetch_video_meta, todo))
        rows = [f for f in fetched if f]
        for row in rows:
            verdict = classify_perspective(
                row.get("title", ""), row.get("description", ""),
                row.get("chapters", []), named_side, named_name,
            )
            row["side_teach"] = verdict["side"] or ""
            row["mode_teach"] = verdict["mode"] or ""
            row["classify_confidence"] = verdict["confidence"]
            row["classify_evidence"] = verdict["evidence"]
        if rows:
            try:
                save_video_meta_rows(rows)
            except Exception as exc:
                logger.warning("Could not cache video metadata: %s", exc)
        cached = get_video_meta_rows(ids)

    out: Dict[str, Dict[str, Any]] = {}
    for vid in ids:
        row = cached.get(vid)
        if not row:
            continue
        chapters = row.get("chapters") or []
        out[vid] = {
            "video_id": vid,
            "description": row.get("description", "")[:MAX_CARD_DESCRIPTION_CHARS],
            "chapters": [
                {"title": c.get("title", ""), "start_time": c.get("start_time", 0.0)}
                for c in chapters if isinstance(c, dict)
            ][:12],
            "chapter_count": len(chapters),
            "upload_date": row.get("upload_date", ""),
            "view_count": row.get("view_count", 0),
            "duration": row.get("duration", 0),
            "side_teach": row.get("side_teach") or None,
            "mode_teach": row.get("mode_teach") or None,
            "classify_confidence": row.get("classify_confidence", "none"),
            "classify_evidence": row.get("classify_evidence") or "",
        }
    return out


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def discover(query: str,
             side_pref: Optional[str] = None,
             mode_pref: Optional[str] = None,
             chosen_eco: Optional[str] = None,
             limit: int = 12) -> Dict[str, Any]:
    """Answer a Scout query: resolve the intent, then return what actually fits it.

    The single call the results tab makes.  It is deliberately two-phase so the
    shelf appears in under a second:

    * **phase 1** (here) -- resolve the intent, fan out up to
      :data:`MAX_DISCOVER_QUERIES` YouTube queries *in parallel*, merge and
      de-duplicate them, classify each hit from its title and the short
      description the search already returned, score it, and return.  The
      ids still missing chapters/date come back in ``needs_enrichment``.
    * **phase 2** (:func:`enrich_videos`) -- the client asks for those, the
      cards fill in, and nothing reflows because the cards were sized for their
      final content.

    Returns::

        {
          "intent": {...},          # resolve_intent
          "queries": [...],         # what was actually asked
          "results": [...],         # scored, sorted, each with "reasons"
          "groups": {"exact": [...], "related": [...], "course": [...]},
          "needs_enrichment": [...],
          "eco_guides": [...],      # local encyclopedia master lines
        }

    A network failure degrades to "fewer results", never to an error: both
    search legs are individually guarded, so a dead YouTube still returns the
    encyclopedia lines.
    """
    try:
        cap = max(4, min(int(limit or 12), MAX_DISCOVER_RESULTS))
    except (TypeError, ValueError):
        cap = 12

    intent = resolve_intent(query, side_pref, mode_pref, chosen_eco=chosen_eco)

    # An ambiguous name is a *question*, not a request: "Alapin" resolves to
    # eight entries across five families and the top-ranked one is the Ruy
    # Lopez, so fanning out now would search for the wrong opening entirely.
    # Ask instead, and spend no network budget on a guess.  This also makes the
    # first search for an ambiguous name instant.
    if intent.get("needs_disambiguation") and not intent.get("candidate_chosen"):
        return {
            "intent": intent,
            "queries": [],
            "results": [],
            "groups": {"exact": [], "related": [], "course": []},
            "needs_enrichment": [],
            "eco_guides": _eco_guides(intent),
            "replies": [],
            "coverage": repertoire_coverage(None, []),
            "awaiting_choice": True,
        }

    primary = intent.get("primary") or {}

    # The critical replies answer "which continuations must I have ready", and
    # that question only exists in **against** mode.  It cannot be derived from
    # the sides, because the side that *owns* a name is not the side that chose
    # it: the Alapin is filed under "Sicilian Defense" and so classifies as Black,
    # yet White is the one who picks it with 2.c3.  Asking "what must I prepare"
    # while playing it is not merely useless -- walking back from a White Alapin
    # reaches the starting position and returns White's sixteen first moves, i.e.
    # a list of every opening in chess, captioned "the replies you must prepare".
    #
    # So the mode decides, not the side arithmetic.
    replies: List[Dict[str, Any]] = []
    if primary.get("pgn") and intent.get("mode") == MODE_AGAINST:
        try:
            replies = OpeningBook.get_instance().expand_critical_replies(
                primary["pgn"], side=intent.get("side")
            ).get("replies", [])
        except Exception as exc:
            logger.warning("Critical-reply expansion failed: %s", exc)
    elif primary.get("pgn") and primary.get("side") in SIDES:
        # Say why there is no list, rather than showing an empty strip.
        intent.setdefault("reasons", []).append(
            "switch to “play against it” to see the replies you would need to prepare")

    queries = build_queries(intent, replies)
    if not queries:
        queries = [str(intent.get("query") or "")[:MAX_QUERY_CHARS]] if intent.get("query") else []

    merged: Dict[str, Dict[str, Any]] = {}
    ordered_ids: List[str] = []

    def run(q: str) -> List[Dict[str, Any]]:
        # Belt and braces: `_flat_search` already swallows its own errors, but a
        # thread pool re-raises whatever a worker lets escape, and one unhandled
        # query must not take the whole shelf down. A dead leg is a shorter shelf.
        try:
            return _flat_search(q, cap)
        except Exception as exc:
            logger.warning("Discovery query %r failed: %s", q, exc)
            return []

    if queries:
        with ThreadPoolExecutor(max_workers=min(len(queries), MAX_DISCOVER_QUERIES)) as pool:
            for entries in pool.map(run, queries):
                for entry in entries:
                    vid = entry.get("id")
                    if not isinstance(vid, str) or not vid or vid in merged:
                        continue
                    try:
                        views = int(entry.get("view_count") or 0)
                    except (TypeError, ValueError):
                        views = 0
                    try:
                        duration = int(entry.get("duration") or 0)
                    except (TypeError, ValueError):
                        duration = 0
                    cached = get_scouted_content(vid)
                    merged[vid] = {
                        "source_type": "youtube",
                        "id": vid,
                        "url": "https://www.youtube.com/watch?v=%s" % vid,
                        "title": str(entry.get("title") or "")[:MAX_TITLE_CHARS_DISCOVERY]
                                 or ("Chess Guide: %s" % intent.get("query", "")),
                        "channel": str(entry.get("uploader") or entry.get("channel") or "")
                                   or "Chess Creator",
                        "duration": max(0, duration),
                        "duration_formatted": format_seconds(duration),
                        "thumbnail": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid,
                        "view_count": max(0, views),
                        "description": str(entry.get("description") or "")
                                       [:MAX_CARD_DESCRIPTION_CHARS],
                        "is_analyzed": bool(cached),
                        "is_favorite": bool(cached.get("is_favorite")) if cached else False,
                        "eco": str(cached.get("eco") or "") if cached else "",
                        "opening_name": str(cached.get("opening_name") or "") if cached else "",
                        "enriched": False,
                    }
                    ordered_ids.append(vid)
                    if len(ordered_ids) >= MAX_DISCOVER_RESULTS:
                        break
                if len(ordered_ids) >= MAX_DISCOVER_RESULTS:
                    break

    # Reuse anything this process has already paid for before ranking, so a
    # repeat search classifies from the cache instead of from the title alone.
    cached_meta = get_video_meta_rows(ordered_ids[:MAX_ENRICH_VIDEOS])
    for vid, row in cached_meta.items():
        item = merged.get(vid)
        if not item:
            continue
        item["description"] = str(row.get("description") or "")[:MAX_CARD_DESCRIPTION_CHARS] \
            or item["description"]
        chapters = row.get("chapters") or []
        item["chapters"] = [c for c in chapters if isinstance(c, dict)][:12]
        item["chapter_count"] = len(chapters)
        item["upload_date"] = row.get("upload_date", "")
        if row.get("view_count"):
            item["view_count"] = row.get("view_count")
        item["side_teach"] = row.get("side_teach") or None
        item["mode_teach"] = row.get("mode_teach") or None
        item["classify_confidence"] = row.get("classify_confidence", "none")
        item["classify_evidence"] = row.get("classify_evidence") or ""
        item["enriched"] = True

    named_side = primary.get("side")
    named_name = str(primary.get("name") or "")
    for item in merged.values():
        if item.get("enriched"):
            continue
        verdict = classify_perspective(
            item["title"], item.get("description", ""), None, named_side, named_name
        )
        item["side_teach"] = verdict["side"]
        item["mode_teach"] = verdict["mode"]
        item["classify_confidence"] = verdict["confidence"]
        item["classify_evidence"] = verdict["evidence"]
        item.setdefault("chapters", [])
        item.setdefault("chapter_count", 0)
        item.setdefault("upload_date", "")

    results = []
    for item in merged.values():
        verdict = score_result(item, intent)
        item.update({
            "intent_score": verdict["intent_score"],
            "quality_score": verdict["quality_score"],
            "bucket": verdict["bucket"],
            "reasons": verdict["reasons"],
        })
        results.append(item)

    results.sort(key=lambda r: (-r["intent_score"], -r["quality_score"]))
    results = results[:MAX_DISCOVER_RESULTS]

    groups = {"exact": [], "related": [], "course": []}
    for item in results:
        groups.setdefault(item["bucket"], []).append(item)

    eco_guides: List[Dict[str, Any]] = _eco_guides(intent)

    return {
        "intent": intent,
        "queries": queries,
        "results": results,
        "groups": groups,
        "needs_enrichment": [
            item["video_id"] for item in results[:MAX_ENRICH_VIDEOS]
            if not item.get("enriched") and item.get("video_id")
        ],
        "eco_guides": eco_guides,
        "replies": replies,
        "coverage": repertoire_coverage(intent.get("side"), replies),
        "awaiting_choice": False,
    }
