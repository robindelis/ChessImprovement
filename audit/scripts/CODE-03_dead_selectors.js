#!/usr/bin/env node
/* CODE-03 — re-runnable static verification (see audit/reports/CODE-03.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-03_dead_selectors.js
 */
'use strict';
process.argv[2] = 'CODE-03';
require('./CODE-3_static_probe.js');
