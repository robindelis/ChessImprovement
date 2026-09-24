"""Side-aware strategic background for named openings.

`OpeningBook.opening_themes` answers "what is this opening about" in a fixed
White-ish voice, and only for the seven families that were written by hand. That
is enough for a tooltip and not enough for the Scout, which now has to answer two
different questions with it:

* *"I want to play this"* -- the user needs **their** plans and **their** traps.
* *"I want to face this"* -- the user needs the **opponent's** plans, because
  knowing what you are about to be attacked with is the whole point.

Those are different sentences, so the data is stored per side. The 2×N prose lives
here rather than in `backend/openings.py` so the encyclopedia loader stays about
loading, and so a family can be added without touching the index code.

Three rules govern the content:

1. **It must be about the opening, not the position.** A generic line about
   "developing pieces and castling" is true of every game ever played and teaches
   nothing; it is what the previous fallback produced, and it is why the Scout's
   panel could not say anything useful about, say, the Alapin.
2. **Each side gets what that side is trying to do**, not a mirror image. Black's
   counterplay in the Sicilian is not White's plan with the colours swapped.
3. **The ECO code is not the subject.** Families are matched on the *name*, by
   word boundary, so "frenchy" never becomes the French Defense and
   "Queen's Indian Accelerated" is not caught by a "Defense" rule.

Every family is keyed by its `OpeningBook.split_name` family, lower-cased, which
is how it is matched at lookup time. A family that is absent here is not an error:
:func:`OpeningBook.get_strategic_ideas` falls back honestly and says so.
"""

#: Families whose *name* contains this are two-sided and must not be resolved to
#: one colour. Kept here so the strategies and the side classifier agree.
EITHER_SIDED = frozenset({
    "formation", "creepy crawly formation", "english orangutan",
    "symmetrical", "symmetrical defense", "symmetrical defence",
    "slav indian", "india defense", "indian defense",
})

