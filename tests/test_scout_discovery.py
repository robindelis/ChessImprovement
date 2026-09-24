"""
tests/test_scout_discovery.py

Tests for the intent-aware Opening Scout: which side you play, whether you want
to *play* an opening or *face* it, and which video actually answers the question.

The three defects this feature exists to fix, each pinned below:

1. the Scout sent one side-blind query (``f"{query} chess opening guide"``) and
   returned YouTube's order, so "learn the Alapin" and "learn to face the
   Alapin" produced the same unlabelled shelf;
2. it had no notion of which side a video teaches, so a Black video teaching
   the Najdorf could be filed as White material;
3. an ambiguous name ("Alapin" resolves to eight encyclopedia entries across
   five families) was silently resolved to the *top* one, which is the Ruy
   Lopez.

Everything here is offline. The two functions that touch the network
(``_flat_search``, ``_fetch_video_meta``) are patched per test, so the suite
never reaches YouTube and never depends on what is published today.
"""
import json
import os
import unittest
from unittest import mock

import tests.test_setup  # noqa: F401  (BRIEF hard rule 2: temp DB + atexit cleanup)

from fastapi.testclient import TestClient

from backend.api import app
from backend.openings import OpeningBook, SIDE_BLACK, SIDE_EITHER, SIDE_WHITE
from backend import scout_discovery as sd
from backend.scout_discovery import (
    build_queries,
    classify_perspective,
    detect_mode,
    discover,
    enrich_videos,
    resolve_intent,
    score_result,
)


# --------------------------------------------------------------------------- #
# OpeningBook.classify_opening_side
# --------------------------------------------------------------------------- #
class TestClassifyOpeningSide(unittest.TestCase):
    """Which side *plays* a named opening."""

    def test_a_name_that_says_defence_is_black(self):
        for eco, name in [
            ("B10", "Caro-Kann Defense"),
            ("B12", "Caro-Kann Defense: Advance Variation"),
            ("E60", "King's Indian Defense"),
            ("D10", "Slav Defense"),
            ("B01", "Scandinavian Defense"),
        ]:
            with self.subTest(name=name):
                self.assertEqual(OpeningBook.classify_opening_side(eco, name)["side"], SIDE_BLACK)

    def test_curated_families_that_do_not_say_defence(self):
        for eco, name, side in [
            ("C60", "Ruy Lopez", SIDE_WHITE),
            ("C50", "Italian Game", SIDE_WHITE),
            ("D30", "Queen's Gambit Declined", SIDE_BLACK),
            ("D32", "Catalan Opening", SIDE_WHITE),
            ("A45", "Trompowsky Attack", SIDE_WHITE),
            ("C30", "King's Gambit Accepted", SIDE_WHITE),   # the gambit is White's
        ]:
            with self.subTest(name=name):
                self.assertEqual(OpeningBook.classify_opening_side(eco, name)["side"], side)

    def test_a_structurally_two_sided_family_beats_the_defence_word(self):
        """"Symmetrical Defense" contains "Defense" and is not a Black opening."""
        result = OpeningBook.classify_opening_side("", "Symmetrical Defense")
        self.assertEqual(result["side"], SIDE_EITHER)
        self.assertIn("two-sided", result["evidence"])

    def test_confidence_is_reported_and_is_honest(self):
        self.assertEqual(OpeningBook.classify_opening_side("B10", "Caro-Kann Defense")["confidence"], "high")
        # No name match, no family: the ECO letter is the only evidence left.
        eco_only = OpeningBook.classify_opening_side("E12", "Some Unlisted Line")
        self.assertEqual(eco_only["confidence"], "low")
        self.assertIn("ECO", eco_only["evidence"])

    def test_never_raises_on_junk(self):
        for eco, name in [("", ""), (None, None), ("ZZ99", "???")]:
            with self.subTest(name=name):
                result = OpeningBook.classify_opening_side(eco, name)
                self.assertIn(result["side"], (SIDE_WHITE, SIDE_BLACK, SIDE_EITHER))


# --------------------------------------------------------------------------- #
# OpeningBook.expand_critical_replies
# --------------------------------------------------------------------------- #
class TestExpandCriticalReplies(unittest.TestCase):
    """The "which replies must I prepare" list, read out of the encyclopedia."""

    @classmethod
    def setUpClass(cls):
        cls.book = OpeningBook.get_instance()

    def _b98(self):
        """The deepest encyclopedia line for the Najdorf (14 plies, to 7...Be7)."""
        entries = [m for m in self.book.openings_list
                   if m["eco"] == "B98" and m["name"] == "Sicilian Defense: Najdorf Variation"]
        self.assertTrue(entries, "the encyclopedia should contain the Najdorf")
        return max(entries, key=lambda m: len(m["pgn"].split()))["pgn"]

    def test_the_najdorf_answers_what_white_must_prepare(self):
        """The headline case: a 14-ply entry whose tail has ONE named reply.

        Reporting "you must prepare 8.Qf3" is true and useless.  The anchor has
        to back off to 5...a6, where the encyclopedia names ten continuations.
        """
        result = self.book.expand_critical_replies(self._b98(), side=SIDE_WHITE)
        self.assertEqual(result["anchor_ply"], 10)
        self.assertGreaterEqual(len(result["replies"]), 8)
        sans = {r["san"] for r in result["replies"]}
        self.assertIn("Bg5", sans)
        self.assertIn("f4", sans)
        self.assertIn("Be3", sans)
        for reply in result["replies"]:
            self.assertEqual(reply["side"], SIDE_WHITE)
            self.assertTrue(reply["name"], "a reply with no name teaches nothing")
            self.assertTrue(reply["label"])

    def test_asking_black_gives_black_s_replies_at_black_s_turn(self):
        result = self.book.expand_critical_replies(self._b98(), side=SIDE_BLACK)
        self.assertTrue(result["replies"])
        for reply in result["replies"]:
            self.assertEqual(reply["side"], SIDE_BLACK)
        # ...and they are a different list, not the same one filtered away.
        white = self.book.expand_critical_replies(self._b98(), side=SIDE_WHITE)
        self.assertNotEqual({r["san"] for r in result["replies"]},
                            {r["san"] for r in white["replies"]})

    def test_the_sicilian_alapin_answers_the_two_replies_that_matter(self):
        result = self.book.expand_critical_replies("e4 c5 c3", side=SIDE_BLACK)
        self.assertEqual({r["san"] for r in result["replies"]}, {"d5", "Nf6"})
        labels = " ".join(r["label"] for r in result["replies"]).lower()
        self.assertIn("anti-alapin", labels)
        self.assertIn("smith-morra", labels)

    def test_a_line_that_leaves_the_book_anchors_at_the_last_book_position(self):
        """1.d4 d5 2.c4 e6 3.Bf4 (the Alapin) has NO entry past ply 4.

        Asking at the end of the line returns nothing; asking at the deepest book
        position -- the Queen's Gambit Declined -- returns its continuations.
        """
        result = self.book.expand_critical_replies("d4 d5 c4 e6 Bf4", side=SIDE_WHITE)
        self.assertEqual(result["book_ply"], 4)
        self.assertEqual(result["anchor"], "Queen's Gambit Declined")
        self.assertTrue(result["replies"])

    def test_a_position_where_the_asked_colour_cannot_move_reports_nothing(self):
        """After 1.d4 d5 2.c4 e6 it is White's move, so asking for Black backs up.

        The walk is looking for a position where the asked colour is to move, and
        ply 3 (after 2.c4) is one -- so the honest answer to "what must Black
        prepare here" is the Queen's Gambit repertoire, not an empty list.
        """
        result = self.book.expand_critical_replies("d4 d5 c4 e6", side=SIDE_BLACK)
        self.assertEqual(result["anchor_ply"], 3)
        self.assertTrue(result["replies"])
        for reply in result["replies"]:
            self.assertEqual(reply["side"], SIDE_BLACK)

    def test_off_book_is_true_only_when_the_encyclopedia_really_has_nothing(self):
        """`off_book` must not be a euphemism for "your filter removed them"."""
        unfiltered = self.book.expand_critical_replies("d4 d5 c4 e6")
        self.assertTrue(unfiltered["replies"])
        self.assertFalse(unfiltered["off_book"], "it IS in the book; the side filter is what emptied it")
        # And it is True when there is nothing, which is the only honest use.
        self.assertTrue(self.book.expand_critical_replies("")["off_book"])
        self.assertTrue(self.book.expand_critical_replies("zzz qqq")["off_book"])

    def test_junk_and_empty_input_return_the_empty_shape(self):
        for moves in ["", None, "zzz qqq", "'; DROP TABLE games; -- e4 e5", 12345]:
            with self.subTest(moves=moves):
                result = self.book.expand_critical_replies(moves)
                self.assertEqual(result["replies"], [])
                self.assertTrue(result["off_book"])
                self.assertEqual(result["book_ply"], -1)
                self.assertEqual(sorted(result),
                                 ["anchor", "anchor_ply", "book_ply", "fen", "off_book", "replies"])

    def test_the_result_is_stable_across_calls(self):
        first = self.book.expand_critical_replies(self._b98(), side=SIDE_WHITE)
        second = self.book.expand_critical_replies(self._b98(), side=SIDE_WHITE)
        self.assertEqual([r["uci"] for r in first["replies"]],
                         [r["uci"] for r in second["replies"]])


