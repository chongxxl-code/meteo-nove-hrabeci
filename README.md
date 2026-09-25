# Meteo AI — Nové Hraběcí

Lokální meteorologický dashboard pro Nové Hraběcí (Šluknovsko).

## Architektura

- `index.html` — web pro PC i mobil; při otevření načítá živé modely a radar.
- GitHub Pages — veřejná HTTPS adresa aplikace.
- `.github/workflows/collect-weather.yml` — automatický sběr forecastových snapshotů každé 3 hodiny i bez otevřeného webu.
- `data/archive/YYYY-MM.jsonl` — centrální historie modelových forecastů.
- `data/status.json` — stav posledního automatického sběru pro dashboard.

## Zapnutí online verze

1. Repozitář nastav jako **Public** (nejjednodušší bezplatné GitHub Pages).
2. V **Settings → Pages → Build and deployment → Source** vyber **GitHub Actions**.
3. V **Actions → Collect weather data → Run workflow** spusť první sběr ručně.
4. Workflow **Deploy GitHub Pages** nasadí stránku a GitHub ukáže její URL.

Pak už se sběr spouští automaticky. Počítač ani stránka nemusí být zapnuté.

## Mobil

GitHub Pages URL otevřeš v mobilu jako normální web. Na iPhonu ji lze přes **Sdílet → Přidat na plochu** uložit jako webovou aplikaci.

## Lokální učení

Centrální archiv zatím ukládá předpovědi jednotlivých modelů. Dokud nepřidáme spolehlivý observační zdroj / vlastní meteostanici, aplikace nebude tvrdit, že některý model pro Nové Hraběcí prokazatelně vyhrává. Další fáze bude verifikace modelových chyb proti skutečnosti.
## Teplota uvnitř — EMOS/Tuya cloud

- `scripts/collect_indoor_emos.py` se přihlašuje přímo do EMOS/Tuya cloudu; PC ani Android emulátor nejsou potřeba pro běžný provoz.
- Collector běží ve stejném GitHub Actions cyklu jako počasí, tedy každé 3 hodiny.
- Každý běh stáhne překryvnou cloudovou historii DP2/DP3/DP24/DP106; teplotní body chodí přibližně po 5 minutách.
- Při prvním běhu nebo delším výpadku se automaticky vrací až 7 dní zpět, pak pokračuje inkrementálně s překryvem.
- Ukládá aktuální snapshot do `data/indoor-cloud/YYYY-MM.jsonl`, deduplikované surové DP události do `data/indoor-cloud/events-YYYY-MM.jsonl`, poslední stav do `data/indoor-latest.json` a teplotní řadu do `data/indoor-history.json`.
- Přihlašovací údaje a aplikační kryptografické hodnoty musí být pouze v GitHub Actions Secrets: `EMOS_USERNAME`, `EMOS_PASSWORD`, `EMOS_APP_ID`, `EMOS_APP_SECRET`, `EMOS_BMP_KEY`, `EMOS_CERT_SHA256`. Bez nich se indoor collector bezpečně přeskočí a meteorologický sběr pokračuje.
- `indoor.html` zobrazuje skutečnou časovou osu. Tříhodinový je pouze interval synchronizace; vlastní historie termostatu je výrazně jemnější. Původní 15min řada z grafu zůstává jako starší referenční backfill.
