// Interactive Chessboard Component

//: Paint used for the transient overlay channel (the engine's principal variation).
//: The existing "alt-drag" arrow colour, so an engine line can never be confused
//: with a user arrow of the same shape, and `setOverlayArrows` never has to
//: trust a colour that came from the network.
const OVERLAY_ARROW_COLOR = '#38bdf8';

class InteractiveChessboard {
    constructor(containerEl, options = {}) {
        this.container = containerEl;
        this.options = Object.assign({
            orientation: 'white',
            interactive: true,
            pieceTheme: '/assets/pieces/{piece}.svg',
            onMove: null, // callback (source, target, promo) => bool
            onArrowsChange: null,    // callback (arrows) => void -- the *persisted* arrows
            onMarkedSquaresChange: null, // callback (squares) => void
            onAnnotationsCleared: null    // callback () => void
        }, options);

        this.chess = new Chess();
        this.selectedSquare = null;
        this.legalMovesFromSelected = [];
        this.lastMove = null;
        this.arrows = [];        // [{ from, to, color }] -- user arrows, persisted per move
        this.overlayArrows = []; // [{ from, to, color }] -- transient (engine line); never persisted
        this.markedSquares = []; // ['e4', ...] -- user square marks from a right-click
        this.activeAnimations = [];
        this._dragState = null;
        this._dragFloatingEl = null;
        this._rightClickStartSquare = null;
        this._destroyed = false;

        // CODE3-15: SVG marker ids are resolved document-wide, so they must be unique per board
        // instance, not just unique per arrow index inside one board.
        InteractiveChessboard._instanceCounter = (InteractiveChessboard._instanceCounter || 0) + 1;
        this._markerPrefix = `ab${InteractiveChessboard._instanceCounter}`;

        this._boundPointerMove = (e) => this._onPointerMove(e);
        this._boundPointerUp = (e) => this._onPointerUp(e);
        window.addEventListener('pointermove', this._boundPointerMove);
        window.addEventListener('pointerup', this._boundPointerUp);

        this._initDOM();
    }

    /**
     * Detaches the two global `window` pointer listeners this instance registered and releases
     * the DOM/animation state it holds. Without this every board ever constructed is retained
     * by `window` for the whole session (CODE3-16).
     */
    destroy() {
        if (this._destroyed) return;
        this._destroyed = true;
        if (this._resizeCleanup) {
            this._resizeCleanup();
            this._resizeCleanup = null;
        }
        window.removeEventListener('pointermove', this._boundPointerMove);
        window.removeEventListener('pointerup', this._boundPointerUp);
        this.stopAnimations();
        this._cleanupDragState();
        this.arrows = [];
        this.overlayArrows = [];
        this.markedSquares = [];
        this.squares = {};
    }

    _initDOM() {
        this.container.innerHTML = '';
        this.container.classList.add('chess-board-wrapper');

        // Main board container
        this.boardEl = document.createElement('div');
        this.boardEl.classList.add('chess-board');

        // SVG overlay for arrows
        this.svgEl = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        this.svgEl.classList.add('board-svg-overlay');
        this.boardEl.appendChild(this.svgEl);

        this.squares = {};

        const ranks = this.options.orientation === 'white' ? [8,7,6,5,4,3,2,1] : [1,2,3,4,5,6,7,8];
        const files = this.options.orientation === 'white' ? ['a','b','c','d','e','f','g','h'] : ['h','g','f','e','d','c','b','a'];

        ranks.forEach(rank => {
            files.forEach(file => {
                const sq = `${file}${rank}`;
                // Standard chess board: a1 is a dark (black) square
                const isLight = (file.charCodeAt(0) - 97 + rank) % 2 === 0;

                const sqEl = document.createElement('div');
                sqEl.classList.add('square', isLight ? 'light' : 'dark');
                sqEl.dataset.square = sq;

                this.boardEl.appendChild(sqEl);
                this.squares[sq] = sqEl;
            });
        });

        this.boardEl.addEventListener('contextmenu', (e) => e.preventDefault());
        this.boardEl.addEventListener('pointerdown', (e) => this._onPointerDown(e));

        this.container.appendChild(this.boardEl);
        if (this.options.resizable !== false) {
            this._initResizeHandle();
        }
    }