# --------------------------------------------------------------------------- #
# OpeningBook.suggest_openings
# --------------------------------------------------------------------------- #
class TestSuggestOpenings(unittest.TestCase):
    """Autocomplete has to be prefix-tolerant; `search_openings` is not."""

    @classmethod
    def setUpClass(cls):
        cls.book = OpeningBook.get_instance()

    def test_a_prefix_matches_where_a_whole_word_would_not(self):
        self.assertEqual(self.book.search_openings("alap", limit=3), [],
                         "the search matcher should reject a mid-word prefix")
        found = self.book.suggest_openings("alap", limit=5)
        self.assertTrue(found)
        self.assertTrue(all("alapi" in m["name"].lower() for m in found))

    def test_it_leads_with_the_shortest_most_specific_name(self):
        found = self.book.suggest_openings("najd", limit=3)
        self.assertTrue(found)
        lengths = [len(m["name"]) for m in found]
        self.assertEqual(lengths, sorted(lengths))

    def test_no_match_and_no_prefix_are_empty(self):
        self.assertEqual(self.book.suggest_openings("zzzzqqq"), [])
        self.assertEqual(self.book.suggest_openings(""), [])


# --------------------------------------------------------------------------- #
# Intent resolution
# --------------------------------------------------------------------------- #
class TestDetectMode(unittest.TestCase):
    def test_against_cues_win_over_play_cues(self):
        for phrase in ["how to beat the Alapin", "counter the Najdorf",
                       "crush the Sicilian", "defending against the Caro-Kann",
                       "Alapin vs Sicilian", "survive 1.e4"]:
            with self.subTest(phrase=phrase):
                self.assertEqual(detect_mode(phrase)[0], "against")

    def test_play_cues(self):
        for phrase in ["how to play the Najdorf", "learn the Italian Game",
                       "Najdorf for black", "Ruy Lopez strategy"]:
            with self.subTest(phrase=phrase):
                self.assertEqual(detect_mode(phrase)[0], "play")

    def test_neither(self):
        self.assertEqual(detect_mode("Sicilian")[0], "either")
        self.assertEqual(detect_mode("")[0], "either")


class TestResolveIntent(unittest.TestCase):
    def test_an_unambiguous_name_resolves_to_one_family(self):
        intent = resolve_intent("Najdorf")
        self.assertFalse(intent["needs_disambiguation"])
        self.assertEqual(intent["side"], SIDE_BLACK)
        self.assertEqual(intent["side_source"], "named_opening")
        self.assertTrue(any("black opening" in r for r in intent["reasons"]))

    def test_an_ambiguous_name_asks_rather_than_guesses(self):
        """The reported bug, verbatim: "Alapin" spans five families."""
        intent = resolve_intent("Alapin")
        self.assertTrue(intent["needs_disambiguation"])
        self.assertGreaterEqual(len(intent["families"]), 4)
        self.assertFalse(intent["candidate_chosen"])

    def test_candidate_chips_are_distinct_and_carry_their_side(self):
        intent = resolve_intent("Alapin")
        labels = [c["label"] for c in intent["candidates"]]
        self.assertEqual(len(labels), len(set(labels)),
                         "six identical chips is not a choice: the encyclopedia "
                         "stores one entry per depth of the same line")
        for cand in intent["candidates"]:
            self.assertIn(cand["side"], (SIDE_WHITE, SIDE_BLACK, SIDE_EITHER))
            self.assertTrue(cand["eco"])
            self.assertIn("Sicilian", " ".join(c["label"] for c in intent["candidates"]))

    def test_an_ambiguous_name_reports_the_majority_side_not_the_top_one(self):
        """The top ECO match for "Alapin" is the Ruy Lopez (White)."""
        intent = resolve_intent("Alapin")
        self.assertEqual(intent["side_source"], "candidate_majority")
        self.assertEqual(intent["side"], SIDE_BLACK)

    def test_choosing_a_chip_pins_the_opening_and_clears_the_question(self):
        intent = resolve_intent("Alapin", chosen_eco="B22")
        self.assertTrue(intent["candidate_chosen"])
        self.assertFalse(intent["needs_disambiguation"])
        self.assertEqual(intent["primary"]["eco"], "B22")
        self.assertEqual(intent["side"], SIDE_BLACK)

    def test_against_inverts_the_named_side(self):
        intent = resolve_intent("Najdorf", mode_pref="against")
        self.assertEqual(intent["mode"], "against")
        self.assertEqual(intent["side"], SIDE_WHITE)
        self.assertTrue(any("you meet it as white" in r for r in intent["reasons"]))

    def test_an_explicit_preference_beats_the_named_opening(self):
        intent = resolve_intent("Najdorf", side_pref="black", mode_pref="play")
        self.assertEqual(intent["side_source"], "preference")
        self.assertEqual(intent["side"], SIDE_BLACK)
        self.assertEqual(intent["mode_source"], "preference")

    def test_text_beats_a_stored_preference_for_the_mode(self):
        intent = resolve_intent("how to beat the Najdorf", mode_pref="play")
        self.assertEqual(intent["mode"], "against")
        self.assertEqual(intent["mode_source"], "text")

    def test_an_unknown_name_is_honestly_unsided(self):
        intent = resolve_intent("zzzz qqqq")
        self.assertIsNone(intent["side"])
        self.assertEqual(intent["side_source"], "none")
        self.assertFalse(intent["in_eco_book"])

    def test_the_shape_is_stable_whatever_was_found(self):
        keys = {"query", "side", "mode", "side_source", "mode_source", "reasons",
                "candidates", "primary", "families", "needs_disambiguation",
                "candidate_chosen", "in_eco_book"}
        for query in ["Najdorf", "Alapin", "zzzz", ""]:
            with self.subTest(query=query):
                self.assertEqual(set(resolve_intent(query)), keys)


