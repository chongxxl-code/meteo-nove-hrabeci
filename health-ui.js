(() => {
  const fmtAge = m => {
    if (m == null) return '—';
    if (m < 90) return `${Math.round(m)} min`;
    return `${(m/60).toFixed(m >= 600 ? 0 : 1)} h`;
  };
  const labels = {ok:'OK', delayed:'ZPOŽDĚNÍ', stale:'STALE', error:'CHYBA'};
  const colors = {ok:'var(--g)', delayed:'var(--w)', stale:'#ff9d55', error:'#ff6f66'};

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
      const r = await fetch(`./data/health-status.json?ts=${Date.now()}`, {cache:'no-store'});
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const h = await r.json();
      host.innerHTML = '';
      (h.sources || []).forEach(s => {
        const d = document.createElement('div');
        d.className = 'stat';
        const c = colors[s.status] || 'var(--mu)';
        d.innerHTML = `<b style="color:${c}">${labels[s.status] || s.status}</b><span>${s.label} · sběr ${fmtAge(s.check_age_minutes)}${s.observation_age_minutes != null ? ` · data ${fmtAge(s.observation_age_minutes)}` : ''}</span>`;
        host.appendChild(d);
      });
      const note = document.getElementById('centralnote');
      if (note) {
        note.textContent = h.overall_status === 'ok'
          ? 'Health monitor: všechny sledované zdroje jsou v povoleném intervalu čerstvosti.'
          : 'Health monitor zachytil zpožděný nebo zastaralý zdroj. Detail je vidět ve stavech výše.';
      }
    } catch (e) {
      host.innerHTML = '<div class="stat"><b style="color:#ff6f66">CHYBA</b><span>Health monitor není dostupný</span></div>';
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', loadHealth);
  else loadHealth();
  setInterval(loadHealth, 60000);
  window.addEventListener('focus', loadHealth);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) loadHealth(); });
})();
