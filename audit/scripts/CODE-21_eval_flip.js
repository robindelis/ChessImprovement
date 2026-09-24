#!/usr/bin/env node
/* CODE-21 — re-runnable static verification (see audit/reports/CODE-21.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-21_eval_flip.js
 */
'use strict';
process.argv[2] = 'CODE-21';
require('./CODE-3_static_probe.js');
