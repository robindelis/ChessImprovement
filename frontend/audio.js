// Web Audio API Chess Sound Generator (Zero external dependencies)
//
// CODE-20 hardening: every voice is routed through ONE master bus (gain ->
// DynamicsCompressor "limiter" -> destination) instead of straight to
// `destination`, so overlapping voices can no longer sum past full scale; the
// AudioContext construction / resume are wrapped in try/catch and their promise
// rejections are swallowed, so a sound can never throw into the board's
// animation callback (board.js `_playMoveAudio`); and a voice is never
// scheduled while the context is still suspended (that used to queue every note
// at a frozen `currentTime` and fire them all in one burst on resume).
class ChessAudio {
    constructor() {
        this.ctx = null;
        this.masterGain = null;
        this.limiter = null;
        this.activeVoices = 0;
        this.maxVoices = 12;   // hard cap on concurrently scheduled voices
        this.unavailable = false;
    }

    /**
     * Creates (once) the context + master bus and makes sure the context is
     * actually running. Returns false when audio cannot be used right now, in
     * which case callers silently skip the sound.
     */
    _init() {
        if (this.unavailable) return false;
        try {
            if (!this.ctx) {
                const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
                if (!AudioContextCtor) {
                    this.unavailable = true;
                    return false;
                }
                this.ctx = new AudioContextCtor();

                // Master bus: every voice -> masterGain -> limiter -> destination
                this.masterGain = this.ctx.createGain();
                this.masterGain.gain.value = 0.8;

                let tail = this.masterGain;
                if (typeof this.ctx.createDynamicsCompressor === 'function') {
                    this.limiter = this.ctx.createDynamicsCompressor();
                    this.limiter.threshold.value = -6;
                    this.limiter.knee.value = 0;
                    this.limiter.ratio.value = 20;
                    this.limiter.attack.value = 0.003;
                    this.limiter.release.value = 0.25;
                    this.masterGain.connect(this.limiter);
                    tail = this.limiter;
                }
                tail.connect(this.ctx.destination);
            }

            if (this.ctx.state === 'suspended') {
                // `resume()` returns a promise that rejects when the browser
                // refuses (no user activation yet). Swallow it and skip the
                // sound rather than queueing it at a frozen currentTime.
                try {
                    const p = this.ctx.resume();
                    if (p && typeof p.catch === 'function') p.catch(() => {});
                } catch (_) { /* ignore - sound is dropped below */ }
            }
            return this.ctx.state === 'running';
        } catch (e) {
            this.unavailable = true;
            return false;
        }
    }

    /** Schedules one short voice on the master bus. Returns false if skipped. */
    _voice({ type = 'sine', from, to, start, duration, peak, glideTo = 0.01 }) {
        if (!this._init()) return false;
        if (this.activeVoices >= this.maxVoices) return false;
        try {
            const now = this.ctx.currentTime;
            const t0 = Math.max(now, start || now);
            const osc = this.ctx.createOscillator();
            const gain = this.ctx.createGain();

            osc.type = type;
            osc.frequency.setValueAtTime(from, t0);
            if (to && to !== from) {
                osc.frequency.exponentialRampToValueAtTime(to, t0 + duration);
            }
            gain.gain.setValueAtTime(peak, t0);
            gain.gain.exponentialRampToValueAtTime(glideTo, t0 + duration);

            osc.connect(gain);
            gain.connect(this.masterGain);

            this.activeVoices++;
            const release = () => {
                this.activeVoices = Math.max(0, this.activeVoices - 1);
                try { gain.disconnect(); osc.disconnect(); } catch (_) {}
            };
            osc.onended = release;

            osc.start(t0);
            osc.stop(t0 + duration);
            return true;
        } catch (_) {
            return false;
        }
    }

    playMove() {
        this._voice({ type: 'triangle', from: 140, to: 80, duration: 0.08, peak: 0.3 });
    }

    playCapture() {
        this._voice({ type: 'sine', from: 220, to: 110, duration: 0.12, peak: 0.4 });
    }

    playCheck() {
        if (!this._init()) return;
        const base = this.ctx.currentTime;
        this._voice({ type: 'sine', from: 440, to: 440, start: base, duration: 0.15, peak: 0.2 });
        this._voice({ type: 'sine', from: 554, to: 554, start: base + 0.05, duration: 0.15, peak: 0.2 });
    }

    playSuccess() {
        if (!this._init()) return;
        const base = this.ctx.currentTime;
        // C5, E5, G5, C6
        [523.25, 659.25, 783.99, 1046.50].forEach((freq, idx) => {
            this._voice({ type: 'sine', from: freq, to: freq, start: base + idx * 0.07, duration: 0.25, peak: 0.25, glideTo: 0.001 });
        });
    }

    playBlunder() {
        this._voice({ type: 'sawtooth', from: 130, to: 75, duration: 0.25, peak: 0.25 });
    }

    /** Neutral, deliberately NOT triumphant: used for destructive actions. */
    playRemove() {
        this._voice({ type: 'triangle', from: 300, to: 120, duration: 0.16, peak: 0.22 });
    }
}

window.chessAudio = new ChessAudio();
