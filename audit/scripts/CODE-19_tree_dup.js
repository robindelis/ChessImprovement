#!/usr/bin/env node
/* CODE-19 — re-runnable static verification (see audit/reports/CODE-19.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-19_tree_dup.js
 */
'use strict';
process.argv[2] = 'CODE-19';
require('./CODE-3_static_probe.js');
