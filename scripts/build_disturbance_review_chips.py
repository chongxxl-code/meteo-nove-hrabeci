#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image, ImageDraw
from rasterio.enums import Resampling
from rasterio.windows import from_bounds
from rasterio.warp import transform

from analyze_sentinel_zones import ROOT, EARTH_SEARCH, post_json, asset_info, scale_offset, reproject_to_window

DIST_FILE = ROOT / 'data' / 'validation' / 'open-land-disturbance-2026.json'
EXP_FILE = ROOT / 'data' / 'validation' / 'open-land-experiment-2026.json'
NETWORK_FILE = ROOT / 'config' / 'open-land-experimental.geojson'
OUT_DIR = ROOT / 'data' / 'validation' / 'disturbance-chips'
MANIFEST = ROOT / 'data' / 'validation' / 'disturbance-review-manifest.json'
STATUS = ROOT / 'data' / 'validation' / 'disturbance-review-status.json'

SIZE = 320
CHIP_HALF_M = 150.0
STRETCH_MIN = 0.02
STRETCH_MAX = 0.34

os.environ.setdefault('GDAL_DISABLE_READDIR_ON_OPEN', 'EMPTY_DIR')
os.environ.setdefault('CPL_VSIL_CURL_ALLOWED_EXTENSIONS', '.tif,.TIF')
os.environ.setdefault('GDAL_HTTP_MULTIRANGE', 'YES')
os.environ.setdefault('AWS_NO_SIGN_REQUEST', 'YES')


def scene(scene_id: str) -> dict:
    d = post_json(EARTH_SEARCH, {'collections':['sentinel-2-c1-l2a'],'ids':[scene_id],'limit':1})
    fs = d.get('features') or []
    if not fs:
        raise RuntimeError(f'Scene not found: {scene_id}')
    return fs[0]


def safe(s: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_-]+','-',s).strip('-').lower()


def center_and_size(feature: dict) -> tuple[float,float,float]:
    p=feature.get('properties') or {}
    lon=p.get('center_lon'); lat=p.get('center_lat')
    if lon is None or lat is None:
        pts=((feature.get('geometry') or {}).get('coordinates') or [[]])[0][:-1]
        lon=sum(float(q[0]) for q in pts)/len(pts); lat=sum(float(q[1]) for q in pts)/len(pts)
    sample=float(p.get('sample_square_m') or 75.0)
    return float(lon),float(lat),sample


def reflectance(arr: np.ndarray, asset: dict) -> np.ndarray:
    scale,off=scale_offset(asset)
    x=arr.astype('float32')
    x=x*scale+off
    return x


def render_chip(item: dict, lon: float, lat: float, sample_m: float, title: str, outfile: Path) -> None:
    red_a=asset_info(item,'red'); green_a=asset_info(item,'green'); blue_a=asset_info(item,'blue')
    with rasterio.Env(AWS_NO_SIGN_REQUEST='YES'):
        with rasterio.open(red_a['href']) as ref, rasterio.open(green_a['href']) as gs, rasterio.open(blue_a['href']) as bs:
            xs,ys=transform('EPSG:4326',ref.crs,[lon],[lat]); x=float(xs[0]); y=float(ys[0])
            win=from_bounds(x-CHIP_HALF_M,y-CHIP_HALF_M,x+CHIP_HALF_M,y+CHIP_HALF_M,transform=ref.transform)
            r=ref.read(1,window=win,out_shape=(SIZE,SIZE),resampling=Resampling.bilinear).astype('float32')
            tr=ref.window_transform(win)
            g=reproject_to_window(gs,(SIZE,SIZE),tr,ref.crs,Resampling.bilinear)
            b=reproject_to_window(bs,(SIZE,SIZE),tr,ref.crs,Resampling.bilinear)
            r=reflectance(r,red_a); g=reflectance(g,green_a); b=reflectance(b,blue_a)
            rgb=np.dstack([r,g,b])
            rgb=np.clip((rgb-STRETCH_MIN)/(STRETCH_MAX-STRETCH_MIN),0,1)
            rgb=np.power(rgb,0.82)
            out=(rgb*255).astype('uint8')
            im=Image.fromarray(out,'RGB')
            dr=ImageDraw.Draw(im)
            side=max(8,int(round(sample_m/(CHIP_HALF_M*2)*SIZE)))
            x0=(SIZE-side)//2; y0=(SIZE-side)//2; x1=x0+side; y1=y0+side
            dr.rectangle([x0,y0,x1,y1],outline=(255,90,70),width=3)
            dr.rectangle([0,0,SIZE,25],fill=(5,12,16,215))
            dr.text((7,6),title,fill=(245,250,252))
            outfile.parent.mkdir(parents=True,exist_ok=True)
            im.save(outfile,optimize=True)


