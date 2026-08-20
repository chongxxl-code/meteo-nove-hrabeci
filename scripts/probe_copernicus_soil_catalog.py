#!/usr/bin/env python3
from __future__ import annotations

import csv, io, json, re, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'data'/'copernicus-soil-probe.json'
UA='nove-hrabeci-local-observatory/1.0'
CATALOGS={
 'ssm':'https://s3.waw3-1.cloudferro.com/swift/v1/CatalogueCSV/bio-geophysical/surface_soil_moisture/ssm_europe_1km_daily_v1/ssm_europe_1km_daily_v1_cog.csv',
 'swi':'https://s3.waw3-1.cloudferro.com/swift/v1/CatalogueCSV/bio-geophysical/soil_water_index/swi_europe_1km_daily_v2/swi_europe_1km_daily_v2_cog.csv',
}

def get(url):
    req=urllib.request.Request(url,headers={'User-Agent':UA})
    with urllib.request.urlopen(req,timeout=45) as r:
        return r.read().decode('utf-8','replace')

def score_row(row):
    text=' '.join(str(v) for v in row.values())
    dates=re.findall(r'20\d{2}[-_/]?\d{2}[-_/]?\d{2}|20\d{6}',text)
    return max(dates) if dates else ''

def inspect(name,url):
    txt=get(url)
    r=csv.DictReader(io.StringIO(txt))
    rows=list(r)
    rows.sort(key=score_row)
    tail=rows[-3:] if rows else []
    return {
      'ok':bool(rows),
      'catalog_url':url,
      'fieldnames':r.fieldnames,
      'row_count':len(rows),
      'latest_rows':tail,
    }

def main():
    result={'computed_at_utc':datetime.now(timezone.utc).isoformat(),'products':{}}
    for name,url in CATALOGS.items():
        try: result['products'][name]=inspect(name,url)
        except Exception as e: result['products'][name]={'ok':False,'catalog_url':url,'error':str(e)[:300]}
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'ok':True,'products':{k:v.get('ok') for k,v in result['products'].items()}},ensure_ascii=False))

if __name__=='__main__': main()
