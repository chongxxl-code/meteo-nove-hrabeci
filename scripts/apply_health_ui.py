#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / 'index.html'
TAG = '<script src="./health-ui.js?v=1" defer></script>'
ANCHOR = '<script src="./radar-nowcast-ui.js?v=2" defer></script>'

text = INDEX.read_text(encoding='utf-8')
if TAG not in text:
    if ANCHOR not in text:
        raise SystemExit('Expected radar UI script anchor not found in index.html')
    text = text.replace(ANCHOR, ANCHOR + TAG, 1)
    INDEX.write_text(text, encoding='utf-8')
    print('health-ui.js inserted into index.html')
else:
    print('health-ui.js already present')