# --------------------------------------------------------------------------- #
# Query fan-out
# --------------------------------------------------------------------------- #
class TestBuildQueries(unittest.TestCase):
    def _queries(self, query, side=None, mode=None, chosen=None):
        return build_queries(resolve_intent(query, side, mode, chosen_eco=chosen))

    def test_a_query_never_ends_in_a_dangling_preposition(self):
        """The defect a naive join produces: "how to beat the Alapin as"."""
        for query, side, mode in [("Najdorf", None, None), ("Najdorf", None, "against"),
                                  ("Alapin", "white", "against"), ("zzzz qqq", None, "against")]:
            for built in self._queries(query, side, mode):
                with self.subTest(query=query, built=built):
                    self.assertFalse(
                        built.split()[-1].lower() in {"as", "for", "with", "against", "to", "the"},
                        "query ends in a preposition with nothing after it: %r" % built)

    def test_no_encyclopedia_colon_leaks_into_a_query(self):
        for built in self._queries("Najdorf", "black", "play"):
            self.assertNotIn(":", built)

    def test_a_family_is_not_repeated_within_one_query(self):
        """A template naming both the opening and its family must not say it twice."""
        for query, side, mode in [("Najdorf", "white", "against"), ("Najdorf", "black", "play"),
                                  ("Alapin", "black", "against")]:
            for built in self._queries(query, side, mode, chosen="B98"):
                with self.subTest(built=built):
                    words = [w.lower() for w in built.split()]
                    for size in (2, 3, 4):
                        grams = [tuple(words[i:i + size])
                                 for i in range(len(words) - size + 1)]
                        with self.subTest(gram=grams[0], size=size):
                            self.assertEqual(len(grams), len(set(grams)),
                                             "repeated %d-gram in %r" % (size, built))

    def test_against_mode_leads_with_the_against_phrasing(self):
        queries = self._queries("Najdorf", "white", "against")
        self.assertTrue(any("beat" in q for q in queries), queries)
        self.assertLessEqual(len(queries), sd.MAX_DISCOVER_QUERIES)

    def test_play_mode_leads_with_the_plain_name(self):
        queries = self._queries("Ruy Lopez", "white", "play")
        self.assertEqual(queries[0], "Ruy Lopez")

    def test_the_budget_is_capped_and_entries_are_distinct(self):
        replies = [{"name": f"Reply Number {i}", "family": "Test"} for i in range(50)]
        queries = build_queries(resolve_intent("Najdorf"), replies)
        self.assertLessEqual(len(queries), sd.MAX_DISCOVER_QUERIES)
        self.assertEqual(len(queries), len({q.lower() for q in queries}))

    def test_an_empty_query_builds_nothing(self):
        self.assertEqual(build_queries(resolve_intent("")), [])


# --------------------------------------------------------------------------- #
# Perspective classification
# --------------------------------------------------------------------------- #
class TestClassifyPerspective(unittest.TestCase):
    """Which side does a video TEACH -- the rule the old Scout did not have.

    Every title/description here is a real YouTube result captured while
    building the feature, which is why they are quoted rather than invented.
    """

    BLACK_OPENING = SIDE_BLACK   # the Sicilian / the Najdorf

    def test_the_inversion_trap_crush_the_sicilian_teaches_white(self):
        """The most-viewed result for "Alapin" -- a WHITE video.

        "CRUSH the Sicilian Defense" makes the Sicilian the *opponent's* move,
        so the teacher plays the other side.  Reading "crush" as "teaches Black"
        gets this exactly backwards.
        """
        for title in ["CRUSH the Sicilian Defense: The ALAPIN",
                      "DESTROY the Sicilian Defense in 10 Moves | The Alapin Variation",
                      "Master the Sicilian Defense: Crush the Alapin (2.c3)!",
                      "Crush the Sicilian Najdorf the Easy Way with the Classical Variation"]:
            with self.subTest(title=title):
                verdict = classify_perspective(title, "", None, self.BLACK_OPENING, "Sicilian Defense")
                self.assertEqual(verdict["side"], SIDE_WHITE)
                self.assertEqual(verdict["mode"], "against")

    def test_against_a_move_is_not_against_the_opening(self):
        """The subtlety that decides a Giri video.

        "the opening grandmasters use against 1.e4" names a **move**, so the
        video is about whoever *answers* it -- Black, not White.
        """
        verdict = classify_perspective(
            "HOW TO PLAY Sicilian Defense Najdorf Variation by Grandmaster Anish Giri",
            "World top 10 chess player Anish Giri provides an overview of the "
            "opening favored by so many grandmasters against 1.e4: The Najdorf",
            None, self.BLACK_OPENING, "Sicilian Defense: Najdorf Variation")
        self.assertEqual(verdict["side"], SIDE_BLACK)
        self.assertEqual(verdict["confidence"], "high")

    def test_an_explicit_side_statement_wins_outright(self):
        for title, side in [
            ("GM Sam Shankland Explains how to play the Alapin as Black", SIDE_BLACK),
            ("How to play against the adams attack in the Najdorf Sicilian as White", SIDE_WHITE),
            ("Unleashing Black's Tactical Power | Sicilian Defense: Alapin", SIDE_BLACK),
        ]:
            with self.subTest(title=title):
                verdict = classify_perspective(title, "", None, self.BLACK_OPENING, "Sicilian")
                self.assertEqual(verdict["side"], side)
                self.assertEqual(verdict["confidence"], "high")

    def test_ambiguity_is_reported_rather_than_guessed(self):
        verdict = classify_perspective(
            "The Sicilian Najdorf Is Actually EASY With This Simple Setup",
            "", None, self.BLACK_OPENING, "Sicilian Defense: Najdorf Variation")
        self.assertIsNone(verdict["side"])
        self.assertEqual(verdict["confidence"], "none")

    def test_chapters_can_settle_what_the_title_cannot(self):
        verdict = classify_perspective(
            "The Sicilian Najdorf Is Actually EASY With This Simple Setup", "",
            [{"title": "Part 1: your 6.Bg5 ideas as White against the Najdorf"}],
            self.BLACK_OPENING, "Sicilian Defense: Najdorf Variation")
        self.assertEqual(verdict["side"], SIDE_WHITE)

    def test_it_never_raises(self):
        for args in [(None, None, None, None, None), ("", "", None, "", ""), (123, 456, None, None, None)]:
            with self.subTest(args=args):
                self.assertIn(classify_perspective(*args)["side"], (None, SIDE_WHITE, SIDE_BLACK))


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
class TestScoreResult(unittest.TestCase):
    def _intent(self, side, mode):
        return resolve_intent("Najdorf", side, mode, chosen_eco="B98")

    def test_the_right_side_outranks_the_wrong_side_by_a_lot(self):
        intent = self._intent(SIDE_WHITE, "against")
        mine = score_result({"title": "How to beat the Najdorf as White", "side_teach": SIDE_WHITE,
                             "mode_teach": "against"}, intent)
        theirs = score_result({"title": "Play the Najdorf with Black", "side_teach": SIDE_BLACK,
                               "mode_teach": "play"}, intent)
        self.assertGreater(mine["intent_score"], theirs["intent_score"])

    def test_a_video_for_the_other_side_is_penalised_not_just_unranked(self):
        intent = self._intent(SIDE_BLACK, "play")
        score = score_result({"title": "x", "side_teach": SIDE_WHITE}, intent)
        self.assertLess(score["intent_score"], 0)
        self.assertTrue(any("not black" in r for r in score["reasons"]))

    def test_an_unknown_side_is_neither_rewarded_nor_punished(self):
        intent = self._intent(SIDE_BLACK, "play")
        unknown = score_result({"title": "x", "side_teach": None}, intent)
        wrong = score_result({"title": "x", "side_teach": SIDE_WHITE}, intent)
        self.assertGreater(unknown["intent_score"], wrong["intent_score"])

    def test_reasons_are_returned_so_the_card_can_explain_itself(self):
        intent = self._intent(SIDE_WHITE, "against")
        verdict = score_result({"title": "Crush the Sicilian Najdorf",
                                "side_teach": SIDE_WHITE, "mode_teach": "against",
                                "view_count": 1_000_000, "chapters": [1, 2, 3, 4, 5],
                                "upload_date": "20250101", "is_analyzed": True}, intent)
        self.assertTrue(verdict["reasons"])
        self.assertIn(verdict["bucket"], ("exact", "related", "course"))
        self.assertGreater(verdict["quality_score"], 0)
        self.assertTrue(any("Extracted" in r or "extracted" in r for r in verdict["reasons"]))

    def test_intent_and_quality_are_reported_separately(self):
        intent = self._intent(SIDE_WHITE, "against")
        short = score_result({"title": "Crush the Najdorf as White", "duration": 120,
                              "side_teach": SIDE_WHITE, "mode_teach": "against"}, intent)
        long = score_result({"title": "Crush the Najdorf as White", "duration": 5000,
                             "side_teach": SIDE_WHITE, "mode_teach": "against"}, intent)
        self.assertEqual(short["intent_score"], long["intent_score"])
        self.assertGreaterEqual(long["quality_score"], short["quality_score"])

    def test_it_survives_a_hostile_result_payload(self):
        intent = self._intent(SIDE_WHITE, "against")
        verdict = score_result({"view_count": "not a number", "upload_date": "nope",
                                "duration": None, "reasons": "not a list", "title": None}, intent)
        self.assertIsInstance(verdict["intent_score"], float)
        self.assertIsInstance(verdict["quality_score"], float)


