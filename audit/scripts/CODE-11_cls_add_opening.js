#!/usr/bin/env node
/* CODE-11 — re-runnable static verification (see audit/reports/CODE-11.md).
 * Delegates to the shared implementation so every finding can be
 * re-verified independently:  node audit/scripts/CODE-11_cls_add_opening.js
 */
'use strict';
process.argv[2] = 'CODE-11';
require('./CODE-3_static_probe.js');
