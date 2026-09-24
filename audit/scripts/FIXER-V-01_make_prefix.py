"""
Reconstruct the PRE-FIX snapshot of this fixer wave by reverting FIXER-V's changes.

Run once, at the moment the patch is being prepared:

    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-V-01_make_prefix.py

It copies the *current* `backend/repertoire.py` and `frontend/repertoire_trainer.js` to
`/tmp/opencode/fixerv/` and then applies the exact inverse of every FIXER-V hunk, so the new
regression tests in `tests/test_line_end_semantics.py` can be run against the code as it was
before the patch (fail-before / pass-after evidence, AGENTS.md §6.4).

Idempotent: re-running rebuilds the snapshot from the current files.
"""
import os
import shutil
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = "/tmp/opencode/fixerv"
REPERTOIRE_PREFIX = os.path.join(OUT_DIR, "repertoire_prefix.py")
TRAINER_PREFIX = os.path.join(OUT_DIR, "repertoire_trainer_prefix.js")

# (label, new_text, old_text) -- the inverse of every FIXER-V hunk, applied to the copy.
BACKEND_REVERTS = [
    (
        "module constants + classify_line_position",
        "",
        '''
# --------------------------------------------------------------------------- #
# Line-end semantics (audit FIXER-V / TRAINER-09, owner ruling)
# --------------------------------------------------------------------------- #

#: The line stops on the **user's own prepared move** and the repertoire genuinely has nothing
#: more: a real completion. `is_line_end` is `True`.
LINE_END_REPERTOIRE_EXHAUSTED = "repertoire_exhausted"

#: The repertoire holds one or more opponent replies for which the user has prepared **no**
#: answer. The board is still resting on the user's own move (the trainer refuses to move onto
#: an unanswered opponent branch), so `is_line_end` is `True`, but the line stopped because of a
#: *gap in the repertoire*, not because the line is finished. The UI must not celebrate it.
LINE_END_UNANSWERED_BRANCHES = "unanswered_opponent_branches"

#: The three states a drill position can be in, per the owner's ruling.
LINE_POSITION_CONTINUES = "continues"            # the opponent moved and the user has a reply
LINE_POSITION_COMPLETED = "completed_line"      # the user's own move is the last thing prepared
LINE_POSITION_AWAITING_REPLY = "awaiting_user_reply"  # the opponent moved, nothing is prepared


def classify_line_position(reached_by_user_move: bool, has_prepared_reply: bool) -> str:
    """The single definition of "is this line finished?" (owner ruling, FIXER-V).

    A **completed line** is a position reached at the end of the *user's own prepared move*,
    where the repertoire genuinely has nothing more. An **unanswered opponent branch** is a
    position reached by an *opponent* move for which the user has prepared nothing — that is a
    gap in the repertoire, never a completion, and it is never presented as a drill choice.

    :param reached_by_user_move: whether the move that produced this position is one of the
        user's own prepared moves (i.e. the board is resting on their move, so it is their turn).
    :param has_prepared_reply: whether the repertoire holds a prepared user move at this position.
    :returns: one of the three ``LINE_POSITION_*`` constants. The
        ``(False, False)`` case - the opponent moved and nothing is prepared - is the bug the
        owner overruled, and it is deliberately **not** a completion.
    """
    if has_prepared_reply:
        # The opponent moved and the user has something to play: the line continues.
        return LINE_POSITION_CONTINUES
    if reached_by_user_move:
        # The user's own move is the end of what they have prepared. A real completion.
        return LINE_POSITION_COMPLETED
    # THE BUG: the opponent moved and the user has not prepared a reply. A gap, not an end.
    return LINE_POSITION_AWAITING_REPLY
''',
    ),
    (
        "evaluate_trainer_move: opponent-reply selection",
        '''            if opp_replies:
                # Check which opponent replies have a prepared user follow-up move
                replies_with_followup = []
                for opp in opp_replies:
                    opp_fen = opp["fen"]
                    opp_fen_3 = " ".join(opp_fen.split()[:3])
                    prep_cand = None
                    if active_study:
                        clean_study = active_study.strip()
                        cursor.execute("""
                        SELECT DISTINCT rm.* FROM repertoire_moves rm
                        LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
                        LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
                        WHERE rm.repertoire = ? AND (rm.parent_fen = ? OR rm.parent_fen = ? OR rm.parent_fen LIKE ?) AND rm.is_user_move = 1
                          AND (rs.name = ? OR rm.study_name = ?)
                        LIMIT 1
                        """, (repertoire, opp_fen, opp_fen_3, f"{opp_fen_3} %", clean_study, clean_study))
                        prep_cand = cursor.fetchone()

                    if not prep_cand:
                        cursor.execute("""
                        SELECT * FROM repertoire_moves
                        WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?) AND is_user_move = 1
                        ORDER BY is_important DESC, id ASC
                        LIMIT 1
                        """, (repertoire, opp_fen, opp_fen_3, f"{opp_fen_3} %"))
                        prep_cand = cursor.fetchone()

                    if prep_cand:
                        replies_with_followup.append((opp, dict(prep_cand)))

                if replies_with_followup:
                    weights = [max(1.0, float(r[0].get("popularity_pct") or 0.0)) for r in replies_with_followup]
                    chosen_pair = random.choices(replies_with_followup, weights=weights, k=1)[0]
                    opponent_reply = chosen_pair[0]
                    next_prep = chosen_pair[1]
                    next_expected_uci = next_prep["move_uci"]
                    next_expected_san = next_prep["move_san"]
                    # TRAINER-04: report the study the continuation REALLY belongs to, not the
                    # requested one, so `next_study_name` and `next_opening_name` cannot disagree.
                    next_study = ((next_prep.get("study_name") or "").strip()
                                  or active_study
                                  or next_prep.get("opening_name") or "")
                    next_opening_name = next_study or next_prep.get("opening_name") or ""
                else:
                    # Opponent replies exist, but line ends after opponent move (no prepared user response)
                    weights = [max(1.0, float(r.get("popularity_pct") or 0.0)) for r in opp_replies]
                    opponent_reply = random.choices(opp_replies, weights=weights, k=1)[0]
                    next_prep = None
                    next_expected_uci = None
                    next_expected_san = None
''',
        '''            if opp_replies:
                # Check which opponent replies have a prepared user follow-up move
                replies_with_followup = []
                for opp in opp_replies:
                    opp_fen = opp["fen"]
                    opp_fen_3 = " ".join(opp_fen.split()[:3])
                    prep_cand = None
                    if active_study:
                        clean_study = active_study.strip()
                        cursor.execute("""
                        SELECT DISTINCT rm.* FROM repertoire_moves rm
                        LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
                        LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
                        WHERE rm.repertoire = ? AND (rm.parent_fen = ? OR rm.parent_fen = ? OR rm.parent_fen LIKE ?) AND rm.is_user_move = 1
                          AND (rs.name = ? OR rm.study_name = ?)
                        LIMIT 1
                        """, (repertoire, opp_fen, opp_fen_3, f"{opp_fen_3} %", clean_study, clean_study))
                        prep_cand = cursor.fetchone()

                    if not prep_cand:
                        cursor.execute("""
                        SELECT * FROM repertoire_moves
                        WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?) AND is_user_move = 1
                        ORDER BY is_important DESC, id ASC
                        LIMIT 1
                        """, (repertoire, opp_fen, opp_fen_3, f"{opp_fen_3} %"))
                        prep_cand = cursor.fetchone()

                    if prep_cand:
                        replies_with_followup.append((opp, dict(prep_cand)))
                    else:
                        unanswered_branches += 1
                        unanswered_branch_sans.append(opp.get("move_san") or opp.get("move_uci") or "?")

                if replies_with_followup:
                    weights = [max(1.0, float(r[0].get("popularity_pct") or 0.0)) for r in replies_with_followup]
                    chosen_pair = random.choices(replies_with_followup, weights=weights, k=1)[0]
                    opponent_reply = chosen_pair[0]
                    next_prep = chosen_pair[1]
                    next_expected_uci = next_prep["move_uci"]
                    next_expected_san = next_prep["move_san"]
                    # TRAINER-04: report the study the continuation REALLY belongs to, not the
                    # requested one, so `next_study_name` and `next_opening_name` cannot disagree.
                    next_study = ((next_prep.get("study_name") or "").strip()
                                  or active_study
                                  or next_prep.get("opening_name") or "")
                    next_opening_name = next_study or next_prep.get("opening_name") or ""
                # else: every opponent branch in the repertoire is unanswered. The line stops on
                # the user's OWN move (`opponent_reply` stays None) and `unanswered_branches`
                # reports how much of the position is still unplayable, so the client can say
                # "add a reply in the Builder" instead of celebrating a completed line.
''',
    ),
    (
        "evaluate_trainer_move: the FIXER-V preamble",
        "            if opp_replies:\n                # Check which opponent replies have a prepared user follow-up move\n",
        '''            # AUDIT FIXER-V / TRAINER-09 (owner ruling: "A line should never finish on an
            # opponent's move."): an opponent move the user has **no prepared reply** for is not a
            # line end, it is an unanswered branch. It is therefore *not a drill choice*: the
            # trainer never moves the board onto it, so the board can never come to rest on an
            # opponent's move with nothing to play. `is_line_end` below is derived from that
            # invariant (see `is_line_ended`), and `line_end_reason` states which of the two
            # honest reasons the line stopped. The "line ends after an opponent move" branch that
            # used to live here is gone.
            unanswered_branches = 0
            unanswered_branch_sans: List[str] = []
            if opp_replies:
                # Check which opponent replies have a prepared user follow-up move
''',
    ),
    (
        "evaluate_trainer_move: is_line_end derivation",
        "        is_line_ended = (opponent_reply is None or next_expected_uci is None)\n",
        '''        line_position = classify_line_position(
            reached_by_user_move=(opponent_reply is None),
            has_prepared_reply=bool(next_expected_uci),
        )
        if line_position == LINE_POSITION_AWAITING_REPLY:
            is_line_ended = False
            line_end_reason = None
            unanswered_branches = max(1, unanswered_branches)
            opponent_reply = None
            next_prep = None
            next_expected_uci = None
            next_expected_san = None
        elif line_position == LINE_POSITION_COMPLETED:
            is_line_ended = True
            line_end_reason = (LINE_END_UNANSWERED_BRANCHES if unanswered_branches
                               else LINE_END_REPERTOIRE_EXHAUSTED)
        else:
            is_line_ended = False
            line_end_reason = None
''',
    ),
    (
        "evaluate_trainer_move: response fields",
        '            "is_line_end": is_line_ended\n        }',
        '''            "is_line_end": is_line_ended,
            # FIXER-V: the classification that produced `is_line_end`, plus the reason the line
            # stopped. "repertoire_exhausted" = the user's own prepared move is genuinely the last
            # thing in the repertoire (a real completion); "unanswered_opponent_branches" = the
            # repertoire holds N opponent replies the user has prepared no answer to, so the line
            # stops because of a *gap*, not because the line is finished. `null` while it continues.
            "line_position": line_position,
            "line_end_reason": line_end_reason,
            "unanswered_branches": unanswered_branches,
            "unanswered_branch_sans": unanswered_branch_sans[:8],
        }''',
    ),
    (
        "get_repertoire_tree: answered/unanswered preamble",
        "        for pos_fen, op_name, eco in opp_positions:\n",
        '''        user_move_parents3 = {
            " ".join((m.get("parent_fen") or "").split()[:3])
            for m in moves if m.get("is_user_move")
        }
        unanswered_branch_count = 0
        counted_positions = set()
        for pos_fen, op_name, eco in opp_positions:
''',
    ),
    (
        "get_repertoire_tree: answered/unanswered classification",
        '            prepared_replies = [reply["move_uci"] for reply in tree_by_parent.get(pos_fen, [])]\n',
        '''
            pos_3 = " ".join((pos_fen or "").split()[:3])
            covered_replies, unanswered_replies = set(), set()
            for reply in tree_by_parent.get(pos_fen, []) or tree_by_parent.get(pos_3, []):
                if reply.get("is_user_move"):
                    continue
                uci = reply.get("move_uci") or ""
                if not uci or uci in covered_replies or uci in unanswered_replies:
                    continue
                child_3 = " ".join((reply.get("fen") or "").split()[:3])
                if child_3 and child_3 in user_move_parents3:
                    covered_replies.add(uci)
                else:
                    unanswered_replies.add(uci)
                    # `opp_positions` can name one position twice (a 4-token and a 3-token
                    # spelling of it), so the repertoire-side count must be de-duplicated.
                    key = (pos_fen, uci)
                    if key not in counted_positions:
                        counted_positions.add(key)
                        unanswered_branch_count += 1
''',
    ),
    (
        "get_repertoire_tree: coverage + gap loop",
        '''                # Coverage metric (was a second, identical explorer loop)
                total_opp_weight += pop_pct
                if opp_m.get("uci") in prepared_replies:
                    covered_opp_weight += pop_pct

                if pop_pct >= gap_threshold and opp_m.get("uci") not in prepared_replies:
                    gaps.append({
                        "parent_fen": pos_fen,
                        "opening_name": op_name,
                        "eco": eco,
                        "opponent_san": opp_san,
                        "opponent_uci": opp_m.get("uci", ""),
                        "popularity_pct": pop_pct,
                        "win_rate": float(opp_m.get("win_rate") or 0.0),
                        "games": opp_m.get("games", 0)
                    })
''',
        '''                cand_uci = opp_m.get("uci", "")
                # FIXER-V: only an ANSWERED branch counts as coverage (see the comment above).
                total_opp_weight += pop_pct
                if cand_uci in covered_replies:
                    covered_opp_weight += pop_pct

                if pop_pct >= gap_threshold and cand_uci not in covered_replies:
                    # FIXER-V: a branch the repertoire holds but cannot answer is a gap of the
                    # same kind as a branch it does not hold at all - it finishes on the
                    # opponent's move either way - so it is reported here, tagged so the Builder
                    # can say "you have this move, you have no answer" instead of "missing".
                    is_unanswered = cand_uci in unanswered_replies
                    gaps.append({
                        "parent_fen": pos_fen,
                        "opening_name": op_name,
                        "eco": eco,
                        "opponent_san": opp_san,
                        "opponent_uci": cand_uci,
                        "popularity_pct": pop_pct,
                        "win_rate": float(opp_m.get("win_rate") or 0.0),
                        "games": opp_m.get("games", 0),
                        "in_repertoire": is_unanswered,
                        "gap_kind": "unanswered_branch" if is_unanswered else "missing_branch",
                    })
''',
    ),
    (
        "get_repertoire_tree: unanswered_branches in the result",
        '            "total_moves": len(moves),\n',
        '''            "total_moves": len(moves),
            # FIXER-V: how many of this repertoire's own opponent branches have no prepared user
            # reply. A pure repertoire-side count (no explorer involved), reported next to the
            # coverage figure so a dead-end branch is never silently read as a finished line.
            "unanswered_branches": unanswered_branch_count,
''',
    ),
]

