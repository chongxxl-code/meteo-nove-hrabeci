from pathlib import Path

p = Path('index.html')
s = p.read_text(encoding='utf-8')
needle = '<a class="action secondary" href="./zones.html">↗ Otevřít satelitní krajinné zóny</a>'
addition = '<a class="action secondary" href="./predisposition.html">↗ Otevřít mapu predispozic</a>'
if addition not in s:
    if needle not in s:
        raise SystemExit('Landscape navigation anchor not found')
    s = s.replace(needle, needle + addition, 1)
    p.write_text(s, encoding='utf-8')
    print('Patched index.html')
else:
    print('Predisposition link already present')