# --------------------------------------------------------------------------- #
# discover() -- the two-phase call
# --------------------------------------------------------------------------- #
def _yt(entries):
    """A `_flat_search` stand-in that answers the same shelf to every query.

    The real search returns a different ranking per query; for the ranking tests
    what matters is the *merged* shelf, so one fixture keeps them readable.
    """
    return lambda query, limit: list(entries)


def _entry(vid, title, desc="", views=1000, duration=600):
    return {"id": vid, "title": title, "description": desc, "view_count": views,
            "duration": duration, "uploader": "Someone"}


class TestDiscover(unittest.TestCase):
    def test_an_ambiguous_name_costs_no_network_at_all(self):
        asked = []

        def spy(query, limit):
            asked.append(query)
            return []

        with mock.patch.object(sd, "_flat_search", side_effect=spy):
            result = discover("Alapin")
        self.assertTrue(result["awaiting_choice"])
        self.assertEqual(result["results"], [])
        self.assertEqual(asked, [], "an ambiguous name must not be searched for")
        self.assertTrue(result["intent"]["candidates"])

    def test_a_chosen_opening_produces_ranked_labelled_results(self):
        shelf = [_entry("black1", "How to beat the Sicilian Alapin as Black"),
                 _entry("black2", "Beat the Alapin Sicilian: FULL REPERTOIRE!"),
                 _entry("white1", "CRUSH the Sicilian Defense: The ALAPIN")]

        with mock.patch.object(sd, "_flat_search", side_effect=_yt(shelf)):
            result = discover("Alapin", side_pref="black", mode_pref="against", chosen_eco="B22")
        self.assertFalse(result["awaiting_choice"])
        by_id = {r["id"]: r for r in result["results"]}
        self.assertEqual(len(by_id), 3, "the three queries must de-duplicate to three results")
        self.assertEqual(by_id["black1"]["side_teach"], SIDE_BLACK)
        self.assertEqual(by_id["white1"]["side_teach"], SIDE_WHITE)
        for row in result["results"]:
            self.assertIn("reasons", row)
            self.assertIn(row["bucket"], ("exact", "related", "course"))
            self.assertIsInstance(row["intent_score"], float)
        # The Black video is the answer to "face it as Black"; the White one is not.
        self.assertGreater(by_id["black1"]["intent_score"], by_id["white1"]["intent_score"])
        self.assertEqual(by_id["black1"]["bucket"], "exact")
        groups = result["groups"]
        self.assertEqual(sum(len(v) for v in groups.values()), len(result["results"]))

    def test_the_same_opening_gives_different_shelves_for_different_intents(self):
        """The whole point: one opening, two opposite questions."""
        both = [_entry("shared", "Sicilian Defense Alapin Variation"),
                _entry("w", "CRUSH the Sicilian Defense: The ALAPIN"),
                _entry("b", "How to play the Alapin as Black")]
        with mock.patch.object(sd, "_flat_search", side_effect=_yt(both)):
            as_white = discover("Alapin", side_pref="white", mode_pref="play", chosen_eco="B22")
            as_black = discover("Alapin", side_pref="black", mode_pref="against", chosen_eco="B22")
        white_sides = {r["id"]: r["side_teach"] for r in as_white["results"]}
        black_sides = {r["id"]: r["side_teach"] for r in as_black["results"]}
        self.assertEqual(white_sides.get("w"), SIDE_WHITE)
        self.assertEqual(black_sides.get("b"), SIDE_BLACK)
        # Classification is a property of the video, not of the question asked,
        # so it must not change between the two shelves.
        self.assertEqual(white_sides, black_sides)
        # The ranking must.
        self.assertNotEqual([r["id"] for r in as_white["results"]],
                            [r["id"] for r in as_black["results"]])

    def test_a_dead_youtube_degrades_to_the_encyclopedia_not_an_error(self):
        with mock.patch.object(sd, "_flat_search", side_effect=RuntimeError("network down")):
            result = discover("Najdorf", chosen_eco="B98")
        self.assertEqual(result["results"], [])
        self.assertFalse(result["awaiting_choice"])
        self.assertTrue(result["eco_guides"], "theory lines must survive a dead search")

    def test_junk_input_never_raises(self):
        with mock.patch.object(sd, "_flat_search", side_effect=_yt([])):
            for query in ["", "   ", "'; DROP TABLE games; --", "x" * 5000]:
                with self.subTest(query=query):
                    result = discover(query)
                    self.assertIn("intent", result)
                    self.assertIn("results", result)

    def test_critical_replies_only_appear_when_you_are_facing_the_opening(self):
        """"Which replies must I prepare" only exists in *against* mode.

        It cannot be derived from the sides: the Alapin is filed under "Sicilian
        Defense" and so classifies as Black, yet White picks it with 2.c3. Walking
        back from a White Alapin reaches the starting position and returns
        White's sixteen first moves, captioned "the replies you must prepare".
        """
        with mock.patch.object(sd, "_flat_search", side_effect=_yt([])):
            facing = discover("Alapin", side_pref="black", mode_pref="against", chosen_eco="B22")
            playing = discover("Alapin", side_pref="white", mode_pref="play", chosen_eco="B22")
        self.assertEqual({r["san"] for r in facing["replies"]}, {"d5", "Nf6"})
        self.assertEqual(playing["replies"], [])
        self.assertTrue(any("play against it" in r for r in playing["intent"]["reasons"]))


