#!/usr/bin/env node
/* CODE-09 — re-runnable static verification (see audit/reports/CODE-09.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-09_unhandled_rejection.js
 */
'use strict';
process.argv[2] = 'CODE-09';
require('./CODE-3_static_probe.js');
