#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import re
import time
import urllib.request
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Prague')
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
OBS = DATA / 'observations'
URL = 'https://hydro.chmi.cz/hpps/srz/objekt/20189995'
UA = 'nove-hrabeci-meteo/0.7 (+github-actions)'


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        elif tag in ('td', 'th') and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None and self.row is not None:
            txt = ' '.join(''.join(self.cell).replace('\xa0', ' ').split())
            self.row.append(txt)
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None
            self.cell = None


def get_html(tries=3):
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(URL, headers={'User-Agent': UA, 'Accept': 'text/html,*/*'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode('utf-8', errors='replace')
        except Exception as exc:
            err = exc
            if i + 1 < tries:
                time.sleep(2 + i * 2)
    raise RuntimeError(f'ČHMÚ fetch failed: {err}')


def num(s):
    s = (s or '').strip().replace(',', '.')
    if not s or s in ('-', '—'):
        return None
    try:
        return float(s)
    except Exception:
        return None


def parse_date(text, now):
    m = re.search(r'(\d{1,2})\.\s*(\d{1,2})\.', text)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    year = now.year
    candidate = datetime(year, month, day, tzinfo=TZ)
    if candidate - now > timedelta(days=180):
        candidate = candidate.replace(year=year - 1)
    elif now - candidate > timedelta(days=180):
        candidate = candidate.replace(year=year + 1)
    return candidate.date()


def main():
    DATA.mkdir(exist_ok=True)
    OBS.mkdir(exist_ok=True)
    raw = get_html()
    parser = TableParser()
    parser.feed(raw)
    now = datetime.now(TZ)

    date_row = None
    for row in parser.rows:
        if sum(bool(re.search(r'\d{1,2}\.\s*\d{1,2}\.', c)) for c in row) >= 2:
            date_row = row
            break
    if not date_row:
        raise RuntimeError(f'ČHMÚ table date header not found; parsed_rows={len(parser.rows)}')

    dates = [parse_date(c, now) for c in date_row]
    dates = [d for d in dates if d is not None]
    if not dates:
        raise RuntimeError('ČHMÚ current date not parsed')
    current_date = dates[0]

    points = []
    for row in parser.rows:
        if len(row) < 2:
            continue
        label = row[0].strip()
        if not re.fullmatch(r'\d{1,2}:\d{2}', label):
            continue
        value = num(row[1])
        if value is None:
            continue
        hh, mm = map(int, label.split(':'))
        if hh == 24:
            dt = datetime.combine(current_date + timedelta(days=1), datetime.min.time(), TZ)
        else:
            dt = datetime(current_date.year, current_date.month, current_date.day, hh, mm, tzinfo=TZ)
        if dt <= now + timedelta(minutes=10):
            points.append((dt, value))

    if not points:
        preview = parser.rows[:8]
        raise RuntimeError(f'No current-day ČHMÚ 10-minute precipitation values found; date_row={date_row!r}; rows_preview={preview!r}')
    points.sort(key=lambda x: x[0])
    latest_dt, latest_val = points[-1]
    last_hour_start = latest_dt - timedelta(minutes=50)
    hour_points = [(dt, v) for dt, v in points if last_hour_start <= dt <= latest_dt]
    rain_60 = round(sum(v for _, v in hour_points), 3)

    text = ' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', raw)).replace('\xa0', ' ').split())
    state = None
    m = re.search(r'Aktuální data\s*-\s*(.{1,60}?)(?:Název stanice|Detail stanice|$)', text, re.I)
    if m:
        state = ' '.join(m.group(1).split())[:60]
    if not state:
        if 'bez deště' in text.lower():
            state = 'bez deště'
        elif 'déšť' in text.lower():
            state = 'déšť'

    payload = {
        'ok': True,
        'provider': 'ČHMÚ HPPS',
        'station_name': 'Šluknov',
        'station_id': 'U2SLUK01',
        'station_page_id': 20189995,
        'elevation_m': 352,
        'source_url': URL,
        'collected_at_local': now.isoformat(),
        'observed_at_local': latest_dt.isoformat(),
        'age_minutes': round((now - latest_dt).total_seconds() / 60, 1),
        'precipitation_10m_mm': latest_val,
        'precipitation_last_60m_mm': rain_60,
        'samples_last_60m': len(hour_points),
        'current_state': state,
    }

    status = DATA / 'chmi-status.json'
    status.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    archive = OBS / f'chmi-sluknov-{now:%Y-%m}.jsonl'
    with archive.open('a', encoding='utf-8') as f:
        f.write(json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n')
    print(json.dumps(payload, ensure_ascii=False))


def safe_main():
    try:
        main()
    except Exception as exc:
        DATA.mkdir(exist_ok=True)
        now = datetime.now(TZ)
        payload = {
            'ok': False,
            'provider': 'ČHMÚ HPPS',
            'station_name': 'Šluknov',
            'station_id': 'U2SLUK01',
            'source_url': URL,
            'collected_at_local': now.isoformat(),
            'error': str(exc),
        }
        (DATA / 'chmi-status.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(payload, ensure_ascii=False))


if __name__ == '__main__':
    safe_main()
