#!/usr/bin/env node
/* CODE-08 — re-runnable static verification (see audit/reports/CODE-08.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-08_eval_cache.js
 */
'use strict';
process.argv[2] = 'CODE-08';
require('./CODE-3_static_probe.js');
