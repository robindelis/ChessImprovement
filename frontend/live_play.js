// Live Lichess Play Controller (Board API + Ticking Clocks + Fair-Play Lock)

class LivePlayController {
    constructor() {
        this.board = null;
        this.state = {
            seeking: false,
            seek: null,
            game: null,
            incoming_challenges: [],
            outgoing_challenge: null,
            fair_play_locked: false,
            server_now_ms: Date.now()
        };
        this.auth = { connected: false, can_play: false, username: '' };
        this.clockSkewMs = 0;
        this.selectedRated = true;
        this.selectedColor = 'random';
        this.lobbyMode = 'pool';
        this.orientation = 'white';
        this.resignConfirmUntil = 0;
        this.lastRenderedGameId = null;
        this.lastRenderedPly = -1;
        this.sseAbort = null;
        this.clockTimer = null;

        this._initBoard();
        this._bindUI();
        if (document.getElementById('viewPlay')) {
            this.refreshAuthStatus();
            this.connectStream();
        }
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
        const container = document.getElementById('playBoard');
        if (!container || typeof InteractiveChessboard === 'undefined') return;
        this.board = new InteractiveChessboard(container, {
            orientation: 'white',
            interactive: false,
            movableColor: null,
            promptPromotion: true,
            onMove: (from, to, promo) => this._handleBoardMove(from, to, promo)
        });
    }

    _bindUI() {
        document.getElementById('btnPlayFlip')?.addEventListener('click', () => {
            this.orientation = this.orientation === 'white' ? 'black' : 'white';
            this.board?.setOrientation(this.orientation);
            this._renderPlayerBars();
        });

        ['btnPlayTabPool', 'btnPlayTabAi', 'btnPlayTabUser'].forEach(id => {
            const btn = document.getElementById(id);
            if (btn) {
                btn.addEventListener('click', () => this._switchLobbyMode(btn.dataset.mode || 'pool'));
            }
        });

        ['btnPlayRated', 'btnPlayCasual'].forEach(id => {
            const btn = document.getElementById(id);
            if (btn) {
                btn.addEventListener('click', () => {
                    this.selectedRated = btn.dataset.rated === 'true';
                    document.getElementById('btnPlayRated')?.classList.toggle('active', this.selectedRated);
                    document.getElementById('btnPlayCasual')?.classList.toggle('active', !this.selectedRated);
                });
            }
        });

        ['btnPlayColorWhite', 'btnPlayColorRandom', 'btnPlayColorBlack'].forEach(id => {
            const btn = document.getElementById(id);
            if (btn) {
                btn.addEventListener('click', () => {
                    this.selectedColor = btn.dataset.color || 'random';
                    ['btnPlayColorWhite', 'btnPlayColorRandom', 'btnPlayColorBlack'].forEach(otherId => {
                        const el = document.getElementById(otherId);
                        if (el) el.classList.toggle('active', el.dataset.color === this.selectedColor);
                    });
                });
            }
        });

        const seekGrid = document.getElementById('playSeekGrid');
        if (seekGrid) {
            seekGrid.querySelectorAll('.seek-tile').forEach(tile => {
                tile.addEventListener('click', () => {
                    const mins = parseInt(tile.dataset.min || '10', 10);
                    const inc = parseInt(tile.dataset.inc || '0', 10);
                    this.startSeek(mins, inc);
                });
            });
        }

        document.getElementById('btnPlayCancelSeek')?.addEventListener('click', () => this.cancelSeek());
        document.getElementById('btnPlayStartAi')?.addEventListener('click', () => this.startAiChallenge());
        document.getElementById('btnPlaySendChallenge')?.addEventListener('click', () => this.sendUserChallenge());
        document.getElementById('btnPlayConnectLichess')?.addEventListener('click', () => window.app?.startLichessOAuth());

        document.getElementById('btnPlayAbort')?.addEventListener('click', () => this.sendAction('abort'));
        document.getElementById('btnPlayOfferDraw')?.addEventListener('click', () => this.sendAction('draw_offer'));
        document.getElementById('btnPlayAcceptDraw')?.addEventListener('click', () => this.sendAction('draw_accept'));
        document.getElementById('btnPlayDeclineDraw')?.addEventListener('click', () => this.sendAction('draw_decline'));

        document.getElementById('btnPlayResign')?.addEventListener('click', () => {
            const now = Date.now();
            const btn = document.getElementById('btnPlayResign');
            if (now < this.resignConfirmUntil) {
                this.resignConfirmUntil = 0;
                if (btn) btn.textContent = 'Resign';
                this.sendAction('resign');
            } else {
                this.resignConfirmUntil = now + 3500;
                if (btn) btn.textContent = 'Confirm?';
                setTimeout(() => {
                    if (Date.now() >= this.resignConfirmUntil && btn) {
                        btn.textContent = 'Resign';
                    }
                }, 3600);
            }
        });

        document.getElementById('btnPlayAnalyzeFinished')?.addEventListener('click', () => {
            const g = this.state.game;
            if (!g || !g.id) return;
            window.app?.showTab('analysis');
            window.gameAnalysis?.openGame(g.id, { autoAnalyze: true });
        });

        document.getElementById('btnPlayNewSeek')?.addEventListener('click', () => {
            const g = this.state.game;
            if (g && g.id) {
                this.sendAction('dismiss');
            }
        });
    }

