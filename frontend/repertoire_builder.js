// Chess Opening Improvement Platform - Repertoire Builder (Chessbook Experience)

/**
 * Canonical FEN: the backend's `normalize_fen` (first four fields). The
 * halfmove/fullmove counters are semantically irrelevant to position identity
 * but participated in every raw string comparison in the session tree, which is
 * what let the same (position, move) pair be recorded twice (CODE-17).
 *
 * DUP-01: byte-identical to the copy in `scout.js` - this file has to be
 * self-sufficient because `tests/frontend_harness.js` loads it on its own.
 */
function normalizeTreeFen(fen) {
    if (!fen) return '';
    return String(fen).trim().split(/\s+/).slice(0, 4).join(' ');
}

// DUP-01 (consolidation pass, FIXER-M): this used to be a *fork* of the copy in
// `scout.js` that had drifted - it lacked the canonical-FEN child match (CODE-17),
// `isAttached()` + the `currentNode` repair in `deleteLine()` (CODE-02), the
// `1...` prefix and move-number handling in `_buildPgn()` (CODE-03) and the
// `lineId` overlap key in `getAllLines()` (CODE-06). Two live class objects meant
// every fix had to be written twice, and writing it once silently kept the other
// panel broken. The two class bodies are now BYTE-IDENTICAL (modulo the
// `typeof ... === 'undefined'` wrapper and the comments around them), which
// `tests/test_frontend_followups.py::test_the_two_session_tree_copies_are_identical`
// enforces - so a future divergence is a test failure, not a silent bug.
//
// What is still NOT done, and why, is in `audit/patches/FIXER-M-01.md` section 3:
// collapsing the two copies into one new `frontend/session_line_tree.js` needs a
// `<script>` tag in `index.html` (not this patch's file) AND a one-line change to
// the file list in `tests/frontend_harness.js`, because that harness loads only
// `board.js` + `repertoire_builder.js` and `audit/scripts/FIXER-J_xss_scenario.js`
// exercises `b.lineTree`. Until both land, the fork stays byte-identical instead
// of divergent.
if (typeof SessionTreeNode === 'undefined') {
    class SessionTreeNode {
        constructor(fen, parent = null, move = null) {
            this.id = 'node_' + Math.random().toString(36).substring(2, 9);
            this.fen = fen;
            this.parent = parent;
            this.move = move; // { fen_before, fen_after, uci, san, color, move_number, opening_name, eco, is_custom, comment, is_important, arrows }
            this.children = [];
        }

        isLeaf() {
            return this.children.length === 0;
        }

        isRoot() {
            return this.parent === null;
        }

        getPathFromRoot() {
            const path = [];
            let curr = this;
            while (curr && curr.move) {
                path.unshift(curr.move);
                curr = curr.parent;
            }
            return path;
        }
    }
    window.SessionTreeNode = SessionTreeNode;
}

if (typeof SessionLineTree === 'undefined') {
    class SessionLineTree {
        constructor(initialFen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1') {
            this.rootFen = initialFen;
            this.root = new SessionTreeNode(initialFen, null, null);
            this.currentNode = this.root;
        }

        reset(initialFen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1') {
            this.rootFen = initialFen;
            this.root = new SessionTreeNode(initialFen, null, null);
            this.currentNode = this.root;
        }

        addMove(moveObj) {
            if (!moveObj) return this.currentNode;
            const targetFen = normalizeTreeFen(moveObj.fen_after || moveObj.fen || '');
            // Match an existing child by UCI, or - when there is no UCI - by
            // canonical (4-field) FEN. The comparison is canonicalised: study-seeded
            // nodes carry the backend's normalised FEN while user-played nodes used
            // to carry chess.js's 6-field FEN, so the raw strings never matched and
            // the same (position, move) pair was recorded twice (CODE-17).
            const child = this.currentNode.children.find(c =>
                (c.move && moveObj.uci && c.move.uci === moveObj.uci) ||
                (!moveObj.uci && c.fen && targetFen && normalizeTreeFen(c.fen) === targetFen)
            );

            if (!child) {
                const fenAfter = targetFen;
                const created = new SessionTreeNode(fenAfter, this.currentNode, moveObj);
                this.currentNode.children.push(created);
                this.currentNode = created;
                return created;
            }

            // Update metadata if child already exists (richest-field-wins, so a
            // re-played move inherits the stored note / importance / arrows).
            if (child.move) {
                if (moveObj.san && !child.move.san) child.move.san = moveObj.san;
                if (moveObj.opening_name && !child.move.opening_name) child.move.opening_name = moveObj.opening_name;
                if (moveObj.eco && !child.move.eco) child.move.eco = moveObj.eco;
                if (moveObj.comment && !child.move.comment) child.move.comment = moveObj.comment;
                if (moveObj.is_important) child.move.is_important = true;
                if (moveObj.arrows && !child.move.arrows) child.move.arrows = moveObj.arrows;
            }
            if (!child.fen && targetFen) child.fen = targetFen;

            this.currentNode = child;
            return child;
        }

        stepBack() {
            if (this.currentNode && this.currentNode.parent) {
                this.currentNode = this.currentNode.parent;
                return this.currentNode;
            }
            return null;
        }

        getActiveLine() {
            if (!this.currentNode) return [];
            return this.currentNode.getPathFromRoot();
        }

        getAllLines() {
            const lines = [];
            const dfs = (node, path) => {
                if (node.isLeaf()) {
                    if (path.length > 0) {
                        const pgn = this._buildPgn(path);
                        const lastMove = path[path.length - 1];
                        const studyName = node.studyName || lastMove?.study_name || (path.find(m => m.study_name)?.study_name) || '';
                        lines.push({
                            leafNode: node,
                            lineId: node.id,
                            moves: [...path],
                            pgn: pgn,
                            plyCount: path.length,
                            name: node.lineName || lastMove?.opening_name || (path[0]?.opening_name) || 'Variation',
                            eco: lastMove?.eco || (path[0]?.eco) || '',
                            lastFen: node.fen,
                            studyName: studyName
                        });
                    }
                    return;
                }
                for (const child of node.children) {
                    if (child && child.move) {
                        dfs(child, [...path, child.move]);
                    }
                }
            };

            dfs(this.root, []);
            return lines;
        }

        /**
         * True while `node` is still reachable from the root *and* still listed in
         * its parent's `children` (a pruned node keeps its `parent` pointer, so the
         * upward walk alone would wrongly report it as attached).
         */
        isAttached(node) {
            let curr = node;
            while (curr) {
                if (curr === this.root) return true;
                const parent = curr.parent;
                if (!parent || !parent.children || parent.children.indexOf(curr) === -1) return false;
                curr = parent;
            }
            return false;
        }

        deleteLine(leafNode) {
            if (!leafNode || leafNode.isRoot()) return;
            let curr = leafNode;
            while (curr && curr.parent) {
                const parent = curr.parent;
                parent.children = parent.children.filter(c => c !== curr);
                if (parent.children.length > 0 || parent.isRoot()) {
                    break;
                }
                curr = parent;
            }
            // CODE-02: `currentNode` could stay on a pruned node, which detached it
            // from the tree - the next played move was then grafted onto the orphan
            // and vanished from getAllLines(). Fall back to the highest ancestor
            // that is still attached.
            if (!this.currentNode || !this.isAttached(this.currentNode)) {
                let deepest = this.root;
                let walker = this.currentNode;
                while (walker && walker !== this.root && this.isAttached(walker)) {
                    deepest = walker;
                    walker = walker.parent;
                }
                this.currentNode = deepest;
            }
            return this.currentNode;
        }

        getAllUniqueMoves() {
            const moves = [];
            const seen = new Set();
            const traverse = (node) => {
                if (node.move && node.move.uci && node.move.fen_before) {
                    // Canonical key: the 4-field FEN, so a study-seeded (normalised)
                    // and a user-played (6-field) record of the same move collapse.
                    const key = `${normalizeTreeFen(node.move.fen_before)}_${node.move.uci}`;
                    if (!seen.has(key)) {
                        seen.add(key);
                        moves.push(node.move);
                    }
                }
                for (const child of node.children) {
                    traverse(child);
                }
            };
            traverse(this.root);
            return moves;
        }

        _buildPgn(moves) {
            if (!moves || moves.length === 0) return '';
            let res = '';
            let moveNum = 1;
            for (let i = 0; i < moves.length; i++) {
                const m = moves[i];
                if (!m) continue;
                let cleanSan = (m.san || m.uci || '').trim();
                cleanSan = cleanSan.replace(/^\d+\.+(\.\.)?\s*/, '').trim();
                if (!cleanSan) continue;
                const color = (m.color === 'white' || m.color === 'black')
                    ? m.color
                    : (i % 2 === 0 ? 'white' : 'black');
                if (color === 'white') {
                    // The move number is printed but only advanced after Black's
                    // reply, so "1. e4 e5 2. Nf3" does not become "1. e4 e5 3. Nf3".
                    res += `${moveNum}. ${cleanSan} `;
                } else {
                    // A line may start with Black's reply ("1... e5"), which PGN
                    // spells with an explicit `1...` prefix (CODE-03).
                    res += (i === 0) ? `${moveNum}... ${cleanSan} ` : `${cleanSan} `;
                    moveNum++;
                }
            }
            return res.trim();
        }
    }
    window.SessionLineTree = SessionLineTree;
}

class RepertoireBuilder {
    constructor() {
        this.activeColor = 'white';
        this.gapThreshold = 5.0;
        this.openingGamesThreshold = 30;
        this.showPatchedGaps = false;
        this.history = [];
        this.currentStepIndex = 0;
        this.treeData = null;
        this.board = null;
        this.isInitialized = false;
        this.candidateCache = new Map();
        // CODE3-08: bounded LRU, keyed on the NORMALISED position (halfmove/fullmove are
        // irrelevant to the evaluation) so transpositions hit and the heap cannot grow forever.
        this.evalCache = new Map();
        this.evalCacheMax = 400;
        this.currentEvalAbort = null;
        this.lastEval = { scoreCp: 0, mate: null, depth: 0 };
        this.candidateSort = { col: 'threat', dir: 'desc' };
        this.currentCandidateData = null;
        this.candidateGen = 0;

        // Session Line Trees for White and Black
        this.sessionTrees = {
            white: new SessionLineTree(),
            black: new SessionLineTree()
        };
        this.activeSidebarTab = 'tree'; // 'tree' | 'games' | 'gaps'
        this.recentGames = [];
        this.activeReviewGame = null;
        this.activeReviewGap = null;
        this.gamesColorFilter = 'all';
        this.gamesSpeedFilter = 'all';
        this.gamesSearchQuery = '';
        // CODE3-05: request sequencing for /api/games/recent (list + tab badge).
        this.recentGamesSeq = 0;
        this.recentGamesAbort = null;
        this.recentCountAbort = null;
        this.treeOverlapMap = { white: {}, black: {} };
        this.activeOverlapDebounce = null;
        // CODE3-06: one generation counter + one abort handle for the whole overlap cluster.
        this.overlapGen = 0;
        this.overlapAbort = null;
        // CODE3-04: two INDEPENDENT sync streams, two independent cancel handles.
        this.resyncAbortController = null;
        this.recentSyncAbortController = null;
        // CODE3-12: in-flight latch for mutating UI actions.
        this.writeInFlight = new Set();
        this.currentStudy = '';
        this.studiesList = [];

        // --- AGENTS.md 4.H: the engine line and the opening description ---------
        // `engineLineOn` gates the *drawing* of Stockfish's principal variation only.
        // The evaluation stream itself is untouched (it is what feeds the eval bar),
        // so turning the line off costs no engine time and no extra request: the
        // same `pv` field arrives either way and is simply not rendered.
        this.engineLineOn = true;
        this.currentPv = null;
        // Set when the user wipes the board; suppresses the line until the next
        // navigation re-arms it (see `onBoardAnnotationsCleared`).
        this.engineLineDismissed = false;
        // Bounded LRU for the opening description, keyed on the 4-token position plus
        // the line that reached it, because a repertoire line is walked forwards and
        // backwards constantly and the answer for a given (position, line) is stable.
        this.openingInfoCache = new Map();
        this.openingInfoMax = 400;
        // One generation counter + one abort handle, so a fast walk along a line
        // cannot let a slow response for an abandoned position overwrite the strip.
        this.openingInfoGen = 0;
        this.openingInfoAbort = null;
    }

    /**
     * Toast helper that never throws, even before app.js has loaded.
     * `duration` is forwarded to `window.showToast` (app.js) so a long import warning
     * (e.g. "N move(s) skipped: …") is readable instead of vanishing after 3.2 s.
     */
    notify(message, color = '#94a3b8', duration) {
        if (typeof window.showToast === 'function') {
            if (typeof duration === 'number') window.showToast(message, color, duration);
            else window.showToast(message, color);
        } else if (typeof console !== 'undefined') {
            console.warn(message);
        }
    }

    /**
     * In-flight latch (CODE3-12). Returns false when `key` is already running, so a double click
     * on "+ Set as My Move" cannot fire two POSTs each fanning out into three follow-up requests.
     */
    beginWrite(key) {
        if (this.writeInFlight.has(key)) return false;
        this.writeInFlight.add(key);
        return true;
    }

    endWrite(key) {
        this.writeInFlight.delete(key);
    }

    get lineTree() {
        return this.sessionTrees[this.activeColor] || this.sessionTrees.white;
    }

    init() {
        if (this.isInitialized) return;

        const boardEl = document.getElementById('repertoireBoard');
        if (!boardEl) return;

        this.board = new InteractiveChessboard(boardEl, {
            orientation: 'white',
            interactive: true,
            onMove: (from, to, promo, moveRes) => this.onBoardMove(from, to, promo, moveRes),
            onArrowsChange: (arrows) => this.onArrowsChange(arrows),
            onAnnotationsCleared: () => this.onBoardAnnotationsCleared()
        });

        // Color toggles
        const btnWhite = document.getElementById('btnRepWhite');
        const btnBlack = document.getElementById('btnRepBlack');
        if (btnWhite) btnWhite.addEventListener('click', () => this.switchColor('white'));
        if (btnBlack) btnBlack.addEventListener('click', () => this.switchColor('black'));

        // Study selector and new study button
        const studySelect = document.getElementById('builderStudySelect');
        if (studySelect) {
            studySelect.addEventListener('change', (e) => this.onStudyChange(e.target.value));
        }
        const btnNewStudy = document.getElementById('btnNewStudy');
        if (btnNewStudy) {
            btnNewStudy.addEventListener('click', () => this.onNewStudyClick());
        }
        const btnGroupSubtree = document.getElementById('btnGroupSubtree');
        if (btnGroupSubtree) {
            btnGroupSubtree.addEventListener('click', () => this.openGroupSubtreeModal());
        }
        const btnCloseSubtreeModal = document.getElementById('btnCloseSubtreeModal');
        if (btnCloseSubtreeModal) {
            btnCloseSubtreeModal.addEventListener('click', () => this.closeGroupSubtreeModal());
        }
        const btnCancelSubtreeModal = document.getElementById('btnCancelSubtreeModal');
        if (btnCancelSubtreeModal) {
            btnCancelSubtreeModal.addEventListener('click', () => this.closeGroupSubtreeModal());
        }
        const btnConfirmGroupSubtree = document.getElementById('btnConfirmGroupSubtree');
        if (btnConfirmGroupSubtree) {
            btnConfirmGroupSubtree.addEventListener('click', () => this.submitGroupSubtree());
        }

        // Gap threshold selector
        const thresholdSelect = document.getElementById('repGapThreshold');
        if (thresholdSelect) {
            thresholdSelect.value = String(this.gapThreshold);
            thresholdSelect.addEventListener('change', (e) => {
                this.gapThreshold = parseFloat(e.target.value) || 5.0;
                this.refreshTree();
            });
        }

        // Opening Games Threshold selector (AGENTS.md 4.B.3: 10/20/30/50/100 games in the 2000+ DB)
        const openingThresholdSelect = document.getElementById('repOpeningThreshold');
        if (openingThresholdSelect) {
            openingThresholdSelect.value = String(this.openingGamesThreshold);
            openingThresholdSelect.addEventListener('change', (e) => {
                const next = parseInt(e.target.value, 10);
                if (Number.isNaN(next) || next < 0) return;
                this.openingGamesThreshold = next;
                this.refreshTree();
            });
        }

        // Navigation buttons
        const btnStart = document.getElementById('btnRepStart');
        const btnPrev = document.getElementById('btnRepPrev');
        const btnNext = document.getElementById('btnRepNext');
        const btnEnd = document.getElementById('btnRepEnd');
        const btnFlip = document.getElementById('btnRepFlip');
        const btnReset = document.getElementById('btnRepReset');
        const btnSeed = document.getElementById('btnSeedDefaults');

        if (btnStart) btnStart.addEventListener('click', () => this.jumpToStart());
        if (btnPrev) btnPrev.addEventListener('click', () => this.stepBack());
        if (btnNext) btnNext.addEventListener('click', () => this.stepForward());
        if (btnEnd) btnEnd.addEventListener('click', () => this.jumpToEnd());
        if (btnFlip) {
            btnFlip.addEventListener('click', () => {
                const newOr = this.board.options.orientation === 'white' ? 'black' : 'white';
                this.board.setOrientation(newOr);
                this.applyEvalBarOrientation(newOr);
                // The label colour class is orientation-independent, so it must be re-applied
                // after the flip or the last score stays dark-on-dark / light-on-white.
                this.renderEvalBar(this.lastEval.scoreCp, this.lastEval.mate, this.lastEval.depth);
            });
        }
        if (btnReset) {
            btnReset.addEventListener('click', () => {
                if (this.activeReviewGame || this.activeReviewGap) {
                    this.exitGameReview();
                } else {
                    this.jumpToStart();
                }
            });
        }
        if (btnSeed) {
            btnSeed.addEventListener('click', async () => {
                if (confirm("Reset and seed standard Italian Game, Sicilian Dragon, and King's Indian repertoire lines?")) {
                    await this.seedDefaults();
                }
            });
        }
        const btnResetDB = document.getElementById('btnResetRepertoire');
        if (btnResetDB) {
            btnResetDB.addEventListener('click', async () => {
                await this.confirmAndResetRepertoire();
            });
        }
        this.initImportModal();
        const btnResync = document.getElementById('btnResyncGames');
        if (btnResync) {
            btnResync.addEventListener('click', async () => {
                await this.resyncGames();
            });
        }
        const btnCancelSync = document.getElementById('btnCancelSync');
        if (btnCancelSync) {
            btnCancelSync.addEventListener('click', () => {
                this.cancelSync();
            });
        }

        // Move Tools Panel events
        const btnToggleImp = document.getElementById('btnRepToggleImportant');
        if (btnToggleImp) {
            btnToggleImp.addEventListener('click', async () => {
                await this.toggleCurrentMoveImportant();
            });
        }

        const btnDelCurrentMove = document.getElementById('btnRepDeleteCurrentMove');
        if (btnDelCurrentMove) {
            btnDelCurrentMove.addEventListener('click', async () => {
                await this.deleteCurrentMoveAndLine();
            });
        }

        const commentTextarea = document.getElementById('repMoveComment');
        if (commentTextarea) {
            let debounceTimer = null;
            commentTextarea.addEventListener('input', () => {
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(() => {
                    this.saveMoveComment(commentTextarea.value);
                }, 600);
            });
            commentTextarea.addEventListener('blur', () => {
                clearTimeout(debounceTimer);
                this.saveMoveComment(commentTextarea.value);
            });
        }

        const btnClearArr = document.getElementById('btnRepClearArrows');
        if (btnClearArr) {
            btnClearArr.addEventListener('click', () => {
                this.board.clearArrows();
                this.onArrowsChange([]);
            });
        }

        // Engine-line toggle (AGENTS.md 4.H). The button is never added or removed,
        // only de-emphasised, so the wrapping toolbar cannot re-flow (8.4), and the
        // preference survives a reload: an analysis view you had to re-arm on every
        // visit would just get turned off and left off.
        const btnEngineLine = document.getElementById('btnRepEngineLine');
        if (btnEngineLine) {
            this.engineLineOn = this._readEngineLinePref();
            this.renderEngineLineToggle();
            btnEngineLine.addEventListener('click', () => {
                this.engineLineOn = !this.engineLineOn;
                try { localStorage.setItem('chess.engineLineOn', this.engineLineOn ? '1' : '0'); } catch (e) {}
                this.renderEngineLineToggle();
                if (!this.engineLineOn) {
                    this.clearEngineLine();
                } else {
                    // Re-open the stream for the position already on the board so the
                    // line comes back immediately instead of waiting for a navigation.
                    const curr = this.history[this.currentStepIndex];
                    if (curr) this.startEvalStream(curr.fen);
                }
            });
        }

        // Keyboard navigation (arrows and vim keys: h / l, k / j)
        window.addEventListener('keydown', (e) => {
            const tag = e.target.tagName?.toLowerCase();
            if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) {
                return;
            }

            const repView = document.getElementById('viewRepertoire');
            if (!repView || !repView.classList.contains('active')) {
                return;
            }

            if (e.key === 'ArrowLeft' || e.key === 'h' || e.key === 'H') {
                e.preventDefault();
                this.stepBack();
            } else if (e.key === 'ArrowRight' || e.key === 'l' || e.key === 'L') {
                e.preventDefault();
                this.stepForward();
            } else if (e.key === 'ArrowUp' || e.key === 'k' || e.key === 'K' || e.key === 'Home') {
                e.preventDefault();
                this.jumpToStart();
            } else if (e.key === 'ArrowDown' || e.key === 'j' || e.key === 'J' || e.key === 'End') {
                e.preventDefault();
                this.jumpToEnd();
            }
        });

        // Setup candidate table interactive column sorting
        const bindSortHeader = (elId, col) => {
            const th = document.getElementById(elId);
            if (!th) return;
            th.addEventListener('click', () => {
                if (this.candidateSort.col === col) {
                    this.candidateSort.dir = (this.candidateSort.dir === 'desc' ? 'asc' : 'desc');
                } else {
                    this.candidateSort.col = col;
                    this.candidateSort.dir = (col === 'move' ? 'asc' : 'desc');
                }
                this.updateSortHeaderUI();
                const curr = this.history[this.currentStepIndex];
                if (curr) {
                    const isUserTurn = (this.activeColor === 'white' && (curr.ply % 2 === 0)) ||
                                       (this.activeColor === 'black' && (curr.ply % 2 === 1));
                    this.renderCandidateTable(curr.fen, isUserTurn);
                }
            });
        };

        bindSortHeader('thSortMove', 'move');
        bindSortHeader('thSortMaster', 'master');
        bindSortHeader('thSortMyStats', 'threat');
        bindSortHeader('thSortEval', 'eval');

        // Explored Lines Tree, Recent Games & Coverage Gaps Tab switching
        const btnTabTree = document.getElementById('btnTabRepLinesTree');
        const btnTabGames = document.getElementById('btnTabRepGames');
        const btnTabGaps = document.getElementById('btnTabRepCoverageGaps');
        if (btnTabTree) btnTabTree.addEventListener('click', () => this.switchSidebarTab('tree'));
        if (btnTabGames) btnTabGames.addEventListener('click', () => this.switchSidebarTab('games'));
        if (btnTabGaps) btnTabGaps.addEventListener('click', () => this.switchSidebarTab('gaps'));

        // Recent Games filters and actions
        const filterBtns = [
            { id: 'btnFilterGamesAll', filter: 'all' },
            { id: 'btnFilterGamesWhite', filter: 'white' },
            { id: 'btnFilterGamesBlack', filter: 'black' }
        ];
        filterBtns.forEach(({ id, filter }) => {
            const btn = document.getElementById(id);
            if (btn) {
                btn.addEventListener('click', () => {
                    filterBtns.forEach(b => {
                        const el = document.getElementById(b.id);
                        if (el) el.classList.toggle('active', b.filter === filter);
                    });
                    this.gamesColorFilter = filter;
                    this.loadRecentGames();
                });
            }
        });

