// Full-Game Stockfish 19 Analysis & Guided Repertoire Sequence Builder

class GameAnalysisController {
    constructor() {
        this.board = null;
        this.games = [];
        this.activeGameId = null;
        this.report = null;
        this.rawSteps = [];   // [{ ply, fen_before, fen_after, san, uci, move_number, color }]
        this.currentPly = 0;  // 0 = starting position, 1..N = after ply
        this.orientation = 'white';

        // Interactive Sandbox Sequence (when user plays an alternative move or builds a sequence)
        this.sandboxActive = false;
        this.sandboxBranchPly = 0; // game ply index before divergence
        this.sandboxMoves = [];    // full root->current sequence [{ fen_before, uci, san, move_number, color }]
        this.sandboxPreview = null;

        this.analysisAbort = null;
        this.liveEvalAbort = null;
        this.liveLines = [];
        this.explorerMoves = [];
        this.refutationTimer = null;

        this._initBoard();
        this._bindUI();
    }

    _esc(val) {
        return String(val == null ? '' : val)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    _initBoard() {
        const container = document.getElementById('analysisBoard');
        if (!container || typeof InteractiveChessboard === 'undefined') return;
        this.board = new InteractiveChessboard(container, {
            orientation: 'white',
            interactive: true,
            promptPromotion: true,
            onMove: (from, to, promo, moveRes) => this._onSandboxBoardMove(from, to, promo, moveRes)
        });
    }

    _bindUI() {
        const gameSel = document.getElementById('analysisGameSelect');
        if (gameSel) {
            gameSel.addEventListener('change', () => {
                if (gameSel.value) {
                    this.openGame(gameSel.value, { autoAnalyze: true });
                }
            });
        }

        document.getElementById('btnAnalysisSyncGames')?.addEventListener('click', async () => {
            try {
                window.showToast?.('Syncing latest Lichess games...', '#5b9bd5');
                const res = await fetch('/api/games/sync_recent?max_games=20', { method: 'POST' });
                if (res.ok) {
                    await this.loadRecentGames(true);
                    window.showToast?.('Games synced.', '#34d399');
                }
            } catch (e) {
                window.showToast?.(`Sync failed: ${e.message}`, '#ef4444');
            }
        });

        document.getElementById('btnRunGameAnalysis')?.addEventListener('click', () => {
            if (this.activeGameId) {
                this.runFullAnalysis(this.activeGameId, { force: true });
            }
        });

        document.getElementById('btnAnalysisStart')?.addEventListener('click', () => this.jumpToPly(0));
        document.getElementById('btnAnalysisPrev')?.addEventListener('click', () => this.step(-1));
        document.getElementById('btnAnalysisNext')?.addEventListener('click', () => this.step(1));
        document.getElementById('btnAnalysisEnd')?.addEventListener('click', () => this.jumpToPly(this.rawSteps.length));

        document.getElementById('btnAnalysisPrevMistake')?.addEventListener('click', () => this.jumpCritical(-1));
        document.getElementById('btnAnalysisNextMistake')?.addEventListener('click', () => this.jumpCritical(1));

        document.getElementById('btnAnalysisFlip')?.addEventListener('click', () => {
            this.orientation = this.orientation === 'white' ? 'black' : 'white';
            this.board?.setOrientation(this.orientation);
            this._applyEvalBarOrientation();
        });

        document.getElementById('btnAnalysisWatchRefutation')?.addEventListener('click', () => this._playRefutation());
        document.getElementById('btnAnalysisResetSandbox')?.addEventListener('click', () => this.exitSandbox());
        document.getElementById('btnAnalysisAddOpeningPrefix')?.addEventListener('click', () => this.startSandboxAtCurrentPly());
        document.getElementById('btnAnalysisSaveToRep')?.addEventListener('click', () => this.saveSequenceToRepertoire(false));
        document.getElementById('btnAnalysisTrainNow')?.addEventListener('click', () => this.saveAndTrainNow());

        window.addEventListener('keydown', (ev) => {
            if (window.app?.currentTab !== 'analysis') return;
            const tag = (ev.target?.tagName || '').toLowerCase();
            if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
            if (ev.key === 'ArrowLeft') { ev.preventDefault(); this.step(-1); }
            else if (ev.key === 'ArrowRight') { ev.preventDefault(); this.step(1); }
            else if (ev.key === 'Home') { ev.preventDefault(); this.jumpToPly(0); }
            else if (ev.key === 'End') { ev.preventDefault(); this.jumpToPly(this.rawSteps.length); }
        });
    }

    async onShowTab() {
        await this.loadRecentGames(false);
        if (!this.activeGameId && this.games.length > 0) {
            await this.openGame(this.games[0].id, { autoAnalyze: false });
        }
    }

    async loadRecentGames(autoSelectNewest = false) {
        try {
            const res = await fetch('/api/games/recent?limit=50');
            if (!res.ok) return;
            const data = await res.json();
            this.games = data.games || [];
            const sel = document.getElementById('analysisGameSelect');
            if (sel) {
                const current = this.activeGameId || sel.value;
                sel.innerHTML = '<option value="">Select Game...</option>' + this.games.map(g => {
                    const userAcc = g.user_color === 'white' ? g.white_accuracy : g.black_accuracy;
                    const accTxt = userAcc != null ? ` · ${Math.round(userAcc)}%` : '';
                    const resLabel = g.result === 'win' ? 'W' : (g.result === 'loss' ? 'L' : 'D');
                    return `<option value="${this._esc(g.id)}">[${resLabel}] vs ${this._esc(g.opponent_name)} · ${this._esc(g.opening_name)}${accTxt}</option>`;
                }).join('');
                if (autoSelectNewest && this.games.length > 0) {
                    sel.value = this.games[0].id;
                    await this.openGame(this.games[0].id, { autoAnalyze: false });
                } else if (current) {
                    sel.value = current;
                }
            }
        } catch (_) {}
    }

    async _loadStudiesForColor(color) {
        try {
            const res = await fetch(`/api/repertoire/studies?repertoire=${encodeURIComponent(color)}`);
            if (!res.ok) return;
            const data = await res.json();
            const studies = Array.isArray(data) ? data : (data.studies || []);
            const sel = document.getElementById('analysisTargetStudySelect');
            if (sel) {
                sel.innerHTML = '<option value="">No Study</option>' + studies.map(s =>
                    `<option value="${this._esc(s.name)}">${this._esc(s.name)}</option>`
                ).join('');
            }
        } catch (_) {}
    }

    async openGame(gameId, { autoAnalyze = true } = {}) {
        if (!gameId) return;
        this._clearRefutationTimer();
        this.exitSandbox(false);
        this.activeGameId = gameId;

        const sel = document.getElementById('analysisGameSelect');
        if (sel && sel.value !== gameId) {
            await this.loadRecentGames(false);
            sel.value = gameId;
        }

        const targetDepth = parseInt(document.getElementById('analysisDepthSelect')?.value || '19', 10);

        try {
            const res = await fetch(`/api/analysis/${encodeURIComponent(gameId)}?cached_only=true`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();

            if (data.status === 'ready' && data.analysis) {
                this._ingestReport(data.analysis);
                if (autoAnalyze && (data.analysis.depth || 0) < targetDepth) {
                    await this.runFullAnalysis(gameId, { force: true });
                }
            } else if (data.game) {
                this._ingestRawGame(data.game);
                if (autoAnalyze) {
                    await this.runFullAnalysis(gameId, { force: false });
                }
            }
        } catch (e) {
            window.showToast?.(`Could not open game: ${e.message}`, '#ef4444');
        }
    }

    _ingestRawGame(g) {
        this.report = null;
        this.orientation = g.user_color === 'black' ? 'black' : 'white';
        this.board?.setOrientation(this.orientation);
        this._applyEvalBarOrientation();
        this._loadStudiesForColor(this.orientation);

        const title = document.getElementById('analysisGameTitle');
        const eco = document.getElementById('analysisOpeningEco');
        if (title) title.textContent = `${g.white_player} vs ${g.black_player} · ${g.opening_name}`;
        if (eco) eco.textContent = g.eco || '';
        const accStrip = document.getElementById('analysisAccuracyStrip');
        if (accStrip) accStrip.style.display = 'none';

        const chess = new Chess();
        this.rawSteps = [];
        const tokens = (g.moves_san || '').trim().split(/\s+/).filter(t => t && !t.endsWith('.') && !/^\d+$/.test(t));
        for (let i = 0; i < tokens.length; i++) {
            const fenBefore = chess.fen();
            const mv = chess.move(tokens[i], { sloppy: true });
            if (!mv) break;
            this.rawSteps.push({
                ply: i + 1,
                move_number: Math.ceil((i + 1) / 2),
                color: i % 2 === 0 ? 'white' : 'black',
                san: mv.san,
                uci: mv.from + mv.to + (mv.promotion || ''),
                fen_before: fenBefore,
                fen_after: chess.fen()
            });
        }
        this.currentPly = 0;
        this._renderEvalGraph();
        this._renderScoreSheet();
        this._renderCriticalPills();
        this._updateBoardAndCoach();
    }

    _ingestReport(report) {
        this.report = report;
        this.orientation = report.user_color === 'black' ? 'black' : 'white';
        this.board?.setOrientation(this.orientation);
        this._applyEvalBarOrientation();
        this._loadStudiesForColor(this.orientation);

        const title = document.getElementById('analysisGameTitle');
        const eco = document.getElementById('analysisOpeningEco');
        if (title) {
            title.textContent = `${report.white_player} vs ${report.black_player} · ${report.opening_name} (d${report.depth})`;
        }
        if (eco) eco.textContent = report.eco || '';

        const accStrip = document.getElementById('analysisAccuracyStrip');
        const wAcc = document.getElementById('analysisWhiteAcc');
        const bAcc = document.getElementById('analysisBlackAcc');
        if (accStrip) accStrip.style.display = 'inline-flex';
        if (wAcc) wAcc.textContent = `${report.white_accuracy ?? '--'}%`;
        if (bAcc) bAcc.textContent = `${report.black_accuracy ?? '--'}%`;

        this.rawSteps = (report.plies || []).map(p => ({
            ply: p.ply,
            move_number: p.move_number,
            color: p.color,
            san: p.san,
            uci: p.uci,
            fen_before: p.fen_before,
            fen_after: p.fen_after
        }));

        // Jump straight to the #1 critical opening trap / deviation if present!
        const topMoment = (report.critical_moments || [])[0];
        this.currentPly = topMoment ? Math.max(0, topMoment.ply - 1) : 0;

        this._renderClassSummary();
        this._renderCriticalPills();
        this._renderEvalGraph();
        this._renderScoreSheet();
        this._updateBoardAndCoach();
    }

    async runFullAnalysis(gameId, { force = false } = {}) {
        if (this.analysisAbort) {
            try { this.analysisAbort.abort(); } catch (_) {}
        }
        const ctrl = new AbortController();
        this.analysisAbort = ctrl;

        const depth = parseInt(document.getElementById('analysisDepthSelect')?.value || '19', 10);
        const btn = document.getElementById('btnRunGameAnalysis');
        const badge = document.getElementById('analysisProgressBadge');
        if (btn) { btn.disabled = true; btn.textContent = 'Analyzing...'; }
        if (badge) { badge.style.display = 'inline'; badge.textContent = '0%'; }

        try {
            const url = `/api/analysis/${encodeURIComponent(gameId)}/stream?depth=${depth}&force=${force ? 'true' : 'false'}`;
            const res = await fetch(url, { signal: ctrl.signal });
            if (!res.ok || !res.body) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `HTTP ${res.status}`);
            }
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buf = '';

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                buf += decoder.decode(value, { stream: true });
                const blocks = buf.split('\n\n');
                buf = blocks.pop() || '';
                for (const block of blocks) {
                    const dataLine = block.split('\n').find(l => l.startsWith('data: '));
                    if (!dataLine) continue;
                    const msg = JSON.parse(dataLine.slice(6));
                    if (msg.type === 'progress' && badge) {
                        badge.textContent = `${msg.percent || 0}%`;
                    } else if (msg.type === 'complete' && msg.analysis) {
                        this._ingestReport(msg.analysis);
                        this.loadRecentGames(false);
                    } else if (msg.type === 'error') {
                        throw new Error(msg.error || 'Analysis failed');
                    }
                }
            }
        } catch (e) {
            if (!ctrl.signal.aborted) {
                window.showToast?.(`Analysis error: ${e.message}`, '#ef4444');
            }
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = 'Analyze'; }
            if (badge) badge.style.display = 'none';
        }
    }

    _applyEvalBarOrientation() {
        const wrapper = document.getElementById('analysisEvalBarWrapper');
        if (wrapper) {
            wrapper.classList.toggle('flipped', this.orientation === 'black');
        }
    }

    _updateEvalBar(whiteCp, whiteMate) {
        const fill = document.getElementById('analysisEvalBarFill');
        const label = document.getElementById('analysisEvalBarLabel');
        if (!fill || !label) return;

        let cp = whiteCp;
        if (whiteMate != null) {
            cp = whiteMate > 0 ? 10000 : -10000;
            label.textContent = `M${Math.abs(whiteMate)}`;
        } else if (cp != null) {
            const pawns = cp / 100;
            label.textContent = `${pawns >= 0 ? '+' : ''}${pawns.toFixed(1)}`;
        } else {
            cp = 0;
            label.textContent = '0.0';
        }

        const pct = Math.max(3, Math.min(97, 100 / (1 + Math.exp(-0.004 * cp))));
        fill.style.height = `${pct}%`;
        label.className = `eval-bar-label ${cp > 35 ? 'white-winning' : (cp < -35 ? 'black-winning' : 'equal')}`;
    }

    _renderClassSummary() {
        const el = document.getElementById('analysisClassSummary');
        if (!el || !this.report) return;
        const c = (this.report.counts || {})[this.report.user_color] || {};
        el.innerHTML = `
            <span class="cls-chip cls-best" title="Best moves">★ ${c.best || 0}</span>
            <span class="cls-chip cls-inacc" title="Inaccuracies">?! ${c.inaccuracy || 0}</span>
            <span class="cls-chip cls-mistake" title="Mistakes">? ${c.mistake || 0}</span>
            <span class="cls-chip cls-blunder" title="Blunders">?? ${c.blunder || 0}</span>
        `;
    }

    _renderCriticalPills() {
        const wrap = document.getElementById('analysisCriticalPills');
        if (!wrap) return;
        const moments = this.report?.critical_moments || [];
        if (!moments.length) {
            wrap.innerHTML = '';
            wrap.style.display = 'none';
            return;
        }
        wrap.style.display = 'flex';
        wrap.innerHTML = moments.map(m => {
            const tone = m.kind === 'opening_trap' || m.classification === 'blunder'
                ? 'crit-blunder'
                : (m.kind === 'deviation' ? 'crit-dev' : 'crit-mistake');
            const tag = m.kind === 'opening_trap'
                ? `Trap ${m.move_number}.${m.played_san}`
                : (m.kind === 'deviation' ? `Dev ${m.move_number}.${m.played_san}` : `${m.move_number}.${m.played_san}`);
            return `<button type="button" class="crit-pill ${tone}" data-target-ply="${m.ply - 1}">${this._esc(tag)}</button>`;
        }).join('');
        wrap.querySelectorAll('button[data-target-ply]').forEach(btn => {
            btn.addEventListener('click', () => {
                this.exitSandbox(false);
                this.jumpToPly(parseInt(btn.dataset.targetPly, 10));
            });
        });
    }

    _renderEvalGraph() {
        const svg = document.getElementById('analysisEvalSvg');
        if (!svg) return;
        const plies = this.report?.plies || [];
        if (!plies.length) {
            svg.innerHTML = '<line x1="0" y1="38" x2="600" y2="38" stroke="rgba(255,255,255,0.12)" stroke-width="1"/>';
            return;
        }

        const W = 600;
        const H = 76;
        const pts = [{ ply: 0, win: this.report?.root_win_pct ?? 52, cls: 'book' }];
        plies.forEach(p => pts.push({ ply: p.ply, win: p.win_pct, cls: p.classification, is_user: p.is_user }));

        const maxPly = Math.max(1, pts.length - 1);
        const xOf = (i) => (i / maxPly) * W;
        const yOf = (win) => H - (Math.max(2, Math.min(98, win)) / 100) * H;

        const coords = pts.map((p, i) => `${xOf(i).toFixed(1)},${yOf(p.win).toFixed(1)}`).join(' ');
        const areaCoords = `0,${H} ${coords} ${W},${H}`;
        const curX = xOf(Math.min(this.currentPly, maxPly)).toFixed(1);

        let markers = '';
        if (this.report?.first_novelty_ply) {
            const nx = xOf(this.report.first_novelty_ply).toFixed(1);
            markers += `<line x1="${nx}" y1="0" x2="${nx}" y2="${H}" stroke="#d4a853" stroke-dasharray="3,3" stroke-width="1.4"/>`;
        }
        pts.forEach((p, i) => {
            if (!p.is_user) return;
            if (p.cls === 'blunder' || p.cls === 'mistake') {
                const fill = p.cls === 'blunder' ? '#ef4444' : '#f59e0b';
                markers += `<circle cx="${xOf(i).toFixed(1)}" cy="${yOf(p.win).toFixed(1)}" r="3.4" fill="${fill}"/>`;
            }
        });

        svg.innerHTML = `
            <rect x="0" y="0" width="${W}" height="${H}" fill="#0d1016"/>
            <polygon points="${areaCoords}" fill="rgba(225,232,242,0.22)"/>
            <line x1="0" y1="${H / 2}" x2="${W}" y2="${H / 2}" stroke="rgba(255,255,255,0.16)" stroke-width="1"/>
            <polyline points="${coords}" fill="none" stroke="#eef2f7" stroke-width="1.8"/>
            ${markers}
            <line id="analysisGraphCursor" x1="${curX}" y1="0" x2="${curX}" y2="${H}" stroke="#5b9bd5" stroke-width="2"/>
        `;

        svg.onclick = (ev) => {
            const rect = svg.getBoundingClientRect();
            const ratio = Math.max(0, Math.min(1, (ev.clientX - rect.left) / Math.max(1, rect.width)));
            const ply = Math.round(ratio * maxPly);
            this.exitSandbox(false);
            this.jumpToPly(ply);
        };
    }

    _renderScoreSheet() {
        const sheet = document.getElementById('analysisMoveSheet');
        if (!sheet) return;
        const plies = this.report?.plies || this.rawSteps;
        if (!plies.length) {
            sheet.innerHTML = '<div class="muted-tag" style="padding:0.75rem;">No moves loaded</div>';
            return;
        }

        const glyph = (p) => {
            if (!p.classification) return '';
            if (p.repertoire_status === 'repertoire_hit') return '<span class="ply-tag tag-rep">Rep</span>';
            if (p.repertoire_status === 'repertoire_deviation') return '<span class="ply-tag tag-dev">Dev</span>';
            if (p.repertoire_status === 'opponent_novelty') return '<span class="ply-tag tag-nov">New</span>';
            if (p.classification === 'blunder') return '<span class="ply-tag tag-blunder">??</span>';
            if (p.classification === 'mistake') return '<span class="ply-tag tag-mistake">?</span>';
            if (p.classification === 'inaccuracy') return '<span class="ply-tag tag-inacc">?!</span>';
            return '';
        };

        let html = '';
        for (let i = 0; i < plies.length; i += 2) {
            const w = plies[i];
            const b = plies[i + 1];
            html += `
                <div class="score-row">
                    <span class="score-num">${w.move_number}.</span>
                    <button type="button" class="score-ply-btn ${!this.sandboxActive && this.currentPly === w.ply ? 'active' : ''}" data-ply="${w.ply}">
                        <span>${this._esc(w.san)}</span>${glyph(w)}
                    </button>
                    ${b ? `
                        <button type="button" class="score-ply-btn ${!this.sandboxActive && this.currentPly === b.ply ? 'active' : ''}" data-ply="${b.ply}">
                            <span>${this._esc(b.san)}</span>${glyph(b)}
                        </button>
                    ` : '<span></span>'}
                </div>
            `;
        }
        sheet.innerHTML = html;
        sheet.querySelectorAll('button[data-ply]').forEach(btn => {
            btn.addEventListener('click', () => {
                this.exitSandbox(false);
                this.jumpToPly(parseInt(btn.dataset.ply, 10));
            });
        });
    }

    step(delta) {
        this._clearRefutationTimer();
        if (this.sandboxActive) {
            if (delta < 0 && this.sandboxMoves.length > this.sandboxBranchPly) {
                this.sandboxMoves.pop();
                if (this.sandboxMoves.length <= this.sandboxBranchPly) {
                    this.exitSandbox(true);
                } else {
                    this._refreshSandboxView();
                }
            }
            return;
        }
        this.jumpToPly(Math.max(0, Math.min(this.rawSteps.length, this.currentPly + delta)));
    }

    jumpToPly(ply) {
        this._clearRefutationTimer();
        this.currentPly = Math.max(0, Math.min(this.rawSteps.length, ply));
        this._renderScoreSheet();
        const cursor = document.getElementById('analysisGraphCursor');
        if (cursor && this.rawSteps.length > 0) {
            const x = ((this.currentPly / this.rawSteps.length) * 600).toFixed(1);
            cursor.setAttribute('x1', x);
            cursor.setAttribute('x2', x);
        }
        this._updateBoardAndCoach();
    }

    jumpCritical(dir) {
        const moments = (this.report?.critical_moments || []).slice().sort((a, b) => a.ply - b.ply);
        if (!moments.length) return;
        this.exitSandbox(false);
        const targets = moments.map(m => m.ply - 1);
        if (dir > 0) {
            const next = targets.find(t => t > this.currentPly);
            this.jumpToPly(next !== undefined ? next : targets[0]);
        } else {
            const prev = [...targets].reverse().find(t => t < this.currentPly);
            this.jumpToPly(prev !== undefined ? prev : targets[targets.length - 1]);
        }
    }

    _currentFen() {
        if (this.sandboxActive && this.sandboxMoves.length > 0) {
            const chess = new Chess();
            for (const m of this.sandboxMoves) {
                chess.move({ from: m.uci.slice(0, 2), to: m.uci.slice(2, 4), promotion: m.uci.slice(4) || undefined });
            }
            return chess.fen();
        }
        if (this.currentPly === 0 || !this.rawSteps.length) {
            return 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
        }
        return this.rawSteps[this.currentPly - 1].fen_after;
    }

    _updateBoardAndCoach() {
        const fen = this._currentFen();
        if (this.board) {
            this.board.loadFEN(fen);
            let lastUci = null;
            if (this.sandboxActive && this.sandboxMoves.length > 0) {
                lastUci = this.sandboxMoves[this.sandboxMoves.length - 1].uci;
            } else if (this.currentPly > 0 && this.rawSteps[this.currentPly - 1]) {
                lastUci = this.rawSteps[this.currentPly - 1].uci;
            }
            if (lastUci) {
                this.board.setLastMove(lastUci.slice(0, 2), lastUci.slice(2, 4));
            }
        }

        // Show pre-computed eval + arrows immediately when on main game line
        const nextPlyObj = (!this.sandboxActive && this.report?.plies) ? this.report.plies[this.currentPly] : null;
        const justPlayedObj = (!this.sandboxActive && this.currentPly > 0 && this.report?.plies)
            ? this.report.plies[this.currentPly - 1]
            : null;

        if (!this.sandboxActive && this.report) {
            if (justPlayedObj) {
                this._updateEvalBar(justPlayedObj.eval_cp, justPlayedObj.mate);
            } else {
                this._updateEvalBar(this.report.root_eval_cp, null);
            }
        }

        // Draw played mistake arrow vs best move arrow when standing right before a mistake/trap!
        if (this.board && nextPlyObj && nextPlyObj.is_user) {
            const arrows = [];
            if (nextPlyObj.best_uci && nextPlyObj.best_uci.length >= 4) {
                arrows.push({ from: nextPlyObj.best_uci.slice(0, 2), to: nextPlyObj.best_uci.slice(2, 4), color: '#34d399' });
            }
            if (nextPlyObj.uci && nextPlyObj.uci !== nextPlyObj.best_uci && ['blunder', 'mistake', 'inaccuracy'].includes(nextPlyObj.classification)) {
                arrows.push({ from: nextPlyObj.uci.slice(0, 2), to: nextPlyObj.uci.slice(2, 4), color: '#ef4444' });
            }
            this.board.setOverlayArrows(arrows);
        }

        this._renderCoachCallout(nextPlyObj, justPlayedObj);
        this._fetchLivePositionGuidance(fen, nextPlyObj);
    }

    _renderCoachCallout(nextPlyObj, justPlayedObj) {
        const callout = document.getElementById('analysisMomentCallout');
        const watchBtn = document.getElementById('btnAnalysisWatchRefutation');
        if (!callout) return;

        if (this.sandboxActive) {
            const turn = this._currentFen().split(' ')[1] === 'w' ? 'white' : 'black';
            const userColor = this.report?.user_color || this.orientation;
            if (turn === userColor) {
                callout.innerHTML = `<strong>Your Turn in Sequence:</strong> Pick an engine-approved move below or play on the board.`;
            } else {
                callout.innerHTML = `<strong>Opponent Reply:</strong> Click a common 2000+ reply below to prepare your follow-up, or save now.`;
            }
            if (watchBtn) watchBtn.style.display = 'none';
            return;
        }

        const focus = (nextPlyObj && nextPlyObj.is_user && (
            ['blunder', 'mistake', 'inaccuracy'].includes(nextPlyObj.classification)
            || nextPlyObj.repertoire_status === 'repertoire_deviation'
        )) ? nextPlyObj : justPlayedObj;

        if (!focus) {
            callout.textContent = this.report
                ? 'Step through moves or select a critical moment pill to build a repertoire fix.'
                : 'Click Analyze to run Stockfish 19 across every move of this game.';
            if (watchBtn) watchBtn.style.display = 'none';
            return;
        }

        const hasRefutation = Array.isArray(focus.refutation_pv) && focus.refutation_pv.length > 0
            && ['blunder', 'mistake', 'inaccuracy'].includes(focus.classification);
        if (watchBtn) {
            watchBtn.style.display = hasRefutation ? 'inline-flex' : 'none';
            watchBtn.dataset.ply = String(focus.ply);
        }

        if (focus.repertoire_status === 'repertoire_deviation') {
            callout.innerHTML = `
                <div class="coach-diag-title text-amber">Move ${focus.move_number}: Repertoire Deviation (${this._esc(focus.san)})</div>
                <div class="coach-diag-sub">Your repertoire has <strong>${this._esc(focus.prepared_san)}</strong>. Play it below to extend the line, or drill this deviation in Trainer.</div>
            `;
        } else if (['blunder', 'mistake', 'inaccuracy'].includes(focus.classification)) {
            const label = focus.ply <= 36 && focus.repertoire_status === 'unprepared' ? 'Opening Trap / Gap' : focus.classification.toUpperCase();
            callout.innerHTML = `
                <div class="coach-diag-title text-crimson">Move ${focus.move_number}: ${this._esc(label)} on ${this._esc(focus.san)} (−${(focus.cpl / 100).toFixed(1)})</div>
                <div class="coach-diag-sub">Best avoidance: <strong>${this._esc(focus.best_san || '--')}</strong>. Click a safe move below to play out and save the refutation into your repertoire.</div>
            `;
        } else if (focus.repertoire_status === 'opponent_novelty') {
            callout.innerHTML = `
                <div class="coach-diag-title text-accent">Move ${focus.move_number}: Opponent Left Your Repertoire (${this._esc(focus.san)})</div>
                <div class="coach-diag-sub">Pick your response on the next turn to cover this branch.</div>
            `;
        } else {
            callout.innerHTML = `
                <div class="coach-diag-title">${focus.move_number}${focus.color === 'white' ? '.' : '...'} ${this._esc(focus.san)} (${this._esc(focus.classification)})</div>
                <div class="coach-diag-sub">Play any alternative directly on the board to branch and add to your repertoire.</div>
            `;
        }
    }

    async _fetchLivePositionGuidance(fen, nextPlyObj) {
        if (this.liveEvalAbort) {
            try { this.liveEvalAbort.abort(); } catch (_) {}
        }
        const ctrl = new AbortController();
        this.liveEvalAbort = ctrl;

        const userColor = this.report?.user_color || this.orientation;
        const turnColor = fen.split(' ')[1] === 'w' ? 'white' : 'black';
        const isUserTurn = turnColor === userColor;

        const headerEl = document.getElementById('analysisCandidatesHeader');
        if (headerEl) {
            headerEl.textContent = isUserTurn ? 'Safe Moves (Stockfish 19 + 2000+)' : 'Common 2000+ Opponent Replies';
        }

        // Render precomputed MultiPV candidates immediately if available
        if (isUserTurn && nextPlyObj?.candidates?.length) {
            this._renderCandidatesList(nextPlyObj.candidates, true, fen);
        }

        // Fetch 2000+ Explorer moves in parallel
        fetch(`/api/repertoire/explore?fen=${encodeURIComponent(fen)}&color=${userColor}`, { signal: ctrl.signal })
            .then(r => r.ok ? r.json() : null)
            .then(exp => {
                if (ctrl.signal.aborted || !exp) return;
                this.explorerMoves = exp.moves || [];
                if (!isUserTurn) {
                    this._renderOpponentReplies(this.explorerMoves, fen);
                } else if (this.liveLines.length) {
                    this._mergeAndRenderUserCandidates(this.liveLines, this.explorerMoves, fen);
                }
            })
            .catch(() => {});

        // Stream MultiPV-3 Stockfish 19 for current position
        const depth = Math.min(20, parseInt(document.getElementById('analysisDepthSelect')?.value || '19', 10));
        try {
            const res = await fetch(
                `/api/repertoire/eval_stream?fen=${encodeURIComponent(fen)}&depth=${depth}&multipv=3`,
                { signal: ctrl.signal }
            );
            if (!res.ok || !res.body) return;
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buf = '';
            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                buf += decoder.decode(value, { stream: true });
                const blocks = buf.split('\n\n');
                buf = blocks.pop() || '';
                for (const b of blocks) {
                    const dl = b.split('\n').find(l => l.startsWith('data: '));
                    if (!dl) continue;
                    const frame = JSON.parse(dl.slice(6));
                    if (frame.type !== 'eval') continue;
                    const tag = document.getElementById('analysisLiveDepthTag');
                    if (tag) tag.textContent = `SF 19 · d${frame.depth}`;
                    if (this.sandboxActive || !this.report) {
                        this._updateEvalBar(frame.score_cp, frame.mate);
                    }
                    if (Array.isArray(frame.lines) && frame.lines.length) {
                        this.liveLines = frame.lines;
                        if (isUserTurn && (!nextPlyObj?.candidates?.length || this.sandboxActive)) {
                            this._mergeAndRenderUserCandidates(this.liveLines, this.explorerMoves, fen);
                        }
                    }
                }
            }
        } catch (_) {}
    }

    _mergeAndRenderUserCandidates(lines, expMoves, fen) {
        const expByUci = {};
        (expMoves || []).forEach(m => { expByUci[m.uci] = m; });
        const userColor = this.report?.user_color || this.orientation;
        const merged = lines.map(l => {
            const exp = expByUci[l.best_move] || {};
            return {
                rank: l.rank,
                uci: l.best_move,
                san: l.best_san,
                score_cp: l.score_cp,
                user_cp: userColor === 'white' ? l.score_cp : -(l.score_cp || 0),
                mate: l.mate,
                pv: l.pv || [],
                human_games: exp.total_games || 0,
                popularity_pct: exp.popularity_pct || 0,
                human_score_pct: exp.win_rate ?? null
            };
        });
        this._renderCandidatesList(merged, true, fen);
    }

    _renderCandidatesList(candidates, isUserTurn, fen) {
        const listEl = document.getElementById('analysisCandidatesList');
        if (!listEl) return;
        if (!candidates || !candidates.length) {
            listEl.innerHTML = '<div class="muted-tag">Evaluating position...</div>';
            return;
        }

        listEl.innerHTML = candidates.map(c => {
            const cpText = c.mate != null
                ? `M${Math.abs(c.mate)}`
                : `${(c.score_cp / 100) >= 0 ? '+' : ''}${(c.score_cp / 100).toFixed(2)}`;
            const pvSans = (c.pv || []).slice(0, 5).map(x => x.san).join(' ');
            const humanBadge = c.human_games > 0
                ? `<span class="muted-tag">${c.popularity_pct}% · ${c.human_games}g</span>`
                : '';
            return `
                <div class="cand-row">
                    <div class="cand-left">
                        <span class="cand-rank">#${c.rank}</span>
                        <strong class="mono-bold">${this._esc(c.san)}</strong>
                        <span class="eval-badge">${this._esc(cpText)}</span>
                        ${humanBadge}
                        <span class="cand-pv mono-muted">${this._esc(pvSans)}</span>
                    </div>
                    <button type="button" class="btn-secondary btn-xs" data-play-uci="${this._esc(c.uci)}" data-play-san="${this._esc(c.san)}">Play</button>
                </div>
            `;
        }).join('');

        listEl.querySelectorAll('button[data-play-uci]').forEach(btn => {
            btn.addEventListener('click', () => {
                this._applyMoveToSandbox(fen, btn.dataset.playUci, btn.dataset.playSan);
            });
        });
    }

    _renderOpponentReplies(expMoves, fen) {
        const listEl = document.getElementById('analysisCandidatesList');
        if (!listEl) return;
        const top = (expMoves || []).slice(0, 4);
        if (!top.length) {
            listEl.innerHTML = '<div class="muted-tag">Play opponent reply on the board or save sequence now.</div>';
            return;
        }
        listEl.innerHTML = top.map((m, idx) => `
            <div class="cand-row">
                <div class="cand-left">
                    <span class="cand-rank">${idx + 1}.</span>
                    <strong class="mono-bold">${this._esc(m.san)}</strong>
                    <span class="badge-pill">${m.popularity_pct}%</span>
                    <span class="muted-tag">${m.total_games} games (2000+)</span>
                </div>
                <button type="button" class="btn-secondary btn-xs" data-opp-uci="${this._esc(m.uci)}" data-opp-san="${this._esc(m.san)}">Reply &rarr;</button>
            </div>
        `).join('');
        listEl.querySelectorAll('button[data-opp-uci]').forEach(btn => {
            btn.addEventListener('click', () => {
                this._applyMoveToSandbox(fen, btn.dataset.oppUci, btn.dataset.oppSan);
            });
        });
    }

    _clearRefutationTimer() {
        if (this.refutationTimer) {
            clearInterval(this.refutationTimer);
            this.refutationTimer = null;
        }
    }

    _playRefutation() {
        this._clearRefutationTimer();
        const watchBtn = document.getElementById('btnAnalysisWatchRefutation');
        const plyNum = parseInt(watchBtn?.dataset.ply || String(this.currentPly + 1), 10);
        const plyObj = (this.report?.plies || []).find(p => p.ply === plyNum);
        if (!plyObj || !plyObj.refutation_pv?.length || !this.board) return;

        const chess = new Chess(plyObj.fen_after);
        this.board.loadFEN(plyObj.fen_after);
        this.board.setLastMove(plyObj.uci.slice(0, 2), plyObj.uci.slice(2, 4));

        let idx = 0;
        const pv = plyObj.refutation_pv;
        this.refutationTimer = setInterval(() => {
            if (idx >= pv.length) {
                this._clearRefutationTimer();
                return;
            }
            const step = pv[idx++];
            const from = step.uci.slice(0, 2);
            const to = step.uci.slice(2, 4);
            const promo = step.uci.slice(4) || undefined;
            this.board.playMove(from, to, promo, true);
        }, 650);
    }

    // ------------------------------------------------------------------ #
    // Interactive Sequence Sandbox -> Repertoire Saver
    // ------------------------------------------------------------------ #

    startSandboxAtCurrentPly() {
        if (!this.rawSteps.length && this.currentPly === 0) return;
        this.sandboxActive = true;
        this.sandboxBranchPly = this.currentPly;
        this.sandboxMoves = this.rawSteps.slice(0, this.currentPly).map(s => ({
            fen_before: s.fen_before,
            uci: s.uci,
            san: s.san,
            move_number: s.move_number,
            color: s.color
        }));
        this._refreshSandboxView();
    }

    _onSandboxBoardMove(from, to, promo, moveRes) {
        const uci = `${from}${to}${promo || ''}`;
        const san = moveRes?.san || uci;

        // If on main line and user plays the exact next game move, just step forward!
        if (!this.sandboxActive && this.rawSteps[this.currentPly]?.uci === uci) {
            this.jumpToPly(this.currentPly + 1);
            return;
        }

        if (!this.sandboxActive) {
            this.sandboxActive = true;
            this.sandboxBranchPly = this.currentPly;
            this.sandboxMoves = this.rawSteps.slice(0, this.currentPly).map(s => ({
                fen_before: s.fen_before,
                uci: s.uci,
                san: s.san,
                move_number: s.move_number,
                color: s.color
            }));
        }

        const fenBefore = this._sandboxLastFenAfter();
        const turnIsWhite = fenBefore.split(' ')[1] === 'w';
        const ply = this.sandboxMoves.length + 1;
        this.sandboxMoves.push({
            fen_before: fenBefore,
            uci,
            san,
            move_number: Math.ceil(ply / 2),
            color: turnIsWhite ? 'white' : 'black'
        });
        this._refreshSandboxView();
    }

    _sandboxLastFenAfter() {
        const chess = new Chess();
        for (const m of this.sandboxMoves) {
            chess.move({ from: m.uci.slice(0, 2), to: m.uci.slice(2, 4), promotion: m.uci.slice(4) || undefined });
        }
        return chess.fen();
    }

    _applyMoveToSandbox(fenBefore, uci, san) {
        if (!this.sandboxActive) {
            this.sandboxActive = true;
            this.sandboxBranchPly = this.currentPly;
            this.sandboxMoves = this.rawSteps.slice(0, this.currentPly).map(s => ({
                fen_before: s.fen_before,
                uci: s.uci,
                san: s.san,
                move_number: s.move_number,
                color: s.color
            }));
        }
        const ply = this.sandboxMoves.length + 1;
        const turnIsWhite = fenBefore.split(' ')[1] === 'w';
        this.sandboxMoves.push({
            fen_before: fenBefore,
            uci,
            san,
            move_number: Math.ceil(ply / 2),
            color: turnIsWhite ? 'white' : 'black'
        });
        this._refreshSandboxView();
    }

    exitSandbox(rerender = true) {
        this.sandboxActive = false;
        this.sandboxMoves = [];
        this.sandboxPreview = null;
        const panel = document.getElementById('analysisSequencePanel');
        if (panel) panel.style.display = 'none';
        if (rerender) {
            this._updateBoardAndCoach();
        }
    }

    async _refreshSandboxView() {
        const panel = document.getElementById('analysisSequencePanel');
        if (panel) panel.style.display = 'flex';
        this._updateBoardAndCoach();

        const rep = this.report?.user_color || this.orientation;
        try {
            const res = await fetch('/api/analysis/preview_sequence', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repertoire: rep,
                    moves: this.sandboxMoves.map(m => ({ fen_before: m.fen_before, uci: m.uci, san: m.san }))
                })
            });
            if (!res.ok) return;
            const preview = await res.json();
            this.sandboxPreview = preview;

            const badge = document.getElementById('analysisSeqDiffBadge');
            if (badge) {
                if (preview.conflict_count > 0) {
                    badge.textContent = `+${preview.new_count} new · ${preview.conflict_count} conflict`;
                    badge.className = 'badge-pill text-amber';
                } else {
                    badge.textContent = `${preview.existing_count} in rep · +${preview.new_count} new`;
                    badge.className = 'badge-pill text-emerald';
                }
            }

            const crumbs = document.getElementById('analysisSeqBreadcrumbs');
            if (crumbs) {
                crumbs.innerHTML = (preview.steps || []).map(s => {
                    const num = s.color === 'white' ? `${s.move_number}.` : (s.index === 0 ? `${s.move_number}...` : '');
                    const cls = s.status === 'conflict' ? 'seq-chip-conflict' : (s.status === 'new' ? 'seq-chip-new' : 'seq-chip-existing');
                    return `<span class="seq-chip ${cls}">${num}${this._esc(s.san)}</span>`;
                }).join(' ');
            }
        } catch (_) {}
    }

    async saveSequenceToRepertoire(replaceConflicts = false) {
        if (!this.sandboxMoves.length) {
            window.showToast?.('Play or select moves to add first.', '#f59e0b');
            return false;
        }
        const rep = this.report?.user_color || this.orientation;
        const studyName = document.getElementById('analysisTargetStudySelect')?.value || null;
        try {
            const res = await fetch('/api/analysis/save_sequence', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repertoire: rep,
                    moves: this.sandboxMoves.map(m => ({ fen_before: m.fen_before, uci: m.uci, san: m.san })),
                    study_name: studyName,
                    replace_conflicts: Boolean(replaceConflicts),
                    prioritize_for_training: true,
                    note: this.report ? `Added from game vs ${rep === 'white' ? this.report.black_player : this.report.white_player}` : null
                })
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                window.showToast?.(data.detail || 'Could not save sequence', '#ef4444');
                return false;
            }
            if (data.status === 'conflict') {
                const detail = (data.conflicts || [])
                    .map(c => `Move ${c.move_number}: replace ${c.existing_san} with ${c.proposed_san}?`)
                    .join('\n');
                if (confirm(`${data.message}\n\n${detail}`)) {
                    return this.saveSequenceToRepertoire(true);
                }
                return false;
            }
            window.showToast?.(`Saved +${data.added_moves} move(s) to ${rep} repertoire`, '#34d399');
            await this._refreshSandboxView();
            if (window.repertoireBuilder?.refreshTree) {
                window.repertoireBuilder.refreshTree();
            }
            return true;
        } catch (e) {
            window.showToast?.(`Save error: ${e.message}`, '#ef4444');
            return false;
        }
    }

    async saveAndTrainNow() {
        const saved = await this.saveSequenceToRepertoire(false);
        if (!saved && (!this.sandboxPreview || this.sandboxPreview.new_count > 0)) return;
        const rep = this.report?.user_color || this.orientation;
        const study = document.getElementById('analysisTargetStudySelect')?.value || '';
        window.app?.showTab('rep-trainer');
        if (window.repertoireTrainer) {
            window.repertoireTrainer.activeColor = rep;
            if (study) window.repertoireTrainer.selectedOpening = study;
            window.repertoireTrainer.startNewDrill();
        }
    }
}

window.gameAnalysis = new GameAnalysisController();
