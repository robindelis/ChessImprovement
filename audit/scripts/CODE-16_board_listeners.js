#!/usr/bin/env node
/* CODE-16 — re-runnable static verification (see audit/reports/CODE-16.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-16_board_listeners.js
 */
'use strict';
process.argv[2] = 'CODE-16';
require('./CODE-3_static_probe.js');
