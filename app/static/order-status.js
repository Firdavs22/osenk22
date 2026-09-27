'use strict';
(() => {
  const node = document.getElementById('order-tracking');
  if (!node) return;
  const note = document.getElementById('tracking-note');
  const original = JSON.parse(node.dataset.version);
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
        note.textContent = 'Статус обновляется автоматически каждые 10 секунд.';
      } catch (_) {
        note.textContent = 'Не удалось обновить статус. Повторяем проверку; можно нажать «Обновить статус».';
      } finally {
        clearTimeout(timeout);
      }
    }
    setTimeout(refresh, 10000);
  }
  setTimeout(refresh, 10000);
})();
