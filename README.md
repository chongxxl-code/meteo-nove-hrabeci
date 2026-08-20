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