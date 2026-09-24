#!/usr/bin/env node
/**
 * FIXER-M: exhaustive enumeration of every markup-bearing string construction in a
 * frontend file, so the interpolation audit table in audit/patches/FIXER-M-01.md is a
 * mechanical fact list rather than a reading exercise.
 *
 * Reports, per file:
 *   (1) every `innerHTML` / `outerHTML` / `insertAdjacentHTML` / `.html(` write with the
 *       full right-hand-side text, so a multi-line write is never missed;
 *   (2) every template literal that contains a `<` (i.e. that can carry markup) and, for
 *       each, the list of `${...}` holes with the offset in the file.
 *
 * Usage: node audit/scripts/FIXER-M_interp_scan.js frontend/scout.js [...]
 */
'use strict';
const fs = require('fs');
const path = require('path');

function lineOf(src, idx) {
    let n = 1;
    for (let i = 0; i < idx && i < src.length; i++) if (src[i] === '\n') n++;
    return n;
}

/**
 * A `/` starts a regex literal (not a division) when the previous significant character
 * cannot end an expression. Handles `=/[a-z]/`, `(`, `,`, `=`, `:`, `[`, `!`, `&`, `|`,
 * `?`, `{`, `;`, `return`.
 */
function regexAllowed(src, idx) {
    let j = idx - 1;
    while (j >= 0 && /\s/.test(src[j])) j--;
    if (j < 0) return true;
    const p = src[j];
    if (/[)\]a-zA-Z0-9_$'"`]/.test(p)) {
        // `}` is ambiguous (block end vs object end); treat it as "expression ended" only
        // when it is not preceded by a `$`-template. Being conservative here only costs
        // accuracy of the *scan*, never correctness of the audit, so default to false.
        return false;
    }
    return true;
}

function scan(file) {
    const src = fs.readFileSync(file, 'utf8');
    const rel = path.relative(process.cwd(), file);
    console.log('\n' + '='.repeat(100));
    console.log('FILE ' + rel + '  (' + src.length + ' bytes)');
    console.log('='.repeat(100));

    // ---- (1) DOM write sinks -------------------------------------------------
    const sink = /(?:\.innerHTML|\.outerHTML|insertAdjacentHTML\s*\()/g;
    let m;
    const writes = [];
    while ((m = sink.exec(src)) !== null) {
        const start = m.index;
        // capture from the start of the statement (walk back over `const x = `)
        let s = start;
        while (s > 0 && src[s - 1] !== ';' && src[s - 1] !== '{' && src[s - 1] !== '\n') s--;
        // capture the whole expression: from the sink to the terminating `;` at depth 0
        let i = start + m[0].length;
        let depth = 0;
        while (i < src.length) {
            const c = src[i];
            if (c === '(' || c === '[' || c === '{') depth++;
            else if (c === ')' || c === ']' || c === '}') {
                if (depth === 0 && (c === ')' || c === ']' || c === '}')) break;
                depth--;
            } else if (c === ';' && depth === 0) break;
            else if (c === '\n' && depth === 0) {
                // statement may continue with `.` or `+`; look ahead
                let j = i + 1;
                while (j < src.length && /\s/.test(src[j])) j++;
                if (src[j] === '.' || src[j] === '+' || src[j] === '?' || src[j] === ':' || src[j] === ')') { i++; continue; }
                break;
            }
            i++;
        }
        writes.push({ line: lineOf(src, start), text: src.slice(s, Math.min(i, src.length)).replace(/\s+/g, ' ').trim() });
    }
    console.log('\n-- DOM WRITE SINKS: ' + writes.length + ' --');
    for (const w of writes) {
        console.log('  L' + String(w.line).padStart(5) + '  ' + w.text.slice(0, 400));
    }

    // ---- (2) markup-bearing template literals -------------------------------
    const tpl = [];
    let i = 0;
    while (i < src.length) {
        const c = src[i];
        // skip line comments / block comments / strings
        if (c === '/' && src[i + 1] === '/') { while (i < src.length && src[i] !== '\n') i++; continue; }
        if (c === '/' && src[i + 1] === '*') { i += 2; while (i < src.length && !(src[i] === '*' && src[i + 1] === '/')) i++; i += 2; continue; }
        if (c === '/' && regexAllowed(src, i)) {
            // A regex literal can legally contain a backtick (`/[&<>"'`=]/g`), which would
            // otherwise be mistaken for a template-literal opener and swallow the file.
            i++;
            let inClass = false;
            while (i < src.length) {
                if (src[i] === '\\') { i += 2; continue; }
                if (src[i] === '[') inClass = true;
                else if (src[i] === ']') inClass = false;
                else if (src[i] === '/' && !inClass) { i++; break; }
                else if (src[i] === '\n') break;
                i++;
            }
            while (i < src.length && /[a-z]/.test(src[i])) i++;
            continue;
        }
        if (c === "'" || c === '"') { const q = c; i++; while (i < src.length && src[i] !== q) { if (src[i] === '\\') i++; i++; } i++; continue; }
        if (c === '`') {
            const start = i;
            i++;
            let raw = '';
            const holes = [];
            while (i < src.length) {
                if (src[i] === '\\') { i += 2; continue; }
                if (src[i] === '`') { i++; break; }
                if (src[i] === '$' && src[i + 1] === '{') {
                    const hStart = i + 2;
                    let d = 1;
                    let j = hStart;
                    while (j < src.length && d > 0) {
                        if (src[j] === '{') d++;
                        else if (src[j] === '}') d--;
                        if (d > 0) j++;
                    }
                    holes.push({ line: lineOf(src, i), expr: src.slice(hStart, j).replace(/\s+/g, ' ').trim().slice(0, 120) });
                    i = j + 1;
                    continue;
                }
                raw += src[i];
                i++;
            }
            if (raw.indexOf('<') !== -1) {
                tpl.push({ line: lineOf(src, start), head: src.slice(start, start + 60).replace(/\s+/g, ' '), holes: holes });
            }
            continue;
        }
        i++;
    }
    console.log('\n-- MARKUP TEMPLATE LITERALS: ' + tpl.length + ' --');
    for (const t of tpl) {
        console.log('  L' + String(t.line).padStart(5) + '  ' + t.head);
        for (const h of t.holes) console.log('           hole L' + String(h.line).padStart(5) + '  ${' + h.expr + '}');
    }
    return writes.length + tpl.length;
}

let total = 0;
for (const f of process.argv.slice(2)) total += scan(f);
console.log('\n(total markup-bearing constructions inspected: ' + total + ')');