TRAINER_REVERTS = [
    (
        "onUserAttempt preamble + the two line-end branches",
        '''    async onUserAttempt(from, to, promo, moveRes) {
        if (!this.isWaitingForUser || !this.currentFen) return;''',
        None,  # filled in below
    ),
]


def apply_reverts(text, reverts):
    for label, new_text, old_text in reverts:
        if old_text is None:
            continue
        count = text.count(old_text)
        if count != 1:
            raise SystemExit("hunk %r matched %d times (expected 1)" % (label, count))
        text = text.replace(old_text, new_text, 1)
    return text


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    src_py = os.path.join(PROJECT_ROOT, "backend", "repertoire.py")
    src_js = os.path.join(PROJECT_ROOT, "frontend", "repertoire_trainer.js")
    shutil.copyfile(src_py, REPERTOIRE_PREFIX)
    shutil.copyfile(src_js, TRAINER_PREFIX)

    with open(REPERTOIRE_PREFIX, "r", encoding="utf-8") as fh:
        py = fh.read()
    py = apply_reverts(py, BACKEND_REVERTS)
    with open(REPERTOIRE_PREFIX, "w", encoding="utf-8") as fh:
        fh.write(py)

    # The frontend revert is mechanical: drop the helper and restore the two branches.
    with open(TRAINER_PREFIX, "r", encoding="utf-8") as fh:
        js = fh.read()
    start = js.index("    /**\n     * AUDIT FIXER-V")
    end = js.index("    async onUserAttempt(", start)
    js = js[:start] + js[end:]

    new_branch = js[js.index("                        } else {\n                            // AUDIT FIXER-V:"):
                   js.index("                        }\n                    }, 550);")]
    js = js.replace(new_branch, '''                        } else {
                            // Line complete after opponent reply!
                            if (this.currentDrill) {
                                this.currentDrill.expected_uci = null;
                                this.currentDrill.expected_san = null;
                            }
                            this.isWaitingForUser = false;
                            this.updateHintButtonState(false);

                            if (feedback) {
                                feedback.style.display = 'block';
                                feedback.className = 'feedback-box correct';
                                feedback.textContent = `Opponent replied with ${opp.san}. \U0001f389 Line Complete!`;
                            }
                            window.showToast?.('\U0001f389 Line Complete!', '#4ade80', 1200);

                            // Advance immediately in session (short 200ms delay so opponent move animation completes)
                            if (this.sessionIndex < this.sessionTarget) {
                                clearTimeout(this.autoNextTimeout);
                                this.autoNextTimeout = setTimeout(() => {
                                    if (drillToken !== this.drillToken) return;
                                    this.startNewDrill();
                                }, 200);
                            }
''')

    new_no_reply = js[js.index("                } else {\n                    // The board rests on the position the user's OWN prepared move produced."):
                       js.index("                    // Advance immediately in session (quick 150ms delay for smooth transition)")]
    js = js.replace(new_no_reply, '''                } else {
                    // Line complete immediately after user move (no opponent reply in tree)
                    if (this.currentDrill) {
                        this.currentDrill.expected_uci = null;
                        this.currentDrill.expected_san = null;
                    }
                    this.isWaitingForUser = false;
                    this.updateHintButtonState(false);

                    if (feedback) {
                        feedback.style.display = 'block';
                        feedback.className = 'feedback-box correct';
                        feedback.textContent = `\U0001f389 Line Complete!`;
                    }
                    window.showToast?.('\U0001f389 Line Complete!', '#4ade80', 1200);

''')

    with open(TRAINER_PREFIX, "w", encoding="utf-8") as fh:
        fh.write(js)

    print("pre-fix backend : %s" % REPERTOIRE_PREFIX)
    print("pre-fix frontend: %s" % TRAINER_PREFIX)
    return 0


if __name__ == "__main__":
    sys.exit(main())