#: `family -> {"white": {...}, "black": {...}}`, each side carrying
#: `core_idea`, `typical_mistakes` and `key_plans`.
SIDE_THEMES = {
    "sicilian defense": {
        "white": {
            "core_idea": "You have handed Black a half-open c-file and a queenside majority, and been paid a central pawn for it. White's job is to trade pieces quickly so the d4-knight cannot be hit, and to open the c-file before Black is organised.",
            "typical_mistakes": "Playing slowly and letting ...d5 and ...e5 consolidate; grabbing the c-pawn back with d4-cxd4 and losing a tempo; keeping the b1-knight out after the queenside is open; putting the queen's knight on d2 too early.",
            "key_plans": "Nd5 or Be3 immediately, queenside castling, cxd4 or d4, and a minority/pawn majority attack on the c-file before Black castles.",
        },
        "black": {
            "core_idea": "An asymmetric central fight: Black has traded the c-pawn for White's d4-pawn and gained the half-open c-file and a d5/e5 central majority, in exchange for the bishop pair and a backward d6-pawn.",
            "typical_mistakes": "Premature ...e5 before the pieces are developed; playing ...a6 and ...b5 with the king in the centre; trading the c8-bishop for the f3-knight and losing the only thing holding the diagonal; walking into a discovered attack on the d6-pawn.",
            "key_plans": "Develop with ...Nc6 and ...g6, keep the ...e5 break for the right moment, open the c-file with ...b5, and time ...O-O behind the e6-pawn.",
        },
    },
    "najdorf": {
        "white": {
            "core_idea": "The Najdorf is the most flexibly defended Sicilian: Black has not yet committed to a structure, so White must decide fast which of the two plans to meet — the sharp central one or the slower queenside expansion.",
            "typical_mistakes": "Playing Be3 and Bc4 without a purpose and ending up worse in both time and space; grabbing on d4 with cxd4 and giving up the only lasting weakness; attacking on the kingside before the queenside is closed.",
            "key_plans": "Meet 6.Be3 with d4 and a fast queenside attack; meet 6.Bg5 with d5 and a slow, grinding centre; after any 6th move ask which pawn structure Black is heading for and prepare it, not the move.",
        },
        "black": {
            "core_idea": "A move that asks White a question Black has already partly answered. 5...a6 controls b5, discourages White's queenside play and prepares both ...b5 and ...d5 depending on how White continues.",
            "typical_mistakes": "Playing 5...a6 and then not knowing what to do; keeping the d6-pawn forever instead of trading it for the d4-knight; king-safety panics that throw away the position; playing ...Rfe8 without a concrete plan.",
            "key_plans": "After 6.Be3, play d4 and attack; after 6.Bg5, play d5 and prepare ...e6; after 6.Bc4, play d6 and attack the c3-pawn; after 6.f4, kick with ...e5 and go after the weak d4/e5 squares.",
        },
    },
    "french defense": {
        "white": {
            "core_idea": "You have the centre and the space; the difficulty is that Black's light-squared bishop and the b-pawn are static, so White must convert a spatial edge into a real attack before Black trades it away.",
            "typical_mistakes": "Trading the c4-knight for the f6-knight and losing the bishop; an unsound early e5 push; leaving the f7-pawn undefended behind the d6-pawn; letting Black's pieces come out in peace.",
            "key_plans": "f4-f5-f6 to take the f7, or cxd5 to open the d-file; keep the c4-knight alive; play e5 only with the f7-square covered.",
        },
        "black": {
            "core_idea": "A rigid e6-d5 chain that challenges White's e4 outright and forces White to commit, at the cost of the light-squared bishop and a passive c8.",
            "typical_mistakes": "Attacking White's centre with ...c5 while the pieces are undeveloped; the bad-bishop problem solved by ...c6 (blocking its own pieces); queenside castling into a queenside attack; trading into the Winawer, the sharpest French, with no preparation.",
            "key_plans": "Develop with ...Nf6, ...Be7 and ...O-O; strike d4 with ...c5 or the f6-square with ...f6; get the c8-bishop to d7 or b5; in the Winawr, play for ...b4, ...Bb4 and ...c3.",
        },
    },
    "caro-kann defense": {
        "white": {
            "core_idea": "Black has taken on d5 and kept the d5/d6 structure, so White's centre is respected but the game becomes about the half-open d-file and the c6-pawn's hold.",
            "typical_mistakes": "Playing 3.e4 and reaching a position with no plan; grabbing on d5 with cxd4-cxd4 and opening the d-file by accident; rushing f5 without the pieces out; ignoring the classic 3.e4 dxe4 knight-endgame.",
            "key_plans": "3.e4 and a fast d4; or 3.d4 for a quiet centre; after any trade, use the extra tempo for a kingside initiative; be ready for 3.e4 dxe4 4.Nxe4 Nd7 5.Nf3 Nxe4 6.Qe2.",
        },
        "black": {
            "core_idea": "A solid ...c6/...d5 structure that challenges e4 while leaving the light-squared bishop free to develop, giving up a tempo to avoid the French's bad bishop.",
            "typical_mistakes": "Blocking the c8-bishop behind the e-pawn before developing; dropping d5 to an early ...dxe4 without knowing the endgame; playing ...c6 in the Advance Variation and then not knowing what to do with the c5-pawn; refusing all counterplay and letting White grind.",
            "key_plans": "Develop ...Nf6 and ...d6 with ...e6; strike the centre with ...c5; after 3.e4 play ...dxe4 and be happy to go into a knight endgame; in the Advance, play ...c5 and go after the c2-pawn.",
        },
    },
    "italian game": {
        "white": {
            "core_idea": "A classical, harmonious position aiming at the f7-weakness: you want your pieces out, the centre resolved, and the kingside pawn storm to arrive before Black castles.",
            "typical_mistakes": "Trading the active c4/f3-bishop in the Giuoco Piano without a compensating plan; playing f4 too early and getting a target on g4; pushing g4 without the pieces developed; an unsound f3-f4 in the Two Knights.",
            "key_plans": "d3-d4 and O-O-O for the Giuoco Pianissimo; f4-f5-f6 in the Giuoco Piano; in the Two Knights, Nf3-f6 (Fried Liver) or d4 and a kingside attack; keep the c4-bishop.",
        },
        "black": {
            "core_idea": "A classical equal position where Black counters by trading in the centre to remove White's activity, and by fighting for the centre with ...d5.",
            "typical_mistakes": "Grabbing on d5 with cxd4 and handing back the c-file; the Giuoco Plan traps (Keres, Four Knights) played without the pawn moves; taking a bad queen trade by accident; ...Nf6 without a plan for the e5-pawn.",
            "key_plans": "5...d5 and a quick recapture to get the e6-pawn; the Giuoco Piano with ...d6 and ...c6 keeping the bishop; the Two Knights with ...Ng4, ...h6, ...f4 and ...d5; O-O early.",
        },
    },
    "ruy lopez": {
        "white": {
            "core_idea": "You have pressure on the defender of the e5-pawn and a cramped opponent, so the aim is to restrict, trade on e5 and then storm the weak queenside pawns.",
            "typical_mistakes": "Losing the structure of the Berlin by trading d4 for d5; the Open's Ruy without taking the a-pawn; playing a4-a5 too early and dropping the c3-pawn; moving the h3-knight to f1 or g5 and getting hit on the dark squares.",
            "key_plans": "4.Bb5 with pressure on the knight; in the Closed, O-O, Re1, Bc2 and a slow queenside attack on the c-pawn; in the Open, capture a-pawns and go after c3/c2; in the Berlin, keep the position solid and hit the h7-pawn.",
        },
        "black": {
            "core_idea": "Defending a cramped position by trading towards equality: either on e5 (Closed) or by giving up the centre for the dark squares (Berlin, Marshall).",
            "typical_mistakes": "Passive king play — moving the king repeatedly with no pawn structure behind it (the Berlin's main sin); taking a bad trade; in the Open, racing with the wrong pawns; failing to prepare ...d4.",
            "key_plans": "...d6 and ...Re8 with a solid formation (Closed); in the Berlin, play for the h7-pawn with ...b5 and ...Rxc3; in the Open, ...d4 and ...Rxc3 with counterplay; swap pieces to ease the squares.",
        },
    },
    "queen's gambit declined": {
        "white": {
            "core_idea": "You have a space advantage and a bishop pair; the difficulty is converting it, because Black's structure is solid and the c1-bishop often finds no good square.",
            "typical_mistakes": "Trading the c1-bishop for the f6-knight and losing the pair; premature e5 advances that lose the f6-square; attacking the c6-pawn with Nb5 and getting hit on the d5-knight; playing e4 in the Semi-Slav without knowing the structure.",
            "key_plans": "Rd1, Bh2 and a later e4; Nf3-f5 to attack the d6-pawn; e4 with a knight on d5 to take space and open the f-file; in the Chigorin, prepare h4-h5 before committing.",
        },
        "black": {
            "core_idea": "A solid e6/d5 framework that gives up the centre for unbreakable pawns and lives on the c6-knight, the d5-pawn and counterplay on the half-open c-file.",
            "typical_mistakes": "Trading d5 for c4 and opening the d-file for free; premature ...c5; taking the d4-pawn and leaving the king in the centre; chasing White's queen with ...e5 instead of improving.",
            "key_plans": "...c5 to break in the centre; ...cxd4 and ...c5 counterplay on the c-file; ...b5 and ...Bb7 to attack e4; ...b4 to attack the c3-pawn; keep pieces developed before counterplay.",
        },
    },
    "queen's gambit": {
        "white": {
            "core_idea": "You offer a flank pawn to get absolute control of d4 and e4, and you usually do not intend to give it back; the whole opening is a race to develop and centralise before Black catches up.",
            "typical_mistakes": "Giving the pawn back with dxc4 or Qxc4 and handing Black the e4/d4 squares; playing c4 before d4 and delaying development; attacking before the pieces are out; playing e4 and then letting Black close the centre with ...c5.",
            "key_plans": "d4, c4, Nc3/Nf3, Bg5/Bf4 and a fast central pawn advance; 3.dxc4 for a different, more forcing game; e4 with the knights out; castle queenside and attack before Black does.",
        },
        "black": {
            "core_idea": "You decide whether to take or decline, and each choice buys Black a different structure: ...dxc4 gives a hanging pawn and open c-file, ...Nf6 keeps the tension, ...c6 (Slav) exchanges a tempo for a solid structure with an active dark bishop.",
            "typical_mistakes": "Taking the pawn and then not exploiting the open c-file; the Slav played passively instead of ...c5; premature ...c5 before development; ...c5 with the knight to d5 and ...f6 all in the air.",
            "key_plans": "Decline and play ...Nf6, ...Be7 and ...O-O, then ...c5; accept and go after the c-pawn with ...c5; the Semi-Slav with ...c6 and an immediate ...c5; the Chigorin with ...Nc6, ...Bf5 and ...e6.",
        },
    },
    "queen's gambit accepted": {
        "white": {
            "core_idea": "You have an extra tempo and full central control, so the game is about development speed and avoiding a trade that hands Black counterplay on the open c-file.",
            "typical_mistakes": "Playing Bf4 and Ng5 with no follow-up; trading the c1-bishop for the d7-pawn; leaving the e1-pawn backwards; failing to prepare d5 and e4 before Black castles.",
            "key_plans": "Bg5/Be3 followed by d5 and e4; keep the extra pawn, develop with tempo, and open the d-file.",
        },
        "black": {
            "core_idea": "You have taken a flank pawn and must now hold the resulting backward c4/half-open c-file position, using the b7-bishop and ...c5 for counterplay.",
            "typical_mistakes": "Playing passively and being out-developed; trading d5 for c4 to open the c-file for White; moving the king to the queenside with no cover; attacking the c4-pawn too early.",
            "key_plans": "...Nc6, ...Bf5, ...e6 and ...O-O; ...b5 to attack c4; open the c-file with ...c5 and ...cxd4 when White is not ready.",
        },
    },
    "nimzo-indian defense": {
        "white": {
            "core_idea": "You are fighting for e4 with pieces while the bishop pair is in the opponent's hands; the trick is to play for a knight on e3 and a pawn structure you control.",
            "typical_mistakes": "Playing 4.e3 in the Classical and then having no plan for the c1-bishop; the Sämisch 4.a3 Bxc3 bxc3 and then passively recapturing; trading the f3-knight to win e4; walking into the ...Bb4 pin.",
            "key_plans": "The Classical with a3, d3, Nbd2, Ne3 and a later c4; the Sämisch for dynamic play with a3-bxc3; keep the e4/e5 tension and play c4 to attack the d5-pawn.",
        },
        "black": {
            "core_idea": "Pin the f3-knight twice and trade it for the c3-bishop to claim the dark squares, then use the ...Bb4/+c3 shot and the open a-file for a minority attack.",
            "typical_mistakes": "Grabbing with ...Bxc3 and playing passively instead of ...a6; taking on c2 and letting White castle queenside with a tempo; the Classical ...a6 played without a plan; losing the bishop pair and the position together.",
            "key_plans": "...Bxc3 bxc3 and the c5/c6 dark squares; ...a6 with the minority attack on the b5-pawn; ...Bb4, ...c3 and ...Qa5 when White castles queenside; open the a-file in the Sämisch.",
        },
    },
    "queen's indian defense": {
        "white": {
            "core_idea": "You have traded the dark bishop for the f6-knight to claim e4 and a full central pawn majority, and you play for a quick c4-d5 to open the d-file.",
            "typical_mistakes": "Playing e4 too early before your pieces are out; c4 and d5 without support, so Black's pieces re-group; trading the b1-knight for the c6-knight; attacking the b7-pawn before the centre is secure.",
            "key_plans": "a3, d3, c4 and e4 for the central crush; Ne2/Nf3 and Nc3 with d5; the fianchetto with g3, Bg2 and a later d5; keep the c4/d5 pawns supported.",
        },
        "black": {
            "core_idea": "A nimzo-Indian with more dynamic play: use the ...b4 shot and the a4/b5 lever to unbalance White before the pawn centre is consolidated.",
            "typical_mistakes": "Killing the c3-bishop and then ending up with a passive, equal position; ...a3 too early; taking on d5 and letting White's pieces out; not noticing the b4/b5 tactic.",
            "key_plans": "...a6, ...Nc6, ...Bb7 and ...Be6; the main line ...a6, ...e6, ...Bb7, ...Nbd7; counterplay with ...b5 and ...b4; ...c5 to hit d4 and keep the position closed.",
        },
    },
    "catalan opening": {
        "white": {
            "core_idea": "A hybrid: you take the Queen's Gambit with d5, then fianchetto and play g3/Bg2 to aim at the long diagonal and the h7-weakness behind an extra central space advantage.",
            "typical_mistakes": "Missing the g3/Bg2 setup and playing as a plain QGD; d5 too early before the pieces are developed; attacking on the kingside before the queenside is closed; ...a4/...c6 type moves not being answered at all.",
            "key_plans": "d4, c4, g3, Bg2, Nf3, O-O and a later d5 and e4; attack h7 with the bishop and queenside pressure with the a/h-pawns; open the d-file with cxd5 and d5.",
        },
        "black": {
            "core_idea": "A closed centre plus ...c6 keeps the dark squares for the bishop and pressures d4, at the cost of giving White a big centre and the g2-square.",
            "typical_mistakes": "Playing passively and letting White dominate; ...e5 with no preparation, giving White a target; taking on d4 with cxd4 and losing the c-file without compensation; king walks.",
            "key_plans": "...c6, ...e6, ...b6 and ...Bb7; ...c5 to hit d4 and the c1-bishop; the Closed with ...d5 and ...e6; counterplay on the c-file with ...c5.",
        },
    },
    "slav defense": {
        "white": {
            "core_idea": "You have traded a tempo for a solid, mobile structure, and you use it to develop fast and prepare a later d5 and c4 to attack the hanging c6-pawn.",
            "typical_mistakes": "The Semi-Slav 8.bxc4 with no follow-up and a bad structure; playing c4 early against ...b4; attacking the c6-pawn without support; allowing ...c5 to fix the structure and shut you out.",
            "key_plans": "Against the Slav proper, Nf3, d5 and c4; against the Semi-Slav, take on c4 and go early (b5/b4) to break the structure; open the c-file with cxd5.",
        },
        "black": {
            "core_idea": "A ...c6 exchange that develops with tempo and keeps the dark-squared bishop for the queenside, trading a little activity for long-term solidity.",
            "typical_mistakes": "The Semi-Slav ...b4 without knowing the sharp 8.bxc4 b5 line; playing passively; ...c5 too early; king in the centre against a cxd4/ b4 battery.",
            "key_plans": "...c6, ...Nf6, ...d5, ...Nc3, ...Bf5; the Semi-Slav with ...b4 and the ...b5 counterstrike; ...e5 with the b5-pawn sacrifice; ...c5 to exchange and pressure the c-file.",
        },
    },
    "semi-slav defense": {
        "white": {
            "core_idea": "You have traded into a structure where the c4-pawn and the b-pawn can be attacked, so the game is a race: break early or lose the position slowly.",
            "typical_mistakes": "Taking on c4 and then not playing b5; a4 without the b4-pawn behind it; the 9.a4 b4 10.Bg5 line played without knowing it; missing the exchange sac when it is on.",
            "key_plans": "8.bxc4 and 9.a4/b4/b5 to break the centre; cxd5 and dxc4 for the half-open c-file; the sharp 9.a4 (Kasparov Gambit) and 9.b4 lines; play for the c-file and the d5-pawn.",
        },
        "black": {
            "core_idea": "The Slav with a ...b4 wedge: you gain a tempo to attack the c4-pawn and can play for ...c5 to open the half-open c-file for your rook.",
            "typical_mistakes": "Playing 8...bxc4 and 9...b4 with the king in the centre; ...c5 too early; taking the c4-pawn and ending up equal; missing ...e5 in the Kasparov Gambit.",
            "key_plans": "8...bxc4 9.b4 with ...b5, ...Bb4 and a later ...c5; the Kasparov 9...e5 sacrificing the b4-pawn; ...c5 to open the c-file; the Solid with ...a6 and the semi-open a-file.",
        },
    },
    "benoni defense": {
        "white": {
            "core_idea": "You have traded a centre pawn for a kingside initiative and a half-open c-file, so the game is about how fast you can attack before Black castles and manoeuvres.",
            "typical_mistakes": "Not playing g3/Bg2 or f3 before attacking; letting ...a5 and ...Qa5+ happen with pieces off; trading the d4-pawn to relieve the pin and end up with nothing; missing the c5-battery.",
            "key_plans": "g3, Bg2, O-O and the f/e-pawns at the king; cxd5 to open the c-file for a queenside attack; f3 to grab the dark squares; keep the d4-pawn and use the pin.",
        },
        "black": {
            "core_idea": "A deliberately unbalanced counterpunch: give up the centre for a quick ...c5, ...a5, ...Qc7 and a plan to trade pieces and castle long.",
            "typical_mistakes": "Catching the pawn but then playing passively; grabbing the c4-pawn and losing a piece; ...Qc7 without ...c5; trading into a winning position for White's attacking one.",
            "key_plans": "...c5 and ...a5 with ...Qc7, ...Nf6, ...e6; the Taimanov with ...c5, ...Nc6, ...e5; castle queenside and attack the weak g7/...; trade pieces and open lines for the rooks.",
        },
    },
    "grünfeld defense": {
        "white": {
            "core_idea": "You have been allowed a big centre in exchange for being able to trade pieces and attack a king that may not castle; the trade is real: activity against a solid, sometimes cramped, king.",
            "typical_mistakes": "Trading all the pieces and finding it equal; attacking before the pieces are out; giving up the centre with d5-d6; missing the pressure on the d5-pawn's defender.",
            "key_plans": "Trade in the centre, keep one piece for the endgame, and use the d5-pawn's weakness; d4-d5 to open the c-file; g2-g3 to prepare the bishop for the d5-pawn.",
        },
        "black": {
            "core_idea": "Displace the centre with ...d5 and ...g6, keep a pawn on d5 to be attacked, and castle queenside for an ...e5-...c5-...c4 counterattack on the half-open files.",
            "typical_mistakes": "Holding onto too many bishops; ...Rg7 without a plan; ...Qb3 without the ...c5 and ...c4 support; trading into a position where the pieces are all on one side.",
            "key_plans": "3.g6 and ...Bg7 with ...d5; the Fianchetto with ...g6, ...Bg7, ...O-O-O and ...c5, ...c4, ...b5; the Russian with ...c5 and a quick ...cxd4; keep the a/b pawns ready for a counterattack.",
        },
    },
    "king's indian defense": {
        "white": {
            "core_idea": "You have a big centre and a permanent pawn target, and the game is about whether you can open lines against the king before Black's ...f5-f4 storm.",
            "typical_mistakes": "Playing e5 too early and giving away the attack; not preparing g3 and Bg2 to protect the centre; the Saemisch f3 with no follow-up; walking into the kingside storm with pieces off.",
            "key_plans": "g3 and Bg2 to keep the centre and castle; the Saemisch f3, Nf3 and h4 to keep for a pawn storm; e5 and exf6 to attack; a closed centre and a queenside attack.",
        },
        "black": {
            "core_idea": "A hypermodern defence: cede the centre, castle kingside, and let White overstrain his pawns before the ...f5-f4 attack breaks through.",
            "typical_mistakes": "Letting White's pawn storm land first; ...f5 with the pieces unmotivated; taking on f4 or f3 and losing the position; the ...b4 and ...Bb7 systems without a plan.",
            "key_plans": "6...g6, ...Bg7, ...O-O and ...f5-f4; the Sämisch 6...b5 and ...a6; the Bayonet with ...b5, ...d5, ...e5 and ...f5-f4-f3; ...b4 and ...Bb4 to undermine d3/c2.",
        },
    },
    "dutch defense": {
        "white": {
            "core_idea": "You are facing a pawn structure that wants to be attacked from behind: a weak e6-pawn and a backward f-pawn, so White's game is preparation plus a sacrifice when it works.",
            "typical_mistakes": "The Staunton Gambit played without follow-up; attacking before your pieces are out; grabbing on f5 and losing a piece; missing the m5-m6 break to attack the backward f-pawn.",
            "key_plans": "d4 for the Classical or the Stonewall structure; the Leningrad with g3/Bg2 and a later e4; the Staunton Gambit as a weapon, not a plan; h4-h5 with m4-m5.",
        },
        "black": {
            "core_idea": "A reversed Sicilian: a big centre for White, a cramped but active position, and counterplay on the half-open files and the a/b pawns.",
            "typical_mistakes": "The Staunton 4...g5 and then a passive king; ...c6 and ...a5 without a real plan; trading the f5-pawn and losing the initiative; missing the ...Nb6 and ...Bb4 combination.",
            "key_plans": "The Staunton with ...g5, ...g6, ...Bg7, ...O-O and ...f5-f4; the Leningrad with ...Bb4 and ...Ne4, or the Stonewall with ...d5; the Classical with ...Nh6, ...f6 and the e6-pawn as a target; counterplay with ...c5 and ...c4.",
        },
    },
    "sicilian defense": {
        "white": {
            "core_idea": "You have traded a centre pawn for the half-open c-file and a queenside majority; the aim is to trade pieces, open the c-file and attack before Black organises.",
            "typical_mistakes": "The Open: not knowing when to play d4 and c3; the Closed: playing cxd4 without a plan; the Semi-Closed: attacking before d5/e5; the Kalashnikov: playing a slow English and being outprepared.",
            "key_plans": "The Open with d4, cxd4 and a queenside attack; the Closed with d5, Be3, O-O and a slow centre; the Semi-Closed with d5 and f4; the Kalashnikov with c3, d4, Nc3 and a quick d5.",
        },
        "black": {
            "core_idea": "Choose a Sicilian that suits the player: a Najdorf for maximum flexibility, a Poisoned Pawn for immediate activity, a Kalashnikov for a pawn centre, or an Open for dynamic balance.",
            "typical_mistakes": "Automatic responses with no plan; the Poisoned Pawn played without knowing the main lines; dragging out the Open with passive play; answering 5.d4 with the Dragon but then having no plan.",
            "key_plans": "Najdorf: ask White a question and answer the next one; Poisoned Pawn: d6, Nf6, Qe7, Nc3 and a fast ...d5/...b5; Kalashnikov: c5, Nc6, Nge7 and ...d5; Dragon: g6, Bg7, d6 and ...d5, keeping the Yugoslav/Classical options.",
        },
    },
    "french defense, classical": {
        "white": {
            "core_idea": "You have a space advantage and good piece coordination against a passive defence; the plan is to keep the e4-pawn, improve your worst piece, and open a line into Black's camp.",
            "typical_mistakes": "A premature e5 with the f7-pawn undefended; trading the c4-knight; putting the f1-bishop on g2 too late; failing to prepare c4 or f4 before committing.",
            "key_plans": "h3/Qh4, Bd3 and O-O; f4 to take f7; cxd5 to open the d-file; prepare e5 with f4 played first.",
        },
        "black": {
            "core_idea": "The Winawer's quieter cousin: a passive but extremely solid position where you live on ...c5, ...Bb4, ...Qa5 and the c5-battery.",
            "typical_mistakes": "Attacking with ...c5 and ...f6 with the pieces undeveloped; queenside castling into a queenside attack; missing the c5-pawn battery; taking on f4 and losing a piece.",
            "key_plans": "5...Bb4, 6.Bd3 c5, 7.Nge2 cxd4 and the c5-pawn; ...c5 and ...f6 against White's centre; ...Qc7 and ...Rd8 before castling; trade pieces and hold.",
        },
    },
    "alekhine defense": {
        "white": {
            "core_idea": "You have a full centre and an extra tempo, so the game is about converting a superiority in the middle before Black's pieces come out and trade.",
            "typical_mistakes": "Grabbing on d4 with Nxd4 and losing a piece; playing e5 without support and getting hit on the d4-pawn; attacking the king before the pieces are developed; the exchange variation played passively.",
            "key_plans": "c4 first (Four Pawns Attack territory) with a later d5 and e5; Be3, d4, Nf3 and a quick e4; trade pieces to reach a winning endgame with the extra pawn.",
        },
        "black": {
            "core_idea": "A hypermodern defence that concedes the centre to castle kingside and hit the pieces behind it with ...Nd4 and ...Nc6.",
            "typical_mistakes": "Grabbing with ...Nd4 and getting hit on e5/d5; the Four Pawns Exchange played without a plan; ...Kf8-e7 king walks; the ...Bb5 and ...Nc6 systems without counterplay.",
            "key_plans": "Four Pawns with ...Bb5, ...d6, ...Nbd7 and ...c5; the Exchange with ...Nc6, ...e6 and ...Bb4; counterplay with ...Nc6, ...Nge7 and ...d5; trade into an equal endgame.",
        },
    },
    "scandinavian defense": {
        "white": {
            "core_idea": "You get the centre and a free tempo while Black's queen and bishops are awkward, so the game is a race to develop and trade the awkward pieces out.",
            "typical_mistakes": "Not trading when you should; attacking the king before developing; the Modern Variation 4.Nxd4 with a delayed recapture played passively; missing the theme of a quick e5.",
            "key_plans": "Trade pieces early, develop with tempo, and take space in the centre; e5 in the Modern to gain space; the Main Line with Qxd4 and a quick e4.",
        },
        "black": {
            "core_idea": "Trade the d4-pawn early to damage White's structure and develop with tempo, at the cost of the queen leaving early.",
            "typical_mistakes": "Delaying ...Qxd4 and losing the advantage; the Modern 4.Nxd4 without ...Nxd4; queen walks; failing to get both bishops out and castle.",
            "key_plans": "...Qxd4 with a quick ...Nxd4 and ...Bf5; the Modern with ...Nxd4, ...Nf6 and ...Bb4; the Russian with ...Nbd7, ...Nf6 and ...c6; castle kingside and accept the position.",
        },
    },
    "pirc defense": {
        "white": {
            "core_idea": "A big centre, a passive opponent and a coming storm: you must decide quickly whether to play a sharp pawn attack (f4, c4) or a positional one (d4, Nf3, e4).",
            "typical_mistakes": "f4 and f5 before the pieces are out; the sharp Austrian with no follow-up; d4 and then having no plan; allowing ...c5 to shut the centre and reaching a passive position.",
            "key_plans": "Austrian 4.f4 with a quick f5-f6; the sharp f4-f5 lines; the Classical with d4, Nc3, Bg5 and a later d5; trade pieces and keep the e5-square.",
        },
        "black": {
            "core_idea": "Lend the centre, trade pieces into a slightly better endgame, and keep the ...c5 break and the queenside counterplay for after the pieces are gone.",
            "typical_mistakes": "The Austrian played without a plan; ...c5 at the wrong moment; king in the centre; the 180/190 played too passively instead of ...c5 and ...d4.",
            "key_plans": "...d6, ...g6, ...Bg7 and ...O-O; the Austrian with ...c5, ...Nc6 and ...f5; the 180 with ...d5 and ...Nd5-e3 trades; play ...c5 and ...b5 after the queenside is closed.",
        },
    },
    "modern defense": {
        "white": {
            "core_idea": "A reversed Sicilian: you have a big centre, a kingside attack and a passive opponent, so attack quickly or you will simply be outplayed.",
            "typical_mistakes": "Not playing g3 and Bg2; the Averbakh with no plan; playing at the king before the pieces are developed; the Norwood with a passive 4.g3.",
            "key_plans": "Norwood 4.g3, Bg2, d3 and a later e4; the Averbakh with d4, Nc3 and a quick f3-f4; the Elephant with d4 and Be3; the Caro-Kann hedge by taking on c6.",
        },
        "black": {
            "core_idea": "Hold the centre with the king's bishop, trade pieces into a better endgame, and keep the queenside counterplay; accept a slightly cramped position.",
            "typical_mistakes": "Attacking with ...c5 too early; king moves without cover; the Averbakh played without the ...Nc6/d5 plan; the Old Indian played passively.",
            "key_plans": "...d6, ...g6, ...Bg7, ...O-O; the Averbakh with ...Nc6, ...d5 and ...d4; the Caro-Kann move order 3.Nd3 c5 4.dxc4; trade into the endgame and hold.",
        },
    },
    "philidor defense": {
        "white": {
            "core_idea": "The opponent's pieces are awkward, so the plan is a quick central pawn advance that exploits the cramping before Black can trade out of it.",
            "typical_mistakes": "Not exploiting the cramping early; the Classical 6.Bg5 h6 and then playing passively; the Exchange 5.Nxe6 wrongly; allowing a trade into a balanced endgame.",
            "key_plans": "The Classical with d4, Qd2 and a quick d5; the Exchange with d4, Nxd4 and open lines; the Poisoned Pawn 6.Be3 with d4 and d5.",
        },
        "black": {
            "core_idea": "Trade in the centre and hold a slightly worse-looking but solid position; a practical choice that avoids theory and reaches a playable endgame.",
            "typical_mistakes": "playing ...e5 too early; the Classical without ...Nbd7 and ...Bd7; missing the ...d5 break; grabbing on e6 and losing a piece.",
            "key_plans": "Nbd7, Bd7, Qe7 and ...O-O; trade in the centre for the bishop pair's opponent's safety; the Hanham with ...e6, ...Ne7 and ...d6; hold and play ...d5 when possible.",
        },
    },
    "vienna game": {
        "white": {
            "core_idea": "A second-player's opening: you have nothing against the Italian but an extra tempo, so you need a plan that creates pressure without overcommitting.",
            "typical_mistakes": "Playing f4-f5 with nothing prepared; the Frankenstein-Dracula without a point; treating it like the Italian and getting a slightly worse version; not knowing when to transpose.",
            "key_plans": "d4 and a quick f5-f6 against the Frankenstein; the Frankenstein-Dracula with d4, Nd5 and Be3; the Frankenstein with d5, Nd5 and c4; transpose to the Italian or the Open Game with d4.",
        },
        "black": {
            "core_idea": "Simplify to a good Italian or French position with an extra tempo; the Vienna is close to equal and should be played to reach a known structure.",
            "typical_mistakes": "Playing the Vienna instead of transposing to something you know; a premature ...d5; the Frankenstein played passively; missing the ...Bc5 and ...d6 ideas.",
            "key_plans": "...d6 and ...Nf6 towards the Two Knights or the Open Game; ...e6 into the French; the Frankenstein with ...d6, ...Nf6, ...Bc5 and ...d5; use the extra tempo for d5 and c4.",
        },
    },
    "scotch game": {
        "white": {
            "core_idea": "You have hit the centre hard and early; the aim is to prevent Black from getting a good Sicilian or French and to keep the c-file under pressure.",
            "typical_mistakes": "The Mieses with a passive Bd3; trading the c4-knight for the f6-knight and losing the tempo; the Steinitz with no plan; missing the c5-c6 ideas.",
            "key_plans": "Mieses 4.d5 with d4, Ne5 and a quick c4; Steinitz with d4 and a normal centre; the Classical with Nc3, Qf3 and Bg5; play d4 and open the d-file.",
        },
        "black": {
            "core_idea": "Answer 4.d5 with ...d6 and ...Nf6, taking the c4-pawn seriously and steering into a known structure; the Scotch is close to equal.",
            "typical_mistakes": "Greedy ...Nxd5 without a plan; the Mieses played passively; missing the ...b5 and ...Bxc3 counterplay; allowing d4-d5 with no pieces developed.",
            "key_plans": "Mieses 4...d6 5.c4 with ...d5 and ...c6; the Classical with ...Nf6, ...Bb4 and ...O-O; the Steinitz with a normal ...e6 and ...d6 structure; transpose to the Open or the Centre Game.",
        },
    },
    "petrov's defense": {
        "white": {
            "core_idea": "You have the better structure and a bishop pair, so the game is about avoiding trades and using the c-file and centre to build a lasting advantage.",
            "typical_mistakes": "Trading pieces and ending up equal; attacking the d5-pawn without support; the Classical with e4 and no follow-up; playing passively against a solid opponent.",
            "key_plans": "Keep the bishop pair, play d4 and c4 to attack d5; the Steinitz with d4 and a slow centre; the Classical with e4, Nf3 and Be3; trade only on favourable terms.",
        },
        "black": {
            "core_idea": "Simplify into a solid, slightly cramped but resilient position; avoid theory and lean on the ...e5 break and the c-file.",
            "typical_mistakes": "Grabbing on c3 and losing a piece; king walks in the Classical; missing ...e5 at the right moment; trading into a position where the pawn structure is worse.",
            "key_plans": "...e5 with a quick ...c4; the Classical with ...d6, ...Nf6, ...Be7 and ...O-O; the Steinitz with ...d5 and ...c6; hold the position and take ...e5 when White is unprepared.",
        },
    },
    "four knights game": {
        "white": {
            "core_idea": "A transposition tool: you can head for a Spanish, a Scotch, the Two Knights or an Evans, so use the open board for a quick space advantage.",
            "typical_mistakes": "The Scotch/Italian transpositions played without a point; the Two Knights with a passive 4.Nxe5; not exploiting the exposed d5-pawn; playing f4 with no pieces out.",
            "key_plans": "The Italian/Spanish transposition with d4; the Two Knights with Nxe5 and a quick Nc3, d4 and f4; the Scotch with 4.d5; the Evans with d4, Be2 and Bc4.",
        },
        "black": {
            "core_idea": "Play the pieces you know: transpose into the Spanish, the Italian or the Two Knights; in the Two Knights, prepare ...d6, ...Nxe4 and the f5/h4 attack on the king.",
            "typical_mistakes": "Playing the Four Knights passively; the Two Knights ...d6 and ...Nxe4 without the ...Nf6 and ...f5 follow-up; the Italian with a passive ...d6; missing the d5-pawn tactic.",
            "key_plans": "The Spanish with d6, Bc5 and a later ...Re8; the Two Knights with ...d6, ...Nxe4, ...Nf6, ...f5 and ...h4; the Italian with ...d6, ...Bc5 and ...c3; transpose and play it well.",
        },
    },
    "english opening": {
        "white": {
            "core_idea": "A reversed Sicilian: you have a tempo, a big potential centre and a flexible setup, so you can decide between a reversed Sicilian, a Four Pawns Attack and a Maroczy.",
            "typical_mistakes": "The Reversed without knowing the structure; the Botvinnik with a passive 3...Bb4; playing e4 and e5 with nothing behind; the Four Knights with no plan.",
            "key_plans": "The Reversed with d4/e5 or a Botvinnik setup; the Four Pawns with c4, d3, Nc3 and g3; the Maroczy with d4/e5 and a bishop pair; transpose to a Sicilian structure with a tempo in hand.",
        },
        "black": {
            "core_idea": "You may transpose into a Sicilian, play a Botvinnik System, or choose the King's English/Three Knights — the position is flexible, so pick the one you know.",
            "typical_mistakes": "The Botvinnik played passively; the Four Knights with a passive ...e5; the Reversed with a passive ...c5; missing the ...c5 break.",
            "key_plans": "The Botvinnik with ...Bb4, ...e6 and ...c5; the Reversed with ...c5, ...Nc6, ...Nf6 and ...e5; transpose to a Sicilian with ...c5 and ...d6; the King's English with ...e5 and a quick ...c5.",
        },
    },
    "r\u00e9ti opening": {
        "white": {
            "core_idea": "A flank opening that transposes everywhere: 1.Nf3, c4, g3 and d4 lead to the English, the King's Indian defences, the Reti and sometimes a Sicilian.",
            "typical_mistakes": "The Zandvoort with a passive plan; playing d4 against a Dragon transpose and losing the point; not knowing which structure you are heading for; attacking before your pieces are out.",
            "key_plans": "Nf3, d4, c4 and g3 for a Reti/English structure; the Zandvoot with d4, c4 and a quick d5; transpose deliberately and know the target; play g3 and Bg2 to fianchetto.",
        },
        "black": {
            "core_idea": "Transpose deliberately, or play a pure hypermodern ...g6/...d6 set-up that keeps the position flexible and attacks the centre later.",
            "typical_mistakes": "Playing a move that loses a piece in the Zandvoot; the King's Indian Fianchetto with a passive ...d5; staying in the tabiya with no plan by move six; missing the ...c5 break.",
            "key_plans": "...g6, ...Bg7, ...d6, ...Nc3 and ...Nf6 with a later ...e5; the Zandvoot with ...d5 and ...Nbd7; transpose into a King's Indian or a Modern; hold the position and play ...d5.",
        },
    },
    "london system": {
        "white": {
            "core_idea": "A set-up rather than a line: Bf4, c3, d4, e3 and Nd2 with a plan to grab a big centre and use the bishop pair and a space advantage.",
            "typical_mistakes": "The Big Pawn Game with a passive h4-h5; the Bishop's London with a passive king pawn; the Twin Towers pushing d5 too early; not trading pieces when the structure is closed.",
            "key_plans": "The Big Pawn Game with d4, d5 and h4-h5 for the kingside; the Twin Towers with d4, d5, e4 and a later c4; the Bishop's London for the c5 attack; trade pieces and use the extra space in the endgame.",
        },
        "black": {
            "core_idea": "Set up a solid structure that makes the White centre safe, then attack it: ...e6, ...d5, ...b4 and the ...c5, ...Re8, ...Na5 battery in the Anti-London.",
            "typical_mistakes": "The Anti-London with a passive ...e5; the Solid playing too passively; missing the ...Re8 and ...Na5 battery; taking on d5 in the King's Indian Fianchetto and losing the position.",
            "key_plans": "The Solid with ...e6, ...b4, ...Bb7, ...Qe7, ...c5; the Anti-London with ...e5, ...Re8, ...Na5, ...h5 and ...f4; the Fianchetto with ...d6, ...g6, ...B7 and ...d5; exchange pieces and hold the closed centre.",
        },
    },
    "trompowsky attack": {
        "white": {
            "core_idea": "You are playing a Queen's Gambit with the bishop on f4 instead of e3, which allows Bxf4 and often a transposition to the QGD, Nimzo-Indian or Bird.",
            "typical_mistakes": "Playing Bxf4 without a follow-up; the Agripin gambit played unsoundly; no plan after ...Nf6; the Classical with a passive b4.",
            "key_plans": "Transpose to the QGD with d4, c4 and Nc3; the Agripin with b4 and c5; the Classical with d4, c4, Nc3 and a quick d5; play e4 and d4 for a Bird-like centre.",
        },
        "black": {
            "core_idea": "Defend the Queen's Gambit with the Modern set-up: ...Nf6, ...g6, ...Bg7 and ...O-O, keeping the structure flexible and a ...c5 break available.",
            "typical_mistakes": "The ...Nf6 gambit without a follow-up; the Modern with a passive ...d5; missing the ...e5 break; allowing d4 and a big centre with no counterplay.",
            "key_plans": "The Modern with ...Nf6, ...g6, ...Bg7, ...O-O and ...d5; the ...Nf6 gambit with ...exd4 and a quick attack on the c1-bishop; the Classical with ...d5 and ...Be7; transpose to a known structure.",
        },
    },
    "bishop's opening": {
        "white": {
            "core_idea": "A tabiya that buys time to choose a structure: you can reach the Italian, the Ruy Lopez, the English, the King's Gambit or a Bird, so decide what you want before Black does.",
            "typical_mistakes": "Staying in the tabiya without a plan by move six; f4-f5-f6 against a passive opponent; the doubled-pawn Bird positions played without a point; missing the d4 opening.",
            "key_plans": "c3, d4, Nc3, Nf3 and Bg5 towards the Ruy or the Italian; e4, e5 and d3 towards the King's Gambit; f4-d4-e4-e5 for the Bird; b3 and Bb2 for a fianchetto setup.",
        },
        "black": {
            "core_idea": "Choose a structure early and stop the tabiya: the Italian with ...e5 and ...Nf6, the Sicilian with ...c5 and ...d6, the French with ...e6, or the King's Gambit with ...e5.",
            "typical_mistakes": "The Classical (waiting) move with a passive ...e6; delaying the ...c5 in the Sicilian; the 3...Nf6 gambit with no follow-up; playing passively in the tabiya.",
            "key_plans": "The Classical with ...e6, ...d6, ...Nc3; transpose to the Sicilian with ...c5, ...Nc6, ...g6; the French with ...e6 and ...d5; the King's Gambit with ...e5, ...Bc4, ...Nf3 and ...d6.",
        },
    },
    "grob opening": {
        "white": {
            "core_idea": "A sharp gambit of a tempo: you attack the f7/h7 weaknesses and the g-pawn with 1.g4, but you have given Black a free developing tempo and a centre.",
            "typical_mistakes": "The Spies Opening without a follow-up; the Grob with 2.Nf3 and no plan; attacking before the pieces are out; missing d5 and c4 when the position opens.",
            "key_plans": "The Grob with Nf3, Bg5 or d4 and a quick f5-f6; the Spies with d5, f5-f6 and c4; transpose to a reversed Sicilian with ...c5; play for a fast kingside attack.",
        },
        "black": {
            "core_idea": "Use the tempo to develop and punish: ...g5, ...g6, ...Bg7, ...d6, ...e5 and ...h6, grabbing space and attacking the weakened king.",
            "typical_mistakes": "The Grob with a passive ...d5; missing ...h6 against the g-pawn; the Spies with a slow ...d5; trading into the position when the structure is good for White.",
            "key_plans": "The Grob with ...g5, ...g6, ...Bg7, ...d6 and ...Ng7-e5; the Spies with ...g6, ...Bg7, ...e5 and ...d5; the Anti-Grob with ...d5 and ...e5; develop fast and attack the centre.",
        },
    },
    "bird's opening": {
        "white": {
            "core_idea": "A 1.f4 opening that avoids theory and leads to sharp positions with the g3/Bg2 or d5/e4 plans; the f1-bishop goes to b5 or c4 and the king gets castled long.",
            "typical_mistakes": "The Dutch with a passive c4; the d5/e4 Bird without a plan; f4-f5-f6 with the king in the centre; losing a piece in the sharp Lasker and-from-Bird lines.",
            "key_plans": "g3, Bg2, O-O and the f/e-pawns; d5 and e4 for a Bird/Portuguese structure; f5-f6 in the King's Indian Bird; the c3 Bird with Nf3 and e4.",
        },
        "black": {
            "core_idea": "Grind against the weakened king: ...g6, ...Bg7, ...d6, ...Nbd7 and ...e5, or the ...b6/Bb7 system with ...Ne4 and a kingside pawn storm.",
            "typical_mistakes": "Playing passively against the f4-pawn; the King's Indian Bird without a plan; the from-Barn with a passive ...f6; king walks.",
            "key_plans": "The KID with ...g6, ...Bg7, ...d6, ...Nbd7, ...e5; the b6 system with ...Bb7, ...Ne4 and ...f5; the Classical with ...Nf6, ...c6 and ...e6; attack the weakened king and trade pieces.",
        },
    },
    "owen defense": {
        "white": {
            "core_idea": "You deliberately play a strange move to wreck the opponent's coordination; it works when you follow it with c4/d4 and a normal structure, and fails when you improvise.",
            "typical_mistakes": "The Owen without c4 and d4; playing a normal game after the odd move; attacking before the setup is complete; losing a piece for no compensation.",
            "key_plans": "c4, d4, Nc3 and Nf3 after the Owen; transpose to a known structure quickly; use the tempo to prepare d5 and e4; keep the position solid and simple.",
        },
        "black": {
            "core_idea": "Punish the odd move with simple development and a normal structure; the Owen usually gives White a slightly awkward position at best.",
            "typical_mistakes": "Improvising against the odd move; missing the simple ...d5 or ...e5 breaks; playing passively; trading into a position where White is better placed.",
            "key_plans": "Develop simply with ...d6, ...Nf6 and ...e5; the b6 system with ...Bb7 and ...Ne4; play ...d5 early; reach a normal structure and play it well.",
        },
    },
    "polish opening": {
        "white": {
            "core_idea": "A flank opening full of pawns: you get an odd, flexible pawn centre and can reach the King's Indian Attack, the Symmetrical or a reversed Sicilian.",
            "typical_mistakes": "The Symmetrical with a passive king pawn; b4-b5 pushing without a plan; losing a piece in the sharp b4 lines; no plan after the pieces are out.",
            "key_plans": "The King's Indian Attack with d3, d4 and Nc3; the Symmetrical with a4, e4 and a later f4; b4-b5 for a pawn avalanche; c4/d5 for a reversed Sicilian.",
        },
        "black": {
            "core_idea": "Mirror or transpose: enter the King's Indian Attack structures, the Symmetrical, or a reversed Sicilian with ...c5, keeping the position flexible.",
            "typical_mistakes": "The Symmetrical with a passive ...f5; missing the ...c5 break; playing passively in the b4 lines; trading into a position where White has the initiative.",
            "key_plans": "The Symmetrical with ...d5, ...e5, ...f5 and ...g6; the KID structures with ...Nf6, ...c5 and ...d5; the b4 System with ...a5 and ...b5; take space and play ...c5.",
        },
    },
    "king's gambit": {
        "white": {
            "core_idea": "You have sacrificed a flank pawn for a huge centre and an attack on f7; the game is about keeping the initiative and getting the pieces to the kingside.",
            "typical_mistakes": "The Accepted 3...exf4 4.Nf3 with a passive 5.d4; the Kieseritzky with a passive Nf3; playing f4-f5-f6 before developing; losing the initiative with a trade.",
            "key_plans": "The Accepted with Nf3, d4, Bc4 and a quick d5; the Kieseritzky with Nf3 and Bc4, sacrificing back for the e-file; the Muzio with Nf3, Bc4, O-O and d4-d5; keep f4-f5-f6 as a weapon.",
        },
        "black": {
            "core_idea": "The Gambit Accepted is very good for Black: 3...exf4 leaves White with a target on e1 and a weak f4-pawn, so develop, castle and attack.",
            "typical_mistakes": "Taking the pawn and then playing passively, leaving the king vulnerable on e1; the Kieseritzky played without ...Nc6 and ...d6 support; grabbing the f4-pawn a second time and losing a piece; the Falkbeer with the pieces undeveloped.",
            "key_plans": "The Accepted with Nf6, g6, Bg7, O-O and ...Nc6, attacking e1 and then the f-pawn; the Kieseritzky with ...Nc6, ...d6 and ...e5; the Falkbeer with ...e5 and a quick ...d4; play ...g5 to keep the initiative.",
        },
    },
    "allgaier gambit": {
        "white": {
            "core_idea": "You have sacrificed a flank pawn for two bishops and a lead in development against the King's Gambit; the game is about keeping both bishops and the initiative.",
            "typical_mistakes": "The Two Bishops with a passive f4-f5; the Salvio with a passive Qd4; losing a piece in the sharp g4 lines; not knowing when to trade into the endgame.",
            "key_plans": "The Two Bishops with f4-f5-f6 and a quick d4; the Salvio with Qd4, c3, Bd3 and a long diagonals; the Max Lange with d4 and a quick d5; keep the bishops and attack the king.",
        },
        "black": {
            "core_idea": "A pawn down for activity and two bishops: develop fast, castle kingside and attack the weakened king, trading bishops only for the centre.",
            "typical_mistakes": "The Fisher with a passive ...g5; the Classical with a slow ...d6; king walks against two bishops; trading bishops for nothing.",
            "key_plans": "The Fisher with ...g5, ...g6, ...Bg7 and ...Nc3; the Classical with ...g5, ...Bg7, ...O-O and ...d6; attack the king and open lines for the rooks; trade pieces, keep the position",
        },
    },
}

#: The generic fallback. It is deliberately *not* dressed up as opening-specific
#: knowledge: it says the truth ("we do not have notes on this one") and gives
#: the universal priorities, which are still better than a wrong claim.
GENERIC_WHITE = {
    "core_idea": "Take space in the centre, complete development before you attack, and castle so that a counterpunch cannot reach an uncastled king.",
    "typical_mistakes": "Moving the same piece twice, opening a file before the pieces are out, and playing a flank pawn storm before the centre is closed.",
    "key_plans": "Develop, secure the centre, then decide whether the game is a direct attack or a positional squeeze.",
}
GENERIC_BLACK = {
    "core_idea": "Answer the centre, remove or undermine the cramping pawn, and trade into a position where your pieces are on active squares.",
    "typical_mistakes": "Grabbing a pawn without a plan, developing the king slowly, and letting the opponent set up with tempo while the pieces wait.",
    "key_plans": "Defend, find a break in the opponent's centre, and play for the counterattack once the pieces are placed.",
}
