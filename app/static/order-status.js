'use strict';
(() => {
  const node = document.getElementById('order-tracking');
  if (!node) return;
  const note = document.getElementById('tracking-note');
  const original = JSON.parse(node.dataset.version);
  const waiting = original.payment_status==='pending';
  function openPayment() {
    const link = document.querySelector('[data-payment-link]');
    if (node.dataset.autoPay!=='1' || !link || !original.payment_ready) return;
    // Remove auto-navigation before leaving, so Back / bank return cannot loop.
    node.dataset.autoPay = '0';
    history.replaceState(null,'',location.pathname);
    const platform = document.documentElement.dataset.miniApp;
    const app = platform==='telegram' ? window.Telegram?.WebApp : platform==='max' ? window.WebApp : null;
    if (typeof app?.openLink==='function') {
      try { app.openLink(link.href); return; } catch (_) { /* Same-tab fallback. */ }
    }
    location.assign(link.href);
  }
  async function refresh() {
    if (!document.hidden) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        const response = await fetch(node.dataset.url, {credentials: 'same-origin', cache: 'no-store', signal:controller.signal});
        if (!response.ok) throw Error();
        const next = await response.json();
        if (Object.keys(original).some(key => original[key] !== next[key])) {
          location.reload(); return;
        }
        note.textContent = waiting ? 'Проверяем оплату…' : 'Статус обновляется автоматически каждые 10 секунд.';
      } catch (_) {
        note.textContent = 'Не удалось обновить статус. Повторяем проверку; можно нажать «Обновить статус».';
      } finally {
        clearTimeout(timeout);
      }
    }
    setTimeout(refresh, waiting ? 3000 : 10000);
  }
  async function start() {
    openPayment();
    if (node.dataset.startPayment && original.payment_state==='new' && !original.payment_expired) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(),18000);
      try {
        await fetch(node.dataset.startPayment,{method:'POST',credentials:'same-origin',
          signal:controller.signal,headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body:'{}'});
      } catch (_) { /* The durable worker will recover an interrupted request. */ }
      finally { clearTimeout(timeout); }
    }
    refresh();
  }
  start();
})();
