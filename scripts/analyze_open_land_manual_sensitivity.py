#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
NETWORK_FILE = ROOT / 'config' / 'open-land-experimental.geojson'
EXPERIMENT_FILE = ROOT / 'data' / 'validation' / 'open-land-experiment-2026.json'
REVIEW_FILE = ROOT / 'config' / 'disturbance-visual-review.json'
DISTURBANCE_FILE = ROOT / 'data' / 'validation' / 'open-land-disturbance-2026.json'
RAW_GRADIENT_FILE = ROOT / 'data' / 'validation' / 'open-land-gradient-status.json'
ALGO_SENSITIVITY_FILE = ROOT / 'data' / 'validation' / 'open-land-sensitivity-status.json'
OUT = ROOT / 'data' / 'validation' / 'open-land-manual-sensitivity-2026.json'
STATUS = ROOT / 'data' / 'validation' / 'open-land-manual-sensitivity-status.json'
TZ = ZoneInfo('Europe/Prague')


def finite(v):
    try: return math.isfinite(float(v))
    except Exception: return False


def pearson(xs, ys):
    p=[(float(x),float(y)) for x,y in zip(xs,ys) if finite(x) and finite(y)]
    if len(p)<4: return None
    x=np.array([a for a,b in p]); y=np.array([b for a,b in p])
    if np.std(x)<=1e-12 or np.std(y)<=1e-12: return None
    return float(np.corrcoef(x,y)[0,1])


def med(vals):
    v=[float(x) for x in vals if finite(x)]
    return float(median(v)) if v else None


def round4(v): return round(float(v),4) if finite(v) else None


def terrain_by_id(network):
    out={}
    for f in network.get('features') or []:
        p=f.get('properties') or {}
        aspect=p.get('dominant_aspect_deg'); coh=p.get('aspect_coherence'); slope=p.get('slope_deg')
        south=None
        if finite(aspect) and finite(coh) and finite(slope) and float(coh)>=0.65 and float(slope)>=4:
            south=math.cos(math.radians(float(aspect)-180.0))
        out[p.get('id')]={'twi':p.get('twi'),'tpi900':p.get('tpi_900m_m'),'elevation':p.get('elevation_m_bpv'),'slope':slope,'southness':south}
    return out


def scene_r(scene, terrain, predictor, index_name, excluded):
    xs=[]; ys=[]; used=0; excluded_count=0
    sid=scene.get('scene_id')
    for z in scene.get('zones') or []:
        if not z.get('ok'): continue
        zid=z.get('id')
        if (zid,sid) in excluded:
            excluded_count+=1; continue
        x=(terrain.get(zid) or {}).get(predictor)
        y=(z.get(index_name) or {}).get('median')
        if not finite(x) or not finite(y): continue
        xs.append(float(x)); ys.append(float(y)); used+=1
    return {'n':used,'excluded':excluded_count,'pearson_r':round4(pearson(xs,ys))}


def rain_mm(scene,days):
    d=scene.get(f'rain_{days}d') or {}
    return d.get('precipitation_mm') if d.get('quality')=='valid' else None


def summarize(rows,predictor,index_name,expected):
    vals=[]
    for r in rows:
        rr=((r.get(index_name) or {}).get(predictor) or {}).get('pearson_r')
        if finite(rr): vals.append(float(rr))
    if expected>0: aligned=sum(1 for x in vals if x>0)
    elif expected<0: aligned=sum(1 for x in vals if x<0)
    else: aligned=None
    out={'scene_count':len(vals),'median_pearson_r':round4(med(vals)),'expected_direction_fraction':round(aligned/len(vals),3) if vals and aligned is not None else None}
    for days in (7,14,30):
        xv=[]; yv=[]
        for r in rows:
            rr=((r.get(index_name) or {}).get(predictor) or {}).get('pearson_r')
            rain=rain_mm(r,days)
            if finite(rr) and finite(rain): xv.append(float(rain)); yv.append(float(rr))
        out[f'corr_rain_{days}d_vs_scene_pearson']=round4(pearson(xv,yv))
    return out


def confirmed_exclusions(review, disturbance):
    confirmed={(x.get('sample_id'), x.get('label')) for x in review.get('confirmed_change') or []}
    out=set(); details=[]
    for e in disturbance.get('events') or []:
        key=(e.get('sample_id'), e.get('label'))
        if key not in confirmed: continue
        sid=e.get('event_scene_id')
        if sid:
            out.add((e.get('sample_id'),sid))
            details.append({'sample_id':e.get('sample_id'),'label':e.get('label'),'scene_id':sid,'scene_datetime':e.get('event_datetime'),'delta_ndvi':e.get('delta_ndvi')})
    return out,details


