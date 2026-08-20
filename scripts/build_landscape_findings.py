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
DISTURBANCE = ROOT / 'data' / 'validation' / 'open-land-disturbance-status.json'
OUT = ROOT / 'data' / 'knowledge' / 'findings.json'
TZ = ZoneInfo('Europe/Prague')


def pct(v):
    return None if v is None else round(float(v) * 100, 1)


def main():
    g = json.loads(GRADIENT.read_text(encoding='utf-8'))
    e = json.loads(EXPERIMENT.read_text(encoding='utf-8'))
    s = json.loads(SENSITIVITY.read_text(encoding='utf-8')) if SENSITIVITY.exists() else {}
    d = json.loads(DISTURBANCE.read_text(encoding='utf-8')) if DISTURBANCE.exists() else {}
    ndmi = ((g.get('summary') or {}).get('ndmi') or {})
    ndvi = ((g.get('summary') or {}).get('ndvi') or {})
    es = e.get('summary') or {}
    sc = ((s.get('comparison') or {}).get('ndmi') or {})
    sc_vi = ((s.get('comparison') or {}).get('ndvi') or {})

    slope = ndmi.get('slope') or {}
    slope_vi = ndvi.get('slope') or {}
    twi = ndmi.get('twi') or {}
    south = ndmi.get('southness') or {}
    pos = es.get('position_ndmi') or {}
    tpi = ndmi.get('tpi900') or {}
    elev = ndmi.get('elevation') or {}

    slope_s = sc.get('slope') or {}
    south_s = sc.get('southness') or {}
    tpi_s = sc.get('tpi900') or {}
    elev_s = sc.get('elevation') or {}
    twi_s = sc.get('twi') or {}
    slope_vi_s = sc_vi.get('slope') or {}
    south_vi_s = sc_vi.get('southness') or {}

    findings = [
        {
            'id': 'open-slope-drainage',
            'title': 'Strmější travní plochy se opakovaně jeví sušší / méně vitální',
            'state': 'emerging_supported',
            'confidence': 'moderate',
            'claim': 'Napříč kontrolovanými travními plochami má vyšší sklon ve většině Sentinel termínů záporný vztah k NDMI i NDVI a tento signál přežil citlivostní filtr možných sečí/pastvy.',
            'evidence': [
                f"NDMI: očekávaný záporný směr v {pct(slope.get('expected_direction_fraction'))} % použitelných scén; medián Pearson r = {slope.get('median_pearson_r')}.",
                f"Po odfiltrování kandidátních narušení: NDMI medián r = {slope_s.get('filtered_median_r')} (raw {slope_s.get('raw_median_r')}); očekávaný směr zůstává {pct(slope_s.get('filtered_expected_direction_fraction'))} %.",
                f"NDVI po filtrování: medián r = {slope_vi_s.get('filtered_median_r')}.",
                f"Síla NDMI vztahu sklonu se s 14denním deštěm mění korelací r = {slope.get('corr_rain_14d_vs_scene_pearson')} (explorační).",
            ],
            'interpretation': 'Pracovní mechanismus: po srážkách strmější místa rychleji odvádějí vodu a plošší místa ji déle drží. To, že efekt po odstranění podezřelých vegetačních skoků nezeslábl, zvyšuje jeho věrohodnost, ale stále nejde o přímé měření půdní vlhkosti.',
            'next_test': 'Sledovat, zda se záporný vztah opakuje po dalších výrazných deštích, a v budoucnu přidat terénní půdní/vlhkostní měření.'
        },
        {
            'id': 'twi-context-dependent',
            'title': 'TWI nevypadá jako stálé pořadí vlhkosti — spíš jako podmíněný efekt',
            'state': 'emerging_hypothesis',
            'confidence': 'low_to_moderate',
            'claim': 'Vyšší TWI samo o sobě není ve všech termínech spojeno s vyšším NDMI; statický signál je slabý, ale vztah se zesiluje po delším vlhkém období.',
            'evidence': [
                f"Medián vztahu TWI–NDMI přes jednotlivé dny: Pearson r = {twi.get('median_pearson_r')}; kladný směr v {pct(twi.get('positive_fraction'))} % scén.",
                f"Po odfiltrování kandidátních narušení klesá medián TWI–NDMI na r = {twi_s.get('filtered_median_r')} a směr zůstává 50/50.",
                f"Korelace 30denního deště se sílou denního TWI–NDMI vztahu: r = {twi.get('corr_rain_30d_vs_scene_pearson')}.",
                f"Skupinový TWI+ − TWI− medián NDMI = {(es.get('twi_ndmi') or {}).get('median_contrast')}.",
            ],
            'interpretation': 'TWI zatím nevypadá jako univerzální žebříček „mokré–suché“. Možný mechanismus: topografická akumulace se projeví hlavně tehdy, když je v krajině dostatek vody k redistribuci.',
            'next_test': 'Zvýšit počet scén po výrazně mokrých a výrazně suchých obdobích a testovat TWI efekt odděleně podle antecedentních srážek.'
        },
        {
            'id': 'southness-drying',
            'title': 'Jižní expozice má po kontrole narušení výraznější sušší signál',
            'state': 'emerging_supported',
            'confidence': 'moderate',
            'claim': 'U dostatečně sklonitých a směrově koherentních travních ploch je vyšší southness většinou spojena s nižším NDMI; po odstranění podezřelých vegetačních skoků vztah zesílil.',
            'evidence': [
                f"Raw southness–NDMI: očekávaný záporný směr v {pct(south.get('expected_direction_fraction'))} % scén; medián Pearson r = {south.get('median_pearson_r')}.",
                f"Po disturbance filtru: NDMI medián r = {south_s.get('filtered_median_r')}; očekávaný záporný směr {pct(south_s.get('filtered_expected_direction_fraction'))} %.",
                f"NDVI po filtrování: medián r = {south_vi_s.get('filtered_median_r')}; očekávaný směr {pct(south_vi_s.get('filtered_expected_direction_fraction'))} %.",
                f"Skupinový jih − sever medián NDMI = {(es.get('aspect_ndmi') or {}).get('median_contrast')}.",
            ],
            'interpretation': 'Směr odpovídá vyššímu oslunění a potenciálnímu výparu na jižních svazích. Dedicated severní skupina má ale stále jen jeden vzorek, takže confidence zůstává pouze střední.',
            'next_test': 'Přidat další čisté severní svahy a sledovat rozdíl zejména během teplých suchých epizod.'
        },
        {
            'id': 'low-position-simple-rule',
            'title': 'Jednoduché pravidlo „nízko = vlhčeji“ se v současné síti nepotvrzuje',
            'state': 'simple_hypothesis_not_supported',
            'confidence': 'moderate_for_current_sample',
            'claim': 'Níže položené travní vzorky nejsou v této sezoně systematicky vlhčí/zelenejší než vyšší vzorky. Část opačného signálu ale slábne po odfiltrování možného hospodaření.',
            'evidence': [
                f"Nízká − vyšší poloha: medián NDMI kontrastu = {pos.get('median_contrast')}; nízká skupina měla vyšší NDMI jen v {pct(pos.get('positive_fraction'))} % srovnatelných scén.",
                f"TPI900–NDMI: raw medián r = {tpi.get('median_pearson_r')}, po disturbance filtru {tpi_s.get('filtered_median_r')}.",
                f"Výška–NDMI: raw medián r = {elev.get('median_pearson_r')}, po filtru {elev_s.get('filtered_median_r')}.",
            ],
            'interpretation': 'Neznamená to, že vyšší terén způsobuje větší vlhkost. Oslabení po filtrování ukazuje, že seč/pastva nebo jiná fenologie pravděpodobně část původního vztahu skutečně zkreslovala.',
            'next_test': 'Použít více sezon a případně informace o hospodaření/půdě; nevyvozovat kauzalitu z výšky samotné.'
        },
        {
            'id': 'management-confounding',
            'title': 'Hospodaření / vegetační narušení je měřitelný rušivý faktor',
            'state': 'emerging_supported',
            'confidence': 'moderate',
            'claim': 'Časová řada obsahuje několik prudkých NDVI propadů, které mohou odpovídat seči, pastvě nebo jinému narušení a dokážou posunovat zdánlivé terénní vztahy.',
            'evidence': [
                f"Detektor označil {d.get('event_count')} kandidátních narušení, z toho {d.get('strong_event_count')} silných.",
                f"Citlivostní test vyřadil {s.get('excluded_observation_count')} sample×datum pozorování.",
                f"Po filtraci se TPI900–NDMI změnilo z {tpi_s.get('raw_median_r')} na {tpi_s.get('filtered_median_r')}, zatímco sklon se z {slope_s.get('raw_median_r')} změnil na {slope_s.get('filtered_median_r')}.",
            ],
            'interpretation': 'To je důležité metodicky: některé zdánlivé „mikroklimatické“ rozdíly mohou být ve skutečnosti zásah člověka nebo fenologie. Proto budeme vždy porovnávat raw a disturbance-filtered výsledek.',
            'next_test': 'U největších NDVI propadů vizuálně ověřit Sentinel/letecký kontext a případně vést ruční záznam seče/pastvy, pokud bude dostupný.'
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
            'data/validation/open-land-sensitivity-status.json'
        ],
        'warning': 'Poznatky jsou pracovní a explorační. Sentinel NDMI není přímá půdní vlhkost; korelace nejsou kauzalita. Stav a confidence se mají měnit s novými daty.',
        'findings': findings,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'ok': True, 'findings': len(findings), 'output': str(OUT.relative_to(ROOT))}, ensure_ascii=False))


if __name__ == '__main__':
    main()
