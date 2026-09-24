// Chess Opening Improvement Platform - Repertoire Trainer (Chessbook Style)
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

        // 20-Move Practice Session State (Chessbook style)
        this.sessionTarget = 20;
        this.sessionIndex = 0;
        this.sessionCorrect = 0;
        this.sessionMistakes = 0;
        this.hasAnsweredCurrentPosition = false;
        this.autoNextTimeout = null;
        // CODE-07: every deferred continuation of a drill is owned by a token.
        // `drillToken` is bumped whenever the drill is replaced, so a 550ms
        // opponent reply or an 800ms mistake reset can never touch the *next*
        // drill's board.
        this.drillToken = 0;
        this.opponentReplyTimeout = null;
        this.mistakeResetTimeout = null;
        // CODE-05: monotonic guards so a slow response can never overwrite a
        // newer one (colour bleed between White/Black panels).
        this.progressSeq = 0;
        this.openingsSeq = 0;

        // Pedagogical Learning Curriculum State
        this.sessionMode = 'adaptive';
        this.sessionLapses = [];
        this.sessionDrilledIds = [];

        // Reinforcement Round and Attempt Tracking State
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

        // Subtab toggle: Practice Drills vs Game Deviations
        const btnTabDrills = document.getElementById('btnTrainerTabDrills');
        const btnTabDeviations = document.getElementById('btnTrainerTabDeviations');
        if (btnTabDrills) btnTabDrills.addEventListener('click', () => this.switchTrainerTab('drills'));
        if (btnTabDeviations) btnTabDeviations.addEventListener('click', () => this.switchTrainerTab('deviations'));

        // Deviation navigation controls
        const btnDevPrev = document.getElementById('btnDevPrev');
        const btnDevNext = document.getElementById('btnDevNext');
        if (btnDevPrev) btnDevPrev.addEventListener('click', () => this.prevDeviation());
        if (btnDevNext) btnDevNext.addEventListener('click', () => this.nextDeviation());

        // Game Deviations filters & controls
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
                        if (el) {
                            el.classList.toggle('active', b.filter === filter);
                            el.style.background = (b.filter === filter) ? '#0284c7' : 'transparent';
                            el.style.color = (b.filter === filter) ? '#fff' : 'var(--text-secondary)';
                        }
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

        // Color toggle
        const btnMixed = document.getElementById('btnTrainMixed');
        const btnWhite = document.getElementById('btnTrainWhite');
        const btnBlack = document.getElementById('btnTrainBlack');
        if (btnMixed) btnMixed.addEventListener('click', () => this.switchColor('mixed'));
        if (btnWhite) btnWhite.addEventListener('click', () => this.switchColor('white'));
        if (btnBlack) btnBlack.addEventListener('click', () => this.switchColor('black'));

        // Action buttons
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
                if (isDev) {
                    this.currentDeviationIndex = 0;
                }
                this.restartSession(false);
                this.startNewDrill({ index: this.currentDeviationIndex });
            });
        }
        if (btnClearFocus) {
            btnClearFocus.addEventListener('click', () => {
                this.clearOpeningFocus();
            });
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

        // Session controls
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
            btnModalClose.addEventListener('click', () => {
                this.closeSessionModal();
            });
        }

        // Keyboard shortcuts (h / l, arrows)
        window.addEventListener('keydown', (e) => {
            const tag = e.target.tagName?.toLowerCase();
            if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) {
                return;
            }

            const trainerView = document.getElementById('viewRepTrainer');
            if (!trainerView || !trainerView.classList.contains('active')) {
                return;
            }

            if (e.key === 'ArrowLeft' || e.key === 'h' || e.key === 'H') {
                e.preventDefault();
                this.retryCurrentPosition();
            } else if (e.key === 'ArrowRight' || e.key === 'l' || e.key === 'L') {
                e.preventDefault();
                this.showHint();
            }
        });

        this.updateSessionUI();
        this.updateDeviationsCountBadge();
        this.isInitialized = true;
    }

    async updateDeviationsCountBadge() {
        try {
            const colorParam = (this.activeColor === 'white' || this.activeColor === 'black') ? `?color=${this.activeColor}` : '';
            const res = await fetch(`/api/repertoire/deviations${colorParam}&limit=1`);
            if (res.ok) {
                const data = await res.json();
                const badge = document.getElementById('trainDeviationsBadge');
                if (badge) badge.textContent = data.games_with_deviations || data.total_deviations || 0;
                this.totalDeviationsCount = data.total_deviations || 0;
            }
        } catch (e) {
            // Background count fetch
        }
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
        if (devBanner) {
            devBanner.style.display = 'block';
            devBanner.innerHTML = `
                <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.75rem;">
                    <div style="display:flex; align-items:center; gap:0.75rem;">
                        <span style="font-size:1.6rem;">🎉</span>
                        <div>
                            <div style="font-weight:700; font-size:1rem; color:#4ade80;">All Game Deviations Resolved!</div>
                            <div style="font-size:0.82rem; color:var(--text-secondary); margin-top:2px;">
                                You have successfully corrected every departure from your prepared repertoire.
                            </div>
                        </div>
                    </div>
                    <button id="btnDevSwitchDrills" class="btn-primary" style="font-size:0.78rem; padding:0.35rem 0.85rem;">
                        Back to Repertoire Drills ➔
                    </button>
                </div>
            `;
            const btnSwitch = document.getElementById('btnDevSwitchDrills');
            if (btnSwitch) {
                btnSwitch.addEventListener('click', () => this.switchTrainerTab('drills'));
            }
        }

        const promptEl = document.getElementById('trainPrompt');
        if (promptEl) {
            promptEl.innerHTML = `
                <div style="font-weight:600; font-size:1.05rem; color:#4ade80; margin-bottom:0.25rem;">
                    🎉 Repertoire 100% Up to Date!
                </div>
                <div style="color:var(--text-secondary); font-size:0.9rem;">
                    No pending real game deviations remaining for ${this.activeColor === 'mixed' ? 'White & Black' : this.activeColor}.
                </div>
            `;
        }

        const feedback = document.getElementById('trainFeedback');
        if (feedback) {
            feedback.style.display = 'block';
            feedback.className = 'feedback-box correct';
            feedback.textContent = '🎉 All game deviations have been corrected and cleared from your list!';
        }
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

    /** CODE-07: cancel every pending continuation of the current drill. */
    _clearDrillTimers() {
        if (this.autoNextTimeout) { clearTimeout(this.autoNextTimeout); this.autoNextTimeout = null; }
        if (this.opponentReplyTimeout) { clearTimeout(this.opponentReplyTimeout); this.opponentReplyTimeout = null; }
        if (this.mistakeResetTimeout) { clearTimeout(this.mistakeResetTimeout); this.mistakeResetTimeout = null; }
    }

    /**
     * AGENTS.md §8.4: conditionally available UI in a toolbar / panel header must
     * keep its box and switch `visibility` (+ `disabled`), never `display`, so
     * the neighbouring controls never reflow when it appears or disappears.
     */
    _setConditional(el, visible, displayMode = 'inline-flex') {
        if (!el) return;
        if (el.style.display !== displayMode) el.style.display = displayMode;
        el.style.visibility = visible ? 'visible' : 'hidden';
        el.style.opacity = visible ? '1' : '0';
        if ('disabled' in el) el.disabled = !visible;
        el.setAttribute('aria-hidden', visible ? 'false' : 'true');
    }

    /**
     * AGENTS.md §8.1/§8.2/§8.5: the move-note callout reserves a constant height
     * and toggles `visibility` instead of `display`, and its text is clamped with
     * a full copy in `title`, so a long (or missing) note never shifts the
     * feedback box below it.
     *
     * FIXER-H measured the 44px reservation as ~13px SHORT of its content: the
     * `Move Note / Idea:` label and `#trainNoteContent` are two *block* children, so
     * the box needed `0.6rem` padding x2 (19.2) + 2px border + label (~17.8) + one
     * 1.45 line of note (~19.7) = ~58.7px, and the last ~13px of the note line was
     * clipped. It was not fixable from `style.css` because this method writes
     * `minHeight`/`maxHeight` **inline**, and an inline declaration beats the
     * stylesheet.
     *
     * It is fixed here instead by putting the label on the note's own line (the
     * AGENTS.md section 8.5 alternative), which is a *layout* change rather than a
     * height change: `19.2 padding + 2 border + ~19.7 line = ~40.9px`, comfortably
     * inside the 44px that `index.html` and `style.css` both already declare, so the
     * reserved box, the stylesheet constant and this inline constant all still say 44
     * and the first paint does not move. Raising the number instead would have needed
     * `index.html` + `style.css` as well (files this patch does not own) and would have
     * introduced a 14px reflow the first time the box was revealed.
     *
     * `display` therefore stays `block` (8.5 / the CODE-12.c contract), so the label is
     * floated out of the flow and the note becomes an inline-block beside it, with its
     * `max-width` reduced by the label's measured width so the note can never wrap onto
     * a second line (a second line would exceed 44px and be clipped again).
     */
    _updateNoteBox(comment) {
        const noteBox = document.getElementById('trainNoteBox');
        const noteContent = document.getElementById('trainNoteContent');
        if (!noteBox || !noteContent) return;
        const text = (comment && String(comment).trim()) ? String(comment).trim() : '';
        noteContent.textContent = text;
        noteContent.title = text;
        noteContent.style.whiteSpace = 'nowrap';
        noteContent.style.overflow = 'hidden';
        noteContent.style.textOverflow = 'ellipsis';
        noteContent.style.verticalAlign = 'top';
        noteContent.style.display = 'inline-block';

        // The label is the first element child of the same box. Float it left so the
        // note's inline-block starts after it instead of on the next line.
        const label = noteBox.firstElementChild;
        const labelWidth = (label && typeof label.offsetWidth === 'number' && label.offsetWidth > 0)
            ? label.offsetWidth
            : 0;
        if (label && label !== noteContent) {
            label.style.float = 'left';
            label.style.marginBottom = '0';
            label.style.marginRight = '0.4rem';
        }
        // `max-width: 100%` alone would let the note slide under the float and wrap,
        // which is exactly the second line the 44px box cannot show.
        noteContent.style.maxWidth = labelWidth
            ? `calc(100% - ${labelWidth + 7}px)`
            : 'calc(100% - 170px)';

        if (noteBox.style.display !== 'block') noteBox.style.display = 'block';
        noteBox.style.minHeight = '44px';
        noteBox.style.maxHeight = '44px';
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
            banner.style.minHeight = '34px';
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
        badgeEl.style.minWidth = '132px';
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
                moveLabel.textContent = `Reinforcing: ${remaining} left`;
                moveLabel.title = `Reinforcing missed moves: ${cleared} of ${this.reinforcementTotal} cleared`;
            }
            if (progBar) {
                const pct = this.reinforcementTotal > 0 ? Math.round((cleared / this.reinforcementTotal) * 100) : 100;
                progBar.style.width = `${Math.max(5, pct)}%`;
                progBar.style.background = 'linear-gradient(90deg, #f59e0b, #22c55e)';
            }
            if (scoreEl) {
                scoreEl.textContent = `${cleared} / ${this.reinforcementTotal} cleared`;
            }
            if (accEl) {
                const initialTotal = Math.max(1, this.sessionTarget);
                const acc = Math.round((this.sessionCorrect / initialTotal) * 100);
                accEl.textContent = `${acc}%`;
            }
        } else {
            const currentMoveNum = Math.min(this.sessionIndex + 1, this.sessionTarget);
            if (moveLabel) {
                moveLabel.textContent = `Move ${currentMoveNum} of ${this.sessionTarget}`;
                moveLabel.title = `Practice session progress: Move ${currentMoveNum} of ${this.sessionTarget}`;
            }
            if (progBar) {
                const pct = Math.min(100, Math.round((this.sessionIndex / this.sessionTarget) * 100));
                progBar.style.width = `${Math.max(5, pct)}%`;
                progBar.style.background = 'linear-gradient(90deg, #38bdf8, #818cf8)';
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
        this.sessionLapses = [...new Set(this.sessionLapses || [])];
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
        const noun = count === 1 ? 'missed move' : 'missed moves';
        window.showToast?.(`🎯 Reinforcing ${count} ${noun}...`, '#f59e0b', 2800);
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
                ? `${this.sessionCorrect} / ${this.sessionTarget} (+${this.reinforcementTotal || 0} reinforced)`
                : `${this.sessionCorrect} / ${this.sessionIndex}`;
        }

        if (titleEl) {
            titleEl.textContent = wasReinforced
                ? '🏆 20-Move Session & Reinforcement Complete!'
                : '🎉 20-Move Session Complete!';
        }
        if (subtitleEl) {
            subtitleEl.textContent = wasReinforced
                ? 'Outstanding work! You completed your 20-move session and reinforced every move you missed.'
                : "Great work! You've completed your 20-move opening training session.";
        }

        const remainingLapses = this.sessionLapses ? this.sessionLapses.length : 0;
        if (reinforceBox) {
            reinforceBox.style.display = (!wasReinforced && remainingLapses > 0) ? 'block' : 'none';
        }
        const missedCountSpan = document.getElementById('modalSessionMissedCount');
        if (missedCountSpan) missedCountSpan.textContent = remainingLapses;

        if (btnReinforce) {
            btnReinforce.style.display = (!wasReinforced && remainingLapses > 0) ? 'block' : 'none';
            const btnCountSpan = document.getElementById('modalBtnReinforceCount');
            if (btnCountSpan) btnCountSpan.textContent = remainingLapses;
        }

        if (modal) modal.style.display = 'flex';
        if (window.chessAudio) window.chessAudio.playSuccess();
    }

    async loadMasteryProgress() {
        // CODE-05: capture the logical context (colour + focus) and a sequence
        // number; a response that belongs to a superseded context is dropped
        // instead of painting White's mastery over a Black drill.
        const seq = ++this.progressSeq;
        const color = this.activeColor;
        const study = this.openingFilter;
        try {
            let url = `/api/repertoire/train/progress?color=${color}`;
            if (study) {
                url += `&study=${encodeURIComponent(study)}`;
            }
            const res = await fetch(url);
            if (seq !== this.progressSeq || color !== this.activeColor || study !== this.openingFilter) return null;
            if (!res.ok) return null;
            const data = await res.json();
            if (seq !== this.progressSeq || color !== this.activeColor || study !== this.openingFilter) return null;

            // Scope badge & headline
            const scopeBadge = document.getElementById('trainScopeBadge');
            const headlineEl = document.getElementById('trainMasteredHeadline');
            if (scopeBadge) scopeBadge.textContent = this.openingFilter || 'All Studies';
            if (headlineEl) headlineEl.textContent = `${data.mastered_pct}%`;

            // Segments
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
                segUnlearned.title = `Unlearned: ${data.unlearned_count} (${data.unlearned_pct}%)`;
            }

            // Legend counts
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
        } catch (e) {
            console.error('Error loading mastery progress:', e);
            return null;
        }
    }

    async loadOpeningsList(options = {}) {
        // CODE-05: same generation guard as loadMasteryProgress, plus the
        // duplicated `/train/progress` request is removed - `startNewDrill`
        // passes the in-flight promise so one drill start costs 2 requests
        // instead of 3.
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
                progData.studies.forEach(s => {
                    studiesMasteryMap[s.name] = s;
                });
            }

            const filterSelect = document.getElementById('trainOpeningFilter');
            if (!filterSelect) return;

            const prevVal = this.openingFilter || filterSelect.value || "";
            filterSelect.innerHTML = '<option value="">All Repertoire Studies (Mixed)</option>';

            if (data.recent_opening && data.recent_opening.name) {
                const rec = data.recent_opening;
                const recOpt = document.createElement('option');
                recOpt.value = rec.name;
                const sData = studiesMasteryMap[rec.name];
                const mastText = sData ? ` • ${sData.mastered_pct}% mastered` : '';
                recOpt.textContent = `🎯 Most Recent: ${rec.name}${mastText}`;
                filterSelect.appendChild(recOpt);
            }

            const addedNames = new Set(data.recent_opening?.name ? [data.recent_opening.name] : []);
            (data.openings || []).forEach(op => {
                if (!addedNames.has(op.opening_name)) {
                    addedNames.add(op.opening_name);
                    const opt = document.createElement('option');
                    opt.value = op.opening_name;
                    const sData = studiesMasteryMap[op.opening_name];
                    const mastText = sData ? ` • ${sData.mastered_pct}% mastered` : '';
                    opt.textContent = `${op.opening_name} (${op.move_count} moves${mastText})`;
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
                if (!found && prevVal) {
                    const opt = document.createElement('option');
                    opt.value = prevVal;
                    opt.textContent = `🎯 ${prevVal}`;
                    filterSelect.appendChild(opt);
                    filterSelect.value = prevVal;
                }
            } else {
                filterSelect.value = "";
            }
        } catch (e) {
            console.error('Error loading openings list for trainer:', e);
        }
    }

    setOpeningFocus(openingName, color = null) {
        this.init();
        if (color && (color === 'white' || color === 'black') && this.activeColor !== color) {
            this.activeColor = color;
            const btnWhite = document.getElementById('btnTrainWhite');
            const btnBlack = document.getElementById('btnTrainBlack');
            if (btnWhite) btnWhite.classList.toggle('active', color === 'white');
            if (btnBlack) btnBlack.classList.toggle('active', color === 'black');
            const colorSpan = document.getElementById('trainColorSpan');
            if (colorSpan) colorSpan.textContent = color;
        }
        this.openingFilter = openingName;
        const filterSelect = document.getElementById('trainOpeningFilter');
        if (filterSelect) {
            filterSelect.value = openingName;
        }
        this.restartSession(false);
        return this.startNewDrill();
    }

    clearOpeningFocus() {
        this.openingFilter = null;
        const filterSelect = document.getElementById('trainOpeningFilter');
        if (filterSelect) filterSelect.value = "";
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

        // Check if standard 20-move session has reached completion target
        if (this.sessionMode !== 'deviations' && !this.isReinforcementPhase && this.sessionIndex >= this.sessionTarget) {
            if (this.sessionLapses && this.sessionLapses.length > 0) {
                this.startReinforcementPhase();
            } else {
                this.onSessionComplete(false);
            }
            return;
        }

        // CODE-07: take ownership of a brand new drill. Any 550ms/800ms
        // continuation still queued for the previous drill is cancelled here and
        // its token is now stale.
        this.drillToken++;
        this._clearDrillTimers();
        const drillToken = this.drillToken;

        // Reset waiting state & clear stale drill immediately so hints/actions cannot use old move!
        this.isWaitingForUser = false;
        this.currentDrill = null;
        this.updateHintButtonState(false);

        // One `/train/progress` request shared by both loaders (was: 3 requests).
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
            // CODE-07: a newer drill may have started while this was in flight.
            if (drillToken !== this.drillToken) return;
            if (res.status === 404) {
                if (this.openingFilter) {
                    alert(`No prepared moves found in your ${this.activeColor} repertoire for '${this.openingFilter}'. Switching to all openings.`);
                    this.clearOpeningFocus();
                    return;
                }
                alert(`No prepared moves found in your ${this.activeColor} repertoire yet. Build your repertoire in the Builder first!`);
                return;
            }
            const data = await res.json();
            if (drillToken !== this.drillToken) return;

            // Handle completion flags in specific modes
            if (data.all_deviations_completed) {
                window.showToast?.('No real game deviations found for this repertoire! 🎉', '#4ade80', 3200);
                this.sessionMode = 'adaptive';
                const modeSelect = document.getElementById('trainModeSelect');
                if (modeSelect) modeSelect.value = 'adaptive';
                return this.startNewDrill();
            }
            if (data.all_due_completed) {
                window.showToast?.('All due spaced repetition reviews are up to date! 🎉', '#4ade80', 3200);
                this.sessionMode = 'adaptive';
                const modeSelect = document.getElementById('trainModeSelect');
                if (modeSelect) modeSelect.value = 'adaptive';
                return this.startNewDrill();
            }
            if (data.all_frontier_completed) {
                window.showToast?.('All repertoire lines learned! Switching to retention review. 🎉', '#38bdf8', 3200);
                this.sessionMode = 'adaptive';
                const modeSelect = document.getElementById('trainModeSelect');
                if (modeSelect) modeSelect.value = 'adaptive';
                return this.startNewDrill();
            }
            if (data.all_lapses_completed || (this.isReinforcementPhase && (!this.sessionLapses || this.sessionLapses.length === 0))) {
                window.showToast?.('All missed moves successfully reinforced! 🎉', '#4ade80', 3200);
                this.isReinforcementPhase = false;
                this.onSessionComplete(true);
                return;
            }
            if (data.all_deviations_completed) {
                this.showDeviationsCompleteBanner();
                return;
            }

            this.currentDrill = data;
            this.currentFen = data.parent_fen;

            if (data.id) {
                this.sessionDrilledIds.push(data.id);
            }

            // Orient board to the side being drilled (White or Black)
            const drillColor = this.getDrillColor(data);
            this.board.setOrientation(drillColor);

            // Load board
            this.board.loadFEN(this.currentFen);
            this.board.clearArrows();
            if (data.last_move && data.last_move.from && data.last_move.to) {
                this.board.setLastMove(data.last_move.from, data.last_move.to);
            }

            // Update Breadcrumb & Pedagogical Badge (0px CLS)
            this.updateBreadcrumb(data.path_formatted, data.drill_reason, data.ply_depth);

            // Set titles
            const nameEl = document.getElementById('trainOpeningName');
            const ecoEl = document.getElementById('trainOpeningEco');
            const displayTitle = data.study_name || this.openingFilter || data.opening_name || (drillColor === 'white' ? 'White Opening Line' : 'Black Opening Line');
            if (nameEl) { nameEl.textContent = displayTitle; nameEl.title = displayTitle; }
            if (ecoEl) ecoEl.textContent = data.study_name ? `Study: ${data.study_name}` : (data.eco ? `${data.eco} • Prepared Repertoire Line` : 'Prepared Repertoire Line');

            // Handle Deviations Banner vs standard session strip
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
                    const res = (data.result || 'loss').toLowerCase();
                    const resClass = (res === 'win' || (res === '1-0' && drillColor === 'white') || (res === '0-1' && drillColor === 'black'))
                        ? 'win'
                        : (res === 'draw' || res === '1/2-1/2')
                            ? 'draw'
                            : 'loss';
                    resultEl.className = `game-outcome-pill ${resClass}`;
                    resultEl.textContent = resClass.toUpperCase();
                }
                if (moveNumEl) moveNumEl.textContent = data.deviation_move_number || 1;
                if (dateEl && data.played_at) dateEl.textContent = `• ${new Date(data.played_at).toLocaleDateString()}`;
                if (playedSanEl) playedSanEl.textContent = data.played_san || '...';
                if (prepSanEl) prepSanEl.textContent = '???';
                if (countEl) countEl.textContent = `${data.deviation_index || 1} of ${data.total_deviations || 1}`;
                if (linkEl) {
                    linkEl.href = data.game_url || (data.game_id ? `https://lichess.org/${data.game_id}` : '#');
                    linkEl.style.display = data.game_id ? 'inline-block' : 'none';
                }

                this.currentDeviationIndex = (data.deviation_index ? data.deviation_index - 1 : 0);
                this.totalDeviationsCount = data.total_deviations || 0;

                const badge = document.getElementById('trainDeviationsBadge');
                if (badge) badge.textContent = data.total_deviations || 0;

                const promptEl = document.getElementById('trainPrompt');
                if (promptEl) {
                    promptEl.innerHTML = `
                        <div style="font-weight:600; font-size:1.05rem; margin-bottom:0.25rem;">
                            Fix Game Deviation as <span style="text-transform:capitalize;">${drillColor}</span>
                        </div>
                        <div style="color:var(--text-secondary); font-size:0.9rem;">
                            In your game vs <strong>${this._escapeHtml(data.opponent_name || 'Opponent')}</strong>, you played <span style="color:#f87171; font-weight:700;">${this._escapeHtml(data.played_san)}</span> on move ${data.deviation_move_number || 1}. Play your prepared repertoire move!
                        </div>
                    `;
                }
            } else {
                this.activeDrillDeviation = null;
                if (devBanner) devBanner.style.display = 'none';
                if (sessionStrip) sessionStrip.style.display = 'flex';
                const promptEl = document.getElementById('trainPrompt');
                if (promptEl) {
                    promptEl.innerHTML = `
                        <div style="font-weight:600; font-size:1.05rem; margin-bottom:0.25rem;">
                            Your turn as <span id="trainColorSpan" style="text-transform:capitalize;">${drillColor}</span>
                        </div>
                        <div style="color:var(--text-secondary); font-size:0.9rem;">
                            Play your prepared repertoire move on the board.
                        </div>
                    `;
                }
            }

            // Priority move badge (visibility-toggled, fixed width: 0px CLS)
            this._updateImportantBadge(data.is_important);

            // Move notes & ideas (constant-height, visibility-toggled: 0px CLS)
            this._updateNoteBox(data.comment);

            // Plan arrows
            this.savedPlanArrows = [];
            if (data.arrows) {
                try {
                    this.savedPlanArrows = JSON.parse(data.arrows);
                } catch (e) {
                    this.savedPlanArrows = [];
                }
            }
            const btnArrows = document.getElementById('btnTrainToggleArrows');
            this._setConditional(btnArrows, !!(this.savedPlanArrows && this.savedPlanArrows.length), 'inline-block');
            if (this.savedPlanArrows && this.savedPlanArrows.length) {
                this.board.setArrows(this.savedPlanArrows);
            }

            this.isWaitingForUser = true;
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
                movesEl.title = `Position path: ${pathFormatted}`;
            } else {
                movesEl.innerHTML = `<span style="color:var(--text-muted); font-size:0.75rem;">Root starting position (Move 1)</span>`;
                movesEl.title = 'Starting position';
            }
        }

        if (badgeEl) {
            if (reason === 'deviations') {
                badgeEl.className = 'trainer-reason-badge badge-lapse';
                const moveNum = this.currentDrill?.deviation_move_number || (plyDepth ? Math.ceil(plyDepth / 2) : 1);
                badgeEl.textContent = `⚠️ Real Game Deviation (Move ${moveNum})`;
                badgeEl.title = 'Real Game Deviation: practice position where you departed from your repertoire';
            } else if (reason === 'frontier_new') {
                badgeEl.className = 'trainer-reason-badge badge-frontier';
                const moveNum = plyDepth ? Math.ceil(plyDepth / 2) : 1;
                badgeEl.textContent = `🌱 New Move (Move ${moveNum})`;
                badgeEl.title = 'Learnable frontier: progressive depth root-to-leaf';
            } else if (reason === 'due_review') {
                badgeEl.className = 'trainer-reason-badge badge-due';
                badgeEl.textContent = '⏰ Due Review';
                badgeEl.title = 'Spaced repetition: memory retention review due';
            } else if (reason === 'session_lapse') {
                badgeEl.className = 'trainer-reason-badge badge-lapse';
                badgeEl.textContent = '🔁 Retry Mistake';
                badgeEl.title = 'Intra-session buffer: re-testing a recent mistake';
            } else if (reason === 'maintenance') {
                badgeEl.className = 'trainer-reason-badge badge-maintenance';
                badgeEl.textContent = '🔄 Retention';
                badgeEl.title = 'Retention practice: refreshing mastered lines';
            } else {
                badgeEl.className = 'trainer-reason-badge badge-maintenance';
                badgeEl.textContent = '🎯 Practice';
                badgeEl.title = 'Repertoire practice';
            }
        }
    }

    updateHintButtonState(enabled) {
        const btnHint = document.getElementById('btnTrainHint');
        if (btnHint) {
            btnHint.disabled = !enabled;
            btnHint.style.opacity = enabled ? '1' : '0.45';
            btnHint.style.cursor = enabled ? 'pointer' : 'not-allowed';
            btnHint.title = enabled ? 'Show hint for your next move' : 'No active move to hint';
        }
    }

    showHint() {
        if (!this.isWaitingForUser || !this.currentDrill || !this.currentDrill.expected_uci) {
            window.showToast?.('No prepared move in this position to hint.', '#94a3b8', 2200);
            return;
        }

        const uci = this.currentDrill.expected_uci;
        const from = uci.slice(0, 2);
        const to = uci.slice(2, 4);

        // Sanity check: Ensure board turn matches user color
        const fen = this.board.getFEN();
        const turn = fen.split(' ')[1];
        const drillColor = this.getDrillColor(this.currentDrill);
        const expectedTurn = drillColor === 'white' ? 'w' : 'b';
        if (turn !== expectedTurn) {
            console.warn('Hint blocked: Not user turn', turn, expectedTurn);
            window.showToast?.('Wait for opponent move to finish.', '#94a3b8', 2200);
            return;
        }

        // Resync board FEN if user played a mistake and board hasn't reset yet
        if (this.currentFen && fen.split(' ')[0] !== this.currentFen.split(' ')[0]) {
            this.board.loadFEN(this.currentFen);
        }

        // Sanity check: Ensure piece on source square exists and belongs to user
        const piece = this.board.chess ? this.board.chess.get(from) : null;
        if (piece && piece.color !== expectedTurn) {
            console.warn('Hint blocked: Piece on source square does not belong to user', from, piece);
            window.showToast?.('Position out of sync with hint.', '#ef4444', 2200);
            return;
        }

        this.currentPositionUsedHint = true;
        if (this.currentDrill && this.currentDrill.id && !this.sessionLapses.includes(this.currentDrill.id)) {
            this.sessionLapses.push(this.currentDrill.id);
        }

        this.board.clearArrows();
        this.board.addArrow(from, to, '#22c55e');

        if (this.currentDrill.expected_san) {
            window.showToast?.(`💡 Hint: Play ${this.currentDrill.expected_san} (${from} → ${to})`, '#38bdf8', 2800);
        }
        if (this.sessionMode === 'deviations' || this.activeDrillDeviation) {
            const prepSanEl = document.getElementById('devBannerPreparedSan');
            if (prepSanEl && this.currentDrill) {
                prepSanEl.textContent = this.currentDrill.expected_san;
            }
        }
    }

    retryCurrentPosition() {
        if (!this.currentFen || !this.currentDrill || !this.currentDrill.expected_uci) return;
        this.board.loadFEN(this.currentFen);
        this.board.clearArrows();
        if (this.currentDrill && this.currentDrill.last_move) {
            this.board.setLastMove(this.currentDrill.last_move.from, this.currentDrill.last_move.to);
        }
        this.isWaitingForUser = true;
        this.updateHintButtonState(true);

        const feedback = document.getElementById('trainFeedback');
        if (feedback) feedback.style.display = 'none';
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

    /**
     * AUDIT FIXER-V — the owner's ruling: "A line should never finish on an opponent's move."
     *
     * There are two honest reasons a drill can stop, and only ONE of them is a completion:
     *
     *   * `repertoire_exhausted` — the position is the one the user's OWN prepared move
     *     produced, and the repertoire genuinely has nothing more. A finished line; celebrated.
     *
     *   * `unanswered_opponent_branches` — the repertoire holds opponent replies for which the
     *     user has prepared NO answer. The line is not finished, it is incomplete: it is missing
     *     the user's own reply. Reported NEUTRAL (amber), never as a completion, and never as an
     *     error either — the user did nothing wrong, they simply have not prepared this
     *     variation, so the message names the fix ("add a reply in the Repertoire Builder")
     *     instead of asserting success or blaming them.
     *
     * This is deliberately a TOTAL function over the payload: it also refuses to celebrate a
     * payload that is not an end at all, or one that leaves the board on an opponent's move, so
     * an old-shaped or malformed response can never produce a false "Line Complete!" here. The
     * guaranteed property is therefore local to this function rather than to whichever branch
     * happens to call it today.
     *
     * Both outcomes reuse the existing `.feedback-box` element and the floating toast, so
     * neither introduces in-flow layout shift (AGENTS.md §8).
     *
     * @returns {{completed: boolean, cls: string, color: string, toast: string, text: string}}
     */
    _lineEndPresentation(data) {
        const answered = (data && typeof data.unanswered_branches === 'number')
            ? data.unanswered_branches
            : 0;
        const reason = (data && data.line_end_reason) || null;
        const declaredEnd = !!(data && data.is_line_end);
        // The board resting on an opponent's move means the line has NOT finished, whatever the
        // payload claims: a completion is a position reached by the user's OWN move.
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
            toast: '🎉 Line Complete!',
            text: '🎉 Line Complete!'
        };
    }

    _advanceAfterLineEnd(drillToken) {
        // Check if reinforcement phase just finished all lapses
        if (this.isReinforcementPhase && (!this.sessionLapses || this.sessionLapses.length === 0)) {
            this.isReinforcementPhase = false;
            this.onSessionComplete(true);
            return;
        }

        // Check if regular 20-move session is complete
        if (!this.isReinforcementPhase && this.sessionIndex >= this.sessionTarget) {
            if (this.sessionLapses && this.sessionLapses.length > 0) {
                this.startReinforcementPhase();
            } else {
                this.onSessionComplete(false);
            }
            return;
        }

        // Otherwise, continue to next drill
        this.startNewDrill();
    }

    async onUserAttempt(from, to, promo, moveRes) {
        if (!this.isWaitingForUser || !this.currentFen) return;

        const attemptedUci = from + to + (promo || '');
        const feedback = document.getElementById('trainFeedback');
        // CODE-07: this attempt belongs to the drill that is on screen *now*.
        const drillToken = this.drillToken;

        const studyContext = this.openingFilter || this.currentDrill?.study_name || undefined;
        const isRetry = Boolean(this.currentPositionHasMistake || this.currentPositionUsedHint);
        const isDeviationMode = (this.sessionMode === 'deviations' || Boolean(this.activeDrillDeviation));
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
                    game_id: deviationGameId,
                    is_deviation: isDeviationMode,
                    played_uci: deviationPlayedUci
                })
            });
            const data = await res.json();
            if (drillToken !== this.drillToken) return;

            if (data.correct) {
                if (!isRetry) {
                    // Success on first attempt!
                    this.streak++;
                    this.practicedCount++;
                    this.correctCount++;
                    this.updateStats();

                    // If this move was previously a lapse in this session, resolve it now
                    if (data.prepared_id && this.sessionLapses.includes(data.prepared_id)) {
                        this.sessionLapses = this.sessionLapses.filter(id => id !== data.prepared_id);
                    }

                    // Track drilled move in session
                    if (data.prepared_id && !this.sessionDrilledIds.includes(data.prepared_id)) {
                        this.sessionDrilledIds.push(data.prepared_id);
                    }

                    // Advance session progress only on first attempt per position
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
                            const expSan = this.currentDrill?.expected_san || data.prepared_san || 'your prepared move';
                            const playedSan = this.currentDrill?.played_san || this.activeDrillDeviation?.played_san || 'game move';
                            feedback.textContent = `✓ Correct! ${expSan} is your prepared move (instead of ${playedSan} in your game). Deviation corrected & removed from your list! 🎉`;
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
                        }).catch(e => console.warn('Could not mark deviation resolved:', e));

                        if (this.totalDeviationsCount > 0) {
                            this.totalDeviationsCount--;
                        }

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
                            this.board.addArrow(this.currentDrill.expected_uci.slice(0, 2), this.currentDrill.expected_uci.slice(2, 4), '#22c55e');
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
                        }, 1200);
                        return;
                    }
                    this.activeDrillDeviation = null;
                } else {
                    // Correct on retry or with hint assistance:
                    // Does NOT count as a first-try win; streak and correct stats are not incremented.
                    // Keep in session lapses so the move is properly reinforced.
                    if (data.prepared_id && !this.sessionLapses.includes(data.prepared_id)) {
                        this.sessionLapses.push(data.prepared_id);
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
                        if (this.sessionMode === 'deviations' || this.activeDrillDeviation) {
                            const expSan = this.currentDrill?.expected_san || data.prepared_san || 'your prepared move';
                            const playedSan = this.currentDrill?.played_san || this.activeDrillDeviation?.played_san || 'game move';
                            feedback.textContent = `↻ Correct! ${expSan} is your prepared move (instead of ${playedSan} in your game).`;
                        } else {
                            feedback.textContent = `↻ ${data.message}`;
                        }
                    }

                    if (this.sessionMode === 'deviations') {
                        if (this.currentDrill?.expected_uci) {
                            this.board.clearArrows();
                            this.board.addArrow(this.currentDrill.expected_uci.slice(0, 2), this.currentDrill.expected_uci.slice(2, 4), '#22c55e');
                        }
                        clearTimeout(this.autoNextTimeout);
                        this.autoNextTimeout = setTimeout(() => {
                            if (this.drillToken !== drillToken) return;
                            this.nextDeviation();
                        }, 1200);
                        return;
                    }
                }

                // Smoothly refresh repertoire mastery progress
                this.loadMasteryProgress();

                // If opponent reply exists, play it after delay
                if (data.opponent_reply) {
                    this.isWaitingForUser = false;
                    this.updateHintButtonState(false);

                    this.opponentReplyTimeout = setTimeout(() => {
                        // CODE-07: the drill may have been replaced (New Drill,
                        // colour switch, focus change) while this was queued.
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

                        // Determine if line continues or has completed
                        if (data.next_expected_uci && !data.is_line_end) {
                            if (this.currentDrill) {
                                this.currentDrill.expected_uci = data.next_expected_uci;
                                this.currentDrill.expected_san = data.next_expected_san;
                                if (data.study_name) {
                                    this.currentDrill.study_name = data.study_name;
                                }
                                if (data.next_opening_name) {
                                    this.currentDrill.opening_name = data.next_opening_name;
                                    const nameEl = document.getElementById('trainOpeningName');
                                    if (nameEl) nameEl.textContent = data.next_opening_name;
                                }

                                const prevPath = this.currentDrill.path_formatted || '';
                                const userSan = data.your_san || attemptedUci;
                                const oppSan = opp.san;
                                const updatedPath = prevPath ? `${prevPath} ${userSan} ${oppSan}` : `${userSan} ${oppSan}`;
                                this.currentDrill.path_formatted = updatedPath;
                                this.updateBreadcrumb(updatedPath, this.currentDrill.drill_reason, (this.currentDrill.ply_depth || 1) + 2);
                            }

                            // Ready for user's next move in the continuing line
                            this.hasAnsweredCurrentPosition = false;
                            this.currentPositionHasMistake = false;
                            this.currentPositionUsedHint = false;
                            this.isWaitingForUser = true;
                            this.updateHintButtonState(true);

                            if (feedback) {
                                feedback.className = 'feedback-box good_alternative';
                                feedback.textContent = `Opponent replied with ${opp.san}. Your turn!`;
                            }

                            // Update next move's importance badge (0px CLS)
                            this._updateImportantBadge(data.next_is_important);
                            // Update next move's notes (0px CLS)
                            this._updateNoteBox(data.next_comment);
                            // Update next move's plan arrows
                            this.savedPlanArrows = [];
                            if (data.next_arrows) {
                                try {
                                    this.savedPlanArrows = JSON.parse(data.next_arrows);
                                } catch (e) {
                                    this.savedPlanArrows = [];
                                }
                            }
                            const btnArrows = document.getElementById('btnTrainToggleArrows');
                            this._setConditional(btnArrows, !!(this.savedPlanArrows && this.savedPlanArrows.length), 'inline-block');
                            if (this.savedPlanArrows && this.savedPlanArrows.length) {
                                this.board.setArrows(this.savedPlanArrows);
                            } else {
                                this.board.clearArrows();
                            }
                        } else {
                            // Line paused on opponent's move with no prepared reply
                            if (this.currentDrill) {
                                this.currentDrill.expected_uci = null;
                                this.currentDrill.expected_san = null;
                            }
                            this.isWaitingForUser = false;
                            this.updateHintButtonState(false);

                            if (feedback) {
                                feedback.style.display = 'block';
                                feedback.className = 'feedback-box good_alternative';
                                feedback.textContent =
                                    `Opponent replied ${opp.san}. Your repertoire has no prepared answer here — `
                                    + 'add a reply for it in the Repertoire Builder to finish this line.';
                            }
                            window.showToast?.(
                                `Line paused — no prepared answer to ${opp.san}. Add it in the Repertoire Builder.`,
                                '#f59e0b', 3200);

                            // Advance immediately in session (short 200ms delay so opponent move animation completes)
                            clearTimeout(this.autoNextTimeout);
                            this.autoNextTimeout = setTimeout(() => {
                                if (drillToken !== this.drillToken) return;
                                this._advanceAfterLineEnd(drillToken);
                            }, 200);
                        }
                    }, 550);
                } else {
                    // Line ended on user's own prepared move
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
                    window.showToast?.(lineEnd.toast, lineEnd.color, lineEnd.completed ? 1200 : 3200);

                    // Advance immediately in session (quick 150ms delay for smooth transition)
                    clearTimeout(this.autoNextTimeout);
                    this.autoNextTimeout = setTimeout(() => {
                        if (drillToken !== this.drillToken) return;
                        this._advanceAfterLineEnd(drillToken);
                    }, 150);
                }
            } else {
                // Incorrect
                this.streak = 0;
                this.practicedCount++;
                this.updateStats();

                this.currentPositionHasMistake = true;

                // Add to intra-session blunder buffer for re-drilling
                if (data.prepared_id && !this.sessionLapses.includes(data.prepared_id)) {
                    this.sessionLapses.push(data.prepared_id);
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
                        if (attemptedUci === this.currentDrill.played_uci) {
                            feedback.textContent = `⚠️ That's what you played in your game (${this.currentDrill.played_san})! But your prepared repertoire move is ${this.currentDrill.expected_san}.`;
                        } else {
                            feedback.textContent = `✗ Incorrect. In your game vs ${this.currentDrill.opponent_name || 'Opponent'} you played ${this.currentDrill.played_san}. Your prepared move is ${this.currentDrill.expected_san}.`;
                        }
                    } else {
                        feedback.textContent = `✗ ${data.message}`;
                    }
                }

                if (data.expected_uci) {
                    this.board.clearArrows();
                    this.board.addArrow(data.expected_uci.slice(0, 2), data.expected_uci.slice(2, 4), '#22c55e');
                }

                this.loadMasteryProgress();

                // Reset board back to current position so user can immediately retry with correct move
                this.mistakeResetTimeout = setTimeout(() => {
                    // CODE-07: never repaint a drill that is no longer on screen.
                    if (drillToken !== this.drillToken) return;
                    this.board.loadFEN(this.currentFen);
                    if (this.currentDrill && this.currentDrill.last_move) {
                        this.board.setLastMove(this.currentDrill.last_move.from, this.currentDrill.last_move.to);
                    }
                    if (data.expected_uci) {
                        this.board.clearArrows();
                        this.board.addArrow(data.expected_uci.slice(0, 2), data.expected_uci.slice(2, 4), '#22c55e');
                    }
                }, 800);
            }
        } catch (e) {
            console.error('Error verifying repertoire move:', e);
        }
    }

    // ================================================================== //
    //  Game Deviations Methods (Subtab in Trainer)
    // ================================================================== //
    switchTrainerTab(tab) {
        this.activeTrainerTab = tab;
        const btnDrills = document.getElementById('btnTrainerTabDrills');
        const btnDeviations = document.getElementById('btnTrainerTabDeviations');
        const drillsSection = document.getElementById('trainerDrillsSection');
        const deviationsView = document.getElementById('trainerDeviationsView');
        const modeSelect = document.getElementById('trainModeSelect');

        if (btnDrills) btnDrills.classList.toggle('active', tab === 'drills');
        if (btnDeviations) btnDeviations.classList.toggle('active', tab === 'deviations');
        if (drillsSection) drillsSection.style.display = 'flex';
        if (deviationsView) deviationsView.style.display = 'none';

        if (tab === 'deviations') {
            this.sessionMode = 'deviations';
            if (modeSelect) modeSelect.value = 'deviations';
            this.currentDeviationIndex = 0;
            this.startNewDrill({ index: 0 });
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
            list.innerHTML = '<div style="text-align:center; color:var(--text-muted); padding:2rem;">Analyzing game repertoire deviations...</div>';
        }

        try {
            const colorParam = (this.deviationsFilterColor && this.deviationsFilterColor !== 'all')
                ? `&color=${this.deviationsFilterColor}`
                : '';
            const res = await fetch(`/api/repertoire/deviations?limit=60${colorParam}`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            this.deviationsData = data;

            // Update stats
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
            console.error('Error loading game deviations:', e);
            if (list) {
                list.textContent = '';
                const errDiv = document.createElement('div');
                errDiv.style.cssText = 'text-align:center; color:#ef4444; padding:2rem;';
                errDiv.textContent = 'Could not load game deviations: ' + (e.message || '');
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
            list.innerHTML = `
                <div class="card-panel" style="text-align:center; padding:2.5rem; color:var(--text-muted); background:rgba(15,23,42,0.6);">
                    <div style="font-size:2rem; margin-bottom:0.5rem;">🎉</div>
                    <div style="font-weight:700; color:#fff; font-size:1.1rem; margin-bottom:0.25rem;">No Deviations Found!</div>
                    <div style="font-size:0.85rem;">You followed your prepared opening repertoire in all stored games for this filter.</div>
                </div>
            `;
            return;
        }

        list.innerHTML = devs.map((d, idx) => {
            const resultCls = (d.result === 'win' || d.result === '1-0' && d.user_color === 'white' || d.result === '0-1' && d.user_color === 'black')
                ? 'win'
                : (d.result === 'draw' || d.result === '1/2-1/2')
                    ? 'draw'
                    : 'loss';
            const resultLabel = resultCls.toUpperCase();
            const colorSymbol = d.user_color === 'white' ? '⚪' : '⚫';
            const speed = d.speed ? d.speed.toUpperCase() : 'GAME';
            const rating = d.opponent_rating ? `(${d.opponent_rating})` : '';
            const gameDate = d.played_at ? new Date(d.played_at).toLocaleDateString() : '';

            return `
                <div class="deviation-card" data-idx="${idx}">
                    <div class="deviation-header">
                        <div class="deviation-game-info">
                            <span style="font-size:0.8rem;" title="${d.user_color}">${colorSymbol}</span>
                            <span class="deviation-opponent">vs ${this._escapeHtml(d.opponent_name || 'Opponent')} ${rating}</span>
                            <span class="game-outcome-pill ${resultCls}">${resultLabel}</span>
                            <span class="badge-pill" style="font-size:0.68rem; color:var(--text-muted);">${this._escapeHtml(speed)}</span>
                            <span style="font-size:0.75rem; color:var(--text-muted);">${gameDate}</span>
                        </div>
                        <div class="deviation-opening-tag">
                            <strong>${this._escapeHtml(d.opening_name || 'Opening')}</strong> ${d.eco ? '· ' + this._escapeHtml(d.eco) : ''}
                        </div>
                    </div>

                    <div class="deviation-comparison-grid">
                        <div class="deviation-box played">
                            <span class="deviation-box-label">Played in Game (Move ${d.deviation_move_number})</span>
                            <span class="deviation-move-val" style="color:#f87171;">${this._escapeHtml(d.played_san)}</span>
                        </div>
                        <div class="deviation-arrow-icon">➔</div>
                        <div class="deviation-box prepared">
                            <span class="deviation-box-label">Your Prepared Repertoire</span>
                            <span class="deviation-move-val" style="color:#4ade80;">${this._escapeHtml(d.prepared_san)}</span>
                        </div>
                    </div>

                    ${d.prepared_comment ? `<div class="deviation-note-callout" title="${this._escapeAttr(d.prepared_comment)}">📝 <strong>Plan:</strong> ${this._escapeHtml(d.prepared_comment)}</div>` : ''}

                    <div class="deviation-actions">
                        ${d.game_id ? `<a href="https://lichess.org/${this._escapeAttr(d.game_id)}" target="_blank" rel="noopener noreferrer" class="btn-secondary" style="font-size:0.72rem; padding:0.25rem 0.55rem; text-decoration:none;">↗ Lichess</a>` : ''}
                        <button class="btn-primary btn-drill-deviation" data-idx="${idx}" style="font-size:0.75rem; padding:0.3rem 0.75rem;">
                            🎯 Drill Position
                        </button>
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

        // Switch to dev color if needed
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
