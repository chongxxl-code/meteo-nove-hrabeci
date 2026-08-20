#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAV = '<link rel="icon" href="./favicon.svg" type="image/svg+xml">'
ABOUT = '<a class="action secondary" href="./about.html">↗ Jak to funguje</a>'


def add_favicon(text: str) -> str:
    if 'href="./favicon.svg"' in text:
        return text
    if '<meta name="theme-color"' in text:
        i = text.find('<meta name="theme-color"')
        j = text.find('>', i)
        if j >= 0:
            return text[:j+1] + FAV + text[j+1:]
    if '</title>' in text:
        return text.replace('</title>', '</title>' + FAV, 1)
    return text


def main():
    changed = []
    for path in sorted(ROOT.glob('*.html')):
        text = path.read_text(encoding='utf-8')
        new = add_favicon(text)
        if path.name == 'index.html' and 'href="./about.html"' not in new:
            anchor = '<a class="action secondary" href="./knowledge.html">↗ Co jsme se naučili</a>'
            if anchor in new:
                new = new.replace(anchor, anchor + ABOUT, 1)
            else:
                marker = '</div><div class="note" id="landnote">'
                if marker in new:
                    new = new.replace(marker, ABOUT + marker, 1)
        if new != text:
            path.write_text(new, encoding='utf-8')
            changed.append(path.name)
    print('changed:', ', '.join(changed) if changed else 'none')

if __name__ == '__main__':
    main()
