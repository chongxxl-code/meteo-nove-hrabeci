#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
FORECAST_ARCHIVE = DATA / 'archive'
RADAR_ARCHIVE = DATA / 'radar-observations'
OBS_ARCHIVE = DATA / 'observations'
OUT = DATA / 'forecast-verification.json'
TZ = ZoneInfo('Europe/Prague')
RAIN_FORECAST_THRESHOLD_MM = 0.1
RAIN_OBS_THRESHOLD_MM = 0.1
RADAR_WINDOW_MIN = 40
SOHLAND_TEMP_MATCH_MIN = 45
CHMI_TEMP_MATCH_MIN = 60
CHMI_RAIN_WINDOW_MIN = 40
DWD_MATCH_MIN = 90


def dt(s):
    if not s:
        return None
    d = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    if d.tzinfo is None:
        d = d.replace(tzinfo=TZ)
    return d.astimezone(timezone.utc)


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def rounded(v, n=3):
    return None if v is None or not math.isfinite(v) else round(v, n)


def lead_bin(h):
    if h < 0: return None
    if h <= 6: return '0–6 h'
    if h <= 12: return '6–12 h'
    if h <= 24: return '12–24 h'
    if h <= 48: return '24–48 h'
    return '48–72 h'


def load_jsonl_dir(folder, pattern='*.jsonl'):
    out = []
    if not folder.exists():
        return out
    for p in sorted(folder.glob(pattern)):
        for line in p.read_text(encoding='utf-8').splitlines():
            try: out.append(json.loads(line))
            except Exception: pass
    return out


def load_forecasts(): return load_jsonl_dir(FORECAST_ARCHIVE)


def load_radar():
    out=[]
    for r in load_jsonl_dir(RADAR_ARCHIVE):
        t=dt(r.get('radar_time_utc'))
        if t: out.append((t,r))
    out.sort(key=lambda x:x[0]); return out


def load_temperature_archive(pattern):
    out=[]; seen=set()
    if not OBS_ARCHIVE.exists(): return out
    for p in sorted(OBS_ARCHIVE.glob(pattern)):
        for line in p.read_text(encoding='utf-8').splitlines():
            try:
                r=json.loads(line)
                if r.get('temperature_c') is None: continue
                t=dt(r.get('observed_at_utc')); key=(r.get('station_id') or r.get('station_wsi'),r.get('observed_at_utc'))
                if t and key not in seen:
                    seen.add(key); out.append((t,float(r['temperature_c']),r))
            except Exception: pass
    out.sort(key=lambda x:x[0]); return out


def load_chmi_rain():
    out=[]; seen=set()
    if not OBS_ARCHIVE.exists(): return out
    for p in sorted(OBS_ARCHIVE.glob('chmi-rain-*.jsonl')):
        for line in p.read_text(encoding='utf-8').splitlines():
            try:
                r=json.loads(line)
                if r.get('precipitation_10m_mm') is None: continue
                t=dt(r.get('observed_at_utc')); key=(r.get('station_wsi'),r.get('observed_at_utc'))
                if t and key not in seen:
                    seen.add(key); out.append((t,float(r['precipitation_10m_mm']),r))
            except Exception: pass
    out.sort(key=lambda x:x[0]); return out


def build_dwd_observations(snapshots):
    temp,rain=[],[]; seen=set()
    for s in snapshots:
        dwd=((s.get('observations') or {}).get('dwd') or {})
        tu=dwd.get('tu') or {}; t=dt(tu.get('observed_at_utc'))
        if t and tu.get('temperature_c') is not None:
            k=('t',t.isoformat(),tu.get('station_id'))
            if k not in seen: seen.add(k); temp.append((t,float(tu['temperature_c']),tu))
        rr=dwd.get('rr') or {}; t=dt(rr.get('observed_at_utc'))
        if t and rr.get('precipitation_1h_mm') is not None:
            k=('r',t.isoformat(),rr.get('station_id'))
            if k not in seen: seen.add(k); rain.append((t,float(rr['precipitation_1h_mm']),rr))
    temp.sort(key=lambda x:x[0]); rain.sort(key=lambda x:x[0]); return temp,rain


