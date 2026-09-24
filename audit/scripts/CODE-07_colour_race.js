#!/usr/bin/env node
/* CODE-07 — re-runnable static verification (see audit/reports/CODE-07.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-07_colour_race.js
 */
'use strict';
process.argv[2] = 'CODE-07';
require('./CODE-3_static_probe.js');
