'use strict';
document.querySelectorAll('[data-menu-order]').forEach(form => {
  const list = form.querySelector('ol'), input = form.elements.ids;
  form.addEventListener('click', event => {
    const button = event.target.closest('[data-move]');
    if (!button) return;
    const row = button.closest('[data-id]');
    const other = button.dataset.move==='up' ? row.previousElementSibling : row.nextElementSibling;
    if (!other) return;
    if (button.dataset.move==='up') list.insertBefore(row,other);
    else list.insertBefore(other,row);
    const rows = [...list.querySelectorAll('[data-id]')];
    input.value = JSON.stringify(rows.map(r=>Number(r.dataset.id)));
    rows.forEach((r,i)=>{
      r.querySelector('[data-move="up"]').disabled=i===0;
      r.querySelector('[data-move="down"]').disabled=i===rows.length-1;
    });
    form.querySelector('[data-order-note]').textContent='Порядок изменён — нажмите «Сохранить».';
    if (button.disabled) row.querySelector('[data-move]:not(:disabled)')?.focus();
    else button.focus();
  });
});
