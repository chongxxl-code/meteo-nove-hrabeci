#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = ROOT / 'index.html'
s = p.read_text(encoding='utf-8')

sdk_tag = '<script src="https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.page.js" defer></script>'
client_tag = '<script src="./push-client.js" defer></script>'
changed = False

if sdk_tag not in s:
    if '</head>' not in s:
        raise SystemExit('index.html has no </head> marker')
    s = s.replace('</head>', sdk_tag + '</head>', 1)
    changed = True
    print('OneSignal SDK injected into head')

if client_tag not in s:
    if '</body>' not in s:
        raise SystemExit('index.html has no </body> marker')
    s = s.replace('</body>', client_tag + '</body>', 1)
    changed = True
    print('push client injected')

if changed:
    p.write_text(s, encoding='utf-8')
else:
    print('OneSignal SDK and push client already present')