    _initResizeHandle() {
        if (typeof document === 'undefined') return;
        const handle = document.createElement('div');
        handle.className = 'board-resize-handle';
        handle.setAttribute('title', 'Drag to resize chessboard');

        let isResizing = false;
        let startX = 0;
        let startWidth = 0;

        const onPointerMove = (ev) => {
            if (!isResizing) return;
            ev.preventDefault();
            const deltaX = ev.clientX - startX;
            const newSize = Math.round(Math.max(380, Math.min(1400, startWidth + deltaX)));
            InteractiveChessboard.setGlobalBoardSize(newSize, false);
        };

        const onPointerUp = (ev) => {
            if (!isResizing) return;
            isResizing = false;
            handle.classList.remove('active');
            if (typeof document !== 'undefined' && document.body && document.body.style) {
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
            }
            if (typeof window !== 'undefined') {
                window.removeEventListener('pointermove', onPointerMove);
                window.removeEventListener('pointerup', onPointerUp);
            }
            const finalSize = InteractiveChessboard.getGlobalBoardSize();
            InteractiveChessboard.setGlobalBoardSize(finalSize, true);
        };

        const onPointerDown = (e) => {
            if (e.button !== 0) return;
            e.preventDefault();
            e.stopPropagation();

            isResizing = true;
            startX = e.clientX;
            const rect = (this.container && typeof this.container.getBoundingClientRect === 'function')
                ? this.container.getBoundingClientRect()
                : { width: 600 };
            startWidth = rect.width || 600;

            handle.classList.add('active');
            if (typeof document !== 'undefined' && document.body && document.body.style) {
                document.body.style.cursor = 'nwse-resize';
                document.body.style.userSelect = 'none';
            }

            if (typeof window !== 'undefined') {
                window.addEventListener('pointermove', onPointerMove);
                window.addEventListener('pointerup', onPointerUp);
            }
        };

        handle.addEventListener('pointerdown', onPointerDown);
        this.container.appendChild(handle);
        this._resizeCleanup = () => {
            handle.removeEventListener('pointerdown', onPointerDown);
            if (typeof window !== 'undefined') {
                window.removeEventListener('pointermove', onPointerMove);
                window.removeEventListener('pointerup', onPointerUp);
            }
            if (typeof document !== 'undefined' && document.body && document.body.style) {
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
            }
        };
    }

    setOrientation(color) {
        // The square map is about to be replaced, so any in-flight animation would be animating
        // detached elements (and `loadFEN` later would clobber them). Cancel first.
        this.stopAnimations();
        this._cleanupDragState();
        if (this._resizeCleanup) {
            this._resizeCleanup();
            this._resizeCleanup = null;
        }
        this.options.orientation = color;
        this._initDOM();
        this.render();
    }

    stopAnimations() {
        if (this.activeAnimations && this.activeAnimations.length) {
            const list = [...this.activeAnimations];
            this.activeAnimations = [];
            list.forEach(anim => {
                try { anim.cancel(); } catch (e) {}
            });
        }
    }

    _cleanupDragState() {
        document.querySelectorAll('.square.drag-hover').forEach(el => el.classList.remove('drag-hover'));
        if (this._dragFloatingEl) {
            this._dragFloatingEl.remove();
            this._dragFloatingEl = null;
        }
        if (this._dragState && this._dragState.pieceImg) {
            this._dragState.pieceImg.style.opacity = '1';
        }
        this._dragState = null;
        this._rightClickStartSquare = null;
    }

    /**
     * Loads a position into the model. By default this is a full "reset the board to this
     * position" operation, which also drops the per-move UI state (selection, arrows, last-move
     * highlight). Callers that have just restored per-move state (e.g. repertoire arrows) must
     * pass `{ keepArrows: true }` so the state they restored is not wiped.
     *
     * `overlayArrows` is position-specific (it is the engine's line for *this* position), so it
     * is always dropped; `markedSquares` are the user's own annotations and deliberately
     * survive navigation until they are cleared explicitly, exactly like the shapes on
     * lichess.com and chess.com.
     */
    loadFEN(fen, options = {}) {
        const keepArrows = options && options.keepArrows === true;
        this.stopAnimations();
        if (!fen) return;
        let validFen = String(fen).trim();
        const parts = validFen.split(/\s+/);
        if (parts.length === 4) {
            validFen += ' 0 1';
        } else if (parts.length === 5) {
            validFen += ' 1';
        }
        const success = this.chess.load(validFen);
        if (!success) {
            console.warn('Failed to load FEN into chess.js:', fen, 'normalized:', validFen);
        }
        this.selectedSquare = null;
        this.legalMovesFromSelected = [];
        if (!keepArrows) this.arrows = [];
        this.overlayArrows = [];
        this.lastMove = null;
        this.render();
    }

    setLastMove(from, to) {
        if (from && to) {
            this.lastMove = { from, to };
        } else {
            this.lastMove = null;
        }
        this.render();
    }

    getFEN() {
        return this.chess.fen();
    }

    /**
     * Boundary check for arrow data. `arrows` arrives from `JSON.parse()` of a persisted
     * `repertoire_moves.arrows` column and from other components, so a `null` entry or a
     * typo'd square name must not be allowed to throw from inside `render()`.
     */
    _isValidArrow(a) {
        if (!a || typeof a !== 'object') return false;
        return this._isValidSquare(a.from) && this._isValidSquare(a.to);
    }

    _isValidSquare(sq) {
        if (typeof sq !== 'string' || sq.length !== 2) return false;
        const f = sq.charCodeAt(0);
        if (f < 97 || f > 104) return false;      // a..h
        const r = sq.charCodeAt(1);
        if (r < 49 || r > 56) return false;       // 1..8
        return true;
    }

    /**
     * The `color` of an arrow arrives from `JSON.parse()` of the persisted
     * `repertoire_moves.arrows` column and is then written into two SVG presentation attributes
     * (`fill`, `stroke`) and re-serialised back to the database by `onArrowsChange`. Only the
     * paint values this component itself produces are accepted: `#rgb`, `#rgba`, `#rrggbb`,
     * `#rrggbbaa`, `rgb(...)` and `rgba(...)`. Anything else falls back to the default green, so a
     * hostile colour can neither repaint the board nor be persisted.
     */
    _sanitizeArrowColor(color, fallback = '#22c55e') {
        if (typeof color !== 'string') return fallback;
        const c = color.trim();
        if (/^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(c)) return c;
        if (/^rgba?\(\s*[\d.\s,%+-]+\)$/i.test(c)) return c;
        return fallback;
    }

