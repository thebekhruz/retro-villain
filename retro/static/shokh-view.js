/* Шох видит свой баланс (ТЗ 02.10, п. 5): остаток, выделено и потрачено за
   месяц, история. Вносить ничего не нужно — расходы записывает бухгалтер. */
(() => {
  const $ = id => document.getElementById(id);
  const number = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 0});
  const fmt = value => number.format(Number(value || 0));
  const dm = iso => iso ? iso.slice(8, 10) + '.' + iso.slice(5, 7) : '';
  const MONTHS = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];
  function line(row) {
    const give = row.kind === 'give';
    const item = document.createElement('div');
    item.className = 'shokh-purchase sv-row';
    const body = document.createElement('span');
    body.className = 'sv-row-body';
    const title = document.createElement('strong');
    title.textContent = give ? 'Выделено' : 'Расход' + (row.place ? ' · ' + row.place : '');
    const sub = document.createElement('small');
    sub.textContent = dm(row.day) + (row.note && !give ? ' · ' + row.note : '');
    // Комментарий бухгалтера — данные, а не надпись интерфейса.
    if (!give) sub.dataset.i18n = 'off';
    body.append(title, sub);
    const amount = document.createElement('b');
    amount.className = 'rm-num ' + (give ? 'is-give' : 'is-spent');
    amount.textContent = (give ? '+' : '−') + fmt(row.amount);
    item.append(body, amount);
    return item;
  }
  async function load() {
    const response = await fetch('/api/shokh/balance', {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить баланс.');
    $('sv-balance').textContent = data.balance === null ? '—' : fmt(data.balance);
    $('sv-date').textContent = 'Баланс на ' + dm(data.date);
    const month = Number(data.month.slice(5, 7)) - 1;
    $('sv-month-title').textContent = MONTHS[month][0].toUpperCase() + MONTHS[month].slice(1);
    $('sv-given').textContent = fmt(data.month_given);
    $('sv-spent').textContent = fmt(data.month_spent);
    const box = $('sv-history');
    box.replaceChildren(...(data.history || []).map(line));
    if (!(data.history || []).length) {
      const empty = document.createElement('p');
      empty.className = 'shokh-note';
      empty.textContent = 'В этом месяце выдач и расходов ещё нет.';
      box.append(empty);
    }
  }
  // Учредитель и администратор заходят сюда из панели — им нужна дорога назад.
  fetch('/api/config').then(r => r.json()).then(config => {
    if (config.role !== 'shokh') $('shokh-back').hidden = false;
  }).catch(() => {});
  load().catch(error => globalThis.RetroToast?.show(error.message, 'error'));
})();
