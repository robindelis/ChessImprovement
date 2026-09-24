#!/usr/bin/env node
/* CODE-04 — re-runnable static verification (see audit/reports/CODE-04.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-04_sync_abort_race.js
 */
'use strict';
process.argv[2] = 'CODE-04';
require('./CODE-3_static_probe.js');