        const speedFilterBtns = [
            { id: 'btnFilterSpeedAll', filter: 'all' },
            { id: 'btnFilterSpeedRapid', filter: 'rapid' },
            { id: 'btnFilterSpeedBlitz', filter: 'blitz' },
            { id: 'btnFilterSpeedBullet', filter: 'bullet' }
        ];
        speedFilterBtns.forEach(({ id, filter }) => {
            const btn = document.getElementById(id);
            if (btn) {
                btn.addEventListener('click', () => {
                    speedFilterBtns.forEach(b => {
                        const el = document.getElementById(b.id);
                        if (el) el.classList.toggle('active', b.filter === filter);
                    });
                    this.gamesSpeedFilter = filter;
                    this.loadRecentGames();
                });
            }
        });

        const btnSyncGames = document.getElementById('btnSyncRecentGames');
        if (btnSyncGames) btnSyncGames.addEventListener('click', () => this.syncRecentGames());

        const searchInput = document.getElementById('repGamesSearchInput');
        if (searchInput) {
            let debounce = null;
            searchInput.addEventListener('input', (e) => {
                clearTimeout(debounce);
                debounce = setTimeout(() => {
                    this.gamesSearchQuery = e.target.value;
                    this.loadRecentGames();
                }, 250);
            });
        }

        // Quick Whole-Line actions bar buttons
        const btnAddLine = document.getElementById('btnRepAddWholeLine');
        if (btnAddLine) btnAddLine.addEventListener('click', () => this.importToRepertoire(this.activeColor));

        const btnViewTree = document.getElementById('btnRepViewTree');
        if (btnViewTree) btnViewTree.addEventListener('click', () => {
            this.switchSidebarTab('tree');
            document.getElementById('repVariationsList')?.scrollIntoView({ behavior: 'smooth' });
        });

        const btnClearTree = document.getElementById('btnRepClearTree');
        if (btnClearTree) btnClearTree.addEventListener('click', () => this.clearSessionTree());

        // Tree header action buttons
        const btnImportAll = document.getElementById('btnRepImportAll');
        if (btnImportAll) btnImportAll.addEventListener('click', () => this.importAllLinesToRepertoire(this.activeColor));

        const btnClearAllTree = document.getElementById('btnRepClearAllTree');
        if (btnClearAllTree) btnClearAllTree.addEventListener('click', () => this.clearSessionTree());