    addArrow(from, to, color = '#22c55e') {
        if (!this._isValidArrow({ from, to })) {
            console.warn('Ignoring malformed arrow:', from, '->', to);
            return false;
        }
        this.arrows.push({ from, to, color: this._sanitizeArrowColor(color) });
        this._renderArrows();
        return true;
    }

    clearArrows() {
        this.arrows = [];
        this._renderArrows();
    }

    getArrows() {
        return [...this.arrows];
    }

    setArrows(arrows) {
        this.arrows = Array.isArray(arrows)
            ? arrows.filter(a => this._isValidArrow(a)).map(a => ({
                from: a.from,
                to: a.to,
                color: this._sanitizeArrowColor(a.color)
            }))
            : [];
        this._renderArrows();
        return this.arrows.length;
    }

    // ------------------------------------------------------------------ #
    // Overlay arrows -- a second, TRANSIENT arrow channel
    // ------------------------------------------------------------------ #
    // `this.arrows` is the user's annotation: it is POSTed to
    // `repertoire_moves.arrows`, restored per move by the Repertoire Builder's
    // `renderMoveToolsPanel()`, and therefore owned by that host.  The engine
    // line is not: it is re-derived on every SSE frame of every position, and it
    // must not be able to overwrite a saved annotation.  Keeping it in its own
    // array means `getArrows()`/`onArrowsChange` keep meaning exactly what they
    // mean today, and `clearArrows()` cannot silently wipe the engine line.
    setOverlayArrows(arrows) {
        this.overlayArrows = Array.isArray(arrows)
            ? arrows.filter(a => this._isValidArrow(a)).map(a => ({
                from: a.from,
                to: a.to,
                color: this._sanitizeArrowColor(a.color, OVERLAY_ARROW_COLOR)
            }))
            : [];
        this._renderArrows();
        return this.overlayArrows.length;
    }

    getOverlayArrows() {
        return [...this.overlayArrows];
    }

    clearOverlayArrows() {
        this.overlayArrows = [];
        this._renderArrows();
    }

    // ------------------------------------------------------------------ #
    // Square marks (right-click) and clear-all
    // ------------------------------------------------------------------ #
    getMarkedSquares() {
        return [...this.markedSquares];
    }

    /**
     * Reconciles the `marked` class on all 64 squares with `this.markedSquares`.
     * Total in both directions, so it is correct after `render()` (which wipes
     * every square) *and* after a single toggle, and there is exactly one place
     * that decides what a mark looks like.
     */
    _applyMarks() {
        if (!this.squares) return;
        const wanted = new Set(this.markedSquares);
        for (const sq in this.squares) {
            const el = this.squares[sq];
            if (!el || !el.classList) continue;
            if (wanted.has(sq)) el.classList.add('marked');
            else el.classList.remove('marked');
        }
    }

    setMarkedSquares(squares) {
        const seen = new Set();
        this.markedSquares = (Array.isArray(squares) ? squares : [])
            .filter(sq => {
                if (!this._isValidSquare(sq) || seen.has(sq)) return false;
                seen.add(sq);
                return true;
            });
        this._applyMarks();
        return this.markedSquares.length;
    }

    /** @returns {boolean} true when the square ended up marked. */
    toggleMarkedSquare(sq) {
        if (!this._isValidSquare(sq)) return false;
        const idx = this.markedSquares.indexOf(sq);
        if (idx !== -1) this.markedSquares.splice(idx, 1);
        else this.markedSquares.push(sq);
        this._applyMarks();
        if (typeof this.options.onMarkedSquaresChange === 'function') {
            this.options.onMarkedSquaresChange(this.getMarkedSquares());
        }
        return idx === -1;
    }

    clearMarkedSquares() {
        this.markedSquares = [];
        this._applyMarks();
    }

    /**
     * Drops every line and every highlighted square at once -- the "click an
     * empty square" gesture.
     *
     * The persisted arrows go too, but only through `onArrowsChange([])`, i.e.
     * exactly the same channel the right-click gesture writes through.  Clearing
     * them behind the host's back would leave `#repArrowsCount` and the
     * `repertoire_moves.arrows` column claiming arrows that are no longer on
     * screen; routing the empty list through the callback is what keeps the
     * panel and the database truthful.
     */
    clearAnnotations() {
        const hadArrows = this.arrows.length > 0;
        const hadAnything = hadArrows || this.overlayArrows.length > 0 || this.markedSquares.length > 0;
        this.arrows = [];
        this.overlayArrows = [];
        this.markedSquares = [];
        this._applyMarks();
        this._renderArrows();
        if (hadAnything) {
            if (hadArrows && typeof this.options.onArrowsChange === 'function') {
                // The same channel, the same call: a delete and a draw are the same
                // "this is the arrow list now" fact, and the host decides what (if
                // anything) to tell the user about it.
                this.options.onArrowsChange([]);
            }
            if (typeof this.options.onAnnotationsCleared === 'function') {
                this.options.onAnnotationsCleared();
            }
        }
        return hadAnything;
    }

