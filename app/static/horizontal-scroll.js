(() => {
  const selector = '.category-tabs, .tag-tabs, .orders-kanban';
  let drag = null, ignoredStrip = null, ignoreClickUntil = 0;
  document.addEventListener('pointerdown', event => {
    const strip = event.target.closest(selector);
    if (!strip || event.pointerType !== 'mouse' || event.button !== 0 ||
        strip.scrollWidth <= strip.clientWidth || event.target.closest('input, textarea, select')) return;
    drag = {strip,id:event.pointerId,x:event.clientX,left:strip.scrollLeft,moved:false};
  });
  document.addEventListener('pointermove', event => {
    if (!drag || drag.id !== event.pointerId) return;
    const distance = event.clientX - drag.x;
    if (!drag.moved && Math.abs(distance) < 6) return;
    if (!drag.strip.isConnected) { finish(event); return; }
    if (!drag.moved) {
      drag.moved = true;
      drag.strip.setPointerCapture(event.pointerId);
      drag.strip.classList.add('dragging');
    }
    event.preventDefault(); drag.strip.scrollLeft = drag.left - distance;
  }, {passive:false});
  function finish(event) {
    if (!drag || drag.id !== event.pointerId) return;
    const {strip,moved} = drag;
    if (moved) { ignoredStrip = strip; ignoreClickUntil = performance.now() + 300; }
    drag = null; strip.classList.remove('dragging');
    if (strip.hasPointerCapture(event.pointerId)) strip.releasePointerCapture(event.pointerId);
  }
  document.addEventListener('pointerup', finish);
  document.addEventListener('pointercancel', finish);
  document.addEventListener('lostpointercapture', finish);
  window.addEventListener('blur', () => { if (drag) finish({pointerId:drag.id}); });
  document.addEventListener('click', event => {
    if (event.detail && performance.now() < ignoreClickUntil && ignoredStrip?.contains(event.target)) {
      event.preventDefault(); event.stopImmediatePropagation();
    }
  }, true);
  document.addEventListener('wheel', event => {
    const strip = event.target.closest(selector);
    if (!strip || event.ctrlKey || event.shiftKey || Math.abs(event.deltaX) >= Math.abs(event.deltaY)) return;
    const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? strip.clientWidth : 1);
    const limit = strip.scrollWidth - strip.clientWidth;
    if ((delta > 0 && strip.scrollLeft < limit - 1) || (delta < 0 && strip.scrollLeft > 1)) {
      event.preventDefault(); strip.scrollLeft += delta;
    }
  }, {passive:false});
  document.addEventListener('keydown', event => {
    const strip = event.target.closest('.category-tabs, .tag-tabs');
    if (!strip || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    const buttons = [...strip.querySelectorAll('button')], index = buttons.indexOf(event.target);
    if (index < 0) return;
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length-1 :
      Math.max(0,Math.min(buttons.length-1,index + (event.key === 'ArrowRight' ? 1 : -1)));
    event.preventDefault(); buttons[next].focus({preventScroll:true});
    buttons[next].scrollIntoView({block:'nearest',inline:'nearest'});
  });
})();
