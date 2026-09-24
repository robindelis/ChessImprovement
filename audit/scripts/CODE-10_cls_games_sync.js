#!/usr/bin/env node
/* CODE-10 — re-runnable static verification (see audit/reports/CODE-10.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-10_cls_games_sync.js
 */
'use strict';
process.argv[2] = 'CODE-10';
require('./CODE-3_static_probe.js');
