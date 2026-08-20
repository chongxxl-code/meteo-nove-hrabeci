#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = ROOT / 'index.html'
s = p.read_text(encoding='utf-8')

remove = [
    '<script src="https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.page.js" defer></script>',
    '<script src="./push-client.js" defer></script>',
    '<script src="./push-client.js"></script>',
]
changed = False
for tag in remove:
    if tag in s:
        s = s.replace(tag, '')
        changed = True

ui_tag = '<link rel="stylesheet" href="./ui-tune.css?v=1">'
if ui_tag not in s:
    if '</head>' not in s:
        raise SystemExit('index.html has no </head> marker')
    s = s.replace('</head>', ui_tag + '</head>', 1)
    changed = True
    print('UI polish stylesheet attached')

if changed:
    p.write_text(s, encoding='utf-8')
    print('index.html updated')
else:
    print('push integration absent and UI polish already attached')