    _switchLobbyMode(mode) {
        this.lobbyMode = mode;
        ['pool', 'ai', 'user'].forEach(m => {
            const tabId = m === 'pool' ? 'btnPlayTabPool' : (m === 'ai' ? 'btnPlayTabAi' : 'btnPlayTabUser');
            const paneId = m === 'pool' ? 'playModePool' : (m === 'ai' ? 'playModeAi' : 'playModeUser');
            document.getElementById(tabId)?.classList.toggle('active', m === mode);
            const pane = document.getElementById(paneId);
            if (pane) pane.style.display = m === mode ? 'flex' : 'none';
        });
        const ratedWrap = document.getElementById('playRatedToggleWrap');
        if (ratedWrap) ratedWrap.style.display = mode === 'ai' ? 'none' : 'inline-flex';
    }

    async refreshAuthStatus() {
        try {
            const res = await fetch('/api/auth/lichess/status');
            if (!res.ok) return;
            this.auth = await res.json();
            this._renderAuthGate();
        } catch (_) {}
    }

    _renderAuthGate() {
        const gate = document.getElementById('playAuthGate');
        const msg = document.getElementById('playAuthGateMsg');
        const statusLbl = document.getElementById('playLobbyStatus');
        if (!gate) return;

        if (!this.auth.connected) {
            gate.style.display = 'block';
            if (msg) msg.textContent = 'Sign in with Lichess to play live games';
            if (statusLbl) statusLbl.textContent = 'Disconnected';
        } else if (!this.auth.can_play) {
            gate.style.display = 'block';
            if (msg) msg.textContent = `${this.auth.username}: token needs board:play permission`;
            if (statusLbl) statusLbl.textContent = 'Read-only token';
        } else {
            gate.style.display = 'none';
            if (statusLbl) statusLbl.textContent = this.auth.username || 'Ready';
        }
    }