def nearest_obs(target,arr,tol_min):
    best=None
    for item in arr:
        delta=abs((item[0]-target).total_seconds())/60
        if delta<=tol_min and (best is None or delta<best[0]): best=(delta,item)
    return best[1] if best else None


def chmi_rain_obs(target,rain_obs):
    vals=[]; meta=None
    for t,value,r in rain_obs:
        delta=abs((t-target).total_seconds())/60
        if delta<=CHMI_RAIN_WINDOW_MIN:
            vals.append(value)
            if meta is None or delta<meta[0]: meta=(delta,r)
    if not vals: return None,None,0,None
    total=sum(vals); return total>=RAIN_OBS_THRESHOLD_MM,total,len(vals),meta[1] if meta else None


def radar_obs(target,radar):
    vals=[]
    for t,r in radar:
        delta=abs((t-target).total_seconds())/60
        if delta<=RADAR_WINDOW_MIN and r.get('local_rain_signal') is not None: vals.append(bool(r['local_rain_signal']))
    if not vals: return None,0
    return any(vals),len(vals)


def main():
    snapshots=load_forecasts(); radar=load_radar()
    sohland_temp=load_temperature_archive('dwd-sohland-*.jsonl')
    chmi_temp=load_temperature_archive('chmi-weather-*.jsonl')
    chmi_rain=load_chmi_rain(); dwd_temp,dwd_rain=build_dwd_observations(snapshots)
    now=datetime.now(timezone.utc); temp_cases=[]; rain_cases=[]

    for s in snapshots:
        issued=dt(s.get('collected_at_utc'))
        if not issued: continue
        for model,m in (s.get('models') or {}).items():
            times=m.get('time') or []; temps=m.get('temperature_2m') or []; rains=m.get('precipitation') or []
            for i,ts in enumerate(times):
                target=dt(ts)
                if not target or target>now: continue
                lead_h=(target-issued).total_seconds()/3600; bucket=lead_bin(lead_h)
                if bucket is None or lead_h>72: continue

                if i<len(temps) and temps[i] is not None:
                    obs=nearest_obs(target,sohland_temp,SOHLAND_TEMP_MATCH_MIN); source='dwd_sohland_10min'
                    if not obs:
                        obs=nearest_obs(target,chmi_temp,CHMI_TEMP_MATCH_MIN); source='chmi_nearest_temperature_station'
                    if not obs:
                        obs=nearest_obs(target,dwd_temp,DWD_MATCH_MIN); source='dwd_hourly_station'
                    if obs:
                        observed=obs[1]; forecast=float(temps[i]); meta=obs[2]
                        temp_cases.append({'model':model,'lead_bin':bucket,'lead_h':round(lead_h,2),
                            'issued_at_utc':issued.isoformat().replace('+00:00','Z'),'target_at_utc':target.isoformat().replace('+00:00','Z'),
                            'forecast_c':forecast,'observed_c':observed,'error_c':round(forecast-observed,3),'verification_source':source,
                            'station':meta.get('station_name'),'station_distance_km':meta.get('distance_to_nove_hrabeci_km',meta.get('distance_km'))})

                if i<len(rains) and rains[i] is not None:
                    forecast_mm=float(rains[i]); forecast_rain=forecast_mm>=RAIN_FORECAST_THRESHOLD_MM
                    local,radar_samples=radar_obs(target,radar)
                    gauge_rain,gauge_mm,gauge_samples,gauge_meta=chmi_rain_obs(target,chmi_rain)
                    dwd=nearest_obs(target,dwd_rain,DWD_MATCH_MIN)
                    if local is not None: observed_rain=local; source='local_radar'
                    elif gauge_rain is not None: observed_rain=gauge_rain; source='chmi_sluknov_gauge'
                    elif dwd is not None: observed_rain=dwd[1]>=RAIN_OBS_THRESHOLD_MM; source='dwd_station'
                    else: continue
                    outcome=('hit' if forecast_rain and observed_rain else 'false_alarm' if forecast_rain and not observed_rain else 'miss' if (not forecast_rain) and observed_rain else 'correct_dry')
                    rain_cases.append({'model':model,'lead_bin':bucket,'lead_h':round(lead_h,2),
                        'issued_at_utc':issued.isoformat().replace('+00:00','Z'),'target_at_utc':target.isoformat().replace('+00:00','Z'),
                        'forecast_mm':forecast_mm,'forecast_rain':forecast_rain,'observed_rain':observed_rain,'outcome':outcome,
                        'verification_source':source,'radar_samples':radar_samples,'chmi_gauge_mm_window':None if gauge_mm is None else round(gauge_mm,3),
                        'chmi_gauge_samples':gauge_samples,'station_rain_mm':None if dwd is None else dwd[1],
                        'station':(gauge_meta or {}).get('station_name') if gauge_meta else (None if dwd is None else dwd[2].get('station_name')),
                        'station_distance_km':(gauge_meta or {}).get('distance_to_nove_hrabeci_km') if gauge_meta else (None if dwd is None else dwd[2].get('distance_km'))})

    summary={}; models=sorted({c['model'] for c in temp_cases+rain_cases}); bins=['0–6 h','6–12 h','12–24 h','24–48 h','48–72 h']
    for model in models:
        summary[model]={}
        for b in bins:
            tc=[x for x in temp_cases if x['model']==model and x['lead_bin']==b]; rc=[x for x in rain_cases if x['model']==model and x['lead_bin']==b]
            errs=[x['error_c'] for x in tc]; hits=sum(x['outcome']=='hit' for x in rc); fa=sum(x['outcome']=='false_alarm' for x in rc); misses=sum(x['outcome']=='miss' for x in rc); dry=sum(x['outcome']=='correct_dry' for x in rc); total=len(rc)
            summary[model][b]={'temperature':{'n':len(tc),'mae_c':rounded(mean([abs(e) for e in errs]),2),'bias_c':rounded(mean(errs),2)},
                'rain':{'n':total,'hits':hits,'false_alarms':fa,'misses':misses,'correct_dry':dry,'accuracy':rounded((hits+dry)/total,3) if total else None,
                'pod':rounded(hits/(hits+misses),3) if hits+misses else None,'false_alarm_ratio':rounded(fa/(hits+fa),3) if hits+fa else None}}

    preferred_temp=sohland_temp[-1][2] if sohland_temp else (chmi_temp[-1][2] if chmi_temp else None)
    out={'schema':4,'generated_at_utc':now.isoformat().replace('+00:00','Z'),'method':{
        'temperature_truth_priority':'fresh DWD Sohland/Spree 10-minute T (~4.9 km), then nearest current ČHMÚ 10-minute T station, then DWD hourly observation',
        'temperature_station':None if not preferred_temp else preferred_temp.get('station_name'),
        'temperature_station_distance_km':None if not preferred_temp else preferred_temp.get('distance_to_nove_hrabeci_km',preferred_temp.get('distance_km')),
        'rain_truth_priority':'qualitative RainViewer signal within 7 km of NH, then ČHMÚ Šluknov 10-minute gauge (1.85 km), then fresh DWD hourly gauge',
        'rain_forecast_threshold_mm':RAIN_FORECAST_THRESHOLD_MM,
        'note':'Radar is rain/no-rain only; ČHMÚ gauge retains measured millimetres. Model weights are not changed automatically.'},
        'coverage':{'forecast_snapshots':len(snapshots),'sohland_temperature_observations':len(sohland_temp),'chmi_temperature_observations':len(chmi_temp),
        'chmi_rain_observations':len(chmi_rain),'dwd_temperature_observations':len(dwd_temp),'dwd_rain_observations':len(dwd_rain),'radar_observations':len(radar),
        'temperature_cases':len(temp_cases),'rain_cases':len(rain_cases)},'summary':summary,
        'recent_rain_cases':sorted(rain_cases,key=lambda x:(x['target_at_utc'],x['issued_at_utc']),reverse=True)[:80],
        'recent_temperature_cases':sorted(temp_cases,key=lambda x:(x['target_at_utc'],x['issued_at_utc']),reverse=True)[:40]}
    OUT.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); print(json.dumps(out['coverage'],ensure_ascii=False))


if __name__=='__main__': main()
