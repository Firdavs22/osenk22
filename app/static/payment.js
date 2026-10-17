'use strict';
(() => {
  const dialog = document.getElementById('payment-dialog');
  const link = document.querySelector('[data-payment-link]');
  if (!dialog || !link || typeof dialog.showModal!=='function') return;
  const container = document.getElementById('payment-container');
  const message = document.getElementById('payment-message');
  let setup, loadingTimer, loaded = false;
  function checkStatus() { document.dispatchEvent(new Event('payment:check')); }
  function fallback() {
    message.textContent = 'Форма не загрузилась. Нажмите «Открыть оплату отдельно» ниже.';
    container.setAttribute('aria-busy','false');
  }
  function loadSDK() {
    return new Promise((resolve,reject) => {
      if (window.PaymentIntegration) { resolve(); return; }
      const script = document.createElement('script');
      script.src = 'https://integrationjs.t-static.ru/integration.js';
      script.async = true;
      script.onload = resolve;
      script.onerror = reject;
      document.head.append(script);
    });
  }
  async function mount() {
    await loadSDK();
    const integration = await window.PaymentIntegration.init({
      terminalKey:dialog.dataset.terminal, product:'eacq', features:{iframe:{}}
    });
    const frame = await integration.iframe.create('order-payment', {
      loadedCallback:() => {
        loaded = true;
        clearTimeout(loadingTimer);
        container.setAttribute('aria-busy','false');
        message.textContent = 'Выберите удобный способ оплаты.';
      },
      status:{changedCallback:async status => {
        // Browser events are hints only. Our server verifies the bank's financial state.
        if (status==='SUCCESS') message.textContent = 'Проверяем подтверждение оплаты…';
        else if (['CANCELED','EXPIRED','REJECTED','PROCESSING_ERROR'].includes(status)) {
          message.textContent = 'Оплата не завершена. Проверьте статус перед повторной попыткой.';
        }
        checkStatus();
      }}
    });
    await frame.mount(container,link.href);
    const iframe = container.querySelector('iframe');
    if (iframe) iframe.title = 'Безопасная оплата через Т-Банк';
  }
  function open() {
    if (dialog.open) return;
    dialog.showModal();
    document.body.classList.add('payment-open');
    // Keep the same frame/payment when closed and reopened. Never call Init from the SDK.
    if (!setup) {
      loadingTimer = setTimeout(() => { if (!loaded) fallback(); },15000);
      setup = mount().catch(() => { clearTimeout(loadingTimer); fallback(); });
    }
  }
  link.addEventListener('click',event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button!==0) return;
    event.preventDefault(); open();
  });
  dialog.querySelector('[data-close-payment]').addEventListener('click',() => dialog.close());
  dialog.addEventListener('close',() => {
    document.body.classList.remove('payment-open');
    link.focus();
    checkStatus();
  });
  dialog.querySelector('[data-payment-external]').addEventListener('click',event => {
    const platform = document.documentElement.dataset.miniApp;
    const app = platform==='telegram' ? window.Telegram?.WebApp : platform==='max' ? window.WebApp : null;
    if (typeof app?.openLink==='function') {
      try { app.openLink(event.currentTarget.href); event.preventDefault(); } catch (_) { /* Normal link remains usable. */ }
    }
  });
  window.StorePayment = {open, isOpen:() => dialog.open};
})();
