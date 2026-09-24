#!/usr/bin/env node
/* CODE-17 — re-runnable static verification (see audit/reports/CODE-17.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-17_dead_selectors.js
 */
'use strict';
process.argv[2] = 'CODE-17';
require('./CODE-3_static_probe.js');
