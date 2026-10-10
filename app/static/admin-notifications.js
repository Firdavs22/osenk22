(() => {
  const button = document.querySelector('#browser-notifications');
  if (!button) return;
  const note = document.querySelector('#browser-notifications-note');
  const badge = document.querySelector('#new-orders-count');
  const storageKey = 'shop-admin-order-alerts-v1';
  const cursorKey = 'shop-admin-order-cursor-v1';
  let running = false, stopped = false, worker = null;
  const read = key => { try { return localStorage.getItem(key); } catch (_) { return null; } };
  const save = (key,value) => { try { localStorage.setItem(key,String(value)); } catch (_) {} };
  const supported = 'Notification' in window && window.isSecureContext;
  function enabled() { return supported && Notification.permission === 'granted' && read(storageKey)==='on'; }
  function label() {
    button.disabled = !supported;
    button.textContent = enabled() ? 'Отключить уведомления' : 'Включить уведомления';
    note.textContent = !supported ? 'Браузер не поддерживает уведомления. Откройте админку по HTTPS.' :
      Notification.permission === 'denied' ? 'Разрешите уведомления в настройках сайта в браузере.' :
      'Оставьте админку открытой. Уведомления работают и в фоновой вкладке.';
  }
  async function feed(after) {
    const response = await fetch('/admin/order-feed'+(after===null?'':'?after='+after),{cache:'no-store'});
    if (response.redirected || response.status===401 || response.status===403) {
      stopped = true; throw Error('Войдите в админку, чтобы получать уведомления.');
    }
    if (!response.ok) throw Error('Не удалось проверить заказы. Повторим автоматически.');
    return response.json();
  }
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
        const saved = read(cursorKey);
        const after = saved!==null && /^\d+$/.test(saved) ? Number(saved) : null;
        const data = await feed(enabled()?after:null);
        badge.textContent = data.pending ? String(data.pending) : '';
        if (enabled() && after!==null && data.orders.length) {
          await notify(data.orders);
          window.dispatchEvent(new Event('orders-arrived'));
        }
        save(cursorKey,data.cursor);
      };
      // One notification across all open admin tabs on browsers supporting Web Locks.
      if (navigator.locks) await navigator.locks.request('shop-order-notifications',check);
      else await check();
    } catch (e) { note.textContent = e.message; }
    finally { running = false; }
  }
  button.addEventListener('click',async () => {
    if (enabled()) { save(storageKey,'off'); label(); return; }
    try {
      const permission = await Notification.requestPermission();
      if (permission==='granted') {
        const data = await feed(null); save(cursorKey,data.cursor); save(storageKey,'on');
      }
      label();
    } catch (_) { note.textContent = 'Не удалось включить уведомления. Проверьте настройки браузера.'; }
  });
  window.addEventListener('storage',label);
  label(); poll(); setInterval(poll,10000);
})();
