(() => {
  const LIVE_BASE = 'https://raw.githubusercontent.com/chongxxl-code/meteo-nove-hrabeci/main/';
  const fmtAge = m => {
    if (m == null) return '—';
    if (m < 90) return `${Math.round(m)} min`;
    return `${(m/60).toFixed(m >= 600 ? 0 : 1)} h`;
  };
  const labels = {ok:'OK', delayed:'ZPOŽDĚNÍ', stale:'STALE', error:'CHYBA'};
  const colors = {ok:'var(--g)', delayed:'var(--w)', stale:'#ff9d55', error:'#ff6f66'};
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  async function liveJson(path) {
    const ts = Date.now();
    try {
      const r = await fetch(`${LIVE_BASE}${path}?ts=${ts}`, {cache:'no-store', mode:'cors'});
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    } catch (e) {
      const r = await fetch(`./${path}?ts=${ts}`, {cache:'no-store'});
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    }
  }

  const pct = v => Number.isFinite(Number(v)) ? `${Math.round(Number(v) * 100)} %` : '—';
  const sourceByName = (h, name) => (h?.sources || []).find(s => s.name === name) || null;
  const qualityTone = state => state === 'good' ? 'var(--g)' : state === 'warn' ? 'var(--w)' : '#ff6f66';

  async function loadHealth(){
    const central = document.querySelector('.central');
    if (!central) return;
    let host = document.getElementById('healthgrid');
    if (!host) {
      host = document.createElement('div');
      host.id = 'healthgrid';
      host.className = 'centralgrid';
      host.style.marginTop = '10px';
      const note = document.getElementById('centralnote');
      (note || central.lastElementChild)?.after(host);
    }

    try {
      const [h, status, thermal] = await Promise.all([
        liveJson('data/health-status.json'),
        liveJson('data/status.json').catch(() => null),
        liveJson('data/indoor-thermal-model.json').catch(() => null),
      ]);

      const indoor = sourceByName(h, 'indoor');
      const sohland = sourceByName(h, 'sohland');
      const chmi = sourceByName(h, 'chmi');
      const radar = sourceByName(h, 'radar');
      const thermalHealth = sourceByName(h, 'thermal_model');

      const indoorCoverage = indoor?.quality?.coverage;
      const indoorFresh = indoor?.observation_age_minutes;
      const indoorState = indoor?.data_fresh && indoor?.coverage_sufficient ? 'good'
        : indoor?.data_fresh ? 'warn' : 'bad';
      const indoorText = `${pct(indoorCoverage)} / fresh ${fmtAge(indoorFresh)}`;

      const weatherCandidates = [sohland, chmi]
        .filter(Boolean)
        .filter(s => s.data_fresh && s.observation_age_minutes != null)
        .sort((a,b) => Number(a.observation_age_minutes) - Number(b.observation_age_minutes));
      const weather = weatherCandidates[0] || sohland || chmi;
      const weatherAge = weather?.observation_age_minutes;
      const weatherState = weather?.data_fresh ? (weather?.coverage_sufficient === false ? 'warn' : 'good') : 'bad';
      const weatherText = weatherAge == null ? 'unknown' : `fresh ${fmtAge(weatherAge)}`;

      const models = status?.models || {};
      const forecastAvailable = ['dwd','chmi','ec'].filter(k => models?.[k]?.ok === true).length;
      const forecastState = forecastAvailable === 3 ? 'good' : forecastAvailable >= 2 ? 'warn' : 'bad';
      const forecastText = `${forecastAvailable}/3 models available`;

      const rq = radar?.quality || {};
      const radarRatio = Number(rq.coverage);
      let radarText = 'unknown', radarState = 'bad';
      if (radar?.data_fresh) {
        if (radar?.coverage_sufficient && Number.isFinite(radarRatio) && radarRatio >= .999) {
          radarText = 'full';
          radarState = 'good';
        } else if (radar?.coverage_sufficient || (Number.isFinite(radarRatio) && radarRatio > 0)) {
          radarText = 'partial';
          radarState = 'warn';
        } else {
          radarText = 'insufficient';
          radarState = 'bad';
        }
      } else if (radar) {
        radarText = 'stale';
      }

      const fc = thermal?.passive_forecast?.forecast_coverage_contract || {};
      const modelReady = thermal?.prediction_ready === true;
      const inputRecentOk = thermalHealth?.data_fresh === true && thermalHealth?.coverage_sufficient !== false;
      const forecastCoverageOk = fc.coverage_sufficient !== false;
      const thermalOk = modelReady && inputRecentOk && forecastCoverageOk;
      const thermalState = thermalOk ? 'good' : (modelReady || inputRecentOk ? 'warn' : 'bad');
      const thermalText = thermalOk ? 'sufficient coverage' : modelReady ? 'partial coverage' : 'insufficient coverage';

      const rows = [
        ['Indoor', indoorText, indoorState],
        ['Weather', weatherText, weatherState],
        ['Forecast', forecastText, forecastState],
        ['Radar', radarText, radarState],
        ['Thermal model', thermalText, thermalState],
      ];

      host.innerHTML = rows.map(([label, value, state]) =>
        `<div class="stat"><span>${esc(label)}</span><b style="color:${qualityTone(state)}">${esc(value)}</b></div>`
      ).join('');

      const note = document.getElementById('centralnote');
      if (note) {
        note.textContent = 'DATA QUALITY · collector running, freshness a coverage jsou vyhodnocovány odděleně.';
      }
    } catch (e) {
      host.innerHTML = '<div class="stat"><span>DATA QUALITY</span><b style="color:#ff6f66">unavailable</b></div>';
    }
  }

  async function loadLiveAlerts() {
    const root = document.getElementById('riskgrid');
    const note = document.getElementById('risknote');
    if (!root) return;
    try {
      const d = await liveJson('data/alerts.json');
      root.innerHTML = (d.alerts || []).map(a =>
        `<div class="riskitem ${esc(a.level)}"><div class="riskname">${esc(a.icon)} ${esc(a.name)}</div><div class="riskheadline">${esc(a.headline)}</div><div class="riskmeta">${esc((a.reasons || [])[0] || '')}</div></div>`
      ).join('');
      const active = (d.alerts || []).filter(a => Number(a.rank) > 0);
      const stamp = d.computed_at_local
        ? new Intl.DateTimeFormat('cs-CZ',{hour:'2-digit',minute:'2-digit'}).format(new Date(d.computed_at_local))
        : '—';
      if (note) note.textContent = active.length
        ? `Aktivní lokální signály: ${active.map(a => a.name).join(', ')}. Aktualizace ${stamp} · živá data z main.`
        : `Bez zvýšeného lokálního rizika · aktualizace ${stamp} · živá data z main.`;
    } catch (e) {
      // Keep the page-rendered value as a fallback; do not replace it with an error if Pages data exists.
    }
  }

  function refreshAll() {
    loadHealth();
    loadLiveAlerts();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', refreshAll);
  else refreshAll();
  setTimeout(loadLiveAlerts, 1500);
  setInterval(refreshAll, 60000);
  window.addEventListener('focus', refreshAll);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshAll(); });
})();
