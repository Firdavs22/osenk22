const selectAll = document.getElementById('select-all-products');
const selections = [...document.querySelectorAll('input[name="product_id"][form="bulk-products"]')];
if (selectAll) {
  selectAll.addEventListener('change', () => {
    selections.forEach(input => { input.checked = selectAll.checked; });
  });
  selections.forEach(input => input.addEventListener('change', () => {
    selectAll.checked = selections.length > 0 && selections.every(item => item.checked);
    selectAll.indeterminate = selections.some(item => item.checked) && !selectAll.checked;
  }));
}