# --------------------------------------------------------------------------- #
# enrich_videos() -- the second paint
# --------------------------------------------------------------------------- #
class TestEnrichVideos(unittest.TestCase):
    def setUp(self):
        from backend.db import get_db
        conn = get_db()
        conn.execute("DELETE FROM scout_video_meta")
        conn.commit()
        conn.close()

    def test_metadata_is_fetched_once_and_then_served_from_the_cache(self):
        calls = []

        def fake_fetch(video_id):
            calls.append(video_id)
            return {"video_id": video_id, "title": "T " + video_id, "channel": "C",
                    "duration": 900, "view_count": 4242, "upload_date": "20240102",
                    "description": "A description", "chapters": [{"title": "Intro", "start_time": 0.0}]}

        with mock.patch.object(sd, "_fetch_video_meta", side_effect=fake_fetch):
            first = enrich_videos(["abcdefghijk"], SIDE_BLACK, "Sicilian Defense")
            self.assertEqual(len(calls), 1)
            second = enrich_videos(["abcdefghijk"], SIDE_BLACK, "Sicilian Defense")
            self.assertEqual(len(calls), 1, "the second call must be served from the cache")
        self.assertEqual(first, second)
        self.assertEqual(first["abcdefghijk"]["upload_date"], "20240102")
        self.assertEqual(first["abcdefghijk"]["chapter_count"], 1)

    def test_a_video_that_fails_to_fetch_is_absent_not_fatal(self):
        with mock.patch.object(sd, "_fetch_video_meta", return_value=None):
            meta = enrich_videos(["abcdefghijk"], SIDE_BLACK, "Sicilian")
        self.assertEqual(meta, {})

    def test_it_is_bounded(self):
        with mock.patch.object(sd, "_fetch_video_meta", side_effect=lambda v: None) as spy:
            enrich_videos(["a" * 11] * 100, None, "")
        self.assertLessEqual(spy.call_count, sd.MAX_ENRICH_VIDEOS)

    def test_a_malformed_id_never_reaches_the_extractor(self):
        """`_fetch_video_meta` is the gate; prove it refuses before any socket."""
        with mock.patch.object(sd.yt_dlp, "YoutubeDL") as ydl:
            for bad in ["../etc/passwd", "'; DROP TABLE games; --", "", "x", "a" * 200, "abc def"]:
                with self.subTest(bad=bad):
                    self.assertIsNone(sd._fetch_video_meta(bad))
            ydl.assert_not_called()


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #
class TestScoutDiscoveryApi(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app, base_url="http://testserver", raise_server_exceptions=False)

    def test_the_legacy_search_route_is_untouched(self):
        """It is kept byte-identical: a side-blind, side-blind-shaped contract."""
        with mock.patch("backend.scout.yt_dlp.YoutubeDL.extract_info", return_value={"entries": []}), \
             mock.patch.object(sd.OpeningBook.get_instance(), "search_openings", return_value=[]):
            res = self.client.get("/api/scout/search?query=Italian+Game&limit=4")
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.json(), list)

    def test_discover_rejects_a_nonsense_side_or_mode(self):
        self.assertEqual(self.client.get("/api/scout/discover", params={"query": "x", "side": "purple"}).status_code, 422)
        self.assertEqual(self.client.get("/api/scout/discover", params={"query": "x", "mode": "nope"}).status_code, 422)

    def test_discover_has_one_shape(self):
        with mock.patch.object(sd, "_flat_search", return_value=[]):
            res = self.client.get("/api/scout/discover", params={"query": "zzzz", "limit": 8})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        for key in ("intent", "queries", "results", "groups", "needs_enrichment",
                    "eco_guides", "replies", "awaiting_choice"):
            self.assertIn(key, body)
        self.assertIn("needs_disambiguation", body["intent"])

    def test_suggest_is_offline_and_side_aware(self):
        res = self.client.get("/api/scout/suggest", params={"q": "alap"})
        self.assertEqual(res.status_code, 200)
        rows = res.json()
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("side", row)
            self.assertTrue(row["eco"])
        self.assertEqual(self.client.get("/api/scout/suggest", params={"q": ""}).json(), [])

    def test_enrich_is_bounded_and_fails_soft(self):
        with mock.patch.object(sd, "_fetch_video_meta", return_value={"id": "a" * 11, "title": "T", "duration": 100}):
            res = self.client.post("/api/scout/enrich", json={"ids": ["a" * 11]})
        self.assertEqual(res.status_code, 200)
        self.assertIn("meta", res.json())
        self.assertEqual(self.client.post("/api/scout/enrich", json={"ids": []}).status_code, 422)
        self.assertEqual(
            self.client.post("/api/scout/enrich", json={"ids": ["x" * 200]}).status_code, 422)


