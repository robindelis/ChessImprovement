// Repertoire Studio — Main Controller
class ChessApp {
    constructor() {
        this.currentTab = 'repertoire';
        this.account = null;
        this.fairPlayLocked = false;
        this.activeLiveGameId = null;

        this._bindUI();
        this.init();
    }

    async init() {
        await this.loadAccount();
        this.showTab('repertoire');
    }

    _bindUI() {
        document.querySelectorAll('.nav-tab').forEach(tab => {
            tab.addEventListener('click', () => this.showTab(tab.dataset.tab));
        });

        const btnOpenSettings = document.getElementById('btnOpenSettings');
        const btnCloseSettings = document.getElementById('btnCloseSettingsModal');
        const settingsModal = document.getElementById('settingsModal');

        if (btnOpenSettings) {
            btnOpenSettings.addEventListener('click', () => this.openSettingsModal());
        }
        if (btnCloseSettings) {
            btnCloseSettings.addEventListener('click', () => this.closeSettingsModal());
        }
        if (settingsModal) {
            settingsModal.addEventListener('click', (e) => {
                const rect = settingsModal.getBoundingClientRect();
                const inside = (
                    rect.top <= e.clientY && e.clientY <= rect.top + rect.height &&
                    rect.left <= e.clientX && e.clientX <= rect.left + rect.width
                );
                if (!inside) this.closeSettingsModal();
            });
        }

        const form = document.getElementById('settingsForm');
        if (form) {
            form.addEventListener('submit', (e) => {
                e.preventDefault();
                this.saveSettings();
            });
        }

        const btnSettingsReset = document.getElementById('btnSettingsResetRepertoire');
        if (btnSettingsReset) {
            btnSettingsReset.addEventListener('click', () => {
                this.closeSettingsModal();
                if (window.repertoireBuilder && typeof window.repertoireBuilder.confirmAndResetRepertoire === 'function') {
                    window.repertoireBuilder.confirmAndResetRepertoire();
                }
            });
        }

        const btnOAuth = document.getElementById('btnSettingsOAuthLichess');
        if (btnOAuth) {
            btnOAuth.addEventListener('click', () => this.startLichessOAuth());
        }

        window.addEventListener('focus', () => {
            if (this._awaitingOAuth) {
                this._checkOAuthCompletion();
            }
        });

        if (window.desktopApp && typeof window.desktopApp.onSyncGames === 'function') {
            window.desktopApp.onSyncGames(() => {
                if (window.repertoireBuilder && typeof window.repertoireBuilder.resyncGames === 'function') {
                    window.repertoireBuilder.resyncGames();
                }
            });
        }
    }

    openSettingsModal() {
        this.loadSettings();
        const modal = document.getElementById('settingsModal');
        if (modal && typeof modal.showModal === 'function' && !modal.open) {
            modal.showModal();
        }
    }

    closeSettingsModal() {
        const modal = document.getElementById('settingsModal');
        if (modal && modal.open) {
            modal.close();
        }
    }

    setFairPlayLock(locked, gameId = null) {
        this.fairPlayLocked = Boolean(locked);
        this.activeLiveGameId = gameId;
        document.querySelectorAll('.nav-tab').forEach(t => {
            const tab = t.dataset.tab;
            const disabled = this.fairPlayLocked && tab !== 'play';
            t.classList.toggle('is-locked', disabled);
        });
    }

    async startLichessOAuth() {
        try {
            const res = await fetch('/api/auth/lichess/start', { method: 'POST' });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            if (data.auth_url) {
                this._awaitingOAuth = true;
                const link = document.createElement('a');
                link.href = data.auth_url;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                document.body.appendChild(link);
                link.click();
                link.remove();
                window.showToast?.('Authorize in your browser, then return here.', '#5b9bd5', 5000);
            }
        } catch (e) {
            window.showToast?.(`Could not start Lichess login: ${e.message}`, '#ef4444');
        }
    }

    async _checkOAuthCompletion() {
        try {
            const res = await fetch('/api/auth/lichess/status');
            if (!res.ok) return;
            const st = await res.json();
            if (st.connected && st.can_play) {
                this._awaitingOAuth = false;
                await this.loadAccount();
                await window.livePlay?.refreshAuthStatus();
                this._updateScopeBadge(st);
                window.showToast?.(`Connected to Lichess as ${st.username}`, '#34d399');
            }
        } catch (_) {}
    }

