#!/usr/bin/env node
/* CODE-13 — re-runnable static verification (see audit/reports/CODE-13.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-13_animation_cancel.js
 */
'use strict';
process.argv[2] = 'CODE-13';
require('./CODE-3_static_probe.js');