    render() {
        // Render pieces
        const board = this.chess.board();
        for (let r = 0; r < 8; r++) {
            for (let f = 0; f < 8; f++) {
                const file = String.fromCharCode(97 + f);
                const rank = 8 - r;
                const sq = `${file}${rank}`;
                const sqEl = this.squares[sq];
                if (!sqEl) continue;

                sqEl.innerHTML = '';
                sqEl.classList.remove('selected', 'highlight-last', 'check');

                if (this.lastMove && (this.lastMove.from === sq || this.lastMove.to === sq)) {
                    sqEl.classList.add('highlight-last');
                }

                if (this.selectedSquare === sq) {
                    sqEl.classList.add('selected');
                }

                const piece = board[r][f];
                if (piece) {
                    const pCode = (piece.color === 'w' ? 'w' : 'b') + piece.type.toUpperCase();
                    const img = document.createElement('img');
                    img.src = this.options.pieceTheme.replace('{piece}', pCode);
                    img.classList.add('piece');
                    img.draggable = false;
                    img.addEventListener('dragstart', (e) => e.preventDefault());
                    sqEl.appendChild(img);

                    if (piece.type === 'k' && this.chess.in_check() && piece.color === this.chess.turn()) {
                        sqEl.classList.add('check');
                    }
                }
            }
        }

        // Render move destination hints
        this.legalMovesFromSelected.forEach(m => {
            const sqEl = this.squares[m.to];
            if (sqEl) {
                const hint = document.createElement('div');
                hint.classList.add(sqEl.querySelector('img') ? 'capture-hint' : 'move-hint');
                sqEl.appendChild(hint);
            }
        });

        // `render()` wipes every square, so the user's right-click marks have to be
        // re-applied here; `_applyMarks` is also what removes a mark that is no
        // longer wanted, so it is the single source of truth for the class.
        this._applyMarks();

        this._renderArrows();
    }

    /**
     * Resolves the board square a pointer event was released over.
     *
     * `document.elementFromPoint` is the accurate answer, but it returns `null`
     * whenever the pointer is outside the viewport and it simply does not exist
     * in a non-browser host, so the event's own target is used as the fallback.
     * The fallback is not a test-only convenience: a release over a piece `<img>`
     * is resolved by `closest('.square')` either way, and the two agree in every
     * case where `elementFromPoint` succeeds.
     */
    _squareAtPoint(e) {
        let el = null;
        if (e && typeof document !== 'undefined' && typeof document.elementFromPoint === 'function') {
            try {
                el = document.elementFromPoint(e.clientX, e.clientY);
            } catch (err) {
                el = null;
            }
        }
        if (!el && e && e.target) el = e.target;
        if (!el || typeof el.closest !== 'function') return null;
        const sqEl = el.closest('.square');
        return (sqEl && sqEl.dataset && sqEl.dataset.square) ? sqEl.dataset.square : null;
    }

    _onPointerDown(e) {
        if (!this.options.interactive) return;

        // Right-click handles arrow drawing
        if (e.button === 2) {
            const sqEl = e.target.closest('.square');
            this._rightClickStartSquare = sqEl ? sqEl.dataset.square : null;
            return;
        }

        if (e.button !== 0) return; // Only primary button for piece moves

        const sqEl = e.target.closest('.square');
        if (!sqEl) return;
        const sq = sqEl.dataset.square;

        // Check if clicking a legal move destination for currently selected piece
        if (this.selectedSquare) {
            const move = this.legalMovesFromSelected.find(m => m.to === sq);
            if (move) {
                const promo = move.promotion || (move.piece === 'p' && (sq[1] === '8' || sq[1] === '1') ? 'q' : undefined);
                this._executeMove(move.from, move.to, promo, true);
                this._dragState = null;
                return;
            }
        }

        // Check if clicking/pressing on a piece of current turn
        const piece = this.chess.get(sq);
        if (piece && piece.color === this.chess.turn()) {
            this.selectedSquare = sq;
            this.legalMovesFromSelected = this.chess.moves({ square: sq, verbose: true });
            this.render();

            const pieceImg = this.squares[sq]?.querySelector('img.piece');
            this._dragState = {
                fromSq: sq,
                pieceImg: pieceImg,
                startX: e.clientX,
                startY: e.clientY,
                isDragging: false,
                pieceCode: (piece.color === 'w' ? 'w' : 'b') + piece.type.toUpperCase()
            };
        } else {
            this.selectedSquare = null;
            this.legalMovesFromSelected = [];
            // An empty square that is not a legal destination is the "dismiss"
            // gesture (lichess.com / chess.com): every line and every highlighted
            // square goes.  It is scoped to a genuinely empty square so that
            // clicking one of the opponent's pieces keeps its old meaning
            // (deselect, and nothing else).
            if (!piece) {
                this.clearAnnotations();
            }
            this.render();
            this._dragState = null;
        }
    }