def main():
    network=json.loads(NETWORK_FILE.read_text(encoding='utf-8'))
    exp=json.loads(EXPERIMENT_FILE.read_text(encoding='utf-8'))
    review=json.loads(REVIEW_FILE.read_text(encoding='utf-8'))
    dist=json.loads(DISTURBANCE_FILE.read_text(encoding='utf-8'))
    raw=json.loads(RAW_GRADIENT_FILE.read_text(encoding='utf-8'))
    algo=json.loads(ALGO_SENSITIVITY_FILE.read_text(encoding='utf-8')) if ALGO_SENSITIVITY_FILE.exists() else {}
    review_status=str(review.get('status') or '')
    if not review_status.startswith('complete_manual_review'):
        raise RuntimeError('Manual disturbance review is not complete.')
    terrain=terrain_by_id(network)
    excluded,details=confirmed_exclusions(review,dist)
    predictors=['twi','tpi900','elevation','slope','southness']
    expected={'twi':1,'tpi900':-1,'elevation':0,'slope':-1,'southness':-1}
    rows=[]
    for scene in exp.get('scenes') or []:
        row={'scene_id':scene.get('scene_id'),'scene_datetime':scene.get('scene_datetime'),'rain_7d':scene.get('rain_7d'),'rain_14d':scene.get('rain_14d'),'rain_30d':scene.get('rain_30d'),'ndmi':{},'ndvi':{}}
        for idx in ('ndmi','ndvi'):
            for p in predictors: row[idx][p]=scene_r(scene,terrain,p,idx,excluded)
        rows.append(row)
    manual={'ndmi':{},'ndvi':{}}
    for idx in ('ndmi','ndvi'):
        for p in predictors: manual[idx][p]=summarize(rows,p,idx,expected[p])
    comparison={}
    raw_summary=raw.get('summary') or {}; algo_cmp=algo.get('comparison') or {}
    for idx in ('ndmi','ndvi'):
        comparison[idx]={}
        for p in predictors:
            rr=(raw_summary.get(idx) or {}).get(p) or {}
            aa=(algo_cmp.get(idx) or {}).get(p) or {}
            mm=manual[idx][p]
            comparison[idx][p]={
                'raw_median_r':rr.get('median_pearson_r'),
                'algorithm_filtered_median_r':aa.get('filtered_median_r'),
                'manual_confirmed_filtered_median_r':mm.get('median_pearson_r'),
                'manual_expected_direction_fraction':mm.get('expected_direction_fraction'),
            }
    out={
        'ok':True,
        'quality_status':'valid_manual_confirmed_disturbance_sensitivity_test',
        'computed_at_local':datetime.now(TZ).isoformat(),
        'network':network.get('name'),
        'scene_count':len(rows),
        'sample_count':len(terrain),
        'manual_review_status':review.get('status'),
        'algorithmic_candidate_count':(review.get('summary') or {}).get('algorithmic_candidates'),
        'manual_confirmed_change_count':len(review.get('confirmed_change') or []),
        'manual_no_visible_change_count':len(review.get('no_visible_change') or []),
        'excluded_observation_count':len(excluded),
        'excluded_observations':details,
        'warning':'Sensitivity test excludes only observations whose vegetation-state change was manually visible in historical Sentinel chips. This still does not prove mowing/grazing or any specific cause.',
        'comparison':comparison,
        'manual_filtered_summary':manual,
        'scenes':rows,
    }
    OUT.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    status={k:out[k] for k in ('ok','quality_status','computed_at_local','network','scene_count','sample_count','manual_review_status','algorithmic_candidate_count','manual_confirmed_change_count','manual_no_visible_change_count','excluded_observation_count','warning','comparison')}
    status['output_file']=str(OUT.relative_to(ROOT)).replace('\\','/')
    status['next_step']='Use the manual-confirmed filter as the stricter sensitivity test; compare raw, algorithm-filtered and manually-filtered results before changing finding confidence.'
    STATUS.write_text(json.dumps(status,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'ok':True,'excluded':len(excluded),'output':status['output_file']},ensure_ascii=False))

if __name__=='__main__': main()
