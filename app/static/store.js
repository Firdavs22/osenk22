'use strict';
(() => {
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const csrf = $('meta[name="csrf-token"]')?.content;
  const cfg = $('#store-config');
  if (!cfg) return;
  const money = n => new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2}).format(n / 100) + ' ' + cfg.dataset.currency;
  let quote = null, quoteVersion = 0, cartQueue = Promise.resolve(), toastTimer;
  const form = $('#checkout-form'), cartDialog = $('#cart-dialog'), error = $('#checkout-error');
  function toast(message) {
    const node = $('#toast'); node.textContent = message; node.classList.add('visible');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => node.classList.remove('visible'), 3000);
  }
  async function api(path, data) {
    const res = await fetch(path, data === undefined ? {credentials: 'same-origin'} : {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify(data)
    });
    const result = await res.json();
    if (!res.ok) {
      const error = Error(typeof result.detail === 'string' ? result.detail : 'Не удалось выполнить запрос');
      error.status = res.status; throw error;
    }
    return result;
  }
  function show(dialog) { dialog.showModal(); document.body.classList.add('modal-open'); }
  $$('dialog').forEach(d => d.addEventListener('close', () => {
    if (!$$('dialog[open]').length) document.body.classList.remove('modal-open');
  }));
  function element(tag, text, cls) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; }
  function renderCart(cart) {
    $$('[data-cart-count]').forEach(n => n.textContent = cart.quantity);
    const container = $('#cart-items'); container.replaceChildren();
    form.hidden = !cart.items.length; $('#cart-empty').hidden = !!cart.items.length;
    for (const p of cart.items) {
      const row = element('div', undefined, 'cart-row'), info = element('div');
      info.append(element('h3', p.name), element('small', p.active ? money(p.price) : 'Сейчас нет в наличии'));
      const controls = element('div', undefined, 'quantity-control');
      for (const [label, delta, accessible] of [['−', -1, 'Уменьшить'], [String(p.quantity), 0, ''], ['+', 1, 'Увеличить'], ['×', -99, 'Удалить']]) {
        const node = element(delta ? 'button' : 'span', label);
        if (delta) { node.type = 'button'; node.setAttribute('aria-label', accessible + ': ' + p.name); node.addEventListener('click', () => mutate(p.id, delta)); }
        controls.append(node);
      }
      row.append(info, controls); container.append(row);
    }
  }
  async function updateQuote() {
    const version = ++quoteVersion; quote = null; $('.checkout-submit').disabled = true;
    const method = $('#delivery-method').value;
    if ($('#district-field')) { $('#district-field').hidden = method !== 'delivery'; form.elements.district.required = method === 'delivery'; }
    $('#address-field').hidden = method !== 'delivery'; form.elements.address.required = method === 'delivery';
    try {
      const next = await api('/api/quote', {method, district: form.elements.district?.value || ''});
      if (version !== quoteVersion) return;
      quote = next; error.textContent = '';
      const summary = $('#quote-summary'); summary.replaceChildren();
      for (const [label, value, total] of [['Товары', quote.subtotal], ...(quote.discount ? [['Скидка на самовывоз', -quote.discount]] : []), ['Доставка', quote.delivery], ['Итого', quote.total, true]]) {
        const line = element('div', undefined, 'quote-line' + (total ? ' total' : ''));
        line.append(element('span', label), element('span', money(value))); summary.append(line);
      }
      $('.checkout-submit').disabled = false;
    } catch (e) { if (version === quoteVersion) { error.textContent = e.message; $('#quote-summary').replaceChildren(); } }
  }
  function mutate(id, delta, announce = false) {
    quote = null; $('.checkout-submit').disabled = true;
    cartQueue = cartQueue.then(async () => {
      const cart = await api('/api/cart', {id, delta}); renderCart(cart);
      if (delta > 0) window.shopAnalytics?.goal('add_to_cart');
      if (announce) toast('Добавлено в корзину');
      if (cartDialog.open && cart.items.length) await updateQuote();
    }).catch(e => toast(e.message));
    return cartQueue;
  }
  document.addEventListener('click', async event => {
    const add = event.target.closest('[data-add]');
    if (add) { add.disabled = true; await mutate(Number(add.dataset.add), 1, true); add.disabled = false; }
    const detail = event.target.closest('[data-detail]');
    if (detail) { window.shopAnalytics?.goal('product_view'); $('#product-content').replaceChildren($('#detail-' + detail.dataset.detail).content.cloneNode(true)); show($('#product-dialog')); }
    const close = event.target.closest('[data-close]'); if (close) close.closest('dialog').close();
    if (event.target.closest('[data-open-cart]')) {
      try { await cartQueue; renderCart(await api('/api/cart')); show(cartDialog); window.shopAnalytics?.goal('begin_checkout'); if (!form.hidden) await updateQuote(); }
      catch (e) { toast(e.message); }
    }
  });
  $('#delivery-method').addEventListener('change', updateQuote);
  $('#delivery-district')?.addEventListener('change', updateQuote);
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (!quote) { await updateQuote(); return; }
    $('.checkout-submit').disabled = true; error.textContent = '';
    const data = Object.fromEntries(new FormData(form)); data.consent = form.elements.consent.checked;
    try {
      const result = await api('/api/checkout', {...data, key: quote.key, fingerprint: quote.fingerprint});
      location.assign(result.url);
    } catch (e) {
      if (e.status === 400) await updateQuote();
      else $('.checkout-submit').disabled = false;
      // Keep the original key on a network/server failure: the order may already exist.
      error.textContent = e.status ? e.message : 'Связь прервалась. Нажмите подтвердить ещё раз — заказ не продублируется.';
    }
  });
  let category = 'all', tag = '';
  function filter() {
    const query = $('#search').value.trim().toLocaleLowerCase('ru'); let visible = 0;
    $$('[data-product]').forEach(card => {
      const tags = card.dataset.tags.split(',').map(t => t.trim());
      const match = (category === 'all' || card.dataset.categoryId === category) && (!tag || tags.includes(tag)) && card.dataset.name.toLocaleLowerCase('ru').includes(query);
      card.hidden = !match; if (match) visible++;
    });
    $('#empty-search').hidden = visible > 0;
  }
  function select(nodes, selected) { nodes.forEach(n => { const on = n === selected; n.classList.toggle('selected', on); n.setAttribute('aria-pressed', String(on)); }); }
  $$('[data-category]').forEach(button => button.addEventListener('click', () => { category = button.dataset.category; select($$('[data-category]'), button); filter(); }));
  $$('[data-tag]').forEach(button => button.addEventListener('click', () => { tag = button.dataset.tag; select($$('[data-tag]'), button); filter(); }));
  $('#search').addEventListener('input', filter);
  function hashCategory() {
    if (/^#category-\d+$/.test(location.hash)) { const target = $(location.hash); if (target) { target.click(); $('#catalog').scrollIntoView(); } }
    if (location.hash === '#catalog') { category = 'all'; select($$('[data-category]'), $('[data-category="all"]')); filter(); }
  }
  window.addEventListener('hashchange', hashCategory); hashCategory();
  // Manual carousel: no automatic movement, so it remains readable and keyboard accessible.
  const slides = $$('.hero-slide'); let active = 0;
  $$('[data-slide]').forEach(button => button.addEventListener('click', () => {
    active = (active + Number(button.dataset.slide) + slides.length) % slides.length;
    slides.forEach((slide, index) => slide.hidden = index !== active); $('#slide-count').textContent = `${active + 1} / ${slides.length}`;
  }));
  api('/api/cart').then(renderCart).catch(() => toast('Не удалось загрузить корзину. Обновите страницу.'));
})();
