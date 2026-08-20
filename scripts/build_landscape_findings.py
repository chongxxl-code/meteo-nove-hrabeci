#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
GRADIENT = ROOT / 'data' / 'validation' / 'open-land-gradient-status.json'
EXPERIMENT = ROOT / 'data' / 'validation' / 'open-land-experiment-status.json'
SENSITIVITY = ROOT / 'data' / 'validation' / 'open-land-sensitivity-status.json'
MANUAL_SENSITIVITY = ROOT / 'data' / 'validation' / 'open-land-manual-sensitivity-status.json'
DISTURBANCE = ROOT / 'data' / 'validation' / 'open-land-disturbance-status.json'
REVIEW = ROOT / 'config' / 'disturbance-visual-review.json'
OUT = ROOT / 'data' / 'knowledge' / 'findings.json'
TZ = ZoneInfo('Europe/Prague')


def pct(v):
    return None if v is None else round(float(v) * 100, 1)


def main():
    g = json.loads(GRADIENT.read_text(encoding='utf-8'))
    e = json.loads(EXPERIMENT.read_text(encoding='utf-8'))
    s = json.loads(SENSITIVITY.read_text(encoding='utf-8')) if SENSITIVITY.exists() else {}
    ms = json.loads(MANUAL_SENSITIVITY.read_text(encoding='utf-8')) if MANUAL_SENSITIVITY.exists() else {}
    d = json.loads(DISTURBANCE.read_text(encoding='utf-8')) if DISTURBANCE.exists() else {}
    rv = json.loads(REVIEW.read_text(encoding='utf-8')) if REVIEW.exists() else {}

    ndmi = ((g.get('summary') or {}).get('ndmi') or {})
    ndvi = ((g.get('summary') or {}).get('ndvi') or {})
    es = e.get('summary') or {}
    algo = ((s.get('comparison') or {}).get('ndmi') or {})
    manual = ((ms.get('comparison') or {}).get('ndmi') or {})
    manual_vi = ((ms.get('comparison') or {}).get('ndvi') or {})

    slope = ndmi.get('slope') or {}
    twi = ndmi.get('twi') or {}
    south = ndmi.get('southness') or {}
    pos = es.get('position_ndmi') or {}
    tpi = ndmi.get('tpi900') or {}
    elev = ndmi.get('elevation') or {}

    slope_m = manual.get('slope') or {}
    south_m = manual.get('southness') or {}
    twi_m = manual.get('twi') or {}
    tpi_m = manual.get('tpi900') or {}
    elev_m = manual.get('elevation') or {}
    slope_vi_m = manual_vi.get('slope') or {}
    south_vi_m = manual_vi.get('southness') or {}

    confirmed = len(rv.get('confirmed_change') or [])
    nochange = len(rv.get('no_visible_change') or [])
    candidates = (rv.get('summary') or {}).get('algorithmic_candidates', d.get('event_count'))

    findings = [
        {
            'id': 'open-slope-drainage',
            'title': 'Strmější travní plochy se opakovaně jeví sušší / méně vitální',
            'state': 'emerging_supported',
            'confidence': 'moderate',
            'claim': 'Napříč kontrolovanými travními plochami má vyšší sklon ve většině Sentinel termínů záporný vztah k NDMI i NDVI a tento signál zůstává i po vyřazení pouze ručně potvrzených vegetačních změn.',
            'evidence': [
                f"Raw NDMI: očekávaný záporný směr v {pct(slope.get('expected_direction_fraction'))} % použitelných scén; medián Pearson r = {slope.get('median_pearson_r')}.",
                f"Po ručním filtru 5 potvrzených změn: NDMI medián r = {slope_m.get('manual_confirmed_filtered_median_r')}; očekávaný směr {pct(slope_m.get('manual_expected_direction_fraction'))} %.",
                f"NDVI po ručním filtru: medián r = {slope_vi_m.get('manual_confirmed_filtered_median_r')}.",
                f"Síla NDMI vztahu sklonu se s 14denním deštěm mění korelací r = {slope.get('corr_rain_14d_vs_scene_pearson')} (explorační).",
            ],
            'interpretation': 'Pracovní mechanismus: po srážkách strmější místa mohou rychleji odvádět vodu a plošší místa ji déle držet. Ručně ověřený filtr tento vztah neoslabuje, což zvyšuje jeho věrohodnost, ale stále nejde o přímé měření půdní vlhkosti ani důkaz mechanismu.',
            'next_test': 'Sledovat vztah po dalších výrazných deštích a v další sezoně; ideálně přidat terénní měření půdní vlhkosti.'
        },
        {
            'id': 'twi-context-dependent',
            'title': 'TWI nevypadá jako stálé pořadí vlhkosti — spíš jako podmíněný efekt',
            'state': 'emerging_hypothesis',
            'confidence': 'low_to_moderate',
            'claim': 'Vyšší TWI samo o sobě není ve všech termínech spojeno s vyšším NDMI; statický signál je slabý i po ručním filtru, ale vztah se zesiluje po delším vlhkém období.',
            'evidence': [
                f"Raw TWI–NDMI: medián Pearson r = {twi.get('median_pearson_r')}; kladný směr v {pct(twi.get('positive_fraction'))} % scén.",
                f"Po ručním filtru: medián r = {twi_m.get('manual_confirmed_filtered_median_r')}; očekávaný kladný směr {pct(twi_m.get('manual_expected_direction_fraction'))} %.",
                f"Korelace 30denního deště se sílou denního TWI–NDMI vztahu: r = {twi.get('corr_rain_30d_vs_scene_pearson')}.",
                f"Skupinový TWI+ − TWI− medián NDMI = {(es.get('twi_ndmi') or {}).get('median_contrast')}.",
            ],
            'interpretation': 'TWI zatím není univerzální žebříček „mokré–suché“. Možná se topografická akumulace projeví hlavně tehdy, když je v krajině dost vody k redistribuci.',
            'next_test': 'Zvýšit počet scén po výrazně mokrých i suchých obdobích a testovat TWI efekt odděleně podle předchozích srážek.'
        },
        {
            'id': 'southness-drying',
            'title': 'Jižní expozice má po ruční kontrole stále sušší signál',
            'state': 'emerging_supported',
            'confidence': 'moderate',
            'claim': 'U dostatečně sklonitých a směrově koherentních travních ploch je vyšší southness většinou spojena s nižším NDMI; po vyřazení pouze ručně potvrzených změn vztah zůstává zřetelně záporný.',
            'evidence': [
                f"Raw southness–NDMI: očekávaný záporný směr v {pct(south.get('expected_direction_fraction'))} % scén; medián Pearson r = {south.get('median_pearson_r')}.",
                f"Po ručním filtru: NDMI medián r = {south_m.get('manual_confirmed_filtered_median_r')}; očekávaný záporný směr {pct(south_m.get('manual_expected_direction_fraction'))} %.",
                f"NDVI po ručním filtru: medián r = {south_vi_m.get('manual_confirmed_filtered_median_r')}; očekávaný směr {pct(south_vi_m.get('manual_expected_direction_fraction'))} %.",
                f"Skupinový jih − sever medián NDMI = {(es.get('aspect_ndmi') or {}).get('median_contrast')}.",
            ],
            'interpretation': 'Směr odpovídá vyššímu oslunění a potenciálnímu výparu na jižních svazích. Dedicated severní skupina ale zatím obsahuje jen jeden schválený vzorek; nové N4–N8 čekají na ruční kontrolu.',
            'next_test': 'Schválit další čisté severní kontrolní plochy a znovu přepočítat sever × jih, zejména během teplých suchých epizod.'
        },
        {
            'id': 'low-position-simple-rule',
            'title': 'Jednoduché pravidlo „nízko = vlhčeji“ se v současné síti nepotvrzuje',
            'state': 'simple_hypothesis_not_supported',
            'confidence': 'moderate_for_current_sample',
            'claim': 'Níže položené travní vzorky nejsou v této sezoně systematicky vlhčí/zelenejší než vyšší vzorky. Ruční filtr potvrzených vegetačních změn tento překvapivý opačný signál neodstranil.',
            'evidence': [
                f"Nízká − vyšší poloha: medián NDMI kontrastu = {pos.get('median_contrast')}; nízká skupina měla vyšší NDMI jen v {pct(pos.get('positive_fraction'))} % srovnatelných scén.",
                f"TPI900–NDMI: raw medián r = {tpi.get('median_pearson_r')}, po ručním filtru {tpi_m.get('manual_confirmed_filtered_median_r')}.",
                f"Výška–NDMI: raw medián r = {elev.get('median_pearson_r')}, po ručním filtru {elev_m.get('manual_confirmed_filtered_median_r')}.",
            ],
            'interpretation': 'Neznamená to, že vyšší terén způsobuje větší vlhkost. Naopak to říká, že samotná kategorie „nízká/vysoká poloha“ je pro naše plochy příliš jednoduchá. Rozdíl může souviset s půdou, lokální drenáží, hospodařením mimo zachycené termíny, fenologií nebo kombinací terénních vlastností.',
            'next_test': 'Použít více sezon, případně půdní mapy a terénní měření; jednoduchou výškovou hypotézu nepovyšovat na kauzální vysvětlení.'
        },
        {
            'id': 'management-confounding',
            'title': 'Algoritmický NDVI propad není totéž co viditelná změna porostu',
            'state': 'emerging_supported',
            'confidence': 'moderate',
            'claim': 'Automatický detektor našel 12 kandidátních vegetačních narušení, ale ruční historická kontrola potvrdila přesvědčivou změnu jen u části z nich.',
            'evidence': [
                f"Detektor označil {candidates} kandidátních událostí; ruční kontrola potvrdila {confirmed} a u {nochange} nenašla přesvědčivou viditelnou změnu.",
                f"Ručně ověřený citlivostní test proto vyřazuje jen {ms.get('excluded_observation_count')} sample×datum pozorování, zatímco široký algoritmický filtr vyřazoval {s.get('excluded_observation_count')}.",
                f"Například TPI900–NDMI je raw {tpi.get('median_pearson_r')}, po širokém algoritmickém filtru {(algo.get('tpi900') or {}).get('filtered_median_r')}, ale po ručním filtru {tpi_m.get('manual_confirmed_filtered_median_r')}.",
            ],
            'interpretation': 'Spektrální NDVI změna může být reálná i bez nápadné změny v RGB, takže sedm případů neoznačujeme za definitivní falešné poplachy. Pro kauzální interpretaci ale budeme jako přísnější kontrolu používat ručně potvrzené změny a automatické flagy držet odděleně.',
            'next_test': 'Při dalších událostech kombinovat NDVI/NDMI, true-colour snímek a pokud možno místní záznam o seči, pastvě nebo jiném zásahu.'
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
            'data/validation/open-land-gradient-status.json',
            'data/validation/open-land-disturbance-status.json',
            'data/validation/open-land-sensitivity-status.json',
            'config/disturbance-visual-review.json',
            'data/validation/open-land-manual-sensitivity-status.json'
        ],
        'warning': 'Poznatky jsou pracovní a explorační. Sentinel NDMI není přímá půdní vlhkost; korelace nejsou kauzalita. Stav a confidence se mají měnit s novými daty.',
        'findings': findings,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'ok': True, 'findings': len(findings), 'output': str(OUT.relative_to(ROOT))}, ensure_ascii=False))


if __name__ == '__main__':
    main()