    _updateScopeBadge(st) {
        const badge = document.getElementById('settingsScopeBadge');
        if (!badge || !st) return;
        if (!st.connected) {
            badge.textContent = 'Not connected';
            badge.className = 'muted-tag';
        } else if (st.can_play) {
            badge.textContent = `✓ ${st.username} (Play ready)`;
            badge.className = 'muted-tag text-emerald';
        } else {
            badge.textContent = `${st.username} (Missing board:play)`;
            badge.className = 'muted-tag text-amber';
        }
    }

    showTab(tabName) {
        if (tabName === 'settings') {
            this.openSettingsModal();
            return;
        }
        if (this.fairPlayLocked && tabName !== 'play') {
            window.showToast?.('Fair-Play Lock: Repertoire & Engine tools unlock as soon as your live game finishes.', '#f59e0b', 3500);
            return;
        }
        this.currentTab = tabName;
        document.querySelectorAll('.nav-tab').forEach(t => {
            t.classList.toggle('active', t.dataset.tab === tabName);
        });

        document.querySelectorAll('.view-section').forEach(s => s.classList.remove('active'));

        if (tabName === 'play') {
            const viewPlay = document.getElementById('viewPlay');
            if (viewPlay) viewPlay.classList.add('active');
            window.livePlay?.refreshAuthStatus();
        } else if (tabName === 'analysis') {
            const viewAnalysis = document.getElementById('viewAnalysis');
            if (viewAnalysis) viewAnalysis.classList.add('active');
            window.gameAnalysis?.onShowTab();
        } else if (tabName === 'repertoire') {
            const viewRep = document.getElementById('viewRepertoire');
            if (viewRep) viewRep.classList.add('active');
            if (window.repertoireBuilder) {
                const col = window.repertoireBuilder.activeColor || 'white';
                if (typeof window.repertoireBuilder.loadRepertoire === 'function') {
                    window.repertoireBuilder.loadRepertoire(col);
                } else if (typeof window.repertoireBuilder.refreshTree === 'function') {
                    window.repertoireBuilder.refreshTree();
                }
            }
        } else if (tabName === 'rep-trainer') {
            const viewTrain = document.getElementById('viewRepTrainer');
            if (viewTrain) viewTrain.classList.add('active');
            if (window.repertoireTrainer) {
                window.repertoireTrainer.startNewDrill();
            }
        }
    }

    async loadAccount() {
        try {
            const res = await fetch('/api/account');
            const data = await res.json();
            this.account = data;
            if (data.username) {
                const badge = document.getElementById('userBadge');
                if (badge) badge.style.display = 'inline-flex';
                const uname = document.getElementById('badgeUsername');
                if (uname) uname.textContent = data.username;
                const rating = document.getElementById('badgeRating');
                const rapid = data.perfs?.rapid?.rating || data.perfs?.blitz?.rating || '';
                if (rating) {
                    rating.textContent = rapid ? String(rapid) : 'Local';
                }
            }
        } catch (e) {
            console.error('Account load error:', e);
        }
    }

    async loadSettings() {
        try {
            const res = await fetch('/api/settings');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            const uname = document.getElementById('inputUsername');
            if (uname) uname.value = data.lichess_username || '';
            const tokenInput = document.getElementById('inputToken');
            if (data.has_token && tokenInput) {
                tokenInput.placeholder = '•••••••••••• (configured)';
            }
            if (data.gap_threshold !== undefined && data.gap_threshold !== null) {
                const gapInput = document.getElementById('inputDefaultGap');
                if (gapInput) gapInput.value = String(data.gap_threshold);
                if (window.repertoireBuilder) {
                    const parsedGap = parseFloat(data.gap_threshold);
                    if (!Number.isNaN(parsedGap)) {
                        window.repertoireBuilder.gapThreshold = parsedGap;
                        const sel = document.getElementById('repGapThreshold');
                        if (sel) sel.value = String(parsedGap);
                    }
                }
            }
            if (data.analysis_depth !== undefined && data.analysis_depth !== null) {
                const depthSel = document.getElementById('inputAnalysisDepth');
                if (depthSel) depthSel.value = String(data.analysis_depth);
                const analysisBarSel = document.getElementById('analysisDepthSelect');
                if (analysisBarSel) analysisBarSel.value = String(data.analysis_depth);
            }
            fetch('/api/auth/lichess/status')
                .then(r => r.ok ? r.json() : null)
                .then(st => { if (st) this._updateScopeBadge(st); })
                .catch(() => {});
            if (data.sync_perf_types) {
                const types = data.sync_perf_types.split(',').map(s => s.trim().toLowerCase());
                const checkboxMap = {
                    bullet: 'syncSpeedBullet',
                    blitz: 'syncSpeedBlitz',
                    rapid: 'syncSpeedRapid',
                    classical: 'syncSpeedClassical'
                };
                Object.entries(checkboxMap).forEach(([type, id]) => {
                    const el = document.getElementById(id);
                    if (el) el.checked = types.includes(type);
                });
            }
        } catch (e) {
            console.error('Settings load error:', e);
            window.showToast?.('Could not load settings.', '#ef4444');
        }
    }

