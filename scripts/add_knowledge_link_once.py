#!/usr/bin/env python3
from pathlib import Path

p = Path('index.html')
s = p.read_text(encoding='utf-8')
needle = '<a class="action secondary" href="./validation.html">↗ Otevřít časovou řadu / ověřování</a>'
insert = needle + '<a class="action secondary" href="./knowledge.html">↗ Co jsme se naučili</a>'
if 'href="./knowledge.html"' in s:
    print('Knowledge link already present.')
elif needle not in s:
    raise SystemExit('Expected validation navigation link not found.')
else:
    p.write_text(s.replace(needle, insert, 1), encoding='utf-8')
    print('Knowledge link added.')
