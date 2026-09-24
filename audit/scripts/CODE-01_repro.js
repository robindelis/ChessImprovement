#!/usr/bin/env node
/* CODE-01 — re-runnable static verification (see audit/reports/CODE-01.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-01_repro.js
 */
'use strict';
process.argv[2] = 'CODE-01';
require('./CODE-3_static_probe.js');
