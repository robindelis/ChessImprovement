// Chess Opening Improvement Platform - Opening Scout & Interactive Line Explorer

// ============================================================================
//  SEC-01 / SEC-02 / CODE-19 - PURE RENDER HELPERS
//  No DOM access. `tests/test_scout_frontend_contracts.py` extracts this block
//  verbatim (it is delimited by the two marker comments) and runs it under
//  `node`, so the escaping guarantees below are covered by real tests.
// ============================================================================
// <BEGIN SCOUT_PURE_HELPERS>
const SCOUT_HTML_ESCAPES = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
    '`': '&#96;',
    '=': '&#61;'
};

/**
 * Escapes a value for interpolation into HTML text **or** into a quoted
 * attribute value. Escaping `=` and the backtick as well closes the unquoted
 * attribute contexts that a template literal can accidentally produce.
 */
function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"'`=]/g, (ch) => SCOUT_HTML_ESCAPES[ch]);
}

/**
 * Real numeric coercion for a value that is about to be interpolated into markup or a
 * `style="height:N%"`. SEC-M: every remote field reaches this file as JSON, so a count can
 * legitimately arrive as the *string* `"4"`, and `String.prototype.toLocaleString` /
 * `Number.prototype.toFixed` return their receiver's content — an un-coerced value therefore
 * lands verbatim in the markup. `Number()` alone is not enough either: it turns a corrupt
 * payload into `NaN` (rendered as the literal text "NaN") and `"1e999"` into `Infinity`, so
 * this helper falls back instead.
 */
function _scoutNum(value, fallback = 0) {
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
}

const SCOUT_RAW_HTML = Symbol('scoutRawHtml');

/** Marks a fragment as trusted, already-escaped markup for `safeHtml`. */
function rawHtml(markup) {
    return { [SCOUT_RAW_HTML]: true, markup: String(markup === null || markup === undefined ? '' : markup) };
}

function _scoutSerialize(value) {
    if (value && typeof value === 'object' && value[SCOUT_RAW_HTML]) return value.markup;
    return escapeHtml(value);
}

/**
 * Tagged template that escapes EVERY interpolation by default. Anything that has
 * to be raw must be passed through `rawHtml()` explicitly, so a remote title /
 * channel / study name can never become markup again.
 */
function safeHtml(strings, ...values) {
    let out = strings[0];
    for (let i = 0; i < values.length; i++) {
        out += _scoutSerialize(values[i]) + strings[i + 1];
    }
    return out;
}

const SCOUT_IMAGE_FALLBACK = '/assets/pieces/wN.svg';

/**
 * Only same-origin-relative paths and http(s) URLs are allowed through; every
 * other scheme (`javascript:`, `data:`, `vbscript:`, ...) falls back to the
 * local placeholder.
 */