# --------------------------------------------------------------------------- #
# The real, shipped frontend helpers, executed under node
# --------------------------------------------------------------------------- #
_SCENARIO = r"""
const out = {};
const eq = (name, actual, expected) => {
    out[name] = { actual: JSON.stringify(actual), expected: JSON.stringify(expected),
                  pass: JSON.stringify(actual) === JSON.stringify(expected) };
};

// --- length classes -------------------------------------------------------
eq('quick', scoutLengthInfo(180).key, 'quick');
eq('single line', scoutLengthInfo(1800).key, 'single');
eq('short course', scoutLengthInfo(3000).key, 'course');
eq('full course', scoutLengthInfo(99999).key, 'long');
eq('no duration is theory', scoutLengthInfo(0).key, 'theory');
eq('junk duration is theory', scoutLengthInfo('abc').key, 'theory');

// --- views ----------------------------------------------------------------
eq('millions', scoutFormatViews(1500000), '1.5M views');
eq('thousands', scoutFormatViews(21400), '21K views');
eq('small', scoutFormatViews(842), '842 views');
eq('zero views is blank', scoutFormatViews(0), '');

// --- upload dates ---------------------------------------------------------
const now = Date.UTC(2026, 0, 15);
eq('today', scoutFormatUploadDate('20260115', now), 'today');
eq('yesterday', scoutFormatUploadDate('20260114', now), 'yesterday');
eq('days', scoutFormatUploadDate('20260105', now), '10 days ago');
eq('months', scoutFormatUploadDate('20250915', now), '4 months ago');
eq('years', scoutFormatUploadDate('20240115', now), '2 years ago');
eq('a corrupt date is blank, never NaN', scoutFormatUploadDate('garbage', now), '');
eq('a nonsense month is blank', scoutFormatUploadDate('20261315', now), '');
eq('a missing date is blank', scoutFormatUploadDate(undefined, now), '');
eq('a future date is blank', scoutFormatUploadDate('20300101', now), '');

// --- the perspective badge ------------------------------------------------
const whiteCrush = { side_teach: 'white', mode_teach: 'against', classify_confidence: 'high',
                     classify_evidence: 'Najdorf: "Crush" means facing the black' };
const blackPlays = { side_teach: 'black', mode_teach: 'play', classify_confidence: 'high',
                     classify_evidence: 'as Black' };
const unknown = { side_teach: null, mode_teach: null, classify_confidence: 'none', classify_evidence: '' };
eq('white meeting it', scoutPerspectiveInfo(whiteCrush).text, '🛡 White · meets this');
eq('black playing it', scoutPerspectiveInfo(blackPlays).text, '▶ Black');
eq('unknown is a first-class answer', scoutPerspectiveInfo(unknown).text, '? side unclear');
eq('unknown carries a tooltip', scoutPerspectiveInfo(unknown).title.length > 0, true);
eq('a corrupt side is not a colour', scoutPerspectiveInfo({ side_teach: 'purple' }).text, '? side unclear');
eq('the tooltip quotes the evidence',
   scoutPerspectiveInfo(whiteCrush).title.indexOf('Crush') !== -1, true);

// --- reason chips ---------------------------------------------------------
eq('reasons become chips', scoutReasonChips({ reasons: ['teaches White', 'names Sicilian'] })
     .split('<span').length - 1, 2);
eq('a hostile reason is escaped, not executed',
   scoutReasonChips({ reasons: ['<img src=x onerror=alert(1)>'] }).indexOf('onerror='), -1);
eq('no reasons means no chips', scoutReasonChips({ reasons: [] }), '');
eq('a non-list is not a crash', scoutReasonChips({ reasons: 'nope' }), '');

// --- intent summary -------------------------------------------------------
const intent = { side: 'black', mode: 'against', needs_disambiguation: false };
eq('summary states the question',
   scoutIntentSummary(intent, { count: 12, replies: 2 }),
   'Showing as Black · play against it · 12 results · 2 critical replies');
eq('one reply is singular',
   scoutIntentSummary(intent, { count: 1, replies: 1 }),
   'Showing as Black · play against it · 1 results · 1 critical reply');
eq('gaps are reported when known',
   scoutIntentSummary(intent, { count: 3, replies: 4, gaps: 2 }).indexOf('2 gaps') !== -1, true);
eq('one gap is singular',
   scoutIntentSummary(intent, { count: 3, replies: 4, gaps: 1 }).indexOf('1 gap in your repertoire') !== -1, true);
eq('an empty intent still renders something',
   typeof scoutIntentSummary(null, {}), 'string');
eq('an empty intent is not empty', scoutIntentSummary(null, {}).length > 0, true);
eq('disambiguation asks the question',
   scoutIntentSummary({ side: null, mode: 'either', needs_disambiguation: true }, { count: 0 })
     .indexOf('pick an opening') !== -1, true);

// --- sorting --------------------------------------------------------------
const rows = [
  { id: 'a', intent_score: 10, quality_score: 1, view_count: 50, duration: 900, upload_date: '20200101', is_analyzed: false },
  { id: 'b', intent_score: 90, quality_score: 1, view_count: 10, duration: 100, upload_date: '20240101', is_analyzed: false },
  { id: 'c', intent_score: 50, quality_score: 9, view_count: 900, duration: 5000, upload_date: '20260101', is_analyzed: true }
];
eq('match leads with the best answer', scoutSortResults(rows, 'match').map(r => r.id)[0], 'b');
eq('shortest first', scoutSortResults(rows, 'shortest').map(r => r.id)[0], 'b');
eq('newest first', scoutSortResults(rows, 'newest').map(r => r.id)[0], 'c');
eq('extracted first', scoutSortResults(rows, 'extracted').map(r => r.id)[0], 'c');
eq('sorting does not mutate its input', rows.map(r => r.id).join(''), 'abc');
eq('an unknown sort falls back to match', scoutSortResults(rows, 'nope').map(r => r.id)[0], 'b');
eq('no items is no crash', scoutSortResults(null, 'match').length, 0);

// --- the card -------------------------------------------------------------
const card = scoutResultCardMarkup({
  id: 'vid1', source_type: 'youtube', title: 'How to beat the Najdorf as White',
  channel: 'Some Channel', duration: 620, duration_formatted: '10:20', view_count: 1500000,
  upload_date: '20240102', is_analyzed: true, is_favorite: true, side_teach: 'white',
  mode_teach: 'against', classify_confidence: 'high', classify_evidence: 'as White',
  description: 'A useful description', chapters: [{ title: 'Intro' }, { title: 'Main' }],
  reasons: ['teaches White, your side'], eco: 'B98'
});
eq('the card keeps its click target', /data-source-id="vid1"/.test(card), true);
eq('the card shows the perspective', card.indexOf('meets this') !== -1, true);
eq('the card shows it is extracted', card.indexOf('✓ Extracted') !== -1, true);
eq('the card shows the bookmark state', card.indexOf('★') !== -1, true);
eq('the card shows the chapter count', card.indexOf('2 chapters') !== -1, true);
eq('the card shows the views', card.indexOf('1.5M views') !== -1, true);
eq('the card shows the length class', card.indexOf('scout-length-tag--quick') !== -1, true);

const sparse = scoutResultCardMarkup({ id: 'x', source_type: 'youtube', title: 'Bare' });
eq('a bare card still reserves its slots',
   ['scout-card-desc', 'scout-card-why', 'scout-card-foot']
     .every(c => sparse.indexOf(c) !== -1), true);
eq('a bare card does not say NaN', /NaN|undefined/.test(sparse), false);
eq('a hostile title is escaped',
   scoutResultCardMarkup({ id: 'x', title: '<img src=x onerror=alert(1)>' }).indexOf('onerror=alert'), -1);

// --- disambiguation chips -------------------------------------------------
const chip = scoutCandidateChipMarkup({ eco: 'B22', name: 'Sicilian Defense: Alapin Variation',
  label: 'Sicilian Defense › Alapin Variation', side: 'black', opening_moves: ['e4', 'c5', 'c3'] });
eq('the chip carries the eco the API pins on', chip.indexOf('data-candidate-eco="B22"') !== -1, true);
eq('the chip shows the side', chip.indexOf('⚫') !== -1, true);
eq('the chip shows the opening', chip.indexOf('Alapin') !== -1, true);
eq('a hostile chip name is escaped',
   scoutCandidateChipMarkup({ eco: 'B22', name: '<b>x</b>' }).indexOf('<b>') , -1);
eq('a side-less chip is a question mark',
   scoutCandidateChipMarkup({ eco: 'B22', name: 'x', side: 'either' }).indexOf('?') !== -1, true);

// --- coverage states -----------------------------------------------------
eq('prepared', scoutCoverageState({ state: 'prepared' }).mark, '✓');
eq('unanswered is named', scoutCoverageState({ state: 'unanswered_branch' }).label, 'no answer');
eq('missing', scoutCoverageState({ state: 'missing_branch' }).label, 'not prepared');
// One glyph per meaning: `⚡` belongs to §4.E's overlap badge, and reusing it
// here would give a single symbol two unrelated readings (and trips CODE-19).
eq('the coverage marks do not collide with the §4.E overlap badge',
   [scoutCoverageState({state:'prepared'}).mark,
    scoutCoverageState({state:'unanswered_branch'}).mark,
    scoutCoverageState({state:'missing_branch'}).mark].indexOf('⚡'), -1);
eq('an unknown state falls back to missing, never to prepared',
   scoutCoverageState({ state: 'purple' }).label, 'not prepared');
eq('a row with no state at all is a gap', scoutCoverageState({}).label, 'not prepared');
eq('the classes are the shared vocabulary',
   [scoutCoverageState({state:'prepared'}).cls, scoutCoverageState({state:'unanswered_branch'}).cls,
    scoutCoverageState({state:'missing_branch'}).cls],
   ['is-prepared', 'is-unanswered', 'is-missing']);
eq('the unanswered tooltip names the actual problem',
   scoutCoverageTitle({ san: 'Nf6', state: 'unanswered_branch', label: 'Smith-Morra' })
     .indexOf('stop on their move') !== -1, true);
eq('the missing tooltip says it is a gap',
   scoutCoverageTitle({ san: 'd5', state: 'missing_branch' }).indexOf('gap you have to close') !== -1, true);
eq('the prepared tooltip is reassuring',
   scoutCoverageTitle({ san: 'd5', state: 'prepared' }).indexOf('prepared answer') !== -1, true);

const covRow = scoutCoverageRowMarkup({ san: 'd5', eco: 'B22', label: 'Anti-Alapin', uci: 'd7d5',
  parent_fen: 'x y', state: 'missing_branch' }, 'black');
eq('a gap row carries the action', covRow.indexOf('data-coverage-uci="d7d5"') !== -1, true);
eq('a hostile label is escaped',
   scoutCoverageRowMarkup({ san: 'd5', eco: 'x', label: '<b>y</b>', state: 'missing' }, 'black')
     .indexOf('<b>'), -1);
eq('a prepared row offers no pointless action',
   scoutCoverageRowMarkup({ san: 'd5', state: 'prepared' }, 'black').indexOf('scout-coverage-action'), -1);

// --- the plan panel ------------------------------------------------------
const ideas = { matched: true, family: 'Sicilian Defense', side: 'white',
  white: { core_idea: 'W idea', typical_mistakes: 'W traps', key_plans: 'W plans' },
  black: { core_idea: 'B idea', typical_mistakes: 'B traps', key_plans: 'B plans' } };
const whitePanel = scoutPlanPanelMarkup(ideas, 'white', 'What White is doing');
const blackPanel = scoutPlanPanelMarkup(ideas, 'black', 'What Black is doing');
eq('the panel shows the requested side', whitePanel.indexOf('W idea') !== -1, true);
eq('the panel does not show the other side', whitePanel.indexOf('B idea'), -1);
eq('switching the side switches the text', blackPanel.indexOf('B idea') !== -1, true);
eq('the panel is a <details>, so it costs nothing collapsed',
   whitePanel.indexOf('<details') !== -1, true);
eq('the rows are keyed by data-field, not by per-field ids',
   whitePanel.indexOf('data-field="core_idea"') !== -1, true);
// A `.map(...).join('')` of already-escaped markup spliced into an outer
// safeHtml is escaped a SECOND time, and the panel renders as visible text.
// Nothing else in the suite catches that, so it is asserted directly here.
eq('the rows are real markup, not escaped text',
   whitePanel.indexOf('&lt;div') , -1);
eq('the same for the coverage rows and the coverage strip',
   scoutCoverageRowMarkup({ san: 'd5', state: 'missing' }, 'black').indexOf('&lt;'), -1);
eq('a matched family shows no disclaimer', whitePanel.indexOf('scout-plan-note'), -1);
eq('an unmatched family says so honestly',
   scoutPlanPanelMarkup({ matched: false, white: ideas.white }, 'white', 'x').indexOf('No notes on this opening') !== -1, true);
eq('no ideas renders nothing at all', scoutPlanPanelMarkup(null, 'white', 'x'), '');
eq('ideas with no side block render nothing', scoutPlanPanelMarkup({ matched: true }, 'white', 'x'), '');
eq('a hostile plan is escaped',
   scoutPlanPanelMarkup({ matched: true, white: { core_idea: '<img src=x onerror=alert(1)>' } }, 'white', 'x')
     .indexOf('onerror=alert'), -1);
eq('a blank plan field is dropped rather than rendered empty',
   scoutPlanPanelMarkup({ matched: true, white: { core_idea: 'idea', typical_mistakes: '   ', key_plans: '' } },
     'white', 'x').indexOf('data-field="typical_mistakes"'), -1);

return out;
"""


