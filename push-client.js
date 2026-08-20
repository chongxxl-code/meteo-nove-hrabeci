(() => {
  const CONFIG = './data/push-config.json';
  const SDK = 'https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.page.js';

  function basePath() {
    const p = location.pathname;
    const marker = '/meteo-nove-hrabeci/';
    return p.includes(marker) ? marker : '/';
  }

  function addButton() {
    const sect = document.querySelector('.risks .sect');
    if (!sect || document.getElementById('pushCriticalBtn')) return null;
    const b = document.createElement('button');
    b.id = 'pushCriticalBtn';
    b.type = 'button';
    b.textContent = '🔔 Nouzová upozornění';
    b.style.cssText = 'border:1px solid #2c5962;background:#0a1a21;color:#edf8fb;border-radius:10px;padding:8px 10px;font-weight:800;cursor:pointer;margin-left:auto';
    b.title = 'Pouze skutečně kritické lokální výstrahy';
    sect.appendChild(b);
    return b;
  }

  function loadSdk() {
    return new Promise((resolve, reject) => {
      if (document.querySelector(`script[src="${SDK}"]`)) return resolve();
      const s = document.createElement('script');
      s.src = SDK;
      s.defer = true;
      s.onload = resolve;
      s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  async function init() {
    let cfg;
    try {
      cfg = await fetch(`${CONFIG}?_=${Date.now()}`, { cache: 'no-store' }).then(r => r.json());
    } catch (_) { return; }
    if (!cfg.enabled || !cfg.app_id) return;

    const btn = addButton();
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = '🔔 Připravuji upozornění…';

    try {
      await loadSdk();
      window.OneSignalDeferred = window.OneSignalDeferred || [];
      window.OneSignalDeferred.push(async function(OneSignal) {
        const base = basePath();
        await OneSignal.init({
          appId: cfg.app_id,
          serviceWorkerPath: `${base}push/onesignal/OneSignalSDKWorker.js`,
          serviceWorkerParam: { scope: `${base}push/onesignal/` },
          notifyButton: { enable: false },
          welcomeNotification: { disable: true }
        });

        const refresh = () => {
          const on = !!OneSignal.User.PushSubscription.optedIn;
          btn.disabled = false;
          btn.textContent = on ? '🔔 Kritické push: zapnuto' : '🔔 Zapnout kritické push';
          btn.style.borderColor = on ? '#70e2a1' : '#2c5962';
        };
        refresh();
        OneSignal.User.PushSubscription.addEventListener('change', refresh);

        btn.addEventListener('click', async () => {
          const isiOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
          const standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
          if (isiOS && !standalone) {
            alert('Na iPhonu musí být observatoř nejdřív přidaná na plochu a otevřená z ikony. Potom lze upozornění povolit.');
            return;
          }
          try {
            if (OneSignal.User.PushSubscription.optedIn) {
              const off = confirm('Kritická upozornění jsou zapnutá. Chceš je vypnout?');
              if (off) await OneSignal.User.PushSubscription.optOut();
            } else {
              await OneSignal.User.PushSubscription.optIn();
            }
            refresh();
          } catch (e) {
            alert('Upozornění se nepodařilo nastavit: ' + (e?.message || e));
          }
        });
      });
    } catch (e) {
      btn.disabled = true;
      btn.textContent = '🔕 Push není dostupný';
      console.warn('Push init failed', e);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