function safeImageUrl(url) {
    const raw = (url === null || url === undefined) ? '' : String(url).trim();
    if (!raw) return SCOUT_IMAGE_FALLBACK;
    if (raw.startsWith('/') || raw.startsWith('./') || raw.startsWith('../')) return raw;
    if (/^https?:\/\//i.test(raw)) return raw;
    return SCOUT_IMAGE_FALLBACK;
}

/** "M:SS" / "H:MM:SS" clock used by the video sync strip. */
function formatClock(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return (h > 0 ? `${h}:${pad(m)}` : pad(m)) + ':' + pad(s);
}

/**
 * Canonical FEN: the backend's `normalize_fen` (first four fields). The
 * halfmove/fullmove counters are semantically irrelevant to position identity
 * but participated in every raw string comparison in this file, which is what
 * let the same (position, move) pair be recorded twice (CODE-17).
 */
function normalizeTreeFen(fen) {
    if (!fen) return '';
    return String(fen).trim().split(/\s+/).slice(0, 4).join(' ');
}

const SCOUT_REP_LABELS = { white: 'White', black: 'Black' };

function _scoutOverlapStatus(data) {
    return (data && typeof data.status === 'string') ? data.status : 'new';
}

function _scoutOverlapMessage(data, repertoire, fallback) {
    const serverMessage = (data && typeof data.message === 'string') ? data.message.trim() : '';
    return serverMessage || fallback;
}

/**
 * The single source of truth for the AGENTS.md §4E overlap badge wording.
 * Previously three renderers produced three different strings for the same
 * line, so the text visibly mutated under the user (CODE-19).
 */
function overlapBadgeInfo(data, repertoire) {
    const rep = SCOUT_REP_LABELS[repertoire] || SCOUT_REP_LABELS.white;
    const status = _scoutOverlapStatus(data);
    // SEC-M: these three land in the badge *text* (see the `partially_present` branch
    // below), so a corrupt / hostile count must render as `0`, never as its own payload.
    const matched = _scoutNum(data && data.matched_plies, 0);
    const total = _scoutNum(data && data.total_plies, 0);
    const fresh = _scoutNum(data && data.new_plies, 0);

    if (status === 'already_present') {
        return {
            cls: 'rep-present',
            text: `✓ In ${rep}`,
            title: _scoutOverlapMessage(data, repertoire, `All ${total} plies of this line are already in your ${rep} repertoire.`)
        };
    }
    if (status === 'partially_present') {
        return {
            cls: 'rep-partial',
            text: `⚡ ${rep}: ${matched}/${total} in Rep (+${fresh} new)`,
            title: _scoutOverlapMessage(data, repertoire, `${matched} of ${total} plies are in your ${rep} repertoire; ${fresh} new ${fresh === 1 ? 'move' : 'moves'} to extend it.`)
        };
    }
    return {
        cls: 'rep-new',
        text: '★ New Line',
        title: _scoutOverlapMessage(data, repertoire, `New line - none of its plies is in your ${rep} repertoire yet.`)
    };
}

function overlapBadgeMarkup(data, repertoire) {
    const badge = overlapBadgeInfo(data, repertoire);
    return safeHtml`<span class="scout-overlap-badge ${badge.cls}" title="${badge.title}">${badge.text}</span>`;
}

/** Label + tooltip of the "import this line" action on a line card. */
function overlapActionInfo(data, repertoire) {
    const rep = SCOUT_REP_LABELS[repertoire] || SCOUT_REP_LABELS.white;
    const status = _scoutOverlapStatus(data);
    const matched = _scoutNum(data && data.matched_plies, 0);
    const fresh = _scoutNum(data && data.new_plies, 0);
    if (status === 'already_present') {
        return {
            text: `✓ In ${rep} (${matched}p)`,
            title: _scoutOverlapMessage(data, repertoire, `All ${matched} plies of this line are already in your ${rep} repertoire.`)
        };
    }
    if (status === 'partially_present') {
        return {
            text: `📥 Extend ${rep} (+${fresh})`,
            title: _scoutOverlapMessage(data, repertoire, `Only the first ${matched} plies are in your ${rep} repertoire; importing adds the remaining ${fresh}.`)
        };
    }
    return {
        text: `📥 Line → ${rep}`,
        title: `Save this whole line into ${rep} Repertoire`
    };
}

function overlapIsInRepertoire(data) {
    const status = _scoutOverlapStatus(data);
    return status === 'already_present' || status === 'partially_present';
}

/**
 * AGENTS.md §4E: "If a user move diverges at a turn where a move is already
 * prepared, warns that the new move replaces the prepared move." Importing
 * such a line DESTROYS the prepared move, so the client must know.
 */
function overlapReplacesPreparedMove(data) {
    const divergence = (data && data.divergence_info) || null;
    return !!(divergence && divergence.type === 'user_move_alternative');
}

function overlapReplacementWarning(data) {
    const divergence = (data && data.divergence_info) || null;
    if (!overlapReplacesPreparedMove(data)) return '';
    const rep = SCOUT_REP_LABELS[data.repertoire] || 'your';
    const newSan = divergence.new_san || divergence.new_uci || 'this move';
    const oldSan = divergence.existing_san || divergence.existing_uci || 'your prepared move';
    return `At move ${divergence.ply}, ${newSan} REPLACES your prepared ${oldSan} for ${rep}. The old line will be deleted.`;
}

/** One search-result card. All remote fields go through `safeHtml`. */
function scoutCardMarkup(item) {
    const src = item || {};
    const isYoutube = src.source_type === 'youtube';
    const sourceBadge = isYoutube
        ? '<span class="scout-badge-youtube">YouTube</span>'
        : '<span class="scout-badge-guide">Master Guide</span>';
    const ecoBadge = src.eco ? safeHtml`<span class="badge-eco">${src.eco}</span>` : '';
    const title = src.title || 'Untitled';
    return safeHtml`
        <div class="card-panel scout-card" data-source-id="${src.id}" data-source-type="${src.source_type}">
            <div class="scout-card-thumb-wrap">
                <img src="${safeImageUrl(src.thumbnail)}" alt="${title}" class="scout-card-thumb" data-fallback-src="${SCOUT_IMAGE_FALLBACK}">
                <span class="scout-card-duration">${src.duration_formatted || ''}</span>
            </div>
            <div class="scout-card-body">
                <div class="scout-card-meta">${rawHtml(sourceBadge)}${rawHtml(ecoBadge)}</div>
                <div class="scout-card-title" title="${title}">${title}</div>
                <div class="scout-card-channel">${src.channel || 'Chess Guide'}</div>
            </div>
        </div>`;
}

/** One "Explored Lines" card. `ctx` carries the pre-computed overlap data. */
function scoutLineCardMarkup(line, index, ctx) {
    const l = line || {};
    const context = ctx || {};
    const wData = context.white || null;
    const bData = context.black || null;
    const isActive = !!context.isActive;
    const study = l.studyName || context.currentStudy || '';
    const wAction = overlapActionInfo(wData, 'white');
    const bAction = overlapActionInfo(bData, 'black');
    const badges = (wData ? overlapBadgeMarkup(wData, 'white') : '') + (bData ? overlapBadgeMarkup(bData, 'black') : '');
    const hasNotes = !!(l.moves && l.moves.some(m => m.comment && String(m.comment).trim()));
    const hasImportant = !!(l.moves && l.moves.some(m => m.is_important));
    const lineId = context.lineId || '';

    return safeHtml`
        <div class="scout-var-card ${isActive ? 'active' : ''}" data-idx="${index}" data-line-id="${lineId}">
            <div class="scout-var-header">
                <div style="display:flex; align-items:center; gap:0.4rem; overflow:hidden;">
                    <span class="scout-var-badge">#${index + 1}</span>
                    <span class="scout-line-name" title="${l.name || 'Explored Line'}" style="font-weight:700; color:#fff; font-size:0.83rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                        ${l.name || 'Explored Line'}
                    </span>
                    ${l.eco ? rawHtml(safeHtml`<span style="font-size:0.7rem; color:var(--accent-primary); font-weight:700;">${l.eco}</span>`) : rawHtml('')}
                </div>
                <div style="display:flex; align-items:center; gap:0.35rem; flex-shrink:0;">
                    ${hasNotes ? rawHtml('<span title="Contains move notes" style="cursor:help; font-size:0.75rem;">📝</span>') : rawHtml('')}
                    ${hasImportant ? rawHtml('<span title="Contains priority moves" style="cursor:help; font-size:0.75rem;">⭐</span>') : rawHtml('')}
                    <span class="scout-var-plies">${_scoutNum(l.plyCount, 0)} plies</span>
                    ${isActive ? rawHtml('<span class="scout-var-status">Active</span>') : rawHtml('')}
                </div>
            </div>
            <div class="scout-var-pgn" title="${l.pgn}">${l.pgn}</div>
            <div class="scout-card-overlap-badges" style="display:flex; gap:0.35rem; align-items:center; margin-top:0.35rem; flex-wrap:wrap; min-height:20px;">
                ${rawHtml(badges)}
            </div>
            <div class="scout-line-study-row" style="margin-top:0.35rem; display:flex; align-items:center; justify-content:space-between; gap:0.4rem; background:rgba(15, 23, 42, 0.55); padding:0.25rem 0.5rem; border-radius:5px; border:1px solid rgba(51, 65, 85, 0.6); font-size:0.75rem;">
                <div style="display:flex; align-items:center; gap:0.35rem; overflow:hidden;">
                    <span style="color:#38bdf8; font-weight:700; flex-shrink:0;">📁 Study:</span>
                    <span class="scout-line-study-name" title="${study}" style="color:${study ? '#f1f5f9' : 'var(--text-muted)'}; font-weight:600; text-overflow:ellipsis; overflow:hidden; white-space:nowrap;">
                        ${study || '(No Study Assigned)'}
                    </span>
                </div>
                <button class="btn-var-action btn-assign-line-study" data-idx="${index}" title="Assign this line to a study group" style="font-size:0.7rem; padding:0.15rem 0.45rem; flex-shrink:0;">
                    ✏️ Assign
                </button>
            </div>
            <div class="scout-var-actions" style="display:flex; gap:0.3rem; align-items:center; flex-wrap:wrap; margin-top:0.4rem;">
                <button class="btn-var-action btn-load-tree-line" data-idx="${index}" title="Load this line onto the board">
                    👀 Load
                </button>
                <button class="btn-var-action btn-import-tree-white" data-idx="${index}" title="${wAction.title}">
                    ${wAction.text}
                </button>
                <button class="btn-var-action btn-import-tree-black" data-idx="${index}" title="${bAction.title}">
                    ${bAction.text}
                </button>
                ${overlapIsInRepertoire(wData)
                    ? rawHtml(safeHtml`<button class="btn-var-action btn-del-tree-white" data-idx="${index}" title="Remove this line from White Repertoire" style="color:#ef4444; border-color:rgba(239,68,68,0.35);">🗑️ White</button>`)
                    : rawHtml('')}
                ${overlapIsInRepertoire(bData)
                    ? rawHtml(safeHtml`<button class="btn-var-action btn-del-tree-black" data-idx="${index}" title="Remove this line from Black Repertoire" style="color:#ef4444; border-color:rgba(239,68,68,0.35);">🗑️ Black</button>`)
                    : rawHtml('')}
                <button class="btn-var-action btn-del-session-line" data-idx="${index}" title="Delete this line from session tree" style="color:var(--text-muted); padding:0.25rem 0.45rem;">
                    ✕
                </button>
            </div>
        </div>`;
}

/** The strip under the board; uses the very same badge renderer as the cards. */
function scoutOverlapStripMarkup(dataW, dataB) {
    return safeHtml`
        <span style="font-size:0.72rem; color:var(--text-muted); font-weight:600;">Repertoire:</span>
        ${rawHtml(overlapBadgeMarkup(dataW, 'white'))}
        ${rawHtml(overlapBadgeMarkup(dataB, 'black'))}`;
}

/**
 * AGENTS.md §8.2: clamp a dynamic-text element from JS (nowrap + ellipsis +
 * explicit max-width) so a long opening name / SAN cannot reflow its row. The
 * `title` attribute carries the full string.
 */
function _clampInline(el, maxWidth) {
    if (!el) return;
    el.style.whiteSpace = 'nowrap';
    el.style.overflow = 'hidden';
    el.style.textOverflow = 'ellipsis';
    el.style.display = 'inline-block';
    el.style.verticalAlign = 'bottom';
    if (maxWidth) el.style.maxWidth = maxWidth;
}

// ===========================================================================
// Discovery: which side, which intent, and why this result is on the shelf.
//
// Everything below is a pure function of the `/api/scout/discover` payload, so
// it lives in this block and is executed verbatim by
// `tests/test_scout_frontend_contracts.py` under node.  That is deliberate: the
// one thing the Scout got wrong before was deciding what a video teaches, and
// the rules for that are the part most worth testing without a browser.
// ===========================================================================

const SCOUT_SIDE_LABEL = { white: 'White', black: 'Black' };
const SCOUT_MODE_LABEL = { play: 'play it', against: 'play against it', either: 'either' };

/** Length classes. A 3-minute answer and a 3-hour course are not the same shelf. */
const SCOUT_LENGTH_CLASSES = [
    { max: 900, key: 'quick', label: 'Quick hit' },
    { max: 2400, key: 'single', label: 'Single line' },
    { max: 7200, key: 'course', label: 'Short course' }
];
const SCOUT_LENGTH_LONG = { key: 'long', label: 'Full course' };

function scoutLengthInfo(seconds) {
    const s = _scoutNum(seconds, 0);
    if (s <= 0) return { key: 'theory', label: 'Theory' };
    for (const tier of SCOUT_LENGTH_CLASSES) {
        if (s <= tier.max) return { key: tier.key, label: tier.label };
    }
    return SCOUT_LENGTH_LONG;
}

function scoutFormatViews(views) {
    const n = _scoutNum(views, 0);
    if (n <= 0) return '';
    if (n >= 1000000) return `${(n / 1000000).toFixed(n >= 10000000 ? 0 : 1)}M views`;
    if (n >= 1000) return `${Math.round(n / 1000)}K views`;
    return `${n} views`;
}

/**
 * "3 years ago" from a `YYYYMMDD` upload date.  Returns '' for anything
 * unusable rather than "NaN years ago" - the string lands in a card, and a
 * corrupt date must not become a corrupt claim.
 */
function scoutFormatUploadDate(uploadDate, nowMs) {
    const raw = String(uploadDate || '').trim();
    if (!/^\d{8}$/.test(raw)) return '';
    const year = Number(raw.slice(0, 4));
    const month = Number(raw.slice(4, 6));
    const day = Number(raw.slice(6, 8));
    if (month < 1 || month > 12 || day < 1 || day > 31) return '';
    const then = Date.UTC(year, month - 1, day);
    if (!Number.isFinite(then)) return '';
    const now = Number.isFinite(nowMs) ? nowMs : Date.now();
    const days = Math.floor((now - then) / 86400000);
    if (days < 0) return '';
    if (days < 1) return 'today';
    if (days === 1) return 'yesterday';
    if (days < 30) return `${days} days ago`;
    const months = Math.round(days / 30.4);
    if (months < 12) return months === 1 ? '1 month ago' : `${months} months ago`;
    const years = Math.round(months / 12);
    return years === 1 ? '1 year ago' : `${years} years ago`;
}

/**
 * The perspective badge: which side does this video teach, and how sure are we?
 *
 * The `unknown` case is a first-class outcome, not a fallback.  Before this
 * existed a shelf of "how to beat X" videos silently mixed in the videos for
 * playing X, because nothing recorded which was which; now an honest "?" is
 * better than a confident wrong colour.
 */
function scoutPerspectiveInfo(item) {
    const src = item || {};
    const side = src.side_teach;
    const mode = src.mode_teach;
    const confidence = src.classify_confidence || 'none';
    if (side !== 'white' && side !== 'black') {
        return {
            cls: 'scout-perspective--unknown',
            text: '? side unclear',
            title: src.classify_evidence
                ? `Could not tell which side this teaches: ${src.classify_evidence}`
                : 'This title does not say which side it teaches. Open it to find out.'
        };
    }
    const sideLabel = SCOUT_SIDE_LABEL[side] || side;
    const isAgainst = mode === 'against';
    return {
        cls: `scout-perspective--${side}${isAgainst ? '--against' : ''}`,
        text: isAgainst ? `🛡 ${sideLabel} · meets this` : `▶ ${sideLabel}`,
        title: src.classify_evidence
            ? `Teaches ${sideLabel}${isAgainst ? ' meeting this opening' : ''}: ${src.classify_evidence}`
            : `Teaches ${sideLabel}.`
    };
}

/** The "why is this here" chips. Empty is fine; a wrong claim is not. */
function scoutReasonChips(item) {
    const reasons = (item && Array.isArray(item.reasons)) ? item.reasons : [];
    return reasons
        .filter(r => typeof r === 'string' && r.trim())
        .slice(0, 3)
        .map(r => safeHtml`<span class="scout-why-chip">${r.trim()}</span>`)
        .join('');
}

/** One encyclopedia master line, offered as theory rather than as a video. */
function scoutEcoGuideMarkup(guide) {
    const g = guide || {};
    const side = g.side === 'white' ? '⚪ White' : (g.side === 'black' ? '⚫ Black' : '? side');
    const moves = _scoutNum(g.move_count, 0);
    return safeHtml`
        <div class="card-panel scout-card" data-source-id="${g.id}" data-source-type="eco_guide" data-card="guide">
            <div class="scout-card-thumb-wrap scout-card-thumb-wrap--guide">
                <img src="${safeImageUrl(g.thumbnail)}" alt="${g.label || g.title || ''}" class="scout-card-thumb" data-fallback-src="${SCOUT_IMAGE_FALLBACK}">
                <span class="scout-card-duration">${moves ? `${moves} moves` : 'Theory'}</span>
            </div>
            <div class="scout-card-body">
                <div class="scout-card-meta">
                    <span class="scout-badge-guide">Master Guide</span>
                    ${g.eco ? safeHtml`<span class="badge-eco">${g.eco}</span>` : ''}
                    <span class="scout-perspective scout-perspective--theory">${side}</span>
                </div>
                <div class="scout-card-title" title="${g.label || g.title || ''}">${g.label || g.title || 'Theory line'}</div>
                <div class="scout-card-channel">Encyclopedia master line · opens on the board</div>
            </div>
        </div>`;
}

/**
 * One search result.  Every remote field goes through `safeHtml`.
 *
 * The card is *sized for its final content*: the second (enrichment) paint only
 * fills text into slots that already exist, so filling them is 0px of layout
 * shift (AGENTS.md §8.1/§8.5).  Slots that have nothing to show render their
 * placeholder rather than disappearing, for the same reason.
 */
function scoutResultCardMarkup(item) {
    const src = item || {};
    const title = src.title || 'Untitled';
    const isYoutube = src.source_type === 'youtube';
    const perspective = scoutPerspectiveInfo(src);
    const length = scoutLengthInfo(src.duration);
    const views = scoutFormatViews(src.view_count);
    const uploaded = scoutFormatUploadDate(src.upload_date);
    const chapters = Array.isArray(src.chapters) ? src.chapters : [];
    const firstChapters = chapters.slice(0, 2)
        .map(c => (c && typeof c.title === 'string') ? c.title : '')
        .filter(Boolean);
    const description = String(src.description || '').trim();
    const analyzed = src.is_analyzed
        ? safeHtml`<span class="scout-flag scout-flag--done" title="Already extracted once — opening it reopens the studio instantly.">✓ Extracted</span>`
        : '';
    const favBtn = safeHtml`<button class="scout-fav-toggle${src.is_favorite ? ' is-on' : ''}" data-fav-id="${src.id}" title="${src.is_favorite ? 'Remove bookmark' : 'Bookmark this for later'}" aria-label="Bookmark">${src.is_favorite ? '★' : '☆'}</button>`;

    return safeHtml`
        <div class="card-panel scout-card" data-source-id="${src.id}" data-source-type="${src.source_type}" data-card="rich">
            <div class="scout-card-thumb-wrap">
                <img src="${safeImageUrl(src.thumbnail)}" alt="${title}" class="scout-card-thumb" data-fallback-src="${SCOUT_IMAGE_FALLBACK}">
                <span class="scout-card-duration">${src.duration_formatted || ''}</span>
            </div>
            <div class="scout-card-body">
                <div class="scout-card-meta">
                    ${rawHtml(isYoutube ? '<span class="scout-badge-youtube">YouTube</span>' : '<span class="scout-badge-guide">Master Guide</span>')}
                    ${src.eco ? rawHtml(`<span class="badge-eco">${escapeHtml(src.eco)}</span>`) : ''}
                    <span class="scout-perspective ${perspective.cls}" title="${perspective.title}">${perspective.text}</span>
                    ${rawHtml(analyzed)}
                </div>
                <div class="scout-card-title" title="${title}">${title}</div>
                <div class="scout-card-channel" title="${src.channel || ''}">${src.channel || 'Chess Guide'} · <span class="scout-length-tag scout-length-tag--${length.key}">${length.label}</span></div>
                <div class="scout-card-desc" title="${description}">${description || ' '}</div>
                <div class="scout-card-why">${rawHtml(scoutReasonChips(src))}</div>
                <div class="scout-card-foot">
                    <span class="scout-card-stat">${views || ' '}</span>
                    <span class="scout-card-stat">${uploaded || ' '}</span>
                    <span class="scout-card-stat" title="${firstChapters.join(' · ')}">${chapters.length ? `${chapters.length} chapters` : ' '}</span>
                    ${rawHtml(favBtn)}
                </div>
            </div>
        </div>`;
}

/**
 * Sort modes. `match` is the server's intent order and is the default, because
 * "best answer to my question" is not "most watched".
 */
const SCOUT_SORTS = {
    match: (a, b) => (_scoutNum(b.intent_score) - _scoutNum(a.intent_score)) || (_scoutNum(b.quality_score) - _scoutNum(a.quality_score)),
    watched: (a, b) => _scoutNum(b.view_count) - _scoutNum(a.view_count),
    shortest: (a, b) => _scoutNum(a.duration) - _scoutNum(b.duration),
    newest: (a, b) => String(b.upload_date || '').localeCompare(String(a.upload_date || '')),
    extracted: (a, b) => (a.is_analyzed === b.is_analyzed ? 0 : (a.is_analyzed ? -1 : 1))
};

function scoutSortResults(items, mode) {
    const list = Array.isArray(items) ? items.slice() : [];
    const cmp = SCOUT_SORTS[mode] || SCOUT_SORTS.match;
    return list.sort(cmp);
}

/**
 * The one-line statement of what the shelf is answering.  Always rendered (an
 * empty state renders a placeholder, not nothing) so the strip is a constant
 * height and explaining itself costs zero layout shift.
 */
function scoutIntentSummary(intent, info) {
    const i = intent || {};
    const meta = info || {};
    const side = SCOUT_SIDE_LABEL[i.side];
    const mode = SCOUT_MODE_LABEL[i.mode];
    const bits = [];
    if (side) bits.push(`as ${side}`);
    if (mode && mode !== 'either') bits.push(mode);
    if (i.needs_disambiguation) bits.push('pick an opening below');
    if (!bits.length && !meta.count) return 'Search an opening to see what answers your question.';
    const head = bits.length ? `Showing ${bits.join(' · ')}` : 'Showing everything';
    const tail = [];
    if (_scoutNum(meta.count, 0)) tail.push(`${_scoutNum(meta.count, 0)} results`);
    if (_scoutNum(meta.replies, 0)) tail.push(`${_scoutNum(meta.replies, 0)} critical ${_scoutNum(meta.replies, 0) === 1 ? 'reply' : 'replies'}`);
    if (meta.gaps) tail.push(`${meta.gaps} gap${meta.gaps === 1 ? '' : 's'} in your repertoire`);
    return tail.length ? `${head} · ${tail.join(' · ')}` : head;
}

/** One disambiguation chip. Clicking it is the answer to "which Alapin?". */
function scoutCandidateChipMarkup(cand) {
    const c = cand || {};
    const side = c.side === 'white' ? '⚪' : (c.side === 'black' ? '⚫' : '?');
    const moves = (Array.isArray(c.opening_moves) ? c.opening_moves : []).slice(0, 5).join(' ');
    return safeHtml`
        <button class="scout-candidate-chip" data-candidate-eco="${c.eco}" title="${c.name || ''}${moves ? ` — ${moves}` : ''}">
            <span class="scout-candidate-eco">${c.eco || '--'}</span>
            <span class="scout-candidate-side">${side}</span>
            <span class="scout-candidate-name">${c.label || c.name || ''}</span>
        </button>`;
}

// ===========================================================================
// Coverage: which of the critical replies the user still has to prepare.
//
// This is the answer a shelf of theory cannot give. The three states are the
// Scout's own vocabulary, shared with the Repertoire Builder (`gap_kind`), so the
// two surfaces cannot describe the same position differently:
//
//   prepared          ✓ In repertoire, and something is prepared after it
//   unanswered_branch ! In repertoire, but the line stops on THEIR move
//   missing_branch    ✗ Not in the repertoire at all
//
// The middle one is the subtle one. AGENTS.md §4.G ruled that a branch you hold
// but cannot answer is a *gap of the same kind* as one you do not hold, because
// both stop the line on the opponent's move -- so it is counted as a gap here
// for exactly that reason, and it is not presented as a finished line.
//
// `✓` is deliberately the same glyph §4.E's "✓ In White" badge uses: "you have
// this" should look the same everywhere. The other two are distinct from §4.E's
// `⚡` and `★` on purpose -- one glyph per meaning, and the overlap badge's
// wording is produced in exactly one place (CODE-19).
// ===========================================================================

const SCOUT_COVERAGE_META = {
    prepared: { cls: 'is-prepared', mark: '✓', label: 'prepared' },
    unanswered_branch: { cls: 'is-unanswered', mark: '!', label: 'no answer' },
    missing_branch: { cls: 'is-missing', mark: '✗', label: 'not prepared' }
};

function scoutCoverageState(row) {
    const state = row && row.state;
    return SCOUT_COVERAGE_META[state] || SCOUT_COVERAGE_META.missing_branch;
}

/** One sentence per reply, saying what is wrong with it rather than what is right. */
function scoutCoverageTitle(row) {
    const r = row || {};
    const name = r.label || r.name || '';
    switch (r.state) {
        case 'prepared':
            return `${r.san || ''} — you have this reply and a prepared answer after it. ${name}`;
        case 'unanswered_branch':
            return `${r.san || ''} — you have this reply but nothing prepared after it, so the line would stop on their move. ${name}`;
        default:
            return `${r.san || ''} — not in your repertoire. This is a gap you have to close. ${name}`;
    }
}

function scoutCoverageRowMarkup(row, side) {
    const r = row || {};
    const meta = scoutCoverageState(r);
    const rep = SCOUT_SIDE_LABEL[side] || 'your';
    // Only a gap is actionable; offering to "prepare" something already prepared
    // would be a button that does nothing.
    const action = r.state === 'prepared'
        ? ''
        : safeHtml`<button class="scout-coverage-action" data-coverage-uci="${r.uci}" data-coverage-fen="${r.parent_fen || ''}" title="${r.state === 'unanswered_branch' ? 'You have the reply; the Builder will show the position where your answer is missing' : 'Open the Builder at this position to add the reply'}">Prepare this</button>`;
    return safeHtml`
        <span class="scout-coverage-row ${meta.cls}" title="${scoutCoverageTitle(r)}">
            <span class="scout-coverage-mark">${meta.mark}</span>
            <span class="scout-coverage-san">${r.san || '?'}</span>
            <span class="scout-coverage-eco">${r.eco || ''}</span>
            <span class="scout-coverage-state">${meta.label}</span>
            ${rawHtml(action)}
        </span>`;
}

/**
 * The plan panel: the side-appropriate strategic background for the opening.
 *
 * `strategic_ideas` carries a `white` and a `black` block plus `matched`. In
 * "play it" mode the user reads their own side; in "play against it" they read
 * the side they are about to face, which is the only one that tells them what is
 * coming. When nothing is known for the family, `matched` is false and the panel
 * says so instead of dressing up generic advice as opening theory.
 *
 * The three rows are keyed by `data-field`, which is why this component needs no
 * per-field element ids at all.
 */
function scoutPlanPanelMarkup(ideas, side, title) {
    const data = ideas && typeof ideas === 'object' ? ideas : {};
    const block = (side === 'black' ? data.black : data.white) || null;
    if (!block || typeof block.core_idea !== 'string') return '';
    const matched = data.matched !== false;
    const rows = [
        ['core_idea', 'The idea', block.core_idea],
        ['typical_mistakes', 'Typical mistakes', block.typical_mistakes],
        ['key_plans', 'Plans', block.key_plans]
    ].filter(r => typeof r[2] === 'string' && r[2].trim());
    if (!rows.length) return '';
    const who = side === 'black' ? 'Black' : 'White';
    return safeHtml`
        <details class="scout-plan-panel" data-matched="${matched ? 'true' : 'false'}">
            <summary class="scout-plan-summary">${title || `What ${who} is trying to do here`}</summary>
            <div class="scout-plan-body">
                ${rawHtml(matched ? '' : '<div class="scout-plan-note">No notes on this opening in the local encyclopedia — these are the universal priorities.</div>')}
                ${rawHtml(rows.map(r => safeHtml`<div class="scout-plan-row" data-field="${r[0]}">
                    <span class="scout-plan-label">${r[1]}</span>
                    <span class="scout-plan-text">${r[2]}</span>
                </div>`).join(''))}
            </div>
        </details>`;
}
// <END SCOUT_PURE_HELPERS>

// Session Tree Node representing a board state in an explored line
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

// Tree tracking all lines and branches explored in the session
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

        // Update metadata if child already exists (richest-field-wins, matching
        // repertoire_builder.js so a re-played move inherits the stored note).
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
                // DUP-01: the `child && child.move` guard is the builder's copy's null
                // defence; it is adopted here so the two `SessionLineTree` class bodies
                // stay byte-identical (a no-op in practice - `addMove` returns early on
                // a null move, so a child always has one).
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

class OpeningScout {
    constructor() {
        this.board = null;
        this.activeStudy = null;
        this.lineTree = new SessionLineTree(); // Line and branch exploration tree
        this.ytPlayer = null;
        this.ytPlayerReady = false;
        this.ytVideoId = null;
        this.ytPendingVideoId = null;
        this.ytApiRetryTimer = null;
        this.ytSyncTimer = null;
        this.evalAbortController = null;
        // Purely cosmetic. CODE-01: this used to be the *repertoire key*, so one
        // "Flip Board" click wrote a White move into the Black repertoire. It
        // must never be read by any write path again.
        this.orientation = 'white';
        this.activeFilter = 'all';
        this.cachedResults = [];
        this.lastBoardFen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
        this.treeOverlapMap = { white: {}, black: {} };
        this.activeOverlapDebounce = null;
        this.currentStudy = '';
        this.studiesList = [];
        // CODE-05: request generation counters (stale responses are dropped).
        this.searchSeq = 0;
        this.analyzeSeq = 0;
        this.activeOverlapSeq = 0;
        this.treeOverlapSeq = 0;
        this.activeOverlapController = null;
        this.treeOverlapController = null;
        this.searchController = null;
        this.activeLineOverlap = null;
        this._treeOverlapSnapshot = '';
        this._lastSyncedUci = null;
        // CODE-01: the last repertoire the user *explicitly* targeted with an
        // import / delete button. This - never `orientation` - is the fallback
        // key for actions that are not about a single move.
        this.lastActionRepertoire = 'white';
        // CODE-10: uci -> video timestamp map of the loaded study, for the
        // board <-> player sync.
        this.moveTimestamps = {};
        this._videoSyncGuard = false;

        // --- discovery state (side / play-vs-against) -------------------
        // `intentSide` and `intentMode` are '' when the user has expressed no
        // preference, which is what tells the server to infer from the query.
        // Anything else is sent verbatim.
        this.intentSide = '';
        this.intentMode = '';
        this.intent = null;
        this.intentReplies = [];
        this.coverage = null;
        this.chosenEco = '';
        this.ecoGuides = [];
        this.needsEnrichment = [];
        this.enrichSeq = 0;
        this.sortMode = 'match';
        this.suggestTimer = null;
        this.suggestSeq = 0;

        this._init();
    }

    _init() {
        this._bindUI();
        this._initBoard();
        this._primeQuickRepToolbar();
        this.loadStudies();
        // Load initial popular search
        this.search('Caro-Kann Defense');
    }

    /**
     * AGENTS.md §8.4: the quick-rep toolbar is a wrapping flex row, so every
     * conditional action must reserve its box from the first paint (fixed
     * `min-width` + `visibility:hidden` + `disabled`) instead of appearing and
     * disappearing with `display`.
     */
    _primeQuickRepToolbar() {
        const fixed = {
            btnScoutMarkImportant: '212px',
            btnScoutRemoveFromRep: '150px',
            btnScoutTrainAfterImport: '150px'
        };
        Object.keys(fixed).forEach((id) => {
            const el = document.getElementById(id);
            if (!el) return;
            el.style.minWidth = fixed[id];
            el.style.justifyContent = 'center';
        });
        ['btnScoutRemoveFromRep', 'btnScoutTrainAfterImport'].forEach((id) => {
            const el = document.getElementById(id);
            if (!el) return;
            el.style.display = 'inline-flex';
            el.style.visibility = 'hidden';
            el.style.opacity = '0';
            el.disabled = true;
        });
    }

    _initBoard() {
        const boardEl = document.getElementById('scoutBoard');
        if (!boardEl) return;
        this.board = new InteractiveChessboard(boardEl, {
            orientation: this.orientation,
            interactive: true,
            onMove: (from, to, promo, moveRes) => this._onUserBoardMove(from, to, promo, moveRes),
            onArrowsChange: (arrows) => this._onArrowsChange(arrows)
        });
    }

    _bindUI() {
        // Search form submit
        const form = document.getElementById('scoutSearchForm');
        if (form) {
            form.addEventListener('submit', (e) => {
                e.preventDefault();
                const q = document.getElementById('scoutSearchInput')?.value.trim();
                if (q) this.search(q);
            });
        }

        this._bindIntentControls();
        this._bindSuggest();

        // Popular opening chips
        document.querySelectorAll('.scout-chip').forEach(chip => {
            chip.addEventListener('click', () => {
                document.querySelectorAll('.scout-chip').forEach(c => c.classList.remove('active'));
                chip.classList.add('active');
                if (chip.id === 'chipCurrentRep') {
                    const repTitle = document.getElementById('repOpeningTitle')?.textContent || 'Italian Game';
                    const cleanTitle = repTitle.replace(/:.*/, '').trim();
                    const input = document.getElementById('scoutSearchInput');
                    if (input) input.value = cleanTitle;
                    this.search(cleanTitle);
                } else {
                    const q = chip.dataset.query;
                    const input = document.getElementById('scoutSearchInput');
                    if (input) input.value = q;
                    this.search(q);
                }
            });
        });

        // Shelf filter buttons (CODE-16: the literals below are the ones
        // `data-filter` actually carries in index.html - they used to be
        // 'videos'/'guides'/'favorites', none of which matched a button).
        document.querySelectorAll('.scout-filter-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.scout-filter-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.activeFilter = btn.dataset.filter;
                if (this.activeFilter === 'recent') {
                    this.loadRecent();
                } else {
                    this.renderResults();
                }
            });
        });

        // Direct input modal dialog
        const btnDirect = document.getElementById('btnScoutDirectInput');
        const dialog = document.getElementById('scoutDirectDialog');
        const btnClose = document.getElementById('btnCloseScoutModal');
        const btnCancel = document.getElementById('btnCancelScoutDirect');
        const btnSubmit = document.getElementById('btnSubmitScoutDirect');

        if (btnDirect && dialog) {
            btnDirect.addEventListener('click', () => dialog.showModal());
        }
        if (btnClose && dialog) {
            btnClose.addEventListener('click', () => dialog.close());
        }
        if (btnCancel && dialog) {
            btnCancel.addEventListener('click', () => dialog.close());
        }
        if (dialog) {
            dialog.addEventListener('click', (e) => {
                const rect = dialog.getBoundingClientRect();
                const inDialog = (
                    rect.top <= e.clientY &&
                    e.clientY <= rect.top + rect.height &&
                    rect.left <= e.clientX &&
                    e.clientX <= rect.left + rect.width
                );
                if (!inDialog) dialog.close();
            });
        }
        if (btnSubmit && dialog) {
            btnSubmit.addEventListener('click', () => {
                const text = document.getElementById('scoutDirectInputText')?.value.trim();
                if (text) {
                    dialog.close();
                    this.analyzeSource(text);
                }
            });
        }

        // Back to results button
        const btnBack = document.getElementById('btnBackToScoutSearch');
        if (btnBack) {
            btnBack.addEventListener('click', () => {
                if (this.ytPlayer && typeof this.ytPlayer.stopVideo === 'function') {
                    try { this.ytPlayer.stopVideo(); } catch (_) {}
                }
                document.getElementById('scoutStudio').style.display = 'none';
                document.getElementById('scoutResultsSection').style.display = 'block';
                const searchPanel = document.querySelector('.scout-search-panel');
                if (searchPanel) searchPanel.style.display = 'block';
                document.getElementById('viewScout')?.classList.remove('studio-mode');
                document.body.classList.remove('scout-studio-active');
            });
        }

        // Chapter selector dropdown
        const chapSelect = document.getElementById('scoutChapterSelect');
        if (chapSelect) {
            chapSelect.addEventListener('change', (e) => {
                const sec = parseFloat(e.target.value);
                if (!isNaN(sec)) {
                    this.seekVideoTo(sec);
                }
            });
        }

        // Board Playback & Navigation buttons
        document.getElementById('btnScoutUndoMove')?.addEventListener('click', () => this.undoUserMove());
        document.getElementById('btnScoutStart')?.addEventListener('click', () => this.firstStep());
        document.getElementById('btnScoutPrev')?.addEventListener('click', () => this.prevStep());
        document.getElementById('btnScoutNext')?.addEventListener('click', () => this.nextStep());
        document.getElementById('btnScoutEnd')?.addEventListener('click', () => this.lastStep());
        document.getElementById('btnScoutResetBoard')?.addEventListener('click', () => this.resetBoard());
        document.getElementById('btnScoutFlip')?.addEventListener('click', () => this.flipBoard());

        // Keyboard navigation when scout tab is visible
        // CODE-22: guard every focusable form control (`e.target`, not
        // `document.activeElement`) and never swallow a browser/OS shortcut such
        // as Ctrl/Cmd+Z.
        window.addEventListener('keydown', (e) => {
            const scoutView = document.getElementById('viewScout');
            if (!scoutView || !scoutView.classList.contains('active')) return;
            const tag = (e.target && e.target.tagName) ? String(e.target.tagName).toLowerCase() : '';
            if (tag === 'input' || tag === 'textarea' || tag === 'select' || (e.target && e.target.isContentEditable)) return;
            if (e.ctrlKey || e.metaKey || e.altKey) return;   // leave browser shortcuts alone
            if (e.repeat && e.key === 'u') return;             // no key-repeat spam

            if (e.key === 'ArrowLeft' || e.key === 'h') {
                e.preventDefault();
                this.prevStep();
            } else if (e.key === 'ArrowRight' || e.key === 'l') {
                e.preventDefault();
                this.nextStep();
            } else if (e.key === 'Home' || e.key === 'ArrowUp' || e.key === 'k') {
                e.preventDefault();
                this.firstStep();
            } else if (e.key === 'End' || e.key === 'ArrowDown' || e.key === 'j') {
                e.preventDefault();
                this.lastStep();
            } else if (e.key === 'u' && !e.ctrlKey && !e.metaKey) {
                e.preventDefault();
                this.undoUserMove();
            }
        });

        // Quick add / edit whole line to repertoire (Under Board)
        document.getElementById('btnScoutAddMoveWhite')?.addEventListener('click', () => this.importToRepertoire('white'));
        document.getElementById('btnScoutAddMoveBlack')?.addEventListener('click', () => this.importToRepertoire('black'));
        document.getElementById('btnScoutMarkImportant')?.addEventListener('click', () => this.markLastMoveAsImportant());
        document.getElementById('btnScoutRemoveFromRep')?.addEventListener('click', () => this.removeCurrentLineFromRepertoire());
        document.getElementById('btnScoutViewTree')?.addEventListener('click', () => this.showTreeTab());

        // Explored Lines Tree header actions
        document.getElementById('btnImportAllWhite')?.addEventListener('click', () => this.importAllLinesToRepertoire('white'));
        document.getElementById('btnImportAllBlack')?.addEventListener('click', () => this.importAllLinesToRepertoire('black'));
        document.getElementById('btnClearTree')?.addEventListener('click', () => this.clearSessionTree());

        // Train this opening in Repertoire Trainer
        document.getElementById('btnScoutTrainOpening')?.addEventListener('click', () => this.trainThisOpening());
        document.getElementById('btnScoutTrainAfterImport')?.addEventListener('click', () => this.trainThisOpening());

        // Scout Study selector and new study button
        const scoutStudySelect = document.getElementById('scoutStudySelect');
        if (scoutStudySelect) {
            scoutStudySelect.addEventListener('change', (e) => this.onStudyChange(e.target.value));
        }
        const btnScoutNewStudy = document.getElementById('btnScoutNewStudy');
        if (btnScoutNewStudy) {
            btnScoutNewStudy.addEventListener('click', () => this.onNewStudyClick());
        }

        // Bookmark / Favorite
        document.getElementById('btnScoutFav')?.addEventListener('click', () => this.toggleFavorite());

        // Move note input (auto-saves on input debounce and on blur)
        const scoutMoveComment = document.getElementById('scoutMoveComment');
        if (scoutMoveComment) {
            let debounceTimer = null;
            scoutMoveComment.addEventListener('input', () => {
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(() => {
                    this.saveMoveNote(scoutMoveComment.value);
                }, 400);
            });
            scoutMoveComment.addEventListener('blur', () => {
                clearTimeout(debounceTimer);
                this.saveMoveNote(scoutMoveComment.value);
            });
        }
    }

    /**
     * The two intent axes, as real controls rather than as a guess.
     *
     * Both are segmented groups with a fixed height and a fixed button width, so
     * switching between "White" and "Black" cannot reflow the row (AGENTS.md
     * §8.4).  The `auto` option exists and is the default: it is what tells the
     * server to infer from the query, and it is why the Scout usually gets the
     * intent right without the user touching anything.
     */
    _bindIntentControls() {
        const wire = (attr, apply) => {
            const key = `intent${attr.charAt(0).toUpperCase()}${attr.slice(1)}`;
            document.querySelectorAll(`[data-intent-${attr}]`).forEach(btn => {
                btn.addEventListener('click', () => {
                    document.querySelectorAll(`[data-intent-${attr}]`)
                        .forEach(b => b.classList.toggle('active', b === btn));
                    apply(btn.dataset[key] || '');
                });
            });
        };
        wire('side', (value) => { this.intentSide = value; this._rerunDiscovery(); });
        wire('mode', (value) => { this.intentMode = value; this._rerunDiscovery(); });
        wire('sort', (value) => { this.sortMode = value; this.renderResults(); });

        // A disambiguation chip is the answer to "which Alapin?" - clicking it
        // re-runs the same search with the opening pinned.
        document.getElementById('scoutCandidateChips')?.addEventListener('click', (e) => {
            const chip = e.target.closest('.scout-candidate-chip');
            if (!chip) return;
            const eco = chip.dataset.candidateEco;
            if (!eco) return;
            this.chosenEco = (this.chosenEco === eco) ? '' : eco;
            this._rerunDiscovery();
        });

        // Bookmark straight from a card: no studio round-trip, because the
        // shelf is where the decision is made.
        document.getElementById('scoutResultsGrid')?.addEventListener('click', (e) => {
            const fav = e.target.closest('.scout-fav-toggle');
            if (!fav) return;
            e.stopPropagation();
            this.toggleFavorite(fav.dataset.favId);
        });
    }

    /**
     * Autocomplete over the local encyclopedia: instant, offline, and the only
     * place the ambiguous names (Alapin, Albin, Larsen) become decidable before
     * the search is even sent.
     */
    _bindSuggest() {
        const input = document.getElementById('scoutSearchInput');
        const box = document.getElementById('scoutSuggestBox');
        if (!input || !box) return;

        const close = () => {
            box.innerHTML = '';
            box.classList.add('is-hidden');
        };

        input.addEventListener('input', () => {
            if (this.suggestTimer) clearTimeout(this.suggestTimer);
            const q = input.value.trim();
            if (q.length < 2) { close(); return; }
            this.suggestTimer = setTimeout(() => this._fetchSuggest(q, close), 180);
        });
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' || e.key === 'Enter') close();
        });
        box.addEventListener('click', (e) => {
            const item = e.target.closest('.scout-suggest-item');
            if (!item) return;
            input.value = item.dataset.suggestName || '';
            close();
            this.search(input.value.trim());
        });
        document.addEventListener('click', (e) => {
            if (!box.contains(e.target) && e.target !== input) close();
        });
    }

    async _fetchSuggest(q, close) {
        const seq = ++this.suggestSeq;
        const box = document.getElementById('scoutSuggestBox');
        try {
            const res = await fetch(`/api/scout/suggest?q=${encodeURIComponent(q)}&limit=6`);
            const data = await res.json();
            if (seq !== this.suggestSeq || !box) return;
            const items = Array.isArray(data) ? data : [];
            if (!items.length) { close(); return; }
            // `rawHtml(safeHtml`…`)` is the idiom for "escaped, but spliced into a
            // larger string": `safeHtml` escapes the holes, `rawHtml` marks the
            // result as trusted so the outer template does not escape it again.
            box.innerHTML = items.map(it => {
                const side = it.side === 'white' ? '⚪' : (it.side === 'black' ? '⚫' : '?');
                const moves = (Array.isArray(it.opening_moves) ? it.opening_moves : []).join(' ');
                return safeHtml`<div class="scout-suggest-item" data-suggest-name="${it.name || ''}">`
                     + rawHtml(safeHtml`<span class="scout-suggest-eco">${it.eco || ''}</span>`)
                     + rawHtml(safeHtml`<span class="scout-suggest-side">${side}</span>`)
                     + rawHtml(safeHtml`<span class="scout-suggest-name">${it.label || it.name || ''}</span>`)
                     + rawHtml(safeHtml`<span class="scout-suggest-moves">${moves}</span>`)
                     + '</div>';
            }).join('');
            box.classList.remove('is-hidden');
        } catch (_) {
            close();
        }
    }

    /** Re-run the current query after an intent/candidate change. */
    _rerunDiscovery() {
        const q = document.getElementById('scoutSearchInput')?.value.trim();
        if (q) this.search(q);
    }

    /**
     * CODE-05: a monotonically increasing sequence number plus an
     * `AbortController` - a slow earlier search can never overwrite a fast later
     * one (the previous code assigned `cachedResults` unconditionally).
     *
     * It now also resolves the *intent* before searching, and paints twice: the
     * first response carries everything the flat search knew, the second fills in
     * chapters, upload dates and the side of anything the first could not
     * classify.  The cards are sized for their final content, so the second paint
     * is 0px of layout shift.
     */
    async search(query) {
        const seq = ++this.searchSeq;
        const loading = document.getElementById('scoutLoadingIndicator');
        const loadingText = document.getElementById('scoutLoadingText');
        const grid = document.getElementById('scoutResultsGrid');
        if (loading) loading.style.display = 'flex';
        if (loadingText) loadingText.textContent = `Working out what "${String(query || '').slice(0, 60)}" means...`;
        if (grid) grid.innerHTML = '';

        const params = new URLSearchParams({ query: String(query || ''), limit: '12' });
        if (this.intentSide) params.set('side', this.intentSide);
        if (this.intentMode) params.set('mode', this.intentMode);
        if (this.chosenEco) params.set('chosen_eco', this.chosenEco);

        const controller = new AbortController();
        this.searchController = controller;
        try {
            const res = await fetch(`/api/scout/discover?${params.toString()}`, { signal: controller.signal });
            const data = await res.json();
            if (seq !== this.searchSeq) return;   // superseded by a newer search
            if (!res.ok) throw new Error(data && data.detail ? data.detail : `HTTP ${res.status}`);

            this.intent = data.intent || null;
            this.intentReplies = Array.isArray(data.replies) ? data.replies : [];
            this.coverage = data.coverage || null;
            this.ecoGuides = Array.isArray(data.eco_guides) ? data.eco_guides : [];
            this.needsEnrichment = Array.isArray(data.needs_enrichment) ? data.needs_enrichment : [];
            this.cachedResults = Array.isArray(data.results) ? data.results : [];
            this.renderIntentBar();
            this.renderCandidates();
            this.renderResults();

            if (this.needsEnrichment.length) {
                if (loadingText) loadingText.textContent = 'Reading chapters and descriptions...';
                this._enrich(this.needsEnrichment, seq);
            }
        } catch (e) {
            if (seq !== this.searchSeq || (e && e.name === 'AbortError')) return;
            console.error('Scout search failed:', e);
            if (grid) {
                // SEC-01: the error text is a server/network string - build the
                // node with textContent instead of interpolating it into HTML.
                grid.innerHTML = '';
                const box = document.createElement('div');
                box.style.cssText = 'grid-column: 1/-1; text-align:center; padding:2rem; color:var(--text-muted);';
                box.textContent = `Failed to load scout results: ${e && e.message ? e.message : 'unknown error'}`;
                grid.appendChild(box);
            }
        } finally {
            if (seq === this.searchSeq && loading) loading.style.display = 'none';
        }
    }

    /** The second paint. Guarded by its own generation counter. */
    async _enrich(ids, searchSeqAtStart) {
        const seq = ++this.enrichSeq;
        if (searchSeqAtStart !== this.searchSeq) return;
        try {
            const res = await fetch('/api/scout/enrich', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ids: ids.slice(0, 8), named_eco: this.chosenEco || '' })
            });
            const data = await res.json();
            if (seq !== this.enrichSeq) return;
            const meta = (data && data.meta) || {};
            const byId = new Map(this.cachedResults.map(r => [r.id, r]));
            let touched = false;
            for (const entry of Object.entries(meta)) {
                const target = byId.get(entry[0]);
                if (!target) continue;
                target.description = entry[1].description || target.description;
                target.chapters = Array.isArray(entry[1].chapters) ? entry[1].chapters : [];
                target.chapter_count = _scoutNum(entry[1].chapter_count, 0);
                target.upload_date = entry[1].upload_date || '';
                if (_scoutNum(entry[1].view_count, 0)) target.view_count = _scoutNum(entry[1].view_count, 0);
                if (entry[1].duration) target.duration = _scoutNum(entry[1].duration, 0);
                target.side_teach = entry[1].side_teach || null;
                target.mode_teach = entry[1].mode_teach || null;
                target.classify_confidence = entry[1].classify_confidence || 'none';
                target.classify_evidence = entry[1].classify_evidence || '';
                target.enriched = true;
                touched = true;
            }
            if (touched) {
                this.needsEnrichment = [];
                this._rescore();
                this.renderResults();
            }
        } catch (e) {
            // Enrichment is cosmetic: a failure leaves the first paint intact.
            console.warn('Scout enrichment failed:', e);
        }
    }

    /** Re-rank locally after enrichment taught us what a video actually is. */
    _rescore() {
        const intent = this.intent || {};
        this.cachedResults.forEach(item => {
            const modeMatch = intent.mode && intent.mode !== 'either' && item.mode_teach === intent.mode;
            const sideMatch = intent.side && item.side_teach === intent.side;
            if (modeMatch) item.intent_score = _scoutNum(item.intent_score) + 35;
            if (sideMatch) item.intent_score = _scoutNum(item.intent_score) + 40;
            else if (item.side_teach) item.intent_score = _scoutNum(item.intent_score) - 25;
            item.bucket = item.intent_score >= 55 ? 'exact' : (item.intent_score >= 25 ? 'related' : 'course');
        });
    }

    /** The always-in-flow statement of what this shelf is answering. */
    renderIntentBar() {
        const el = document.getElementById('scoutIntentBar');
        if (!el) return;
        const coverage = this.coverage || {};
        const text = scoutIntentSummary(this.intent, {
            count: this.cachedResults.length,
            replies: this.intentReplies.length,
            // §4.G's count: a branch you hold but cannot answer is a gap too, so
            // the number here is what the user actually has to work on.
            gaps: _scoutNum(coverage.gaps, 0)
        });
        el.textContent = text;
        el.title = coverage.summary ? `${text} — ${coverage.summary}` : text;  // §8.2
    }

    /** The disambiguation chips. Always rendered; the empty state is a hint, not a hole. */
    renderCandidates() {
        const box = document.getElementById('scoutCandidateChips');
        const label = document.getElementById('scoutCandidateLabel');
        if (!box) return;
        const intent = this.intent || {};
        if (!intent.needs_disambiguation) {
            box.innerHTML = '';
            if (label) label.textContent = '';
            return;
        }
        const candidates = Array.isArray(intent.candidates) ? intent.candidates : [];
        if (label) {
            label.textContent = `"${intent.query || ''}" matches ${candidates.length} openings — pick one:`;
        }
        box.innerHTML = candidates.map(scoutCandidateChipMarkup).join('');
    }

    /** CODE-16: this was dead code, so `GET /api/scout/recent` had no caller. */
    async loadRecent() {
        const seq = ++this.searchSeq;
        const grid = document.getElementById('scoutResultsGrid');
        try {
            const res = await fetch('/api/scout/recent?limit=20');
            const data = await res.json();
            if (seq !== this.searchSeq) return;
            this.cachedResults = (Array.isArray(data) ? data : []).map(item => ({
                source_type: item.source_type,
                id: item.source_id,
                url: item.source_type === 'youtube' ? `https://www.youtube.com/watch?v=${item.source_id}` : '',
                title: item.title,
                channel: item.channel,
                duration: item.duration,
                duration_formatted: item.duration ? `${Math.floor(item.duration / 60)}m` : '',
                thumbnail: item.thumbnail_url || SCOUT_IMAGE_FALLBACK,
                eco: item.eco,
                opening_name: item.opening_name,
                is_favorite: !!item.is_favorite
            }));
            this.renderResults();
        } catch (e) {
            if (e && e.name === 'AbortError') return;
            console.error('Failed to load recent scout content:', e);
            if (grid) {
                grid.innerHTML = '';
                const box = document.createElement('div');
                box.style.cssText = 'grid-column: 1/-1; text-align:center; padding:2rem; color:var(--text-muted);';
                box.textContent = 'Failed to load recent scout history.';
                grid.appendChild(box);
            }
        }
    }

    /**
     * CODE-16: the filter vocabulary is validated against the values the shelf
     * buttons actually carry (`all` / `youtube` / `eco_guide` / `recent`, plus
     * `favorites` once the HTML gains that button).
     */
    _filterResults() {
        const all = Array.isArray(this.cachedResults) ? this.cachedResults : [];
        switch (this.activeFilter) {
            case 'youtube':
                return all.filter(r => r && r.source_type === 'youtube');
            case 'eco_guide':
                return all.filter(r => r && (r.source_type === 'eco_guide' || r.source_type === 'lichess_study'));
            case 'favorites':
                return all.filter(r => r && r.is_favorite);
            case 'recent':
            case 'all':
            default:
                return all;
        }
    }

    renderResults() {
        const grid = document.getElementById('scoutResultsGrid');
        if (!grid) return;

        // Total over its own state: the node contract harness builds a Scout with
        // `Object.create(prototype)` and a hand-written field list, so these are
        // genuinely absent in that environment and a render must not depend on
        // the constructor having run.
        if (!Array.isArray(this.ecoGuides)) this.ecoGuides = [];
        if (!Array.isArray(this.intentReplies)) this.intentReplies = [];
        if (!this.coverage) this.coverage = null;
        if (!this.intent) this.intent = null;
        if (!this.sortMode) this.sortMode = 'match';

        const filtered = this._filterResults();

        // CODE-10: `#scoutResultsCount` existed in the HTML but was never written.
        const countEl = document.getElementById('scoutResultsCount');
        if (countEl) countEl.textContent = filtered.length;

        if (!filtered || filtered.length === 0) {
            grid.innerHTML = `
                <div style="grid-column: 1/-1; text-align:center; padding:3rem 1rem; color:var(--text-muted);">
                    <div style="font-size:2.5rem; margin-bottom:0.5rem;">🔍</div>
                    <div style="font-size:1.1rem; font-weight:600; color:var(--text-primary);">No material found</div>
                    <div style="font-size:0.85rem; margin-top:0.3rem;">Try another opening name, widen the side/mode controls, or paste a YouTube / PGN URL above.</div>
                </div>
            `;
            return;
        }

        // SEC-01: every remote field is escaped by the markup functions; the
        // inline `onerror` attribute is gone and replaced by a real listener.
        const guides = (this.activeFilter === 'all' || this.activeFilter === 'eco_guide')
            ? this.ecoGuides.slice(0, 3) : [];

        let html = '';
        const plan = this._planPanelMarkup();
        if (plan) html += plan;

        if (guides.length) {
            html += safeHtml`<div class="scout-group-title">📖 Theory from the encyclopedia</div>`;
            html += safeHtml`<div class="scout-cards-grid scout-cards-grid--nested">${rawHtml(guides.map(scoutEcoGuideMarkup).join(''))}</div>`;
        }

        if (this.activeFilter === 'all' && this.intent && this.intentReplies.length) {
            html += this._repliesStripMarkup();
        }

        // Grouped by how well the video answers the question, so the exact
        // matches are not interleaved with a 3-hour course that happens to
        // share a word.  The user's chosen sort overrides the bucket order.
        const buckets = [
            { key: 'exact', title: '🎯 Answers your question' },
            { key: 'related', title: '🔗 Related' },
            { key: 'course', title: '📚 Wider courses & background' }
        ];
        if (this.sortMode === 'match') {
            for (const bucket of buckets) {
                const items = filtered.filter(r => (r.bucket || 'course') === bucket.key);
                if (!items.length) continue;
                html += safeHtml`<div class="scout-group-title">${bucket.title}</div>`;
                html += safeHtml`<div class="scout-cards-grid scout-cards-grid--nested">${rawHtml(items.map(scoutResultCardMarkup).join(''))}</div>`;
            }
        } else {
            html += safeHtml`<div class="scout-group-title">${filtered.length} results · sorted by ${this.sortMode}</div>`;
            html += safeHtml`<div class="scout-cards-grid scout-cards-grid--nested">${rawHtml(scoutSortResults(filtered, this.sortMode).map(scoutResultCardMarkup).join(''))}</div>`;
        }

        grid.innerHTML = html;
        this._wireResultCards(grid);
    }

    /**
     * The critical-reply strip: "which replies must I prepare", read out of the
     * encyclopedia rather than out of a video title.  It is the one part of the
     * shelf that is true regardless of what YouTube has published this week.
     */
    _repliesStripMarkup() {
        const coverage = this.coverage;
        const replies = this.intentReplies || [];
        if (!replies.length || !coverage) return '';
        const side = (this.intent && this.intent.side) || '';
        const sideWord = side === 'white' ? 'White' : (side === 'black' ? 'Black' : 'you');
        const header = coverage.total
            ? `🎯 The ${coverage.total} replies ${sideWord} must prepare to face this`
            : `🎯 Replies ${sideWord} must prepare to face this`;
        const rows = (coverage.rows || []).slice(0, 14)
            .map(r => scoutCoverageRowMarkup(r, side)).join('');
        return safeHtml`<div class="scout-replies" data-state="${coverage.state || 'unknown'}">`
             + rawHtml(safeHtml`<div class="scout-group-title">${header}</div>`)
             + safeHtml`<div class="scout-coverage-summary">${coverage.summary || ''}</div>`
             + safeHtml`<div class="scout-coverage-rows">${rawHtml(rows)}</div></div>`;
    }

    /**
     * The strategic plan for the opening, shown above the results — but only when
     * we actually resolved an opening and have notes for it.
     */
    _planPanelMarkup() {
        // The *chosen* opening, not the first guide. After a disambiguation the
        // shelf deliberately shows several openings, and the panel must describe
        // the one the user picked -- for "Alapin" the first candidate is the Ruy
        // Lopez, so using the first guide would explain the wrong opening.
        const primary = (this.intent && this.intent.primary) || {};
        const guide = (this.ecoGuides || []).find(g => g.eco === primary.eco);
        const data = (guide && guide.strategic_ideas) || primary.strategic_ideas || null;
        if (!data) return '';
        const side = (this.intent && this.intent.side) || '';
        const mode = (this.intent && this.intent.mode) || 'play';
        // Facing the opening means reading the side that plays it.
        const viewSide = (mode === 'against' && side) ? (side === 'black' ? 'white' : 'black') : side;
        const who = viewSide === 'black' ? 'Black' : 'White';
        const title = mode === 'against'
            ? `What ${who} (your opponent) is trying to do here`
            : `What ${who} is trying to do here`;
        return scoutPlanPanelMarkup(data, viewSide, title);
    }

    /**
     * Hand off to the Repertoire Builder at the position where a gap is. The
     * Builder is the surface that can actually close a gap, so this is a
     * navigation, not a write.
     */
    _openGapInBuilder(fen, uci) {
        if (window.app && typeof window.app.showTab === 'function') {
            window.app.showTab('repertoire');
        }
        const builder = window.repertoireBuilder;
        if (builder) {
            try {
                const targetSide = (this.intent && (this.intent.side === 'white' || this.intent.side === 'black'))
                    ? this.intent.side
                    : builder.activeColor;
                const jumpToGap = () => {
                    if (fen && uci && typeof builder.addressGap === 'function') {
                        builder.addressGap({ parent_fen: fen, opponent_uci: uci });
                    }
                };
                if (targetSide && typeof builder.loadRepertoire === 'function' && builder.activeColor !== targetSide) {
                    Promise.resolve(builder.loadRepertoire(targetSide)).then(jumpToGap).catch(() => jumpToGap());
                } else {
                    jumpToGap();
                }
            } catch (e) {
                console.warn('Could not load the repertoire in the Builder:', e);
            }
        }
        // §8.1: a notice about an action goes through the toast system.
        if (window.showToast) {
            window.showToast(`Opened the Builder for ${uci || 'this reply'}`, '#38bdf8', 2800);
        }
    }

    _wireResultCards(grid) {
        // A gap row hands off to the Builder; the card click below must not also
        // fire, or one click opens a studio and a tree at once.
        grid.querySelectorAll('.scout-coverage-action').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                this._openGapInBuilder(btn.dataset.coverageFen, btn.dataset.coverageUci);
            });
        });
        grid.querySelectorAll('img.scout-card-thumb').forEach(img => {
            img.addEventListener('error', () => {
                const fallback = img.getAttribute('data-fallback-src') || SCOUT_IMAGE_FALLBACK;
                if (img.getAttribute('src') !== fallback) img.setAttribute('src', fallback);
            });
        });
        grid.querySelectorAll('.scout-card').forEach(card => {
            card.addEventListener('click', () => {
                const sId = card.dataset.sourceId;
                const sType = card.dataset.sourceType;
                let sourceParam = sId;
                if (sType === 'youtube') {
                    sourceParam = `https://www.youtube.com/watch?v=${sId}`;
                }
                this.analyzeSource(sourceParam);
            });
        });
    }

    /**
     * Bookmark from the shelf or from the Studio header. The server's answer is
     * applied to both the active study and the cached shelf results so the ★
     * state can never drift from what is stored.
     */
    async toggleFavorite(sourceId = null) {
        const targetId = sourceId
            || (this.activeStudy && (this.activeStudy.source_id || this.activeStudy.video_id || this.activeStudy.id))
            || null;
        if (!targetId) return;
        const cardItem = (this.cachedResults || []).find(r => r && r.id === targetId)
            || (this.ecoGuides || []).find(r => r && r.id === targetId)
            || (this.activeStudy && (this.activeStudy.source_id === targetId || this.activeStudy.video_id === targetId) ? this.activeStudy : null);
        const payload = { source_id: targetId };
        if (cardItem) {
            if (cardItem.source_type) payload.source_type = cardItem.source_type;
            if (cardItem.title) payload.title = cardItem.title;
            if (cardItem.channel) payload.channel = cardItem.channel;
            if (cardItem.opening_name) payload.opening_name = cardItem.opening_name;
            if (cardItem.eco) payload.eco = cardItem.eco;
            if (cardItem.thumbnail_url || cardItem.thumbnail) payload.thumbnail_url = cardItem.thumbnail_url || cardItem.thumbnail;
            if (cardItem.duration || cardItem.duration_seconds) payload.duration = cardItem.duration || cardItem.duration_seconds;
        }
        try {
            const res = await fetch('/api/scout/favorite', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data && data.detail ? data.detail : `HTTP ${res.status}`);
            const on = !!data.is_favorite;
            if (Array.isArray(this.cachedResults)) {
                this.cachedResults.forEach(r => { if (r && r.id === targetId) r.is_favorite = on; });
            }
            if (Array.isArray(this.ecoGuides)) {
                this.ecoGuides.forEach(r => { if (r && r.id === targetId) r.is_favorite = on; });
            }
            if (this.activeStudy && (this.activeStudy.source_id === targetId || this.activeStudy.video_id === targetId)) {
                this.activeStudy.is_favorite = on;
            }
            const btn = document.getElementById('btnScoutFav');
            if (btn) {
                btn.textContent = on ? '★ Favorited' : '☆ Favorite';
                btn.style.color = on ? '#fbbf24' : '';
            }
            this.renderResults();
            if (window.showToast) {
                window.showToast(on ? '★ Bookmarked' : 'Bookmark removed', on ? '#4ade80' : '#94a3b8', 2200);
            }
        } catch (e) {
            if (window.showToast) {
                window.showToast(`Could not update bookmark: ${e && e.message ? e.message : 'error'}`, '#ef4444', 3200);
            }
        }
    }

    async analyzeSource(source) {
        // CODE-05: two quick card clicks produced two analyses and the slower
        // one won. Only the newest request may open the studio.
        const seq = ++this.analyzeSeq;
        const loading = document.getElementById('scoutLoadingIndicator');
        const loadingText = document.getElementById('scoutLoadingText');
        if (loading) loading.style.display = 'flex';
        if (loadingText) {
            loadingText.textContent = String(source).includes('youtube.com')
                ? 'Reading the video description and chapters...'
                : 'Analysing the pasted PGN / study...';
        }

        try {
            const res = await fetch('/api/scout/analyze', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ source: source })
            });

            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `Analysis failed (HTTP ${res.status})`);
            }

            const study = await res.json();
            if (seq !== this.analyzeSeq) return;
            this.openStudio(study);
        } catch (e) {
            if (seq !== this.analyzeSeq) return;
            console.error('Error analyzing scout source:', e);
            this._showQuickAddNotice(`Could not open source: ${e.message}`, '#ef4444');
        } finally {
            if (seq === this.analyzeSeq && loading) loading.style.display = 'none';
        }
    }

    openStudio(study) {
        this.activeStudy = study;
        this.loadStudies();

        // Switch view from results to studio
        const searchPanel = document.querySelector('.scout-search-panel');
        if (searchPanel) searchPanel.style.display = 'none';
        const searchSec = document.getElementById('scoutResultsSection');
        const studioSec = document.getElementById('scoutStudio');
        if (searchSec) searchSec.style.display = 'none';
        if (studioSec) studioSec.style.display = 'flex';
        document.getElementById('viewScout')?.classList.add('studio-mode');
        document.body.classList.add('scout-studio-active');

        // Populate top bar metadata
        const titleEl = document.getElementById('scoutStudioTitle');
        if (titleEl) titleEl.textContent = study.title || 'Opening Studio';
        const chanEl = document.getElementById('scoutStudioChannel');
        if (chanEl) chanEl.textContent = study.channel || 'Chess Study';
        const durEl = document.getElementById('scoutStudioDuration');
        if (durEl) durEl.textContent = study.duration_formatted || '';
        const ecoEl = document.getElementById('scoutStudioEco');
        if (ecoEl) {
            ecoEl.textContent = study.eco || 'A00';
            ecoEl.style.display = study.eco ? 'inline-block' : 'none';
        }
        const nameEl = document.getElementById('scoutStudioOpeningName');
        if (nameEl) nameEl.textContent = study.opening_name || '';

        // YouTube link
        const ytLink = document.getElementById('btnScoutOpenYoutube');
        if (ytLink) {
            if (study.source_type === 'youtube' && study.video_id) {
                ytLink.href = `https://www.youtube.com/watch?v=${study.video_id}`;
                ytLink.style.display = 'inline-flex';
            } else {
                ytLink.style.display = 'none';
            }
        }

        // Chapters dropdown
        const chapSelect = document.getElementById('scoutChapterSelect');
        const chapterCount = (study.chapters || []).length;
        if (chapSelect) {
            chapSelect.innerHTML = '<option value="">Jump to Chapter...</option>';
            (study.chapters || []).forEach(ch => {
                const opt = document.createElement('option');
                opt.value = ch.start_time;
                opt.textContent = `${ch.start_formatted} - ${ch.title}`;
                chapSelect.appendChild(opt);
            });
        }

        // CODE-10: the bookmark button was never initialised from the study.
        const favBtn = document.getElementById('btnScoutFav');
        if (favBtn) {
            favBtn.textContent = study.is_favorite ? '★ Favorited' : '☆ Favorite';
            favBtn.style.color = study.is_favorite ? '#fbbf24' : '';
        }

        // CODE-10: uci -> timestamp index for the board <-> player sync.
        this.moveTimestamps = {};
        (study.variations || []).forEach(v => (v.moves || []).forEach(m => {
            if (m && m.uci && typeof m.timestamp === 'number') this.moveTimestamps[m.uci] = m.timestamp;
        }));
        this._resetVideoSyncUi(chapSelect, chapterCount);

        // Setup YouTube Player or non-video placeholder
        const playerWrap = document.getElementById('scoutYoutubePlayer');
        const nonVidPanel = document.getElementById('scoutNonVideoPlaceholder');
        if (study.source_type === 'youtube' && study.video_id) {
            if (nonVidPanel) nonVidPanel.style.display = 'none';
            if (playerWrap) playerWrap.style.display = 'block';
            this.setupYouTubePlayer(study.video_id);
        } else {
            if (playerWrap) playerWrap.style.display = 'none';
            if (nonVidPanel) {
                nonVidPanel.style.display = 'flex';
                const t = document.getElementById('scoutNonVideoTitle');
                if (t) t.textContent = study.title;
            }
        }

        // Reset and seed session line tree
        this.lineTree.reset();
        if (study.variations && study.variations.length > 0) {
            for (const v of study.variations) {
                if (v.moves && v.moves.length > 0) {
                    this.lineTree.currentNode = this.lineTree.root;
                    for (const m of v.moves) {
                        this.lineTree.addMove(m);
                    }
                }
            }
        }
        this.lineTree.currentNode = this.lineTree.root;

        // Reset board to starting position
        if (this.board) {
            this.board.loadFEN(this.lineTree.rootFen);
            this.board.lastMove = null;
            this.board.render();
        }
        this.lastBoardFen = this.lineTree.rootFen;

        this._updateBoardLabels();
        this.streamEvaluation(this.lineTree.rootFen);
        this.updateActiveLineCounts();
        this.renderTreeLines();
    }

    // ---------------------------------------------------------------------
    //  CODE-10 / CODE-11 - YouTube <-> board synchronisation
    //  The player used to expose exactly one event (`onReady`, a boolean), so
    //  `#scoutCurrentTimeDisplay` / `#scoutLiveIndicator` were dead markup and
    //  chapter seeking died for the whole session whenever `window.YT` was not
    //  ready on the first open (one-shot capability check, no retry).
    // ---------------------------------------------------------------------

    _resetVideoSyncUi(chapSelect, chapterCount) {
        if (this.ytSyncTimer) {
            clearInterval(this.ytSyncTimer);
            this.ytSyncTimer = null;
        }
        this._videoSyncGuard = false;
        this._lastSyncedUci = null;
        const timeEl = document.getElementById('scoutCurrentTimeDisplay');
        if (timeEl) {
            timeEl.textContent = '00:00 / 00:00';
            timeEl.title = '';
        }
        const liveEl = document.getElementById('scoutLiveIndicator');
        if (liveEl) {
            liveEl.classList.remove('live');
            liveEl.classList.add('idle');
            liveEl.title = 'Video not playing';
        }
        if (chapSelect) {
            // Never leave a control that silently does nothing.
            chapSelect.disabled = true;
            chapSelect.title = (chapterCount > 0)
                ? 'Video player not ready yet - seeking will be retried automatically'
                : 'This study has no chapters';
        }
    }

    _updateVideoSyncUi() {
        const player = this.ytPlayer;
        if (!player || !this.ytPlayerReady) return;
        let current = 0;
        let duration = 0;
        try {
            if (typeof player.getCurrentTime === 'function') current = player.getCurrentTime() || 0;
            if (typeof player.getDuration === 'function') duration = player.getDuration() || 0;
        } catch (_) {
            return;
        }
        const timeEl = document.getElementById('scoutCurrentTimeDisplay');
        if (timeEl) {
            timeEl.textContent = `${formatClock(current)} / ${formatClock(duration)}`;
            timeEl.title = `${formatClock(current)} of ${formatClock(duration)}`;
        }
        // Video -> board: step the board to the last study move already spoken.
        if (!this._videoSyncGuard) this._syncBoardToVideoTime(current);
    }

    _startVideoSyncPolling() {
        if (this.ytSyncTimer) return;
        this.ytSyncTimer = setInterval(() => this._updateVideoSyncUi(), 500);
    }

    _stopVideoSyncPolling() {
        if (this.ytSyncTimer) {
            clearInterval(this.ytSyncTimer);
            this.ytSyncTimer = null;
        }
    }

    _onYtStateChange(event) {
        const liveEl = document.getElementById('scoutLiveIndicator');
        const state = (event && typeof event.data === 'number') ? event.data : (event && event.data);
        if (state === 1) {          // YT.PlayerState.PLAYING
            if (liveEl) {
                liveEl.classList.remove('idle');
                liveEl.classList.add('live');
                liveEl.title = 'Video playing';
            }
            this._startVideoSyncPolling();
        } else {
            if (liveEl) {
                liveEl.classList.remove('live');
                liveEl.classList.add('idle');
                liveEl.title = state === 0 ? 'Video ended' : 'Video paused';
            }
            if (state === 0) this._stopVideoSyncPolling();
        }
    }

    /** Board -> video: seek the player to this move's timestamp, if known. */
    _syncVideoToMove(uci) {
        if (!uci || this._videoSyncGuard) return;
        const ts = this.moveTimestamps ? this.moveTimestamps[uci] : null;
        if (typeof ts !== 'number') return;
        this.seekVideoTo(ts, { syncBoard: false });
    }

    /** Video -> board: the deepest study move whose timestamp has passed. */
    _syncBoardToVideoTime(seconds) {
        const stamps = this.moveTimestamps || {};
        let bestUci = null;
        let bestTs = -1;
        Object.keys(stamps).forEach(uci => {
            const ts = stamps[uci];
            if (ts <= seconds && ts > bestTs) { bestTs = ts; bestUci = uci; }
        });
        if (!bestUci) return;
        if (this._lastSyncedUci === bestUci) return;
        this._lastSyncedUci = bestUci;
        // Highlight the move on the board when it is part of the active line.
        const activeLine = this.lineTree.getActiveLine();
        const isInLine = activeLine.some(m => m && m.uci === bestUci);
        const wrap = document.getElementById('scoutYoutubePlayer');
        if (wrap) {
            wrap.setAttribute('data-sync-uci', isInLine ? bestUci : '');
            wrap.title = isInLine ? `Board move ${bestUci} @ ${formatClock(bestTs)}` : '';
        }
        // Step the board to the node matching bestUci if found in lineTree
        const findNodeByUci = (node) => {
            if (!node) return null;
            if (node.move && node.move.uci === bestUci) return node;
            for (const c of (node.children || [])) {
                const found = findNodeByUci(c);
                if (found) return found;
            }
            return null;
        };
        const targetNode = findNodeByUci(this.lineTree?.root);
        if (targetNode && this.lineTree.currentNode !== targetNode && this.board) {
            this.lineTree.currentNode = targetNode;
            this.board.loadFEN(targetNode.fen);
            this.lastBoardFen = targetNode.fen;
            this.board.lastMove = (targetNode.move && targetNode.move.uci && targetNode.move.uci.length >= 4)
                ? { from: targetNode.move.uci.substring(0, 2), to: targetNode.move.uci.substring(2, 4) }
                : null;
            this.board.render();
            this._updateBoardLabels();
            this.streamEvaluation(targetNode.fen);
            this.renderTreeLines();
        }
    }

    setupYouTubePlayer(videoId) {
        if (!videoId) return;
        this.ytVideoId = videoId;
        this.ytPlayerReady = false;
        this._lastSyncedUci = null;

        if (window.YT && window.YT.Player) {
            if (this.ytPlayer && typeof this.ytPlayer.loadVideoById === 'function') {
                // CODE-11: do NOT optimistically set `ytPlayerReady = true` here;
                // the player is still loading the new video, so a chapter chosen
                // in the meantime would seek inside the previous timeline.
                this.ytPlayer.loadVideoById(videoId);
            } else {
                this._createYouTubePlayer(videoId);
            }
        } else {
            // Fallback iframe + deferred upgrade (CODE-11).
            const playerEl = document.getElementById('scoutYoutubePlayer');
            if (playerEl) {
                playerEl.innerHTML = safeHtml`<iframe id="scoutIframeFallback" title="YouTube player" src="https://www.youtube.com/embed/${videoId}?enablejsapi=1" allowfullscreen></iframe>`;
            }
            this._scheduleYouTubeApiRetry(videoId);
        }
        this._refreshChapterSelectState();
    }

    _createYouTubePlayer(videoId) {
        const playerEl = document.getElementById('scoutYoutubePlayer');
        // Remove a leftover fallback iframe before handing the element to the API.
        if (playerEl) playerEl.innerHTML = '';
        const videoIdForPlayer = videoId || this.ytPendingVideoId;
        this.ytPendingVideoId = null;
        if (!window.YT || !window.YT.Player) {
            this._scheduleYouTubeApiRetry(videoIdForPlayer);
            return;
        }
        this.ytPlayer = new window.YT.Player('scoutYoutubePlayer', {
            videoId: videoIdForPlayer,
            playerVars: {
                autoplay: 0,
                enablejsapi: 1,
                rel: 0,
                modestbranding: 1
            },
            events: {
                onReady: (e) => {
                    this.ytPlayerReady = true;
                    this.ytVideoId = videoIdForPlayer;
                    this._refreshChapterSelectState();
                    this._updateVideoSyncUi();
                },
                onStateChange: (e) => this._onYtStateChange(e),
                onError: (e) => console.warn('YouTube player error', e)
            }
        });
    }

    /**
     * CODE-11: `<script src=".../iframe_api">` only *queues* the real API, so
     * `window.YT` may be missing long after the DOM is ready. Retry (bounded)
     * and upgrade the fallback iframe into a real player as soon as it appears.
     */
    _scheduleYouTubeApiRetry(videoId) {
        if (videoId) this.ytPendingVideoId = videoId;
        if (this.ytApiRetryTimer) return;
        let attempts = 0;
        const tryUpgrade = () => {
            attempts++;
            if (window.YT && window.YT.Player) {
                this.ytApiRetryTimer = null;
                const pending = this.ytPendingVideoId || videoId;
                this._createYouTubePlayer(pending);
                return;
            }
            if (attempts >= 40) {          // ~20s, then give up quietly
                this.ytApiRetryTimer = null;
                this._refreshChapterSelectState();
                return;
            }
            this.ytApiRetryTimer = setTimeout(tryUpgrade, 500);
        };
        this.ytApiRetryTimer = setTimeout(tryUpgrade, 500);
    }

    _refreshChapterSelectState() {
        const chapSelect = document.getElementById('scoutChapterSelect');
        if (!chapSelect) return;
        const optionCount = (chapSelect.options ? chapSelect.options.length : 0);
        chapSelect.disabled = !(this.ytPlayerReady && optionCount > 1);
        chapSelect.title = chapSelect.disabled
            ? (optionCount > 1 ? 'Video player not ready yet - retrying automatically' : 'This study has no chapters')
            : 'Jump the player to this chapter';
    }

    /**
     * Seek the player. `syncBoard` (default true) also walks the board to the
     * last study move at or before `seconds` - the two-way sync of §4E.
     */
    seekVideoTo(seconds, options = {}) {
        const value = Number(seconds);
        if (!Number.isFinite(value)) return false;
        if (!this.ytPlayer || !this.ytPlayerReady || typeof this.ytPlayer.seekTo !== 'function') {
            // CODE-11: no longer a silent no-op - retry the API once and tell the
            // user when seeking is genuinely unavailable.
            this._scheduleYouTubeApiRetry(this.ytVideoId);
            this._showQuickAddNotice('Video player is not ready yet - chapter seek will retry automatically.', '#f59e0b');
            return false;
        }
        try {
            this._videoSyncGuard = true;
            this.ytPlayer.seekTo(value, true);
            this._updateVideoSyncUi();
        } finally {
            this._videoSyncGuard = false;
        }
        if (options.syncBoard !== false) this._syncBoardToVideoTime(value);
        return true;
    }

    _onUserBoardMove(from, to, promo, moveRes) {
        // CODE-17: the board hands back a 6-field FEN while study-seeded nodes
        // carry the backend's 4-field one. Canonicalise on the way in so every
        // later comparison (tree identity, repertoire lookups) is stable.
        const fenBefore = normalizeTreeFen(this.lastBoardFen || this.lineTree.rootFen);
        const fenAfter = normalizeTreeFen(this.board.getFEN());
        this.lastBoardFen = fenAfter;

        const uci = from + to + (promo || '');
        const color = moveRes.color === 'w' ? 'white' : 'black';
        const activeLine = this.lineTree.getActiveLine();
        const moveNumber = Math.floor(activeLine.length / 2) + 1;

        let existingComment = '';
        let existingImportant = false;
        let existingArrows = '';
        if (window.repertoireBuilder && typeof window.repertoireBuilder.findMoveInTree === 'function') {
            const repMove = window.repertoireBuilder.findMoveInTree(fenBefore, uci);
            if (repMove) {
                existingComment = repMove.comment || '';
                existingImportant = Boolean(repMove.is_important);
                existingArrows = repMove.arrows || '';
            }
        }

        const moveObj = {
            fen_before: fenBefore,
            fen_after: fenAfter,
            uci: uci,
            san: moveRes.san,
            color: color,
            move_number: moveNumber,
            opening_name: this.activeStudy?.opening_name || '',
            eco: this.activeStudy?.eco || '',
            is_custom: true,
            comment: existingComment,
            is_important: existingImportant,
            arrows: existingArrows
        };

        // Add to session tree (branches automatically if user stepped back)
        this.lineTree.addMove(moveObj);

        // Update labels and tree
        this._updateBoardLabels();
        this.updateActiveLineCounts();
        this.renderTreeLines();

        // Stream real-time engine evaluation
        this.streamEvaluation(fenAfter);

        // CODE-10: keep the player in step with the board.
        this._syncVideoToMove(uci);
    }

    undoUserMove() {
        const prevNode = this.lineTree.stepBack();
        if (prevNode) {
            this.board.loadFEN(prevNode.fen);
            this.lastBoardFen = prevNode.fen;
            if (prevNode.move && prevNode.move.uci && prevNode.move.uci.length >= 4) {
                this.board.lastMove = {
                    from: prevNode.move.uci.substring(0, 2),
                    to: prevNode.move.uci.substring(2, 4)
                };
            } else {
                this.board.lastMove = null;
            }
            this.board.render();
            this._updateBoardLabels();
            this.streamEvaluation(prevNode.fen);
        } else {
            this.resetBoard();
        }

        this.updateActiveLineCounts();
        this.renderTreeLines();
    }

    firstStep() {
        this.lineTree.currentNode = this.lineTree.root;
        this.board.loadFEN(this.lineTree.rootFen);
        this.lastBoardFen = this.lineTree.rootFen;
        this.board.lastMove = null;
        this.board.render();
        this._updateBoardLabels();
        this.streamEvaluation(this.lineTree.rootFen);
        this.updateActiveLineCounts();
        this.renderTreeLines();
    }

    prevStep() {
        this.undoUserMove();
    }

    /**
     * CODE-17: "Next >" used to walk into `children[0]`, i.e. the branch that
     * happened to be created first - not the line the user is on. Playback now
     * follows the most recently created child (the user's own most recent
     * exploration), which is deterministic and matches what the tree shows last.
     */
    _preferredChild(node) {
        if (!node || !node.children || node.children.length === 0) return null;
        if (node.children.length === 1) return node.children[0];
        return node.children[node.children.length - 1];
    }

    nextStep() {
        const nextNode = this._preferredChild(this.lineTree.currentNode);
        if (nextNode) {
            this.lineTree.currentNode = nextNode;
            this.board.loadFEN(nextNode.fen);
            this.lastBoardFen = nextNode.fen;
            if (nextNode.move && nextNode.move.uci && nextNode.move.uci.length >= 4) {
                this.board.lastMove = {
                    from: nextNode.move.uci.substring(0, 2),
                    to: nextNode.move.uci.substring(2, 4)
                };
            }
            this.board.render();
            this._updateBoardLabels();
            this.streamEvaluation(nextNode.fen);
            this.updateActiveLineCounts();
            this.renderTreeLines();
        }
    }

    lastStep() {
        let next = this._preferredChild(this.lineTree.currentNode);
        while (next) {
            this.lineTree.currentNode = next;
            next = this._preferredChild(this.lineTree.currentNode);
        }
        const curr = this.lineTree.currentNode;
        this.board.loadFEN(curr.fen);
        this.lastBoardFen = curr.fen;
        if (curr.move && curr.move.uci && curr.move.uci.length >= 4) {
            this.board.lastMove = {
                from: curr.move.uci.substring(0, 2),
                to: curr.move.uci.substring(2, 4)
            };
        }
        this.board.render();
        this._updateBoardLabels();
        this.streamEvaluation(curr.fen);
        this.updateActiveLineCounts();
        this.renderTreeLines();
    }

    resetBoard() {
        this.lineTree.currentNode = this.lineTree.root;
        this.board.loadFEN(this.lineTree.rootFen);
        this.lastBoardFen = this.lineTree.rootFen;
        this.board.lastMove = null;
        this.board.render();
        this._updateBoardLabels();
        this.streamEvaluation(this.lineTree.rootFen);
        this.updateActiveLineCounts();
        this.renderTreeLines();
        this._showQuickAddNotice('Board reset to starting position. Play a new line or branch!', '#38bdf8');
    }

    loadTreeLine(line) {
        if (!line || !line.leafNode) return;
        this.lineTree.currentNode = line.leafNode;
        this.board.loadFEN(line.leafNode.fen);
        this.lastBoardFen = line.leafNode.fen;
        if (line.leafNode.move && line.leafNode.move.uci && line.leafNode.move.uci.length >= 4) {
            this.board.lastMove = {
                from: line.leafNode.move.uci.substring(0, 2),
                to: line.leafNode.move.uci.substring(2, 4)
            };
        } else {
            this.board.lastMove = null;
        }
        this.board.render();
        this._updateBoardLabels();
        this.streamEvaluation(line.leafNode.fen);
        this.updateActiveLineCounts();
        this.renderTreeLines();
        this._showQuickAddNotice(`✓ Loaded line: ${line.name || 'Variation'} (${line.plyCount} plies)`, '#38bdf8');
    }

    clearSessionTree() {
        this.lineTree.reset();
        this.firstStep();
        this.updateActiveLineCounts();
        this.renderTreeLines();
        this._showQuickAddNotice('Session move tree cleared. Start playing fresh moves!', '#38bdf8');
    }

    showTreeTab() {
        document.getElementById('scoutVariationsList')?.scrollIntoView({ behavior: 'smooth' });
    }

    async loadStudies() {
        try {
            const res = await fetch('/api/repertoire/studies');
            if (!res.ok) return;
            const studies = await res.json();
            this.studiesList = Array.isArray(studies) ? studies : [];
            const select = document.getElementById('scoutStudySelect');
            if (!select) return;

            const prevStudy = this.currentStudy || '';
            select.innerHTML = '<option value="">(All Studies)</option>';
            let hasPrev = false;

            this.studiesList.forEach(s => {
                const opt = document.createElement('option');
                opt.value = s.name;
                const repDot = s.repertoire === 'white' ? '⚪' : '⚫';
                const moves = s.total_moves ? ` (${s.total_moves} moves)` : '';
                opt.textContent = `${repDot} ${s.name}${moves}`;
                if (s.name === prevStudy) hasPrev = true;
                select.appendChild(opt);
            });

            if (hasPrev) {
                select.value = prevStudy;
            } else if (prevStudy) {
                const opt = document.createElement('option');
                opt.value = prevStudy;
                opt.textContent = `📁 ${prevStudy}`;
                select.appendChild(opt);
                select.value = prevStudy;
            } else {
                this.currentStudy = '';
                select.value = '';
            }
        } catch (e) {
            console.error('Error loading studies in scout:', e);
        }
    }

    async onStudyChange(studyName) {
        this.currentStudy = studyName ? studyName.trim() : '';
        this.renderTreeLines();
        if (this.currentStudy) {
            this._showQuickAddNotice(`✓ Active study set to "${this.currentStudy}"`, '#38bdf8');
        }
    }

    async onNewStudyClick() {
        if (typeof window.promptStudySelection !== 'function') return;
        // CODE-01: a study belongs to the repertoire the user is working in, not
        // to the cosmetic board orientation.
        const rep = this._lineStudyRepertoire(this.lineTree.getActiveLine());
        const defaultName = (this.activeStudy?.title || this.activeStudy?.opening_name || '').replace(/[\(\)\[\]]/g, '').trim();
        const name = await window.promptStudySelection({
            repertoire: rep,
            defaultName: defaultName,
            title: `Create New Study Group`,
            mode: 'new',
            confirmText: 'Create Study'
        });
        if (name) {
            this.currentStudy = name;
            await this.loadStudies();
            const select = document.getElementById('scoutStudySelect');
            if (select) select.value = name;
            this.renderTreeLines();
            this._showQuickAddNotice(`✓ Study "${name}" active`, '#4ade80');
            if (window.repertoireBuilder && typeof window.repertoireBuilder.loadStudies === 'function') {
                window.repertoireBuilder.loadStudies();
            }
        }
    }

    async assignLineStudy(idx) {
        const lines = this.lineTree.getAllLines();
        const line = lines[idx];
        if (!line) return;

        const defaultName = line.studyName || this.currentStudy || line.name || this.activeStudy?.opening_name || '';
        // CODE-01: derive the study's repertoire from the line's own side to move
        // (falling back to the last explicitly chosen import target), never from
        // the cosmetic board orientation.
        const rep = this._lineStudyRepertoire(line.moves);
        const chosen = (typeof window.promptStudySelection === 'function')
            ? await window.promptStudySelection({
                repertoire: rep,
                defaultName: defaultName,
                title: `Assign Study Group to Line #${idx + 1}`
            })
            : prompt('Enter Study Name for this line:', defaultName);

        if (chosen && chosen.trim()) {
            const name = chosen.trim();
            if (line.leafNode) {
                line.leafNode.studyName = name;
            }
            line.studyName = name;
            line.moves.forEach(m => { m.study_name = name; });
            await this.loadStudies();
            this.renderTreeLines();
            this._showQuickAddNotice(`✓ Line #${idx + 1} assigned to study "${name}"`, '#4ade80');
            if (window.repertoireBuilder && typeof window.repertoireBuilder.loadStudies === 'function') {
                window.repertoireBuilder.loadStudies();
            }
        }
    }

    updateActiveLineCounts() {
        const activeLine = this.lineTree.getActiveLine();
        const plyCount = activeLine.length;
        const countBadge = document.getElementById('scoutActiveLinePlyCount');
        if (countBadge) countBadge.textContent = plyCount;

        const lines = this.lineTree.getAllLines();
        const quickLineCount = document.getElementById('scoutQuickLineCount');
        if (quickLineCount) quickLineCount.textContent = lines.length;
        const treeBadge = document.getElementById('scoutTreeCountBadge');
        if (treeBadge) treeBadge.textContent = `${lines.length} Line${lines.length === 1 ? '' : 's'}`;

        this.scheduleOverlapCheck();
    }

    _updateBoardLabels() {
        const numEl = document.getElementById('scoutCurrentMoveNum');
        const sanEl = document.getElementById('scoutCurrentMoveSan');
        const tagEl = document.getElementById('scoutCurrentOpeningTag');
        const turnBadge = document.getElementById('scoutTurnBadge');

        const activeLine = this.lineTree.getActiveLine();
        const currNode = this.lineTree.currentNode;

        if (!currNode || currNode.isRoot() || activeLine.length === 0) {
            if (numEl) numEl.textContent = 'Start';
            if (sanEl) sanEl.textContent = '1. ...';
            if (tagEl) {
                const op = this.activeStudy?.opening_name ? `${this.activeStudy.opening_name} (Ready)` : 'Starting Position';
                tagEl.textContent = op;
                tagEl.title = op;
                _clampInline(tagEl, '240px');
            }
            if (turnBadge) {
                turnBadge.textContent = 'White to move';
                turnBadge.title = 'White to move';
                _clampInline(turnBadge, '110px');
            }
        } else {
            const lastMove = activeLine[activeLine.length - 1];
            if (numEl) numEl.textContent = `Move ${lastMove.move_number || Math.floor(activeLine.length / 2) + 1}`;
            if (sanEl) sanEl.textContent = `${lastMove.san}`;
            if (tagEl) {
                const fullTag = `${lastMove.eco || this.activeStudy?.eco || ''} ${lastMove.opening_name || this.activeStudy?.opening_name || 'Chess Variation'}`.trim();
                tagEl.textContent = fullTag;
                tagEl.title = fullTag;
                _clampInline(tagEl, '240px');
            }
            const isWhiteTurn = (currNode.fen || '').split(' ')[1] === 'w';
            if (turnBadge) {
                const turnText = isWhiteTurn ? 'White to move' : 'Black to move';
                turnBadge.textContent = turnText;
                turnBadge.title = turnText;
                _clampInline(turnBadge, '110px');
            }
        }
        this._updateMoveNoteUI();
    }

    _updateMoveNoteUI() {
        const noteLabel = document.getElementById('scoutNoteMoveLabel');
        const noteInput = document.getElementById('scoutMoveComment');
        const saveStatus = document.getElementById('scoutCommentSaveStatus');
        const btnImportant = document.getElementById('btnScoutMarkImportant');
        if (saveStatus) saveStatus.style.opacity = '0';

        const currNode = this.lineTree.currentNode;
        if (!currNode || currNode.isRoot() || !currNode.move) {
            if (noteLabel) {
                noteLabel.textContent = 'Move Note';
                noteLabel.title = 'Move Note';
                _clampInline(noteLabel, '160px');
            }
            if (noteInput) {
                noteInput.value = '';
                noteInput.placeholder = 'Play a move on the board to write ideas, advice, or plans...';
                noteInput.disabled = true;
            }
            if (btnImportant) {
                btnImportant.style.color = '';
                btnImportant.style.borderColor = '';
                btnImportant.classList.remove('active-important');
                btnImportant.textContent = '⭐ Mark Last Move Important';
                btnImportant.disabled = true;
                // CODE-14: fixed width + visibility so the toolbar never reflows
                // when the label changes between its two variants.
                btnImportant.style.visibility = 'hidden';
                btnImportant.style.opacity = '0';
                btnImportant.style.minWidth = '212px';
                btnImportant.style.justifyContent = 'center';
            }
            if (this.board) this.board.clearArrows();
            return;
        }

        const move = currNode.move;
        if (noteLabel) {
            const moveNumStr = move.move_number ? `Move ${move.move_number} ` : '';
            const colorDot = move.color === 'white' ? '⚪' : '⚫';
            const labelText = `${colorDot} ${moveNumStr}(${move.san || move.uci || 'Move'}) Note`;
            noteLabel.textContent = labelText;
            noteLabel.title = labelText;              // §8.2: full text on hover
            _clampInline(noteLabel, '160px');
        }

        if (noteInput) {
            noteInput.disabled = false;
            noteInput.placeholder = `Write thoughts, master explanations, or video advice for ${move.san || 'this move'}...`;
            if (move.comment !== undefined && move.comment !== null && move.comment !== '') {
                noteInput.value = move.comment;
            } else if (window.repertoireBuilder && typeof window.repertoireBuilder.findMoveInTree === 'function') {
                const repMove = window.repertoireBuilder.findMoveInTree(move.fen_before, move.uci);
                if (repMove && repMove.comment) {
                    move.comment = repMove.comment;
                    noteInput.value = repMove.comment;
                } else {
                    noteInput.value = '';
                }
            } else {
                noteInput.value = '';
            }
        }

        // Synchronize importance button display
        let isImportant = Boolean(move.is_important);
        if (!isImportant && window.repertoireBuilder && typeof window.repertoireBuilder.findMoveInTree === 'function') {
            const repMove = window.repertoireBuilder.findMoveInTree(move.fen_before, move.uci);
            if (repMove && repMove.is_important) {
                move.is_important = true;
                isImportant = true;
            }
        }
        if (btnImportant) {
            btnImportant.disabled = false;
            btnImportant.classList.toggle('active-important', isImportant);
            btnImportant.style.visibility = 'visible';
            btnImportant.style.opacity = '1';
            btnImportant.style.minWidth = '212px';
            btnImportant.style.justifyContent = 'center';
            btnImportant.style.color = isImportant ? '#fbbf24' : '';
            btnImportant.style.borderColor = isImportant ? '#fbbf24' : '';
            btnImportant.textContent = isImportant ? '⭐ Important Move' : '⭐ Mark Last Move Important';
        }

        // Render arrows if attached to this move
        if (this.board) {
            if (move.arrows) {
                try {
                    const arr = typeof move.arrows === 'string' ? JSON.parse(move.arrows) : move.arrows;
                    this.board.setArrows(arr);
                } catch (_) {
                    this.board.clearArrows();
                }
            } else if (window.repertoireBuilder && typeof window.repertoireBuilder.findMoveInTree === 'function') {
                const repMove = window.repertoireBuilder.findMoveInTree(move.fen_before, move.uci);
                if (repMove && repMove.arrows) {
                    try {
                        const arr = typeof repMove.arrows === 'string' ? JSON.parse(repMove.arrows) : repMove.arrows;
                        move.arrows = repMove.arrows;
                        this.board.setArrows(arr);
                    } catch (_) {
                        this.board.clearArrows();
                    }
                } else {
                    this.board.clearArrows();
                }
            } else {
                this.board.clearArrows();
            }
        }
    }

    /**
     * CODE-01 (CRITICAL): which repertoire a move belongs to is a property of
     * the LINE, never of `this.orientation` (a cosmetic "Flip Board" toggle).
     * The old `const rep = (this.orientation === 'black') ? 'black' : 'white'`
     * meant one flip + a typed note inserted `1.e4` as an *opponent* node at the
     * root of the Black repertoire while the UI said "Saved ✓".
     *
     * Resolution order:
     *   1. the repertoire that actually stores this exact move (Builder cache),
     *   2. otherwise the side that plays the move in this line.
     */
    _repertoireForMove(moveObj) {
        if (!moveObj) return this.lastActionRepertoire;
        const builder = window.repertoireBuilder;
        if (builder && typeof builder.findMoveInTree === 'function' && moveObj.fen_before && moveObj.uci) {
            const stored = builder.findMoveInTree(moveObj.fen_before, moveObj.uci);
            if (stored && (stored.repertoire === 'white' || stored.repertoire === 'black')) {
                return stored.repertoire;
            }
        }
        let color = moveObj.color;
        if (color !== 'white' && color !== 'black') {
            const turn = normalizeTreeFen(moveObj.fen_before).split(' ')[1];
            color = (turn === 'b') ? 'black' : 'white';
        }
        return color === 'black' ? 'black' : 'white';
    }

    /** Records an explicit user choice (import / delete buttons). */
    _targetRepertoire(explicit) {
        if (explicit === 'white' || explicit === 'black') {
            this.lastActionRepertoire = explicit;
            return explicit;
        }
        return this.lastActionRepertoire;
    }

    /**
     * Read-only derivation for study-scoped actions: the side that plays the
     * first move of the line (that is the side whose repertoire the line
     * belongs to), falling back to the last explicitly chosen target.
     */
    _lineStudyRepertoire(moves) {
        const first = Array.isArray(moves) ? moves[0] : null;
        if (first && (first.color === 'white' || first.color === 'black')) return first.color;
        return this.lastActionRepertoire;
    }

    async saveMoveNote(commentText) {
        const currNode = this.lineTree.currentNode;
        if (!currNode || currNode.isRoot() || !currNode.move) return;

        currNode.move.comment = commentText;
        const saveStatus = document.getElementById('scoutCommentSaveStatus');
        if (saveStatus) {
            saveStatus.style.opacity = '1';
            saveStatus.textContent = 'Saving...';
            saveStatus.style.color = '#38bdf8';
        }

        const rep = this._repertoireForMove(currNode.move);
        try {
            const res = await fetch('/api/repertoire/move_meta', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repertoire: rep,
                    parent_fen: currNode.move.fen_before,
                    move_uci: currNode.move.uci,
                    comment: commentText
                })
            });
            const data = await res.json().catch(() => ({}));
            const writtenTo = data && data.move ? data.move.repertoire : null;
            if (data.status === 'success' && (!writtenTo || writtenTo === rep)) {
                if (saveStatus) {
                    saveStatus.textContent = 'Saved ✓';
                    saveStatus.style.color = '#4ade80';
                    saveStatus.style.opacity = '1';
                    setTimeout(() => { if (saveStatus) saveStatus.style.opacity = '0'; }, 2000);
                }
                if (window.repertoireBuilder) {
                    window.repertoireBuilder.loadRepertoire(rep);
                }
            } else {
                // Never claim success for a write that did not land where it was
                // asked to land.
                if (saveStatus) {
                    saveStatus.textContent = (writtenTo && writtenTo !== rep)
                        ? `Not saved (stored in ${writtenTo} repertoire)`
                        : 'Saved in session line';
                    saveStatus.style.color = '#f59e0b';
                    setTimeout(() => { if (saveStatus) saveStatus.style.opacity = '0'; }, 2000);
                }
            }
        } catch (e) {
            console.error('Error saving move note in scout:', e);
            if (saveStatus) {
                saveStatus.textContent = 'Saved in session line only';
                saveStatus.style.color = '#f59e0b';
                setTimeout(() => { if (saveStatus) saveStatus.style.opacity = '0'; }, 2000);
            }
        }
        this.renderTreeLines();
    }

    async _onArrowsChange(arrows) {
        const currNode = this.lineTree.currentNode;
        if (!currNode || currNode.isRoot() || !currNode.move) return;
        currNode.move.arrows = JSON.stringify(arrows);
        const rep = this._repertoireForMove(currNode.move);
        try {
            const res = await fetch('/api/repertoire/move_meta', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repertoire: rep,
                    parent_fen: currNode.move.fen_before,
                    move_uci: currNode.move.uci,
                    arrows: JSON.stringify(arrows)
                })
            });
            if (res && res.ok && window.repertoireBuilder) {
                window.repertoireBuilder.loadRepertoire(rep);
            }
        } catch (e) {
            console.error('Error saving arrows in scout:', e);
        }
    }

    renderTreeLines() {
        const list = document.getElementById('scoutVariationsList');
        const badge = document.getElementById('scoutTreeCountBadge');
        const quickCount = document.getElementById('scoutQuickLineCount');

        const lines = this.lineTree.getAllLines();
        const count = lines.length;

        if (badge) badge.textContent = `${count} Line${count === 1 ? '' : 's'}`;
        if (quickCount) quickCount.textContent = count;

        if (!list) return;

        if (count === 0) {
            list.innerHTML = `
                <div style="color:var(--text-muted); padding:2rem 1rem; text-align:center; display:flex; flex-direction:column; align-items:center; gap:0.6rem;">
                    <div style="font-size:2.2rem; opacity:0.6;">\u265f\ufe0f</div>
                    <div style="font-weight:600; color:var(--text-primary); font-size:0.88rem;">No lines played yet</div>
                    <div style="font-size:0.75rem; line-height:1.45; max-width:260px;">
                        Move pieces freely on the board to explore opening moves. Every sequence and branch will automatically appear in this tree.
                    </div>
                </div>
            `;
            return;
        }

        // SEC-02 / CODE-19: every card (name, ECO, PGN, study name, badge
        // tooltips) is produced by the escaped `scoutLineCardMarkup` helper.
        // CODE-06: badges are looked up by the leaf node's stable id, never by
        // array position.
        list.innerHTML = lines.map((line, idx) => scoutLineCardMarkup(line, idx, {
            white: this._overlapForLine(line, 'white'),
            black: this._overlapForLine(line, 'black'),
            isActive: this.lineTree.isAttached(line.leafNode) && this._isOnActivePath(line.leafNode),
            currentStudy: this.currentStudy,
            lineId: line.lineId || (line.leafNode ? line.leafNode.id : '')
        })).join('');

        // Attach event listeners
        list.querySelectorAll('.scout-var-card').forEach(card => {
            card.addEventListener('click', (e) => {
                if (e.target.closest('button')) return;
                const idx = parseInt(card.dataset.idx, 10);
                if (lines[idx]) this.loadTreeLine(lines[idx]);
            });
        });

        list.querySelectorAll('.btn-load-tree-line').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const idx = parseInt(btn.dataset.idx, 10);
                if (lines[idx]) this.loadTreeLine(lines[idx]);
            });
        });

        list.querySelectorAll('.btn-assign-line-study').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const idx = parseInt(btn.dataset.idx, 10);
                this.assignLineStudy(idx);
            });
        });

        list.querySelectorAll('.btn-import-tree-white').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const idx = parseInt(btn.dataset.idx, 10);
                if (!lines[idx]) return;
                this._targetRepertoire('white');
                this.importToRepertoire('white', lines[idx].moves, idx);
            });
        });

        list.querySelectorAll('.btn-import-tree-black').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const idx = parseInt(btn.dataset.idx, 10);
                if (!lines[idx]) return;
                this._targetRepertoire('black');
                this.importToRepertoire('black', lines[idx].moves, idx);
            });
        });

        list.querySelectorAll('.btn-del-tree-white').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const idx = parseInt(btn.dataset.idx, 10);
                if (!lines[idx]) return;
                this._targetRepertoire('white');
                this.removeTreeLineFromRepertoire(lines[idx], 'white');
            });
        });

        list.querySelectorAll('.btn-del-tree-black').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const idx = parseInt(btn.dataset.idx, 10);
                if (!lines[idx]) return;
                this._targetRepertoire('black');
                this.removeTreeLineFromRepertoire(lines[idx], 'black');
            });
        });

        list.querySelectorAll('.btn-del-session-line').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const idx = parseInt(btn.dataset.idx, 10);
                if (lines[idx]) this.deleteSessionLine(lines[idx]);
            });
        });

        this.scheduleOverlapCheck();
    }

    /** CODE-06: stable, node-identity keyed overlap lookup. */
    _lineKey(line) {
        if (!line) return '';
        if (line.lineId) return line.lineId;
        return (line.leafNode && line.leafNode.id) ? line.leafNode.id : '';
    }

    _overlapForLine(line, repertoire) {
        const key = this._lineKey(line);
        if (!key) return null;
        const bucket = (this.treeOverlapMap && this.treeOverlapMap[repertoire]) || {};
        return bucket[key] || null;
    }

    /**
     * A line is "Active" only when the tree's current node really is on that
     * line's path. The old prefix match (`lineUcis.startsWith(activeUcis)`)
     * flagged every descendant line at once.
     */
    _isOnActivePath(leafNode) {
        let curr = leafNode;
        const target = this.lineTree.currentNode;
        if (!curr || !target) return false;
        while (curr) {
            if (curr === target) return true;
            curr = curr.parent;
        }
        return false;
    }

    // DEAD-01(b): `renderVariations(variations)` was a vestige of the pre-tree
    // design (unused parameter, no caller) - removed. The session line tree is
    // the single source of truth for this panel.

    scheduleOverlapCheck() {
        if (this.activeOverlapDebounce) {
            clearTimeout(this.activeOverlapDebounce);
        }
        this.activeOverlapDebounce = setTimeout(() => {
            this.checkActiveLineOverlap();
            this.checkTreeOverlap();
        }, 120);
    }

    /** CODE-21: shared, abortable fetch for the white/black overlap pair. */
    async _postOverlapPair(url, payloadFor, signal) {
        const [resW, resB] = await Promise.all([
            fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payloadFor('white')),
                signal
            }),
            fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payloadFor('black')),
                signal
            })
        ]);
        return [await resW.json(), await resB.json()];
    }

    async checkActiveLineOverlap() {
        // CODE-05 / CODE-21: generation counter + AbortController, so a stale
        // response can never repaint the strip or re-toggle the Remove button
        // after the tree has moved on.
        const seq = ++this.activeOverlapSeq;
        if (this.activeOverlapController) this.activeOverlapController.abort();
        const controller = new AbortController();
        this.activeOverlapController = controller;

        const strip = document.getElementById('scoutActiveLineOverlap');
        const btnRemoveRep = document.getElementById('btnScoutRemoveFromRep');
        const activeLine = this.lineTree.getActiveLine();

        // §8.4: the Remove button keeps its box and toggles visibility.
        const setRemoveVisible = (visible) => {
            if (!btnRemoveRep) return;
            btnRemoveRep.style.display = 'inline-flex';
            btnRemoveRep.style.visibility = visible ? 'visible' : 'hidden';
            btnRemoveRep.style.opacity = visible ? '1' : '0';
            btnRemoveRep.style.minWidth = '150px';
            btnRemoveRep.style.justifyContent = 'center';
            btnRemoveRep.disabled = !visible;
        };

        if (!activeLine || activeLine.length === 0) {
            if (strip) {
                strip.innerHTML = '<span style="font-size:0.72rem; color:var(--text-muted); font-weight:500;">Repertoire: Starting Position</span>';
                strip.style.display = 'flex';
            }
            this.activeLineOverlap = null;
            setRemoveVisible(false);
            return;
        }

        const formattedMoves = activeLine.map(m => ({
            fen_before: m.fen_before,
            uci: m.uci,
            san: m.san,
            opening_name: this.activeStudy?.opening_name || m.opening_name || '',
            eco: this.activeStudy?.eco || m.eco || ''
        }));

        try {
            const [dataW, dataB] = await this._postOverlapPair(
                '/api/scout/check_line',
                (repertoire) => ({ repertoire, moves: formattedMoves }),
                controller.signal
            );
            if (seq !== this.activeOverlapSeq) return;

            // CODE-19: the strip and the line cards now share one renderer.
            if (strip) {
                strip.innerHTML = scoutOverlapStripMarkup(dataW, dataB);
                strip.style.display = 'flex';
            }

            // CODE-01: the Remove button follows the *line's* repertoire, not
            // the cosmetic board orientation.
            const lastMove = activeLine[activeLine.length - 1];
            const rep = this._repertoireForMove(lastMove);
            const relevantData = (rep === 'black') ? dataB : dataW;
            setRemoveVisible(overlapIsInRepertoire(relevantData));
            this.activeLineOverlap = { white: dataW, black: dataB };
        } catch (e) {
            if (e && e.name === 'AbortError') return;
            if (seq !== this.activeOverlapSeq) return;
            console.error('Failed to check line overlap:', e);
        }
    }

    async checkTreeOverlap() {
        const lines = this.lineTree.getAllLines();
        if (!lines || lines.length === 0) return;

        // CODE-06: the request and the response are keyed by the leaf node's
        // stable id. Array positions shift whenever a move is added, which is
        // what put every badge on the wrong card.
        const payloadLines = lines.map(l => ({
            id: this._lineKey(l),
            moves: l.moves.map(m => ({
                fen_before: m.fen_before,
                uci: m.uci,
                san: m.san,
                opening_name: this.activeStudy?.opening_name || m.opening_name || '',
                eco: this.activeStudy?.eco || m.eco || ''
            }))
        }));
        const snapshotIds = payloadLines.map(p => p.id).join('|');

        const seq = ++this.treeOverlapSeq;
        if (this.treeOverlapController) this.treeOverlapController.abort();
        const controller = new AbortController();
        this.treeOverlapController = controller;

        try {
            const [listW, listB] = await this._postOverlapPair(
                '/api/scout/check_tree',
                (repertoire) => ({ repertoire, lines: payloadLines }),
                controller.signal
            );
            if (seq !== this.treeOverlapSeq) return;

            const nextMap = { white: {}, black: {} };
            if (Array.isArray(listW)) {
                listW.forEach(item => { nextMap.white[item.id] = item; });
            }
            if (Array.isArray(listB)) {
                listB.forEach(item => { nextMap.black[item.id] = item; });
            }
            this.treeOverlapMap = nextMap;
            this._treeOverlapSnapshot = snapshotIds;

            this._applyTreeBadges(lines);
        } catch (e) {
            if (e && e.name === 'AbortError') return;
            if (seq !== this.treeOverlapSeq) return;
            console.error('Failed to check tree overlap:', e);
        }
    }

    _applyTreeBadges(lines) {
        const list = document.getElementById('scoutVariationsList');
        if (!list) return;
        const snapshot = lines.map(l => this._lineKey(l)).join('|');
        // CODE-21: never patch a panel that was re-rendered from a newer tree.
        if (this._treeOverlapSnapshot && snapshot && this._treeOverlapSnapshot !== snapshot) return;

        const cards = list.querySelectorAll('.scout-var-card');
        cards.forEach((card) => {
            // CODE-06: identity, not position.
            const key = card.dataset ? card.dataset.lineId : '';
            const wData = (this.treeOverlapMap && this.treeOverlapMap.white && this.treeOverlapMap.white[key]) || null;
            const bData = (this.treeOverlapMap && this.treeOverlapMap.black && this.treeOverlapMap.black[key]) || null;

            let badgesWrap = card.querySelector('.scout-card-overlap-badges');
            if (!badgesWrap) {
                badgesWrap = document.createElement('div');
                badgesWrap.className = 'scout-card-overlap-badges';
                badgesWrap.style.cssText = 'display:flex; gap:0.35rem; align-items:center; margin-top:0.35rem; flex-wrap:wrap; min-height:20px;';
                const pgnEl = card.querySelector('.scout-var-pgn');
                if (pgnEl) card.insertBefore(badgesWrap, pgnEl.nextSibling);
            }

            // CODE-19 + SEC-02: one escaped renderer, identical wording to the
            // freshly rendered card.
            badgesWrap.innerHTML = (wData ? overlapBadgeMarkup(wData, 'white') : '') + (bData ? overlapBadgeMarkup(bData, 'black') : '');

            const btnW = card.querySelector('.btn-import-tree-white');
            if (btnW) {
                const info = overlapActionInfo(wData, 'white');
                btnW.textContent = info.text;
                btnW.title = info.title;
            }

            const btnB = card.querySelector('.btn-import-tree-black');
            if (btnB) {
                const info = overlapActionInfo(bData, 'black');
                btnB.textContent = info.text;
                btnB.title = info.title;
            }
        });
    }

    /**
     * AGENTS.md §4E pre-flight: a divergent line REPLACES a prepared user move
     * and the backend deletes the old branch. The overlap result is already
     * cached (from the debounced check), so the warning costs no extra request -
     * and the import is confirmed *before* anything is destroyed.
     */
    _cachedOverlapForMoves(repertoire, moves) {
        if (this.activeLineOverlap && this.activeLineOverlap[repertoire]) {
            return this.activeLineOverlap[repertoire];
        }
        const ucis = (moves || []).map(m => m.uci).join(' ');
        const line = this.lineTree.getAllLines().find(l => l.moves.map(m => m.uci).join(' ') === ucis) || null;
        return line ? this._overlapForLine(line, repertoire) : null;
    }

    _confirmDestructiveImport(repertoire, moves) {
        const data = this._cachedOverlapForMoves(repertoire, moves);
        if (!overlapReplacesPreparedMove(data)) return true;
        const warning = overlapReplacementWarning(data);
        const confirmed = (typeof window.confirm === 'function') ? window.confirm(`⚠️ ${warning}\n\nImport it anyway?`) : true;
        return confirmed !== false;
    }

    /**
     * CODE-04: the outcome is decided by what actually happened
     * (`imported_count` + `errors` + `overlap_status`), never by the HTTP
     * envelope alone. Previously a total per-move failure was still announced as
     * "✓ Added to ... Repertoire", played the success chime, refreshed the
     * builder and revealed the "Train in Trainer" button - and the backend's
     * "Nc3 replaces your prepared Nf3" warning was never shown.
     */
    _reportImportResult(repertoire, data, studyName, successPrefix) {
        const repCap = repertoire.charAt(0).toUpperCase() + repertoire.slice(1);
        const errors = Array.isArray(data.errors) ? data.errors.filter(Boolean) : [];
        // SCOUT-11 (FIXER-I): `imported_count` counts every row the write loop *touched*
        // (so an explicit comment/arrows/study_name on an already-stored move is still
        // applied), while `inserted_count` counts only the rows that did not exist before
        // this call. Prefer the latter for the "added N moves" wording and fall back to the
        // former when an older backend does not send it.
        const inserted = (data.inserted_count === undefined || data.inserted_count === null)
            ? Number(data.imported_count || 0)
            : Number(data.inserted_count);
        const touched = Number(data.imported_count || 0);
        const overlapDetails = data.overlap_details || {};
        const alreadyPresent = !!data.already_present;
        const diverges = overlapReplacesPreparedMove(overlapDetails);

        if (touched === 0 && !alreadyPresent) {
            const detail = errors.length ? errors.join(' | ') : (data.message || 'no moves were stored');
            this._showQuickAddNotice(`✗ Import into ${repCap} repertoire failed: ${detail}`, '#ef4444', 6000);
            return false;
        }

        const parts = [];
        if (alreadyPresent) {
            parts.push(`already in ${repCap} (${_scoutNum(data.total_moves, 0)} plies)`);
        } else {
            parts.push(`added ${_scoutNum(inserted, 0)} ${inserted === 1 ? 'move' : 'moves'} to ${repCap}`);
            // An idempotent re-import of an already-stored move: say so instead of
            // silently reporting the same number of "added" moves every time.
            if (touched > inserted) {
                parts.push(`(${_scoutNum(touched - inserted, 0)} already stored, updated)`);
            }
        }
        if (studyName) parts.push(`[${studyName}]`);
        this._showQuickAddNotice(`✓ ${successPrefix}${parts.join(' ')}`, alreadyPresent ? '#38bdf8' : '#4ade80');

        if (errors.length) {
            this._showQuickAddNotice(`⚠ ${errors.length} move(s) skipped: ${errors.join(' | ')}`, '#f59e0b', 6000);
        }
        if (diverges) {
            this._showQuickAddNotice(`⚠️ ${overlapReplacementWarning(overlapDetails)}`, '#f59e0b', 8000);
        }
        if (window.chessAudio) window.chessAudio.playSuccess();
        return true;
    }

    async _submitLineImport(repertoire, formattedMoves, studyName, successPrefix) {
        try {
            const res = await fetch('/api/scout/import_repertoire', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repertoire: repertoire,
                    moves: formattedMoves,
                    study_name: studyName
                })
            });
            if (!res.ok) {
                let detail = `HTTP ${res.status}`;
                try {
                    const body = await res.json();
                    if (body && body.detail) detail = body.detail;
                } catch (_) {}
                this._showQuickAddNotice(`✗ Import failed: ${detail}`, '#ef4444', 6000);
                return false;
            }
            const data = await res.json().catch(() => ({}));
            // FIXER-I (SCOUT-11) made the endpoint truthful: `already_present` is a
            // successful no-op (AGENTS.md §4E: "prevents redundant writes") and `error`
            // is the only terminal failure. Keying the failure test on `'success'`
            // alone therefore answered a red "✗ Import rejected" toast for a
            // legitimate double-click.
            if (data.status !== 'success') {
                if (data.status !== 'already_present') {
                    const errs = Array.isArray(data.errors) && data.errors.length
                        ? `: ${data.errors.filter(Boolean).join(' | ')}`
                        : '';
                    this._showQuickAddNotice(
                        `✗ Import rejected: ${data.message || 'check the move format'}${errs}`,
                        '#ef4444', 6000);
                    return false;
                }
            }

            const ok = this._reportImportResult(repertoire, data, studyName, successPrefix);
            if (!ok) return false;

            const btnTrain = document.getElementById('btnScoutTrainAfterImport');
            if (btnTrain) {
                // §8.4: fixed box + visibility toggle (it used to be revealed
                // once and never hidden again).
                btnTrain.style.display = 'inline-flex';
                btnTrain.style.visibility = 'visible';
                btnTrain.style.opacity = '1';
                btnTrain.style.minWidth = '150px';
                btnTrain.style.justifyContent = 'center';
                btnTrain.disabled = false;
            }
            if (window.repertoireBuilder) {
                window.repertoireBuilder.loadRepertoire(repertoire);
            }

            await this.loadStudies();
            if (window.repertoireBuilder && typeof window.repertoireBuilder.loadStudies === 'function') {
                window.repertoireBuilder.loadStudies();
            }

            // Refresh badges for the new state (debounced + abortable).
            this.scheduleOverlapCheck();
            this.renderTreeLines();
            return true;
        } catch (e) {
            console.error('Import error:', e);
            this._showQuickAddNotice(`✗ Import error: ${e.message}`, '#ef4444', 6000);
            return false;
        }
    }

    async importToRepertoire(repertoire, specificLineMoves = null, lineIndex = null) {
        let movesToImport = specificLineMoves;

        if (!movesToImport || movesToImport.length === 0) {
            movesToImport = this.lineTree.getActiveLine();
        }

        if (!movesToImport || movesToImport.length === 0) {
            // §8.1: floating toast only. The in-flow `#scoutImportAlert` element
            // this used to touch does not exist in index.html at all.
            this._showQuickAddNotice('No moves played to import yet. Play moves on the board first!', '#ef4444');
            return false;
        }

        const targetRep = this._targetRepertoire(repertoire);
        const allLines = this.lineTree.getAllLines();
        const line = (lineIndex !== null && allLines && allLines[lineIndex])
            ? allLines[lineIndex]
            : (allLines.find(l => {
                const lUcis = l.moves.map(m => m.uci).join(' ');
                const mUcis = movesToImport.map(m => m.uci).join(' ');
                return lUcis === mUcis;
            }) || null);

        let studyName = line?.studyName || this.currentStudy;
        if (!studyName) {
            const defaultStudy = (this.activeStudy?.title || this.activeStudy?.opening_name || (line ? line.name : '') || '').replace(/[\(\)\[\]]/g, '').trim();
            try {
                studyName = (typeof window.promptStudySelection === 'function')
                    ? await window.promptStudySelection({
                        repertoire: targetRep,
                        defaultName: defaultStudy,
                        title: `Save Line to ${targetRep.charAt(0).toUpperCase() + targetRep.slice(1)} Study`
                    })
                    : defaultStudy;
            } catch (e) {
                // CODE-18: a rejected modal promise must abort *visibly*.
                console.error('Study selection failed:', e);
                this._showQuickAddNotice(`✗ Import cancelled: could not choose a study (${e.message})`, '#ef4444', 5000);
                return false;
            }

            if (!studyName) return false; // User cancelled
            this.currentStudy = studyName;
            if (line && line.leafNode) {
                line.leafNode.studyName = studyName;
            }
        }

        // §4E: warn before a divergent import destroys a prepared move.
        if (!this._confirmDestructiveImport(targetRep, movesToImport)) {
            this._showQuickAddNotice('Import cancelled - your prepared move was kept.', '#94a3b8');
            return false;
        }

        const formattedMoves = movesToImport.map(m => ({
            fen_before: m.fen_before,
            uci: m.uci,
            san: m.san,
            opening_name: studyName || this.activeStudy?.opening_name || m.opening_name || '',
            eco: this.activeStudy?.eco || m.eco || '',
            comment: m.comment || '',
            is_important: Boolean(m.is_important),
            arrows: m.arrows || ''
        }));

        return this._submitLineImport(targetRep, formattedMoves, studyName, 'Line ');
    }

    async importAllLinesToRepertoire(repertoire) {
        const allMoves = this.lineTree.getAllUniqueMoves();

        if (!allMoves || allMoves.length === 0) {
            this._showQuickAddNotice('No explored lines in tree to import. Play moves on the board first!', '#ef4444');
            return false;
        }

        const targetRep = this._targetRepertoire(repertoire);
        let studyName = this.currentStudy;
        if (!studyName) {
            const defaultStudy = (this.activeStudy?.title || this.activeStudy?.opening_name || '').replace(/[\(\)\[\]]/g, '').trim();
            try {
                studyName = (typeof window.promptStudySelection === 'function')
                    ? await window.promptStudySelection({
                        repertoire: targetRep,
                        defaultName: defaultStudy,
                        title: `Save All Lines to ${targetRep.charAt(0).toUpperCase() + targetRep.slice(1)} Study`
                    })
                    : defaultStudy;
            } catch (e) {
                console.error('Study selection failed:', e);
                this._showQuickAddNotice(`✗ Import cancelled: could not choose a study (${e.message})`, '#ef4444', 5000);
                return false;
            }

            if (!studyName) return false; // User cancelled
            this.currentStudy = studyName;
        }

        const formattedMoves = allMoves.map(m => ({
            fen_before: m.fen_before,
            uci: m.uci,
            san: m.san,
            opening_name: m.study_name || studyName || this.activeStudy?.opening_name || m.opening_name || '',
            eco: this.activeStudy?.eco || m.eco || '',
            comment: m.comment || '',
            is_important: Boolean(m.is_important),
            arrows: m.arrows || ''
        }));

        return this._submitLineImport(targetRep, formattedMoves, studyName, 'All lines - ');
    }

    async markLastMoveAsImportant() {
        const activeLine = this.lineTree.getActiveLine();
        if (!activeLine || activeLine.length === 0) {
            this._showQuickAddNotice('Play moves on the board first!', '#ef4444');
            return;
        }

        const lastMove = activeLine[activeLine.length - 1];
        const newImportant = !lastMove.is_important;
        lastMove.is_important = newImportant;

        const moveName = lastMove.san || lastMove.uci || 'Move';

        // CODE-01: write to the repertoire this move actually belongs to. The
        // old code looped over BOTH repertoires, so marking a fresh move created
        // it in the White *and* the Black tree - and the target followed the
        // cosmetic board flip.
        const rep = this._repertoireForMove(lastMove);
        const repName = rep.charAt(0).toUpperCase() + rep.slice(1);
        let persisted = false;
        let storedIn = null;
        try {
            const res = await fetch('/api/repertoire/move_meta', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repertoire: rep,
                    parent_fen: lastMove.fen_before,
                    move_uci: lastMove.uci,
                    is_important: newImportant
                })
            });
            if (res.ok) {
                const data = await res.json().catch(() => ({}));
                if (data.status === 'success') {
                    persisted = true;
                    storedIn = (data.move && data.move.repertoire) || rep;
                }
            }
        } catch (_) {}

        // CODE-20: no success chime when nothing was written.
        if (window.chessAudio) {
            if (newImportant && persisted) window.chessAudio.playSuccess();
            else window.chessAudio.playMove();
        }
        const noticeMsg = newImportant
            ? (persisted
                ? `⭐ Marked ${moveName} as Important in your ${repName} repertoire`
                : `⭐ Marked ${moveName} as Important (kept in this session - it is not in your repertoire yet)`)
            : `Unmarked ${moveName} priority`;
        this._showQuickAddNotice(noticeMsg, newImportant ? '#fbbf24' : '#94a3b8');
        if (storedIn && storedIn !== rep) {
            this._showQuickAddNotice(`⚠️ The server stored the flag in the ${storedIn} repertoire, not ${rep}.`, '#f59e0b', 6000);
        }

        const btn = document.getElementById('btnScoutMarkImportant');
        if (btn) {
            btn.classList.toggle('active-important', newImportant);
            btn.style.color = newImportant ? '#fbbf24' : '';
            btn.style.borderColor = newImportant ? '#fbbf24' : '';
            btn.textContent = newImportant ? '⭐ Important Move' : '⭐ Mark Last Move Important';
            btn.style.minWidth = '212px';
            btn.style.justifyContent = 'center';
        }
        if (window.repertoireBuilder && typeof window.repertoireBuilder.refreshTree === 'function') {
            window.repertoireBuilder.refreshTree();
        }
        this.renderTreeLines();
    }

    /**
     * CODE-09: `DELETE /api/repertoire/move` answers `{"status":"ok"}`
     * unconditionally, so the UI celebrated deletes that removed nothing (and
     * then undid the user's board position anyway). The outcome is verified
     * before anything is claimed:
     *   1. the HTTP status must be 2xx,
     *   2. `deleted_count` is used when the server provides it (backend hand-off:
     *      `backend/api.py::delete_repertoire_move` must return it),
     *   3. otherwise the move is re-checked against the repertoire.
     */
    async _deleteRepertoireBranch(repertoire, parentFen, moveUci) {
        let res;
        let data = null;
        try {
            res = await fetch('/api/repertoire/move', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repertoire: repertoire,
                    parent_fen: parentFen,
                    move_uci: moveUci
                })
            });
            data = await res.json().catch(() => null);
        } catch (e) {
            console.error('Error removing move from repertoire:', e);
            this._showQuickAddNotice(`✗ Could not reach the server: ${e.message}`, '#ef4444', 5000);
            return { removed: 0, verified: true, error: e.message };
        }

        if (!res.ok) {
            const detail = (data && data.detail) ? data.detail : `HTTP ${res.status}`;
            this._showQuickAddNotice(`✗ Remove failed: ${detail}`, '#ef4444', 5000);
            return { removed: 0, verified: true, error: detail };
        }

        const reported = (data && typeof data.deleted_count === 'number') ? data.deleted_count : null;
        if (reported !== null) {
            return { removed: reported, verified: true, error: null };
        }

        // No affected-row signal yet: verify independently via the overlap endpoint.
        try {
            const probe = await fetch('/api/scout/check_line', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repertoire: repertoire,
                    moves: [{ fen_before: parentFen, uci: moveUci, san: '' }]
                })
            });
            if (probe.ok) {
                const check = await probe.json();
                const stillThere = overlapIsInRepertoire(check);
                return { removed: stillThere ? 0 : 1, verified: true, error: null };
            }
        } catch (_) {}
        return { removed: null, verified: false, error: null };
    }

    /** Turns a verified delete outcome into an honest notice. */
    _reportRemoval(repertoire, outcome, moveLabel) {
        const repName = repertoire.charAt(0).toUpperCase() + repertoire.slice(1);
        const label = moveLabel || 'that move';
        if (outcome.removed && outcome.removed > 0) {
            // CODE-20: a destructive action must not sound like a reward.
            if (window.chessAudio) {
                if (typeof window.chessAudio.playRemove === 'function') window.chessAudio.playRemove();
                else window.chessAudio.playCapture();
            }
            this._showQuickAddNotice(`✓ Removed ${label} from ${repName} Repertoire`, '#ef4444');
            if (window.repertoireBuilder) {
                window.repertoireBuilder.loadRepertoire(repertoire);
            }
            this.scheduleOverlapCheck();
            return true;
        }
        if (outcome.verified) {
            this._showQuickAddNotice(
                `Nothing was removed - ${label} is not in your ${repName} repertoire.`,
                '#f59e0b', 6000
            );
        } else {
            this._showQuickAddNotice(
                `Could not confirm the removal of ${label} from your ${repName} repertoire - check the Builder.`,
                '#f59e0b', 6000
            );
        }
        return false;
    }

    async removeCurrentLineFromRepertoire() {
        const activeLine = this.lineTree.getActiveLine();
        if (!activeLine || activeLine.length === 0) return;
        const lastMove = activeLine[activeLine.length - 1];
        // CODE-01: the target repertoire follows the move, not the board flip.
        const rep = this._repertoireForMove(lastMove);
        const repName = rep.charAt(0).toUpperCase() + rep.slice(1);
        const moveLabel = lastMove.san || lastMove.uci || 'this move';

        const confirmed = confirm(
            `⚠️ Remove move ${moveLabel} and every line continuing below it from your ${repName} repertoire?`
        );
        if (!confirmed) return;

        const outcome = await this._deleteRepertoireBranch(rep, lastMove.fen_before, lastMove.uci);
        const removed = this._reportRemoval(rep, outcome, moveLabel);
        // Only step the board back when something was really deleted.
        if (removed) this.undoUserMove();
    }

    async removeTreeLineFromRepertoire(line, repertoire) {
        if (!line || !line.moves || line.moves.length === 0) return;
        const rep = this._targetRepertoire(repertoire);
        const repName = rep.charAt(0).toUpperCase() + rep.slice(1);
        const lastMove = line.moves[line.moves.length - 1];
        const moveLabel = lastMove.san || lastMove.uci || 'this move';
        const confirmed = confirm(
            `⚠️ Remove move ${moveLabel} and every line continuing below it from your ${repName} repertoire?`
        );
        if (!confirmed) return;

        const outcome = await this._deleteRepertoireBranch(rep, lastMove.fen_before, lastMove.uci);
        this._reportRemoval(rep, outcome, moveLabel);
    }

    deleteSessionLine(line) {
        if (!line || !line.leafNode) return;
        const before = this.lineTree.currentNode;
        this.lineTree.deleteLine(line.leafNode);
        // CODE-02: if the deleted line contained the current node, the board must
        // follow the tree instead of showing a position that no longer exists.
        if (this.lineTree.currentNode !== before) {
            const node = this.lineTree.currentNode;
            if (this.board) {
                this.board.loadFEN(node.fen);
                this.board.lastMove = (node.move && node.move.uci && node.move.uci.length >= 4)
                    ? { from: node.move.uci.substring(0, 2), to: node.move.uci.substring(2, 4) }
                    : null;
                this.board.render();
            }
            this.lastBoardFen = node.fen;
            this._updateBoardLabels();
            this.streamEvaluation(node.fen);
        }
        this.updateActiveLineCounts();
        this.renderTreeLines();
        this._showQuickAddNotice('Line removed from session tree', '#94a3b8');
    }
    flipBoard() {
        this.orientation = (this.orientation === 'white') ? 'black' : 'white';
        if (this.board) {
            this.board.setOrientation(this.orientation);
        }
        const barFill = document.getElementById('scoutEvalBarFill');
        if (barFill && barFill.parentElement) {
            barFill.parentElement.classList.toggle('flipped', this.orientation === 'black');
        }
    }

    streamEvaluation(fen) {
        if (this.evalAbortController) {
            this.evalAbortController.abort();
        }
        this.evalAbortController = new AbortController();

        fetch(`/api/repertoire/eval_stream?fen=${encodeURIComponent(fen)}&depth=16`, {
            signal: this.evalAbortController.signal
        }).then(response => {
            if (!response.body) return;
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            const readChunk = () => {
                reader.read().then(({ done, value }) => {
                    if (done) return;
                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n\n');
                    buffer = lines.pop();

                    for (const block of lines) {
                        const eventLine = block.split('\n').find(l => l.startsWith('data: '));
                        if (eventLine) {
                            try {
                                const data = JSON.parse(eventLine.slice(6));
                                this._updateEvalBar(data.eval, data.mate);
                            } catch (e) {}
                        }
                    }
                    readChunk();
                }).catch(() => {});
            };
            readChunk();
        }).catch(() => {});
    }

    _updateEvalBar(scoreCp, mate) {
        const barFill = document.getElementById('scoutEvalBarFill');
        const barLabel = document.getElementById('scoutEvalBarLabel');
        if (!barFill || !barLabel) return;
        if (barFill.parentElement) {
            barFill.parentElement.classList.toggle('flipped', this.orientation === 'black');
        }

        let evalText = '+0.0';
        let pct = 50;

        if (mate !== null && mate !== undefined) {
            // SEC-M: `Math.abs()` on a corrupt JSON value yields NaN/Infinity, which
            // rendered as the literal text "MNaN" / "MInfinity". Coerce first.
            const m = _scoutNum(mate, 0);
            evalText = `M${Math.abs(m)}`;
            pct = m > 0 ? 100 : 0;
            barLabel.className = 'eval-bar-label ' + (m > 0 ? 'white-winning white-lead' : 'black-winning black-lead');
        } else if (scoreCp !== null && scoreCp !== undefined) {
            // SEC-M: the logistic curve turns a non-numeric `scoreCp` into NaN, so
            // `style.height` was set to the invalid "NaN%" (the browser then silently
            // keeps the previous height) and the label read "NaN".
            const cp = _scoutNum(scoreCp, 0);
            const pawnEval = (cp / 100).toFixed(1);
            evalText = (cp > 0 ? `+${pawnEval}` : `${pawnEval}`);
            pct = _scoutNum(50 + 50 * (2 / (1 + Math.exp(-0.004 * cp)) - 1), 50);
            pct = Math.max(5, Math.min(95, pct));

            barLabel.className = 'eval-bar-label ' + (
                cp > 100 ? 'white-winning white-lead' :
                cp < -100 ? 'black-winning black-lead' : 'equal white-lead'
            );
        }

        if (this.orientation === 'black') {
            pct = 100 - pct;
        }

        barFill.style.height = `${pct}%`;
        barLabel.textContent = evalText;
    }

    async trainThisOpening() {
        const openingName = this.activeStudy?.opening_name ||
                           (document.getElementById('scoutStudioOpeningName')?.textContent || '').trim() ||
                           'Chess Opening';
        const eco = this.activeStudy?.eco || '';

        try {
            await fetch('/api/repertoire/train/recent_opening', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: openingName, eco: eco })
            });
        } catch (e) {}

        // CODE-01: the training colour follows the repertoire the user is
        // working in, never the cosmetic board orientation.
        const trainColor = this._lineStudyRepertoire(this.lineTree.getActiveLine());

        if (window.app && typeof window.app.showTab === 'function') {
            window.app.showTab('rep-trainer');
        } else {
            document.querySelectorAll('.nav-tab').forEach(t => {
                if (t.dataset.tab === 'rep-trainer') t.click();
            });
        }

        if (window.repertoireTrainer) {
            window.repertoireTrainer.setOpeningFocus(openingName, trainColor);
        }
    }

    /** §8.1: every notice goes through the floating toast system, never the DOM flow. */
    _showQuickAddNotice(text, color = '#4ade80', duration) {
        if (typeof window.showToast === 'function') {
            if (typeof duration === 'number') window.showToast(text, color, duration);
            else window.showToast(text, color);
        }
    }
}

// Global initialization
window.videoScout = null;
document.addEventListener('DOMContentLoaded', () => {
    window.videoScout = new OpeningScout();
    window.openingScout = window.videoScout;
});