class TestShippedScoutHelpersUnderNode(unittest.TestCase):
    """Executes the REAL `frontend/scout.js` helpers -- skipped without node."""

    @classmethod
    def setUpClass(cls):
        from tests import frontend_runtime
        if not frontend_runtime.node_available():
            raise unittest.SkipTest("node binary not available")
        missing = frontend_runtime.missing_frontend_sources()
        if missing:
            raise AssertionError("frontend sources are missing/empty: %s" % missing)
        cls.result = frontend_runtime.run_scenario(_SCENARIO)

    def test_the_real_shipped_scout_js_was_executed(self):
        loaded = [s["filename"] for s in (self.result.get("__harness") or {}).get("loadedSources", [])]
        self.assertIn("frontend/scout.js", loaded)

    def test_every_helper_behaves(self):
        checks = {k: v for k, v in self.result.items() if k != "__harness"}
        self.assertGreaterEqual(len(checks), 45, "the scenario lost checks: %s" % sorted(checks)[:10])
        failed = {k: v for k, v in checks.items() if not v.get("pass")}
        self.assertEqual(failed, {},
                         "shipped scout.js helper regressions:\n%s" % json.dumps(failed, indent=2)[:3000])


# --------------------------------------------------------------------------- #
# Phase 2: coverage against the user's own repertoire
# --------------------------------------------------------------------------- #
def _fen4(fen):
    return " ".join(str(fen).split()[:4])


def _play(*ucis):
    import chess
    board = chess.Board()
    for uci in ucis:
        board.push_uci(uci)
    return board


def _add_moves(repertoire, rows):
    """Insert repertoire rows. `rows` is `(parent_fen, san, fen_after, uci, is_user)`.

    The two kinds are separate columns of the same table and they mean different
    things (§4.G): an ``is_user_move = 0`` row is a branch the repertoire HOLDS
    (the opponent plays it), an ``is_user_move = 1`` row is a move the USER can
    play. Tests that conflate them assert the wrong thing.
    """
    from backend.db import get_db
    conn = get_db()
    for parent, san, after, uci, is_user in rows:
        conn.execute(
            "INSERT OR REPLACE INTO repertoire_moves "
            "(repertoire,parent_fen,fen,move_uci,move_san,is_user_move) "
            "VALUES (?,?,?,?,?,?)", (repertoire, parent, after, uci, san, 1 if is_user else 0))
    conn.commit()
    conn.close()


def _alapin(side="black", mode="against", chosen_eco="B22"):
    """`discover()` for the Sicilian Alapin, with the network stubbed out."""
    with mock.patch.object(sd, "_flat_search", side_effect=_yt([])):
        return discover("Alapin", side_pref=side, mode_pref=mode, chosen_eco=chosen_eco)


def _first_legal(board, exclude=()):
    """Any legal move -- so a test never hard-codes a square that is pinned."""
    for move in board.legal_moves:
        if board.san(move) not in exclude:
            return move
    raise AssertionError("no legal move")


class TestRepertoireCoverage(unittest.TestCase):
    """"Which of these do I actually lack?" -- the question a shelf cannot answer."""

    @classmethod
    def tearDownClass(cls):
        tests.test_setup.reset_fixtures()

    def setUp(self):
        from backend.db import get_db
        conn = get_db()
        conn.execute("DELETE FROM repertoire_moves")
        conn.commit()
        conn.close()

    def test_the_three_states_use_the_builders_own_vocabulary(self):
        """The Scout and the Builder must not describe one position differently."""
        from backend.repertoire import classify_line_position
        self.assertEqual(sd.COVERAGE_MISSING, "missing_branch")
        self.assertEqual(sd.COVERAGE_UNANSWERED, "unanswered_branch")
        # "Held but unanswerable" is exactly the §4.G case: the opponent moved and
        # there is no prepared reply, which is a gap and never a finished line.
        self.assertEqual(classify_line_position(False, False), "awaiting_user_reply")
        self.assertEqual(classify_line_position(False, True), "continues")
        self.assertEqual(classify_line_position(True, False), "completed_line")

    def test_an_empty_repertoire_reports_every_reply_as_a_gap(self):
        cov = _alapin()["coverage"]
        self.assertEqual(cov["state"], "gaps")
        self.assertEqual(cov["total"], 2)
        self.assertEqual(cov["prepared"], 0)
        self.assertEqual(cov["gaps"], 2)
        for row in cov["rows"]:
            self.assertEqual(row["state"], "missing_branch")
            self.assertFalse(row["in_repertoire"])

    def test_holding_a_reply_is_not_enough_you_must_be_able_to_answer_it(self):
        """§4.G: a branch you hold but cannot answer still stops on their move."""
        anchor = _fen4(_play("e2e4", "c7c5", "c2c3").fen())
        after_d5 = _fen4(_play("e2e4", "c7c5", "c2c3", "d7d5").fen())
        after_nf6 = _fen4(_play("e2e4", "c7c5", "c2c3", "g8f6").fen())
        after_d4 = _fen4(_play("e2e4", "c7c5", "c2c3", "d7d5", "d2d4").fen())
        # Both branches held; only 3...d5 has a prepared answer behind it.
        _add_moves("black", [
            (anchor, "d5", after_d5, "d7d5", False),      # branch held
            (anchor, "Nf6", after_nf6, "g8f6", False),    # branch held
            (after_d5, "d4", after_d4, "d2d4", True),     # and answered
        ])
        cov = _alapin()["coverage"]
        by_san = {r["san"]: r for r in cov["rows"]}
        self.assertEqual(by_san["d5"]["state"], "prepared")
        self.assertEqual(by_san["Nf6"]["state"], "unanswered_branch")
        self.assertTrue(by_san["Nf6"]["in_repertoire"],
                        "the branch IS held; what is missing is the answer after it")
        # An unanswered branch is a gap of the same kind as a missing one.
        self.assertEqual(cov["gaps"], 1)
        self.assertEqual(cov["prepared"], 1)
        self.assertEqual(cov["unanswered"], 1)
        self.assertIn("stop the line on their move", cov["summary"])

    def test_a_fully_covered_repertoire_says_so(self):
        anchor = _fen4(_play("e2e4", "c7c5", "c2c3").fen())
        after_d5 = _fen4(_play("e2e4", "c7c5", "c2c3", "d7d5").fen())
        after_d4 = _fen4(_play("e2e4", "c7c5", "c2c3", "d7d5", "d2d4").fen())
        after_nf6 = _fen4(_play("e2e4", "c7c5", "c2c3", "g8f6").fen())
        board = _play("e2e4", "c7c5", "c2c3", "g8f6")
        answer = _first_legal(board)
        answer_san = board.san(answer)
        board.push(answer)
        _add_moves("black", [
            (anchor, "d5", after_d5, "d7d5", False),
            (anchor, "Nf6", after_nf6, "g8f6", False),
            (after_d5, "d4", after_d4, "d2d4", True),
            (after_nf6, answer_san, _fen4(board.fen()), answer.uci(), True),
        ])
        cov = _alapin()["coverage"]
        self.assertEqual(cov["state"], "covered")
        self.assertEqual(cov["gaps"], 0)
        self.assertIn("prepared and answered", cov["summary"])

    def test_it_never_claims_cover_it_did_not_check(self):
        for side, replies in [(None, [{"san": "d5"}]), ("white", []), ("purple", [{"san": "d5"}])]:
            with self.subTest(side=side):
                cov = sd.repertoire_coverage(side, replies)
                self.assertEqual(cov["state"], "unknown")
                self.assertEqual(cov["rows"], [])
                self.assertNotIn("covered", cov["summary"])
                self.assertTrue(cov["summary"])

    def test_the_shape_is_stable(self):
        for side, replies in [(None, []), ("black", []),
                              ("black", [{"san": "d5", "parent_fen": "", "uci": ""}])]:
            with self.subTest(side=side):
                self.assertEqual(sorted(sd.repertoire_coverage(side, replies)),
                                 ["gaps", "prepared", "rows", "side", "state",
                                  "summary", "total", "unanswered"])

    def test_a_user_move_is_not_a_branch_the_repertoire_holds(self):
        """The two row kinds are different; reading the wrong index is a real bug.

        A user row at the anchor means the *user* would play a move there -- but
        after 2.c3 it is Black's turn, so it is not a branch held against the
        Alapin. It must not make the reply look prepared.
        """
        anchor = _fen4(_play("e2e4", "c7c5", "c2c3").fen())
        after_d5 = _fen4(_play("e2e4", "c7c5", "c2c3", "d7d5").fen())
        _add_moves("black", [(anchor, "d5", after_d5, "d7d5", True)])
        cov = _alapin()["coverage"]
        by_san = {r["san"]: r for r in cov["rows"]}
        self.assertEqual(by_san["d5"]["state"], "missing_branch")
        self.assertFalse(by_san["d5"]["in_repertoire"])

    def test_an_opponent_row_is_not_an_answer(self):
        """`is_user_move = 0` after the branch must not make a dead end look covered."""
        anchor = _fen4(_play("e2e4", "c7c5", "c2c3").fen())
        after_d5 = _fen4(_play("e2e4", "c7c5", "c2c3", "d7d5").fen())
        _add_moves("black", [
            (anchor, "d5", after_d5, "d7d5", False),          # branch held
            (after_d5, "e4", after_d5, "e2e4", False),        # an opponent row, not an answer
        ])
        cov = _alapin()["coverage"]
        by_san = {r["san"]: r for r in cov["rows"]}
        self.assertEqual(by_san["d5"]["state"], "unanswered_branch")

    def test_it_survives_a_position_with_a_legal_en_passant_square(self):
        """One logical position, two 4-token spellings -- the verdict must not flip."""
        anchor = _fen4(_play("e2e4", "c7c5", "c2c3").fen())
        after_d5 = _fen4(_play("e2e4", "c7c5", "c2c3", "d7d5").fen())
        after_d4 = _fen4(_play("e2e4", "c7c5", "c2c3", "d7d5", "d2d4").fen())
        # Store the answer under the OTHER spelling of the same position: the
        # en-passant token differs, the 3-token form does not.
        twin = " ".join(after_d5.split()[:3] + ["-"])
        _add_moves("black", [
            (anchor, "d5", after_d5, "d7d5", False),
            (twin, "d4", after_d4, "d2d4", True),
        ])
        cov = _alapin()["coverage"]
        by_san = {r["san"]: r for r in cov["rows"]}
        self.assertEqual(by_san["d5"]["state"], "prepared")

    def test_the_front_end_receives_the_coverage_with_the_results(self):
        self.assertEqual(_alapin()["coverage"]["state"], "gaps")
        with mock.patch.object(sd, "_flat_search", side_effect=_yt([])):
            pending = discover("Alapin")
        self.assertIn("coverage", pending)
        self.assertEqual(pending["coverage"]["state"], "unknown")

    def test_playing_an_opening_reports_no_coverage_to_check(self):
        cov = _alapin("white", "play")["coverage"]
        self.assertEqual(cov["state"], "unknown")
        self.assertEqual(cov["rows"], [])