    _onPointerMove(e) {
        if (!this._dragState) return;

        const dist = Math.hypot(e.clientX - this._dragState.startX, e.clientY - this._dragState.startY);
        if (!this._dragState.isDragging && dist > 3) {
            this._dragState.isDragging = true;
            if (this._dragState.pieceImg) {
                this._dragState.pieceImg.style.opacity = '0.2';
            }
            const rect = this.squares[this._dragState.fromSq]?.getBoundingClientRect();
            const w = rect ? rect.width * 0.9 : 45;
            const h = rect ? rect.height * 0.9 : 45;

            const floating = document.createElement('img');
            floating.src = this.options.pieceTheme.replace('{piece}', this._dragState.pieceCode);
            floating.classList.add('floating-drag-piece');
            floating.style.width = `${w}px`;
            floating.style.height = `${h}px`;
            floating.style.left = `${e.clientX}px`;
            floating.style.top = `${e.clientY}px`;
            document.body.appendChild(floating);
            this._dragFloatingEl = floating;
        }

        if (this._dragState.isDragging && this._dragFloatingEl) {
            this._dragFloatingEl.style.left = `${e.clientX}px`;
            this._dragFloatingEl.style.top = `${e.clientY}px`;

            const targetEl = document.elementFromPoint?.(e.clientX, e.clientY)?.closest?.('.square');
            document.querySelectorAll('.square.drag-hover').forEach(el => el.classList.remove('drag-hover'));
            if (targetEl && targetEl.dataset.square !== this._dragState.fromSq) {
                targetEl.classList.add('drag-hover');
            }
        }
    }

    _onPointerUp(e) {
        // Handle right-click arrow drawing completion.
        // NOTE: `_rightClickStartSquare` must be cleared on *every* exit path, including a
        // release outside the board, otherwise the next right-click is anchored to a stale square.
        if (e.button === 2) {
            const startSquare = this._rightClickStartSquare;
            this._rightClickStartSquare = null;
            if (startSquare) {
                const targetSq = this._squareAtPoint(e);
                if (targetSq === startSquare) {
                    // A right-click that does not travel anywhere is a *mark*, not an
                    // arrow: there is no second square to point at.  This is the
                    // lichess.com / chess.com behaviour, and it is why the two
                    // gestures stay unambiguous: travel == arrow, no travel == mark.
                    this.toggleMarkedSquare(startSquare);
                } else if (targetSq) {
                    const existingIdx = this.arrows.findIndex(a => a.from === startSquare && a.to === targetSq);
                    if (existingIdx !== -1) {
                        this.arrows.splice(existingIdx, 1);
                    } else {
                        let arrowColor = '#22c55e';
                        if (e.shiftKey) arrowColor = '#ef4444';
                        else if (e.altKey) arrowColor = '#38bdf8';
                        this.arrows.push({ from: startSquare, to: targetSq, color: arrowColor });
                    }
                    this._renderArrows();
                    if (typeof this.options.onArrowsChange === 'function') {
                        this.options.onArrowsChange(this.getArrows());
                    }
                }
                // A right-release can also end a left-button drag that is still in flight.
                this._cleanupDragState();
                return;
            }
        }

        // Clean up drag hover highlights & floating clone
        document.querySelectorAll('.square.drag-hover').forEach(el => el.classList.remove('drag-hover'));

        if (this._dragFloatingEl) {
            this._dragFloatingEl.remove();
            this._dragFloatingEl = null;
        }

        if (this._dragState) {
            if (this._dragState.pieceImg) {
                this._dragState.pieceImg.style.opacity = '1';
            }

            if (this._dragState.isDragging) {
                const toSq = this._squareAtPoint(e);

                if (toSq && toSq !== this._dragState.fromSq) {
                    const move = this.legalMovesFromSelected.find(m => m.from === this._dragState.fromSq && m.to === toSq);
                    if (move) {
                        const promo = move.promotion || (move.piece === 'p' && (toSq[1] === '8' || toSq[1] === '1') ? 'q' : undefined);
                        this._executeMove(move.from, move.to, promo, false);
                    } else {
                        this.selectedSquare = null;
                        this.legalMovesFromSelected = [];
                        this.render();
                    }
                } else {
                    // Dropped back onto origin (or released off-board): keep it selected, but make
                    // sure the ghost is gone and the piece is fully opaque again.
                    this._dragFloatingEl = null;
                    this.render();
                }
            }
            this._dragState = null;
        }
    }

