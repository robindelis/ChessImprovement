#!/usr/bin/env bash
# CODE-4: cross-check every getElementById() in the CODE-4 scope against
# frontend/index.html, and list index.html ids referenced by no JS file.
# Read-only, idempotent, no DB / no network.
set -uo pipefail
cd "$(dirname "$0")/../.."

PY=/home/robin/miniconda3/bin/python3

$PY - <<'EOF'
import re

html = open('frontend/index.html').read()
ids = set(re.findall(r'id="([^"]+)"', html))
scope = ['frontend/scout.js', 'frontend/repertoire_trainer.js',
         'frontend/app.js', 'frontend/audio.js']
allsrc = ''.join(open(f).read() for f in scope + ['frontend/repertoire_builder.js'])

print('== ids in index.html:', len(ids))
for f in scope:
    used = set(re.findall(r"getElementById\('([^']+)'\)", open(f).read()))
    missing = sorted(used - ids)
    print('== %-34s getElementById=%-3d  MISSING=%s' % (f, len(used), missing or '-'))

unused = sorted(i for i in ids
                if ("'%s'" % i) not in allsrc and ('"%s"' % i) not in allsrc)
print('\n== ids in index.html referenced by NO js file (%d):' % len(unused))
for i in unused:
    print('   ', i)

print('\n== css classes emitted by JS with no rule in style.css')
css = open('frontend/style.css').read()
for cls in ['scout-badge-youtube', 'scout-badge-guide', 'badge-eco',
            'scout-overlap-banner', 'scout-line-study', 'scout-note-bar',
            'scout-header-titles', 'scout-header-actions']:
    print('   %-26s %s' % (cls, 'FOUND' if '.' + cls in css else 'MISSING'))
print('   feedback-box.mistake     %s' %
      ('FOUND' if '.feedback-box.mistake' in css else 'MISSING'))
print('   toast-fadeout            %s' %
      ('FOUND' if 'toast-fadeout' in css else 'MISSING'))
EOF
