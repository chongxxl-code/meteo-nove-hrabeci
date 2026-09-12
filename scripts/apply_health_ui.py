#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / 'index.html'

HEALTH_TAG = '<script src="./health-ui.js?v=1" defer></script>'
HEALTH_ANCHOR = '<script src="./radar-nowcast-ui.js?v=2" defer></script>'

CALIBRATION_TAG = '<a class="action secondary" href="./calibration-lab.html">↗ Kalibrační laboratoř</a>'
CALIBRATION_ANCHOR = '<a class="action secondary" href="./validation.html">↗ Otevřít časovou řadu / ověřování</a>'

text = INDEX.read_text(encoding='utf-8')
changed = False

if HEALTH_TAG not in text:
    if HEALTH_ANCHOR not in text:
        raise SystemExit('Expected radar UI script anchor not found in index.html')
    text = text.replace(HEALTH_ANCHOR, HEALTH_ANCHOR + HEALTH_TAG, 1)
    changed = True
    print('health-ui.js inserted into index.html')
else:
    print('health-ui.js already present')

if CALIBRATION_TAG not in text:
    if CALIBRATION_ANCHOR not in text:
        raise SystemExit('Expected validation link anchor not found in index.html')
    text = text.replace(CALIBRATION_ANCHOR, CALIBRATION_ANCHOR + CALIBRATION_TAG, 1)
    changed = True
    print('calibration lab link inserted into index.html')
else:
    print('calibration lab link already present')

if changed:
    INDEX.write_text(text, encoding='utf-8')