# --------------------------------------------------------------------------- #
# Phase 2: side-aware strategic background
# --------------------------------------------------------------------------- #
class TestStrategicIdeasAreSideAware(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = OpeningBook.get_instance()

    def test_the_legacy_contract_is_untouched(self):
        """`GET /api/blunders/{id}`'s `opening_theme` and the study payloads rely on it."""
        ideas = self.book.get_strategic_ideas("B20", "Sicilian Defense")
        for key in ("name", "core_idea", "typical_mistakes", "key_plans"):
            self.assertIn(key, ideas)
        self.assertIn("c-pawn", ideas["core_idea"])
        self.assertEqual(ideas["core_idea"], self.book.opening_themes["Sicilian"]["core_idea"])

    def test_both_sides_are_available_and_different(self):
        ideas = self.book.get_strategic_ideas("B22", "Sicilian Defense: Alapin Variation")
        self.assertTrue(ideas["matched"])
        self.assertEqual(ideas["family"], "Sicilian Defense")
        for side in ("white", "black"):
            for key in ("core_idea", "typical_mistakes", "key_plans"):
                self.assertTrue(ideas[side][key].strip(), "%s.%s is empty" % (side, key))
        self.assertNotEqual(ideas["white"]["core_idea"], ideas["black"]["core_idea"])

    def test_facing_the_opening_reads_the_opponents_plans(self):
        """`side` selects the block; the two blocks are always both present.

        The side argument is a *selector*, not a filter: a caller asking about
        Black still receives White's notes, because the Scout's "against" mode
        needs the opponent's plans and must not have to ask twice.
        """
        as_black = self.book.get_strategic_ideas("B22", "Sicilian Defense: Alapin Variation", "black")
        as_white = self.book.get_strategic_ideas("B22", "Sicilian Defense: Alapin Variation", "white")
        self.assertEqual(as_black["side"], "black")
        self.assertEqual(as_white["side"], "white")
        self.assertNotEqual(as_black["white"]["core_idea"], as_black["black"]["core_idea"])
        # ...and the blocks do not move when the selector does.
        self.assertEqual(as_black["white"]["core_idea"], as_white["white"]["core_idea"])
        self.assertEqual(as_black["black"]["core_idea"], as_white["black"]["core_idea"])

    def test_an_unknown_family_is_reported_not_dressed_up(self):
        ideas = self.book.get_strategic_ideas("A99", "Zzz Nonexistent Opening", "white")
        self.assertFalse(ideas["matched"])
        self.assertTrue(ideas["core_idea"])
        self.assertTrue(ideas["white"]["key_plans"])

    def test_the_table_is_never_mutated_by_a_caller(self):
        a = self.book.get_strategic_ideas("B20", "Sicilian Defense", "white")
        a["core_idea"] = "MUTATED"
        a["white"]["key_plans"] = "MUTATED"
        a["name"] = "MUTATED"
        b = self.book.get_strategic_ideas("B20", "Sicilian Defense")
        self.assertNotEqual(b["core_idea"], "MUTATED")
        self.assertNotEqual(b["white"]["key_plans"], "MUTATED")
        self.assertNotEqual(b["name"], "MUTATED")
        self.assertIn("c-pawn", self.book.opening_themes["Sicilian"]["core_idea"])

    def test_every_family_has_the_same_three_fields_on_both_sides(self):
        from backend.opening_strategies import GENERIC_BLACK, GENERIC_WHITE, SIDE_THEMES
        want = {"core_idea", "typical_mistakes", "key_plans"}
        self.assertGreaterEqual(len(SIDE_THEMES), 30, "the table was supposed to be widened")
        for family, sides in SIDE_THEMES.items():
            for side in ("white", "black"):
                with self.subTest(family=family, side=side):
                    self.assertEqual(set(sides[side]), want)
                    for key in want:
                        self.assertTrue(sides[side][key].strip(),
                                        "%s[%s].%s is empty" % (family, side, key))
        self.assertEqual(set(GENERIC_WHITE), want)
        self.assertEqual(set(GENERIC_BLACK), want)

    def test_the_chosen_opening_leads_the_guide_cards(self):
        """Picking a chip must change which master line and which notes you get."""
        chosen = _alapin()["eco_guides"][0]
        self.assertEqual(chosen["eco"], "B22")
        self.assertEqual(chosen["strategic_ideas"]["family"], "Sicilian Defense")
        with mock.patch.object(sd, "_flat_search", side_effect=_yt([])):
            other = discover("Alapin", side_pref="black", mode_pref="against", chosen_eco="D16")
        self.assertEqual(other["eco_guides"][0]["eco"], "D16")
        self.assertEqual(other["eco_guides"][0]["strategic_ideas"]["family"], "Slav Defense")

    def test_guides_carry_the_opponents_view_when_facing_it(self):
        ideas = _alapin()["eco_guides"][0]["strategic_ideas"]
        # The user is Black and the Alapin is played by White, so the notes the
        # guide carries describe the opponent.
        self.assertEqual(ideas["side"], "white")


if __name__ == "__main__":
    unittest.main()