        this.isInitialized = true;
    }

    updateSortHeaderUI() {
        const totalAnalyzed = this.treeData?.game_gaps?.total_games_analyzed || this.currentCandidateData?.user_games_total || 0;
        const myStatsLabel = totalAnalyzed > 0 ? `My Stats (${totalAnalyzed} Games)` : 'My Stats';

        const headers = [
            { id: 'thSortMove', col: 'move', label: 'Move' },
            { id: 'thSortMaster', col: 'master', label: 'Master (2000+)' },
            { id: 'thSortMyStats', col: 'threat', label: myStatsLabel },
            { id: 'thSortEval', col: 'eval', label: 'Eval' }
        ];

        headers.forEach(h => {
            const el = document.getElementById(h.id);
            if (!el) return;
            if (this.candidateSort.col === h.col) {
                el.classList.add('active-sort');
                const arrow = this.candidateSort.dir === 'desc' ? '▾' : '▴';
                // SEC-J: `h.label` embeds `total_games_analyzed` / `user_games_total` from a JSON
                // payload, so it is a text node, not markup (the previous innerHTML interpolated it).
                // `replaceChildren` (not `appendChild`) so a re-sort cannot accumulate labels.
                const arrowEl = document.createElement('span');
                arrowEl.className = 'sort-arrow';
                arrowEl.textContent = arrow;
                el.replaceChildren(document.createTextNode(`${h.label} `), arrowEl);
            } else {
                el.classList.remove('active-sort');
                el.textContent = h.label;
            }
        });
    }

    sortCandidateMoves(moves) {
        if (!moves || !moves.length) return [];
        const sorted = [...moves];
        const { col, dir } = this.candidateSort;
        const mul = (dir === 'desc' ? -1 : 1);

        sorted.sort((a, b) => {
            if (col === 'threat') {
                const sA = a.my_stats?.threat_score || 0;
                const sB = b.my_stats?.threat_score || 0;
                if (sA !== sB) return mul * (sA - sB);
                return (b.popularity_pct || 0) - (a.popularity_pct || 0);
            }
            if (col === 'master') {
                const gA = a.games || 0;
                const gB = b.games || 0;
                if (gA !== gB) return mul * (gA - gB);
                return (b.popularity_pct || 0) - (a.popularity_pct || 0);
            }
            if (col === 'eval') {
                const eA = (a.eval !== undefined && a.eval !== null) ? a.eval : (dir === 'desc' ? -99999 : 99999);
                const eB = (b.eval !== undefined && b.eval !== null) ? b.eval : (dir === 'desc' ? -99999 : 99999);
                return mul * (eA - eB);
            }
            if (col === 'move') {
                return mul * a.san.localeCompare(b.san);
            }
            return 0;
        });
        return sorted;
    }

    /**
     * The eval bar fill direction is tied to the board orientation via the `.flipped` class.
     * The label's own `.white-lead` / `.black-lead` classes are colour-coupled, so the CSS also
     * needs a flipped variant (see style.css) - this is the single place that keeps the two in
     * sync (CODE3-21).
     */
    applyEvalBarOrientation(orientation) {
        const evalWrapper = document.getElementById('repEvalBarWrapper');
        if (evalWrapper) {
            evalWrapper.classList.toggle('flipped', orientation === 'black');
        }
    }

    async seedDefaults() {
        // NOTE (AGENTS.md 9.3/9.4): the endpoint requires ?force=true, which would overwrite the
        // user's prepared 3.e5 Anti-French / 3.f3 Caro-Kann lines with generic ones. We therefore
        // deliberately do NOT pass force, and we surface the backend's "skipped" answer instead of
        // dereferencing a null body (CODE3-22).
        try {
            const res = await fetch('/api/repertoire/seed', { method: 'POST' });
            const data = await res.json().catch(() => null);
            if (!res.ok) {
                this.notify(`Seeding failed: HTTP ${res.status}`, '#ef4444');
                return;
            }
            if (data && data.status === 'ok') {
                this.notify('Default repertoires seeded successfully!', '#4ade80');
                await this.loadRepertoire(this.activeColor);
                return;
            }
            if (data && data.status === 'skipped') {
                this.notify('Seeding skipped — your repertoire already has moves, so it was left untouched.', '#f59e0b');
                return;
            }
            this.notify('Seeding returned an unexpected response; nothing was changed.', '#f59e0b');
        } catch (e) {
            console.error('Error seeding defaults:', e);
            this.notify(`Error seeding defaults: ${e.message}`, '#ef4444');
        }
    }

    async confirmAndResetRepertoire() {
        const confirmed = confirm(
            "⚠️ WARNING: This will permanently delete ALL moves from your White and Black repertoire database.\n\n" +
            "Your downloaded games and analyzed blunders will remain safe, but all custom repertoire lines will be wiped.\n\n" +
            "Are you sure you want to completely reset your repertoire database?"
        );
        if (confirmed) {
            await this.resetRepertoire();
        }
    }

    async resetRepertoire() {
        try {
            const res = await fetch('/api/repertoire/clear', { method: 'POST' });
            const data = await res.json();
            if (data.status === 'ok') {
                alert(`Repertoire database has been completely reset! (${data.deleted_count || 0} moves removed)`);
                if (this.sessionTrees) {
                    if (this.sessionTrees.white) this.sessionTrees.white.reset();
                    if (this.sessionTrees.black) this.sessionTrees.black.reset();
                }
                this.updateActiveLineCounts();
                this.renderTreeLines();
                this.jumpToStart();
                await this.loadRepertoire(this.activeColor);
                if (window.repertoireTrainer && typeof window.repertoireTrainer.startNewDrill === 'function') {
                    window.repertoireTrainer.currentDrill = null;
                }
            } else {
                alert(`Failed to reset repertoire: ${data.detail || 'Unknown error'}`);
            }
        } catch (e) {
            console.error('Error resetting repertoire database:', e);
            alert(`Error resetting repertoire: ${e.message}`);
        }
    }

    initImportModal() {
        const dialog = document.getElementById('importRepertoireModal');
        const btnOpen = document.getElementById('btnOpenImportModal');
        const btnClose = document.getElementById('btnCloseImportModal');
        const btnCancel = document.getElementById('btnCancelImportModal');
        const btnSubmit = document.getElementById('btnSubmitImportModal');
        const dropZone = document.getElementById('importPgnDropZone');
        const fileInput = document.getElementById('importFileInput');
        const pgnText = document.getElementById('importPgnText');
        const splitCheck = document.getElementById('importSplitChaptersCheck');
        const singleWrap = document.getElementById('importSingleStudyWrap');

        if (btnOpen) {
            btnOpen.addEventListener('click', () => this.openImportModal());
        }
        if (btnClose) {
            btnClose.addEventListener('click', () => this.closeImportModal());
        }
        if (btnCancel) {
            btnCancel.addEventListener('click', () => this.closeImportModal());
        }

        const repButtons = [
            { el: document.getElementById('importRepAuto'), val: 'auto' },
            { el: document.getElementById('importRepWhite'), val: 'white' },
            { el: document.getElementById('importRepBlack'), val: 'black' }
        ];
        this.importColor = 'auto';
        repButtons.forEach(({ el, val }) => {
            if (el) {
                el.addEventListener('click', () => {
                    repButtons.forEach(b => b.el && b.el.classList.remove('active'));
                    el.classList.add('active');
                    this.importColor = val;
                });
            }
        });

        if (splitCheck && singleWrap) {
            splitCheck.addEventListener('change', () => {
                singleWrap.style.display = splitCheck.checked ? 'none' : 'block';
            });
        }

        if (dropZone && fileInput) {
            dropZone.addEventListener('click', () => fileInput.click());
            dropZone.addEventListener('dragover', (e) => {
                e.preventDefault();
                dropZone.style.borderColor = '#38bdf8';
                dropZone.style.background = 'rgba(56, 189, 248, 0.1)';
            });
            dropZone.addEventListener('dragleave', () => {
                dropZone.style.borderColor = '#475569';
                dropZone.style.background = 'rgba(15, 23, 42, 0.6)';
            });
            dropZone.addEventListener('drop', (e) => {
                e.preventDefault();
                dropZone.style.borderColor = '#475569';
                dropZone.style.background = 'rgba(15, 23, 42, 0.6)';
                if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
                    this.handlePgnFile(e.dataTransfer.files[0]);
                }
            });
            fileInput.addEventListener('change', (e) => {
                if (e.target.files && e.target.files.length > 0) {
                    this.handlePgnFile(e.target.files[0]);
                }
            });
        }

        if (pgnText) {
            let previewDebounce = null;
            pgnText.addEventListener('input', () => {
                clearTimeout(previewDebounce);
                previewDebounce = setTimeout(() => this.fetchPgnPreview(), 400);
            });
        }

        if (btnSubmit) {
            btnSubmit.addEventListener('click', () => this.submitImportModal());
        }
    }

    openImportModal() {
        const dialog = document.getElementById('importRepertoireModal');
        if (!dialog) return;

        const pgnText = document.getElementById('importPgnText');
        if (pgnText) pgnText.value = '';
        const fileInput = document.getElementById('importFileInput');
        if (fileInput) fileInput.value = '';
        const errEl = document.getElementById('importModalError');
        if (errEl) { errEl.textContent = ''; errEl.style.display = 'none'; }
        const previewCard = document.getElementById('importPreviewCard');
        if (previewCard) previewCard.style.display = 'none';
        const btnSubmit = document.getElementById('btnSubmitImportModal');
        if (btnSubmit) { btnSubmit.disabled = true; btnSubmit.textContent = 'Start Import'; }

        this.importColor = 'auto';
        ['importRepAuto', 'importRepWhite', 'importRepBlack'].forEach(id => {
            const btn = document.getElementById(id);
            if (btn) btn.classList.toggle('active', id === 'importRepAuto');
        });

        dialog.showModal();
    }

    closeImportModal() {
        const dialog = document.getElementById('importRepertoireModal');
        if (dialog && dialog.open) {
            dialog.close();
        }
    }

    handlePgnFile(file) {
        if (!file) return;
        const reader = new FileReader();
        reader.onload = (e) => {
            const text = e.target.result;
            const pgnText = document.getElementById('importPgnText');
            if (pgnText) {
                pgnText.value = text;
            }
            this.fetchPgnPreview();
        };
        reader.readAsText(file);
    }

    async fetchPgnPreview() {
        const pgnText = document.getElementById('importPgnText');
        const text = (pgnText ? pgnText.value : '').trim();
        const previewCard = document.getElementById('importPreviewCard');
        const btnSubmit = document.getElementById('btnSubmitImportModal');
        const errEl = document.getElementById('importModalError');

        if (!text) {
            if (previewCard) previewCard.style.display = 'none';
            if (btnSubmit) btnSubmit.disabled = true;
            if (errEl) { errEl.textContent = ''; errEl.style.display = 'none'; }
            return;
        }

        try {
            const res = await fetch('/api/repertoire/preview_pgn', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ pgn_text: text })
            });
            const data = await res.json();
            if (!res.ok || data.status !== 'success') {
                if (errEl) {
                    errEl.textContent = data.detail || 'Could not parse PGN preview.';
                    errEl.style.display = 'block';
                }
                if (previewCard) previewCard.style.display = 'none';
                if (btnSubmit) btnSubmit.disabled = true;
                return;
            }

            if (errEl) { errEl.textContent = ''; errEl.style.display = 'none'; }
            if (previewCard) {
                previewCard.style.display = 'block';
                const badge = document.getElementById('previewDetectedBadge');
                if (badge) {
                    badge.textContent = data.detected_color === 'white' ? '⚪ White Repertoire' : '⚫ Black Repertoire';
                    badge.style.color = data.detected_color === 'white' ? '#f8fafc' : '#94a3b8';
                }
                const totalPlies = document.getElementById('previewTotalPlies');
                if (totalPlies) totalPlies.textContent = String(data.total_moves || 0);
                const userPlies = document.getElementById('previewUserPlies');
                if (userPlies) userPlies.textContent = String(data.user_moves || 0);
                const oppPlies = document.getElementById('previewOppPlies');
                if (oppPlies) oppPlies.textContent = String(data.opponent_moves || 0);

                const chaptersList = document.getElementById('previewChaptersList');
                if (chaptersList) {
                    if (data.chapters && data.chapters.length > 1) {
                        const chapterNames = data.chapters.slice(0, 4).map(c => c.title).join(', ');
                        const more = data.chapters.length > 4 ? ` (+${data.chapters.length - 4} more)` : '';
                        chaptersList.textContent = `Chapters (${data.game_count}): ${chapterNames}${more}`;
                    } else if (data.chapters && data.chapters.length === 1) {
                        chaptersList.textContent = `Chapter: ${data.chapters[0].title} (${data.total_moves} plies)`;
                    } else {
                        chaptersList.textContent = '';
                    }
                }
            }
            if (btnSubmit) btnSubmit.disabled = false;
        } catch (e) {
            console.error('Error fetching PGN preview:', e);
            if (errEl) {
                errEl.textContent = `Preview error: ${e.message}`;
                errEl.style.display = 'block';
            }
            if (previewCard) previewCard.style.display = 'none';
            if (btnSubmit) btnSubmit.disabled = true;
        }
    }

    async submitImportModal() {
        const pgnText = document.getElementById('importPgnText');
        const text = (pgnText ? pgnText.value : '').trim();
        if (!text) return;

        const splitCheck = document.getElementById('importSplitChaptersCheck');
        const studyInput = document.getElementById('importStudyNameInput');
        const cleanWipeCheck = document.getElementById('importCleanWipeCheck');
        const conflictRadios = document.querySelectorAll('input[name="importConflictMode"]');
        let conflictMode = 'replace';
        conflictRadios.forEach(r => { if (r.checked) conflictMode = r.value; });

        const isClean = cleanWipeCheck ? cleanWipeCheck.checked : false;
        if (isClean) {
            const confirmed = confirm(
                "⚠️ CONFIRM WIPE:\n\nYou selected 'Clean import: wipe existing repertoire for this color'.\n" +
                "This will permanently delete your existing moves for this color before importing.\n\n" +
                "Are you sure you want to continue?"
            );
            if (!confirmed) return;
        }

        const btnSubmit = document.getElementById('btnSubmitImportModal');
        const errEl = document.getElementById('importModalError');
        if (btnSubmit) {
            btnSubmit.disabled = true;
            btnSubmit.textContent = 'Importing moves...';
        }
        if (errEl) { errEl.textContent = ''; errEl.style.display = 'none'; }

        try {
            const res = await fetch('/api/repertoire/import_pgn', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    pgn_text: text,
                    repertoire: this.importColor || 'auto',
                    study_name: studyInput ? studyInput.value : '',
                    split_chapters_as_studies: splitCheck ? splitCheck.checked : false,
                    replace_conflicts: conflictMode === 'replace',
                    clean_repertoire: isClean
                })
            });
            const data = await res.json();
            if (!res.ok || data.status !== 'success') {
                throw new Error(data.detail || 'Repertoire import failed.');
            }

            this.closeImportModal();
            if (window.showToast) {
                window.showToast(data.message || 'Repertoire imported successfully!', '#4ade80', 5000);
            }

            const targetRep = data.repertoire;
            if (targetRep && targetRep !== this.activeColor) {
                this.switchColor(targetRep);
            } else {
                await this.loadStudies();
                await this.loadRepertoire(this.activeColor);
            }

            if (window.repertoireTrainer && typeof window.repertoireTrainer.startNewDrill === 'function') {
                window.repertoireTrainer.currentDrill = null;
            }
        } catch (e) {
            console.error('Import error:', e);
            if (errEl) {
                errEl.textContent = e.message;
                errEl.style.display = 'block';
            }
            if (btnSubmit) {
                btnSubmit.disabled = false;
                btnSubmit.textContent = 'Start Import';
            }
        }
    }

    /**
     * Cancels BOTH independent sync streams (CODE3-04). They used to share one
     * `syncAbortController` slot whose unconditional `finally = null` disarmed the cancel
     * button; each stream now owns its own handle and only clears it after an identity check.
     */
    cancelSync() {
        if (this.resyncAbortController) {
            this.resyncAbortController.abort();
            this.resyncAbortController = null;
        }
        if (this.recentSyncAbortController) {
            this.recentSyncAbortController.abort();
            this.recentSyncAbortController = null;
        }
        const container = document.getElementById('syncProgressContainer');
        if (container) {
            container.style.display = 'none';
        }
        const btnResync = document.getElementById('btnResyncGames');
        if (btnResync) {
            btnResync.disabled = false;
            btnResync.innerHTML = '🔄 Refresh All Games';
        }
        const btnSyncRecent = document.getElementById('btnSyncRecentGames');
        if (btnSyncRecent) {
            btnSyncRecent.disabled = false;
            btnSyncRecent.textContent = '🔄 Sync Latest';
        }
    }

    async resyncGames() {
        const resyncKey = 'resyncGames';
        if (!this.beginWrite(resyncKey)) return;   // CODE3-12 latch
        const btnResync = document.getElementById('btnResyncGames');
        const origHtml = btnResync ? btnResync.innerHTML : '🔄 Refresh All Games';
        if (btnResync) {
            btnResync.disabled = true;
            btnResync.innerHTML = '⏳ Scanning Games...';
        }

        const container = document.getElementById('syncProgressContainer');
        const pulseDot = document.getElementById('syncPulseDot');
        const statusTitle = document.getElementById('syncStatusTitle');
        const statusDesc = document.getElementById('syncStatusDesc');
        const progressBar = document.getElementById('syncProgressBar');
        const pctBadge = document.getElementById('syncPctBadge');
        const countProcessed = document.getElementById('syncCountProcessed');
        const countTotal = document.getElementById('syncCountTotal');
        const countCached = document.getElementById('syncCountCached');
        const countNewBlunders = document.getElementById('syncCountNewBlunders');
        const blunderPill = document.getElementById('syncBlunderPill');
        const blunderCount = document.getElementById('syncBlunderCount');
        const activeOpening = document.getElementById('syncActiveOpening');

        // Reset UI elements to starting state
        if (container) container.style.display = 'flex';
        if (pulseDot) {
            pulseDot.className = 'sync-pulse-dot';
        }
        if (statusTitle) statusTitle.textContent = 'Connecting to Lichess...';
        if (statusDesc) statusDesc.textContent = 'Downloading recent games...';
        if (progressBar) {
            progressBar.value = 0;
            progressBar.setAttribute('aria-valuenow', 0);
        }
        if (pctBadge) {
            pctBadge.textContent = '0%';
            pctBadge.classList.remove('completed');
        }
        if (countProcessed) countProcessed.textContent = '0';
        if (countTotal) countTotal.textContent = '--';
        if (countCached) countCached.textContent = '0';
        if (countNewBlunders) countNewBlunders.textContent = '0';
        if (blunderPill) blunderPill.style.visibility = 'hidden';   // AGENTS.md 8.4: never display
        if (blunderCount) blunderCount.textContent = '0';
        if (activeOpening) activeOpening.textContent = 'Initializing...';

        if (this.resyncAbortController) {
            this.resyncAbortController.abort();
        }
        const abortCtrl = new AbortController();
        this.resyncAbortController = abortCtrl;

        try {
            const resp = await fetch('/api/repertoire/sync_games_stream', {
                signal: abortCtrl.signal
            });

            if (!resp.ok) {
                throw new Error(`Server returned HTTP ${resp.status}`);
            }

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let completedData = null;

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n\n');
                buffer = lines.pop() || '';

                for (const block of lines) {
                    // BUILDER-14: pick the `data:` line out of the block instead of
                    // assuming it is the block's only line.
                    const eventLine = block.split('\n').find(l => l.startsWith('data: '));
                    if (eventLine) {
                        try {
                            const data = JSON.parse(eventLine.slice(6));
                            if (data.stage === 'fetching') {
                                if (statusTitle) statusTitle.textContent = 'Connecting to Lichess...';
                                if (statusDesc) statusDesc.textContent = data.message || 'Fetching games...';
                            } else if (data.stage === 'analyzing_start') {
                                if (statusTitle) statusTitle.textContent = 'Analyzing Game Theory...';
                                if (statusDesc) statusDesc.textContent = data.message || 'Starting engine analysis...';
                                if (countTotal) countTotal.textContent = data.total;
                            } else if (data.stage === 'analyzing') {
                                const cur = data.current || 0;
                                const tot = data.total || 0;
                                const pct = data.percent !== undefined ? data.percent : (tot > 0 ? Math.round((cur / tot) * 100) : 0);

                                if (statusTitle) statusTitle.textContent = `Analyzing Game ${cur} of ${tot}`;
                                if (statusDesc) statusDesc.textContent = data.opening ? `${data.opening} ${data.eco ? '(' + data.eco + ')' : ''}` : 'Analyzing move theory...';
                                if (activeOpening) activeOpening.textContent = data.opening ? `${data.eco ? data.eco + ' • ' : ''}${data.opening}` : 'Standard Game';

                                if (progressBar) {
                                    progressBar.value = pct;
                                    progressBar.setAttribute('aria-valuenow', pct);
                                }
                                if (pctBadge) pctBadge.textContent = `${pct}%`;
                                if (countProcessed) countProcessed.textContent = cur;
                                if (countTotal) countTotal.textContent = tot;
                                if (countCached) countCached.textContent = data.already_analyzed || 0;
                                if (countNewBlunders) countNewBlunders.textContent = data.new_blunders || 0;

                                if (data.new_blunders > 0 && blunderPill) {
                                    blunderPill.style.visibility = 'visible';  // AGENTS.md 8.4
                                    if (blunderCount) blunderCount.textContent = data.new_blunders;
                                }
                            } else if (data.stage === 'completed') {
                                completedData = data;
                                if (statusTitle) statusTitle.textContent = 'Analysis Complete!';
                                if (statusDesc) statusDesc.textContent = data.message || `Scanned ${data.synced_games} games.`;
                                if (progressBar) {
                                    progressBar.value = 100;
                                    progressBar.setAttribute('aria-valuenow', 100);
                                }
                                if (pctBadge) {
                                    pctBadge.textContent = '100%';
                                    pctBadge.classList.add('completed');
                                }
                                if (pulseDot) pulseDot.className = 'sync-pulse-dot completed';
                                if (countProcessed) countProcessed.textContent = data.synced_games || 0;
                                if (countTotal) countTotal.textContent = data.synced_games || 0;
                                if (countCached) countCached.textContent = data.already_analyzed || 0;
                                if (countNewBlunders) countNewBlunders.textContent = data.new_blunders || 0;
                            } else if (data.stage === 'no_games') {
                                if (statusTitle) statusTitle.textContent = 'No Games Retrieved';
                                if (statusDesc) statusDesc.textContent = data.message || 'No games found or rate limit reached.';
                                if (pulseDot) pulseDot.className = 'sync-pulse-dot error';
                            } else if (data.stage === 'error') {
                                if (statusTitle) statusTitle.textContent = 'Sync Error';
                                if (statusDesc) statusDesc.textContent = data.message || 'Failed to sync games.';
                                if (pulseDot) pulseDot.className = 'sync-pulse-dot error';
                            }
                        } catch (e) {
                            console.warn('Sync chunk parse error:', e);
                        }
                    }
                }
            }

            if (completedData) {
                this.candidateCache.clear();
                await this.refreshTree();
                await this.updatePosition();

                // Auto-dismiss after 8 seconds, but never hide a *different* live stream's card
                setTimeout(() => {
                    const c = document.getElementById('syncProgressContainer');
                    if (c && !this.resyncAbortController && !this.recentSyncAbortController) {
                        c.style.display = 'none';
                    }
                }, 8000);
            }
        } catch (err) {
            if (err.name === 'AbortError') {
                return;
            }
            console.error('Error syncing games stream:', err);
            if (statusTitle) statusTitle.textContent = 'Sync Failed';
            if (statusDesc) statusDesc.textContent = err.message || 'Network error communicating with server.';
            if (pulseDot) pulseDot.className = 'sync-pulse-dot error';
            this.notify(`Game sync failed: ${err.message || 'network error'}`, '#ef4444');
        } finally {
            if (btnResync) {
                btnResync.disabled = false;
                btnResync.innerHTML = origHtml;
            }
            // Identity check: only the stream that owns this handle may clear it.
            if (this.resyncAbortController === abortCtrl) {
                this.resyncAbortController = null;
            }
            this.endWrite(resyncKey);
        }
    }

    async loadStudies() {
        try {
            const res = await fetch(`/api/repertoire/studies?repertoire=${this.activeColor}`);
            if (!res.ok) {
                throw new Error(`HTTP ${res.status}`);
            }
            const studies = await res.json();
            this.studiesList = Array.isArray(studies) ? studies : [];
            const select = document.getElementById('builderStudySelect');
            if (!select) return;

            const prevStudy = this.currentStudy || '';
            select.innerHTML = '<option value="">(All Studies)</option>';
            let hasPrev = false;
            this.studiesList.forEach(s => {
                const opt = document.createElement('option');
                opt.value = s.name;
                const moves = s.total_moves ? ` (${s.total_moves} moves)` : '';
                opt.textContent = `${s.name}${moves}`;
                if (s.name === prevStudy) hasPrev = true;
                select.appendChild(opt);
            });

            if (hasPrev) {
                select.value = prevStudy;
            } else {
                this.currentStudy = '';
                select.value = '';
            }
        } catch (e) {
            console.error('Error loading studies in builder:', e);
            this.notify(`Could not load studies: ${e.message}`, '#ef4444');
        }
    }

    async onStudyChange(studyName) {
        this.currentStudy = studyName ? studyName.trim() : '';
        await this.refreshTree();
    }

    async onNewStudyClick() {
        if (typeof window.promptStudySelection !== 'function') return;
        const name = await window.promptStudySelection({
            repertoire: this.activeColor,
            title: `Create New ${this.activeColor.charAt(0).toUpperCase() + this.activeColor.slice(1)} Study`,
            mode: 'new',
            confirmText: 'Create Study'
        });
        if (name) {
            this.currentStudy = name;
            await this.loadStudies();
            const select = document.getElementById('builderStudySelect');
            if (select) select.value = name;
            await this.refreshTree();
            this.showQuickNotice(`✓ Study "${name}" active`, '#4ade80');
        }
    }

    countDescendantsFromFen(startFen) {
        if (!this.treeData || !this.treeData.moves_by_parent || !startFen) return 0;
        const normStart = this.normalizeFen(startFen);
        const normStart3 = normStart.split(' ').slice(0, 3).join(' ');
        const queue = [normStart];
        const visitedFens = new Set([normStart, normStart3]);
        const seenMoves = new Set();
        const movesByParent = this.treeData.moves_by_parent;

        while (queue.length > 0) {
            const curr = queue.shift();
            const curr3 = curr.split(' ').slice(0, 3).join(' ');
            const children = movesByParent[curr] || movesByParent[curr3] || [];
            for (const child of children) {
                const moveKey = `${child.parent_fen || curr}:${child.move_uci || child.uci}`;
                if (seenMoves.has(moveKey)) continue;
                seenMoves.add(moveKey);

                const childFen = this.normalizeFen(child.fen || '');
                if (childFen) {
                    const childFen3 = childFen.split(' ').slice(0, 3).join(' ');
                    if (!visitedFens.has(childFen) && !visitedFens.has(childFen3)) {
                        visitedFens.add(childFen);
                        visitedFens.add(childFen3);
                        queue.push(childFen);
                    }
                }
            }
        }
        return seenMoves.size;
    }

    openGroupSubtreeModal() {
        const curr = this.history[this.currentStepIndex] || this.history[0];
        const currentFen = curr ? curr.fen : 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
        const descendantCount = this.countDescendantsFromFen(currentFen);
        const currentOpening = curr?.openingName || 'Current Variation';

        const modal = document.getElementById('groupSubtreeModal');
        const openingEl = document.getElementById('subtreeCurrentOpening');
        const countEl = document.getElementById('subtreeDescendantCount');
        const inputEl = document.getElementById('subtreeStudyNameInput');
        const errEl = document.getElementById('subtreeModalError');

        if (openingEl) openingEl.textContent = currentOpening;
        if (countEl) countEl.textContent = `${descendantCount} move${descendantCount === 1 ? '' : 's'}`;
        if (inputEl) {
            if (currentOpening && !currentOpening.includes('Repertoire') && currentOpening !== 'Standard Opening' && currentOpening !== 'Current Variation') {
                inputEl.value = currentOpening;
            } else {
                inputEl.value = '';
            }
        }
        if (errEl) {
            errEl.style.display = 'none';
            errEl.textContent = '';
        }

        if (modal) {
            if (typeof modal.showModal === 'function') {
                modal.showModal();
            } else {
                modal.style.display = 'block';
            }
        }
    }

    closeGroupSubtreeModal() {
        const modal = document.getElementById('groupSubtreeModal');
        if (modal) {
            if (typeof modal.close === 'function') {
                modal.close();
            } else {
                modal.style.display = 'none';
            }
        }
    }

    async submitGroupSubtree() {
        const inputEl = document.getElementById('subtreeStudyNameInput');
        const errEl = document.getElementById('subtreeModalError');
        const includePreludeCheck = document.getElementById('subtreeIncludePreludeCheck');
        const btnConfirm = document.getElementById('btnConfirmGroupSubtree');

        const studyName = inputEl ? inputEl.value.trim() : '';
        if (!studyName) {
            if (errEl) {
                errEl.textContent = 'Please enter a study name.';
                errEl.style.display = 'block';
            }
            return;
        }

        const curr = this.history[this.currentStepIndex] || this.history[0];
        const startFen = curr ? curr.fen : 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
        const pathUci = this.history.slice(1, this.currentStepIndex + 1).map(h => h.uci).filter(Boolean);
        const includePrelude = includePreludeCheck ? includePreludeCheck.checked : true;

        if (btnConfirm) {
            btnConfirm.disabled = true;
            btnConfirm.textContent = 'Grouping...';
        }

        try {
            const res = await fetch('/api/repertoire/studies/group_subtree', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repertoire: this.activeColor,
                    start_fen: startFen,
                    study_name: studyName,
                    include_path_from_root: includePrelude,
                    path_uci: pathUci
                })
            });
            const data = await res.json();
            if (!res.ok || data.status !== 'success') {
                throw new Error(data.detail || 'Failed to group subtree.');
            }

            this.closeGroupSubtreeModal();

            // Set current study to newly created/grouped study
            this.currentStudy = studyName;
            await this.loadStudies();
            const select = document.getElementById('builderStudySelect');
            if (select) select.value = studyName;

            await this.refreshTree();

            const msg = `✓ Sub-block "${studyName}" created with ${data.descendant_moves} subtree moves${data.prelude_moves ? ` (+${data.prelude_moves} prelude moves)` : ''}!`;
            if (window.showToast) {
                window.showToast(msg, '#4ade80', 5000);
            } else {
                this.notify(msg, '#4ade80');
            }
        } catch (e) {
            console.error('Group subtree error:', e);
            if (errEl) {
                errEl.textContent = e.message;
                errEl.style.display = 'block';
            }
        } finally {
            if (btnConfirm) {
                btnConfirm.disabled = false;
                btnConfirm.textContent = 'Create & Group Moves';
            }
        }
    }

    async switchColor(color) {
        if (this.activeColor === color && this.history.length > 0) return;
        this.activeColor = color;
        this.currentStudy = '';

        const btnWhite = document.getElementById('btnRepWhite');
        const btnBlack = document.getElementById('btnRepBlack');
        if (btnWhite) btnWhite.classList.toggle('active', color === 'white');
        if (btnBlack) btnBlack.classList.toggle('active', color === 'black');

        const colorLabel = document.getElementById('repAddLineColorLabel');
        if (colorLabel) colorLabel.textContent = color.charAt(0).toUpperCase() + color.slice(1);

        await this.loadRepertoire(color);
    }

    async loadRepertoire(color = 'white') {
        this.init();
        this.activeColor = color;
        this.activeReviewGame = null;
        this.activeReviewGap = null;
        this.showPatchedGaps = false;
        // CODE3-07: a colour switch must also invalidate any in-flight explorer request, otherwise
        // the previous colour's response passes the (position-only) staleness guard.
        if (this.candidateAbortController) {
            this.candidateAbortController.abort();
            this.candidateAbortController = null;
        }
        this.candidateGen++;
        const gameBanner = document.getElementById('gameReviewBanner');
        if (gameBanner) gameBanner.style.display = 'none';
        if (this.board) this.board.clearArrows();

        // Set board orientation to match trained color
        this.board.setOrientation(color);
        this.applyEvalBarOrientation(color);

        const colorLabel = document.getElementById('repAddLineColorLabel');
        if (colorLabel) colorLabel.textContent = color.charAt(0).toUpperCase() + color.slice(1);

        // Reset history to standard starting position
        const startFen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
        this.history = [{
            ply: 0,
            fen: startFen,
            san: 'Start',
            uci: null,
            openingName: color === 'white' ? 'White Opening Repertoire' : 'Black Opening Repertoire',
            eco: ''
        }];
        this.currentStepIndex = 0;

        // Kick off recent games count in parallel so the tab badge updates immediately
        this.loadRecentGamesCount();

        await this.loadStudies();
        await this.refreshTree();
        await this.updatePosition();
        this.updateActiveLineCounts();
        this.renderTreeLines();
    }

    async refreshTree() {
        try {
            const res = await fetch(`/api/repertoire/tree?color=${this.activeColor}&threshold=${this.gapThreshold}&min_games=${this.openingGamesThreshold}&include_patched=${this.showPatchedGaps}`);
            if (!res.ok) {
                // A non-2xx here used to be assigned to treeData as if valid, which silently
                // mislabels every "★ Your Move" / "✓ In Tree" badge in the candidate table.
                throw new Error(`HTTP ${res.status}`);
            }
            const data = await res.json();
            if (!data || typeof data !== 'object') {
                throw new Error('malformed tree payload');
            }
            this.treeData = data;
            this.renderCoverageAndGaps();
        } catch (e) {
            console.error('Error loading repertoire tree:', e);
            this.notify(`Could not refresh repertoire: ${e.message}`, '#ef4444');
        }
    }

    renderCoverageAndGaps() {
        if (!this.treeData) return;

        // Coverage Gauge
        const pctEl = document.getElementById('repCoveragePct');
        const fillEl = document.getElementById('repCoverageFill');

        const cov = this.treeData.coverage_pct || 0;
        if (pctEl) {
            pctEl.textContent = `${cov}%`;
            pctEl.title = `${cov}% of opponent reply weight covered`;
        }
        if (fillEl) {
            fillEl.style.width = `${cov}%`;
            if (cov >= 70) {
                fillEl.style.background = 'linear-gradient(90deg, #38bdf8, #22c55e)';
            } else if (cov >= 40) {
                fillEl.style.background = 'linear-gradient(90deg, #f59e0b, #38bdf8)';
            } else {
                fillEl.style.background = 'linear-gradient(90deg, #ef4444, #f59e0b)';
            }
        }

        this.updateSortHeaderUI();
        this.renderGapLists();

        // Update Recent Games count and load if active
        if (this.activeSidebarTab === 'games') {
            this.loadRecentGames();
        } else {
            // CODE3-05: the badge must describe the SAME query the list would use, otherwise a
            // filtered list ends up with an unfiltered badge. Sequenced + abortable.
            this.loadRecentGamesCount();
        }
    }

    /**
     * Loads only the `repGamesCount` badge for the sidebar tab. Uses the same colour/search
     * filters as `loadRecentGames()` and its own sequence token, so a stale response can never
     * overwrite a newer one.
     */
    async loadRecentGamesCount() {
        const seq = ++this.recentGamesSeq;
        if (this.recentCountAbort) this.recentCountAbort.abort();
        const abortCtrl = new AbortController();
        this.recentCountAbort = abortCtrl;
        const url = this._recentGamesUrl(1);
        try {
            const res = await fetch(url, { signal: abortCtrl.signal });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            if (seq !== this.recentGamesSeq) return;   // a newer request already answered
            const gamesCount = document.getElementById('repGamesCount');
            if (gamesCount) {
                const total = data.total !== undefined ? data.total : (data.games || []).length;
                gamesCount.textContent = total;
                gamesCount.title = this._recentGamesQueryLabel();
            }
        } catch (e) {
            if (e.name === 'AbortError') return;
            console.error('Error loading recent games count:', e);
        } finally {
            if (this.recentCountAbort === abortCtrl) this.recentCountAbort = null;
        }
    }

    _recentGamesQueryLabel() {
        const parts = [];
        if (this.gamesColorFilter && this.gamesColorFilter !== 'all') parts.push(`as ${this.gamesColorFilter}`);
        if (this.gamesSpeedFilter && this.gamesSpeedFilter !== 'all') parts.push(`(${this.gamesSpeedFilter})`);
        if (this.gamesSearchQuery && this.gamesSearchQuery.trim()) parts.push(`matching "${this.gamesSearchQuery.trim()}"`);
        return parts.length ? parts.join(' ') : 'all games';
    }

    _recentGamesUrl(limit) {
        let url = `/api/games/recent?limit=${limit}`;
        if (this.gamesColorFilter && this.gamesColorFilter !== 'all') {
            url += `&color=${encodeURIComponent(this.gamesColorFilter)}`;
        }
        if (this.gamesSpeedFilter && this.gamesSpeedFilter !== 'all') {
            url += `&speed=${encodeURIComponent(this.gamesSpeedFilter)}`;
        }
        const q = (this.gamesSearchQuery || '').trim();
        if (q) {
            url += `&search=${encodeURIComponent(q)}`;
        }
        return url;
    }

    async loadRecentGames() {
        const gamesList = document.getElementById('repGamesList');
        const gamesCount = document.getElementById('repGamesCount');
        if (!gamesList) return;

        // CODE3-05: sequence every call, abort the previous one, and never write a stale response.
        const seq = ++this.recentGamesSeq;
        if (this.recentGamesAbort) this.recentGamesAbort.abort();
        const abortCtrl = new AbortController();
        this.recentGamesAbort = abortCtrl;
        const url = this._recentGamesUrl(40);

        try {
            const res = await fetch(url, { signal: abortCtrl.signal });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            if (seq !== this.recentGamesSeq) return;
            const games = data.games || [];
            this.recentGames = games;

            if (gamesCount) {
                gamesCount.textContent = data.total !== undefined ? data.total : games.length;
                gamesCount.title = this._recentGamesQueryLabel();
            }

            gamesList.innerHTML = '';
            if (games.length === 0) {
                gamesList.innerHTML = `
                    <div style="text-align:center; padding:1.5rem 1rem; color:var(--text-muted); font-size:0.85rem;">
                        No games found${this.gamesColorFilter !== 'all' ? ` as ${this._escapeHtml(this.gamesColorFilter)}` : ''}${this.gamesSpeedFilter !== 'all' ? ` (${this._escapeHtml(this.gamesSpeedFilter)})` : ''}.<br>
                        Click <strong>🔄 Sync Latest</strong> above to pull games from Lichess.
                    </div>
                `;
                return;
            }

            games.forEach(g => {
                const item = document.createElement('div');
                const isCurrentReview = (this.activeReviewGame && this.activeReviewGame.id === g.id);
                item.className = 'game-card' + (isCurrentReview ? ' active-review' : '');
                item.innerHTML = this._gameCardHtml(g);

                const btnReview = item.querySelector('.btn-review-game');
                if (btnReview) {
                    btnReview.addEventListener('click', (e) => {
                        e.stopPropagation();
                        this.reviewGame(g);
                    });
                }

                const btnAdd = item.querySelector('.btn-quick-add-opening');
                if (btnAdd) {
                    // AGENTS.md 8.4 + CODE3-11/12: fixed-width button (CSS), disabled for the whole
                    // request, always restored in `finally` (it used to stay permanently inert).
                    btnAdd.addEventListener('click', async (e) => {
                        e.stopPropagation();
                        const key = `importGameOpening:${g.id}`;
                        if (!this.beginWrite(key)) return;
                        const originalLabel = btnAdd.textContent;
                        btnAdd.disabled = true;
                        btnAdd.textContent = 'Adding...';
                        try {
                            const ok = await this.importGameOpening(g.id, g.user_color);
                            btnAdd.textContent = ok ? '✓ Added!' : 'Failed';
                            btnAdd.classList.toggle('added', ok);
                            if (ok) {
                                btnAdd.disabled = true;   // already imported; stay inert but visibly so
                            } else {
                                btnAdd.disabled = false;
                                setTimeout(() => {
                                    if (btnAdd.isConnected) btnAdd.textContent = originalLabel;
                                }, 1800);
                            }
                        } finally {
                            this.endWrite(key);
                        }
                    });
                }

                gamesList.appendChild(item);
            });
        } catch (err) {
            if (err.name === 'AbortError') return;
            if (seq !== this.recentGamesSeq) return;
            console.error('Error loading recent games:', err);
            gamesList.innerHTML = `<div style="text-align:center; color:#f87171; padding:1rem; font-size:0.8rem;">Failed to load games.</div>`;
            this.notify(`Failed to load recent games: ${err.message}`, '#ef4444');
        } finally {
            if (this.recentGamesAbort === abortCtrl) this.recentGamesAbort = null;
        }
    }

    /**
     * Markup for ONE card in the "Recent Games" sidebar list. Extracted from `loadRecentGames()`
     * purely so it can be rendered (and audited) on its own — the produced markup is unchanged.
     *
     * SYNC-13: every field of `g` comes from `/api/games/recent`, which the backend populates
     * from the Lichess API and the local `games` table, so all of it is untrusted. The one value
     * that reaches a `class=` attribute (`result`) goes through an allowlist, the one that reaches
     * an `href=` goes through a scheme allowlist, and every other field is HTML-escaped exactly
     * once - on the way in, never twice.
     */
    _gameCardHtml(g) {
        const game = g || {};
        const outcomePill = `<span class="game-outcome-pill ${this._outcomeClass(game.result)}">${this._escapeHtml(String(game.result == null ? '' : game.result).toUpperCase())} (${this._escapeHtml(game.score_text)})</span>`;
        const colorDot = game.user_color === 'black'
            ? '<span style="display:inline-block; width:9px; height:9px; border-radius:50%; background:#0f172a; border:1px solid #94a3b8;" title="Played as Black"></span>'
            : '<span style="display:inline-block; width:9px; height:9px; border-radius:50%; background:#fff; border:1px solid #94a3b8;" title="Played as White"></span>';
        const userColorAttr = this._escapeAttr(game.user_color === 'black' ? 'black' : 'white');

        return `
            <div style="flex:1; min-width:0;">
                <div style="display:flex; align-items:center; gap:0.4rem; flex-wrap:wrap; margin-bottom:0.2rem;">
                    ${outcomePill}
                    <span style="display:inline-flex; align-items:center; gap:0.25rem; font-size:0.8rem; font-weight:700; color:#fff;">
                        ${colorDot} vs ${this._escapeHtml(game.opponent_name)}
                    </span>
                    <span style="font-size:0.74rem; color:var(--text-muted);">(${this._escapeHtml(String(game.opponent_rating || '?'))})</span>
                    <span style="font-size:0.72rem; color:var(--text-secondary); margin-left:auto;">${this._escapeHtml(game.speed)} • ${this._escapeHtml(game.played_at_formatted)}</span>
                </div>
                <div style="font-size:0.78rem; color:var(--text-secondary); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                    ${game.eco ? `<strong style="color:#38bdf8;">${this._escapeHtml(game.eco)}</strong> • ` : ''}${this._escapeHtml(game.opening_name || 'Standard Opening')}
                </div>
            </div>
            <div style="display:flex; align-items:center; gap:0.35rem; flex-shrink:0;">
                <button class="btn-primary btn-review-game" style="font-size:0.74rem; padding:0.3rem 0.6rem; cursor:pointer;" title="Load game on board to explore opening and variations">
                    Review & Explore &rarr;
                </button>
                <button class="btn-secondary btn-quick-add-opening" style="font-size:0.72rem; padding:0.3rem 0.5rem; cursor:pointer;" title="Import recognized opening into your ${userColorAttr} repertoire">
                    📥 Add Opening
                </button>
                <a href="${this._escapeAttr(this._safeHttpUrl(game.game_url))}" target="_blank" rel="noopener noreferrer" style="color:var(--text-muted); font-size:0.8rem; padding:0.2rem; text-decoration:none;" title="View on Lichess">↗</a>
            </div>
        `;
    }

    async syncRecentGames() {
        const syncGuardKey = 'syncRecentGames';
        if (!this.beginWrite(syncGuardKey)) return;   // CODE3-12 latch
        const btnSync = document.getElementById('btnSyncRecentGames');
        const origText = btnSync ? btnSync.textContent : '';
        if (btnSync) {
            btnSync.disabled = true;
            btnSync.textContent = '⏳ Syncing...';
        }

        const container = document.getElementById('syncProgressContainer');
        const pulseDot = document.getElementById('syncPulseDot');
        const statusTitle = document.getElementById('syncStatusTitle');
        const statusDesc = document.getElementById('syncStatusDesc');
        const progressBar = document.getElementById('syncProgressBar');
        const pctBadge = document.getElementById('syncPctBadge');
        const countProcessed = document.getElementById('syncCountProcessed');
        const countTotal = document.getElementById('syncCountTotal');
        const countCached = document.getElementById('syncCountCached');
        const activeOpening = document.getElementById('syncActiveOpening');

        // Reset progress UI elements to initial state
        if (container) container.style.display = 'flex';
        if (pulseDot) pulseDot.className = 'sync-pulse-dot';
        if (statusTitle) statusTitle.textContent = 'Syncing Lichess Games...';
        if (statusDesc) statusDesc.textContent = 'Connecting to Lichess API...';
        if (progressBar) {
            progressBar.value = 5;
            progressBar.setAttribute('aria-valuenow', 5);
        }
        if (pctBadge) {
            pctBadge.textContent = '5%';
            pctBadge.classList.remove('completed');
        }
        if (countProcessed) countProcessed.textContent = '0';
        if (countTotal) countTotal.textContent = '--';
        if (countCached) countCached.textContent = '0';
        if (activeOpening) activeOpening.textContent = 'Connecting to Lichess...';

        // AGENTS.md 8.1: the old implementation prepended a ~60px banner to the games list and
        // removed it 2.5s later, pushing all 40 cards down and snapping back (two layout shifts
        // per sync). Progress is reported through the fixed sync card + a floating toast instead.

        if (this.recentSyncAbortController) {
            this.recentSyncAbortController.abort();
        }
        const abortCtrl = new AbortController();
        this.recentSyncAbortController = abortCtrl;

        try {
            const resp = await fetch('/api/games/sync_recent_stream?max_games=30', {
                signal: abortCtrl.signal
            });

            if (!resp.ok) {
                throw new Error(`Server returned HTTP ${resp.status}`);
            }

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let completedData = null;

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n\n');
                buffer = lines.pop() || '';

                for (const block of lines) {
                    // BUILDER-14: pick the `data:` line out of the block instead of
                    // assuming it is the block's only line.
                    const eventLine = block.split('\n').find(l => l.startsWith('data: '));
                    if (!eventLine) continue;

                    try {
                        const data = JSON.parse(eventLine.slice(6));
                        const pct = data.percent !== undefined ? data.percent : 0;

                        if (progressBar) {
                            progressBar.value = pct;
                            progressBar.setAttribute('aria-valuenow', pct);
                        }
                        if (pctBadge) {
                            pctBadge.textContent = `${pct}%`;
                        }

                        if (data.stage === 'connecting') {
                            if (statusTitle) statusTitle.textContent = 'Connecting to Lichess...';
                            if (statusDesc) statusDesc.textContent = data.message || 'Connecting to Lichess API...';
                        } else if (data.stage === 'fetching') {
                            if (statusTitle) statusTitle.textContent = 'Downloading Games...';
                            if (statusDesc) statusDesc.textContent = data.message || 'Fetching games from Lichess...';
                        } else if (data.stage === 'processing_start') {
                            if (statusTitle) statusTitle.textContent = 'Processing Theory & Openings...';
                            if (statusDesc) statusDesc.textContent = data.message || 'Saving games to database...';
                            if (countTotal) countTotal.textContent = data.total;
                        } else if (data.stage === 'processing') {
                            const cur = data.current || 0;
                            const tot = data.total || 0;
                            if (statusTitle) statusTitle.textContent = `Syncing Game ${cur} of ${tot}`;
                            if (statusDesc) statusDesc.textContent = data.opening ? `${data.opening} ${data.eco ? '(' + data.eco + ')' : ''}` : (data.message || 'Processing move theory...');
                            if (activeOpening) activeOpening.textContent = data.opening ? `${data.eco ? data.eco + ' • ' : ''}${data.opening}` : (data.opponent ? `vs ${data.opponent}` : 'Processing...');
                            if (countProcessed) countProcessed.textContent = cur;
                            if (countTotal) countTotal.textContent = tot;
                            if (countCached) countCached.textContent = data.synced_games || 0;
                        } else if (data.stage === 'completed') {
                            completedData = data;
                            if (statusTitle) statusTitle.textContent = 'Sync Complete!';
                            if (statusDesc) statusDesc.textContent = data.message || `Successfully synced ${data.synced_games || 0} games.`;
                            if (activeOpening) activeOpening.textContent = 'All games up to date!';
                            if (pulseDot) pulseDot.className = 'sync-pulse-dot completed';
                            if (pctBadge) {
                                pctBadge.textContent = '100%';
                                pctBadge.classList.add('completed');
                            }
                            if (progressBar) {
                                progressBar.value = 100;
                                progressBar.setAttribute('aria-valuenow', 100);
                            }
                            this.notify(data.message || 'Sync complete!', '#38bdf8');
                        } else if (data.stage === 'error') {
                            if (pulseDot) pulseDot.className = 'sync-pulse-dot error';
                            if (statusTitle) statusTitle.textContent = 'Sync Error';
                            if (statusDesc) statusDesc.textContent = data.message || 'An error occurred during sync.';
                            this.showQuickNotice(data.message || 'Error syncing games from Lichess.', '#ef4444');
                        }
                    } catch (parseErr) {
                        console.error('Error parsing SSE event:', parseErr);
                    }
                }
            }

            if (completedData) {
                this.showQuickNotice(`✓ Synced ${completedData.synced_games || 0} games from Lichess!`, '#4ade80');
                await this.loadRecentGames();
            } else {
                await this.loadRecentGames();
            }
        } catch (err) {
            if (err.name === 'AbortError') {
                console.log('Sync was cancelled by user');
                return;
            }
            console.error('Error streaming recent games:', err);
            // Fallback to POST /api/games/sync_recent if SSE failed
            try {
                const fbRes = await fetch('/api/games/sync_recent?max_games=25', { method: 'POST' });
                if (!fbRes.ok) throw new Error(`HTTP ${fbRes.status}`);
                const fbData = await fbRes.json();
                if (fbData && fbData.status === 'success') {
                    this.showQuickNotice(`✓ Synced ${fbData.synced_games} games from Lichess!`, '#4ade80');
                    await this.loadRecentGames();
                } else {
                    this.showQuickNotice(fbData.message || 'No new games found.', '#94a3b8');
                }
            } catch (fbErr) {
                console.error('Fallback sync failed:', fbErr);
                if (pulseDot) pulseDot.className = 'sync-pulse-dot error';
                if (statusTitle) statusTitle.textContent = 'Sync Failed';
                if (statusDesc) statusDesc.textContent = 'Could not connect to Lichess API.';
                this.showQuickNotice('Error syncing games from Lichess.', '#ef4444');
            }
        } finally {
            if (btnSync) {
                btnSync.disabled = false;
                btnSync.textContent = origText || '🔄 Sync Latest';
            }
            // Identity check: never clobber the other stream's handle (CODE3-04).
            if (this.recentSyncAbortController === abortCtrl) {
                this.recentSyncAbortController = null;
            }
            this.endWrite(syncGuardKey);
            setTimeout(() => {
                const c = document.getElementById('syncProgressContainer');
                if (c && !this.resyncAbortController && !this.recentSyncAbortController) {
                    c.style.display = 'none';
                }
            }, 3500);
        }
    }

    async reviewGame(game) {
        if (!game) return;
        this.activeReviewGame = game;
        this.activeReviewGap = null; // keeps test requirement satisfied

        const movesList = (game.moves_san || '').trim().split(/\s+/).filter(san => san && !san.endsWith('.') && !/^\d+$/.test(san));
        if (movesList.length === 0 || typeof Chess === 'undefined') {
            console.warn('Cannot review game with no moves or Chess library missing');
            return;
        }

        // Set active color and board orientation to user's color in this game
        const targetColor = game.user_color === 'black' ? 'black' : 'white';
        this.activeColor = targetColor;
        const btnWhite = document.getElementById('btnRepWhite');
        const btnBlack = document.getElementById('btnRepBlack');
        if (btnWhite) btnWhite.classList.toggle('active', targetColor === 'white');
        if (btnBlack) btnBlack.classList.toggle('active', targetColor === 'black');
        const colorLabel = document.getElementById('repAddLineColorLabel');
        if (colorLabel) colorLabel.textContent = targetColor.charAt(0).toUpperCase() + targetColor.slice(1);

        this.board.setOrientation(targetColor);
        this.applyEvalBarOrientation(targetColor);

        const tempChess = new Chess();
        this.history = [{
            ply: 0,
            fen: tempChess.fen(),
            san: 'Start',
            uci: null,
            moveNumber: 0,
            isWhite: false,
            isTheory: true,
            openingName: game.opening_name || 'Standard Opening',
            eco: game.eco || ''
        }];

        const theoryPlies = game.opening_ply || 10;

        for (let i = 0; i < movesList.length; i++) {
            const san = movesList[i];
            const mRes = tempChess.move(san, { sloppy: true });
            if (!mRes) break;

            const ply = i + 1;
            const moveNum = Math.ceil(ply / 2);
            const isWhite = (ply % 2 === 1);
            const isTheory = (ply <= theoryPlies);

            this.history.push({
                ply: ply,
                fen: tempChess.fen(),
                san: mRes.san,
                uci: mRes.from + mRes.to + (mRes.promotion || ''),
                moveNumber: moveNum,
                isWhite: isWhite,
                isTheory: isTheory,
                openingName: game.opening_name || 'Game Move',
                eco: game.eco || ''
            });
        }

        // Reset and seed session line tree with this game's moves so it forms one coherent line
        this.lineTree.reset();
        this.ensureHistoryInLineTree(this.history.length - 1);

        // Jump to end of theoretical opening (or move 1 if game is short)
        const targetStep = Math.min(theoryPlies, this.history.length - 1);
        this.currentStepIndex = Math.max(0, targetStep);

        this.renderGameReviewBanner(game);
        await this.updatePosition();
    }

    renderGameReviewBanner(game) {
        const banner = document.getElementById('gameReviewBanner');
        if (!banner) return;

        if (!game) {
            banner.style.display = 'none';
            return;
        }

        const targetColorName = game.user_color === 'white' ? 'White' : 'Black';
        const theoryPlies = game.opening_ply || 0;
        const theoryMoveNum = Math.ceil(theoryPlies / 2);
        const curr = this.history[this.currentStepIndex] || {};

        banner.style.display = 'block';
        banner.innerHTML = this._gameReviewBannerHtml(game, targetColorName, theoryPlies, theoryMoveNum);

        const btnJump = document.getElementById('btnJumpToOpeningEnd');
        if (btnJump) {
            btnJump.addEventListener('click', () => {
                const targetIdx = Math.min(theoryPlies, this.history.length - 1);
                this.jumpToStep(targetIdx);
            });
        }

        const btnAdd = document.getElementById('btnAddGameToRep');
        if (btnAdd) {
            btnAdd.addEventListener('click', async () => {
                await this.importToRepertoire(game.user_color);
            });
        }

        const btnExit = document.getElementById('btnExitGameReview');
        if (btnExit) {
            btnExit.addEventListener('click', () => this.exitGameReview());
        }
    }

    /**
     * Markup for the "Reviewing vs …" header. Extracted from `renderGameReviewBanner()` purely
     * so it can be rendered (and audited) on its own — the produced markup is unchanged.
     *
     * SYNC-13: `game` is the same untrusted `/api/games/recent` row as the sidebar card, so
     * `result` (class), `game_url` (href) and every text field are handled exactly as in
     * `_gameCardHtml()`. `targetColorName` is derived from `user_color` in the caller and is only
     * escaped defensively; `theoryPlies` / `theoryMoveNum` are numbers and are coerced.
     */
    _gameReviewBannerHtml(game, targetColorName, theoryPlies, theoryMoveNum) {
        const g = game || {};
        const plies = this._num(theoryPlies, 0);
        const moveNum = this._num(theoryMoveNum, 0);
        const colorName = String(targetColorName == null ? '' : targetColorName);

        return `
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.5rem;">
                <div style="display:flex; align-items:center; gap:0.55rem; flex-wrap:wrap;">
                    <span class="game-outcome-pill ${this._outcomeClass(g.result)}">${this._escapeHtml(String(g.result == null ? '' : g.result).toUpperCase())} (${this._escapeHtml(g.score_text)})</span>
                    <span style="font-weight:700; color:#fff; font-size:0.92rem;">
                        Reviewing vs ${this._escapeHtml(g.opponent_name)} <span style="font-weight:400; color:var(--text-muted);">(${this._escapeHtml(String(g.opponent_rating || '?'))})</span>
                    </span>
                    <span style="color:var(--text-secondary); font-size:0.8rem;">
                        • ${g.eco ? `<strong style="color:#38bdf8;">${this._escapeHtml(g.eco)}</strong> ` : ''}${this._escapeHtml(g.opening_name || 'Opening')}
                    </span>
                    ${plies > 0 ? `<span style="font-size:0.74rem; background:rgba(56,189,248,0.15); color:#38bdf8; border:1px solid rgba(56,189,248,0.3); padding:0.1rem 0.4rem; border-radius:4px;">📖 Theory: ${plies} plies</span>` : ''}
                </div>
                <div style="display:flex; align-items:center; gap:0.4rem; flex-wrap:wrap;">
                    ${plies > 0 ? `
                        <button id="btnJumpToOpeningEnd" class="btn-secondary" style="font-size:0.75rem; padding:0.25rem 0.55rem;" title="Jump to Move ${moveNum} where recognized master theory ends">
                            📖 End of Theory (Move ${moveNum})
                        </button>
                    ` : ''}
                    <button id="btnAddGameToRep" class="btn-primary" style="font-size:0.75rem; padding:0.25rem 0.65rem;" title="Add moves from this game up to current position into ${this._escapeAttr(colorName)} Repertoire">
                        📥 Add Line &rarr; ${this._escapeHtml(colorName)} Rep
                    </button>
                    <a href="${this._escapeAttr(this._safeHttpUrl(g.game_url))}" target="_blank" rel="noopener noreferrer" class="btn-secondary" style="font-size:0.75rem; padding:0.25rem 0.55rem; text-decoration:none;" title="View game on Lichess">
                        ↗ Lichess
                    </a>
                    <button id="btnExitGameReview" class="btn-secondary" style="font-size:0.75rem; padding:0.25rem 0.55rem; color:#f87171;" title="Exit review mode and return to repertoire">
                        ✕ Exit Review
                    </button>
                </div>
            </div>
        `;
    }

    exitGameReview() {
        const banner = document.getElementById('gameReviewBanner');
        if (banner) banner.style.display = 'none';
        this.activeReviewGame = null;
        this.activeReviewGap = null;
        this.loadRepertoire(this.activeColor);
    }
    async importGameOpening(gameId, repertoire) {
        try {
            const res = await fetch('/api/repertoire/import_game_opening', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ game_id: gameId, repertoire: repertoire })
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            if (data && data.status === 'success') {
                this.notify(`✓ Imported ${data.opening_name || 'opening'} (${data.imported_moves} moves) into ${repertoire} repertoire!`, '#4ade80');
                await this.refreshTree();
                return true;
            }
            this.notify((data && data.message) || 'Failed to import opening.', '#f59e0b');
            return false;
        } catch (err) {
            console.error('Error importing game opening:', err);
            this.notify(`Error importing opening into repertoire: ${err.message}`, '#ef4444');
            return false;
        }
    }

    normalizeFen(fen) {
        return fen.split(' ').slice(0, 3).join(' ');
    }

    /**
     * Renders every part of the UI for `history[currentStepIndex]`.
     *
     * ORDERING INVARIANT (AGENTS.md / CODE3-02): the board model + FEN must be loaded FIRST and
     * `renderMoveToolsPanel()` LAST, because `loadFEN()` is a full "reset to this position" that
     * clears selection, last-move highlight and the arrow overlay. The old hand-rolled
     * "animate instead of re-render" fast path in `jumpToStep()` ran the arrows restore *before*
     * an animation callback that called `loadFEN()` 180ms later, wiping the arrows it had just
     * restored while `#repArrowsCount` still said "2 arrows".
     *
     * Everything up to and including `renderMoveToolsPanel()` runs synchronously; only the
     * candidate-move fetch is awaited, so callers may start a decorative animation immediately
     * after invoking this without awaiting it.
     */
    async updatePosition(options = {}) {
        const {
            animateFromTo = null,   // [from, to] -> purely decorative slide, no state mutation
        } = options;

        const curr = this.history[this.currentStepIndex];
        if (!curr) return;

        // 1. Board model first (clears arrows/selection/last-move)
        this.board.loadFEN(curr.fen);

        // 2. Opening title / ECO
        const titleEl = document.getElementById('repOpeningTitle');
        const ecoEl = document.getElementById('repOpeningEco');
        if (titleEl) {
            const t = curr.openingName || (this.activeColor === 'white' ? 'White Opening Repertoire' : 'Black Opening Repertoire');
            titleEl.textContent = t;
            titleEl.title = t;
        }
        if (ecoEl) {
            const e = curr.eco ? `${curr.eco} • Move ${Math.max(1, Math.ceil(curr.ply / 2))}` : (curr.ply === 0 ? 'Starting Position' : `Move ${Math.max(1, Math.ceil(curr.ply / 2))}`);
            ecoEl.textContent = e;
            ecoEl.title = e;
        }

        // 3. Breadcrumbs
        this.renderBreadcrumbs();

        // 4. Blunder banner (root-cause review, AGENTS.md 4.B/4.D)
        this.renderBlunderBanner(curr);

        // 5. Turn logic
        const turn = curr.fen.split(' ')[1]; // 'w' or 'b'
        const isWhiteTurn = (turn === 'w');
        const isUserTurn = (this.activeColor === 'white' && isWhiteTurn) || (this.activeColor === 'black' && !isWhiteTurn);

        const turnIndicator = document.getElementById('repTurnIndicator');
        if (turnIndicator) {
            if (isUserTurn) {
                turnIndicator.innerHTML = `<span style="color:#4ade80; font-weight:700;">★ Your Turn</span> — Pick your prepared response below`;
            } else {
                turnIndicator.innerHTML = `<span style="color:#38bdf8; font-weight:700;">♟ Opponent's Turn</span> — Prepare branches against frequent replies`;
            }
        }

        // 6. Real-time Stockfish progressive evaluation stream. The line it carries is
        //    dropped first, and the user's "clear the board" dismissal is re-armed, so a
        //    line from the previous position can never be left pointing at this one.
        this.engineLineDismissed = false;
        this.clearEngineLine();
        this.startEvalStream(curr.fen);

        // 7. Move Tools panel LAST (restores the saved arrows for this move)
        this.renderMoveToolsPanel(curr);

        // 8. Session line tree + explored-lines cards
        this.syncLineTreeNode();
        this.updateActiveLineCounts();
        this.renderTreeLines();

        // 9. Decoration only: slide a ghost from -> to. Mutates no state, so it can neither
        //    desynchronise the board (CODE3-13) nor clobber the restored arrows.
        if (animateFromTo && typeof this.board.animateGhostMove === 'function') {
            this.board.animateGhostMove(animateFromTo[0], animateFromTo[1]);
        }

        // 10. Name the opening of this position. Fire-and-forget on purpose: it is one
        //     in-memory lookup server-side, and the candidate-move table below is the
        //     only thing worth making a user wait for.
        this.refreshOpeningLine(curr);

        // 11. Fetch candidate moves (the only awaited step)
        await this.loadCandidateMoves(curr.fen, isUserTurn);
    }

    findMoveInTree(parentFen, uci) {
        if (!this.treeData || !this.treeData.moves_by_parent || !parentFen || !uci) return null;
        const norm = this.normalizeFen(parentFen);
        const candidates = this.treeData.moves_by_parent[parentFen] || this.treeData.moves_by_parent[norm] || [];
        return candidates.find(m => m.move_uci === uci) || null;
    }

    renderMoveToolsPanel(curr) {
        const sanEl = document.getElementById('repMoveToolSan');
        const typeEl = document.getElementById('repMoveToolType');
        const btnImportant = document.getElementById('btnRepToggleImportant');
        const btnDelete = document.getElementById('btnRepDeleteCurrentMove');
        const commentEl = document.getElementById('repMoveComment');
        const arrowsCountEl = document.getElementById('repArrowsCount');

        if (this.currentStepIndex === 0) {
            if (sanEl) sanEl.textContent = 'Starting Position';
            if (typeEl) typeEl.textContent = '(Root)';
            if (btnImportant) {
                btnImportant.style.visibility = 'hidden';
                btnImportant.disabled = true;
            }
            if (btnDelete) {
                btnDelete.style.visibility = 'hidden';
                btnDelete.disabled = true;
            }
            if (commentEl) {
                commentEl.value = '';
                commentEl.disabled = true;
            }
            if (arrowsCountEl) arrowsCountEl.textContent = '0 arrows';
            this.board.clearArrows();
            return;
        }

        if (commentEl) commentEl.disabled = false;
        if (sanEl) sanEl.textContent = curr.san || 'Move';
        const isUserTurnMove = (this.activeColor === 'white' && (curr.ply % 2 === 1)) ||
                               (this.activeColor === 'black' && (curr.ply % 2 === 0));
        if (typeEl) typeEl.textContent = isUserTurnMove ? '(Your Move)' : '(Opponent Reply)';

        const parent = this.history[this.currentStepIndex - 1];
        const parentFen = parent ? parent.fen : '';
        const repMove = this.findMoveInTree(parentFen, curr.uci);

        if (btnImportant) {
            btnImportant.style.visibility = 'visible';
            btnImportant.disabled = false;
            const isImportant = repMove ? Boolean(repMove.is_important) : false;
            btnImportant.classList.toggle('active-important', isImportant);
            btnImportant.innerHTML = isImportant ? '⭐ Priority Move' : '☆ Mark Important';
            btnImportant.style.borderColor = isImportant ? '#fbbf24' : '';
            btnImportant.style.color = isImportant ? '#fbbf24' : '';
            btnImportant.style.background = isImportant ? 'rgba(245, 158, 11, 0.18)' : '';
        }

        if (btnDelete) {
            btnDelete.style.visibility = repMove ? 'visible' : 'hidden';
            btnDelete.disabled = !repMove;
        }

        if (commentEl) {
            commentEl.value = (repMove && repMove.comment) ? repMove.comment : '';
        }

        // Load saved arrows. `setArrows` validates and returns the number it actually kept, so the
        // counter can never disagree with what is on the board (CODE3-02 / CODE3-14).
        if (repMove && repMove.arrows) {
            try {
                const parsed = JSON.parse(repMove.arrows);
                const kept = this.board.setArrows(parsed);
                if (arrowsCountEl) {
                    const text = `${kept} arrow${kept === 1 ? '' : 's'}`;
                    arrowsCountEl.textContent = text;
                    arrowsCountEl.title = text;
                }
            } catch (e) {
                this.board.clearArrows();
                if (arrowsCountEl) arrowsCountEl.textContent = '0 arrows';
            }
        } else {
            this.board.clearArrows();
            if (arrowsCountEl) arrowsCountEl.textContent = '0 arrows';
        }
    }

    async toggleCurrentMoveImportant() {
        if (this.currentStepIndex === 0) return;
        const curr = this.history[this.currentStepIndex];
        const parent = this.history[this.currentStepIndex - 1];
        if (!curr || !parent) return;

        const key = `toggleImportant:${parent.fen}:${curr.uci}`;
        if (!this.beginWrite(key)) return;   // CODE3-12

        const repMove = this.findMoveInTree(parent.fen, curr.uci);
        const newImportant = !(repMove && repMove.is_important);

        try {
            const res = await fetch('/api/repertoire/move_meta', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repertoire: this.activeColor,
                    parent_fen: parent.fen,
                    move_uci: curr.uci,
                    is_important: newImportant
                })
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            if (data && data.status === 'success') {
                if (window.chessAudio) window.chessAudio.playSuccess();
                if (this.lineTree && this.lineTree.currentNode && this.lineTree.currentNode.move) {
                    this.lineTree.currentNode.move.is_important = newImportant;
                }
                await this.refreshTree();
                this.renderMoveToolsPanel(curr);
                this.renderTreeLines();
            } else {
                this.notify((data && data.message) || 'Could not mark the move important.', '#f59e0b');
            }
        } catch (e) {
            console.error('Error toggling move importance:', e);
            this.notify(`Could not mark the move important: ${e.message}`, '#ef4444');
        } finally {
            this.endWrite(key);
        }
    }

    async deleteCurrentMoveAndLine() {
        if (this.currentStepIndex === 0) return;
        const curr = this.history[this.currentStepIndex];
        const parent = this.history[this.currentStepIndex - 1];
        if (!curr || !parent) return;

        const confirmed = confirm(
            `⚠️ Remove move ${curr.san} and all continuing lines from your ${this.activeColor.toUpperCase()} repertoire?`
        );
        if (!confirmed) return;

        const ok = await this.removeBranch(parent.fen, curr.uci);
        if (!ok) return;
        this.stepBack();
        this.scheduleOverlapCheck();
        this.renderTreeLines();
        this.notify(`✓ Removed ${curr.san} and its continuing lines`, '#4ade80');
    }

    /**
     * The board's arrow channel (right-click drag writes, `clearAnnotations()` deletes).
     *
     * The "these arrows are not saved" fact is reported on the **arrows count itself**,
     * not as a toast. A toast was wrong twice over: one per arrow gesture, so a
     * two-arrow drag nagged twice (as in the report that prompted this), and a floating
     * panel that never said *which* arrows it meant. The count is where the user is
     * already looking, the label is permanent rather than something that expires, and
     * marking it changes no geometry — the suffix lives inside the already-clamped
     * element, and there is no toast to shift anything.
     */
    async onArrowsChange(arrows) {
        const arrowsCountEl = document.getElementById('repArrowsCount');
        const unsaved = this._arrowsAreUnsaved(arrows);
        if (arrowsCountEl) {
            const n = arrows.length;
            arrowsCountEl.textContent = n
                ? `${n} arrow${n === 1 ? '' : 's'}${unsaved ? ' · not saved' : ''}`
                : '0 arrows';
            arrowsCountEl.classList.toggle('arrows-unsaved', unsaved);
            arrowsCountEl.title = unsaved
                ? 'This move is not in your repertoire yet, so these arrows last for this session only.'
                : `${n} arrow${n === 1 ? '' : 's'} saved with this move.`;
        }
        if (this.lineTree && this.lineTree.currentNode && this.lineTree.currentNode.move) {
            this.lineTree.currentNode.move.arrows = JSON.stringify(arrows);
        }
        if (this.currentStepIndex === 0) return;
        const curr = this.history[this.currentStepIndex];
        const parent = this.history[this.currentStepIndex - 1];
        if (!curr || !parent) return;

        const repMove = this.findMoveInTree(parent.fen, curr.uci);
        if (repMove) {
            try {
                const res = await fetch('/api/repertoire/move_meta', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        repertoire: this.activeColor,
                        parent_fen: parent.fen,
                        move_uci: curr.uci,
                        arrows: JSON.stringify(arrows)
                    })
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                repMove.arrows = JSON.stringify(arrows);
            } catch (e) {
                // A genuine *write* failure is still a toast, and is the only thing in
                // this method that raises one: the user asked for an action that did
                // not happen, and nothing in the panel can say so on its own. (The
                // "these arrows are session-only" case is not a failure — it is the
                // count's own label.)
                console.error('Error saving arrows to move:', e);
                this.notify(`Could not save arrows: ${e.message}`, '#ef4444');
            }
        }
    }

    /**
     * Are these arrows destined for a repertoire row that does not exist?
     *
     * Only meaningful away from the root: at ply 0 there is no move to attach them to.
     * This is the check that was a toast — an arrow drawn on a position that is not in
     * the repertoire used to be silently dropped, so the user lost their annotation.
     */
    _arrowsAreUnsaved(arrows) {
        if (!Array.isArray(arrows) || arrows.length === 0) return false;
        if (this.currentStepIndex === 0) return false;
        const curr = this.history[this.currentStepIndex];
        const parent = this.history[this.currentStepIndex - 1];
        if (!curr || !parent || !curr.uci) return false;
        return !this.findMoveInTree(parent.fen, curr.uci);
    }

    async saveMoveComment(commentText) {
        if (this.currentStepIndex === 0) return;
        const curr = this.history[this.currentStepIndex];
        const parent = this.history[this.currentStepIndex - 1];
        if (!curr || !parent) return;

        if (this.lineTree && this.lineTree.currentNode && this.lineTree.currentNode.move) {
            this.lineTree.currentNode.move.comment = commentText;
        }

        const repMove = this.findMoveInTree(parent.fen, curr.uci);
        if (!repMove) {
            const isUserTurn = (this.activeColor === 'white' && (curr.ply % 2 === 1)) ||
                               (this.activeColor === 'black' && (curr.ply % 2 === 0));
            await this.saveMove(parent.fen, { uci: curr.uci, san: curr.san }, isUserTurn);
        }

        try {
            const res = await fetch('/api/repertoire/move_meta', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repertoire: this.activeColor,
                    parent_fen: parent.fen,
                    move_uci: curr.uci,
                    comment: commentText
                })
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            if (data && data.status === 'success') {
                const saveStatus = document.getElementById('repCommentSaveStatus');
                if (saveStatus) {
                    saveStatus.style.opacity = '1';
                    setTimeout(() => { if (saveStatus) saveStatus.style.opacity = '0'; }, 2000);
                }
                const updatedMove = this.findMoveInTree(parent.fen, curr.uci);
                if (updatedMove) updatedMove.comment = commentText;
                this.renderTreeLines();
            } else {
                this.notify((data && data.message) || 'Could not save the move note.', '#f59e0b');
            }
        } catch (e) {
            console.error('Error saving move comment:', e);
            this.notify(`Could not save the move note: ${e.message}`, '#ef4444');
        }
    }

    renderEvalBar(scoreCp, mate, depth) {
        const fillEl = document.getElementById('repEvalBarFill');
        const labelEl = document.getElementById('repEvalBarLabel');
        if (!fillEl || !labelEl) return;

        let pct = 50;
        let text = '+0.0';
        let full = 'Equal position';

        if (mate !== null && mate !== undefined) {
            pct = (mate > 0) ? 100 : 0;
            text = (mate > 0 ? 'M' : '-M') + Math.abs(mate);
            full = (mate > 0 ? 'White mates in ' : 'Black mates in ') + Math.abs(mate);
        } else if (typeof scoreCp === 'number') {
            // Standard logistic mapping centered at 50%
            const val = 50 + 50 * (2 / (1 + Math.exp(-0.004 * scoreCp)) - 1);
            pct = Math.min(97, Math.max(3, val));
            const pawns = scoreCp / 100;
            text = (pawns > 0 ? '+' : '') + pawns.toFixed(1);
            full = (pawns > 0 ? 'White +' : 'Black ') + Math.abs(pawns).toFixed(2) + ' pawns';
        }

        fillEl.style.height = `${pct}%`;
        labelEl.textContent = text;
        labelEl.classList.remove('white-lead', 'black-lead', 'equal');

        if (pct >= 56) {
            labelEl.classList.add('white-lead');
        } else if (pct <= 44) {
            labelEl.classList.add('black-lead');
        } else {
            labelEl.classList.add('equal');
        }

        // CODE3-08/CODE3-20: `depth` is now actually used - the user gets the search depth and
        // the untruncated score on hover (AGENTS.md 8.2 requires a recoverable full string).
        const depthTxt = (typeof depth === 'number' && depth > 0) ? `Stockfish depth ${depth}` : 'Stockfish';
        const title = `${full} · ${depthTxt}`;
        labelEl.title = title;
        labelEl.setAttribute('aria-label', title);
        fillEl.parentElement && fillEl.parentElement.setAttribute('title', title);
        this.lastEval = { scoreCp, mate: mate === undefined ? null : mate, depth: depth || 0 };
    }

    // ================================================================== //
    //  AGENTS.md 4.H — the engine line and the opening description
    // ================================================================== //
    _readEngineLinePref() {
        try {
            const stored = localStorage.getItem('chess.engineLineOn');
            return stored === null ? true : stored === '1';
        } catch (e) {
            return true;   // a blocked/absent localStorage must not disable the feature
        }
    }

    /** §8.4: the button is only ever de-emphasised, never removed or hidden. */
    renderEngineLineToggle() {
        const btn = document.getElementById('btnRepEngineLine');
        if (!btn) return;
        btn.classList.toggle('active', this.engineLineOn);
        btn.setAttribute('aria-pressed', this.engineLineOn ? 'true' : 'false');
        const label = this.engineLineOn ? 'Engine Line: on' : 'Engine Line: off';
        btn.setAttribute('aria-label', label);
        btn.textContent = this.engineLineOn ? 'Engine Line' : 'Engine Line';
        btn.title = this.engineLineOn
            ? "Stockfish's best line is drawn on the board. Click to turn it off."
            : "Stockfish's best line is hidden. Click to turn it on.";
    }

    /**
     * A UCI principal variation becomes one arrow per ply: ply *i* is the pair
     * `(uci[0:2], uci[2:4])` that the UCI string already encodes. There is no need —
     * and it is a bug — to chain the previous ply's *origin* onto the current ply's
     * *destination*, which draws the line one half-move out of step (e2→c5 for
     * `e2e4 c7c5`).
     *
     * Bounded and shape-checked here as well as on the server, because this array
     * goes straight into SVG. Castling is one arrow (e1 → g1), which is what every
     * board draws.
     */
    pvToOverlayArrows(pv) {
        if (!Array.isArray(pv)) return [];
        const out = [];
        for (const step of pv) {
            const uci = (step && typeof step.uci === 'string') ? step.uci : '';
            if (!/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(uci)) break;
            out.push({ from: uci.slice(0, 2), to: uci.slice(2, 4) });
        }
        return out;
    }

    /**
     * The PV as readable notation: "1. d4 cxd4 2. c3", numbered from the position
     * actually on the board, so stepping back two plies renumbers the line instead
     * of showing a move number that belongs to a different position.
     */
    pvToSanText(pv, basePly) {
        if (!Array.isArray(pv)) return '';
        const base = (typeof basePly === 'number' && basePly >= 0) ? Math.floor(basePly) : 0;
        const parts = [];
        pv.forEach((step, i) => {
            const san = (step && typeof step.san === 'string') ? step.san.trim() : '';
            if (!san) return;
            const absPly = base + i;
            const moveNo = Math.floor(absPly / 2) + 1;
            if (absPly % 2 === 0) parts.push(`${moveNo}.`);
            else if (i === 0) parts.push(`${moveNo}...`);
            parts.push(san);
        });
        return parts.join(' ');
    }

    _currentBasePly() {
        const curr = this.history[this.currentStepIndex];
        const ply = curr && typeof curr.ply === 'number' ? curr.ply : this.currentStepIndex;
        return Number.isFinite(ply) && ply >= 0 ? ply : 0;
    }

    /**
     * The single place that paints `#repEngineLine`. `state` is one of
     * `'thinking' | 'error' | 'idle'`; the line itself is taken from `this.currentPv`,
     * which is the newest frame received for the position on the board.
     */
    renderEngineLine(state) {
        const strip = document.getElementById('repEngineLine');
        const text = document.getElementById('repEngineLineText');
        if (!strip || !text) return;

        let label;
        let live = false;
        if (!this.engineLineOn) {
            label = 'Engine line is off';
        } else if (state === 'error') {
            label = 'Engine unavailable — the evaluation bar above still works';
        } else if (this.currentPv && this.currentPv.pv && this.currentPv.pv.length) {
            const depth = this.currentPv.depth || 0;
            label = `${this.pvToSanText(this.currentPv.pv, this._currentBasePly())}` +
                    (depth > 0 ? ` · depth ${depth}` : '');
            live = true;
        } else if (state === 'thinking') {
            label = 'Thinking…';
            live = true;
        } else {
            label = 'Waiting for the first depth…';
        }

        text.textContent = label;
        // §8.2: the strip truncates with an ellipsis, so the full string has to be
        // recoverable on hover (and for screen readers).
        text.title = label;
        strip.title = label;
        strip.classList.toggle('is-live', live);
        strip.classList.toggle('is-idle', !live);
    }

    /** Drops the drawn line and the depth that produced it. */
    clearEngineLine() {
        this.currentPv = null;
        if (this.board && typeof this.board.clearOverlayArrows === 'function') {
            this.board.clearOverlayArrows();
        }
        this.renderEngineLine('idle');
    }

    /**
     * One SSE frame arrived. The `pv` field is re-sent at every emitted depth, so
     * this is what makes the line *evolve* rather than appear once at the end.
     */
    applyEngineLine(data) {
        if (!this.engineLineOn || this.engineLineDismissed) return;
        const pv = Array.isArray(data && data.pv) ? data.pv : [];
        if (!pv.length) return;               // never blank a line a deeper frame already drew
        this.currentPv = { pv: pv, depth: (data && data.depth) || 0 };
        if (this.board && typeof this.board.setOverlayArrows === 'function') {
            this.board.setOverlayArrows(this.pvToOverlayArrows(pv));
        }
        this.renderEngineLine('line');
    }

    /**
     * The user clicked an empty square: every line and mark went. The eval stream
     * keeps running (the bar still needs it) but must not repaint the board the
     * user just wiped, so the dismissal is remembered for this position and
     * re-armed by `updatePosition` on the next navigation.
     */
    onBoardAnnotationsCleared() {
        this.engineLineDismissed = true;
        this.clearEngineLine();
    }

    /**
     * Paints `#repOpeningLine` from the ECO encyclopedia's answer.
     *
     * **Name only, by product decision.** The ECO *code* (`B21`) is deliberately not
     * printed: the code identifies the line, the name is what a player would say out
     * loud, and `Sicilian Defense › Smith-Mora Gambit · B21` carries nothing the name
     * alone does not. `info.eco` stays in the payload — the route is a data contract
     * and a consumer that does want the code can read it — the display just does not
     * ask for it.
     *
     * Total by design: every input produces an honest label, and the four cases are
     * the four things that can actually be true of a position in a repertoire
     * builder — named, name known only from the game review, the empty board, or
     * genuinely off-book.
     */
    renderOpeningLine(info, curr) {
        const strip = document.getElementById('repOpeningLine');
        const text = document.getElementById('repOpeningLineText');
        if (!strip || !text) return;

        let label = '';
        let live = false;
        if (info && typeof info.label === 'string' && info.label) {
            label = info.label;
            live = true;
        } else if (curr && this._isRealOpeningName(curr.openingName)) {
            // The encyclopedia does not know this position, but the game review named
            // it; that is the best name available, so say it rather than pretending the
            // position is unknown. The placeholders the Builder puts in
            // `history[i].openingName` ("White Opening Repertoire", "Repertoire Line")
            // are UI labels, not opening names, and are rejected here — a placeholder
            // is better described as "no name yet" than printed as if it were one.
            label = curr.openingName;
            live = true;
        } else if (curr && (curr.ply === 0 || curr.ply === undefined)) {
            label = 'Starting Position';
        } else if (!info) {
            label = 'Looking up the opening…';
        } else {
            label = 'Not in the ECO encyclopedia';
        }

        text.textContent = label;
        text.title = label;
        strip.title = label;
        strip.classList.toggle('is-live', live);
        strip.classList.toggle('is-idle', !live);
    }

    /**
     * Is `history[i].openingName` an opening, or one of the Builder's own UI labels?
     *
     * `loadRepertoire` and `onBoardMove` seed that field with "White Opening
     * Repertoire" / "Black Opening Repertoire" / "Repertoire Line" so the *header*
     * (`#repOpeningTitle`) always has something to print. Those are labels, not
     * names, and must never reach `#repOpeningLine` — a real name comes from a game
     * review (`games.opening_name`) or an import, both of which go through the
     * encyclopedia or a Lichess title.
     */
    _isRealOpeningName(name) {
        if (typeof name !== 'string') return false;
        const clean = name.trim();
        if (!clean) return false;
        return !/^(white|black)\s+opening\s+repertoire$/i.test(clean)
            && !/^repertoire\s+line$/i.test(clean)
            && !/^(white|black)\s+repertoire$/i.test(clean);
    }

    _openingInfoCachePut(key, info) {
        const max = this.openingInfoMax || 400;
        this.openingInfoCache.delete(key);
        this.openingInfoCache.set(key, info);
        while (this.openingInfoCache.size > max) {
            const oldest = this.openingInfoCache.keys().next().value;
            if (oldest === undefined) break;
            this.openingInfoCache.delete(oldest);
        }
    }

    /**
     * Names the opening of the position on the board.
     *
     * The SAN line is sent alongside the FEN on purpose: without it the answer for a
     * position six plies past the last book move is "unknown", and in a repertoire
     * builder *every* position the user actually cares about is several plies past
     * book theory. The request is deliberately NOT aborted on navigation — it is one
     * in-memory dictionary lookup, and a superseded answer is still worth caching.
     * `openingInfoGen` is the guard that stops a slow reply for an abandoned
     * position from overwriting the strip.
     */
    async refreshOpeningLine(curr) {
        if (!curr || !curr.fen) return;
        // Claim a generation BEFORE the cache check, not after: a cache hit renders
        // synchronously and returns, and if it did not claim one, a slower in-flight
        // request for the position the user just left would still be "current" and
        // would overwrite the strip with the previous position's opening.
        const gen = ++this.openingInfoGen;
        const line = [];
        for (let i = 1; i <= this.currentStepIndex; i++) {
            const step = this.history[i];
            if (!step) break;
            const token = step.san || step.uci || '';
            if (token) line.push(token);
        }
        // 4-token key (the backend's `normalize_fen`), matching the tree's own
        // `parent_fen` so a transposition hits across navigations.
        const key = `${normalizeTreeFen(curr.fen)}|${line.join(' ')}`;

        if (this.openingInfoCache.has(key)) {
            this.renderOpeningLine(this.openingInfoCache.get(key), curr);
            return;
        }

        this.renderOpeningLine(null, curr);

        const url = `/api/repertoire/opening_info?fen=${encodeURIComponent(curr.fen)}` +
                    (line.length ? `&moves=${encodeURIComponent(line.join(' '))}` : '');
        try {
            const res = await fetch(url);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const info = await res.json();
            // Cached unconditionally, even when stale: the answer for a
            // (position, line) pair does not change, so a superseded reply is still
            // worth keeping and the next visit to that position is free.
            this._openingInfoCachePut(key, info);
            if (gen !== this.openingInfoGen) return;   // a newer position won
            this.renderOpeningLine(info, curr);
        } catch (err) {
            if (gen !== this.openingInfoGen) return;
            console.warn('Opening description failed:', err);
            this.renderOpeningLine(null, curr);
        }
    }

    async startEvalStream(fen) {
        // 1. Instantly abort any prior ongoing evaluation stream (prevents hanging during fast navigation)
        if (this.currentEvalAbort) {
            this.currentEvalAbort.abort();
            this.currentEvalAbort = null;
        }

        // CODE3-08: the cache is keyed on the NORMALISED position (halfmove/fullmove do not affect
        // the evaluation) and bounded, so transpositions hit and a long session cannot grow it
        // without limit.
        const cacheKey = this.normalizeFen(fen);

        // 2. Immediate response: check client-side memory cache first
        if (this.evalCache.has(cacheKey)) {
            const cached = this.evalCache.get(cacheKey);
            this.renderEvalBar(cached.score_cp, cached.mate, cached.depth);
            if (cached.depth >= 14) return;
        } else {
            // Quick heuristic: if this step in game review has precomputed blunder eval
            const currStep = this.history[this.currentStepIndex];
            if (currStep?.blunder?.eval_before !== undefined) {
                this.renderEvalBar(currStep.blunder.eval_before, null, 10);
            }
        }

        // 3. Progressive real-time stream from backend Stockfish
        const abortCtrl = new AbortController();
        this.currentEvalAbort = abortCtrl;
        this.renderEngineLine('thinking');

        try {
            const resp = await fetch(`/api/repertoire/eval_stream?fen=${encodeURIComponent(fen)}&depth=16`, {
                signal: abortCtrl.signal
            });
            if (!resp.ok) return;

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n\n');
                buffer = lines.pop() || '';

                for (const block of lines) {
                    // BUILDER-14: a real frame is TWO lines - `data: {…}` followed by
                    // `event: eval` - so `block.trim().slice(6)` handed
                    // `JSON.parse` the whole block and it threw, and the `catch (e) {}`
                    // below swallowed it: the eval bar never moved, on every position.
                    // Pull the data line out of the block first (the shape scout.js uses).
                    const eventLine = block.split('\n').find(l => l.startsWith('data: '));
                    if (eventLine) {
                        let data = null;
                        try {
                            data = JSON.parse(eventLine.slice(6));
                        } catch (e) {}
                        if (!data) continue;
                        // A terminal `error` frame carries `final: true` and a stable
                        // `code`; it is a real outcome, not a dropped frame, so the
                        // engine strip says so instead of waiting forever.
                        if (data.type === 'error') {
                            console.warn('Eval stream error frame:', data.code, data.error);
                            this.renderEngineLine('error');
                            continue;
                        }
                        if (data.score_cp !== undefined) {
                            this.renderEvalBar(data.score_cp, data.mate, data.depth);
                            this._evalCachePut(cacheKey, data);
                        }
                        // The same frame carries the principal variation at this depth
                        // (contract v2), so one stream feeds both the bar and the line.
                        this.applyEngineLine(data);
                    }
                }
            }
        } catch (err) {
            if (err.name === 'AbortError') {
                // Aborted because user navigated to another move: normal!
                return;
            }
            console.warn('Eval stream error:', err);
            this.renderEngineLine('error');
        } finally {
            if (this.currentEvalAbort === abortCtrl) {
                this.currentEvalAbort = null;
            }
        }
    }

    /** Bounded insert into the eval cache; keeps the deepest result for a position. */
    _evalCachePut(cacheKey, data) {
        const max = this.evalCacheMax || 400;
        const prev = this.evalCache.get(cacheKey);
        if (prev && (prev.depth || 0) > (data.depth || 0)) return;   // never downgrade
        // Re-insert so Map iteration order == recency (LRU).
        this.evalCache.delete(cacheKey);
        this.evalCache.set(cacheKey, data);
        while (this.evalCache.size > max) {
            const oldest = this.evalCache.keys().next().value;
            if (oldest === undefined) break;
            this.evalCache.delete(oldest);
        }
    }

    renderBreadcrumbs() {
        const container = document.getElementById('repBreadcrumbs');
        if (!container) return;
        container.innerHTML = '';

        this.history.forEach((step, idx) => {
            const chip = document.createElement('span');
            chip.className = 'breadcrumb-chip';
            if (idx === 0) chip.classList.add('start');
            if (idx === this.currentStepIndex) chip.classList.add('active');

            if (step.isTheory) {
                chip.classList.add('theory-step');
                chip.title = `Opening Theory: ${step.san}`;
            }

            if (step.blunder) {
                chip.classList.add('blunder-step');
                const bTitle = `Blunder on move ${step.blunder.move_number}: ${step.blunder.played_san || '?'} (engine: ${step.blunder.best_san || '?'})`;
                chip.title = step.isTheory ? `${step.san} — ${bTitle}` : bTitle;
            }

            if (idx === 0) {
                chip.textContent = 'Start';
            } else {
                const moveNum = Math.ceil(step.ply / 2);
                const isWhite = (step.ply % 2 === 1);
                chip.textContent = isWhite ? `${moveNum}. ${step.san}` : `${moveNum}... ${step.san}`;
            }

            chip.addEventListener('click', () => this.jumpToStep(idx));
            container.appendChild(chip);
        });

        // Auto-scroll active chip into view
        const activeChip = container.querySelector('.breadcrumb-chip.active');
        if (activeChip) {
            activeChip.scrollIntoView({ behavior: 'smooth', inline: 'nearest', block: 'nearest' });
        }
    }

    /**
     * Former blunder banner placeholder (retired in favour of Repertoire Trainer Game Deviations).
     * Delegates to the game review banner when reviewing a game.
     */
    renderBlunderBanner(curr) {
        if (this.activeReviewGame) {
            this.renderGameReviewBanner(this.activeReviewGame);
        }
    }

    _escapeHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    _escapeAttr(s) {
        return this._escapeHtml(s);
    }

    /**
     * Coerces a value that is about to be interpolated into a `style="width:N%"` or a numeric
     * column. Explorer/DB payloads are JSON, so `popularity_pct` / `games` can legitimately arrive
     * as strings; `"<img src=x>".toLocaleString()` returns the payload verbatim, so an un-coerced
     * value is an injection primitive. `Number()` + `|| 0` makes every one of them inert.
     */
    _num(v, fallback = 0) {
        const n = Number(v);
        return Number.isFinite(n) ? n : fallback;
    }

    /**
     * Allowlist for a value that lands in a `class="..."` attribute. `games.result` reaches the
     * frontend from the Lichess API, so it must never be able to close the attribute and append an
     * event handler. Anything outside the known set degrades to the neutral `draw` pill.
     */
    _outcomeClass(v) {
        return (v === 'win' || v === 'loss' || v === 'draw') ? v : 'draw';
    }

    /**
     * Allowlist for an `href` built from backend data. `javascript:` (and any other non-web
     * scheme) would execute on click; anything that is not http(s) collapses to '#'.
     */
    _safeHttpUrl(u) {
        const s = String(u == null ? '' : u).trim();
        if (!s) return '#';
        if (/^https?:\/\//i.test(s)) return s;
        return '#';
    }

    /** The biggest blunder in the current payload that is not the one being shown. */
    nextUnpatchedGameGap(excludeId) {
        const gaps = this.treeData?.game_gaps?.gaps;
        if (!Array.isArray(gaps) || gaps.length === 0) return null;
        for (const g of gaps) {
            if (excludeId != null && g.id === excludeId) continue;
            return g;
        }
        return null;
    }

    /** Header action "Address #1 Blunder". */
    addressNextGameGap() {
        const gap = this.nextUnpatchedGameGap(null);
        if (!gap) {
            this.notify('No unaddressed opening blunders found. Sync more games or lower the opening cutoff.', '#94a3b8');
            return;
        }
        this.addressGameGap(gap);
    }

    /**
     * "Adopt the engine's move" for a blunder being reviewed. After the write the patched blunder
     * is filtered out of the active list by the backend (`include_patched=false`) and the next
     * one is promoted to #1 (AGENTS.md 4.D).
     */
    async adoptBlunderSolution(blunder, btn) {
        if (!blunder) return;
        const key = `adopt:${blunder.id}`;
        if (!this.beginWrite(key)) return;
        const original = btn ? btn.textContent : '';
        if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }
        try {
            const ok = await this.saveMove(blunder.fen_before, {
                uci: (blunder.best_move || blunder.best_uci),
                san: blunder.best_san,
                popularity_pct: 0,
                win_rate: 50,
                eval: blunder.eval_after || 0
            }, true);
            if (ok && btn) {
                btn.textContent = '✓ Adopted!';
                btn.style.background = '#22c55e';
            } else if (btn) {
                btn.textContent = original;
                btn.disabled = false;
            }
        } finally {
            this.endWrite(key);
        }
        // refreshTree() inside saveMove() re-rendered the lists, so re-render the banner and
        // the gap list to promote the next blunder to #1.
        const curr = this.history[this.currentStepIndex];
        this.renderBlunderBanner(curr);
        this.renderGapLists();
    }

    /**
     * Renders BOTH gap lists that `/api/repertoire/tree` returns and the frontend previously
     * discarded: `game_gaps.gaps` (root-cause blunders from the user's own games) and
     * `gaps` (unprepared frequent opponent replies). Both are pure string builders; every
     * dynamic string is escaped and every text node that varies with the data is clamped
     * (AGENTS.md 8.2).
     */
    renderGapLists() {
        const covGaps = Array.isArray(this.treeData?.gaps) ? this.treeData.gaps : [];
        const tabCount = document.getElementById('repCoverageGapsCount');
        if (tabCount) {
            tabCount.textContent = covGaps.length;
            tabCount.title = `${covGaps.length} unprepared opponent moves to address`;
        }

        // AGENTS.md 4.G: a coverage gap is one of TWO honest things, and they are not the
        // same problem. A `missing_branch` is a frequent opponent reply the repertoire does
        // not hold at all. An `unanswered_branch` is a reply the repertoire DOES hold but has
        // no prepared answer for, so the line would finish on the opponent's move — which is
        // exactly what the owner ruled must never happen (TRAINER-09).
        const unansweredGaps = covGaps.filter(g => this._isUnansweredBranch(g));
        const missingGaps = covGaps.length - unansweredGaps.length;
        const covMeta = document.getElementById('repCoverageGapsMeta');
        if (covMeta) {
            const t = unansweredGaps.length
                ? `${unansweredGaps.length} branch${unansweredGaps.length === 1 ? '' : 'es'} with no prepared reply`
                  + ` · ${missingGaps} not in your repertoire`
                  + ` · ≥ ${this.gapThreshold}%`
                : `${covGaps.length} opponent move${covGaps.length === 1 ? '' : 's'} not in your repertoire`
                  + ` ≥ ${this.gapThreshold}%`;
            covMeta.textContent = t;
            covMeta.title = t;
        }
        const covList = document.getElementById('repCoverageGapsList');
        if (covList) {
            covList.innerHTML = covGaps.length === 0
                ? '<div style="text-align:center; color:var(--text-muted); padding:0.75rem; font-size:0.8rem;">Every frequent opponent reply is already prepared. 🎉</div>'
                : covGaps.map(g => this._coverageGapCardHtml(g)).join('');
            covList.querySelectorAll('.btn-address-coverage-gap').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const pf = btn.getAttribute('data-parent');
                    const gap = covGaps.find(x => x.parent_fen === pf) || null;
                    this.addressGap(gap);
                });
            });
        }

        const gapsList = document.getElementById('repGapsList');
        if (gapsList) {
            const gameGapsData = this.treeData?.game_gaps || {};
            const gaps = Array.isArray(gameGapsData.gaps) ? gameGapsData.gaps : [];
            gapsList.innerHTML = gaps.length === 0
                ? '<div style="text-align:center; color:var(--text-muted); padding:0.75rem;">No unpatched blunders found in your recent games. 🎉</div>'
                : gaps.map((g, idx) => this._blunderGapCardHtml(g, idx)).join('');
            gapsList.querySelectorAll('.btn-address-blunder').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const gid = btn.getAttribute('data-gap-id');
                    const gap = gaps.find(x => String(x.id) === gid) || null;
                    this.addressGap(gap);
                });
            });
        }
    }

    /**
     * AGENTS.md 4.G — is this coverage gap a branch the repertoire HOLDS but cannot answer?
     *
     * Total over anything `/api/repertoire/tree` might hand it. `gap_kind` is authoritative
     * when present; a payload from before the field existed still carries `in_repertoire`,
     * which the server sets to exactly this condition, so the fallback is exact rather than
     * a guess. An unrecognised explicit `gap_kind` is treated as `missing_branch` — the
     * pre-4.G wording — because a new kind must not silently be labelled "you have no
     * answer to a move you have not prepared".
     */
    _isUnansweredBranch(gap) {
        if (!gap || typeof gap !== 'object') return false;
        const kind = typeof gap.gap_kind === 'string' ? gap.gap_kind : null;
        if (kind === 'unanswered_branch') return true;
        if (kind === 'missing_branch') return false;
        if (kind !== null) return false;
        return Boolean(gap.in_repertoire);
    }

    /**
     * One row of `#repCoverageGapsList`. The two gap kinds are rendered with the same
     * structure, the same number of clamped lines and the same button footprint, so
     * switching between them cannot shift the list (AGENTS.md 8.1/8.2). Only the wording,
     * the badge, the accent and the button label differ.
     *
     * SEC-J: every hole into markup is an escape call, a `Number()` coercion, or a ternary
     * between two *literals*. Nothing derived from a server field is ever interpolated raw,
     * and no value is interpolated into a `class=` attribute (an attribute-break-out primitive).
     */
    _coverageGapCardHtml(g) {
        const gap = g || {};
        const unanswered = this._isUnansweredBranch(gap);
        const san = gap.opponent_san || '?';
        const pct = `${this._num(gap.popularity_pct, 0).toFixed(1)}%`;
        const games = `${this._num(gap.games, 0).toLocaleString()} games`;
        const where = `after ${gap.opening_name || 'Repertoire Line'}${gap.eco ? ' · ' + gap.eco : ''}`;
        // 4.G: the unanswered case is NOT a success and NOT an error — the user played their
        // own move correctly and simply has not prepared that variation yet. Amber is the
        // neutral/warning slot of the toast palette (8.1) and is what the Trainer uses for
        // the same state, so the two halves of the product say the same thing. The sentence
        // names the fix, exactly as `_lineEndPresentation()` in repertoire_trainer.js does.
        const note = unanswered
            ? 'You have this reply but no prepared answer after it — the line would stop on their move. Add your reply in the Repertoire Builder.'
            : 'Not in your repertoire. Add this reply, then prepare your answer to it.';
        // 8.2: every line that varies with the data is clamped, and the untruncated sentence
        // goes in the row's `title` so a clamped row is still fully readable on hover.
        const head = `${san} — ${pct} of 2000+ games (${games}), ${where}. ${note}`;
        return `
            <div class="gap-item gap-item--${unanswered ? 'unanswered' : 'missing'}" title="${this._escapeAttr(head)}">
                <div class="gap-left" style="flex:1; min-width:0;">
                    <div style="display:flex; align-items:center; gap:0.4rem;">
                        <span class="gap-san" style="font-weight:700; color:#fff;">${this._escapeHtml(san)}</span>
                        <span class="badge-critical">${this._num(gap.popularity_pct, 0).toFixed(1)}%</span>
                        ${unanswered
                            ? '<span class="badge-mistake">No prepared reply</span>'
                            : '<span class="badge-critical">Not in repertoire</span>'}
                        <span class="gap-meta" style="color:var(--text-muted); margin-left:auto; white-space:nowrap;">${this._num(gap.games, 0).toLocaleString()} games</span>
                    </div>
                    <div class="gap-meta" style="color:var(--text-secondary); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${this._escapeHtml(where)}</div>
                    <div class="gap-meta" style="color:${unanswered ? '#fbbf24' : 'var(--text-secondary)'}; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${this._escapeHtml(note)}</div>
                </div>
                <div style="flex-shrink:0; margin-top:0.3rem;">
                    <button class="btn-secondary btn-address-coverage-gap" style="font-size:0.7rem; padding:0.18rem 0.45rem; min-width:110px; justify-content:center;"
                        data-parent="${this._escapeAttr(gap.parent_fen || '')}">${unanswered ? 'Add your reply →' : 'Add this reply →'}</button>
                </div>
            </div>
        `;
    }

    _blunderGapCardHtml(g, idx) {
        const isPatched = Boolean(g.is_patched);
        const root = g.earlier_mistake;
        const head = `Move ${g.move_number}: played ${g.played_san || '?'}, engine preferred ${g.best_san || '?'}` +
            (g.cpl_loss ? ` (-${Math.round(g.cpl_loss)} cp)` : '');
        return `
            <div class="gap-item" title="${this._escapeAttr(head)}">
                <div style="display:flex; align-items:center; gap:0.4rem;">
                    <span class="gap-san" style="font-weight:700; color:#fff;">${idx + 1}. ${this._escapeHtml(g.played_san || '?')}</span>
                    <span style="color:var(--text-muted);">→</span>
                    <span class="gap-san" style="font-weight:700; color:#4ade80;">${this._escapeHtml(g.best_san || '?')}</span>
                    ${isPatched
                        ? '<span class="badge-patched">✓ Patched</span>'
                        : '<span class="badge-critical">Unpatched</span>'}
                    <span class="gap-meta" style="color:var(--text-muted); margin-left:auto; white-space:nowrap;">-${Math.round(g.cpl_loss || 0)} cp</span>
                </div>
                <div class="gap-meta" style="color:var(--text-secondary); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                    ${this._escapeHtml(g.opening_name || 'Opening Line')}${g.eco ? ' · ' + this._escapeHtml(g.eco) : ''} · vs ${this._escapeHtml(g.opponent_name || '?')} · ${this._num(g.opening_games, 0).toLocaleString()} master games
                </div>
                ${root ? `<div class="gap-meta" style="color:#fca5a5; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">↳ root cause: move ${this._num(root.move_number, 0)} (${this._escapeHtml(root.played_san || '?')})</div>` : ''}
                <div style="margin-top:0.3rem; display:flex; gap:0.3rem; align-items:center;">
                    <button class="btn-secondary btn-address-blunder" style="font-size:0.7rem; padding:0.18rem 0.5rem; min-width:110px; justify-content:center;"
                        data-gap-id="${this._escapeAttr(String(g.id))}">Review &amp; Fix →</button>
                </div>
            </div>
        `;
    }

    jumpToStep(index) {
        if (index < 0) index = 0;
        if (index >= this.history.length) index = this.history.length - 1;

        const prevIndex = this.currentStepIndex;
        this.currentStepIndex = index;
        const curr = this.history[index];
        if (!curr) return;

        const now = Date.now();
        const isRapid = (now - (this.lastNavTime || 0) < 160);
        this.lastNavTime = now;

        // Stop any running animations so the board is clean immediately. `animateMove` now always
        // delivers its completion callback (CODE3-13), so a cancel cannot leave the board behind.
        if (this.board && typeof this.board.stopAnimations === 'function') {
            this.board.stopAnimations();
        }

        // Stepping forward exactly one move: reuse the single canonical render path
        // (`updatePosition`, which loads the FEN first and restores arrows last) and layer a
        // purely decorative slide on top. The old fast path re-implemented 9 of 12 statements of
        // `updatePosition` and omitted the title/ECO/turn-indicator updates.
        if (!isRapid && index === prevIndex + 1) {
            const nextStep = this.history[index];
            if (nextStep && nextStep.uci && nextStep.uci.length >= 4) {
                const from = nextStep.uci.slice(0, 2);
                const to = nextStep.uci.slice(2, 4);
                if (this.board && typeof this.board.animateGhostMove === 'function') {
                    this.updatePosition({ animateFromTo: [from, to] });
                    return;
                }
            }
        }

        this.updatePosition();
    }

    jumpToStart() {
        this.jumpToStep(0);
    }

    jumpToEnd() {
        this.jumpToStep(this.history.length - 1);
    }

    stepBack() {
        if (this.currentStepIndex > 0) {
            this.jumpToStep(this.currentStepIndex - 1);
        }
    }

    stepForward() {
        // If we previously stepped back, move forward in existing history
        if (this.currentStepIndex < this.history.length - 1) {
            this.jumpToStep(this.currentStepIndex + 1);
            return;
        }

        // At end of line: check if there's a prepared move or top opponent branch to advance to
        const curr = this.history[this.currentStepIndex];
        if (!curr) return;

        const normParent = this.normalizeFen(curr.fen);
        const inTreeMoves = (this.treeData?.moves_by_parent?.[normParent]) || [];

        // 1. Check if user's turn and has prepared move
        const userPrepared = inTreeMoves.find(m => m.is_user_move === 1);
        if (userPrepared) {
            this.playMoveFromUci(userPrepared.move_uci, userPrepared.move_san);
            return;
        }

        // 2. Check if opponent's turn and has branch in tree
        const oppBranches = inTreeMoves.filter(m => m.is_user_move === 0);
        if (oppBranches.length > 0) {
            const topOpp = [...oppBranches].sort((a, b) => (b.popularity_pct || 0) - (a.popularity_pct || 0))[0];
            this.playMoveFromUci(topOpp.move_uci, topOpp.move_san);
            return;
        }

        // 3. Fallback: play the top 2000+ candidate move if it is already cached, BUT only if not
        //    spamming arrows. NOTE: the cache is only ever written as `${color}_${normFen}`, so the
        //    old second lookup key (`normParent` on its own) was permanently dead.
        const now = Date.now();
        if (now - (this.lastNavTime || 0) < 250) {
            return;
        }
        const cacheKey = `${this.activeColor}_${normParent}`;
        const cached = this.candidateCache.get(cacheKey);
        if (cached && cached.moves && cached.moves.length > 0) {
            const topCand = cached.moves[0];
            this.playMoveFromUci(topCand.uci, topCand.san);
        } else {
            this.notify('No prepared repertoire move here. Use "+ Set as My Move" to add one.', '#94a3b8');
        }
    }

    onBoardMove(from, to, promo, moveRes) {
        if (!moveRes) return;

        // Check if user was looking at an upcoming blunder and played the solution
        const nextStep = this.history[this.currentStepIndex + 1];
        const upcomingBlunder = nextStep ? nextStep.blunder : null;
        const playedUci = from + to + (promo || '');

        let wasCorrectBlunderSolution = false;
        let solvedBlunder = null;
        if (upcomingBlunder) {
            const bestUci = upcomingBlunder.best_move || upcomingBlunder.best_uci;
            if (playedUci === bestUci) {
                wasCorrectBlunderSolution = true;
                solvedBlunder = upcomingBlunder;
                if (window.chessAudio) window.chessAudio.playSuccess();
            }
        }

        // If the move played is already the next move in existing history, simply advance!
        const nextStepInHist = this.history[this.currentStepIndex + 1];
        if (nextStepInHist && nextStepInHist.uci === playedUci) {
            this.jumpToStep(this.currentStepIndex + 1);
            return;
        }

        // Truncate future history if navigated back
        this.history = this.history.slice(0, this.currentStepIndex + 1);

        const nextPly = this.history.length;
        const newFen = this.board.getFEN();

        // Sanity check: prevent pushing duplicate FEN or identical move
        const curr = this.history[this.currentStepIndex];
        if (curr && curr.fen === newFen) {
            return;
        }

        // Before pushing new move, ensure the preceding line history is anchored in session tree
        this.ensureHistoryInLineTree(this.currentStepIndex);

        const step = {
            ply: nextPly,
            fen: newFen,
            san: moveRes.san,
            uci: playedUci,
            openingName: this.history[this.currentStepIndex]?.openingName || 'Repertoire Line',
            eco: this.history[this.currentStepIndex]?.eco || '',
            blunder: null
        };

        this.history.push(step);
        this.currentStepIndex = this.history.length - 1;

        const prevStep = this.history[this.currentStepIndex - 1];
        const moveObj = {
            fen_before: prevStep ? prevStep.fen : 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
            fen: step.fen,
            fen_after: step.fen,
            uci: step.uci,
            san: step.san,
            color: (prevStep && prevStep.fen.split(' ')[1] === 'w') ? 'white' : 'black',
            move_number: Math.floor((step.ply - 1) / 2) + 1,
            opening_name: step.openingName || '',
            eco: step.eco || '',
            comment: '',
            is_important: false,
            arrows: ''
        };
        const repMove = this.findMoveInTree(moveObj.fen_before, step.uci);
        if (repMove) {
            if (repMove.comment) moveObj.comment = repMove.comment;
            if (repMove.is_important) moveObj.is_important = true;
            if (repMove.arrows) moveObj.arrows = repMove.arrows;
        }

        // Add to session tree from the parent position
        this.lineTree.addMove(moveObj);
        this.updateActiveLineCounts();
        this.renderTreeLines();

        this.updatePosition();

        if (wasCorrectBlunderSolution && solvedBlunder) {
            this.notify(`🎉 Correct! ${moveRes.san} was the best move.`, '#4ade80');
        }
    }

    async loadCandidateMoves(fen, isUserTurn) {
        const tbody = document.getElementById('repExplorerTbody');
        const statsEl = document.getElementById('repDbStats');
        if (!tbody) return;

        const targetFen = fen;
        const normFen = this.normalizeFen(fen);
        // CODE3-07: the request identity is (colour, position, generation), not just the position.
        // The start FEN is re-visited on every colour switch / tab switch, so a FEN-only guard let
        // the other colour's candidates (and badges) render.
        const reqColor = this.activeColor;
        const reqGen = ++this.candidateGen;
        const cacheKey = `${reqColor}_${normFen}`;
        const isStale = () => (
            this.activeColor !== reqColor ||
            this.candidateGen !== reqGen
        );
        try {
            let data = this.candidateCache.get(cacheKey);
            if (!data || (!data.moves?.length && (data.total_games || 0) > 0)) {
                tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color:var(--text-muted); padding:1rem;">Loading candidate moves & personal stats...</td></tr>`;

                if (this.candidateAbortController) {
                    this.candidateAbortController.abort();
                }
                const abortCtrl = new AbortController();
                this.candidateAbortController = abortCtrl;

                const res = await fetch(`/api/repertoire/explore?fen=${encodeURIComponent(fen)}&color=${reqColor}`, {
                    signal: abortCtrl.signal
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                data = await res.json();
                if (isStale()) return;
                if (data && (data.moves?.length > 0 || data.total_games === 0)) {
                    this.candidateCache.set(cacheKey, data);
                }
            }

            // Ensure we are still on the requested position AND colour
            const curr = this.history[this.currentStepIndex];
            if (!curr || curr.fen !== targetFen || isStale()) {
                return;
            }

            this.currentCandidateData = data;
            const total = data?.total_games || 0;
            if (statsEl) statsEl.textContent = `${total.toLocaleString()} games (2000+ Elo)`;

            this.updateSortHeaderUI();
            this.renderCandidateTable(fen, isUserTurn);
        } catch (e) {
            if (e.name === 'AbortError') return;
            console.error('Error loading candidate moves:', e);
            tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color:#f87171; padding:1rem;">Failed to fetch candidate moves.</td></tr>`;
        }
    }

    renderCandidateTable(fen, isUserTurn) {
        const tbody = document.getElementById('repExplorerTbody');
        if (!tbody) return;

        tbody.innerHTML = '';
        const rawMoves = this.currentCandidateData?.moves || [];

        if (rawMoves.length === 0) {
            tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color:var(--text-muted); padding:1.5rem;">No 2000+ master games recorded in this position.</td></tr>`;
            return;
        }

        const moves = this.sortCandidateMoves(rawMoves);

        // Find moves already in tree for this parent_fen
        const normParent = this.normalizeFen(fen);
        const inTreeMoves = (this.treeData?.moves_by_parent?.[normParent]) || [];
        const userPreparedUci = inTreeMoves.find(m => m.is_user_move === 1)?.move_uci;
        const oppPreparedUcis = new Set(inTreeMoves.filter(m => m.is_user_move === 0).map(m => m.move_uci));

        moves.forEach(m => {
            const tr = document.createElement('tr');
            tr.className = 'explorer-row';
            tr.innerHTML = this._candidateRowHtml(m, isUserTurn, m.uci === userPreparedUci, oppPreparedUcis.has(m.uci));

            // Row click: play the move on the board
            tr.addEventListener('click', (e) => {
                if (e.target.closest('button')) return;
                this.playMoveFromUci(m.uci, m.san);
            });

            // Button handlers. Each disables its own button for the duration of the write so a
            // double click cannot fire two POSTs (each of which fans out into three more
            // requests) - CODE3-12. `saveMove`/`removeBranch` also carry a keyed in-flight latch.
            const withButtonLatch = (btn, fn) => {
                btn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    if (btn.disabled) return;
                    btn.disabled = true;
                    try {
                        await fn();
                    } finally {
                        if (btn.isConnected) btn.disabled = false;
                    }
                });
            };

            const btnSet = tr.querySelector('.btn-set-move');
            if (btnSet) withButtonLatch(btnSet, () => this.saveMove(fen, m, true));

            const btnAdd = tr.querySelector('.btn-add-branch');
            if (btnAdd) withButtonLatch(btnAdd, () => this.saveMove(fen, m, false));

            const btnDel = tr.querySelector('.btn-delete-move, .btn-delete-branch');
            if (btnDel) withButtonLatch(btnDel, () => this.removeBranch(fen, m.uci));

            tbody.appendChild(tr);
        });
    }

    /**
     * Markup for one row of the 2000+ Masters explorer table. Extracted from
     * `renderCandidateTable()` purely so it can be rendered (and audited) on its own — the
     * produced markup is unchanged.
     *
     * SEC-J: every value here comes from `/api/repertoire/explore`, whose payload is the
     * Lichess Masters explorer response cached verbatim in `explorer_cache.data_json`. It is
     * remote data, so: `san` is escaped; every numeric field is coerced through `_num()`
     * (a JSON *string* would otherwise reach `.toLocaleString()` intact and land verbatim in
     * `style="width:…%"`); and no field is ever used to build a class name — the three badge
     * classes are chosen by comparing a coerced number against a literal.
     */
    _candidateRowHtml(m, isUserTurn, isUserPrepared, isOppCovered) {
        const move = m || {};

        // Stockfish Eval formatting (+ White advantage, - Black advantage)
        let evalHtml = '<span class="eval-badge zero" title="No engine evaluation">-</span>';
        const hasEval = (move.eval !== undefined && move.eval !== null);
        const evalCp = hasEval ? this._num(move.eval, NaN) : NaN;
        if (Number.isFinite(evalCp)) {
            const score = evalCp / 100.0;
            const tip = this._escapeAttr(score > 0 ? `White advantage (+${score.toFixed(2)})` : (score < 0 ? `Black advantage (${score.toFixed(2)})` : 'Equal position (0.00)'));
            if (score > 0.15) {
                evalHtml = `<span class="eval-badge plus" title="${tip}">+${score.toFixed(2)}</span>`;
            } else if (score < -0.15) {
                evalHtml = `<span class="eval-badge minus" title="${tip}">${score.toFixed(2)}</span>`;
            } else {
                evalHtml = `<span class="eval-badge zero" title="${tip}">${score >= 0 ? '+' : ''}${score.toFixed(2)}</span>`;
            }
        }

        // Win rate segment bar (Master)
        const wWhite = this._num(move.white_win_pct, 0);
        const wDraw = this._num(move.draw_pct, 0);
        const wBlack = this._num(move.black_win_pct, 0);
        const wrBarHtml = `
            <div class="winrate-bar-wrap" title="White ${wWhite}% | Draw ${wDraw}% | Black ${wBlack}%">
                <div class="wr-white" style="width:${wWhite}%;">${wWhite > 18 ? wWhite + '%' : ''}</div>
                <div class="wr-draw" style="width:${wDraw}%;">${wDraw > 18 ? wDraw + '%' : ''}</div>
                <div class="wr-black" style="width:${wBlack}%;">${wBlack > 18 ? wBlack + '%' : ''}</div>
            </div>
        `;

        // Personal Stats (User's last 150 games)
        const ms = move.my_stats;
        let myStatsHtml = '';
        const msGames = this._num(ms && ms.games, 0);
        if (ms && msGames > 0) {
            let badgeClass = 'neutral';
            let badgeIcon = '';
            const msWinRate = (ms.win_rate === null || ms.win_rate === undefined)
                ? NaN : this._num(ms.win_rate, NaN);
            if (Number.isFinite(msWinRate)) {
                if (msWinRate < 40) {
                    badgeClass = 'threat';
                    badgeIcon = '⚠️ ';
                } else if (msWinRate > 60) {
                    badgeClass = 'win';
                    badgeIcon = '✓ ';
                }
            }
            const winRateText = Number.isFinite(msWinRate) ? `${msWinRate}%` : '-';
            const msWins = this._num(ms.wins, 0);
            const msDraws = this._num(ms.draws, 0);
            const msLosses = this._num(ms.losses, 0);
            const threatTitle = this._escapeAttr(
                `Threat Score: ${this._num(ms.threat_score, 0)} | Played in ${msGames} of your games | ` +
                `Win rate: ${winRateText} (${msWins}W - ${msDraws}D - ${msLosses}L)`);
            myStatsHtml = `
                <div style="display:flex; flex-direction:column; gap:2px;" title="${threatTitle}">
                    <div style="display:flex; align-items:center; gap:0.4rem;">
                        <strong style="font-size:0.85rem; color:#fff;">${msGames} ${msGames === 1 ? 'game' : 'games'}</strong>
                        <span class="my-stat-badge ${badgeClass}">${badgeIcon}${winRateText}</span>
                    </div>
                    <div style="font-size:0.7rem; color:var(--text-muted);">
                        ${msWins}W • ${msDraws}D • ${msLosses}L
                    </div>
                </div>
            `;
        } else {
            myStatsHtml = `<span style="color:var(--text-muted); font-size:0.75rem;">—</span>`;
        }

        // Action Column
        let actionHtml = '';
        if (isUserTurn) {
            if (isUserPrepared) {
                actionHtml = `
                    <span class="badge-in-tree">★ Your Move</span>
                    <button class="btn-del-branch btn-delete-move" title="Remove as primary move">✕</button>
                `;
            } else {
                actionHtml = `<button class="btn-add-move btn-set-move">+ Set as My Move</button>`;
            }
        } else {
            if (isOppCovered) {
                actionHtml = `
                    <span class="badge-opp-tree">✓ In Tree</span>
                    <button class="btn-del-branch btn-delete-branch" title="Remove branch">✕</button>
                `;
            } else {
                actionHtml = `<button class="btn-add-move btn-add-branch" style="background:#2563eb;">+ Add Branch</button>`;
            }
        }

        const popPct = this._num(move.popularity_pct, 0);
        return `
            <td>
                <span class="explorer-move-san">${this._escapeHtml(move.san)}</span>
            </td>
            <td>
                <div class="pop-bar-wrap">
                    <div class="pop-bar-track">
                        <div class="pop-bar-fill" style="width:${Math.min(100, popPct)}%;"></div>
                    </div>
                    <span style="font-weight:600; font-size:0.8rem;">${popPct}%</span>
                </div>
                <div style="color:var(--text-muted); font-size:0.72rem; margin-top:2px;">${this._num(move.games, 0).toLocaleString()} games</div>
            </td>
            <td>${wrBarHtml}</td>
            <td>${myStatsHtml}</td>
            <td>${evalHtml}</td>
            <td style="white-space:nowrap;">${actionHtml}</td>
        `;
    }

    playMoveFromUci(uci, san) {
        if (!uci || uci.length < 4) return;
        const from = uci.slice(0, 2);
        const to = uci.slice(2, 4);
        const promo = uci.length === 5 ? uci[4] : undefined;

        // Ensure board is synced to currentStepIndex FEN before playing move
        const curr = this.history[this.currentStepIndex];
        if (curr && this.board.getFEN() !== curr.fen) {
            this.board.loadFEN(curr.fen);
        }

        const moveRes = this.board.playMove(from, to, promo, false);
        if (moveRes) {
            this.onBoardMove(from, to, promo, moveRes);
        }
    }

    async saveMove(parentFen, moveObj, isUserMove) {
        if (!parentFen || !moveObj || !moveObj.uci) {
            this.notify('Cannot save a move without a parent position and a move.', '#ef4444');
            return false;
        }
        const key = `saveMove:${parentFen}:${moveObj.uci}`;
        if (!this.beginWrite(key)) return false;   // CODE3-12: exactly one write per click
        const payload = {
            repertoire: this.activeColor,
            parent_fen: parentFen,
            move_uci: moveObj.uci,
            is_user_move: isUserMove,
            popularity_pct: moveObj.popularity_pct || 0.0,
            win_rate: moveObj.win_rate || 50.0,
            eval_score: moveObj.eval || 0,
            study_name: this.currentStudy || undefined
        };

        try {
            const res = await fetch('/api/repertoire/move', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            if (data && data.status === 'success') {
                if (window.chessAudio) window.chessAudio.playSuccess();
                await this.loadStudies();
                await this.refreshTree();
                await this.loadCandidateMoves(parentFen, isUserMove);
                return true;
            }
            this.notify((data && data.message) || 'Could not save the move.', '#f59e0b');
            return false;
        } catch (e) {
            console.error('Error saving repertoire move:', e);
            this.notify(`Could not save ${moveObj.san || moveObj.uci}: ${e.message}`, '#ef4444');
            return false;
        } finally {
            this.endWrite(key);
        }
    }

    async removeBranch(parentFen, moveUci) {
        const key = `removeBranch:${parentFen}:${moveUci}`;
        if (!this.beginWrite(key)) return false;
        try {
            const res = await fetch('/api/repertoire/move', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repertoire: this.activeColor,
                    parent_fen: parentFen,
                    move_uci: moveUci
                })
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            if (data && data.status === 'ok') {
                if (window.chessAudio) window.chessAudio.playSuccess();
                await this.refreshTree();
                const curr = this.history[this.currentStepIndex];
                const isUserTurn = curr ? ((this.activeColor === 'white' && (curr.ply % 2 === 0)) ||
                                           (this.activeColor === 'black' && (curr.ply % 2 === 1))) : true;
                await this.loadCandidateMoves(parentFen, isUserTurn);
                return true;
            }
            this.notify((data && data.message) || 'Could not remove that branch.', '#f59e0b');
            return false;
        } catch (e) {
            console.error('Error deleting branch:', e);
            this.notify(`Could not remove that branch: ${e.message}`, '#ef4444');
            return false;
        } finally {
            this.endWrite(key);
        }
    }

    /**
     * Loads a root-cause opening blunder (a `game_gaps.gaps` entry) onto the board, replays the
     * whole game, and parks the cursor on the position *before* the mistake so the user can play
     * the engine's move. Reachable from the "🚨 Blunders" sidebar tab, the "Address #1 Blunder"
     * header button and the banner's "Address Next Blunder (#1)" button (CODE3-03).
     */
    async addressGameGap(gap) {
        if (!gap) return;

        this.activeReviewGame = null;
        this.activeReviewGap = gap;
        const movesList = (gap.game_moves || '').trim().split(/\s+/).filter(Boolean);

        if (movesList.length > 0 && typeof Chess !== 'undefined') {
            try {
                const tempChess = new Chess();
                this.history = [{
                    ply: 0,
                    fen: tempChess.fen(),
                    san: 'Start',
                    uci: null,
                    moveNumber: 0,
                    openingName: gap.opening_name || 'Line',
                    eco: gap.eco || '',
                    blunder: null,
                    opponentName: gap.opponent_name
                }];

                // Map of ply -> blunder
                const blunderMap = new Map();
                if (gap.all_blunders && gap.all_blunders.length) {
                    gap.all_blunders.forEach(b => blunderMap.set(b.ply, b));
                } else {
                    blunderMap.set(gap.ply, gap);
                }

                for (let i = 0; i < movesList.length; i++) {
                    const san = movesList[i];
                    const mRes = tempChess.move(san, { sloppy: true });
                    if (!mRes) break;
                    const ply = i + 1;
                    const moveNum = Math.ceil(ply / 2);
                    const isWhite = (ply % 2 === 1);
                    this.history.push({
                        ply: ply,
                        fen: tempChess.fen(),
                        san: mRes.san,
                        uci: mRes.from + mRes.to + (mRes.promotion || ''),
                        moveNumber: moveNum,
                        isWhite: isWhite,
                        openingName: gap.opening_name || 'Line',
                        eco: gap.eco || '',
                        blunder: blunderMap.get(ply) || null,
                        opponentName: gap.opponent_name
                    });
                }

                // Start position right before the blunder
                this.currentStepIndex = Math.max(0, gap.ply - 1);
            } catch (err) {
                console.error("Error building game history:", err);
                this.history = [{
                    ply: gap.ply - 1,
                    fen: gap.fen_before,
                    san: `Move ${gap.move_number}`,
                    uci: null,
                    openingName: gap.opening_name || 'Opening',
                    eco: gap.eco || '',
                    blunder: gap,
                    opponentName: gap.opponent_name
                }];
                this.currentStepIndex = 0;
            }
        } else {
            this.history = [{
                ply: gap.ply - 1,
                fen: gap.fen_before,
                san: `Move ${gap.move_number}`,
                uci: null,
                openingName: gap.opening_name || 'Opening',
                eco: gap.eco || '',
                blunder: gap,
                opponentName: gap.opponent_name
            }];
            this.currentStepIndex = 0;
        }

        // Update board, breadcrumbs, candidate moves, and blunder banner/arrows
        await this.updatePosition();
        this.notify(`Reviewing move ${gap.move_number}: ${gap.played_san || '?'} → ${gap.best_san || '?'}`, '#f59e0b');
    }

    /**
     * Loads a coverage gap (an unprepared, frequent opponent reply from `treeData.gaps`) onto the
     * board by playing the opponent's move. Reachable from the "🛡 Unprepared Opponent Moves"
     * list in the Blunders/Gaps tab; previously it had zero callers anywhere.
     */
    addressGap(gap) {
        if (!gap) return;
        if (gap.fen_before) {
            return this.addressGameGap(gap);
        }
        const oppUci = gap.opponent_uci || '';
        if (!gap.parent_fen || oppUci.length < 4) {
            this.notify('That gap has no usable position or move.', '#f59e0b');
            return;
        }
        this.activeReviewGame = null;
        this.activeReviewGap = gap;

        const from = oppUci.slice(0, 2);
        const to = oppUci.slice(2, 4);
        const promo = oppUci.length === 5 ? oppUci[4] : undefined;

        this.board.loadFEN(gap.parent_fen);
        const moveRes = this.board.playMove(from, to, promo, false);

        this.history = [
            {
                ply: 0,
                fen: gap.parent_fen,
                san: 'Position',
                uci: null,
                openingName: gap.opening_name || 'Line',
                eco: gap.eco || ''
            }
        ];

        if (moveRes) {
            this.history.push({
                ply: 1,
                fen: this.board.getFEN(),
                san: moveRes.san,
                uci: oppUci,
                openingName: gap.opening_name || 'Line',
                eco: gap.eco || ''
            });
            this.currentStepIndex = 1;
        } else {
            // The opponent move is not legal from this position (stale explorer data): stay on the
            // parent so the user can still prepare a reply manually.
            this.currentStepIndex = 0;
            this.notify(`Could not replay ${gap.opponent_san || oppUci} — showing the parent position.`, '#f59e0b');
        }

        this.updatePosition();
    }

    switchSidebarTab(tab) {
        this.activeSidebarTab = tab;
        const btnTree = document.getElementById('btnTabRepLinesTree');
        const btnGames = document.getElementById('btnTabRepGames');
        const btnGaps = document.getElementById('btnTabRepCoverageGaps');
        const treeActions = document.getElementById('repTreeHeaderActions');
        const gamesActions = document.getElementById('repGamesHeaderActions');
        const gapsActions = document.getElementById('repGapsHeaderActions');
        const treeContainer = document.getElementById('repTreeContainer');
        const gamesContainer = document.getElementById('repGamesContainer');
        const gapsContainer = document.getElementById('repGapsContainer');

        if (btnTree) btnTree.classList.toggle('active', tab === 'tree');
        if (btnGames) btnGames.classList.toggle('active', tab === 'games');
        if (btnGaps) btnGaps.classList.toggle('active', tab === 'gaps');
        if (treeActions) treeActions.style.display = (tab === 'tree') ? 'flex' : 'none';
        if (gamesActions) gamesActions.style.display = (tab === 'games') ? 'flex' : 'none';
        if (gapsActions) gapsActions.style.display = (tab === 'gaps') ? 'flex' : 'none';
        if (treeContainer) treeContainer.style.display = (tab === 'tree') ? 'flex' : 'none';
        if (gamesContainer) gamesContainer.style.display = (tab === 'games') ? 'flex' : 'none';
        if (gapsContainer) gapsContainer.style.display = (tab === 'gaps') ? 'flex' : 'none';

        if (tab === 'tree') {
            this.renderTreeLines();
            this.scheduleOverlapCheck();
        } else if (tab === 'games') {
            this.loadRecentGames();
        } else if (tab === 'gaps') {
            this.renderGapLists();
        }
    }

    syncLineTreeNode() {
        if (!this.lineTree) return;
        if (this.currentStepIndex === 0) {
            this.lineTree.currentNode = this.lineTree.root;
            return;
        }
        let curr = this.lineTree.root;
        for (let i = 1; i <= this.currentStepIndex; i++) {
            const step = this.history[i];
            if (!step || !step.uci) break;
            let child = curr.children.find(c => (c.move && c.move.uci === step.uci) || (c.fen && c.fen === step.fen));
            if (!child) {
                this.ensureHistoryInLineTree(this.currentStepIndex);
                return;
            }
            curr = child;
        }
        this.lineTree.currentNode = curr;
    }

    ensureHistoryInLineTree(upToIndex) {
        if (!this.lineTree) return;
        let curr = this.lineTree.root;
        for (let i = 1; i <= upToIndex; i++) {
            const step = this.history[i];
            if (!step || !step.uci) break;
            const prevStep = this.history[i - 1];
            let child = curr.children.find(c => (c.move && c.move.uci === step.uci) || (c.fen && c.fen === step.fen));
            if (!child) {
                const moveObj = {
                    fen_before: prevStep ? prevStep.fen : 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
                    fen: step.fen,
                    fen_after: step.fen,
                    uci: step.uci,
                    san: step.san,
                    color: (prevStep && prevStep.fen.split(' ')[1] === 'w') ? 'white' : 'black',
                    move_number: Math.floor((step.ply - 1) / 2) + 1,
                    opening_name: step.openingName || '',
                    eco: step.eco || '',
                    comment: '',
                    is_important: false,
                    arrows: ''
                };
                const repMove = this.findMoveInTree(moveObj.fen_before, step.uci);
                if (repMove) {
                    if (repMove.comment) moveObj.comment = repMove.comment;
                    if (repMove.is_important) moveObj.is_important = true;
                    if (repMove.arrows) moveObj.arrows = repMove.arrows;
                }
                child = new SessionTreeNode(moveObj.fen || moveObj.fen_after, curr, moveObj);
                curr.children.push(child);
            } else {
                if (step.san && (!child.move || !child.move.san)) {
                    if (!child.move) child.move = {};
                    child.move.san = step.san;
                }
                if (step.openingName && child.move && !child.move.opening_name) {
                    child.move.opening_name = step.openingName;
                }
                if (step.eco && child.move && !child.move.eco) {
                    child.move.eco = step.eco;
                }
            }
            curr = child;
        }
        this.lineTree.currentNode = curr;
    }

    updateActiveLineCounts() {
        const lines = this.lineTree.getAllLines();
        const count = lines.length;
        const tabBadge = document.getElementById('repTreeTabCount');
        if (tabBadge) tabBadge.textContent = count;
        const quickLineCount = document.getElementById('repQuickLineCount');
        if (quickLineCount) quickLineCount.textContent = count;
        this.scheduleOverlapCheck();
    }

    scheduleOverlapCheck() {
        if (this.activeOverlapDebounce) {
            clearTimeout(this.activeOverlapDebounce);
        }
        this.activeOverlapDebounce = setTimeout(() => {
            this.activeOverlapDebounce = null;
            // One generation + one abort handle shared by all four POSTs of this batch.
            const { gen, abortCtrl } = this._beginOverlapBatch();
            this.checkActiveLineOverlap(gen, abortCtrl);
            this.checkTreeOverlap(gen, abortCtrl);
        }, 120);
    }

    /**
     * Overlap detection (CODE3-06).
     *
     * Every position update used to fire FOUR un-abortable POSTs with no request identity, and
     * `checkTreeOverlap` identified lines by their DFS index, so a late response could paint the
     * badges of a *different* line. Now: one generation counter for the whole cluster, one abort
     * handle for all four requests, and stable leaf-node ids instead of positional indexes.
     */
    _beginOverlapBatch() {
        const gen = ++this.overlapGen;
        if (this.overlapAbort) {
            this.overlapAbort.abort();
        }
        const abortCtrl = new AbortController();
        this.overlapAbort = abortCtrl;
        return { gen, abortCtrl };
    }

    _isStaleOverlap(gen) {
        return gen !== this.overlapGen;
    }

    _overlapFetch(url, body, abortCtrl) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            signal: abortCtrl.signal
        });
    }

    async checkActiveLineOverlap(gen = null, abortCtrl = null) {
        const strip = document.getElementById('repActiveLineOverlap');
        const activeLine = this.lineTree.getActiveLine();
        if (!activeLine || activeLine.length === 0) {
            if (strip) {
                strip.innerHTML = '<span style="font-size:0.72rem; color:var(--text-muted); font-weight:500;">Repertoire: Starting Position</span>';
                strip.style.display = 'flex';
            }
            return;
        }

        if (gen === null || abortCtrl === null) {
            ({ gen, abortCtrl } = this._beginOverlapBatch());
        }

        const formattedMoves = activeLine.map(m => ({
            fen_before: m.fen_before,
            uci: m.uci,
            san: m.san,
            opening_name: m.opening_name || '',
            eco: m.eco || ''
        }));

        try {
            const [resW, resB] = await Promise.all([
                this._overlapFetch('/api/repertoire/check_line', { repertoire: 'white', moves: formattedMoves }, abortCtrl),
                this._overlapFetch('/api/repertoire/check_line', { repertoire: 'black', moves: formattedMoves }, abortCtrl)
            ]);

            const dataW = await resW.json();
            const dataB = await resB.json();
            if (this._isStaleOverlap(gen)) return;   // a newer navigation already answered

            if (strip) {
                const getBadge = (data, repName) => {
                    const label = this._overlapBadgeLabel(data, repName);
                    return `<span class="scout-overlap-badge ${this._overlapBadgeClass(data)}" title="${this._escapeAttr(this._overlapBadgeTitle(data))}">${this._escapeHtml(label)}</span>`;
                };

                strip.innerHTML = `
                    <span style="font-size:0.72rem; color:var(--text-muted); font-weight:600; flex-shrink:0;">Repertoire:</span>
                    ${getBadge(dataW, 'White')}
                    ${getBadge(dataB, 'Black')}
                `;
                strip.style.display = 'flex';
            }
        } catch (e) {
            if (e.name === 'AbortError') return;
            if (this._isStaleOverlap(gen)) return;
            console.error('Failed to check repertoire active line overlap:', e);
        }
    }

    async checkTreeOverlap(gen = null, abortCtrl = null) {
        const lines = this.lineTree.getAllLines();
        if (!lines || lines.length === 0) return;

        if (gen === null || abortCtrl === null) {
            ({ gen, abortCtrl } = this._beginOverlapBatch());
        }

        // Stable ids: the leaf node's own id, not its DFS position.
        const payloadLines = lines.map((l) => ({
            id: String(l.leafNode ? l.leafNode.id : ''),
            moves: l.moves.map(m => ({
                fen_before: m.fen_before,
                uci: m.uci,
                san: m.san,
                opening_name: m.opening_name || '',
                eco: m.eco || ''
            }))
        }));

        try {
            const [resW, resB] = await Promise.all([
                this._overlapFetch('/api/repertoire/check_tree', { repertoire: 'white', lines: payloadLines }, abortCtrl),
                this._overlapFetch('/api/repertoire/check_tree', { repertoire: 'black', lines: payloadLines }, abortCtrl)
            ]);

            const listW = await resW.json();
            const listB = await resB.json();
            if (this._isStaleOverlap(gen)) return;

            this.treeOverlapMap = { white: {}, black: {} };
            if (Array.isArray(listW)) {
                listW.forEach(item => { this.treeOverlapMap.white[item.id] = item; });
            }
            if (Array.isArray(listB)) {
                listB.forEach(item => { this.treeOverlapMap.black[item.id] = item; });
            }

            this._applyTreeBadges(lines);
        } catch (e) {
            if (e.name === 'AbortError') return;
            if (this._isStaleOverlap(gen)) return;
            console.error('Failed to check repertoire tree overlap:', e);
        }
    }

    /* ---- Canonical overlap badge rendering (CODE3-18 cluster 1: these were duplicated, and the
       two copies had already diverged - one mutated live DOM, the other regenerated innerHTML).
       Both `renderTreeLines()` and `_applyTreeBadges()` now call these helpers. ---- */

    _overlapBadgeClass(data) {
        if (!data) return 'rep-new';
        if (data.status === 'already_present') return 'rep-present';
        if (data.status === 'partially_present') return 'rep-partial';
        return 'rep-new';
    }

    _overlapBadgeLabel(data, repName) {
        if (!data) return `★ ${repName}: New`;
        if (data.status === 'already_present') return `✓ ${repName} (${data.matched_plies}/${data.total_plies})`;
        if (data.status === 'partially_present') return `⚡ ${repName}: ${data.matched_plies}/${data.total_plies} in Rep (+${data.new_plies} new)`;
        return `★ ${repName}: New`;
    }

    _overlapBadgeTitle(data) {
        return (data && data.message) ? data.message : 'Not present in this repertoire';
    }

    _overlapBadgesHtml(wData, bData) {
        let html = '';
        if (wData) {
            html += `<span class="scout-overlap-badge ${this._overlapBadgeClass(wData)}" title="${this._escapeAttr(this._overlapBadgeTitle(wData))}">${this._escapeHtml(this._overlapBadgeLabel(wData, 'White'))}</span>`;
        }
        if (bData) {
            html += `<span class="scout-overlap-badge ${this._overlapBadgeClass(bData)}" title="${this._escapeAttr(this._overlapBadgeTitle(bData))}">${this._escapeHtml(this._overlapBadgeLabel(bData, 'Black'))}</span>`;
        }
        return html;
    }

    _importButtonState(data, repName) {
        if (data && data.status === 'already_present') {
            return { text: `✓ In ${repName} (${data.matched_plies}p)`, title: data.message };
        }
        if (data && data.status === 'partially_present') {
            return { text: `📥 Extend ${repName} (+${data.new_plies})`, title: data.message };
        }
        return { text: `📥 Line → ${repName}`, title: `Save this whole line into ${repName} Repertoire` };
    }

    _applyTreeBadges(lines) {
        const list = document.getElementById('repVariationsList');
        if (!list) return;
        if (!Array.isArray(lines)) return;

        const cards = list.querySelectorAll('.scout-var-card');
        cards.forEach((card) => {
            const idx = parseInt(card.getAttribute('data-idx'), 10);
            const line = lines[idx];
            if (!line) return;
            const key = String(line.leafNode ? line.leafNode.id : '');
            const wData = this.treeOverlapMap?.white?.[key];
            const bData = this.treeOverlapMap?.black?.[key];

            let badgesWrap = card.querySelector('.scout-card-overlap-badges');
            if (!badgesWrap) {
                badgesWrap = document.createElement('div');
                badgesWrap.className = 'scout-card-overlap-badges';
                badgesWrap.style.cssText = 'display:flex; gap:0.35rem; align-items:center; margin-top:0.35rem; flex-wrap:wrap;';
                const pgnEl = card.querySelector('.scout-var-pgn');
                if (pgnEl) card.insertBefore(badgesWrap, pgnEl.nextSibling);
            }
            badgesWrap.innerHTML = this._overlapBadgesHtml(wData, bData);

            const btnW = card.querySelector('.btn-import-rep-tree-white');
            if (btnW && wData) {
                const st = this._importButtonState(wData, 'White');
                btnW.textContent = st.text;
                btnW.title = st.title;
            }
            const btnB = card.querySelector('.btn-import-rep-tree-black');
            if (btnB && bData) {
                const st = this._importButtonState(bData, 'Black');
                btnB.textContent = st.text;
                btnB.title = st.title;
            }
        });
    }

    /**
     * Markup for one "Explored Line" card. Extracted from `renderTreeLines()` purely so it can be
     * rendered (and audited) on its own — the produced markup is unchanged.
     *
     * SEC-J: `line.name` / `line.eco` are `opening_name` / `eco` values that reached the session
     * tree from a `/api/games/recent` row or from the repertoire DB, and `line.pgn` is assembled
     * from those moves' SAN/UCI — all untrusted, so all three are escaped. `badgesHtml` and the two
     * import-button labels are deliberately NOT escaped here: they are produced by
     * `_overlapBadgesHtml()` / `_importButtonState()` and escaped exactly once, inside those
     * helpers (escaping them again here is the double-escaping bug this comment guards against).
     */
    _treeLineCardHtml(line, idx, activeUcis) {
        const moves = (line && line.moves) ? line.moves : [];
        const lineUcis = moves.map(m => m.uci).join(' ');
        const isActive = Boolean(activeUcis && (lineUcis === activeUcis || lineUcis.startsWith(activeUcis)));

        // Stable, node-derived id (CODE3-06) - a positional index would let a stale response
        // repaint a different line's badges.
        const overlapKey = String(line.leafNode ? line.leafNode.id : '');
        const wData = this.treeOverlapMap?.white?.[overlapKey];
        const bData = this.treeOverlapMap?.black?.[overlapKey];

        const badgesHtml = this._overlapBadgesHtml(wData, bData);

        const btnWhite = this._importButtonState(wData, 'White');
        const btnBlack = this._importButtonState(bData, 'Black');

        const showDelWhite = wData && (wData.status === 'already_present' || wData.status === 'partially_present');
        const showDelBlack = bData && (bData.status === 'already_present' || bData.status === 'partially_present');

        const hasNotes = moves.some(m => m.comment && m.comment.trim());
        const hasImportant = moves.some(m => m.is_important);

        return `
            <div class="scout-var-card ${isActive ? 'active' : ''}" data-idx="${idx}">
                <div class="scout-var-header">
                    <div style="display:flex; align-items:center; gap:0.4rem; overflow:hidden;">
                        <span class="scout-var-badge">#${idx + 1}</span>
                        <span style="font-weight:700; color:#fff; font-size:0.83rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                            ${this._escapeHtml(line.name || 'Explored Line')}
                        </span>
                        ${line.eco ? `<span style="font-size:0.7rem; color:var(--accent-primary); font-weight:700;">${this._escapeHtml(line.eco)}</span>` : ''}
                    </div>
                    <div style="display:flex; align-items:center; gap:0.35rem; flex-shrink:0;">
                        ${hasNotes ? '<span title="Contains move notes" style="cursor:help; font-size:0.75rem;">📝</span>' : ''}
                        ${hasImportant ? '<span title="Contains priority moves" style="cursor:help; font-size:0.75rem;">⭐</span>' : ''}
                        <span class="scout-var-plies">${this._num(line.plyCount, 0)} plies</span>
                        ${isActive ? '<span class="scout-var-status">Active</span>' : ''}
                    </div>
                </div>
                <div class="scout-var-pgn" title="${this._escapeAttr(line.pgn)}">${this._escapeHtml(line.pgn)}</div>
                <div class="scout-card-overlap-badges" style="display:flex; gap:0.35rem; align-items:center; margin-top:0.35rem; flex-wrap:wrap;">
                    ${badgesHtml}
                </div>
                <div class="scout-var-actions" style="display:flex; gap:0.3rem; align-items:center; flex-wrap:wrap; margin-top:0.4rem;">
                    <button class="btn-var-action btn-load-rep-tree-line" data-idx="${idx}" title="Load this line onto the board">
                        👀 Load
                    </button>
                    <button class="btn-var-action btn-import-rep-tree-white" data-idx="${idx}" title="${this._escapeAttr(btnWhite.title)}">
                        ${this._escapeHtml(btnWhite.text)}
                    </button>
                    <button class="btn-var-action btn-import-rep-tree-black" data-idx="${idx}" title="${this._escapeAttr(btnBlack.title)}">
                        ${this._escapeHtml(btnBlack.text)}
                    </button>
                    ${showDelWhite ? `
                        <button class="btn-var-action btn-del-rep-tree-white" data-idx="${idx}" title="Remove line from White Repertoire" style="color:#ef4444; border-color:rgba(239,68,68,0.35);">
                            🗑️ White
                        </button>
                    ` : ''}
                    ${showDelBlack ? `
                        <button class="btn-var-action btn-del-rep-tree-black" data-idx="${idx}" title="Remove line from Black Repertoire" style="color:#ef4444; border-color:rgba(239,68,68,0.35);">
                            🗑️ Black
                        </button>
                    ` : ''}
                    <button class="btn-var-action btn-del-rep-session-line" data-idx="${idx}" title="Delete this line from session tree" style="color:var(--text-muted); padding:0.25rem 0.45rem;">
                        ✕
                    </button>
                </div>
            </div>
        `;
    }

    renderTreeLines() {
        const list = document.getElementById('repVariationsList');
        const tabBadge = document.getElementById('repTreeTabCount');
        const quickCount = document.getElementById('repQuickLineCount');

        const lines = this.lineTree.getAllLines();
        const count = lines.length;

        if (tabBadge) tabBadge.textContent = count;
        if (quickCount) quickCount.textContent = count;

        if (!list) return;

        if (count === 0) {
            list.innerHTML = `
                <div style="color:var(--text-muted); padding:1.5rem 1rem; text-align:center; display:flex; flex-direction:column; align-items:center; gap:0.5rem;">
                    <div style="font-size:2rem; opacity:0.6;">♟️</div>
                    <div style="font-weight:600; color:var(--text-primary); font-size:0.85rem;">No lines explored yet</div>
                    <div style="font-size:0.75rem; line-height:1.45; max-width:260px;">
                        Play moves on the board or click candidate moves. All variations and branches you explore will appear here automatically.
                    </div>
                </div>
            `;
            return;
        }

        const activeLine = this.lineTree.getActiveLine();
        const activeUcis = activeLine.map(m => m.uci).join(' ');

        list.innerHTML = lines
            .map((line, idx) => this._treeLineCardHtml(line, idx, activeUcis))
            .join('');

        // Attach event listeners for each card
        list.querySelectorAll('.btn-load-rep-tree-line').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const idx = parseInt(btn.getAttribute('data-idx'), 10);
                if (lines[idx]) this.loadTreeLine(lines[idx]);
            });
        });
        list.querySelectorAll('.btn-import-rep-tree-white').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const idx = parseInt(btn.getAttribute('data-idx'), 10);
                if (!lines[idx]) return;
                if (btn.disabled) return;
                btn.disabled = true;
                try {
                    await this.importToRepertoire('white', lines[idx].moves);
                } finally {
                    if (btn.isConnected) btn.disabled = false;
                }
            });
        });
        list.querySelectorAll('.btn-import-rep-tree-black').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const idx = parseInt(btn.getAttribute('data-idx'), 10);
                if (!lines[idx]) return;
                if (btn.disabled) return;
                btn.disabled = true;
                try {
                    await this.importToRepertoire('black', lines[idx].moves);
                } finally {
                    if (btn.isConnected) btn.disabled = false;
                }
            });
        });
        list.querySelectorAll('.btn-del-rep-tree-white').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const idx = parseInt(btn.getAttribute('data-idx'), 10);
                if (lines[idx]) this.removeTreeLineFromRepertoire(lines[idx], 'white');
            });
        });
        list.querySelectorAll('.btn-del-rep-tree-black').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const idx = parseInt(btn.getAttribute('data-idx'), 10);
                if (lines[idx]) this.removeTreeLineFromRepertoire(lines[idx], 'black');
            });
        });
        list.querySelectorAll('.btn-del-rep-session-line').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const idx = parseInt(btn.getAttribute('data-idx'), 10);
                if (lines[idx]) this.deleteSessionLine(lines[idx]);
            });
        });
    }

    loadTreeLine(line) {
        if (!line || !line.moves) return;

        const startFen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
        this.history = [{
            ply: 0,
            fen: startFen,
            san: 'Start',
            uci: null,
            openingName: this.activeColor === 'white' ? 'White Opening Repertoire' : 'Black Opening Repertoire',
            eco: ''
        }];

        for (let i = 0; i < line.moves.length; i++) {
            const m = line.moves[i];
            this.history.push({
                ply: i + 1,
                fen: m.fen || m.fen_after,
                san: m.san,
                uci: m.uci,
                openingName: m.opening_name || line.name || '',
                eco: m.eco || line.eco || '',
                blunder: null
            });
        }

        this.currentStepIndex = this.history.length - 1;
        this.lineTree.currentNode = line.leafNode;
        this.updatePosition();
        this.showQuickNotice(`Loaded: ${line.name || 'Variation'} (${line.plyCount} plies)`, '#38bdf8');
    }

    async importToRepertoire(repertoire, specificLineMoves = null) {
        let movesToImport = specificLineMoves;
        if (!movesToImport || movesToImport.length === 0) {
            movesToImport = this.lineTree.getActiveLine();
        }
        if (!movesToImport || movesToImport.length === 0) {
            this.notify('No moves played to import yet!', '#ef4444');
            return;
        }

        // Mandatory Study Selection/Naming. The helper lives in app.js, which is the LAST script
        // tag: without the typeof guard a partial/failed load produced an *unhandled promise
        // rejection* and a completely silent no-op on a primary action button (CODE3-09).
        let studyName = this.currentStudy;
        if (!studyName) {
            if (typeof window.promptStudySelection !== 'function') {
                this.notify('Study selection is unavailable (app.js failed to load) — cannot import.', '#ef4444');
                return;
            }
            try {
                const firstWithOpening = movesToImport.find(m => m.opening_name && m.opening_name !== 'Start Position');
                const defName = firstWithOpening ? firstWithOpening.opening_name : '';
                studyName = await window.promptStudySelection({
                    repertoire: repertoire,
                    defaultName: defName,
                    title: `Save Line to ${repertoire.charAt(0).toUpperCase() + repertoire.slice(1)} Study`
                });
            } catch (e) {
                this.notify(`Study selection failed: ${e.message}`, '#ef4444');
                return;
            }
            if (!studyName) return; // User cancelled
            this.currentStudy = studyName;
        }

        const importKey = `importLine:${repertoire}:${movesToImport.length}:${movesToImport[0]?.uci || ''}`;
        if (!this.beginWrite(importKey)) return;   // CODE3-12 latch

        const formattedMoves = movesToImport.map(m => ({
            fen_before: m.fen_before,
            uci: m.uci,
            san: m.san,
            opening_name: studyName || m.opening_name || '',
            eco: m.eco || '',
            comment: m.comment || '',
            is_important: Boolean(m.is_important),
            arrows: m.arrows || ''
        }));

        try {
            const res = await fetch('/api/repertoire/import_line', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repertoire: repertoire,
                    moves: formattedMoves,
                    study_name: studyName
                })
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            // FIXER-I (SCOUT-11) made the import endpoint truthful: `already_present` is a
            // successful no-op (AGENTS.md §4E: "prevents redundant writes") and `error` is
            // the only terminal failure. Keying the guard on `'success'` alone answered a
            // red "Import: …" warning for a legitimate double-click.
            if (data && (data.status === 'success' || data.status === 'already_present')) {
                const repCap = repertoire.charAt(0).toUpperCase() + repertoire.slice(1);
                const isAlreadyPresent = !!data.already_present;
                const errors = Array.isArray(data.errors) ? data.errors.filter(Boolean) : [];
                // `inserted_count` = rows that did not exist before this call;
                // `imported_count` also counts idempotent updates (e.g. a comment /
                // arrows / study_name re-applied to a stored move).
                const inserted = (data.inserted_count === undefined || data.inserted_count === null)
                    ? this._num(data.imported_count, 0)
                    : this._num(data.inserted_count, 0);
                const touched = this._num(data.imported_count, 0);
                let successMsg;
                if (isAlreadyPresent) {
                    successMsg = `✓ In ${repCap} Repertoire (${studyName}) — nothing to add, all ${this._num(data.total_moves, 0)} plies already stored`;
                } else {
                    successMsg = `✓ Added ${inserted} ${inserted === 1 ? 'move' : 'moves'} to ${repCap} Repertoire (${studyName})`;
                    if (touched > inserted) {
                        successMsg += ` — ${this._num(touched - inserted, 0)} already stored, updated`;
                    }
                }
                this.notify(successMsg, isAlreadyPresent ? '#38bdf8' : '#4ade80');
                if (errors.length) {
                    this.notify(`⚠ ${errors.length} move(s) skipped: ${errors.join(' | ')}`, '#f59e0b', 6000);
                }
                if (window.chessAudio) window.chessAudio.playSuccess();
                await this.loadStudies();
                await this.refreshTree();
                this.scheduleOverlapCheck();
            } else {
                const errs = Array.isArray(data && data.errors) ? data.errors.filter(Boolean) : [];
                const detail = errs.length
                    ? `${(data && data.message) || 'nothing was stored'} — ${errs.join(' | ')}`
                    : ((data && data.message) || 'Check moves format');
                this.notify(`Import failed: ${detail}`, '#ef4444');
            }
        } catch (e) {
            console.error('Repertoire import error:', e);
            this.notify(`Error: ${e.message}`, '#ef4444');
        } finally {
            this.endWrite(importKey);
        }
    }

    async importAllLinesToRepertoire(repertoire) {
        const allMoves = this.lineTree.getAllUniqueMoves();
        if (!allMoves || allMoves.length === 0) {
            this.notify('No explored lines in tree to import!', '#ef4444');
            return;
        }

        // Mandatory Study Selection/Naming (see the typeof-guard note in importToRepertoire).
        let studyName = this.currentStudy;
        if (!studyName) {
            if (typeof window.promptStudySelection !== 'function') {
                this.notify('Study selection is unavailable (app.js failed to load) — cannot import.', '#ef4444');
                return;
            }
            try {
                studyName = await window.promptStudySelection({
                    repertoire: repertoire,
                    title: `Save All Lines to ${repertoire.charAt(0).toUpperCase() + repertoire.slice(1)} Study`
                });
            } catch (e) {
                this.notify(`Study selection failed: ${e.message}`, '#ef4444');
                return;
            }
            if (!studyName) return; // User cancelled
            this.currentStudy = studyName;
        }

        const importKey = `importAllLines:${repertoire}:${allMoves.length}`;
        if (!this.beginWrite(importKey)) return;   // CODE3-12 latch

        const formattedMoves = allMoves.map(m => ({
            fen_before: m.fen_before,
            uci: m.uci,
            san: m.san,
            opening_name: studyName || m.opening_name || '',
            eco: m.eco || '',
            comment: m.comment || '',
            is_important: Boolean(m.is_important),
            arrows: m.arrows || ''
        }));

        try {
            const res = await fetch('/api/repertoire/import_line', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repertoire: repertoire,
                    moves: formattedMoves,
                    study_name: studyName
                })
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            // Same truthful three-status contract as the single-line import (FIXER-I /
            // SCOUT-11): `already_present` is a successful no-op, `error` is the failure.
            if (data && (data.status === 'success' || data.status === 'already_present')) {
                const repCap = repertoire.charAt(0).toUpperCase() + repertoire.slice(1);
                const isAlreadyPresent = !!data.already_present;
                const errors = Array.isArray(data.errors) ? data.errors.filter(Boolean) : [];
                const inserted = (data.inserted_count === undefined || data.inserted_count === null)
                    ? this._num(data.imported_count, 0)
                    : this._num(data.inserted_count, 0);
                const touched = this._num(data.imported_count, 0);
                let msg;
                if (isAlreadyPresent) {
                    msg = `✓ All lines already in ${repCap} Repertoire (${studyName}) — nothing to add`;
                } else {
                    msg = `✓ Added all lines (${inserted} ${inserted === 1 ? 'move' : 'moves'}) to ${repCap} Repertoire (${studyName})`;
                    if (touched > inserted) {
                        msg += ` — ${this._num(touched - inserted, 0)} already stored, updated`;
                    }
                }
                this.notify(msg, isAlreadyPresent ? '#38bdf8' : '#4ade80');
                if (errors.length) {
                    this.notify(`⚠ ${errors.length} move(s) skipped: ${errors.join(' | ')}`, '#f59e0b', 6000);
                }
                if (window.chessAudio) window.chessAudio.playSuccess();
                await this.loadStudies();
                await this.refreshTree();
                this.scheduleOverlapCheck();
            } else {
                const errs = Array.isArray(data && data.errors) ? data.errors.filter(Boolean) : [];
                const detail = errs.length
                    ? `${(data && data.message) || 'nothing was stored'} — ${errs.join(' | ')}`
                    : ((data && data.message) || 'Check moves format');
                this.notify(`Import failed: ${detail}`, '#ef4444');
            }
        } catch (e) {
            console.error('Import all lines error:', e);
            this.notify(`Error: ${e.message}`, '#ef4444');
        } finally {
            this.endWrite(importKey);
        }
    }

    async removeTreeLineFromRepertoire(line, repertoire) {
        if (!line || !line.moves || line.moves.length === 0) return;
        const repName = repertoire.charAt(0).toUpperCase() + repertoire.slice(1);
        const lastMove = line.moves[line.moves.length - 1];
        const confirmed = confirm(
            `⚠️ Remove move ${lastMove.san || lastMove.uci} and following lines from your ${repName} repertoire?`
        );
        if (!confirmed) return;

        const key = `removeTreeLine:${repertoire}:${lastMove.uci}`;
        if (!this.beginWrite(key)) return;

        try {
            const res = await fetch('/api/repertoire/move', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repertoire: repertoire,
                    parent_fen: lastMove.fen_before,
                    move_uci: lastMove.uci
                })
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            if (data && data.status === 'ok') {
                if (window.chessAudio) window.chessAudio.playSuccess();
                this.notify(`✓ Removed from ${repName} Repertoire`, '#4ade80');
                await this.refreshTree();
                this.scheduleOverlapCheck();
            } else {
                this.notify((data && data.message) || `Could not remove from ${repName} repertoire.`, '#f59e0b');
            }
        } catch (e) {
            console.error('Error removing tree line from repertoire:', e);
            this.notify(`Could not remove from ${repName} repertoire: ${e.message}`, '#ef4444');
        } finally {
            this.endWrite(key);
        }
    }

    deleteSessionLine(line) {
        if (!line || !line.leafNode) return;
        this.lineTree.deleteLine(line.leafNode);
        this.renderTreeLines();
        this.updateActiveLineCounts();
        this.showQuickNotice('Line removed from session tree', '#94a3b8');
    }

    clearSessionTree() {
        const count = this.lineTree.getAllLines().length;
        if (count === 0) {
            this.showQuickNotice('Tree is already empty', '#94a3b8');
            return;
        }
        if (!confirm(`Clear all ${count} explored lines from the session tree?`)) {
            return;
        }
        this.lineTree.reset();
        this.renderTreeLines();
        this.updateActiveLineCounts();
        this.showQuickNotice('Session line tree cleared', '#94a3b8');
    }

    showQuickNotice(msg, color = '#4ade80') {
        if (typeof window.showToast === 'function') {
            window.showToast(msg, color);
        }
    }
}

window.repertoireBuilder = new RepertoireBuilder();