    animateMove(from, to, onComplete = null) {
        const fromEl = this.squares[from];
        const toEl = this.squares[to];
        if (!fromEl || !toEl) {
            if (onComplete) onComplete();
            return;
        }

        const pieceImg = fromEl.querySelector('img.piece');
        if (!pieceImg) {
            if (onComplete) onComplete();
            return;
        }

        const fromRect = fromEl.getBoundingClientRect();
        const toRect = toEl.getBoundingClientRect();
        const dx = toRect.left - fromRect.left;
        const dy = toRect.top - fromRect.top;

        // Castling rook animation check
        if (from === 'e1' && to === 'g1') this._animateSecondaryPiece('h1', 'f1');
        else if (from === 'e1' && to === 'c1') this._animateSecondaryPiece('a1', 'd1');
        else if (from === 'e8' && to === 'g8') this._animateSecondaryPiece('h8', 'f8');
        else if (from === 'e8' && to === 'c8') this._animateSecondaryPiece('a8', 'd8');

        // Captured piece fade out
        const capturedImg = toEl.querySelector('img.piece');
        if (capturedImg) {
            try {
                const capAnim = capturedImg.animate([
                    { opacity: 1, transform: 'scale(1)' },
                    { opacity: 0, transform: 'scale(0.6)' }
                ], {
                    duration: 160,
                    easing: 'ease-out',
                    fill: 'forwards'
                });
                this.activeAnimations.push(capAnim);
            } catch (e) {}
        }

        pieceImg.style.zIndex = '50';
        pieceImg.style.pointerEvents = 'none';

        try {
            const anim = pieceImg.animate([
                { transform: 'translate(0px, 0px)' },
                { transform: `translate(${dx}px, ${dy}px)` }
            ], {
                duration: 180,
                easing: 'cubic-bezier(0.25, 1, 0.5, 1)',
                fill: 'forwards'
            });
            this.activeAnimations.push(anim);

            // The completion callback is a *contract*, not a nice-to-have: callers use it to
            // re-synchronise the board. A cancelled animation must therefore still deliver it,
            // otherwise a navigation that lands mid-animation leaves the DOM on a stale position
            // forever. `settled` guarantees exactly-once delivery across finish/cancel/throw.
            let settled = false;
            const finishOnce = () => {
                if (settled) return;
                settled = true;
                const idx = this.activeAnimations.indexOf(anim);
                if (idx !== -1) this.activeAnimations.splice(idx, 1);
                if (onComplete) onComplete();
            };

            anim.onfinish = () => {
                pieceImg.style.zIndex = '';
                pieceImg.style.pointerEvents = '';
                finishOnce();
            };
            anim.oncancel = () => {
                pieceImg.style.zIndex = '';
                pieceImg.style.pointerEvents = '';
                finishOnce();
            };
        } catch (e) {
            pieceImg.style.zIndex = '';
            pieceImg.style.pointerEvents = '';
            if (onComplete) onComplete();
        }
    }

    /**
     * Purely decorative "a piece slid from `from` to `to`" animation that mutates **no** state.
     * Used when the caller has already synchronised the model/FEN (and per-move UI such as
     * arrows) and only wants the motion: unlike `animateMove()`, cancelling it can never leave
     * the board desynchronised, and it never clobbers restored arrows.
     */
    animateGhostMove(from, to, duration = 180) {
        const fromEl = this.squares[from];
        const toEl = this.squares[to];
        if (!fromEl || !toEl) return;

        const piece = this.chess.get(to);
        if (!piece) return;
        const code = (piece.color === 'w' ? 'w' : 'b') + piece.type.toUpperCase();

        const ghost = document.createElement('img');
        ghost.src = this.options.pieceTheme.replace('{piece}', code);
        ghost.classList.add('piece');
        ghost.style.position = 'absolute';
        ghost.style.pointerEvents = 'none';
        ghost.style.zIndex = '50';
        ghost.style.width = '100%';
        ghost.style.height = '100%';
        fromEl.appendChild(ghost);

        const fromRect = fromEl.getBoundingClientRect();
        const toRect = toEl.getBoundingClientRect();
        const dx = toRect.left - fromRect.left;
        const dy = toRect.top - fromRect.top;

        let anim = null;
        const cleanup = () => { try { ghost.remove(); } catch (e) {} };
        try {
            anim = ghost.animate([
                { transform: 'translate(0px, 0px)' },
                { transform: `translate(${dx}px, ${dy}px)` }
            ], {
                duration: duration,
                easing: 'cubic-bezier(0.25, 1, 0.5, 1)',
                fill: 'forwards'
            });
            this.activeAnimations.push(anim);
            let settled = false;
            const done = () => {
                if (settled) return;
                settled = true;
                cleanup();
                const idx = this.activeAnimations.indexOf(anim);
                if (idx !== -1) this.activeAnimations.splice(idx, 1);
            };
            anim.onfinish = done;
            anim.oncancel = done;
        } catch (e) {
            cleanup();
        }
    }

    _animateSecondaryPiece(from, to) {
        const fromEl = this.squares[from];
        const toEl = this.squares[to];
        if (!fromEl || !toEl) return;
        const pieceImg = fromEl.querySelector('img.piece');
        if (!pieceImg) return;

        const fromRect = fromEl.getBoundingClientRect();
        const toRect = toEl.getBoundingClientRect();
        const dx = toRect.left - fromRect.left;
        const dy = toRect.top - fromRect.top;

        pieceImg.style.zIndex = '49';
        try {
            const anim = pieceImg.animate([
                { transform: 'translate(0px, 0px)' },
                { transform: `translate(${dx}px, ${dy}px)` }
            ], {
                duration: 180,
                easing: 'cubic-bezier(0.25, 1, 0.5, 1)',
                fill: 'forwards'
            });
            this.activeAnimations.push(anim);
            anim.onfinish = () => {
                pieceImg.style.zIndex = '';
                const idx = this.activeAnimations.indexOf(anim);
                if (idx !== -1) this.activeAnimations.splice(idx, 1);
            };
            anim.oncancel = () => {
                pieceImg.style.zIndex = '';
                const idx = this.activeAnimations.indexOf(anim);
                if (idx !== -1) this.activeAnimations.splice(idx, 1);
            };
        } catch (e) {
            pieceImg.style.zIndex = '';
        }
    }

    _playMoveAudio(isCapture) {
        if (!window.chessAudio) return;
        if (this.chess.in_check()) {
            window.chessAudio.playCheck();
        } else if (isCapture) {
            window.chessAudio.playCapture();
        } else {
            window.chessAudio.playMove();
        }
    }

