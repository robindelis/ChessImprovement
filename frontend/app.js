// Chess Opening Improvement Platform - Main App Controller
class ChessApp {
    constructor() {
        this.currentTab = 'repertoire';
        this.account = null;

        this._bindUI();
        this.init();
    }

    async init() {
        await this.loadAccount();
        this.showTab('repertoire');
    }

    _bindUI() {
        // Nav tabs
        document.querySelectorAll('.nav-tab').forEach(tab => {
            tab.addEventListener('click', () => this.showTab(tab.dataset.tab));
        });

        // Settings form
        const form = document.getElementById('settingsForm');
        if (form) {
            form.addEventListener('submit', (e) => {
                e.preventDefault();
                this.saveSettings();
            });
        }

        // Settings Danger Zone reset button
        const btnSettingsReset = document.getElementById('btnSettingsResetRepertoire');
        if (btnSettingsReset) {
            btnSettingsReset.addEventListener('click', () => {
                if (window.repertoireBuilder && typeof window.repertoireBuilder.confirmAndResetRepertoire === 'function') {
                    window.repertoireBuilder.confirmAndResetRepertoire();
                }
            });
        }

        // Electron desktop menu integration
        if (window.desktopApp && typeof window.desktopApp.onSyncGames === 'function') {
            window.desktopApp.onSyncGames(() => {
                if (window.repertoireBuilder && typeof window.repertoireBuilder.resyncGames === 'function') {
                    window.repertoireBuilder.resyncGames();
                }
            });
        }
    }

    showTab(tabName) {
        this.currentTab = tabName;
        document.querySelectorAll('.nav-tab').forEach(t => {
            t.classList.toggle('active', t.dataset.tab === tabName);
        });

        document.querySelectorAll('.view-section').forEach(s => s.classList.remove('active'));

        if (tabName === 'repertoire') {
            document.body?.classList?.remove('scout-studio-active');
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
            document.body?.classList?.remove('scout-studio-active');
            const viewTrain = document.getElementById('viewRepTrainer');
            if (viewTrain) viewTrain.classList.add('active');
            if (window.repertoireTrainer) {
                window.repertoireTrainer.startNewDrill();
            }
        } else if (tabName === 'scout') {
            const viewScout = document.getElementById('viewScout');
            if (viewScout) viewScout.classList.add('active');
            if (viewScout?.classList?.contains('studio-mode')) {
                document.body?.classList?.add('scout-studio-active');
            }
            const scoutInst = window.openingScout || window.videoScout;
            if (scoutInst && scoutInst.board) {
                scoutInst.board.render();
            }
        } else if (tabName === 'settings') {
            document.body?.classList?.remove('scout-studio-active');
            const viewSettings = document.getElementById('viewSettings');
            if (viewSettings) viewSettings.classList.add('active');
            this.loadSettings();
        }
    }

    async loadAccount() {
        try {
            const res = await fetch('/api/account');
            const data = await res.json();
            this.account = data;
            if (data.username) {
                const badge = document.getElementById('userBadge');
                if (badge) badge.style.display = 'flex';
                const uname = document.getElementById('badgeUsername');
                if (uname) uname.textContent = data.username;
                const rating = document.getElementById('badgeRating');
                const rapid = data.perfs?.rapid?.rating || data.perfs?.blitz?.rating || 'Unrated';
                if (rating) rating.textContent = `${rapid} Rapid`;
            }
        } catch (e) {
            console.error('Account load error:', e);
        }
    }

