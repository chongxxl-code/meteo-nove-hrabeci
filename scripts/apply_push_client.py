#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = ROOT / 'index.html'
s = p.read_text(encoding='utf-8')
tag = '<script src="./push-client.js" defer></script>'
if tag not in s:
    if '</body>' not in s:
        raise SystemExit('index.html has no </body> marker')
    s = s.replace('</body>', tag + '</body>', 1)
    p.write_text(s, encoding='utf-8')
    print('push client injected')
else:
    print('push client already present')
