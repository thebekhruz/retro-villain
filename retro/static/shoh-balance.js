/* «Баланс Шохруха» (ТЗ 02.10, п. 5): сколько выделили, сколько потратили,
   какой остаток. Всё вносит бухгалтер: выделить деньги — выдача из кассы на
   закуп (касса уменьшается, баланс Шоха растёт), расход по счёт-фактуре —
   дата, базар, сумма, комментарий (баланс Шоха уменьшается, касса — нет).
   История за месяц — только по кнопке «Месячный отчёт». */
(() => {
  const $ = id => document.getElementById(id);
  const number = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
  const fmt = value => number.format(Number(value || 0));
  const money = value => fmt(value) + ' сум';
  const dm = iso => iso ? iso.slice(8, 10) + '.' + iso.slice(5, 7) : '';
  const parse = value => Number(String(value || '').replace(/[\s  ]/g, '').replace(',', '.')) || 0;
  const tr = text => (document.documentElement.lang === 'uz' && globalThis.RetroI18n ? RetroI18n.translate(text) || text : text);
  const B = globalThis.RetroBusy;
  const MONTHS = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];

  let today = null, view = null, busy = false, historyOpen = false, requestNo = 0;

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') el.className = value;
      else if (key === 'text') el.textContent = value;
      else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
      else el.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat()) if (child !== null && child !== undefined && child !== false)
      el.append(child instanceof Node ? child : String(child));
    return el;
  }
  function longDay(iso, weekday) {
    return new Intl.DateTimeFormat('ru-RU', {...(weekday ? {weekday: 'short'} : {}), day: 'numeric', month: 'long', timeZone: 'UTC'})
      .format(new Date(iso + 'T12:00:00Z'));
  }
  function shiftIso(day, delta) {
    const d = new Date(day + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + delta);
    return d.toISOString().slice(0, 10);
  }
  function message(text, error = false) {
    const box = $('sb-message');
    box.textContent = text; box.hidden = !text; box.classList.toggle('is-error', error);
    if (text) globalThis.RetroToast?.show(text, error ? 'error' : 'ok');
  }
  const status = text => { const box = $('connection'); if (box) box.textContent = text; };
  const selectedDay = () => $('sb-date').value;

  async function write(url, body, method = 'POST') {
    const response = await RetroFinancialWrite(url, {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    let result = {};
    try { result = await response.json(); } catch { /* 204 */ }
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось сохранить. Проверьте поля.');
    return result;
  }
  async function run(action, success, button) {
    if (busy) return false;
    busy = true;
    let work = Promise.resolve().then(action);
    globalThis.RetroSave?.track(work);
    if (B && button) work = B.button(button, work);
    let failure = null;
    try { await work; } catch (error) { failure = error; }
    try { await load(); } catch { /* сообщение уже выведено */ }
    busy = false;
    if (failure) { message(failure.message, true); return false; }
    if (success) message(success);
    return true;
  }

  /* ── Отрисовка ─────────────────────────────────────────────────────── */
  function render() {
    const data = view;
    $('sb-balance').textContent = data.balance === null ? '—' : fmt(data.balance);
    $('sb-balance').classList.toggle('is-negative', Number(data.balance) < 0);
    $('sb-start').textContent = data.start === null ? '—' : fmt(data.start);
    $('sb-given').textContent = fmt(data.given_today);
    $('sb-spent').textContent = fmt(data.spent_today);
    const invoices = data.today.filter(row => row.kind === 'expense').length;
    $('sb-spent-sub').textContent = invoices ? invoices + ' ' + plural(invoices, 'счёт-фактура', 'счёт-фактуры', 'счёт-фактур') : '';
    $('sb-day-total').textContent = money(data.spent_today);
    const month = Number(data.month.slice(5, 7)) - 1;
    $('sb-month-title').textContent = MONTHS[month][0].toUpperCase() + MONTHS[month].slice(1) + ' ' + data.month.slice(0, 4) + ' · по ' + dm(data.date);
    $('sb-month-given').textContent = money(data.month_given);
    $('sb-month-spent').textContent = money(data.month_spent);
    $('sb-give-hint').textContent = data.cash_balance === null
      ? 'Касса бухгалтера на этот день не посчитана.'
      : 'В кассе бухгалтера ' + money(data.cash_balance) + '. Выдача уменьшит кассу и попадёт в «Операции за день».';

    const places = $('sb-exp-place'), keep = places.value;
    places.replaceChildren(new Option(tr('Базар'), ''), ...data.bazaars.map(name => new Option(name, name)));
    if (data.bazaars.includes(keep)) places.value = keep;
    if (!$('sb-exp-date').value || $('sb-exp-date').dataset.auto === '1') {
      $('sb-exp-date').value = data.date; $('sb-exp-date').dataset.auto = '1';
    }
    $('sb-exp-date').max = today;

    const box = $('sb-day-rows'); box.replaceChildren();
    data.today.slice().reverse().forEach(row => box.append(entryRow(row, false)));
    if (!data.today.length) box.append(h('div', {class: 'fd-empty is-left', text: 'За ' + longDay(data.date) + ' выдач и расходов нет.'}));

    $('sb-history').hidden = !historyOpen;
    $('sb-report').setAttribute('aria-expanded', String(historyOpen));
    $('sb-report').textContent = historyOpen ? 'Скрыть отчёт' : 'Месячный отчёт';
    if (historyOpen) {
      const list = $('sb-history-rows'); list.replaceChildren(
        h('div', {class: 'fd-thead sb-cols'}, ...['Дата', 'Что', 'Базар', 'Комментарий', 'Сумма', ''].map((text, i) => h('span', {class: i === 4 ? 'num' : null, text}))));
      (data.history || []).forEach(row => list.append(entryRow(row, true)));
      if (!(data.history || []).length) list.append(h('div', {class: 'fd-empty is-left', text: 'В этом месяце выдач и расходов нет.'}));
    }

    const closed = data.closed, chips = $('sb-status');
    chips.replaceChildren();
    if (closed) chips.append(h('span', {class: 'fd-chip is-closed'}, '🔒 Закрыто · ' + stamp(closed.closed_at) + ' · ' + closed.closed_by,
      h('small', {text: ' · ' + closed.name + ' — дни только для чтения'})));
    chips.hidden = !closed;
    document.querySelectorAll('#sb-give-form :is(input,button), #sb-exp-form :is(input,select,button), #sb-bazaar-row :is(input,button)')
      .forEach(el => { el.disabled = !!closed; });
  }
  function plural(n, one, few, many) {
    const a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return many; if (b === 1) return one; if (b > 1 && b < 5) return few; return many;
  }
  function stamp(iso) { return iso ? iso.slice(8, 10) + '.' + iso.slice(5, 7) + '.' + iso.slice(0, 4) + ' ' + iso.slice(11, 16) : ''; }
  /* Строка выдачи или расхода. За день первая колонка — время записи, в
     месячном отчёте — дата. Комментарий и базар вводит бухгалтер: переводчик
     их не трогает. */
  function entryRow(row, withDate) {
    const give = row.kind === 'give';
    const what = give ? (row.source === 'cashier' ? 'Выдано кассиром' : 'Выделено') : 'Расход';
    const line = h('div', {class: 'fd-row sb-cols ' + (give ? 'is-give' : 'is-expense'), 'data-busy-key': 'sb:' + row.kind + ':' + row.id});
    let remove = h('span', {class: 'fd-x-gap'});
    const deletable = !view.closed && (row.operation === 'shoh_expense' || row.operation === 'movement') && row.day === view.date;
    if (deletable) remove = h('button', {type: 'button', class: 'fd-x', text: '×', title: 'Удалить', 'aria-label': 'Удалить запись', onclick: event => {
      if (!confirm(tr((give ? 'Удалить выдачу Шоху ' : 'Удалить расход ') + money(row.amount) + '?'))) return;
      const url = row.operation === 'movement'
        ? '/api/accountant/operations/movement/' + row.id + '?date=' + row.day
        : '/api/accountant/shoh/expenses/' + row.id + '?date=' + row.day;
      run(async () => {
        const response = await fetch(url, {method: 'DELETE'});
        if (!response.ok) { let detail = 'Не удалось удалить.'; try { detail = (await response.json()).detail || detail; } catch { /* нет тела */ } throw new Error(detail); }
      }, give ? 'Выдача удалена.' : 'Расход удалён.', event.currentTarget);
    }});
    line.append(
      h('span', {class: 'sb-when', text: withDate ? dm(row.day) : (row.created_at || '').slice(11, 16) || '—'}),
      h('span', {class: 'sb-what', text: what}),
      h('span', {class: 'sb-place-cell' + (row.place ? '' : ' is-empty'), 'data-i18n': 'off', text: row.place || '—'}),
      h('span', {class: 'sb-note', 'data-i18n': give ? null : 'off', text: give ? '' : row.note || ''}),
      h('strong', {class: 'num sb-amount', text: (give ? '+' : '−') + fmt(row.amount)}),
      h('span', {class: 'fd-x-cell'}, remove));
    return line;
  }

  /* ── Загрузка ──────────────────────────────────────────────────────── */
  async function load() {
    const day = selectedDay(), sequence = ++requestNo;
    $('sb-day-label').textContent = longDay(day, true);
    $('sb-next').disabled = day >= today;
    $('sb-today').classList.toggle('is-active', day === today);
    $('sb-today').setAttribute('aria-pressed', String(day === today));
    try {
      const response = await fetch('/api/accountant/shoh?date=' + day + (historyOpen ? '&history=1' : ''), {cache: 'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить баланс Шоха.');
      if (sequence !== requestNo) return;
      view = data;
      render();
      $('sb-layout').setAttribute('aria-busy', 'false');
      status('Данные за ' + longDay(day));
    } catch (error) {
      if (sequence === requestNo) { message(error.message, true); status('Данные не загрузились'); }
      throw error;
    }
  }
  function go(day) {
    if (!day || day > today) { $('sb-date').value = view ? view.date : today; return; }
    if (view && day !== view.date && !(globalThis.RetroSave?.confirmLeave() ?? true)) { $('sb-date').value = view.date; return; }
    $('sb-date').value = day;
    $('sb-exp-date').dataset.auto = '1';
    const url = new URL(location.href); url.searchParams.set('date', day); history.replaceState(null, '', url);
    message('');
    load().catch(() => {});
  }
  $('sb-prev').addEventListener('click', () => go(shiftIso(selectedDay(), -1)));
  $('sb-next').addEventListener('click', () => go(shiftIso(selectedDay(), 1)));
  $('sb-today').addEventListener('click', () => go(today));
  $('sb-date').addEventListener('change', () => go(selectedDay()));
  $('sb-date-label').addEventListener('click', event => {
    if (event.target === $('sb-date')) return;
    event.preventDefault();
    try { $('sb-date').showPicker(); } catch { $('sb-date').focus(); }
  });
  $('sb-report').addEventListener('click', () => {
    historyOpen = !historyOpen;
    const work = load();
    if (B) B.button($('sb-report'), work); else work.catch(() => {});
  });

  /* ── Выделить деньги ───────────────────────────────────────────────── */
  $('sb-quick').addEventListener('click', event => {
    const button = event.target.closest('button[data-amount]');
    if (!button) return;
    $('sb-give-amount').value = fmt(button.dataset.amount);
    $('sb-give-amount').focus();
  });
  function submitGive(button) {
    const amount = parse($('sb-give-amount').value);
    if (!amount) { message('Укажите сумму для Шоха.', true); $('sb-give-amount').focus(); return Promise.resolve(false); }
    return run(() => write('/api/accountant/procurement', {date: view.date, recipient: 'Шох', purpose: 'закуп за день', amount: String(amount)}),
      'Выделено Шоху ' + money(amount) + '. Баланс и касса обновлены.', button || $('sb-give-form').querySelector('button[type=submit]'))
      .then(ok => { if (ok) $('sb-give-amount').value = ''; return ok; });
  }
  $('sb-give-form').addEventListener('submit', event => { event.preventDefault(); submitGive(event.submitter); });
  globalThis.RetroSave?.register($('sb-give-form'), () => submitGive());

  /* ── Расход по счёт-фактуре ────────────────────────────────────────── */
  $('sb-exp-date').addEventListener('input', () => { $('sb-exp-date').dataset.auto = '0'; });
  function submitExpense(button) {
    const day = $('sb-exp-date').value, place = $('sb-exp-place').value;
    const amount = parse($('sb-exp-amount').value), note = $('sb-exp-note').value.trim();
    if (!day) { message('Укажите дату расхода.', true); $('sb-exp-date').focus(); return Promise.resolve(false); }
    if (day > today) { message('Будущую дату указать нельзя.', true); $('sb-exp-date').focus(); return Promise.resolve(false); }
    if (!place) { message('Выберите базар.', true); $('sb-exp-place').focus(); return Promise.resolve(false); }
    if (!amount) { message('Укажите сумму расхода.', true); $('sb-exp-amount').focus(); return Promise.resolve(false); }
    return run(() => write('/api/accountant/shoh/expenses', {date: day, place, amount: String(amount), note}),
      'Расход записан: ' + place + ' · ' + money(amount) + '.', button || $('sb-exp-form').querySelector('button[type=submit]'))
      .then(ok => { if (ok) { $('sb-exp-amount').value = ''; $('sb-exp-note').value = ''; $('sb-exp-note').focus(); } return ok; });
  }
  $('sb-exp-form').addEventListener('submit', event => { event.preventDefault(); submitExpense(event.submitter); });
  globalThis.RetroSave?.register($('sb-exp-form'), () => submitExpense(),
    {dirty: () => !!($('sb-exp-amount').value.trim() || $('sb-exp-note').value.trim())});

  // Новый базар: бухгалтер пополняет список сам.
  $('sb-add-bazaar').addEventListener('click', () => {
    $('sb-bazaar-row').hidden = !$('sb-bazaar-row').hidden;
    $('sb-add-bazaar').setAttribute('aria-expanded', String(!$('sb-bazaar-row').hidden));
    if (!$('sb-bazaar-row').hidden) $('sb-bazaar-name').focus();
  });
  function saveBazaar() {
    const name = $('sb-bazaar-name').value.trim();
    if (!name) { message('Укажите название базара.', true); $('sb-bazaar-name').focus(); return Promise.resolve(false); }
    return run(() => write('/api/accountant/bazaars', {name}), 'Базар добавлен: ' + name + '.', $('sb-bazaar-save')).then(ok => {
      if (ok) { $('sb-bazaar-name').value = ''; $('sb-bazaar-row').hidden = true; $('sb-add-bazaar').setAttribute('aria-expanded', 'false'); $('sb-exp-place').value = name; }
      return ok;
    });
  }
  $('sb-bazaar-save').addEventListener('click', saveBazaar);
  $('sb-bazaar-name').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); saveBazaar(); } });
  globalThis.RetroSave?.register($('sb-bazaar-row'), () => saveBazaar());

  // Подпись «Базар» в списке — атрибут option, переводчик страницы её не видит:
  // после смены языка перерисовываем сами.
  document.querySelectorAll('[data-lang]').forEach(button => button.addEventListener('click', () => setTimeout(() => { if (view) render(); }, 0)));
  document.addEventListener('change', event => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement) || !input.classList.contains('fd-money')) return;
    const raw = input.value.trim();
    if (raw && /^[\d\s  .,]+$/.test(raw)) input.value = fmt(parse(raw));
  });

  (async () => {
    try {
      const config = await globalThis.RetroConfig;
      today = config.today;
      const requested = new URLSearchParams(location.search).get('date');
      const valid = requested && /^\d{4}-\d{2}-\d{2}$/.test(requested) && requested <= today;
      $('sb-date').max = today;
      $('sb-date').value = valid ? requested : today;
      await load();
    } catch (error) { message(error.message, true); }
  })();
})();
