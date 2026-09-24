#!/usr/bin/env node
/* CODE-15 — re-runnable static verification (see audit/reports/CODE-15.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-15_marker_id_collision.js
 */
'use strict';
process.argv[2] = 'CODE-15';
require('./CODE-3_static_probe.js');