    async saveSettings() {
        const tokenInput = document.getElementById('inputToken');
        const token = tokenInput?.value;
        const username = document.getElementById('inputUsername')?.value;
        const gap = document.getElementById('inputDefaultGap')?.value;
        const analysisDepth = document.getElementById('inputAnalysisDepth')?.value;

        const payload = { lichess_username: username };
        if (token) payload.lichess_token = token;
        if (analysisDepth) {
            const d = parseInt(analysisDepth, 10);
            if (!Number.isNaN(d)) payload.analysis_depth = d;
        }
        if (gap !== undefined && gap !== '') {
            const parsedGapPayload = parseFloat(gap);
            if (!Number.isNaN(parsedGapPayload) && parsedGapPayload >= 0.1 && parsedGapPayload <= 100) {
                payload.gap_threshold = parsedGapPayload;
            }
        }

        const speedCheckboxMap = {
            bullet: 'syncSpeedBullet',
            blitz: 'syncSpeedBlitz',
            rapid: 'syncSpeedRapid',
            classical: 'syncSpeedClassical'
        };
        const selectedSpeeds = [];
        Object.entries(speedCheckboxMap).forEach(([type, id]) => {
            const el = document.getElementById(id);
            if (el && el.checked) selectedSpeeds.push(type);
        });
        if (selectedSpeeds.length > 0) {
            payload.sync_perf_types = selectedSpeeds.join(',');
        }

        try {
            const res = await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            let detail = '';
            try {
                const data = await res.json();
                detail = (data && typeof data.detail === 'string') ? data.detail : '';
            } catch (_) {}
            if (!res.ok) {
                const message = detail || `HTTP ${res.status}`;
                window.showToast?.(`Settings not saved: ${message}`, '#ef4444', 5000);
                return;
            }
        } catch (e) {
            window.showToast?.(`Settings not saved: ${e.message}`, '#ef4444', 5000);
            return;
        }

        window.showToast('Settings saved.', '#34d399');
        if (token && tokenInput) tokenInput.value = '';

        if (gap && window.repertoireBuilder) {
            const parsedGap = parseFloat(gap);
            if (!Number.isNaN(parsedGap)) {
                window.repertoireBuilder.gapThreshold = parsedGap;
                const sel = document.getElementById('repGapThreshold');
                if (sel) sel.value = gap;
            }
        }
        this.closeSettingsModal();
        await this.loadAccount();
        await window.livePlay?.refreshAuthStatus();
    }
}

window.app = new ChessApp();

