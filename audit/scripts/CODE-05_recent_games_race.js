#!/usr/bin/env node
/* CODE-05 — re-runnable static verification (see audit/reports/CODE-05.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-05_recent_games_race.js
 */
'use strict';
process.argv[2] = 'CODE-05';
require('./CODE-3_static_probe.js');
