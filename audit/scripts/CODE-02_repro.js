#!/usr/bin/env node
/* CODE-02 — re-runnable static verification (see audit/reports/CODE-02.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-02_repro.js
 */
'use strict';
process.argv[2] = 'CODE-02';
require('./CODE-3_static_probe.js');
