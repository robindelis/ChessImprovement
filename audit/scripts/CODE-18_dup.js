#!/usr/bin/env node
/* CODE-18 — re-runnable static verification (see audit/reports/CODE-18.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-18_dup.js
 */
'use strict';
process.argv[2] = 'CODE-18';
require('./CODE-3_static_probe.js');
