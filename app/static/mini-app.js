'use strict';
(() => {
  const initialize = () => {
  const platform = document.documentElement.dataset.miniApp;
  const app = platform === 'telegram' ? window.Telegram?.WebApp : platform === 'max' ? window.WebApp : null;
  if (!app) return;
  // Guest checkout: never use initDataUnsafe as an identity or trust client prices.
  try { app.ready?.(); if (platform === 'telegram') app.expand?.(); } catch (_) { /* Browser fallback remains usable. */ }
  const back = () => {
    const dialog = document.querySelector('dialog[open]');
    if (dialog) { dialog.close(); return; }
    location.assign('/mini/' + platform);
  };
  try {
    app.BackButton?.onClick(back);
    if (location.pathname.startsWith('/order/') || location.pathname.startsWith('/legal/')) app.BackButton?.show();
    else app.BackButton?.hide();
  } catch (_) { /* Standard navigation links remain available. */ }
  document.querySelectorAll('[data-payment-link]').forEach(link => link.addEventListener('click', event => {
    if (typeof app.openLink === 'function') {
      event.preventDefault();
      try { app.openLink(link.href); } catch (_) { window.open(link.href, '_blank', 'noopener,noreferrer'); }
    }
  }));
  };
  // A slow messenger SDK must not block the catalog and cart scripts.
  const sdk = document.getElementById('mini-app-sdk');
  if (window.Telegram?.WebApp || window.WebApp) initialize();
  else sdk?.addEventListener('load', initialize, { once: true });
})();
