// Repertoire Studio — FSRS-5 Active-Recall Repertoire Trainer
class RepertoireTrainer {
    constructor() {
        this.activeColor = 'mixed';
        this.board = null;
        this.currentDrill = null;
        this.currentFen = null;
        this.streak = 0;
        this.practicedCount = 0;
        this.correctCount = 0;
        this.isWaitingForUser = false;
        this.isInitialized = false;
        this.openingFilter = null;

        // 20-Move Practice Session State
        this.sessionTarget = 20;
        this.sessionIndex = 0;
        this.sessionCorrect = 0;
        this.sessionMistakes = 0;
        this.hasAnsweredCurrentPosition = false;
        this.autoNextTimeout = null;
        this.drillToken = 0;
        this.opponentReplyTimeout = null;
        this.mistakeResetTimeout = null;
        this.leadInTimeout = null;
        this.progressSeq = 0;
        this.openingsSeq = 0;

        // FSRS-5 Latency & Active-Recall State
        this.drillReadyAt = 0;
        this.pendingMoveNote = '';

        // Pedagogical Curriculum State
        this.sessionMode = 'adaptive';
        this.sessionLapses = [];
        this.sessionReinforcementQueue = [];
        this.sessionLapseClearCounts = {};
        this.sessionDrilledIds = [];
        this.solutionArrowFadeTimeout = null;

        this.currentPositionHasMistake = false;
        this.currentPositionUsedHint = false;
        this.isReinforcementPhase = false;
        this.reinforcementTotal = 0;

        // Game Deviations State
        this.activeTrainerTab = 'drills';
        this.deviationsFilterColor = 'all';
        this.deviationsSearch = '';
        this.deviationsData = null;
        this.activeDrillDeviation = null;
        this.currentDeviationIndex = 0;
        this.totalDeviationsCount = 0;

        // Line Replay State
        this.lineSteps = [];
        this.lineIndex = 0;
        this.replayMode = false;
        this.replayPlaying = false;
        this.replayTimer = null;
        this.replayDelayMs = 650;
        this.replaySavedWaiting = false;
    }

    _nowMs() {
        return (typeof performance !== 'undefined' && typeof performance.now === 'function')
            ? performance.now()
            : Date.now();
    }

    init() {
        if (this.isInitialized) return;

        const boardEl = document.getElementById('trainerBoard');
        if (!boardEl) return;

        this.board = new InteractiveChessboard(boardEl, {
            orientation: 'white',
            interactive: true,
            onMove: (from, to, promo, moveRes) => this.onUserAttempt(from, to, promo, moveRes)
        });

        const btnTabDrills = document.getElementById('btnTrainerTabDrills');
        const btnTabDeviations = document.getElementById('btnTrainerTabDeviations');
        if (btnTabDrills) btnTabDrills.addEventListener('click', () => this.switchTrainerTab('drills'));
        if (btnTabDeviations) btnTabDeviations.addEventListener('click', () => this.switchTrainerTab('deviations'));

        const btnDevPrev = document.getElementById('btnDevPrev');
        const btnDevNext = document.getElementById('btnDevNext');
        if (btnDevPrev) btnDevPrev.addEventListener('click', () => this.prevDeviation());
        if (btnDevNext) btnDevNext.addEventListener('click', () => this.nextDeviation());

        const devFilterBtns = [
            { id: 'btnFilterDevAll', filter: 'all' },
            { id: 'btnFilterDevWhite', filter: 'white' },
            { id: 'btnFilterDevBlack', filter: 'black' }
        ];
        devFilterBtns.forEach(({ id, filter }) => {
            const btn = document.getElementById(id);
            if (btn) {
                btn.addEventListener('click', () => {
                    this.deviationsFilterColor = filter;
                    devFilterBtns.forEach(b => {
                        const el = document.getElementById(b.id);
                        if (el) el.classList.toggle('active', b.filter === filter);
                    });
                    this.loadGameDeviations();
                });
            }
        });
        const devSearchInput = document.getElementById('inputDeviationsSearch');
        if (devSearchInput) {
            devSearchInput.addEventListener('input', (e) => {
                this.deviationsSearch = e.target.value.trim();
                this.renderDeviationsList();
            });
        }
        const btnRefreshDev = document.getElementById('btnRefreshDeviations');
        if (btnRefreshDev) {
            btnRefreshDev.addEventListener('click', () => this.loadGameDeviations());
        }

        const btnMixed = document.getElementById('btnTrainMixed');
        const btnWhite = document.getElementById('btnTrainWhite');
        const btnBlack = document.getElementById('btnTrainBlack');
        if (btnMixed) btnMixed.addEventListener('click', () => this.switchColor('mixed'));
        if (btnWhite) btnWhite.addEventListener('click', () => this.switchColor('white'));
        if (btnBlack) btnBlack.addEventListener('click', () => this.switchColor('black'));

        const btnNew = document.getElementById('btnTrainNewDrill');
        const btnHint = document.getElementById('btnTrainHint');
        const btnFlip = document.getElementById('btnTrainFlip');
        const btnRetry = document.getElementById('btnTrainRetry');
        const filterSelect = document.getElementById('trainOpeningFilter');
        const btnClearFocus = document.getElementById('btnClearTrainFocus');

        if (btnNew) {
            btnNew.addEventListener('click', () => {
                if (this.sessionMode === 'deviations') {
                    this.nextDeviation();
                } else {
                    this.startNewDrill();
                }
            });
        }
        if (btnHint) btnHint.addEventListener('click', () => this.showHint());
        if (btnFlip) {
            btnFlip.addEventListener('click', () => {
                const newOr = this.board.options.orientation === 'white' ? 'black' : 'white';
                this.board.setOrientation(newOr);
            });
        }
        if (btnRetry) btnRetry.addEventListener('click', () => this.retryCurrentPosition());
        if (filterSelect) {
            filterSelect.addEventListener('change', (e) => {
                this.openingFilter = e.target.value.trim() || null;
                this.restartSession(false);
                this.startNewDrill();
            });
        }
        const modeSelect = document.getElementById('trainModeSelect');
        if (modeSelect) {
            modeSelect.addEventListener('change', (e) => {
                this.sessionMode = e.target.value || 'adaptive';
                const isDev = (this.sessionMode === 'deviations');
                this.activeTrainerTab = isDev ? 'deviations' : 'drills';
                const btnDrills = document.getElementById('btnTrainerTabDrills');
                const btnDeviations = document.getElementById('btnTrainerTabDeviations');
                if (btnDrills) btnDrills.classList.toggle('active', !isDev);
                if (btnDeviations) btnDeviations.classList.toggle('active', isDev);
                if (isDev) this.currentDeviationIndex = 0;
                this.restartSession(false);
                this.startNewDrill({ index: this.currentDeviationIndex });
            });
        }
        if (btnClearFocus) {
            btnClearFocus.addEventListener('click', () => this.clearOpeningFocus());
        }
        const btnToggleArrows = document.getElementById('btnTrainToggleArrows');
        if (btnToggleArrows) {
            btnToggleArrows.addEventListener('click', () => {
                if (this.board.arrows && this.board.arrows.length) {
                    this.board.clearArrows();
                } else if (this.savedPlanArrows && this.savedPlanArrows.length) {
                    this.board.setArrows(this.savedPlanArrows);
                }
            });
        }

        const btnReplay = document.getElementById('btnTrainReplay');
        if (btnReplay) btnReplay.addEventListener('click', () => this.startReplay());
        const btnReplayStart = document.getElementById('btnTrainReplayStart');
        if (btnReplayStart) btnReplayStart.addEventListener('click', () => this.replayJumpTo(0));
        const btnReplayPrev = document.getElementById('btnTrainReplayPrev');
        if (btnReplayPrev) btnReplayPrev.addEventListener('click', () => this.replayStep(-1));
        const btnReplayNext = document.getElementById('btnTrainReplayNext');
        if (btnReplayNext) btnReplayNext.addEventListener('click', () => this.replayStep(1));
        const btnReplayExit = document.getElementById('btnTrainReplayExit');
        if (btnReplayExit) btnReplayExit.addEventListener('click', () => this.exitReplay());

        const btnRestartSession = document.getElementById('btnTrainRestartSession');
        if (btnRestartSession) {
            btnRestartSession.addEventListener('click', () => this.restartSession(true));
        }

        const btnModalReinforce = document.getElementById('btnModalReinforceSession');
        if (btnModalReinforce) {
            btnModalReinforce.addEventListener('click', () => {
                this.closeSessionModal();
                this.startReinforcementPhase();
            });
        }

        const btnModalNext = document.getElementById('btnModalNextSession');
        if (btnModalNext) {
            btnModalNext.addEventListener('click', () => {
                this.closeSessionModal();
                this.restartSession(true);
            });
        }

        const btnModalClose = document.getElementById('btnModalCloseSession');
        if (btnModalClose) {
            btnModalClose.addEventListener('click', () => this.closeSessionModal());
        }

        window.addEventListener('keydown', (e) => {
            const tag = e.target?.tagName?.toLowerCase();
            if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target?.isContentEditable) {
                return;
            }
            const trainerView = document.getElementById('viewRepTrainer');
            if (!trainerView || !trainerView.classList.contains('active')) {
                return;
            }
            if (e.ctrlKey || e.metaKey || e.altKey) return;
            this.handleTrainerKey(e);
        });

