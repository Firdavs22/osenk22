'use strict';
(() => {
  const counter = document.querySelector('meta[name="metrika-counter"]')?.content;
  if (!/^[1-9]\d{0,14}$/.test(counter || '')) return;
  // Do not load the third party on legal pages or expose private order URLs.
  const publicPath = location.pathname === '/' || /^\/mini\/(telegram|max)$/.test(location.pathname);
  const orderNode = document.getElementById('order-ecommerce');
  if (!publicPath && !orderNode) return;
  const storage = {
    get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
    set(key,value) { try { localStorage.setItem(key,value); } catch (_) {} }
  };
  let enabled = false;
  function start() {
    if (enabled) return;
    enabled = true;
    window.dataLayer = window.dataLayer || [];
    window.ym = window.ym || function() { (window.ym.a = window.ym.a || []).push(arguments); };
    window.ym.l = Date.now();
    const safe = new URL(location.origin + (orderNode ? '/order' : location.pathname));
    if (!orderNode) {
      const params = new URLSearchParams(location.search);
      ['utm_source','utm_medium','utm_campaign','utm_content','utm_term'].forEach(k => {
        if (params.has(k)) safe.searchParams.set(k,params.get(k).slice(0,100));
      });
    }
    window.ym(Number(counter),'init',{defer:true,clickmap:false,trackLinks:false,accurateTrackBounce:false,webvisor:false,ecommerce:'dataLayer'});
    window.ym(Number(counter),'hit',safe.href,{title:orderNode ? 'Заказ оформлен' : document.title, referer:location.origin+'/'});
    const script = document.createElement('script'); script.async = true; script.src = 'https://mc.yandex.ru/metrika/tag.js';
    document.head.append(script);
    if (orderNode) {
      const order = JSON.parse(orderNode.dataset.order);
      once(order,'created',() => goal('order_created'));
      if (order.purchase) once(order,'purchase',() => window.dataLayer.push({ecommerce:{currencyCode:order.currency,purchase:{actionField:{id:order.id,revenue:order.revenue},products:order.products}}}));
      if (order.paid) once(order,'paid',() => goal('payment_success'));
    }
  }
  function once(order,event,fn) {
    const key = `metrika:${counter}:${order.id}:${event}`;
    if (!storage.get(key)) { fn(); storage.set(key,'1'); }
  }
  function goal(name) { if (enabled) window.ym(Number(counter),'reachGoal',name); }
  window.shopAnalytics = {goal};
  const banner = document.createElement('section'); banner.className='analytics-consent'; banner.setAttribute('aria-label','Аналитика сайта');
  const message = document.createElement('p'); message.textContent='Разрешить Яндекс Метрике собирать статистику посещений и покупок? Это помогает улучшать магазин.';
  const policy = document.createElement('a');policy.href='/legal/privacy';policy.textContent='Политика конфиденциальности';
  const accept = document.createElement('button');accept.textContent='Разрешить';accept.type='button';
  const decline = document.createElement('button');decline.textContent='Без аналитики';decline.type='button';
  banner.append(message,policy,accept,decline);document.body.append(banner);
  const key = 'shop-analytics-consent-v1';
  accept.addEventListener('click',() => { storage.set(key,'yes');banner.hidden=true;start(); });
  decline.addEventListener('click',() => { storage.set(key,'no');banner.hidden=true;if(enabled) location.reload(); });
  const settings = document.querySelector('[data-analytics-settings]');
  if(settings) {settings.hidden=false;settings.addEventListener('click',()=>banner.hidden=false);}
  banner.hidden=!!storage.get(key);
  if(storage.get(key)==='yes') start();
})();