let studyPromptPending = null;
window.promptStudySelection = function({ repertoire = 'white', defaultName = '', title = 'Save to Study', confirmText = '', mode = 'auto' } = {}) {
    if (studyPromptPending) return studyPromptPending;

    const pending = new Promise((resolve) => {
        const settle = (value) => resolve(value);
        const modal = document.getElementById('studySelectModal');
        if (!modal || typeof modal.showModal !== 'function') {
            const fallback = prompt('Study Name:', defaultName);
            settle(fallback && fallback.trim() ? fallback.trim() : null);
            return;
        }

        const titleEl = document.getElementById('studyModalTitle');
        const colorBadge = document.getElementById('studyModalColorBadge');
        const tabExisting = document.getElementById('tabStudyExisting');
        const tabNew = document.getElementById('tabStudyNew');
        const existingSection = document.getElementById('studyExistingSection');
        const newSection = document.getElementById('studyNewSection');
        const selectDropdown = document.getElementById('studySelectDropdown');
        const newNameInput = document.getElementById('studyNewNameInput');
        const errorEl = document.getElementById('studyModalError');
        const btnCancel = document.getElementById('btnCancelStudyModal');
        const btnClose = document.getElementById('btnCloseStudyModal');
        const btnConfirm = document.getElementById('btnConfirmStudyModal');

        if (titleEl) titleEl.textContent = title;
        if (btnConfirm) {
            btnConfirm.textContent = confirmText || ((mode === 'new') ? 'Create Study' : 'Save');
        }
        if (colorBadge) {
            colorBadge.textContent = repertoire.charAt(0).toUpperCase() + repertoire.slice(1);
        }
        if (errorEl) {
            errorEl.style.display = 'none';
            errorEl.textContent = '';
        }

        let activeTab = (mode === 'new') ? 'new' : 'existing';
        const setTab = (tab) => {
            activeTab = tab;
            if (tabExisting) tabExisting.classList.toggle('active', tab === 'existing');
            if (tabNew) tabNew.classList.toggle('active', tab === 'new');
            if (existingSection) existingSection.style.display = tab === 'existing' ? 'flex' : 'none';
            if (newSection) newSection.style.display = tab === 'new' ? 'flex' : 'none';
            if (errorEl) errorEl.style.display = 'none';
            if (!confirmText && btnConfirm) {
                btnConfirm.textContent = (tab === 'new') ? 'Create Study' : 'Save';
            }
            if (tab === 'new' && newNameInput) {
                setTimeout(() => {
                    try { newNameInput.focus(); newNameInput.select(); } catch (_) {}
                }, 50);
            }
        };

        if (tabExisting) tabExisting.onclick = () => setTab('existing');
        if (tabNew) tabNew.onclick = () => setTab('new');
        if (selectDropdown) selectDropdown.innerHTML = '<option value="">Loading...</option>';

        let promptSettled = false;
        let isCleanedUp = false;
        const isLive = () => !isCleanedUp;

        fetch(`/api/repertoire/studies?repertoire=${encodeURIComponent(repertoire)}`)
            .then(res => (res && res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res ? res.status : 'error'}`))))
            .then(studies => {
                if (!selectDropdown || !isLive()) return;
                selectDropdown.innerHTML = '<option value="">-- Choose Study --</option>';
                if (Array.isArray(studies) && studies.length > 0) {
                    studies.forEach(s => {
                        const opt = document.createElement('option');
                        opt.value = s.name;
                        const countInfo = s.total_moves ? ` (${s.total_moves})` : '';
                        opt.textContent = `${s.name}${countInfo}`;
                        selectDropdown.appendChild(opt);
                    });

                    const builderSelect = document.getElementById('builderStudySelect');
                    let matchedExisting = false;
                    if (builderSelect && builderSelect.value) {
                        selectDropdown.value = builderSelect.value;
                        if (selectDropdown.value === builderSelect.value) matchedExisting = true;
                    }
                    if (!matchedExisting && defaultName) {
                        for (let opt of selectDropdown.options) {
                            if (opt.value && opt.value.toLowerCase() === defaultName.toLowerCase()) {
                                selectDropdown.value = opt.value;
                                matchedExisting = true;
                                break;
                            }
                        }
                    }

                    if (mode === 'new') {
                        setTab('new');
                    } else if (matchedExisting) {
                        setTab('existing');
                    } else if (defaultName) {
                        if (newNameInput) newNameInput.value = defaultName;
                        setTab('new');
                    } else {
                        setTab('existing');
                    }
                } else {
                    if (newNameInput && defaultName) newNameInput.value = defaultName;
                    setTab('new');
                }
            })
            .catch(() => {
                if (!isLive()) return;
                if (selectDropdown) selectDropdown.innerHTML = '<option value="">-- No Studies --</option>';
                setTab('new');
            });

        if (newNameInput) newNameInput.value = defaultName || '';

        const cleanup = () => {
            if (isCleanedUp) return;
            isCleanedUp = true;
            if (btnCancel) btnCancel.onclick = null;
            if (btnClose) btnClose.onclick = null;
            if (btnConfirm) btnConfirm.onclick = null;
            if (newNameInput) newNameInput.onkeydown = null;
            modal.onclose = null;
            modal.onclick = null;
        };

        const settleOnce = (value) => {
            if (promptSettled) return;
            promptSettled = true;
            settle(value);
        };

        const handleCancel = () => {
            cleanup();
            if (modal.open) modal.close();
            settleOnce(null);
        };

        const handleConfirm = async () => {
            let chosenName = '';
            if (activeTab === 'existing') {
                chosenName = selectDropdown ? selectDropdown.value.trim() : '';
                if (!chosenName) {
                    if (errorEl) {
                        errorEl.textContent = 'Select a study or create a new one.';
                        errorEl.style.display = 'block';
                    }
                    return;
                }
            } else {
                chosenName = newNameInput ? newNameInput.value.trim() : '';
                if (!chosenName) {
                    if (errorEl) {
                        errorEl.textContent = 'Enter a study name.';
                        errorEl.style.display = 'block';
                    }
                    if (newNameInput) newNameInput.focus();
                    return;
                }
                try {
                    if (btnConfirm) {
                        btnConfirm.disabled = true;
                        btnConfirm.textContent = 'Saving...';
                    }
                    const createRes = await fetch('/api/repertoire/studies', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ name: chosenName, repertoire: repertoire })
                    });
                    if (!createRes.ok) {
                        let detail = `HTTP ${createRes.status}`;
                        try {
                            const body = await createRes.json();
                            if (body && body.detail) detail = body.detail;
                        } catch (_) {}
                        if (errorEl) {
                            errorEl.textContent = detail;
                            errorEl.style.display = 'block';
                        }
                        if (btnConfirm) {
                            btnConfirm.disabled = false;
                            btnConfirm.textContent = confirmText || ((mode === 'new') ? 'Create Study' : 'Save');
                        }
                        return;
                    }
                } catch (e) {
                    if (errorEl) {
                        errorEl.textContent = e.message;
                        errorEl.style.display = 'block';
                    }
                    if (btnConfirm) {
                        btnConfirm.disabled = false;
                        btnConfirm.textContent = confirmText || ((mode === 'new') ? 'Create Study' : 'Save');
                    }
                    return;
                } finally {
                    if (btnConfirm) btnConfirm.disabled = false;
                }
            }

            cleanup();
            if (modal.open) modal.close();
            settleOnce(chosenName);
        };

        if (newNameInput) {
            newNameInput.onkeydown = (e) => {
                if (e.key === 'Enter') { e.preventDefault(); handleConfirm(); }
                else if (e.key === 'Escape') { e.preventDefault(); handleCancel(); }
            };
        }

        modal.onclick = (e) => {
            const rect = modal.getBoundingClientRect();
            const isInDialog = (
                rect.top <= e.clientY && e.clientY <= rect.top + rect.height &&
                rect.left <= e.clientX && e.clientX <= rect.left + rect.width
            );
            if (!isInDialog) handleCancel();
        };

        if (btnCancel) btnCancel.onclick = handleCancel;
        if (btnClose) btnClose.onclick = handleCancel;
        if (btnConfirm) btnConfirm.onclick = handleConfirm;
        modal.onclose = () => { cleanup(); settleOnce(null); };

        const initialTab = (mode === 'new' || (mode === 'auto' && defaultName)) ? 'new' : 'existing';
        setTab(initialTab);
        try {
            modal.showModal();
        } catch (_) {
            cleanup();
            settleOnce(null);
            return;
        }
        if (initialTab === 'new' && newNameInput) {
            setTimeout(() => {
                try { newNameInput.focus(); newNameInput.select(); } catch (_) {}
            }, 50);
        }
    });

    studyPromptPending = pending;
    const release = () => { if (studyPromptPending === pending) studyPromptPending = null; };
    pending.then(release, release);
    return pending;
};

window.showToast = function(msg, color = '#34d399', duration = 3200) {
    if (!msg) return;
    let container = document.getElementById('appToastContainer');
    if (!container) {
        container = document.createElement('div');
        container.id = 'appToastContainer';
        container.className = 'app-toast-container';
        document.body.appendChild(container);
    }

    const toast = document.createElement('div');
    toast.className = 'app-toast';
    toast.style.borderColor = color;

    const dot = document.createElement('span');
    dot.className = 'app-toast-dot';
    dot.style.background = color;

    const text = document.createElement('span');
    text.className = 'app-toast-text';
    text.textContent = msg;

    toast.appendChild(dot);
    toast.appendChild(text);
    container.appendChild(toast);

    requestAnimationFrame(() => toast.classList.add('visible'));

    setTimeout(() => {
        toast.classList.remove('visible');
        toast.classList.add('leaving');
        setTimeout(() => {
            if (toast.parentNode) toast.parentNode.removeChild(toast);
        }, 240);
    }, duration);
};
