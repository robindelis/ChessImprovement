#!/usr/bin/env node
/* CODE-06 — re-runnable static verification (see audit/reports/CODE-06.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-06_overlap_race.js
 */
'use strict';
process.argv[2] = 'CODE-06';
require('./CODE-3_static_probe.js');