        this.updateSessionUI();
        this.updateDeviationsCountBadge();
        this.isInitialized = true;
    }

    async updateDeviationsCountBadge() {
        try {
            const colorParam = (this.activeColor === 'white' || this.activeColor === 'black') ? `?color=${this.activeColor}` : '';
            const sep = colorParam ? '&' : '?';
            const res = await fetch(`/api/repertoire/deviations${colorParam}${sep}limit=1`);
            if (res.ok) {
                const data = await res.json();
                const badge = document.getElementById('trainDeviationsBadge');
                if (badge) badge.textContent = data.games_with_deviations || data.total_deviations || 0;
                this.totalDeviationsCount = data.total_deviations || 0;
            }
        } catch (_) {}
    }

    prevDeviation() {
        if (this.sessionMode !== 'deviations') return;
        if (this.totalDeviationsCount > 0) {
            this.currentDeviationIndex = (this.currentDeviationIndex - 1 + this.totalDeviationsCount) % this.totalDeviationsCount;
        } else {
            this.currentDeviationIndex = Math.max(0, this.currentDeviationIndex - 1);
        }
        this.startNewDrill({ index: this.currentDeviationIndex });
    }

    nextDeviation() {
        if (this.sessionMode !== 'deviations') return;
        if (this.totalDeviationsCount > 0) {
            this.currentDeviationIndex = (this.currentDeviationIndex + 1) % this.totalDeviationsCount;
        } else {
            this.currentDeviationIndex++;
        }
        this.startNewDrill({ index: this.currentDeviationIndex });
    }

    showDeviationsCompleteBanner() {
        this.totalDeviationsCount = 0;
        const badge = document.getElementById('trainDeviationsBadge');
        if (badge) badge.textContent = '0';

        const devBanner = document.getElementById('trainDeviationBanner');
        if (devBanner) devBanner.style.display = 'none';
        const sessionStrip = document.querySelector('.trainer-session-strip');
        if (sessionStrip) sessionStrip.style.display = 'flex';

        const promptEl = document.getElementById('trainPrompt');
        if (promptEl) {
            promptEl.innerHTML = `<div class="drill-prompt-headline text-emerald">All Game Deviations Resolved</div>`;
        }

        const feedback = document.getElementById('trainFeedback');
        if (feedback) {
            feedback.style.display = 'block';
            feedback.className = 'feedback-box correct';
            feedback.textContent = 'All game deviations resolved.';
        }
    }

    _normalizeGameOutcome(rawResult, userColor) {
        const r = String(rawResult || '').toLowerCase().trim();
        const u = String(userColor || 'white').toLowerCase().trim();
        if (r === 'win') return 'win';
        if (r === 'loss') return 'loss';
        if (r === 'draw' || r === '1/2-1/2') return 'draw';
        if (r === 'white' || r === '1-0') return u === 'white' ? 'win' : 'loss';
        if (r === 'black' || r === '0-1') return u === 'black' ? 'win' : 'loss';
        return 'loss';
    }

    getDrillColor(drill = this.currentDrill) {
        if (!drill) {
            return (this.activeColor === 'black' ? 'black' : 'white');
        }
        if (drill.repertoire === 'white' || drill.repertoire === 'black') {
            return drill.repertoire;
        }
        if (drill.user_color === 'white' || drill.user_color === 'black') {
            return drill.user_color;
        }
        const fen = drill.parent_fen || drill.fen_before || this.currentFen;
        if (fen) {
            const turn = String(fen).trim().split(/\s+/)[1];
            if (turn === 'b') return 'black';
            if (turn === 'w') return 'white';
        }
        return (this.activeColor === 'black' ? 'black' : 'white');
    }

    async switchColor(color) {
        if (this.activeColor === color && this.currentDrill) return;
        this.activeColor = color;

        const btnMixed = document.getElementById('btnTrainMixed');
        const btnWhite = document.getElementById('btnTrainWhite');
        const btnBlack = document.getElementById('btnTrainBlack');
        if (btnMixed) btnMixed.classList.toggle('active', color === 'mixed');
        if (btnWhite) btnWhite.classList.toggle('active', color === 'white');
        if (btnBlack) btnBlack.classList.toggle('active', color === 'black');

        const colorSpan = document.getElementById('trainColorSpan');
        if (colorSpan) colorSpan.textContent = (color === 'mixed') ? 'White & Black' : color;

        if (color === 'black' || color === 'white') {
            this.board.setOrientation(color);
        }

        this.currentDeviationIndex = 0;
        this.updateDeviationsCountBadge();
        this.restartSession(false);
        await this.startNewDrill({ index: 0 });
    }

    restartSession(startDrill = true) {
        this.sessionIndex = 0;
        this.sessionCorrect = 0;
        this.sessionMistakes = 0;
        this.sessionLapses = [];
        this.sessionReinforcementQueue = [];
        this.sessionLapseClearCounts = {};
        this.sessionDrilledIds = [];
        this.hasAnsweredCurrentPosition = false;
        this.currentPositionHasMistake = false;
        this.currentPositionUsedHint = false;
        this.isReinforcementPhase = false;
        this.reinforcementTotal = 0;
        this._clearDrillTimers();
        this.updateSessionUI();
        this.closeSessionModal();
        if (startDrill) {
            this.startNewDrill();
        }
    }

    _clearDrillTimers() {
        if (this.autoNextTimeout) { clearTimeout(this.autoNextTimeout); this.autoNextTimeout = null; }
        if (this.opponentReplyTimeout) { clearTimeout(this.opponentReplyTimeout); this.opponentReplyTimeout = null; }
        if (this.mistakeResetTimeout) { clearTimeout(this.mistakeResetTimeout); this.mistakeResetTimeout = null; }
        if (this.solutionArrowFadeTimeout) { clearTimeout(this.solutionArrowFadeTimeout); this.solutionArrowFadeTimeout = null; }
        if (this.leadInTimeout) { clearTimeout(this.leadInTimeout); this.leadInTimeout = null; }
        this._stopReplayTimer();
    }

    _recordSessionLapse(id) {
        if (!id) return;
        if (!this.sessionLapses.includes(id)) {
            this.sessionLapses.push(id);
        }
        if (!this.sessionReinforcementQueue.includes(id)) {
            this.sessionReinforcementQueue.push(id);
        }
        this.sessionLapseClearCounts[id] = 0;
    }

    _pendingReinforcementIds() {
        const merged = [...(this.sessionLapses || []), ...(this.sessionReinforcementQueue || [])];
        return [...new Set(merged)];
    }

    _setConditional(el, visible, displayMode = 'inline-flex') {
        if (!el) return;
        if (el.style.display !== displayMode) el.style.display = displayMode;
        el.style.visibility = visible ? 'visible' : 'hidden';
        el.style.opacity = visible ? '1' : '0';
        if ('disabled' in el) el.disabled = !visible;
        el.setAttribute('aria-hidden', visible ? 'false' : 'true');
    }

    _updateNoteBox(comment) {
        const noteBox = document.getElementById('trainNoteBox');
        const noteContent = document.getElementById('trainNoteContent');
        if (!noteBox || !noteContent) return;
        const text = (comment && String(comment).trim()) ? String(comment).trim() : '';
        noteContent.textContent = text;
        noteContent.title = text;
        noteContent.style.whiteSpace = 'normal';
        noteContent.style.overflow = 'hidden';
        noteContent.style.textOverflow = 'ellipsis';
        noteContent.style.display = '-webkit-box';
        noteContent.style.webkitLineClamp = '2';
        noteContent.style.webkitBoxOrient = 'vertical';
        noteContent.style.maxWidth = '100%';

        if (noteBox.style.display !== 'block') noteBox.style.display = 'block';
        noteBox.style.minHeight = '54px';
        noteBox.style.maxHeight = '54px';
        noteBox.style.overflow = 'hidden';
        noteBox.style.visibility = text ? 'visible' : 'hidden';
        noteBox.style.opacity = text ? '1' : '0';
        noteBox.style.transition = 'opacity 0.18s ease';
    }

    _updateFocusBanner(openingName) {
        const banner = document.getElementById('trainFocusBanner');
        const nameSpan = document.getElementById('trainFocusName');
        if (banner) {
            banner.style.display = 'flex';
            banner.style.minHeight = '32px';
            banner.style.boxSizing = 'border-box';
        }
        if (nameSpan && openingName) {
            nameSpan.textContent = openingName;
            nameSpan.title = openingName;
        }
        const active = !!openingName;
        if (banner) {
            banner.style.visibility = active ? 'visible' : 'hidden';
            banner.style.opacity = active ? '1' : '0';
        }
        const btnClear = document.getElementById('btnClearTrainFocus');
        if (btnClear) btnClear.disabled = !active;
    }

    _updateImportantBadge(isImportant) {
        const badgeEl = document.getElementById('trainImportantBadge');
        if (!badgeEl) return;
        badgeEl.style.minWidth = '96px';
        badgeEl.style.justifyContent = 'center';
        this._setConditional(badgeEl, !!isImportant, 'inline-flex');
    }

    closeSessionModal() {
        const modal = document.getElementById('trainSessionCompleteModal');
        if (modal) modal.style.display = 'none';
    }

    updateSessionUI() {
        const moveLabel = document.getElementById('trainSessionMoveLabel');
        const progBar = document.getElementById('trainSessionProgressBar');
        const scoreEl = document.getElementById('trainSessionScore');
        const accEl = document.getElementById('trainSessionAccuracy');

        if (this.isReinforcementPhase) {
            const remaining = this.sessionLapses.length;
            const cleared = Math.max(0, this.reinforcementTotal - remaining);
            if (moveLabel) {
                moveLabel.textContent = `Reinforce: ${remaining} left`;
            }
            if (progBar) {
                const pct = this.reinforcementTotal > 0 ? Math.round((cleared / this.reinforcementTotal) * 100) : 100;
                progBar.style.width = `${Math.max(5, pct)}%`;
            }
            if (scoreEl) {
                scoreEl.textContent = `${cleared} / ${this.reinforcementTotal}`;
            }
            if (accEl) {
                const initialTotal = Math.max(1, this.sessionTarget);
                const acc = Math.round((this.sessionCorrect / initialTotal) * 100);
                accEl.textContent = `${acc}%`;
            }
        } else {
            const currentMoveNum = Math.min(this.sessionIndex + 1, this.sessionTarget);
            if (moveLabel) {
                moveLabel.textContent = `${currentMoveNum} / ${this.sessionTarget}`;
            }
            if (progBar) {
                const pct = Math.min(100, Math.round((this.sessionIndex / this.sessionTarget) * 100));
                progBar.style.width = `${Math.max(5, pct)}%`;
            }
            if (scoreEl) {
                scoreEl.textContent = `${this.sessionCorrect} / ${this.sessionIndex}`;
            }
            if (accEl) {
                const acc = this.sessionIndex > 0 ? Math.round((this.sessionCorrect / this.sessionIndex) * 100) : 100;
                accEl.textContent = `${acc}%`;
            }
        }
    }

    startReinforcementPhase() {
        this.sessionLapses = this._pendingReinforcementIds();
        if (!this.sessionLapses.length) {
            this.isReinforcementPhase = false;
            this.onSessionComplete(false);
            return;
        }
        this.isReinforcementPhase = true;
        this.reinforcementTotal = this.sessionLapses.length;
        this._clearDrillTimers();
        this.closeSessionModal();
        const count = this.sessionLapses.length;
        window.showToast?.(`Reinforcing ${count} ${count === 1 ? 'move' : 'moves'}...`, '#f59e0b', 2500);
        this.updateSessionUI();
        this.startNewDrill();
    }

    onSessionComplete(wasReinforced = false) {
        const modal = document.getElementById('trainSessionCompleteModal');
        const modalAcc = document.getElementById('modalSessionAccuracy');
        const modalScore = document.getElementById('modalSessionScore');
        const titleEl = document.getElementById('modalSessionTitle');
        const subtitleEl = document.getElementById('modalSessionSubtitle');
        const reinforceBox = document.getElementById('modalSessionReinforceBox');
        const btnReinforce = document.getElementById('btnModalReinforceSession');

        const initialTotal = Math.max(1, this.sessionTarget);
        const acc = this.sessionIndex > 0 ? Math.round((this.sessionCorrect / initialTotal) * 100) : 100;
        if (modalAcc) modalAcc.textContent = `${acc}%`;
        if (modalScore) {
            modalScore.textContent = wasReinforced
                ? `${this.sessionCorrect} / ${this.sessionTarget} (+${this.reinforcementTotal || 0})`
                : `${this.sessionCorrect} / ${this.sessionIndex}`;
        }

        if (titleEl) {
            titleEl.textContent = wasReinforced ? 'Session & Reinforcement Complete' : '20-Move Session Complete';
        }
        if (subtitleEl) {
            subtitleEl.textContent = '';
        }

        const remainingLapses = this._pendingReinforcementIds().length;
        if (reinforceBox) {
            reinforceBox.style.display = (!wasReinforced && remainingLapses > 0) ? 'block' : 'none';
        }
        const missedCountSpan = document.getElementById('modalSessionMissedCount');
        if (missedCountSpan) missedCountSpan.textContent = remainingLapses;

        if (btnReinforce) {
            btnReinforce.style.display = (!wasReinforced && remainingLapses > 0) ? 'inline-flex' : 'none';
            const btnCountSpan = document.getElementById('modalBtnReinforceCount');
            if (btnCountSpan) btnCountSpan.textContent = remainingLapses;
        }

        if (modal) modal.style.display = 'flex';
        if (window.chessAudio) window.chessAudio.playSuccess();
    }

    async loadMasteryProgress() {
        const seq = ++this.progressSeq;
        const color = this.activeColor;
        const study = this.openingFilter;
        try {
            let url = `/api/repertoire/train/progress?color=${color}`;
            if (study) url += `&study=${encodeURIComponent(study)}`;
            const res = await fetch(url);
            if (seq !== this.progressSeq || color !== this.activeColor || study !== this.openingFilter) return null;
            if (!res.ok) return null;
            const data = await res.json();
            if (seq !== this.progressSeq || color !== this.activeColor || study !== this.openingFilter) return null;

            const scopeBadge = document.getElementById('trainScopeBadge');
            const headlineEl = document.getElementById('trainMasteredHeadline');
            if (scopeBadge) scopeBadge.textContent = this.openingFilter || 'All Studies';
            if (headlineEl) headlineEl.textContent = `${data.mastered_pct}%`;

            const segMastered = document.getElementById('masterySegMastered');
            const segLearning = document.getElementById('masterySegLearning');
            const segUnlearned = document.getElementById('masterySegUnlearned');

            if (segMastered) {
                segMastered.style.width = `${data.mastered_pct}%`;
                segMastered.title = `Mastered: ${data.mastered_count} (${data.mastered_pct}%)`;
            }
            if (segLearning) {
                segLearning.style.width = `${data.learning_pct}%`;
                segLearning.title = `Learning: ${data.learning_count} (${data.learning_pct}%)`;
            }
            if (segUnlearned) {
                segUnlearned.style.width = `${data.unlearned_pct}%`;
                segUnlearned.title = `New: ${data.unlearned_count} (${data.unlearned_pct}%)`;
            }

            const elMastCount = document.getElementById('trainMasteredCount');
            const elMastPct = document.getElementById('trainMasteredPct');
            const elLearnCount = document.getElementById('trainLearningCount');
            const elLearnPct = document.getElementById('trainLearningPct');
            const elUnlearnCount = document.getElementById('trainUnlearnedCount');
            const elUnlearnPct = document.getElementById('trainUnlearnedPct');
            const elTotalMoves = document.getElementById('trainTotalMovesCount');

            if (elMastCount) elMastCount.textContent = data.mastered_count;
            if (elMastPct) elMastPct.textContent = `(${data.mastered_pct}%)`;
            if (elLearnCount) elLearnCount.textContent = data.learning_count;
            if (elLearnPct) elLearnPct.textContent = `(${data.learning_pct}%)`;
            if (elUnlearnCount) elUnlearnCount.textContent = data.unlearned_count;
            if (elUnlearnPct) elUnlearnPct.textContent = `(${data.unlearned_pct}%)`;
            if (elTotalMoves) elTotalMoves.textContent = data.total_moves;

            return data;
        } catch (_) {
            return null;
        }
    }

    async loadOpeningsList(options = {}) {
        const seq = ++this.openingsSeq;
        const color = this.activeColor;
        const study = this.openingFilter;
        try {
            const progressRequest = (options && options.progress !== undefined)
                ? Promise.resolve(options.progress)
                : fetch(`/api/repertoire/train/progress?color=${color}`).then(r => (r.ok ? r.json() : null)).catch(() => null);
            const [openingsRes, progData] = await Promise.all([
                fetch(`/api/repertoire/train/openings?color=${color}`),
                progressRequest
            ]);

            if (seq !== this.openingsSeq || color !== this.activeColor || study !== this.openingFilter) return;
            if (!openingsRes.ok) return;
            const data = await openingsRes.json();
            if (seq !== this.openingsSeq || color !== this.activeColor || study !== this.openingFilter) return;

            const studiesMasteryMap = {};
            if (progData && progData.studies) {
                progData.studies.forEach(s => { studiesMasteryMap[s.name] = s; });
            }

            const filterSelect = document.getElementById('trainOpeningFilter');
            if (!filterSelect) return;

            const prevVal = this.openingFilter || filterSelect.value || '';
            filterSelect.innerHTML = '<option value="">All Studies</option>';

            if (data.recent_opening && data.recent_opening.name) {
                const rec = data.recent_opening;
                const recOpt = document.createElement('option');
                recOpt.value = rec.name;
                const sData = studiesMasteryMap[rec.name];
                const mastText = sData ? ` · ${sData.mastered_pct}%` : '';
                recOpt.textContent = `Recent: ${rec.name}${mastText}`;
                filterSelect.appendChild(recOpt);
            }

            const addedNames = new Set(data.recent_opening?.name ? [data.recent_opening.name] : []);
            (data.openings || []).forEach(op => {
                if (!addedNames.has(op.opening_name)) {
                    addedNames.add(op.opening_name);
                    const opt = document.createElement('option');
                    opt.value = op.opening_name;
                    const sData = studiesMasteryMap[op.opening_name];
                    const mastText = sData ? ` · ${sData.mastered_pct}%` : '';
                    opt.textContent = `${op.opening_name} (${op.move_count}${mastText})`;
                    filterSelect.appendChild(opt);
                }
            });

            if (prevVal) {
                let found = false;
                for (let opt of filterSelect.options) {
                    if (opt.value === prevVal) {
                        filterSelect.value = prevVal;
                        found = true;
                        break;
                    }
                }
                if (!found) {
                    const opt = document.createElement('option');
                    opt.value = prevVal;
                    opt.textContent = prevVal;
                    filterSelect.appendChild(opt);
                    filterSelect.value = prevVal;
                }
            } else {
                filterSelect.value = '';
            }
        } catch (_) {}
    }

    setOpeningFocus(openingName, color = null) {
        this.init();
        if (color && (color === 'white' || color === 'black' || color === 'mixed') && this.activeColor !== color) {
            this.activeColor = color;
            const btnMixed = document.getElementById('btnTrainMixed');
            const btnWhite = document.getElementById('btnTrainWhite');
            const btnBlack = document.getElementById('btnTrainBlack');
            if (btnMixed) btnMixed.classList.toggle('active', color === 'mixed');
            if (btnWhite) btnWhite.classList.toggle('active', color === 'white');
            if (btnBlack) btnBlack.classList.toggle('active', color === 'black');
            const colorSpan = document.getElementById('trainColorSpan');
            if (colorSpan) colorSpan.textContent = color;
        }
        this.openingFilter = openingName;
        const filterSelect = document.getElementById('trainOpeningFilter');
        if (filterSelect) filterSelect.value = openingName;
        this.restartSession(false);
        return this.startNewDrill();
    }

    clearOpeningFocus() {
        this.openingFilter = null;
        const filterSelect = document.getElementById('trainOpeningFilter');
        if (filterSelect) filterSelect.value = '';
        this._updateFocusBanner(null);
        this.restartSession(false);
        return this.startNewDrill();
    }

    async startNewDrill(options = {}) {
        this.init();
        if (this.activeColor === 'black' || this.activeColor === 'white') {
            this.board.setOrientation(this.activeColor);
        }
        this.hasAnsweredCurrentPosition = false;
        this.currentPositionHasMistake = false;
        this.currentPositionUsedHint = false;
        this.pendingMoveNote = '';
        this.drillReadyAt = 0;

        if (this.sessionMode !== 'deviations' && !this.isReinforcementPhase && this.sessionIndex >= this.sessionTarget) {
            if (this._pendingReinforcementIds().length > 0) {
                this.startReinforcementPhase();
            } else {
                this.onSessionComplete(false);
            }
            return;
        }

        this.drillToken++;
        this._clearDrillTimers();
        this._resetLineReplay();
        const drillToken = this.drillToken;

        this.isWaitingForUser = false;
        this.currentDrill = null;
        this.updateHintButtonState(false);

        const progressRequest = this.loadMasteryProgress();
        await Promise.all([
            this.loadOpeningsList({ progress: progressRequest }),
            progressRequest
        ]);
        this.updateSessionUI();

        this.hasAnsweredCurrentPosition = false;

        const feedback = document.getElementById('trainFeedback');
        if (feedback) {
            feedback.style.display = 'none';
            feedback.className = 'feedback-box';
        }

        // Anti-spoiler active recall: hide move notes before user attempts the position
        this._updateNoteBox('');

        const colorSpan = document.getElementById('trainColorSpan');
        if (colorSpan) colorSpan.textContent = (this.activeColor === 'mixed') ? 'White & Black' : this.activeColor;

        let mode = this.sessionMode || 'adaptive';
        if (this.isReinforcementPhase) {
            mode = 'lapses_only';
        }
        let url = `/api/repertoire/train/next?color=${this.activeColor}&mode=${encodeURIComponent(mode)}`;
        if (mode === 'deviations') {
            const devIdx = (options && options.index !== undefined)
                ? options.index
                : (this.currentDeviationIndex !== undefined ? this.currentDeviationIndex : 0);
            url += `&index=${devIdx}`;
            if (options && options.game_id) {
                url += `&game_id=${encodeURIComponent(options.game_id)}`;
            }
        }
        if (this.openingFilter && mode !== 'deviations') {
            url += `&opening=${encodeURIComponent(this.openingFilter)}`;
        }
        this._updateFocusBanner(this.openingFilter || null);

        if (this.sessionLapses && this.sessionLapses.length > 0) {
            url += `&lapses=${encodeURIComponent(this.sessionLapses.join(','))}`;
        }
        if (this.sessionDrilledIds && this.sessionDrilledIds.length > 0) {
            const recent = this.sessionDrilledIds.slice(-30);
            url += `&recent_ids=${encodeURIComponent(recent.join(','))}`;
        }

        try {
            const res = await fetch(url);
            if (drillToken !== this.drillToken) return;
            if (res.status === 404) {
                if (this.openingFilter) {
                    window.showToast?.(`No moves in '${this.openingFilter}'. Showing all studies.`, '#f59e0b');
                    this.clearOpeningFocus();
                    return;
                }
                const promptEl = document.getElementById('trainPrompt');
                if (promptEl) {
                    promptEl.innerHTML = `<div class="drill-prompt-headline">No prepared moves yet — add moves in Builder first.</div>`;
                }
                return;
            }
            const data = await res.json();
            if (drillToken !== this.drillToken) return;

            if (data.all_deviations_completed) {
                if (this.sessionMode === 'deviations') {
                    this.showDeviationsCompleteBanner();
                    return;
                }
                window.showToast?.('No game deviations pending.', '#34d399', 2600);
                this.sessionMode = 'adaptive';
                const modeSelect = document.getElementById('trainModeSelect');
                if (modeSelect) modeSelect.value = 'adaptive';
                return this.startNewDrill();
            }
            if (data.all_due_completed) {
                window.showToast?.('All due reviews complete.', '#34d399', 2600);
                this.sessionMode = 'adaptive';
                const modeSelect = document.getElementById('trainModeSelect');
                if (modeSelect) modeSelect.value = 'adaptive';
                return this.startNewDrill();
            }
            if (data.all_frontier_completed) {
                window.showToast?.('All new lines learned. Switching to review.', '#5b9bd5', 2600);
                this.sessionMode = 'adaptive';
                const modeSelect = document.getElementById('trainModeSelect');
                if (modeSelect) modeSelect.value = 'adaptive';
                return this.startNewDrill();
            }
            if (data.all_lapses_completed || (this.isReinforcementPhase && (!this.sessionLapses || this.sessionLapses.length === 0))) {
                window.showToast?.('All missed moves reinforced.', '#34d399', 2600);
                this.isReinforcementPhase = false;
                this.onSessionComplete(true);
                return;
            }

            this.currentDrill = data;
            this.currentFen = data.parent_fen;
            this.pendingMoveNote = data.comment || '';

            if (data.id) {
                this.sessionDrilledIds.push(data.id);
            }

            const drillColor = this.getDrillColor(data);
            this.board.setOrientation(drillColor);

            // Load target position synchronously (zero input latency), then animate dynamic lead-in
            this.board.loadFEN(this.currentFen);
            this.board.clearArrows();
            if (data.last_move && data.last_move.from && data.last_move.to) {
                this.board.setLastMove(data.last_move.from, data.last_move.to);
                if (typeof this.board.animateGhostMove === 'function') {
                    this.board.animateGhostMove(data.last_move.from, data.last_move.to);
                }
            }

            data.path_san = (Array.isArray(data.path_san) && data.path_san.length)
                ? data.path_san.slice()
                : (Array.isArray(data.move_history_sans) ? data.move_history_sans.slice() : []);
            this._loadLineSteps();

            this.updateBreadcrumb(data.path_formatted, data.drill_reason, data.ply_depth);

            const nameEl = document.getElementById('trainOpeningName');
            const ecoEl = document.getElementById('trainOpeningEco');
            const displayTitle = data.study_name || this.openingFilter || data.opening_name || (drillColor === 'white' ? 'White Repertoire' : 'Black Repertoire');
            if (nameEl) { nameEl.textContent = displayTitle; nameEl.title = displayTitle; }
            if (ecoEl) ecoEl.textContent = data.eco || 'Repertoire';

            const devBanner = document.getElementById('trainDeviationBanner');
            const sessionStrip = document.querySelector('.trainer-session-strip');

            if (this.sessionMode === 'deviations' || data.curriculum_mode === 'deviations') {
                this.activeDrillDeviation = data;
                if (devBanner) devBanner.style.display = 'block';
                if (sessionStrip) sessionStrip.style.display = 'none';

                const oppEl = document.getElementById('devBannerOpponent');
                const ratingEl = document.getElementById('devBannerRating');
                const speedEl = document.getElementById('devBannerSpeed');
                const resultEl = document.getElementById('devBannerResult');
                const moveNumEl = document.getElementById('devBannerMoveNum');
                const dateEl = document.getElementById('devBannerDate');
                const playedSanEl = document.getElementById('devBannerPlayedSan');
                const prepSanEl = document.getElementById('devBannerPreparedSan');
                const countEl = document.getElementById('devBannerCount');
                const linkEl = document.getElementById('devBannerGameLink');

                if (oppEl) oppEl.textContent = data.opponent_name || 'Opponent';
                if (ratingEl) ratingEl.textContent = data.opponent_rating ? `(${data.opponent_rating})` : '';
                if (speedEl) speedEl.textContent = (data.speed || 'game').toUpperCase();
                if (resultEl) {
                    const resClass = this._normalizeGameOutcome(data.game_result || data.result, drillColor);
                    resultEl.className = `game-outcome-pill ${resClass}`;
                    resultEl.textContent = resClass.toUpperCase();
                }
                if (moveNumEl) moveNumEl.textContent = data.deviation_move_number || 1;
                if (dateEl && data.played_at) dateEl.textContent = `· ${new Date(data.played_at).toLocaleDateString()}`;
                if (playedSanEl) playedSanEl.textContent = data.played_san || '...';
                if (prepSanEl) prepSanEl.textContent = '?';
                if (countEl) countEl.textContent = `${data.deviation_index || 1} / ${data.total_deviations || 1}`;
                if (linkEl) {
                    linkEl.href = data.game_url || (data.game_id ? `https://lichess.org/${data.game_id}` : '#');
                    linkEl.style.display = data.game_id ? 'inline-flex' : 'none';
                }

                this.currentDeviationIndex = (data.deviation_index ? data.deviation_index - 1 : 0);
                this.totalDeviationsCount = data.total_deviations || 0;

                const badge = document.getElementById('trainDeviationsBadge');
                if (badge) badge.textContent = data.total_deviations || 0;

                const promptEl = document.getElementById('trainPrompt');
                if (promptEl) {
                    promptEl.innerHTML = `
                        <div class="drill-prompt-headline">
                            vs ${this._escapeHtml(data.opponent_name || 'Opponent')} — played <span class="text-crimson">${this._escapeHtml(data.played_san)}</span>
                        </div>
                    `;
                }
            } else {
                this.activeDrillDeviation = null;
                if (devBanner) devBanner.style.display = 'none';
                if (sessionStrip) sessionStrip.style.display = 'flex';
                const promptEl = document.getElementById('trainPrompt');
                if (promptEl) {
                    const capColor = drillColor.charAt(0).toUpperCase() + drillColor.slice(1);
                    if (data.drill_reason === 'game_deviation' && data.played_san) {
                        promptEl.innerHTML = `
                            <div class="drill-prompt-headline">
                                Play as <span id="trainColorSpan">${capColor}</span> · <span class="muted-tag">Played ${this._escapeHtml(data.played_san)} vs ${this._escapeHtml(data.opponent_name || 'Opponent')}</span>
                            </div>
                        `;
                    } else {
                        promptEl.innerHTML = `<div class="drill-prompt-headline">Play as <span id="trainColorSpan">${capColor}</span></div>`;
                    }
                }
            }

            this._updateImportantBadge(data.is_important);

            this.savedPlanArrows = [];
            if (data.arrows) {
                try {
                    this.savedPlanArrows = JSON.parse(data.arrows);
                } catch (_) {
                    this.savedPlanArrows = [];
                }
            }
            const btnArrows = document.getElementById('btnTrainToggleArrows');
            this._setConditional(btnArrows, !!(this.savedPlanArrows && this.savedPlanArrows.length), 'inline-flex');

            this.isWaitingForUser = true;
            this.drillReadyAt = this._nowMs();
            this.updateHintButtonState(true);
        } catch (e) {
            console.error('Error starting repertoire drill:', e);
        }
    }

    updateBreadcrumb(pathFormatted, reason, plyDepth) {
        const movesEl = document.getElementById('trainBreadcrumbMoves');
        const badgeEl = document.getElementById('trainDrillReasonBadge');

        if (movesEl) {
            if (pathFormatted && pathFormatted.trim()) {
                movesEl.textContent = pathFormatted;
                movesEl.title = pathFormatted;
            } else {
                movesEl.textContent = 'Move 1';
                movesEl.title = 'Starting position';
            }
        }

        if (badgeEl) {
            const moveNum = this.currentDrill?.deviation_move_number || (plyDepth ? Math.ceil(plyDepth / 2) : 1);
            if (reason === 'deviations' || reason === 'game_deviation') {
                badgeEl.className = 'trainer-reason-badge badge-lapse';
                badgeEl.textContent = `Deviation · M${moveNum}`;
            } else if (reason === 'frontier_new') {
                badgeEl.className = 'trainer-reason-badge badge-frontier';
                badgeEl.textContent = `New · M${moveNum}`;
            } else if (reason === 'due_review') {
                if (this.currentDrill?.is_leech) {
                    badgeEl.className = 'trainer-reason-badge badge-lapse';
                    badgeEl.textContent = 'Leech';
                } else {
                    badgeEl.className = 'trainer-reason-badge badge-due';
                    badgeEl.textContent = 'Due';
                }
            } else if (reason === 'session_lapse') {
                badgeEl.className = 'trainer-reason-badge badge-lapse';
                badgeEl.textContent = 'Reinforce';
            } else if (reason === 'maintenance' || reason === 'mastery_audit') {
                badgeEl.className = 'trainer-reason-badge badge-maintenance';
                badgeEl.textContent = (this.currentDrill?.is_mastered_audit || reason === 'mastery_audit') ? 'Spot-Check' : 'Review';
            } else {
                badgeEl.className = 'trainer-reason-badge badge-maintenance';
                badgeEl.textContent = 'Drill';
            }
        }
    }

    updateHintButtonState(enabled) {
        const btnHint = document.getElementById('btnTrainHint');
        if (btnHint) {
            btnHint.disabled = !enabled;
            btnHint.style.opacity = enabled ? '1' : '0.45';
            btnHint.style.cursor = enabled ? 'pointer' : 'not-allowed';
        }
    }

    showHint() {
        if (this.replayMode) this.exitReplay();
        if (!this.isWaitingForUser || !this.currentDrill || !this.currentDrill.expected_uci) {
            window.showToast?.('No active move to hint.', '#94a3b8', 2000);
            return;
        }

        const uci = this.currentDrill.expected_uci;
        const from = uci.slice(0, 2);
        const to = uci.slice(2, 4);

        const fen = this.board.getFEN();
        const turn = fen.split(' ')[1];
        const drillColor = this.getDrillColor(this.currentDrill);
        const expectedTurn = drillColor === 'white' ? 'w' : 'b';
        if (turn !== expectedTurn) return;

        if (this.currentFen && fen.split(' ')[0] !== this.currentFen.split(' ')[0]) {
            this.board.loadFEN(this.currentFen);
        }

        const piece = this.board.chess ? this.board.chess.get(from) : null;
        if (piece && piece.color !== expectedTurn) return;

        this.currentPositionUsedHint = true;
        if (this.currentDrill && this.currentDrill.id) {
            this._recordSessionLapse(this.currentDrill.id);
        }

        this.board.clearArrows();
        this.board.addArrow(from, to, '#34d399');

        // Reveal move note when hint is explicitly requested
        if (this.pendingMoveNote) {
            this._updateNoteBox(this.pendingMoveNote);
        }

        if (this.currentDrill.expected_san) {
            window.showToast?.(`Hint: ${this.currentDrill.expected_san}`, '#5b9bd5', 2400);
        }
        if (this.sessionMode === 'deviations' || this.activeDrillDeviation) {
            const prepSanEl = document.getElementById('devBannerPreparedSan');
            if (prepSanEl && this.currentDrill) {
                prepSanEl.textContent = this.currentDrill.expected_san;
            }
        }
    }

    retryCurrentPosition() {
        if (this.replayMode) this.exitReplay();
        if (!this.currentFen || !this.currentDrill || !this.currentDrill.expected_uci) return;
        this.board.loadFEN(this.currentFen);
        this.board.clearArrows();
        if (this.currentDrill && this.currentDrill.last_move) {
            this.board.setLastMove(this.currentDrill.last_move.from, this.currentDrill.last_move.to);
        }
        this.isWaitingForUser = true;
        this.drillReadyAt = this._nowMs();
        this.updateHintButtonState(true);

        const feedback = document.getElementById('trainFeedback');
        if (feedback) feedback.style.display = 'none';
    }

    // ================================================================== //
    //  Line Replay
    // ================================================================== //

    _fen3(fen) {
        return String(fen || '').trim().split(/\s+/).slice(0, 3).join(' ');
    }

    handleTrainerKey(e) {
        const key = e && e.key;
        if (!key) return false;
        if (key === 'ArrowLeft' || key === 'h' || key === 'H') {
            if (e.repeat && !this.replayMode && this.lineSteps.length < 2) return false;
            e.preventDefault();
            this.replayStep(-1);
            return true;
        }
        if (key === 'ArrowRight' || key === 'l' || key === 'L') {
            e.preventDefault();
            this.replayStep(1);
            return true;
        }
        if (key === 'ArrowUp' || key === 'k' || key === 'K' || key === 'Home') {
            e.preventDefault();
            this.replayJumpTo(0);
            return true;
        }
        if (key === 'ArrowDown' || key === 'j' || key === 'J' || key === 'End') {
            e.preventDefault();
            this.replayJumpToEnd();
            return true;
        }
        if (key === ' ' || key === 'Spacebar') {
            const tag = (e.target && e.target.tagName) ? String(e.target.tagName).toLowerCase() : '';
            if (tag === 'button' || tag === 'a') return false;
            if (e.repeat) return false;
            e.preventDefault();
            this.toggleReplayPlayback();
            return true;
        }
        if (key === 'Escape') {
            if (!this.replayMode) return false;
            e.preventDefault();
            this.exitReplay();
            return true;
        }
        if (key === 'r' || key === 'R') { e.preventDefault(); this.retryCurrentPosition(); return true; }
        if (key === 'i' || key === 'I') { e.preventDefault(); this.showHint(); return true; }
        return false;
    }

    _buildLineSteps(drill) {
        if (!drill) return null;
        const sans = (Array.isArray(drill.path_san) && drill.path_san.length)
            ? drill.path_san.slice()
            : (Array.isArray(drill.move_history_sans) ? drill.move_history_sans.slice() : []);
        if (!sans.length) return null;
        let chess;
        try { chess = new Chess(); } catch (_) { return null; }
        const steps = [{ fen: chess.fen(), from: null, to: null, san: null }];
        for (const san of sans) {
            let mv = null;
            try { mv = chess.move(String(san)); } catch (_) { mv = null; }
            if (!mv) return null;
            steps.push({ fen: chess.fen(), from: mv.from, to: mv.to, san: mv.san || String(san) });
        }
        const targetFen = drill.parent_fen || this.currentFen || '';
        const target = this._fen3(targetFen);
        if (target && this._fen3(steps[steps.length - 1].fen) !== target) return null;
        if (targetFen) steps[steps.length - 1].fen = String(targetFen);
        return steps;
    }

    _loadLineSteps() {
        if (this.replayMode) return;
        this.lineSteps = this._buildLineSteps(this.currentDrill) || [];
        this.lineIndex = Math.max(0, this.lineSteps.length - 1);
        this._renderReplayBar();
    }

    _resetLineReplay() {
        this._stopReplayTimer();
        this.replayPlaying = false;
        this.replayMode = false;
        this.lineSteps = [];
        this.lineIndex = 0;
        this.replaySavedWaiting = false;
        if (this.board && this.board.options) this.board.options.interactive = true;
        this._renderReplayBar();
    }

    _stopReplayTimer() {
        if (this.replayTimer) { clearTimeout(this.replayTimer); this.replayTimer = null; }
    }

    _renderReplayBar() {
        const steps = this.lineSteps || [];
        const canReplay = steps.length >= 2;
        const lastIndex = Math.max(0, steps.length - 1);
        const atStart = this.lineIndex <= 0;
        const atEnd = this.lineIndex >= lastIndex;

        const btnReplay = document.getElementById('btnTrainReplay');
        if (btnReplay) {
            btnReplay.style.visibility = canReplay ? 'visible' : 'hidden';
            btnReplay.disabled = !canReplay;
            btnReplay.textContent = this.replayPlaying ? '⏸ Pause' : (this.replayMode ? '▶ Resume' : '▶ Replay Line');
            btnReplay.title = this.replayPlaying
                ? 'Pause the line replay (Space)'
                : (this.replayMode
                    ? (atEnd
                        ? 'Replay the line again from the start (Space)'
                        : 'Resume the line replay (Space)')
                    : 'Replay the moves that led to this position at 650 ms per move (Space)');
        }

        const bar = document.getElementById('trainReplayBar');
        if (bar) {
            bar.style.visibility = this.replayMode ? 'visible' : 'hidden';
            bar.style.opacity = this.replayMode ? '1' : '0';
        }

        const btnStart = document.getElementById('btnTrainReplayStart');
        if (btnStart) btnStart.disabled = !this.replayMode || atStart;
        const btnPrev = document.getElementById('btnTrainReplayPrev');
        if (btnPrev) btnPrev.disabled = !this.replayMode || atStart;
        const btnNext = document.getElementById('btnTrainReplayNext');
        if (btnNext) btnNext.disabled = !this.replayMode || atEnd;
        const btnExit = document.getElementById('btnTrainReplayExit');
        if (btnExit) btnExit.disabled = !this.replayMode;

        const status = document.getElementById('trainReplayStatus');
        if (status) {
            const mode = this.replayPlaying ? 'Playing' : 'Paused';
            status.textContent = `${mode} ${this.lineIndex} / ${lastIndex}`;
        }
    }

    _syncReplayInteractivity() {
        const shouldWait = Boolean(
            this.replaySavedWaiting ||
            (this.currentDrill && this.currentDrill.expected_uci && !this.opponentReplyTimeout && !this.autoNextTimeout)
        );
        if (!this.replayMode) {
            if (this.board && this.board.options) this.board.options.interactive = true;
            return;
        }
        const steps = this.lineSteps || [];
        const lastIndex = Math.max(0, steps.length - 1);
        const atLiveDrill = !this.replayPlaying && this.lineIndex >= lastIndex;
        if (atLiveDrill) {
            if (this.board && this.board.options) this.board.options.interactive = true;
            this.isWaitingForUser = shouldWait;
            if (this.isWaitingForUser) this.drillReadyAt = this._nowMs();
            this.updateHintButtonState(this.isWaitingForUser);
        } else {
            if (this.board && this.board.options) this.board.options.interactive = false;
            this.isWaitingForUser = false;
            this.updateHintButtonState(false);
        }
    }

    _gotoLineStep(index) {
        const steps = this.lineSteps || [];
        if (!steps.length) return;
        const lastIndex = steps.length - 1;
        const clamped = Math.max(0, Math.min(lastIndex, index));
        this.lineIndex = clamped;
        const step = steps[clamped];
        const atLiveDrill = !this.replayPlaying && clamped >= lastIndex;
        if (this.board) {
            const fenToLoad = (atLiveDrill && this.currentFen) ? this.currentFen : step.fen;
            this.board.loadFEN(fenToLoad);
            this.board.clearArrows();
            const lm = (atLiveDrill && this.currentDrill && this.currentDrill.last_move)
                ? this.currentDrill.last_move
                : step;
            if (lm && lm.from && lm.to) {
                this.board.setLastMove(lm.from, lm.to);
            } else {
                this.board.setLastMove(null, null);
            }
            if (atLiveDrill && this.savedPlanArrows && this.savedPlanArrows.length) {
                this.board.setArrows(this.savedPlanArrows);
            }
        }
        this._syncReplayInteractivity();
        this._renderReplayBar();
    }

    _enterReplayMode() {
        if (!this.currentDrill || this.opponentReplyTimeout || this.autoNextTimeout) return false;
        if (!this.replayMode) {
            this.replaySavedWaiting = Boolean(
                this.isWaitingForUser ||
                (this.currentDrill && this.currentDrill.expected_uci)
            );
            this.isWaitingForUser = false;
            if (this.mistakeResetTimeout) { clearTimeout(this.mistakeResetTimeout); this.mistakeResetTimeout = null; }
            if (this.solutionArrowFadeTimeout) { clearTimeout(this.solutionArrowFadeTimeout); this.solutionArrowFadeTimeout = null; }
            this.replayMode = true;
        }
        if (this.board && this.board.options) this.board.options.interactive = false;
        this._renderReplayBar();
        return true;
    }

    startReplay() {
        if (this.replayMode) { this.toggleReplayPlayback(); return; }
        const steps = this._buildLineSteps(this.currentDrill);
        if (!steps || steps.length < 2) {
            window.showToast?.('No recorded move path for this position.', '#94a3b8', 2200);
            return;
        }
        if (!this._enterReplayMode()) return;
        this.lineSteps = steps;
        this.lineIndex = 0;
        this.replayPlaying = true;
        this._gotoLineStep(0);
        this._scheduleReplayTick();
    }

    toggleReplayPlayback() {
        if (!this.replayMode) { this.startReplay(); return; }
        if (this.replayPlaying) { this.pauseReplay(); return; }
        this.replayPlaying = true;
        const lastIndex = Math.max(0, (this.lineSteps || []).length - 1);
        if (lastIndex > 0 && this.lineIndex >= lastIndex) {
            this._gotoLineStep(0);
        } else {
            this._syncReplayInteractivity();
            this._renderReplayBar();
        }
        this._scheduleReplayTick();
    }

    pauseReplay() {
        this._stopReplayTimer();
        this.replayPlaying = false;
        this._syncReplayInteractivity();
        this._renderReplayBar();
    }

    replayStep(delta) {
        const steps = this.lineSteps || [];
        if (!this.replayMode) {
            if (delta > 0) return;
            if (steps.length < 2) {
                window.showToast?.('No recorded move path to step through.', '#94a3b8', 2000);
                return;
            }
            if (!this._enterReplayMode()) return;
            this._stopReplayTimer();
            this.replayPlaying = false;
            this._gotoLineStep(steps.length - 2);
            return;
        }
        this._stopReplayTimer();
        this.replayPlaying = false;
        this._gotoLineStep(this.lineIndex + delta);
    }

    replayJumpTo(index) {
        if (!this.replayMode) {
            if ((this.lineSteps || []).length < 2) return;
            if (!this._enterReplayMode()) return;
        }
        this._stopReplayTimer();
        this.replayPlaying = false;
        this._gotoLineStep(index);
    }

    replayJumpToEnd() {
        this.exitReplay();
    }

    exitReplay() {
        if (!this.replayMode) return;
        this._stopReplayTimer();
        this.replayPlaying = false;
        this.replayMode = false;
        this.lineIndex = Math.max(0, (this.lineSteps || []).length - 1);
        if (this.board) {
            if (this.currentFen) this.board.loadFEN(this.currentFen);
            this.board.clearArrows();
            const lm = this.currentDrill && this.currentDrill.last_move;
            if (lm && lm.from && lm.to) {
                this.board.setLastMove(lm.from, lm.to);
            } else {
                this.board.setLastMove(null, null);
            }
            if (this.savedPlanArrows && this.savedPlanArrows.length) {
                this.board.setArrows(this.savedPlanArrows);
            }
            if (this.board.options) this.board.options.interactive = true;
        }
        const shouldWait = Boolean(
            this.replaySavedWaiting ||
            (this.currentDrill && this.currentDrill.expected_uci && !this.opponentReplyTimeout && !this.autoNextTimeout)
        );
        this.isWaitingForUser = shouldWait;
        if (this.isWaitingForUser) this.drillReadyAt = this._nowMs();
        this.replaySavedWaiting = false;
        this.updateHintButtonState(this.isWaitingForUser);
        this._renderReplayBar();
    }

    _scheduleReplayTick() {
        const token = this.drillToken;
        this._stopReplayTimer();
        this.replayTimer = setTimeout(() => {
            this.replayTimer = null;
            if (token !== this.drillToken) return;
            if (!this.replayMode || !this.replayPlaying) return;
            const lastIndex = Math.max(0, (this.lineSteps || []).length - 1);
            const nextIndex = Math.min(lastIndex, this.lineIndex + 1);
            if (nextIndex >= lastIndex) {
                this.replayPlaying = false;
            }
            this._gotoLineStep(nextIndex);
            if (this.lineIndex >= lastIndex) return;
            this._scheduleReplayTick();
        }, this.replayDelayMs);
    }

    updateStats() {
        const streakEl = document.getElementById('trainStatStreak');
        const accEl = document.getElementById('trainStatAccuracy');
        const practEl = document.getElementById('trainStatPracticed');

        if (streakEl) streakEl.textContent = this.streak;
        if (practEl) practEl.textContent = this.practicedCount;
        if (accEl) {
            const pct = this.practicedCount > 0 ? Math.round((this.correctCount / this.practicedCount) * 100) : 100;
            accEl.textContent = `${pct}%`;
        }
    }

    _lineEndPresentation(data) {
        const answered = (data && typeof data.unanswered_branches === 'number')
            ? data.unanswered_branches
            : 0;
        const reason = (data && data.line_end_reason) || null;
        const declaredEnd = !!(data && data.is_line_end);
        const restsOnOpponentMove = !!(data && data.opponent_reply && !data.next_expected_uci);
        const unfinished = reason === 'unanswered_opponent_branches'
            || answered > 0
            || restsOnOpponentMove
            || !declaredEnd;
        if (unfinished) {
            const n = answered > 0 ? answered : 1;
            const branches = n === 1 ? 'branch has' : 'branches have';
            const pronoun = n === 1 ? 'it' : 'them';
            return {
                completed: false,
                cls: 'good_alternative',
                color: '#f59e0b',
                toast: `Line paused — ${n} opponent ${branches} no prepared reply. Add ${pronoun} in the Repertoire Builder.`,
                text: `Line paused — ${n} opponent ${branches} no prepared reply. Add ${pronoun} in the Repertoire Builder to finish this line.`
            };
        }
        return {
            completed: true,
            cls: 'correct',
            color: '#4ade80',
            toast: 'Line Complete!',
            text: 'Line Complete!'
        };
    }

    _advanceAfterLineEnd(drillToken) {
        if (this.isReinforcementPhase && (!this.sessionLapses || this.sessionLapses.length === 0)) {
            this.isReinforcementPhase = false;
            this.onSessionComplete(true);
            return;
        }

        if (!this.isReinforcementPhase && this.sessionIndex >= this.sessionTarget) {
            if (this._pendingReinforcementIds().length > 0) {
                this.startReinforcementPhase();
            } else {
                this.onSessionComplete(false);
            }
            return;
        }

        this.startNewDrill();
    }

    async onUserAttempt(from, to, promo, moveRes) {
        if (!this.isWaitingForUser || !this.currentFen) return;
        if (this.replayMode) {
            this._stopReplayTimer();
            this.replayPlaying = false;
            this.replayMode = false;
            this.replaySavedWaiting = false;
            this._renderReplayBar();
        }

        const responseMs = this.drillReadyAt > 0
            ? Math.max(1, Math.round(this._nowMs() - this.drillReadyAt))
            : null;

        const attemptedUci = from + to + (promo || '');
        const attemptSan = (moveRes && typeof moveRes.san === 'string') ? moveRes.san : null;
        const feedback = document.getElementById('trainFeedback');
        const drillToken = this.drillToken;

        const studyContext = this.openingFilter || this.currentDrill?.study_name || undefined;
        const isRetry = Boolean(this.currentPositionHasMistake || this.currentPositionUsedHint);
        const isDeviationMode = (
            this.sessionMode === 'deviations' ||
            Boolean(this.activeDrillDeviation) ||
            this.currentDrill?.drill_reason === 'game_deviation'
        );
        const deviationGameId = this.currentDrill?.game_id || this.activeDrillDeviation?.game_id || null;
        const deviationPlayedUci = this.currentDrill?.played_uci || this.activeDrillDeviation?.played_uci || null;

        const drillColor = this.getDrillColor(this.currentDrill);

        try {
            const res = await fetch('/api/repertoire/train/attempt', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repertoire: drillColor,
                    current_fen: this.currentFen,
                    attempted_uci: attemptedUci,
                    opening_filter: studyContext,
                    study_name: studyContext,
                    is_retry: isRetry,
                    response_ms: responseMs,
                    game_id: deviationGameId,
                    is_deviation: isDeviationMode,
                    played_uci: deviationPlayedUci
                })
            });
            const data = await res.json();
            if (drillToken !== this.drillToken) return;

            // Reveal move note immediately upon answering to reinforce the strategic plan
            const answeredNote = data.comment || this.pendingMoveNote || '';
            if (answeredNote) {
                this._updateNoteBox(answeredNote);
            }

            if (data.correct) {
                if (!isRetry) {
                    this.streak++;
                    this.practicedCount++;
                    this.correctCount++;
                    this.updateStats();

                    if (data.prepared_id) {
                        const pid = data.prepared_id;
                        const wasLapse = this.sessionLapses.includes(pid) || this.sessionReinforcementQueue.includes(pid);
                        if (wasLapse) {
                            this.sessionLapseClearCounts[pid] = (this.sessionLapseClearCounts[pid] || 0) + 1;
                        }
                        if (this.sessionLapses.includes(pid)) {
                            this.sessionLapses = this.sessionLapses.filter(id => id !== pid);
                        }
                        if (this.isReinforcementPhase || (this.sessionLapseClearCounts[pid] || 0) >= 2) {
                            this.sessionReinforcementQueue = this.sessionReinforcementQueue.filter(id => id !== pid);
                        }
                    }

                    if (data.prepared_id && !this.sessionDrilledIds.includes(data.prepared_id)) {
                        this.sessionDrilledIds.push(data.prepared_id);
                    }

                    if (!this.hasAnsweredCurrentPosition) {
                        this.hasAnsweredCurrentPosition = true;
                        if (!this.isReinforcementPhase) {
                            this.sessionIndex++;
                            this.sessionCorrect++;
                        }
                        this.updateSessionUI();
                    }

                    if (window.chessAudio) window.chessAudio.playSuccess();

                    if (this.sessionMode === 'deviations' || this.activeDrillDeviation) {
                        const prepSanEl = document.getElementById('devBannerPreparedSan');
                        if (prepSanEl && this.currentDrill) {
                            prepSanEl.textContent = this.currentDrill.expected_san;
                        }
                    }

                    if (feedback) {
                        feedback.style.display = 'block';
                        feedback.className = 'feedback-box correct';
                        if (this.sessionMode === 'deviations' || this.activeDrillDeviation) {
                            const expSan = this.currentDrill?.expected_san || data.your_san || 'prepared move';
                            feedback.textContent = `✓ ${expSan} — deviation resolved.`;
                        } else {
                            feedback.textContent = `✓ ${data.message}`;
                        }
                    }

                    if (this.sessionMode === 'deviations' || this.activeDrillDeviation) {
                        const resolvedGameId = deviationGameId;
                        const resolvedFen = this.currentDrill?.parent_fen || this.currentFen;
                        const resolvedPlayed = deviationPlayedUci;
                        const resolvedPrep = this.currentDrill?.expected_uci || attemptedUci;

                        fetch('/api/repertoire/deviations/resolve', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                game_id: resolvedGameId,
                                fen_before: resolvedFen,
                                played_uci: resolvedPlayed,
                                prepared_uci: resolvedPrep,
                                user_color: drillColor
                            })
                        }).catch(() => {});

                        if (this.totalDeviationsCount > 0) this.totalDeviationsCount--;
                        if (this.deviationsData && Array.isArray(this.deviationsData.deviations)) {
                            this.deviationsData.deviations = this.deviationsData.deviations.filter(d => {
                                if (resolvedGameId && d.game_id === resolvedGameId) return false;
                                return true;
                            });
                            this.deviationsData.total_deviations = this.deviationsData.deviations.length;
                            this.deviationsData.games_with_deviations = this.deviationsData.deviations.length;
                            this.renderDeviationsList();
                        }

                        this.updateDeviationsCountBadge();

                        if (this.currentDrill?.expected_uci) {
                            this.board.clearArrows();
                            this.board.addArrow(this.currentDrill.expected_uci.slice(0, 2), this.currentDrill.expected_uci.slice(2, 4), '#34d399');
                        }

                        this.activeDrillDeviation = null;
                        clearTimeout(this.autoNextTimeout);
                        this.autoNextTimeout = setTimeout(() => {
                            if (this.drillToken !== drillToken) return;
                            if (this.totalDeviationsCount <= 0) {
                                this.showDeviationsCompleteBanner();
                            } else {
                                if (this.currentDeviationIndex >= this.totalDeviationsCount) {
                                    this.currentDeviationIndex = Math.max(0, this.totalDeviationsCount - 1);
                                }
                                this.startNewDrill({ index: this.currentDeviationIndex });
                            }
                        }, 1100);
                        return;
                    }
                    this.activeDrillDeviation = null;
                } else {
                    if (data.prepared_id) {
                        this._recordSessionLapse(data.prepared_id);
                    }
                    if (data.prepared_id && !this.sessionDrilledIds.includes(data.prepared_id)) {
                        this.sessionDrilledIds.push(data.prepared_id);
                    }

                    if (!this.hasAnsweredCurrentPosition) {
                        this.hasAnsweredCurrentPosition = true;
                        if (!this.isReinforcementPhase) {
                            this.sessionIndex++;
                            this.sessionMistakes++;
                        }
                        this.updateSessionUI();
                    }

                    if (window.chessAudio) window.chessAudio.playSuccess();

                    if (this.sessionMode === 'deviations' || this.activeDrillDeviation) {
                        const prepSanEl = document.getElementById('devBannerPreparedSan');
                        if (prepSanEl && this.currentDrill) {
                            prepSanEl.textContent = this.currentDrill.expected_san;
                        }
                    }

                    if (feedback) {
                        feedback.style.display = 'block';
                        feedback.className = 'feedback-box good_alternative';
                        feedback.textContent = `↻ ${data.message}`;
                    }

                    if (this.sessionMode === 'deviations') {
                        if (this.currentDrill?.expected_uci) {
                            this.board.clearArrows();
                            this.board.addArrow(this.currentDrill.expected_uci.slice(0, 2), this.currentDrill.expected_uci.slice(2, 4), '#34d399');
                        }
                        clearTimeout(this.autoNextTimeout);
                        this.autoNextTimeout = setTimeout(() => {
                            if (this.drillToken !== drillToken) return;
                            this.nextDeviation();
                        }, 1100);
                        return;
                    }
                }

                this.loadMasteryProgress();

                if (data.opponent_reply) {
                    this.isWaitingForUser = false;
                    this.updateHintButtonState(false);

                    this.opponentReplyTimeout = setTimeout(() => {
                        if (drillToken !== this.drillToken) return;
                        const opp = data.opponent_reply;
                        const oppFrom = opp.uci.slice(0, 2);
                        const oppTo = opp.uci.slice(2, 4);
                        const oppPromo = opp.uci.length === 5 ? opp.uci[4] : undefined;

                        this.board.playMove(oppFrom, oppTo, oppPromo);
                        this.currentFen = this.board.getFEN();

                        if (this.currentDrill) {
                            this.currentDrill.last_move = { from: oppFrom, to: oppTo, san: opp.san };
                        }

                        if (data.next_expected_uci && !data.is_line_end) {
                            if (this.currentDrill) {
                                this.currentDrill.expected_uci = data.next_expected_uci;
                                this.currentDrill.expected_san = data.next_expected_san;
                                if (data.study_name) this.currentDrill.study_name = data.study_name;
                                if (data.next_opening_name) {
                                    this.currentDrill.opening_name = data.next_opening_name;
                                    const nameEl = document.getElementById('trainOpeningName');
                                    if (nameEl) nameEl.textContent = data.next_opening_name;
                                }

                                const prevPath = this.currentDrill.path_formatted || '';
                                const userSan = data.your_san || attemptSan || attemptedUci;
                                const oppSan = opp.san;
                                const updatedPath = prevPath ? `${prevPath} ${userSan} ${oppSan}` : `${userSan} ${oppSan}`;
                                this.currentDrill.path_formatted = updatedPath;
                                this.updateBreadcrumb(updatedPath, this.currentDrill.drill_reason, (this.currentDrill.ply_depth || 1) + 2);

                                if (Array.isArray(this.currentDrill.path_san)) {
                                    this.currentDrill.path_san.push(userSan, oppSan);
                                    if (this.currentFen) this.currentDrill.parent_fen = this.currentFen;
                                    this._loadLineSteps();
                                }
                            }

                            this.hasAnsweredCurrentPosition = false;
                            this.currentPositionHasMistake = false;
                            this.currentPositionUsedHint = false;
                            this.pendingMoveNote = data.next_comment || '';
                            // Hide next move's note until recall attempt
                            this._updateNoteBox('');
                            this.isWaitingForUser = true;
                            this.drillReadyAt = this._nowMs();
                            this.updateHintButtonState(true);

                            if (feedback) {
                                feedback.className = 'feedback-box good_alternative';
                                feedback.textContent = `Opponent played ${opp.san}. Your turn.`;
                            }

                            this._updateImportantBadge(data.next_is_important);
                            this.savedPlanArrows = [];
                            if (data.next_arrows) {
                                try {
                                    this.savedPlanArrows = JSON.parse(data.next_arrows);
                                } catch (_) {
                                    this.savedPlanArrows = [];
                                }
                            }
                            const btnArrows = document.getElementById('btnTrainToggleArrows');
                            this._setConditional(btnArrows, !!(this.savedPlanArrows && this.savedPlanArrows.length), 'inline-flex');
                            this.board.clearArrows();
                        } else {
                            if (this.currentDrill) {
                                this.currentDrill.expected_uci = null;
                                this.currentDrill.expected_san = null;
                            }
                            this.isWaitingForUser = false;
                            this.updateHintButtonState(false);

                            if (feedback) {
                                feedback.style.display = 'block';
                                feedback.className = 'feedback-box good_alternative';
                                feedback.textContent = `Opponent replied ${opp.san}. Your repertoire has no prepared answer here — add a reply in the Repertoire Builder.`;
                            }
                            window.showToast?.(`Line paused — no prepared answer to ${opp.san}.`, '#f59e0b', 3000);

                            clearTimeout(this.autoNextTimeout);
                            this.autoNextTimeout = setTimeout(() => {
                                if (drillToken !== this.drillToken) return;
                                this._advanceAfterLineEnd(drillToken);
                            }, 200);
                        }
                    }, 520);
                } else {
                    const lineEnd = this._lineEndPresentation(data);
                    if (this.currentDrill) {
                        this.currentDrill.expected_uci = null;
                        this.currentDrill.expected_san = null;
                    }
                    this.isWaitingForUser = false;
                    this.updateHintButtonState(false);

                    if (feedback) {
                        feedback.style.display = 'block';
                        feedback.className = `feedback-box ${lineEnd.cls}`;
                        feedback.textContent = lineEnd.text;
                    }
                    window.showToast?.(lineEnd.toast, lineEnd.color, lineEnd.completed ? 1100 : 3000);

                    clearTimeout(this.autoNextTimeout);
                    this.autoNextTimeout = setTimeout(() => {
                        if (drillToken !== this.drillToken) return;
                        this._advanceAfterLineEnd(drillToken);
                    }, 180);
                }
            } else {
                // Mistake
                this.streak = 0;
                this.practicedCount++;
                this.updateStats();
                this.currentPositionHasMistake = true;

                if (data.prepared_id) {
                    this._recordSessionLapse(data.prepared_id);
                }

                if (!this.hasAnsweredCurrentPosition) {
                    this.hasAnsweredCurrentPosition = true;
                    if (!this.isReinforcementPhase) {
                        this.sessionIndex++;
                        this.sessionMistakes++;
                    }
                    this.updateSessionUI();
                }

                if (window.chessAudio) window.chessAudio.playBlunder();

                if (feedback) {
                    feedback.style.display = 'block';
                    feedback.className = 'feedback-box blunder';
                    if (this.sessionMode === 'deviations' && this.currentDrill) {
                        feedback.textContent = `✗ Game move: ${this.currentDrill.played_san}. Prepared: ${this.currentDrill.expected_san}.`;
                    } else {
                        feedback.textContent = `✗ ${data.message}`;
                    }
                }

                if (data.expected_uci) {
                    this.board.clearArrows();
                    this.board.addArrow(data.expected_uci.slice(0, 2), data.expected_uci.slice(2, 4), '#34d399');
                }

                this.loadMasteryProgress();

                this.mistakeResetTimeout = setTimeout(() => {
                    if (drillToken !== this.drillToken) return;
                    this.board.loadFEN(this.currentFen);
                    if (this.currentDrill && this.currentDrill.last_move) {
                        this.board.setLastMove(this.currentDrill.last_move.from, this.currentDrill.last_move.to);
                    }
                    if (data.expected_uci) {
                        this.board.clearArrows();
                        this.board.addArrow(data.expected_uci.slice(0, 2), data.expected_uci.slice(2, 4), '#34d399');
                        this.solutionArrowFadeTimeout = setTimeout(() => {
                            if (drillToken !== this.drillToken) return;
                            this.board.clearArrows();
                        }, 900);
                    }
                }, 750);
            }
        } catch (e) {
            console.error('Error verifying repertoire move:', e);
        }
    }

    // ================================================================== //
    //  Game Deviations View
    // ================================================================== //

    switchTrainerTab(tab) {
        this.activeTrainerTab = tab;
        const btnDrills = document.getElementById('btnTrainerTabDrills');
        const btnDeviations = document.getElementById('btnTrainerTabDeviations');
        const drillsSection = document.getElementById('trainerDrillsSection');
        const deviationsView = document.getElementById('trainerDeviationsView');
        const modeSelect = document.getElementById('trainModeSelect');
        const isDeviations = (tab === 'deviations');

        if (btnDrills) btnDrills.classList.toggle('active', !isDeviations);
        if (btnDeviations) btnDeviations.classList.toggle('active', isDeviations);
        if (drillsSection) drillsSection.style.display = isDeviations ? 'none' : 'flex';
        if (deviationsView) deviationsView.style.display = isDeviations ? 'flex' : 'none';

        if (isDeviations) {
            this.loadGameDeviations();
        } else {
            if (this.sessionMode === 'deviations') {
                this.sessionMode = 'adaptive';
                if (modeSelect) modeSelect.value = 'adaptive';
            }
            this.startNewDrill();
        }
    }

    async loadGameDeviations() {
        const list = document.getElementById('trainerDeviationsList');
        if (list && (!this.deviationsData || !this.deviationsData.deviations)) {
            list.innerHTML = '<div class="table-empty-cell">Loading deviations...</div>';
        }

        try {
            const colorParam = (this.deviationsFilterColor && this.deviationsFilterColor !== 'all')
                ? `&color=${this.deviationsFilterColor}`
                : '';
            const res = await fetch(`/api/repertoire/deviations?limit=60${colorParam}`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            this.deviationsData = data;

            const badge = document.getElementById('trainDeviationsBadge');
            if (badge) badge.textContent = data.games_with_deviations || 0;

            const totalEl = document.getElementById('devStatTotalGames');
            if (totalEl) totalEl.textContent = data.total_games || 0;

            const compEl = document.getElementById('devStatCompliancePct');
            if (compEl) compEl.textContent = `${data.compliance_pct || 0}%`;

            const devCountEl = document.getElementById('devStatTotalDeviations');
            if (devCountEl) devCountEl.textContent = data.games_with_deviations || 0;

            this.renderDeviationsList();
        } catch (e) {
            if (list) {
                list.textContent = '';
                const errDiv = document.createElement('div');
                errDiv.className = 'table-empty-cell text-crimson';
                errDiv.textContent = 'Could not load deviations: ' + (e.message || '');
                list.appendChild(errDiv);
            }
        }
    }

    renderDeviationsList() {
        const list = document.getElementById('trainerDeviationsList');
        if (!list) return;

        let devs = (this.deviationsData && Array.isArray(this.deviationsData.deviations))
            ? this.deviationsData.deviations
            : [];

        if (this.deviationsSearch) {
            const q = this.deviationsSearch.toLowerCase();
            devs = devs.filter(d =>
                (d.opponent_name && d.opponent_name.toLowerCase().includes(q)) ||
                (d.opening_name && d.opening_name.toLowerCase().includes(q)) ||
                (d.eco && d.eco.toLowerCase().includes(q)) ||
                (d.played_san && d.played_san.toLowerCase().includes(q)) ||
                (d.prepared_san && d.prepared_san.toLowerCase().includes(q))
            );
        }

        if (devs.length === 0) {
            list.innerHTML = `<div class="card-panel table-empty-cell">No deviations found.</div>`;
            return;
        }

        list.innerHTML = devs.map((d, idx) => {
            const resultCls = this._normalizeGameOutcome(d.game_result || d.result, d.user_color);
            const resultLabel = resultCls.toUpperCase();
            const swatchCls = d.user_color === 'white' ? 'swatch-white' : 'swatch-black';
            const speed = d.speed ? d.speed.toUpperCase() : 'GAME';
            const rating = d.opponent_rating ? `(${d.opponent_rating})` : '';
            const gameDate = d.played_at ? new Date(d.played_at).toLocaleDateString() : '';

            return `
                <div class="deviation-card" data-idx="${idx}">
                    <div class="deviation-header">
                        <div class="deviation-game-info">
                            <span class="swatch ${swatchCls}" title="${this._escapeAttr(d.user_color)}"></span>
                            <span class="deviation-opponent">vs ${this._escapeHtml(d.opponent_name || 'Opponent')} ${rating}</span>
                            <span class="game-outcome-pill ${resultCls}">${resultLabel}</span>
                            <span class="badge-pill">${this._escapeHtml(speed)}</span>
                            <span class="muted-tag">${gameDate}</span>
                        </div>
                        <div class="deviation-opening-tag">
                            <strong>${this._escapeHtml(d.opening_name || 'Opening')}</strong> ${d.eco ? '· ' + this._escapeHtml(d.eco) : ''}
                        </div>
                    </div>

                    <div class="deviation-comparison-grid">
                        <div class="deviation-box">
                            <span class="deviation-box-label">Played (Move ${d.deviation_move_number})</span>
                            <span class="deviation-move-val text-crimson">${this._escapeHtml(d.played_san)}</span>
                        </div>
                        <div class="deviation-arrow-icon">&rarr;</div>
                        <div class="deviation-box">
                            <span class="deviation-box-label">Prepared</span>
                            <span class="deviation-move-val text-emerald">${this._escapeHtml(d.prepared_san)}</span>
                        </div>
                    </div>

                    ${d.prepared_comment ? `<div class="deviation-note-callout" title="${this._escapeAttr(d.prepared_comment)}">${this._escapeHtml(d.prepared_comment)}</div>` : ''}

                    <div class="deviation-actions">
                        ${d.game_id ? `<a href="https://lichess.org/${this._escapeAttr(d.game_id)}" target="_blank" rel="noopener noreferrer" class="btn-secondary btn-xs" style="text-decoration:none;">Lichess ↗</a>` : ''}
                        <button class="btn-primary btn-sm btn-drill-deviation" data-idx="${idx}" type="button">Drill</button>
                    </div>
                </div>
            `;
        }).join('');

        list.querySelectorAll('.btn-drill-deviation').forEach(btn => {
            btn.addEventListener('click', () => {
                const idx = parseInt(btn.getAttribute('data-idx'), 10);
                const dev = devs[idx];
                if (dev) this.drillDeviation(dev);
            });
        });
    }

    drillDeviation(dev) {
        if (!dev) return;
        this.activeDrillDeviation = dev;
        this.activeTrainerTab = 'deviations';
        this.sessionMode = 'deviations';

        const modeSelect = document.getElementById('trainModeSelect');
        if (modeSelect) modeSelect.value = 'deviations';

        const btnDrills = document.getElementById('btnTrainerTabDrills');
        const btnDeviations = document.getElementById('btnTrainerTabDeviations');
        if (btnDrills) btnDrills.classList.remove('active');
        if (btnDeviations) btnDeviations.classList.add('active');

        const drillsSection = document.getElementById('trainerDrillsSection');
        const deviationsView = document.getElementById('trainerDeviationsView');
        if (drillsSection) drillsSection.style.display = 'flex';
        if (deviationsView) deviationsView.style.display = 'none';

        if (dev.user_color && (dev.user_color === 'white' || dev.user_color === 'black')) {
            this.activeColor = dev.user_color;
            const btnMixed = document.getElementById('btnTrainMixed');
            const btnWhite = document.getElementById('btnTrainWhite');
            const btnBlack = document.getElementById('btnTrainBlack');
            if (btnMixed) btnMixed.classList.remove('active');
            if (btnWhite) btnWhite.classList.toggle('active', dev.user_color === 'white');
            if (btnBlack) btnBlack.classList.toggle('active', dev.user_color === 'black');
            const colorSpan = document.getElementById('trainColorSpan');
            if (colorSpan) colorSpan.textContent = dev.user_color;
            this.board.setOrientation(dev.user_color);
        }

        this.startNewDrill({ game_id: dev.game_id });
    }

    _escapeHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    _escapeAttr(s) {
        return this._escapeHtml(s);
    }
}

window.repertoireTrainer = new RepertoireTrainer();
