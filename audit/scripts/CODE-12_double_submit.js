#!/usr/bin/env node
/* CODE-12 — re-runnable static verification (see audit/reports/CODE-12.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-12_double_submit.js
 */
'use strict';
process.argv[2] = 'CODE-12';
require('./CODE-3_static_probe.js');