    _executeMove(from, to, promotion = undefined, animated = true) {
        const isCapture = Boolean(this.chess.get(to));
        const moveObj = { from, to };
        if (promotion) moveObj.promotion = promotion;

        const moveRes = this.chess.move(moveObj);
        if (!moveRes) return;

        this.selectedSquare = null;
        this.legalMovesFromSelected = [];
        this.lastMove = { from, to };

        if (animated) {
            this.stopAnimations();
            this.animateMove(from, to, () => {
                this._playMoveAudio(isCapture);
                this.render();
                if (this.options.onMove) {
                    this.options.onMove(from, to, promotion, moveRes);
                }
            });
        } else {
            this.stopAnimations();
            this._playMoveAudio(isCapture);
            this.render();
            if (this.options.onMove) {
                this.options.onMove(from, to, promotion, moveRes);
            }
        }
    }

    playMove(from, to, promotion = undefined, animated = true, onComplete = null) {
        const isCapture = Boolean(this.chess.get(to));
        const moveObj = { from, to };
        if (promotion) moveObj.promotion = promotion;

        const moveRes = this.chess.move(moveObj);
        if (!moveRes) return null;

        this.selectedSquare = null;
        this.legalMovesFromSelected = [];
        this.lastMove = { from, to };

        if (animated) {
            this.stopAnimations();
            this.animateMove(from, to, () => {
                this._playMoveAudio(isCapture);
                this.render();
                if (onComplete) onComplete();
            });
        } else {
            this.stopAnimations();
            this._playMoveAudio(isCapture);
            this.render();
            if (onComplete) onComplete();
        }
        return moveRes;
    }

    _renderArrows() {
        this.svgEl.innerHTML = '';
        if (!this.arrows.length && !this.overlayArrows.length) return;

        // Render arrow definitions
        const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
        this.svgEl.appendChild(defs);

        // The persisted channel is drawn first and the overlay on top of it, and the
        // marker index runs across both so an id can never be issued twice (the ids
        // are resolved document-wide -- see `_markerPrefix`).
        const draw = (arr, i, isOverlay) => {
            const markerId = `${this._markerPrefix}-arrow-marker-${i}`;
            const marker = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
            marker.setAttribute('id', markerId);
            marker.setAttribute('viewBox', '0 0 10 10');
            marker.setAttribute('refX', '5');
            marker.setAttribute('refY', '5');
            marker.setAttribute('markerWidth', '6');
            marker.setAttribute('markerHeight', '6');
            marker.setAttribute('orient', 'auto-start-reverse');

            const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            path.setAttribute('d', 'M 0 1 L 10 5 L 0 9 z');
            path.setAttribute('fill', arr.color);
            marker.appendChild(path);
            defs.appendChild(marker);

            const p1 = this._getSquareCenterPercent(arr.from);
            const p2 = this._getSquareCenterPercent(arr.to);

            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', `${p1.x}%`);
            line.setAttribute('y1', `${p1.y}%`);
            line.setAttribute('x2', `${p2.x}%`);
            line.setAttribute('y2', `${p2.y}%`);
            line.setAttribute('stroke', arr.color);
            // The engine line is deliberately thinner, dashed and more transparent
            // than a user arrow: it is a suggestion that is still changing as the
            // search deepens, and it must stay visually distinct from the user's own
            // saved annotation even where the two overlap.
            line.setAttribute('stroke-width', isOverlay ? '6' : '10');
            line.setAttribute('stroke-linecap', 'round');
            line.setAttribute('opacity', isOverlay ? '0.6' : '0.85');
            if (isOverlay) line.setAttribute('stroke-dasharray', '14 10');
            line.setAttribute('marker-end', `url(#${markerId})`);

            this.svgEl.appendChild(line);
        };

        this.arrows.forEach((arr, i) => draw(arr, i, false));
        this.overlayArrows.forEach((arr, i) => draw(arr, this.arrows.length + i, true));
    }

    _getSquareCenterPercent(sq) {
        if (!this._isValidSquare(sq)) {
            console.warn('Ignoring arrow with invalid square:', sq);
            return { x: 50, y: 50 };
        }
        const file = sq.charCodeAt(0) - 97; // 0..7
        const rank = parseInt(sq[1], 10);   // 1..8
        let col = file;
        let row = 8 - rank;

        if (this.options.orientation === 'black') {
            col = 7 - file;
            row = rank - 1;
        }

        return {
            x: (col + 0.5) * 12.5,
            y: (row + 0.5) * 12.5
        };
    }
    static getGlobalBoardSize() {
        if (typeof document === 'undefined') return 600;
        let val = null;
        if (document.documentElement && document.documentElement.style) {
            if (typeof document.documentElement.style.getPropertyValue === 'function') {
                val = document.documentElement.style.getPropertyValue('--board-size');
            } else if (document.documentElement.style['--board-size']) {
                val = document.documentElement.style['--board-size'];
            }
        }
        if (!val && typeof window !== 'undefined' && typeof window.getComputedStyle === 'function') {
            try {
                val = window.getComputedStyle(document.documentElement).getPropertyValue('--board-size');
            } catch (e) {}
        }
        if (val) {
            const parsed = parseInt(val, 10);
            if (!isNaN(parsed) && parsed > 0) return parsed;
        }
        if (typeof document.querySelector === 'function') {
            const boardEl = document.querySelector('.chess-board') || document.querySelector('.chess-board-wrapper');
            if (boardEl) {
                const w = (boardEl.getBoundingClientRect ? boardEl.getBoundingClientRect().width : boardEl.offsetWidth) || 0;
                if (w > 0) return Math.round(w);
            }
        }
        return 600;
    }

