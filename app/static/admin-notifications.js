(() => {
  const button = document.querySelector('#browser-notifications');
  const opener = document.querySelector('#order-inbox-open');
  if (!opener) return;
  const note = document.querySelector('#browser-notifications-note');
  const badge = document.querySelector('#new-orders-count');
  const storageKey = 'shop-admin-order-alerts-v1';
  const cursorKey = 'shop-admin-order-cursor-v2';
  const dialog = document.querySelector('#order-inbox');
  const list = document.querySelector('#order-inbox-list');
  const inboxBadge = document.querySelector('#order-inbox-count');
  const inboxStatus = document.querySelector('#order-inbox-status');
  const readButton = document.querySelector('#order-inbox-read');
  const seenKey = 'shop-admin-order-seen-v2';
  const memory = new Map();
  let running = false, stopped = false, worker = null, latest = null, recent = [], rendered = '';
  const numeric = value => value!==null && /^\d+$/.test(value) ? Number(value) : null;
  const read = key => { try { return localStorage.getItem(key) ?? memory.get(key) ?? null; } catch (_) { return memory.get(key) ?? null; } };
  const save = (key,value) => { memory.set(key,String(value)); try { localStorage.setItem(key,String(value)); } catch (_) {} };
  const supported = 'Notification' in window && window.isSecureContext;
  function enabled() { return supported && Notification.permission === 'granted' && read(storageKey)==='on'; }
  function label() {
    if (!button) return;
    button.disabled = !supported;
    button.textContent = enabled() ? 'Отключить уведомления' : 'Включить уведомления';
    note.textContent = !supported ? 'Браузер не поддерживает уведомления. Откройте админку по HTTPS.' :
      Notification.permission === 'denied' ? 'Разрешите уведомления в настройках сайта в браузере.' :
      'Оставьте админку открытой. Уведомления работают и в фоновой вкладке.';
  }
  async function feed(after) {
    const query = new URLSearchParams({inbox:'true'});
    if (after!==null) query.set('after',after);
    const response = await fetch('/admin/order-feed?'+query,{cache:'no-store'});
    if (response.redirected || response.status===401 || response.status===403) {
      stopped = true; throw Error('Войдите в админку, чтобы получать уведомления.');
    }
    if (!response.ok) throw Error('Не удалось проверить заказы. Повторим автоматически.');
    return response.json();
  }
  function renderInbox() {
    if (latest===null) return;
    const seen = numeric(read(seenKey)) ?? latest;
    const unread = recent.filter(order => order.event_id>seen).length;
    inboxBadge.hidden = unread===0;
    inboxBadge.textContent = unread ? String(unread) : '';
    opener.setAttribute('aria-label',unread ? `Уведомления о заказах: непрочитанных ${unread}` : 'Уведомления о заказах');
    readButton.disabled = unread===0;
    readButton.textContent = unread ? 'Прочитать все' : 'Всё прочитано';
    inboxStatus.textContent = recent.length ? '' : 'Пока заказов нет. Новые появятся здесь автоматически.';
    const key = JSON.stringify([recent,seen]);
    if (key===rendered) return;
    rendered = key;
    const focused = list.contains(document.activeElement) ? document.activeElement.dataset.order : null;
    const scroll = list.scrollTop;
    const nodes = recent.map(order => {
      const item = document.createElement('li');
      item.classList.toggle('unread',order.event_id>seen);
      const link = document.createElement('a');
      link.href = '/admin/orders/'+order.id;
      link.dataset.order = order.id;
      link.setAttribute('aria-label',`Заказ №${order.id}, ${order.status_label}${order.event_id>seen ? ', не прочитан' : ''}`);
      const top = document.createElement('div'); top.className = 'inbox-order-top';
      const title = document.createElement('strong'); title.textContent = `Заказ №${order.id}`;
      const amount = document.createElement('span');
      amount.textContent = new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(order.total/100)+' '+order.currency;
      top.append(title,amount);
      const bottom = document.createElement('div'); bottom.className = 'inbox-order-bottom';
      const status = document.createElement('span'); status.className = 'badge '+order.status; status.textContent = order.status_label;
      const time = document.createElement('time'); time.dateTime = order.created_at.replace(' ','T')+'Z';
      const date = new Date(time.dateTime);
      time.textContent = Number.isNaN(date.getTime()) ? order.created_at : new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'UTC'}).format(date)+' UTC';
      bottom.append(status,time); link.append(top,bottom); item.append(link); return item;
    });
    list.replaceChildren(...nodes);
    list.scrollTop = scroll;
    if (focused) list.querySelector(`[data-order="${focused}"]`)?.focus({preventScroll:true});
  }
  opener.addEventListener('click',() => { dialog.showModal(); poll(); });
  document.querySelector('#order-inbox-close').addEventListener('click',() => dialog.close());
  dialog.addEventListener('click',event => {
    if (event.target!==dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX<rect.left || event.clientX>rect.right || event.clientY<rect.top || event.clientY>rect.bottom) dialog.close();
  });
  readButton.addEventListener('click',() => {
    if (latest===null) return;
    save(seenKey,Math.max(latest,numeric(read(seenKey)) ?? 0));
    renderInbox();
  });
  async function notify(ids) {
    const title = ids.length===1 ? `Новый заказ №${ids[0]}` : `Новых заказов: ${ids.length}`;
    const options = {body:'Откройте заказы в панели магазина.',tag:'shop-order-'+ids[ids.length-1]};
    if ('serviceWorker' in navigator) {
      worker ||= navigator.serviceWorker.register('/admin/notifications-worker.js',{scope:'/admin/'}).catch(error => { worker=null; throw error; });
      const registration = await worker;
      if (!registration.active) await new Promise((resolve,reject) => {
        const timer = setTimeout(() => reject(Error('Не удалось запустить уведомления. Повторим автоматически.')),10000);
        const installing = registration.installing || registration.waiting;
        if (!installing) { clearTimeout(timer); reject(Error('Сервис уведомлений недоступен.')); return; }
        installing.addEventListener('statechange', () => {
          if (installing.state==='activated') { clearTimeout(timer); resolve(); }
          if (installing.state==='redundant') { clearTimeout(timer); reject(Error('Обновите страницу для уведомлений.')); }
        });
      });
      await registration.showNotification(title,options);
    } else {
      const alert = new Notification(title,options);
      alert.onclick = () => { window.focus(); location.assign('/admin/orders'); alert.close(); };
    }
  }
  async function poll() {
    if (running || stopped) return;
    running = true;
    try {
      const check = async () => {
        const after = numeric(read(cursorKey));
        const data = await feed(enabled()?after:null);
        badge.textContent = data.pending ? String(data.pending) : '';
        const arrived = latest!==null && data.latest>latest;
        latest = data.latest;
        recent = data.recent;
        // Existing orders remain available without treating the whole archive as new.
        if (numeric(read(seenKey))===null) save(seenKey,latest);
        renderInbox();
        if (arrived) window.dispatchEvent(new Event('orders-arrived'));
        if (enabled() && after!==null && data.orders.length) await notify(data.orders);
        save(cursorKey,data.cursor);
      };
      // One notification across all open admin tabs on browsers supporting Web Locks.
      if (navigator.locks) await navigator.locks.request('shop-order-notifications',check);
      else await check();
    } catch (e) { inboxStatus.textContent = e.message; if (note) note.textContent = e.message; }
    finally { running = false; }
  }
  button?.addEventListener('click',async () => {
    if (enabled()) { save(storageKey,'off'); label(); return; }
    try {
      const permission = await Notification.requestPermission();
      if (permission==='granted') {
        const data = await feed(null); save(cursorKey,data.cursor); save(storageKey,'on');
      }
      label();
    } catch (_) { note.textContent = 'Не удалось включить уведомления. Проверьте настройки браузера.'; }
  });
  window.addEventListener('storage',() => { label(); renderInbox(); });
  label(); poll(); setInterval(poll,10000);
})();
