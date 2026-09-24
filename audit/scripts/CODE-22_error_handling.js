#!/usr/bin/env node
/* CODE-22 — re-runnable static verification (see audit/reports/CODE-22.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-22_error_handling.js
 */
'use strict';
process.argv[2] = 'CODE-22';
require('./CODE-3_static_probe.js');