    async connectStream() {
        if (this.sseAbort) {
            try { this.sseAbort.abort(); } catch (_) {}
        }
        const ctrl = new AbortController();
        this.sseAbort = ctrl;

        try {
            const res = await fetch('/api/play/stream', { signal: ctrl.signal });
            if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buf = '';

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                buf += decoder.decode(value, { stream: true });
                const parts = buf.split('\n\n');
                buf = parts.pop() || '';
                for (const block of parts) {
                    const dataLine = block.split('\n').find(l => l.startsWith('data: '));
                    if (!dataLine) continue;
                    try {
                        const payload = JSON.parse(dataLine.slice(6));
                        if (payload.type === 'ping') {
                            if (payload.server_now_ms) {
                                this.clockSkewMs = payload.server_now_ms - Date.now();
                            }
                        } else {
                            this._applyServerState(payload);
                        }
                    } catch (_) {}
                }
            }
        } catch (err) {
            if (ctrl.signal.aborted) return;
            setTimeout(() => this.connectStream(), 3000);
        }
    }

    _applyServerState(payload) {
        const prevStatus = this.state.game?.status;
        const prevId = this.state.game?.id;
        if (payload.server_now_ms) {
            this.clockSkewMs = payload.server_now_ms - Date.now();
        }
        this.state = { ...this.state, ...payload };

        const locked = Boolean(this.state.fair_play_locked);
        window.app?.setFairPlayLock(locked, this.state.game?.id || null);

        const dot = document.getElementById('navPlayLiveDot');
        if (dot) dot.style.display = locked ? 'inline-block' : 'none';

        const game = this.state.game;
        if (game && game.id !== this.lastRenderedGameId) {
            this.lastRenderedGameId = game.id;
            this.lastRenderedPly = -1;
            this.orientation = game.user_color === 'black' ? 'black' : 'white';
            this.board?.setOrientation(this.orientation);
            if (locked && window.app?.currentTab !== 'play') {
                window.app?.showTab('play');
            }
        }

        if (game && prevId === game.id && prevStatus === 'started' && game.status !== 'started') {
            window.chessAudio?.playSuccess?.();
        }

        if (game && game.status === 'started') {
            this._startClockLoop();
        } else {
            this._stopClockLoop();
        }

        this._renderAll();
    }

    _renderAll() {
        this._renderChallenges();
        const game = this.state.game;
        const lobbyCard = document.getElementById('playLobbyCard');
        const gameCard = document.getElementById('playGameCard');

        if (game) {
            if (lobbyCard) lobbyCard.style.display = 'none';
            if (gameCard) gameCard.style.display = 'flex';
            this._renderGameBoardAndCard(game);
        } else {
            if (lobbyCard) lobbyCard.style.display = 'flex';
            if (gameCard) gameCard.style.display = 'none';
            this._renderLobbyState();
            this._renderIdleBoard();
        }
        this._renderPlayerBars();
        this._tickClocks();
    }

    _renderIdleBoard() {
        if (!this.board) return;
        this.lastRenderedGameId = null;
        this.lastRenderedPly = -1;
        this.board.options.interactive = false;
        this.board.options.movableColor = null;
        this.board.loadFEN('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1');
    }

    _renderLobbyState() {
        const banner = document.getElementById('playSeekingBanner');
        const lbl = document.getElementById('playSeekingLabel');
        const seeking = Boolean(this.state.seeking && this.state.seek);
        if (banner) banner.style.display = seeking ? 'flex' : 'none';
        if (seeking && lbl) {
            const s = this.state.seek;
            lbl.textContent = `Seeking ${s.minutes}+${s.increment} (${s.rated ? 'Rated' : 'Casual'})...`;
        }
        document.querySelectorAll('#playSeekGrid .seek-tile').forEach(tile => {
            const active = seeking
                && String(this.state.seek.minutes) === tile.dataset.min
                && String(this.state.seek.increment) === tile.dataset.inc;
            tile.classList.toggle('is-seeking', active);
        });
    }

    _renderChallenges() {
        const strip = document.getElementById('playIncomingChallenges');
        if (!strip) return;
        const list = this.state.incoming_challenges || [];
        if (!list.length) {
            strip.style.display = 'none';
            strip.innerHTML = '';
            return;
        }
        strip.style.display = 'flex';
        strip.innerHTML = list.map(ch => `
            <div class="challenge-item">
                <span><strong>${this._esc(ch.challenger)}</strong> (${this._esc(ch.rating || '?')}) · ${this._esc(ch.time_text || ch.speed)}</span>
                <div class="challenge-item-btns">
                    <button type="button" class="btn-primary btn-xs" data-ch-id="${this._esc(ch.id)}" data-accept="true">Accept</button>
                    <button type="button" class="btn-secondary btn-xs" data-ch-id="${this._esc(ch.id)}" data-accept="false">Decline</button>
                </div>
            </div>
        `).join('');
        strip.querySelectorAll('button[data-ch-id]').forEach(btn => {
            btn.addEventListener('click', () => {
                this.respondChallenge(btn.dataset.chId, btn.dataset.accept === 'true');
            });
        });
    }

    _renderGameBoardAndCard(game) {
        const moves = game.moves || [];
        const ply = moves.length;
        const isStarted = game.status === 'started' || game.status === 'created';
        const userTurn = isStarted && game.turn === game.user_color;

        if (this.board) {
            if (ply !== this.lastRenderedPly) {
                const prevPly = this.lastRenderedPly;
                this.lastRenderedPly = ply;
                this.board.loadFEN(game.fen);
                if (ply > 0) {
                    const last = moves[ply - 1];
                    const from = last.uci.slice(0, 2);
                    const to = last.uci.slice(2, 4);
                    this.board.setLastMove(from, to);
                    if (prevPly >= 0 && ply === prevPly + 1) {
                        this.board.animateGhostMove(from, to, 150);
                        window.chessAudio?.playMove?.();
                    }
                }
            }
            this.board.options.interactive = isStarted;
            this.board.options.movableColor = userTurn ? (game.user_color === 'white' ? 'w' : 'b') : 'none';
        }

        const speedBadge = document.getElementById('playGameSpeedBadge');
        if (speedBadge) {
            speedBadge.textContent = `${String(game.speed || 'RAPID').toUpperCase()}${game.rated ? ' · RATED' : ''}`;
        }

        const turnEl = document.getElementById('playGameTurnStatus');
        if (turnEl) {
            if (isStarted) {
                turnEl.textContent = userTurn ? 'Your Turn' : 'Opponent Thinking';
                turnEl.className = userTurn ? 'mono-bold text-emerald' : 'mono-muted';
            } else {
                turnEl.textContent = this._formatResultBadge(game);
                turnEl.className = 'mono-bold text-accent';
            }
        }

        const drawStrip = document.getElementById('playDrawOfferStrip');
        if (drawStrip) {
            const oppColor = game.user_color === 'white' ? 'black' : 'white';
            drawStrip.style.display = (isStarted && game.draw_offer_from === oppColor) ? 'flex' : 'none';
        }

        const sheet = document.getElementById('playMoveList');
        if (sheet) {
            let html = '';
            for (let i = 0; i < moves.length; i += 2) {
                const w = moves[i];
                const b = moves[i + 1];
                html += `
                    <div class="score-row">
                        <span class="score-num">${w.move_number}.</span>
                        <span class="score-san ${i === moves.length - 1 ? 'active' : ''}">${this._esc(w.san)}</span>
                        <span class="score-san ${b && i + 1 === moves.length - 1 ? 'active' : ''}">${b ? this._esc(b.san) : ''}</span>
                    </div>
                `;
            }
            sheet.innerHTML = html || '<div class="muted-tag" style="padding:0.75rem;">Make or await first move</div>';
            sheet.scrollTop = sheet.scrollHeight;
        }

        const activeCtrls = document.getElementById('playActiveControls');
        const finishedCtrls = document.getElementById('playFinishedControls');
        const abortBtn = document.getElementById('btnPlayAbort');
        const drawBtn = document.getElementById('btnPlayOfferDraw');
        const summaryEl = document.getElementById('playResultSummary');

        if (isStarted) {
            if (activeCtrls) activeCtrls.style.display = 'flex';
            if (finishedCtrls) finishedCtrls.style.display = 'none';
            if (abortBtn) abortBtn.style.display = ply < 2 ? 'inline-flex' : 'none';
            if (drawBtn) drawBtn.style.display = ply >= 2 ? 'inline-flex' : 'none';
        } else {
            if (activeCtrls) activeCtrls.style.display = 'none';
            if (finishedCtrls) finishedCtrls.style.display = 'flex';
            if (summaryEl) summaryEl.textContent = this._formatResultHeadline(game);
        }
    }

    _formatResultBadge(game) {
        if (game.status === 'aborted') return 'Aborted';
        if (!game.winner) return 'Draw · ½-½';
        return game.winner === 'white' ? '1-0' : '0-1';
    }

    _formatResultHeadline(game) {
        const statusMap = {
            mate: 'Checkmate',
            resign: 'Resignation',
            outoftime: 'Time out',
            stalemate: 'Stalemate',
            draw: 'Draw',
            aborted: 'Game aborted'
        };
        const reason = statusMap[game.status] || game.status;
        if (game.status === 'aborted') return 'Game Aborted';
        if (!game.winner) return `Draw (${reason})`;
        const won = game.winner === game.user_color;
        return `${won ? 'Victory' : 'Defeat'} · ${reason}`;
    }

    _renderPlayerBars() {
        const g = this.state.game;
        const bottomIsWhite = this.orientation === 'white';

        const topColor = bottomIsWhite ? 'black' : 'white';
        const botColor = bottomIsWhite ? 'white' : 'black';

        const topSwatch = document.getElementById('playTopSwatch');
        const botSwatch = document.getElementById('playBottomSwatch');
        if (topSwatch) topSwatch.className = `swatch swatch-${topColor}`;
        if (botSwatch) botSwatch.className = `swatch swatch-${botColor}`;

        const myName = this.auth.username || window.app?.account?.username || 'You';
        const topName = g ? (topColor === 'white' ? g.white_name : g.black_name) : 'Opponent';
        const botName = g ? (botColor === 'white' ? g.white_name : g.black_name) : myName;
        const topRating = g ? (topColor === 'white' ? g.white_rating : g.black_rating) : '';
        const botRating = g ? (botColor === 'white' ? g.white_rating : g.black_rating) : '';

        const elTopName = document.getElementById('playTopName');
        const elBotName = document.getElementById('playBottomName');
        const elTopRat = document.getElementById('playTopRating');
        const elBotRat = document.getElementById('playBottomRating');

        if (elTopName) elTopName.textContent = topName || 'Opponent';
        if (elBotName) elBotName.textContent = botName || myName;
        if (elTopRat) elTopRat.textContent = topRating ? String(topRating) : '--';
        if (elBotRat) elBotRat.textContent = botRating ? String(botRating) : '--';
    }

    _startClockLoop() {
        if (this.clockTimer) return;
        this.clockTimer = setInterval(() => this._tickClocks(), 100);
        if (this.clockTimer && typeof this.clockTimer.unref === 'function') {
            this.clockTimer.unref();
        }
    }

    _stopClockLoop() {
        if (this.clockTimer) {
            clearInterval(this.clockTimer);
            this.clockTimer = null;
        }
    }

    _formatClockMs(ms) {
        const clamped = Math.max(0, ms);
        if (clamped < 10000) {
            const secs = Math.floor(clamped / 1000);
            const tenths = Math.floor((clamped % 1000) / 100);
            return `0:0${secs}.${tenths}`;
        }
        const totalSec = Math.ceil(clamped / 1000);
        const mins = Math.floor(totalSec / 60);
        const secs = totalSec % 60;
        return `${mins}:${secs < 10 ? '0' : ''}${secs}`;
    }

    _tickClocks() {
        const topClock = document.getElementById('playTopClock');
        const botClock = document.getElementById('playBottomClock');
        if (!topClock || !botClock) return;

        const g = this.state.game;
        if (!g) {
            const s = this.state.seek;
            const defaultStr = s ? `${s.minutes}:00` : '10:00';
            topClock.textContent = defaultStr;
            botClock.textContent = defaultStr;
            topClock.className = 'digital-clock';
            botClock.className = 'digital-clock';
            return;
        }

        const nowServer = Date.now() + this.clockSkewMs;
        const isStarted = g.status === 'started';
        const bothMoved = (g.moves || []).length >= 2;
        const elapsed = (isStarted && bothMoved) ? Math.max(0, nowServer - (g.clock_synced_at_ms || nowServer)) : 0;

        const wMs = g.turn === 'white' ? Math.max(0, (g.wtime_ms || 0) - elapsed) : (g.wtime_ms || 0);
        const bMs = g.turn === 'black' ? Math.max(0, (g.btime_ms || 0) - elapsed) : (g.btime_ms || 0);

        const bottomIsWhite = this.orientation === 'white';
        const topMs = bottomIsWhite ? bMs : wMs;
        const botMs = bottomIsWhite ? wMs : bMs;
        const topTurn = isStarted && g.turn === (bottomIsWhite ? 'black' : 'white');
        const botTurn = isStarted && g.turn === (bottomIsWhite ? 'white' : 'black');

        topClock.textContent = this._formatClockMs(topMs);
        botClock.textContent = this._formatClockMs(botMs);

        topClock.className = `digital-clock${topTurn ? ' is-active' : ''}${topTurn && topMs < 30000 ? ' is-low' : ''}`;
        botClock.className = `digital-clock${botTurn ? ' is-active' : ''}${botTurn && botMs < 30000 ? ' is-low' : ''}`;
    }

    async _handleBoardMove(from, to, promo) {
        const g = this.state.game;
        if (!g || g.status !== 'started') return;
        const uci = `${from}${to}${promo || ''}`;
        this.board.options.movableColor = 'none';
        try {
            const res = await fetch('/api/play/move', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ game_id: g.id, move_uci: uci })
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `HTTP ${res.status}`);
            }
        } catch (e) {
            window.showToast?.(e.message || 'Move rejected', '#ef4444');
            this.lastRenderedPly = -1;
            this._renderGameBoardAndCard(g);
        }
    }

    async startSeek(minutes, increment) {
        if (this.state.seeking && this.state.seek?.minutes === minutes && this.state.seek?.increment === increment) {
            return this.cancelSeek();
        }
        try {
            const res = await fetch('/api/play/seek', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    minutes,
                    increment,
                    rated: this.selectedRated,
                    color: this.selectedColor
                })
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                window.showToast?.(data.detail || `Could not start seek (${res.status})`, '#ef4444', 4500);
                return;
            }
            this._applyServerState(data);
        } catch (e) {
            window.showToast?.(`Seek error: ${e.message}`, '#ef4444');
        }
    }

    async cancelSeek() {
        try {
            const res = await fetch('/api/play/seek/cancel', { method: 'POST' });
            if (res.ok) {
                this._applyServerState(await res.json());
            }
        } catch (_) {}
    }

    async startAiChallenge() {
        const level = parseInt(document.getElementById('playAiLevelSelect')?.value || '5', 10);
        const [limitSec, incSec] = (document.getElementById('playAiClockSelect')?.value || '600,0')
            .split(',').map(v => parseInt(v, 10));
        try {
            const res = await fetch('/api/play/challenge', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    mode: 'ai',
                    ai_level: level,
                    clock_limit_sec: limitSec || 600,
                    increment_sec: incSec || 0,
                    color: this.selectedColor
                })
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                window.showToast?.(data.detail || 'Could not start AI game', '#ef4444');
                return;
            }
            this._applyServerState(data);
        } catch (e) {
            window.showToast?.(`Challenge error: ${e.message}`, '#ef4444');
        }
    }

    async sendUserChallenge() {
        const username = (document.getElementById('playChallengeUsername')?.value || '').trim();
        if (!username) {
            window.showToast?.('Enter a Lichess username', '#f59e0b');
            return;
        }
        const [limitSec, incSec] = (document.getElementById('playUserClockSelect')?.value || '600,0')
            .split(',').map(v => parseInt(v, 10));
        try {
            const res = await fetch('/api/play/challenge', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    mode: 'user',
                    username,
                    clock_limit_sec: limitSec || 600,
                    increment_sec: incSec || 0,
                    rated: this.selectedRated,
                    color: this.selectedColor
                })
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                window.showToast?.(data.detail || 'Challenge rejected', '#ef4444');
                return;
            }
            window.showToast?.(`Challenge sent to ${username}`, '#34d399');
            this._applyServerState(data);
        } catch (e) {
            window.showToast?.(`Challenge error: ${e.message}`, '#ef4444');
        }
    }

    async respondChallenge(challengeId, accept) {
        try {
            const res = await fetch('/api/play/challenge/respond', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ challenge_id: challengeId, accept })
            });
            if (res.ok) {
                this._applyServerState(await res.json());
            }
        } catch (_) {}
    }

    async sendAction(action) {
        const g = this.state.game;
        if (!g || !g.id) return;
        try {
            const res = await fetch('/api/play/action', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ game_id: g.id, action })
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                window.showToast?.(data.detail || `Action failed (${res.status})`, '#ef4444');
                return;
            }
            if (action === 'draw_offer') {
                window.showToast?.('Draw offer sent', '#5b9bd5');
            } else if (action === 'dismiss') {
                this._applyServerState(data);
            }
        } catch (e) {
            window.showToast?.(`Error: ${e.message}`, '#ef4444');
        }
    }
}

window.livePlay = new LivePlayController();