    static setGlobalBoardSize(sizeInPx, persist = false) {
        if (typeof document === 'undefined') return;
        const clamped = Math.max(380, Math.min(1400, Math.round(Number(sizeInPx) || 600)));

        if (document.documentElement && document.documentElement.style) {
            if (typeof document.documentElement.style.setProperty === 'function') {
                document.documentElement.style.setProperty('--board-size', `${clamped}px`);
            } else {
                document.documentElement.style['--board-size'] = `${clamped}px`;
            }
        }

        const labels = (typeof document.querySelectorAll === 'function') ? document.querySelectorAll('.board-size-display-val') : [];
        if (labels && labels.forEach) {
            labels.forEach(lbl => {
                lbl.textContent = `${clamped}px`;
            });
        }

        if (persist) {
            try {
                if (typeof localStorage !== 'undefined') {
                    localStorage.setItem('chess_board_size', clamped.toString());
                }
            } catch (e) {}
        }
    }

    static fitToScreen() {
        if (typeof window === 'undefined') return;
        const vh = window.innerHeight || 800;
        const vw = window.innerWidth || 1200;

        const maxV = Math.max(380, vh - 180);
        const maxH = Math.max(380, vw - 440);

        const optimal = Math.min(maxV, maxH);
        const fitSize = Math.max(480, Math.min(1200, Math.round(optimal)));
        InteractiveChessboard.setGlobalBoardSize(fitSize, true);
    }

    static initGlobalResizing() {
        if (typeof document === 'undefined') return;

        try {
            if (typeof localStorage !== 'undefined') {
                const saved = localStorage.getItem('chess_board_size');
                if (saved) {
                    const parsed = parseInt(saved, 10);
                    if (!isNaN(parsed) && parsed >= 380 && parsed <= 1400) {
                        InteractiveChessboard.setGlobalBoardSize(parsed, false);
                    }
                }
            }
        } catch (e) {}

        const current = InteractiveChessboard.getGlobalBoardSize();
        const labels = (typeof document.querySelectorAll === 'function') ? document.querySelectorAll('.board-size-display-val') : [];
        if (labels && labels.forEach) {
            labels.forEach(lbl => {
                lbl.textContent = `${current}px`;
            });
        }

        const bindButtons = (root = document) => {
            if (!root || typeof root.querySelectorAll !== 'function') return;
            root.querySelectorAll('.btn-board-size-dec').forEach(btn => {
                if (btn._hasSizeBound) return;
                btn._hasSizeBound = true;
                btn.addEventListener('click', (e) => {
                    if (e && e.preventDefault) e.preventDefault();
                    const cur = InteractiveChessboard.getGlobalBoardSize();
                    InteractiveChessboard.setGlobalBoardSize(cur - 40, true);
                });
            });
            root.querySelectorAll('.btn-board-size-inc').forEach(btn => {
                if (btn._hasSizeBound) return;
                btn._hasSizeBound = true;
                btn.addEventListener('click', (e) => {
                    if (e && e.preventDefault) e.preventDefault();
                    const cur = InteractiveChessboard.getGlobalBoardSize();
                    InteractiveChessboard.setGlobalBoardSize(cur + 40, true);
                });
            });
            root.querySelectorAll('.btn-board-size-fit').forEach(btn => {
                if (btn._hasSizeBound) return;
                btn._hasSizeBound = true;
                btn.addEventListener('click', (e) => {
                    if (e && e.preventDefault) e.preventDefault();
                    InteractiveChessboard.fitToScreen();
                });
            });
        };
        InteractiveChessboard._bindSizeButtons = bindButtons;
        bindButtons(document);

        if (typeof document.addEventListener === 'function') {
            document.addEventListener('click', (e) => {
                const target = e.target;
                if (!target || !target.closest) return;

                const decBtn = target.closest('.btn-board-size-dec');
                if (decBtn) {
                    if (e.preventDefault) e.preventDefault();
                    const cur = InteractiveChessboard.getGlobalBoardSize();
                    InteractiveChessboard.setGlobalBoardSize(cur - 40, true);
                    return;
                }

                const incBtn = target.closest('.btn-board-size-inc');
                if (incBtn) {
                    if (e.preventDefault) e.preventDefault();
                    const cur = InteractiveChessboard.getGlobalBoardSize();
                    InteractiveChessboard.setGlobalBoardSize(cur + 40, true);
                    return;
                }

                const fitBtn = target.closest('.btn-board-size-fit');
                if (fitBtn) {
                    if (e.preventDefault) e.preventDefault();
                    InteractiveChessboard.fitToScreen();
                    return;
                }
            });
        }
    }
}
window.InteractiveChessboard = InteractiveChessboard;

if (typeof window !== 'undefined' && typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
        if (typeof document.addEventListener === 'function') {
            document.addEventListener('DOMContentLoaded', () => {
                InteractiveChessboard.initGlobalResizing();
            });
        }
    } else {
        InteractiveChessboard.initGlobalResizing();
    }
}
