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

if changed:
    p.write_text(s, encoding='utf-8')
    print('push client and OneSignal SDK removed from index.html')
else:
    print('push integration already absent from index.html')
