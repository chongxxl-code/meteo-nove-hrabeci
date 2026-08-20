#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
GRADIENT = ROOT / 'data' / 'validation' / 'open-land-gradient-status.json'
EXPERIMENT = ROOT / 'data' / 'validation' / 'open-land-experiment-status.json'
OUT = ROOT / 'data' / 'knowledge' / 'findings.json'
TZ = ZoneInfo('Europe/Prague')


def pct(v):
    return None if v is None else round(float(v) * 100, 1)


def main():
    g = json.loads(GRADIENT.read_text(encoding='utf-8'))
    e = json.loads(EXPERIMENT.read_text(encoding='utf-8'))
    ndmi = ((g.get('summary') or {}).get('ndmi') or {})
    ndvi = ((g.get('summary') or {}).get('ndvi') or {})
    es = e.get('summary') or {}

    slope = ndmi.get('slope') or {}
    slope_vi = ndvi.get('slope') or {}
    twi = ndmi.get('twi') or {}
    south = ndmi.get('southness') or {}
    pos = es.get('position_ndmi') or {}
    tpi = ndmi.get('tpi900') or {}
    elev = ndmi.get('elevation') or {}

    findings = [
        {
            'id': 'open-slope-drainage',
            'title': 'Strmější travní plochy se opakovaně jeví sušší / méně vitální',
            'state': 'emerging_supported',
            'confidence': 'moderate',
            'claim': 'Napříč kontrolovanými travními plochami má vyšší sklon ve většině Sentinel termínů záporný vztah k NDMI i NDVI.',
            'evidence': [
                f"NDMI: očekávaný záporný směr v {pct(slope.get('expected_direction_fraction'))} % použitelných scén; medián Pearson r = {slope.get('median_pearson_r')}.",
                f"NDVI: očekávaný záporný směr v {pct(slope_vi.get('expected_direction_fraction'))} % scén; medián Pearson r = {slope_vi.get('median_pearson_r')}.",
                f"Síla NDMI vztahu sklonu se s 14denním deštěm mění korelací r = {slope.get('corr_rain_14d_vs_scene_pearson')} (explorační).",
            ],
            'interpretation': 'Pracovní mechanismus: po srážkách strmější místa rychleji odvádějí vodu a plošší místa ji déle drží. Zatím nejde o důkaz odtoku ani půdní vlhkosti.',
            'next_test': 'Sledovat, zda se záporný vztah opakuje po dalších výrazných deštích a v dalších vegetačních sezonách.'
        },
        {
            'id': 'twi-context-dependent',
            'title': 'TWI nevypadá jako stálé pořadí vlhkosti — spíš jako podmíněný efekt',
            'state': 'emerging_hypothesis',
            'confidence': 'low_to_moderate',
            'claim': 'Vyšší TWI samo o sobě není ve všech termínech spojeno s vyšším NDMI, ale vztah se zesiluje po delším vlhkém období.',
            'evidence': [
                f"Medián vztahu TWI–NDMI přes jednotlivé dny: Pearson r = {twi.get('median_pearson_r')}; kladný směr v {pct(twi.get('positive_fraction'))} % scén.",
                f"Korelace 30denního deště se sílou denního TWI–NDMI vztahu: r = {twi.get('corr_rain_30d_vs_scene_pearson')}.",
                f"Skupinový TWI+ − TWI− medián NDMI = {(es.get('twi_ndmi') or {}).get('median_contrast')}.",
            ],
            'interpretation': 'Možný mechanismus: topografická predispozice k akumulaci vody se projeví hlavně tehdy, když je v krajině dostatek vody k redistribuci.',
            'next_test': 'Zvýšit počet scén po výrazně mokrých a výrazně suchých obdobích a porovnat velikost TWI efektu.'
        },
        {
            'id': 'southness-drying',
            'title': 'Jižní expozice má zatím slabý, ale poměrně konzistentní sušší signál',
            'state': 'emerging_hypothesis',
            'confidence': 'low_to_moderate',
            'claim': 'U dostatečně sklonitých a směrově koherentních travních ploch je vyšší southness většinou spojena s nižším NDMI.',
            'evidence': [
                f"Očekávaný záporný směr southness–NDMI v {pct(south.get('expected_direction_fraction'))} % scén; medián Pearson r = {south.get('median_pearson_r')}.",
                f"Skupinový jih − sever medián NDMI = {(es.get('aspect_ndmi') or {}).get('median_contrast')}.",
            ],
            'interpretation': 'Směr odpovídá vyššímu oslunění a potenciálnímu výparu na jižních svazích, ale dedicated severní skupina má zatím jen jeden vzorek.',
            'next_test': 'Přidat další čisté severní svahy a sledovat rozdíl zejména během teplých suchých epizod.'
        },
        {
            'id': 'low-position-simple-rule',
            'title': 'Jednoduché pravidlo „nízko = vlhčeji“ se v současné síti nepotvrzuje',
            'state': 'simple_hypothesis_not_supported',
            'confidence': 'moderate_for_current_sample',
            'claim': 'Níže položené travní vzorky nejsou v této sezoně systematicky vlhčí/zelenejší než vyšší vzorky.',
            'evidence': [
                f"Nízká − vyšší poloha: medián NDMI kontrastu = {pos.get('median_contrast')}; nízká skupina měla vyšší NDMI jen v {pct(pos.get('positive_fraction'))} % srovnatelných scén.",
                f"Kontinuální TPI900–NDMI medián Pearson r = {tpi.get('median_pearson_r')} (kladná hodnota zde znamená, že vyšší relativní poloha často vyšla s vyšším NDMI).",
                f"Výška–NDMI medián Pearson r = {elev.get('median_pearson_r')}.",
            ],
            'interpretation': 'Neznamená to, že vyšší terén způsobuje větší vlhkost. Výsledek může být silně ovlivněn půdou, sečí/pastvou, fenologií, expozicí a místní drenáží.',
            'next_test': 'Použít více sezon a případně terénní informace o hospodaření/půdě; nevyvozovat kauzalitu z výšky samotné.'
        },
    ]

    out = {
        'ok': True,
        'generated_at_local': datetime.now(TZ).isoformat(),
        'status': 'living_exploratory_knowledge_base',
        'scope': 'Nové Hraběcí open-land experimental network',
        'sample_count': g.get('sample_count'),
        'scene_count': g.get('scene_count'),
        'source_analyses': [
            'data/validation/open-land-experiment-status.json',
            'data/validation/open-land-gradient-status.json'
        ],
        'warning': 'Poznatky jsou pracovní a explorační. Sentinel NDMI není přímá půdní vlhkost; korelace nejsou kauzalita. Stav a confidence se mají měnit s novými daty.',
        'findings': findings,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'ok': True, 'findings': len(findings), 'output': str(OUT.relative_to(ROOT))}, ensure_ascii=False))


if __name__ == '__main__':
    main()