    async loadSettings() {
        try {
            const res = await fetch('/api/settings');
            if (!res.ok) {
                throw new Error(`Server responded ${res.status}`);
            }
            const data = await res.json();
            const uname = document.getElementById('inputUsername');
            if (uname) uname.value = data.lichess_username || '';
            const tokenInput = document.getElementById('inputToken');
            if (data.has_token && tokenInput) {
                tokenInput.placeholder = '•••••••••••••••••••• (configured)';
            }
            const geminiInput = document.getElementById('inputGeminiKey');
            if (data.has_gemini_key && geminiInput) {
                geminiInput.placeholder = '•••••••••••••••••••• (configured)';
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
            window.showToast?.('Could not load settings from the server.', '#ef4444');
        }
    }

    async saveSettings() {
        const tokenInput = document.getElementById('inputToken');
        const token = tokenInput?.value;
        const username = document.getElementById('inputUsername')?.value;
        const geminiInput = document.getElementById('inputGeminiKey');
        const geminiKey = geminiInput?.value;
        const gap = document.getElementById('inputDefaultGap')?.value;

        const payload = {
            lichess_username: username
        };
        if (token) payload.lichess_token = token;
        if (geminiKey) payload.gemini_api_key = geminiKey;
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

        // CODE-08: the success path used to run unconditionally - `res.ok` and
        // the server's `detail` were both ignored, so a 400/422/500 still showed
        // "Settings saved successfully!". Every non-2xx answer is now surfaced.
        let data = null;
        try {
            const res = await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            let detail = '';
            try {
                data = await res.json();
                detail = (data && typeof data.detail === 'string') ? data.detail : '';
            } catch (_) {
                detail = '';
            }
            if (!res.ok) {
                const message = detail || `Server responded ${res.status} ${res.statusText || ''}`.trim();
                window.showToast?.(`Settings NOT saved: ${message}`, '#ef4444', 5000);
                return;
            }
        } catch (e) {
            console.error('Error saving settings:', e);
            window.showToast?.(`Settings NOT saved: ${e.message}`, '#ef4444', 5000);
            return;
        }

        window.showToast('Settings saved successfully!', '#4ade80');
        // Never keep the raw `lip_...` secret in the DOM (and never re-submit it
        // on the next save) - the server only ever reports `has_token`.
        if (token && tokenInput) tokenInput.value = '';
        if (geminiKey && geminiInput) geminiInput.value = '';

        if (gap && window.repertoireBuilder) {
            const parsedGap = parseFloat(gap);
            if (!Number.isNaN(parsedGap)) {
                window.repertoireBuilder.gapThreshold = parsedGap;
                const sel = document.getElementById('repGapThreshold');
                if (sel) sel.value = gap;
            } else {
                window.showToast?.('Default gap threshold is not a number - ignored.', '#f59e0b');
            }
        }
        await this.loadAccount();
    }
}

window.app = new ChessApp();

// Mandatory Study Selection & Creation Modal Helper
//
// CODE-18: `modal.showModal()` throws `InvalidStateError` when the dialog is
// already open. That call sat inside the Promise executor, so a double click
// rejected the promise *outside* every caller's try/catch - the import silently
// aborted with no toast at all. A single in-flight promise is now shared by
// every concurrent caller and the modal is never opened twice.
let studyPromptPending = null;
window.promptStudySelection = function({ repertoire = 'white', defaultName = '', title = 'Save Line to Study', confirmText = '', mode = 'auto' } = {}) {
    if (studyPromptPending) return studyPromptPending;

    const pending = new Promise((resolve) => {
        const settle = (value) => resolve(value);
        const modal = document.getElementById('studySelectModal');
        if (!modal || typeof modal.showModal !== 'function') {
            const fallback = prompt('Enter Study Name for this line:', defaultName);
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
        if (confirmText && btnConfirm) {
            btnConfirm.textContent = confirmText;
        } else if (btnConfirm) {
            btnConfirm.textContent = (mode === 'new') ? 'Create Study' : 'Save to Study';
        }

        if (colorBadge) {
            colorBadge.textContent = repertoire.toUpperCase();
            colorBadge.style.background = repertoire === 'white' ? '#f8fafc' : '#0f172a';
            colorBadge.style.color = repertoire === 'white' ? '#0f172a' : '#f8fafc';
            colorBadge.style.border = '1px solid #475569';
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
            if (existingSection) existingSection.style.display = tab === 'existing' ? 'block' : 'none';
            if (newSection) newSection.style.display = tab === 'new' ? 'block' : 'none';
            if (errorEl) errorEl.style.display = 'none';
            if (!confirmText && btnConfirm) {
                btnConfirm.textContent = (tab === 'new') ? 'Create Study' : 'Save to Study';
            }
            if (tab === 'new' && newNameInput) {
                setTimeout(() => {
                    try {
                        newNameInput.focus();
                        newNameInput.select();
                    } catch (_) {}
                }, 60);
            }
        };

        if (tabExisting) tabExisting.onclick = () => setTab('existing');
        if (tabNew) tabNew.onclick = () => setTab('new');

        if (selectDropdown) {
            selectDropdown.innerHTML = '<option value="">Loading studies...</option>';
        }

        let promptSettled = false;
        let isCleanedUp = false;
        const isLive = () => !isCleanedUp;

        fetch(`/api/repertoire/studies?repertoire=${encodeURIComponent(repertoire)}`)
            .then(res => (res && res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res ? res.status : 'error'}`))))
            .then(studies => {
                // The dialog may have been cancelled while this was in flight.
                if (!selectDropdown || !isLive()) return;
                selectDropdown.innerHTML = '<option value="">-- Choose Existing Study --</option>';
                if (Array.isArray(studies) && studies.length > 0) {
                    studies.forEach(s => {
                        const opt = document.createElement('option');
                        opt.value = s.name;
                        const countInfo = s.total_moves ? ` (${s.total_moves} moves)` : '';
                        opt.textContent = `${s.name}${countInfo}`;
                        selectDropdown.appendChild(opt);
                    });

                    // Pre-select if currently selected in builder or scout
                    const builderSelect = document.getElementById('builderStudySelect');
                    const scoutSelect = document.getElementById('scoutStudySelect');
                    const activeSelect = (builderSelect && builderSelect.value) ? builderSelect : ((scoutSelect && scoutSelect.value) ? scoutSelect : null);

                    let matchedExisting = false;
                    if (activeSelect && activeSelect.value) {
                        selectDropdown.value = activeSelect.value;
                        if (selectDropdown.value === activeSelect.value) {
                            matchedExisting = true;
                        }
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
                    if (newNameInput && defaultName) {
                        newNameInput.value = defaultName;
                    }
                    setTab('new');
                }
            })
            .catch(err => {
                console.error('Error fetching studies for modal:', err);
                if (!isLive()) return;
                if (selectDropdown) selectDropdown.innerHTML = '<option value="">-- No Studies Found --</option>';
                setTab('new');
            });

        if (newNameInput) {
            newNameInput.value = defaultName || '';
        }

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
                        errorEl.textContent = 'Please select an existing study, or switch to "+ New Study" to create one.';
                        errorEl.style.display = 'block';
                    }
                    return;
                }
            } else {
                chosenName = newNameInput ? newNameInput.value.trim() : '';
                if (!chosenName) {
                    if (errorEl) {
                        errorEl.textContent = 'Please enter a name for the study (mandatory).';
                        errorEl.style.display = 'block';
                    }
                    if (newNameInput) newNameInput.focus();
                    return;
                }
                // Pre-register study on server. `res.ok` is checked: previously a
                // 4xx (e.g. duplicate/invalid name) still resolved the promise
                // with the name, and the line was imported against a study that
                // did not exist.
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
                        let detail = `Server responded ${createRes.status}`;
                        try {
                            const body = await createRes.json();
                            if (body && body.detail) detail = body.detail;
                        } catch (_) {}
                        if (errorEl) {
                            errorEl.textContent = `Could not create study: ${detail}`;
                            errorEl.style.display = 'block';
                        }
                        if (btnConfirm) {
                            btnConfirm.disabled = false;
                            btnConfirm.textContent = confirmText || ((mode === 'new') ? 'Create Study' : 'Save to Study');
                        }
                        return;
                    }
                } catch (e) {
                    console.error('Error creating study:', e);
                    if (errorEl) {
                        errorEl.textContent = `Could not create study: ${e.message}`;
                        errorEl.style.display = 'block';
                    }
                    if (btnConfirm) {
                        btnConfirm.disabled = false;
                        btnConfirm.textContent = confirmText || ((mode === 'new') ? 'Create Study' : 'Save to Study');
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

        // Keyboard handler for Enter key in newNameInput
        if (newNameInput) {
            newNameInput.onkeydown = (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    handleConfirm();
                } else if (e.key === 'Escape') {
                    e.preventDefault();
                    handleCancel();
                }
            };
        }

        // Click outside backdrop to close
        modal.onclick = (e) => {
            const rect = modal.getBoundingClientRect();
            const isInDialog = (
                rect.top <= e.clientY &&
                e.clientY <= rect.top + rect.height &&
                rect.left <= e.clientX &&
                e.clientX <= rect.left + rect.width
            );
            if (!isInDialog) {
                handleCancel();
            }
        };

        if (btnCancel) btnCancel.onclick = handleCancel;
        if (btnClose) btnClose.onclick = handleCancel;
        if (btnConfirm) btnConfirm.onclick = handleConfirm;
        modal.onclose = () => { cleanup(); settleOnce(null); };

        const initialTab = (mode === 'new' || (mode === 'auto' && defaultName)) ? 'new' : 'existing';
        setTab(initialTab);
        try {
            modal.showModal();
        } catch (e) {
            console.error('Could not open the study selection dialog:', e);
            cleanup();
            settleOnce(null);
            return;
        }
        if (initialTab === 'new' && newNameInput) {
            setTimeout(() => {
                try {
                    newNameInput.focus();
                    newNameInput.select();
                } catch (_) {}
            }, 60);
        }
    });

    // Register the in-flight promise AFTER construction (the executor can settle
    // synchronously), then release the re-entrancy slot once it settles.
    studyPromptPending = pending;
    const release = () => { if (studyPromptPending === pending) studyPromptPending = null; };
    pending.then(release, release);
    return pending;
};

// Global Floating Toast Notification System (Zero Layout Shift)
window.showToast = function(msg, color = '#4ade80', duration = 3200) {
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

    // Dot indicator
    const dot = document.createElement('span');
    dot.className = 'app-toast-dot';
    dot.style.background = color;
    dot.style.boxShadow = `0 0 8px ${color}88`;

    // Text content
    const text = document.createElement('span');
    text.className = 'app-toast-text';
    text.textContent = msg;

    toast.appendChild(dot);
    toast.appendChild(text);
    container.appendChild(toast);

    // Trigger appear animation
    requestAnimationFrame(() => {
        toast.classList.add('visible');
    });

    // Auto remove
    setTimeout(() => {
        toast.classList.remove('visible');
        toast.classList.add('leaving');
        setTimeout(() => {
            if (toast.parentNode) toast.parentNode.removeChild(toast);
        }, 280);
    }, duration);
};