def ndvi_lookup(exp: dict) -> dict[tuple[str,str],float|None]:
    out={}
    for rec in exp.get('scenes') or []:
        sid=rec.get('scene_id')
        for z in rec.get('zones') or []:
            out[(sid,z.get('id'))]=((z.get('ndvi') or {}).get('median')) if z.get('ok') else None
    return out


def next_valid_scene(exp: dict, sample_id: str, event_scene_id: str) -> str|None:
    scenes=exp.get('scenes') or []
    seen=False
    for rec in scenes:
        sid=rec.get('scene_id')
        if sid==event_scene_id:
            seen=True; continue
        if not seen: continue
        z=next((z for z in rec.get('zones') or [] if z.get('id')==sample_id),None)
        if z and z.get('ok') and (z.get('ndvi') or {}).get('median') is not None:
            return sid
    return None


def main():
    dist=json.loads(DIST_FILE.read_text(encoding='utf-8'))
    exp=json.loads(EXP_FILE.read_text(encoding='utf-8'))
    net=json.loads(NETWORK_FILE.read_text(encoding='utf-8'))
    features={(f.get('properties') or {}).get('id'):f for f in net.get('features') or []}
    ndvi=ndvi_lookup(exp)
    scene_dt={r.get('scene_id'):r.get('scene_datetime') for r in exp.get('scenes') or []}
    events=[]; cache={}; errors=[]
    for i,e in enumerate(dist.get('events') or [],start=1):
        sample_id=e.get('sample_id'); feat=features.get(sample_id)
        if not feat:
            errors.append({'sample_id':sample_id,'error':'sample not found in network'}); continue
        label=e.get('label') or sample_id; lon,lat,sample_m=center_and_size(feat)
        after=(e.get('recovery_within_2_scenes') or {}).get('scene_id') or next_valid_scene(exp,sample_id,e.get('event_scene_id'))
        stages=[('before',e.get('previous_scene_id')),('event',e.get('event_scene_id')),('after',after)]
        stage_rows=[]
        for stage,sid in stages:
            if not sid: continue
            try:
                item=cache.get(sid)
                if item is None:
                    item=scene(sid); cache[sid]=item
                dt=scene_dt.get(sid) or (item.get('properties') or {}).get('datetime')
                d=datetime.fromisoformat(dt.replace('Z','+00:00')).date().isoformat() if dt else 'unknown'
                fn=f'{safe(label)}-{safe(e.get("event_scene_id") or "event")}-{stage}.png'
                rel=f'data/validation/disturbance-chips/{fn}'
                render_chip(item,lon,lat,sample_m,f'{label} · {stage} · {d}',ROOT/rel)
                stage_rows.append({'stage':stage,'scene_id':sid,'date':d,'ndvi':ndvi.get((sid,sample_id)),'image':rel})
            except Exception as exc:
                errors.append({'sample_id':sample_id,'scene_id':sid,'stage':stage,'error':str(exc)})
        events.append({'event_index':i,'sample_id':sample_id,'label':label,'role':e.get('role'),'severity':e.get('severity'),'delta_ndvi':e.get('delta_ndvi'),'classification':e.get('classification'),'stages':stage_rows})
    manifest={'ok':bool(events),'quality_status':'historical_rgb_review_ready' if not errors else 'historical_rgb_review_partial','generated_at_local':datetime.now().astimezone().isoformat(),'chip_size_px':SIZE,'chip_width_m':CHIP_HALF_M*2,'fixed_rgb_stretch':[STRETCH_MIN,STRETCH_MAX],'event_count':len(events),'errors':errors,'events':events,'warning':'True-colour Sentinel chips are visual evidence only. A visible vegetation change can support but does not prove mowing/grazing.'}
    MANIFEST.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    STATUS.write_text(json.dumps({'ok':bool(events),'quality_status':manifest['quality_status'],'event_count':len(events),'image_count':sum(len(x['stages']) for x in events),'error_count':len(errors),'output_file':str(MANIFEST.relative_to(ROOT)).replace('\\','/'),'next_step':'Review disturbance-review.html; classify only visually convincing events as likely management, otherwise leave unknown.'},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(json.loads(STATUS.read_text(encoding='utf-8')),ensure_ascii=False))

if __name__=='__main__':
    main()
