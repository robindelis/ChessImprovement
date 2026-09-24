#!/usr/bin/env node
/* CODE-14 — re-runnable static verification (see audit/reports/CODE-14.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-14_arrow_validation.js
 */
'use strict';
process.argv[2] = 'CODE-14';
require('./CODE-3_static_probe.js');
