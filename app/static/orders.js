(() => {
  const board = document.querySelector('#orders-board'), modal = document.querySelector('#order-modal');
  const content = document.querySelector('#order-content'), error = document.querySelector('#order-error');
  const sync = document.querySelector('#orders-sync');
  let refreshing = false, loading = 0, current = null, opener = null;
  async function fragment(url, selector) {
    const response = await fetch(url, {cache: 'no-store'});
    if (!response.ok || response.redirected) throw new Error('Не удалось загрузить данные. Проверьте соединение и вход в админку.');
    const html = await response.text();
    if (!new DOMParser().parseFromString(html, 'text/html').querySelector(selector)) throw new Error('Неожиданный ответ сервера. Обновите страницу.');
    return html;
  }
  async function refresh(force = false) {
    if (refreshing || board.querySelector('[data-busy]') || (!force && (document.hidden || modal.open))) return;
    refreshing = true;
    try {
      const url = new URL(location.href); url.searchParams.set('fragment', 'true');
      const html = await fragment(url, '[data-orders-board]');
      if (!force && modal.open) return;
      const scroll = board.querySelector('.orders-kanban')?.scrollLeft || 0;
      const focused = board.contains(document.activeElement) ? document.activeElement.dataset.order : null;
      board.innerHTML = html; board.querySelector('.orders-kanban').scrollLeft = scroll;
      if (focused) board.querySelector(`[data-order="${focused}"]`)?.focus({preventScroll:true});
      sync.textContent = 'Обновлено ' + new Date().toLocaleTimeString('ru-RU') + ' · проверка каждые 10 секунд';
    } catch (e) { sync.textContent = e.message; } finally { refreshing = false; }
  }
  async function details(id) {
    const request = ++loading;
    try {
      const html = await fragment(`/admin/orders/${id}?fragment=true`, '[data-order-detail]');
      if (request === loading && modal.open) content.innerHTML = html;
    } catch (e) { if (request === loading) error.textContent = e.message; }
  }
  board.addEventListener('click', event => {
    const link = event.target.closest('[data-order]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault(); opener = link; current = link.dataset.order;
    content.textContent = 'Загрузка заказа…'; error.textContent = ''; modal.showModal(); details(current);
  });
  modal.querySelector('.order-close').addEventListener('click', () => modal.close());
  modal.addEventListener('close', () => { ++loading; current = null; if (opener?.isConnected) opener.focus(); });
  modal.addEventListener('click', event => { if (event.target === modal) { const r = modal.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) modal.close(); } });
  async function changeStatus(event) {
    const form = event.target; if (!form.matches('form[action$="/status"]')) return;
    event.preventDefault(); const id = current; error.textContent = ''; form.dataset.busy = 'true';
    const quick = form.classList.contains('quick-status');
    const buttons = [...form.querySelectorAll('button')]; buttons.forEach(b => b.disabled = true);
    try {
      const response = await fetch(form.action, {method:'POST', body:new FormData(form), headers:{Accept:'application/json'}});
      if (response.redirected || !(response.headers.get('content-type') || '').includes('application/json')) throw new Error('Войдите в админку заново.');
      const result = await response.json(); if (!response.ok) throw new Error(result.detail || 'Не удалось изменить статус.');
      delete form.dataset.busy;
      if (!quick && current === id) await details(id); await refresh(true);
    } catch (e) {
      if (quick) { delete form.dataset.busy; await refresh(true); sync.textContent = e.message; }
      else if (current === id) { error.textContent = e.message; await details(id); }
    }
    finally { delete form.dataset.busy; buttons.forEach(b => b.disabled = false); }
  }
  content.addEventListener('submit', changeStatus);
  board.addEventListener('submit', changeStatus);
  window.addEventListener('orders-arrived', () => refresh());
  document.querySelector('#orders-refresh').addEventListener('click', () => refresh(true));
  setInterval(refresh, 10000);
})();
