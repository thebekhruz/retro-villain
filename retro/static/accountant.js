const $ = id => document.getElementById(id);
const number = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
const money = value => number.format(Number(value || 0)) + ' сум';
let today, current, requestNo = 0, catalog = [], saving = false;
let shiftTab = 'all';
const special = {
  reserve_dividends_transfer: {account: "dividends", kind: "transfer", label: "Отложить в сейф", impact: "Уменьшает деньги на расходы; увеличивает резерв дивидендов."},
  reserve_dividends_withdrawal: {account: "dividends", kind: "withdrawal", label: "Выдать собственнику из сейфа", impact: "Уменьшает только резерв дивидендов. Повторного списания из кассы нет."},
  reserve_usd_deposit: {account: "usd", kind: "deposit", label: "Поступили реальные USD", impact: "Увеличивает реальные USD. Конвертация сумов не проводится. Укажите источник в наименовании."},
  reserve_usd_withdrawal: {account: "usd", kind: "withdrawal", label: "Выданы реальные USD", impact: "Уменьшает только реальные USD, не сумовую кассу."},
  reserve_shoh_withdrawal: {account: "shoh", kind: "withdrawal", label: "Принята закупка от Шоха", impact: "Уменьшает подотчёт Шоха по подтверждённой накладной. Деньги повторно не списываются."}
};
function itemInfo(code) {
  for (const group of catalog) { const item = group.items.find(x => x.code === code); if (item) return {category: group.label, label: item.label}; }
  return {category: "Операция", label: code || "—"};
}
function expenseImpact() {
  const code = $("expense-item").value, item = special[code], income = incomeCodes.includes(code);
  $('expense-paid-label').hidden = !!item || income;
  $('expense-paid').disabled = !!item || income;
  $("expense-currency").textContent = item?.account === "usd" ? "USD" : "сум";
  $("expense-impact").textContent = item ? item.impact : income ? 'Поступление увеличивает остаток бухгалтера.' : 'Пустое поле «Оплачено сейчас» означает полную оплату. Ноль или меньшая сумма создадут долг; деньги уменьшатся только на фактически оплаченное.';
  updateButtons();
}
function updateButtons() {
  const ready = current && current.date === selectedDay() && !saving;
  document.querySelectorAll(".finance-layout form button[type=submit]").forEach(b => b.disabled = !ready);
  if (!ready) {
    $('pay-all').disabled = true;
    document.querySelectorAll('.shift-pay').forEach(input => input.disabled = true);
    return;
  }
  // Без данных кассира за день выплата всё равно упадёт на сервере, поэтому
  // запираем и строки, и «Выдать всем» — одним правилом, а не по-разному.
  const noCash = current.ledger.cash_balance === null;
  document.querySelectorAll('.shift-pay').forEach(input => {
    input.disabled = noCash || input.dataset.locked === '1';
    if (noCash) input.title = 'Нет данных кассира за этот день — выплату записать нельзя';
  });
  const code = $("expense-item").value, entry = special[code], income = incomeCodes.includes(code);
  const needsCash = !entry && !income || entry?.kind === "transfer";
  const unpaid = !entry && $('expense-paid').value === '0';
  $("other-expense-form").querySelector("button").disabled = needsCash && !unpaid && current.ledger.cash_balance === null;
  $('payroll-confirm-form').querySelector('button').disabled = current.ledger.payroll_confirmed || current.payroll.unknown_count > 0 || !current.employees.length;
  const payable = current.ledger.accruals.some(row => Number(row.debt) > 0);
  $('salary-payment-form').querySelector('button').disabled = !payable || current.ledger.cash_balance === null;
  $('pay-all').disabled = !payable || current.ledger.cash_balance === null;
  $('debt-payment-form').querySelector('button').disabled = !current.ledger.manual_debts.length || current.ledger.cash_balance === null;
  $('cash-opening-form').querySelector('button').disabled = current.ledger.cash_opening !== null || current.expected_cashier === null;
}
function categoryChanged() {
  const group = catalog.find(g => g.code === $("expense-category").value);
  options($("expense-item"), (group?.items || []).map(i => ({id:i.code,label:i.label})), "Выберите тип");
  if (group?.code === 'income') {
    const opening = [...$("expense-item").options].find(option => option.value === 'income_opening');
    if (opening) opening.disabled = true;
  }
  expenseImpact();
}

function previousDay(day) { const d = new Date(day + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() - 1); return d.toISOString().slice(0, 10); }
function nextDay(day) { const d = new Date(day + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + 1); return d.toISOString().slice(0, 10); }
// Точку в конце подписи ставит сама подпись, поэтому «г.» тут лишнее:
// иначе на экране выходит «2026 г.. Hikvision синхронизирован».
function formattedDay(day) { return new Intl.DateTimeFormat('ru-RU', {day: 'numeric', month: 'long', year: 'numeric', timeZone: 'Asia/Tashkent'}).format(new Date(day + 'T12:00:00+05:00')).replace(/\s*г\.$/, ''); }
function shortDay(day) { return new Intl.DateTimeFormat('ru-RU', {weekday: 'short', day: 'numeric', month: 'long', timeZone: 'Asia/Tashkent'}).format(new Date(day + 'T12:00:00+05:00')); }
function entryTime(value) { return value ? new Date(value).toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Tashkent'}) : '—'; }
function message(value, error = false) { const n = $('accountant-message'); n.textContent = value; n.hidden = !value; n.setAttribute('role', error ? 'alert' : 'status'); globalThis.RetroToast?.show(value, error ? 'error' : 'ok'); }
function node(tag, cls, value) { const n = document.createElement(tag); if (cls) n.className = cls; if (value !== undefined) n.textContent = value; return n; }
// Единое пустое состояние: знак, объяснение и, если есть, следующий шаг.
function emptyState(glyph, text, extra) {
  const box = node('div', 'empty-state' + (extra ? ' ' + extra : ''));
  const sign = node('span', '', glyph); sign.setAttribute('aria-hidden', 'true');
  box.append(sign, node('p', '', text));
  return box;
}
function options(select, items, placeholder) { const old = select.value; select.replaceChildren(new Option(placeholder, '')); items.forEach(x => select.add(new Option(x.label, String(x.id)))); if (items.some(x => String(x.id) === old)) select.value = old; }
const selectedDay = () => $('accountant-date').value;

function attendanceHealth(value) {
  const status = value?.status || 'starting';
  const states = {
    ok: ['Hikvision: работает', 'Hikvision синхронизирован.'],
    starting: ['Hikvision: подключение', 'Hikvision подключается; отсутствие входа пока не считается прогулом.'],
    stale: ['Hikvision: данные устарели', 'Данные Hikvision устарели; отсутствие входа не считается прогулом.'],
    not_configured: ['Hikvision: не настроен', 'Hikvision не настроен; отсутствие входа не считается прогулом.']
  };
  const state = states[status] || ['Hikvision: нет связи', 'Hikvision недоступен; отсутствие входа не считается прогулом.'];
  return {short: state[0], detail: state[1]};
}

const STATUS_TEXT = {on_time: 'Вовремя', late: 'Опоздал', missing: 'Не пришёл', unlinked: 'Нет привязки', unavailable: 'Нет данных'};

// Оплата строкой: у выдачи есть ключ идемпотентности, поэтому двойной клик или
// перезагрузка посреди запроса не выдаст зарплату дважды.
async function payAccrual(accrualId, amount) {
  const response = await RetroFinancialWrite('/api/accountant/salary-payments', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({date: selectedDay(), accrual_id: Number(accrualId), amount: String(amount)})
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось записать выплату.');
}

/* ── Смена: кому и сколько выдаём за отработанный день ──────────────────
   До подтверждения начислений платить нечему, поэтому показываем реестр с
   предварительным расчётом. После подтверждения появляются начисления с
   долгом, и выдачу вводят прямо в строке. */
function renderShift(data) {
  const l = data.ledger, confirmed = l.payroll_confirmed;
  const container = $('shift-rows');
  container.replaceChildren();
  $('shift-title').textContent = 'Смена ' + shortDay(data.date);

  const shift = AccountantLogic.shiftRows(data);
  const {stale, own, rows} = shift;

  const tabs = AccountantLogic.shiftTabs(rows);
  if (!tabs.some(([key]) => key === shiftTab)) shiftTab = 'all';
  const tabsBox = $('shift-tabs'); tabsBox.replaceChildren();
  tabs.forEach(([key, label, count]) => {
    const button = node('button', 'emp-tab' + (shiftTab === key ? ' is-active' : ''), label);
    button.type = 'button';
    button.append(node('small', '', String(count)));
    button.addEventListener('click', () => { shiftTab = key; renderShift(data); });
    tabsBox.append(button);
  });

  const shown = rows.filter(row => AccountantLogic.matchesTab(row, shiftTab));

  shown.forEach(row => {
    const line = node('div', 'rm-table-row shift-cols');
    if (row.id !== null) line.dataset.accrual = String(row.id);
    // Отметка «выдано полностью»: один клик вместо набора суммы вручную.
    const mark = node('button', 'rm-check' + (row.debt !== null && Number(row.debt) === 0 ? ' is-on' : ''),
      row.debt !== null && Number(row.debt) === 0 ? '✓' : '');
    mark.type = 'button';
    mark.title = 'Отметить выдачу полностью';
    mark.disabled = row.id === null || !(Number(row.debt) > 0) || l.cash_balance === null;
    if (l.cash_balance === null && Number(row.debt) > 0) mark.title = 'Нет данных кассира за этот день';
    mark.classList.add('shift-mark');
    mark.addEventListener('click', async () => {
      try { await payAccrual(row.id, row.debt); await loadDay(); message('Выдано: ' + row.name + ' · ' + money(row.debt)); }
      catch (error) { message(error.message, true); }
    });

    const who = node('div', 'emp-cell-who');
    const text = node('div', 'emp-who-text');
    const name = node('div', 'emp-who-name', row.name);
    if (row.noHik) { const flag = node('span', 'rm-flag-nohik', '⊘ без Hikvision'); flag.title = 'Не зарегистрирован в Hikvision — присутствие отмечается вручную'; name.append(' ', flag); }
    text.append(name, node('div', 'emp-who-role', row.role + (row.day !== data.date ? ' · смена ' + row.day.split('-').reverse().join('.') : '')));
    who.append(node('span', 'rm-avatar', row.name.split(' ').map(w => w[0]).slice(0, 2).join('').toUpperCase()), text);

    const time = node('div', 'emp-cell-entry');
    time.append(node('div', 'emp-entry-time' + (row.status === 'late' ? ' is-late' : row.first_entry ? '' : ' is-none'),
      row.showEntry ? entryTime(row.first_entry) : '·'));

    const statusCell = node('div', 'emp-cell-status');
    statusCell.append(node('span', 'rm-pill ' + (row.status || 'unavailable'), STATUS_TEXT[row.status] || '—'));

    const accrued = node('div', 'emp-cell-num rm-num' + (row.rate === null ? ' is-missing' : ''),
      row.rate === null ? 'Нет ставки' : row.accrued === null ? 'Не рассчитано' : number.format(Number(row.accrued)));

    const payCell = node('div', 'shift-pay-cell');
    if (row.id === null) {
      payCell.append(node('span', 'rm-cell-note', confirmed ? '' : 'после подтверждения'));
    } else {
      const input = node('input', 'rm-cell-input shift-pay');
      input.type = 'number'; input.min = '0'; input.step = '0.01'; input.placeholder = '—';
      input.setAttribute('aria-label', 'Выдать сейчас · ' + row.name);
      // Выдавать поверх закрытого долга нечего: иначе это переплата, которую
      // тот же экран потом покажет как ошибку. Поле запираем, не прячем.
      if (!(Number(row.debt) > 0)) {
        input.dataset.locked = '1';
        input.disabled = true;
        input.title = row.status === 'missing' ? 'Входа нет — начисление 0 сум' : 'Долг закрыт';
      }
      const commit = async () => {
        const value = input.value.trim();
        if (!value || Number(value) <= 0) return;
        input.disabled = true;
        try { await payAccrual(row.id, value); await loadDay(); message('Выплата записана: ' + row.name + ' · ' + money(value)); }
        catch (error) { input.disabled = false; message(error.message, true); }
      };
      input.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); commit(); } });
      input.addEventListener('blur', commit);
      const owed = Number(row.debt);
      payCell.append(input, node('div', 'rm-cell-note' + (owed > 0 ? ' is-owed' : ''),
        owed > 0 ? 'долг ' + number.format(owed) : 'выдано ' + number.format(Number(row.paid))));
    }
    line.append(mark, who, time, statusCell, accrued, payCell);
    container.append(line);
  });
  if (!shown.length) container.append(emptyState('▤', confirmed ? 'В этом срезе никого нет.' : 'Реестр за этот день пуст.'));

  const totals = shift.totals;
  $('shift-accrued').textContent = number.format(totals.accrued);
  $('shift-paid').textContent = number.format(totals.paid);
  $('shift-paid-count').textContent = confirmed ? totals.settled + ' из ' + totals.count + ' выдано' : '';
  $('shift-sum').textContent = totals.owed > 0 ? 'к выдаче ' + money(totals.owed)
    : confirmed ? 'всё выдано' : 'предварительно ' + money(data.payroll.draft_total);
  $('shift-note').textContent = (confirmed
    ? 'Введите сумму в строке или отметьте галочкой, чтобы выдать долг целиком.'
    : 'Начисления за смену ещё не подтверждены: ' + data.payroll.unknown_count + ' сотрудников без расчёта.')
    + (totals.staleOwed > 0 ? ' Сверху показан долг прошлых смен — ' + money(totals.staleOwed) + '.' : '');
  $('payroll-confirm-form').hidden = confirmed;

  const noHik = rows.filter(row => row.noHik).length;
  $('hik-strip').hidden = !noHik;
  if (noHik) $('hik-strip-text').textContent = 'Присутствие ' + noHik + ' сотрудников отмечается вручную: их нет в Hikvision.';
}

/* ── Подотчёт Шоха ────────────────────────────────────────────────────
   Баланс собирается из выдач (deposit) и принятых накладных (withdrawal),
   поэтому «на начало дня» считаем по записям прошлых дней, а не отдельным
   запросом. Список покупок с фото появится вместе с модулем закупа. */
function renderShoh(data) {
  const shoh = data.reserves.shoh;
  const {known, start, given, accepted} = AccountantLogic.shohPosition(shoh);
  $('shoh-start').textContent = known ? number.format(start) : 'Не задан';
  $('shoh-given').textContent = number.format(given);
  $('shoh-spent').textContent = number.format(accepted);
  $('shoh-balance').textContent = known ? number.format(Number(shoh.balance)) : 'Не задан';
  $('shoh-sum').textContent = known ? 'на руках ' + money(shoh.balance) : 'начальный остаток не задан';

  renderShohBuys(data.date);
  const list = $('shoh-entries'); list.replaceChildren();
  const todayEntries = shoh.entries;
  todayEntries.forEach(entry => {
    const row = node('div', 'shoh-entry');
    row.append(node('span', 'shoh-entry-kind', entry.kind === 'deposit' ? 'Выдано' : entry.kind === 'opening' ? 'Начальный остаток' : 'Принята закупка'),
      node('span', 'shoh-entry-note', entry.note || '—'),
      node('strong', 'rm-num', (entry.kind === 'withdrawal' ? '−' : '+') + number.format(Number(entry.amount))));
    list.append(row);
  });
  if (!todayEntries.length) list.append(emptyState('◇', 'За этот день движений по подотчёту не было.', 'compact'));
}

/* ── Покупки Шоха: проверка и приёмка ────────────────────────────────────
   Приёмка уменьшает подотчёт и кассу второй раз не списывает: наличные ушли
   ещё при выдаче под отчёт. Поэтому это отдельное действие, а не расход. */
async function renderShohBuys(day) {
  const box = $('shoh-buys');
  box.replaceChildren();
  let rows = [];
  try {
    const response = await fetch('/api/accountant/shokh/purchases?date=' + encodeURIComponent(day),
      {cache: 'no-store'});
    if (!response.ok) throw new Error('Не удалось загрузить покупки закупа.');
    rows = (await response.json()).purchases;
  } catch (error) {
    box.append(node('p', 'rm-section-note', error.message));
    return;
  }
  const pending = rows.filter(row => row.accepted_at === null).length;
  $('shoh-buys-count').textContent = rows.length
    ? rows.length + ' покупок · ждут приёмки ' + pending
    : 'Шох ещё не вносил покупки за этот день';
  if (!rows.length) {
    box.append(node('p', 'rm-section-note', 'Покупок за этот день нет.'));
    return;
  }
  rows.forEach(row => {
    const line = node('div', 'rm-table-row shoh-buy-cols' + (row.price_above_usual ? ' is-flagged' : ''));
    line.append(node('span', 'muted', row.created_at.slice(11, 16)),
      node('span', 'muted', row.point));
    const item = node('div', 'shoh-buy-item');
    if (row.has_photo) {
      const image = document.createElement('img');
      image.className = 'shoh-buy-photo'; image.alt = 'Фото покупки';
      image.loading = 'lazy'; image.src = '/api/shokh/photo/' + row.id;
      item.append(image);
    } else {
      const mark = node('span', 'shoh-no-photo', '⊘');
      mark.title = 'Без фото';
      item.append(mark);
    }
    item.append(node('span', '', row.item));
    const price = node('div', 'shoh-buy-price rm-num' + (row.price_above_usual ? ' is-above' : ''));
    price.append(node('b', '', number.format(Number(row.price))));
    if (row.usual_price !== null) price.append(node('small', '', 'обычно ' + number.format(Number(row.usual_price))));
    const check = node('div', 'shoh-buy-check');
    if (row.accepted_at !== null) {
      check.append(node('span', 'shoh-accepted', 'принято бухгалтером'));
    } else {
      if (row.price_above_usual) check.append(node('span', 'shoh-flag-text',
        'дороже обычного' + (row.price_delta_percent ? ' на ' + Math.round(Number(row.price_delta_percent)) + '%' : '')));
      else check.append(node('span', 'shoh-norm', '✓ в норме'));
      const accept = node('button', 'shoh-accept', 'Принять');
      accept.type = 'button';
      accept.addEventListener('click', async () => {
        accept.disabled = true;
        try {
          const response = await RetroFinancialWrite(
            '/api/accountant/shokh/purchases/' + row.id + '/accept',
            {method: 'POST', headers: {'Content-Type': 'application/json'},
             body: JSON.stringify({date: day})});
          const result = await response.json();
          if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось принять покупку.');
          await loadDay();
          message('Покупка принята: ' + row.item + ' · ' + money(row.total));
        } catch (error) { accept.disabled = false; message(error.message, true); }
      });
      check.append(accept);
    }
    line.append(item, node('div', 'num rm-num', row.quantity + ' ' + row.unit), price,
      node('strong', 'num rm-num', number.format(Number(row.total))), check);
    box.append(line);
  });
}

/* ── Оклады, выданные сегодня ───────────────────────────────────────────── */
function renderSalary(data) {
  const l = data.ledger;
  const paidToday = l.movements.filter(item => item.type === 'salary_payment');
  const box = $('salary-today'); box.replaceChildren();
  paidToday.forEach(item => {
    const row = node('div', 'salary-row');
    row.append(node('span', 'salary-row-name', item.description),
      node('strong', 'rm-num', number.format(Number(item.amount))));
    box.append(row);
  });
  if (!paidToday.length) box.append(emptyState('◇', 'Сегодня оклады не выдавались.', 'compact'));
  const total = paidToday.reduce((sum, item) => sum + Number(item.amount), 0);
  $('salary-sum').textContent = total ? money(total) : 'ничего не выдано';
}

/* ── Проверки ──────────────────────────────────────────────────────────
   Считаем на клиенте по данным дня: сервер отдаёт факты, а не заключения.
   Клик по пункту подсвечивает строку, из-за которой он появился. */
function renderChecks(data) {
  // Какие проверки вообще возможны — решает accountant-logic.js; здесь только
  // подписи и переход к виновной строке.
  const TEXT = {
    'Остаток ушёл в минус': f => 'На конец дня ' + money(f.amount) + '.',
    'Невыданные смены': f => 'Долг с прошлых дней ' + money(f.amount) + '.',
    'Без ставки': f => 'Этим сотрудникам смена не начисляется, пока не укажете ставку.',
    'Смена не подтверждена': () => 'Расчёт готов — подтвердите начисления, чтобы выдавать деньги.',
    'Неоплаченные расходы': f => 'Долг поставщикам ' + money(f.amount) + '.',
    'Касса не передана': () => 'Передачи от кассира за ' + formattedDay(data.date) + ' ещё нет.',
    'Отстаём от недельных дивидендов': f => 'До плана к этому дню не хватает ' + money(Math.round(f.amount)) + '.'
  };
  const items = AccountantLogic.dayChecks(data).map(item => ({
    level: item.level, accrualId: item.accrualId,
    text: item.text + (item.sub.count ? ': ' + item.sub.count : ''),
    sub: (TEXT[item.text] || (() => ''))(item.sub)
  }));

  const box = $('finance-checks'); box.replaceChildren();
  const badge = $('checks-badge');
  const bad = items.filter(i => i.level === 'bad').length;
  badge.textContent = items.length ? items.length : 'чисто';
  badge.classList.toggle('is-bad', bad > 0);
  badge.classList.toggle('is-clean', !items.length);
  $('checks-card').classList.toggle('is-clean', !items.length);
  if (!items.length) { box.append(node('p', 'checks-ok', 'Ошибок не найдено.')); return; }
  items.forEach(item => {
    const button = node('button', 'check-item', undefined);
    button.type = 'button';
    button.append(node('span', 'check-dot is-' + item.level));
    const body = node('span', 'check-body');
    body.append(node('span', 'check-text', item.text), node('span', 'check-sub', item.sub));
    button.append(body);
    if (item.accrualId != null) button.addEventListener('click', () => {
      $('shift-section').open = true;
      const row = document.querySelector('[data-accrual="' + item.accrualId + '"]');
      if (!row) return;
      row.scrollIntoView({block: 'center', behavior: 'smooth'});
      row.classList.add('is-flagged');
      setTimeout(() => row.classList.remove('is-flagged'), 2200);
    });
    else button.disabled = true;
    box.append(button);
  });
}

/* ── Дивиденды · неделя ────────────────────────────────────────────────
   Сумму ставит учредитель; бухгалтер видит цель, отставание от плана и
   рекомендацию на сегодня, а вносит её обычной строкой «Отложить в сейф». */
function renderDividends(data) {
  const week = data.dividends_week, card = $('dividends-card');
  card.hidden = !week;
  if (!week) return;
  const target = week.target === null ? null : Number(week.target), set = Number(week.collected);
  const dm = iso => iso.slice(8, 10) + '.' + iso.slice(5, 7);
  $('dividends-week').textContent = dm(week.start) + '–' + dm(week.end);
  $('dividends-target').textContent = target === null ? 'не задано' : money(target);
  const source = week.target_source;
  $('dividends-changed').textContent = !source ? 'Учредитель ещё не поставил сумму на неделю.' : source.inherited
    ? 'Как на прошлой неделе' : 'изменено учредителем ' + new Date(source.changed_at).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Tashkent'});
  $('dividends-fill').style.width = target ? Math.min(100, set / target * 100) + '%' : '0%';
  $('dividends-pace').hidden = !target;
  $('dividends-pace').style.left = target ? Math.min(100, Number(week.pace) / target * 100) + '%' : '0';
  $('dividends-set').textContent = 'Отложено ' + number.format(set);
  $('dividends-left').textContent = target ? 'осталось ' + number.format(Number(week.left)) : '';
  const status = $('dividends-status');
  status.textContent = target === null ? '' : week.done ? 'Недельная сумма собрана' : week.behind
    ? 'Отстаём от плана на ' + money(Math.round(Number(week.due) - set)) : 'Идём по плану';
  status.className = 'dividends-status ' + (week.behind ? 'is-behind' : 'is-ok');
  $('dividends-today').textContent = Number(week.collected_today) ? 'Сегодня отложено: ' + money(week.collected_today) : 'Сегодня ещё не откладывали';
  const suggest = week.suggest_today === null ? 0 : Number(week.suggest_today);
  $('dividends-hint').textContent = target && Number(week.left) > 0
    ? 'Рекомендуем сегодня ' + money(suggest) + ' · до выдачи ' + week.days_left + ' ' + (week.days_left === 1 ? 'день' : week.days_left < 5 ? 'дня' : 'дней')
    : target ? 'Выдача ' + formattedDay(week.payout_day) : '';
  $('dividends-fill-button').hidden = !(target && Number(week.left) > 0);
  card.classList.toggle('is-behind', Boolean(week.behind));
}

function prefillDividends() {
  const week = current && current.dividends_week;
  if (!week) return;
  $('expense-category').value = 'reserves';
  categoryChanged();
  $('expense-item').value = 'reserve_dividends_transfer';
  expenseImpact();
  $('expense-note').value = 'Дивиденды в сейф · неделя ' + week.week;
  $('expense-amount').value = week.suggest_today === null ? '' : String(Math.round(Number(week.suggest_today)));
  $('journal-section').open = true;
  $('other-expense-form').scrollIntoView({block: 'center', behavior: 'smooth'});
  $('expense-amount').focus({preventScroll: true});
}

function renderStaff(data) {
  const rows = data.employees.filter(row => row.status === 'late'), container = $('late-list');
  const health = attendanceHealth(data.attendance);
  $('staff-summary').textContent = 'Опоздавших после 10:00: ' + rows.length +
    ' · автоматический штраф не начисляется. ' + health.detail;
  $('attendance-chip').textContent = health.short;
  $('attendance-banner-text').textContent = health.detail;
  $('attendance-footnote').textContent = health.detail;
  container.replaceChildren();
  if (!rows.length) container.append(emptyState('✓', 'За этот день никто не опоздал.'));
  rows.forEach(row => {
      const card = node('article', 'staff-row is-' + row.status), identity = node('div'), detail = node('div', 'staff-line');
      identity.append(node('div', 'staff-name', row.name), node('div', 'staff-role', row.role + (row.rate === null ? ' · Нет ставки' : ' · ' + money(row.rate))));
      const entry = entryTime(row.first_entry);
      detail.append(node('span', '', 'Вход: ' + entry + (row.exception ? ' · разовое разрешение' : '')), node('strong', '', row.payable === null ? 'Не рассчитано' : money(row.payable)));
      card.append(identity, node('span', 'staff-status late', 'Опоздал'), detail); container.append(card);
  });
}
function renderLedger(data) {
  const l = data.ledger, confirmed = l.payroll_confirmed, hasCashierData = l.cash_balance !== null;
  const cashierDay = data.date;
  $('cashier-transfer-label').textContent = 'Касса за ' + formattedDay(cashierDay);
  $('expected-cashier').textContent = l.cash_flow.missing_day
    ? (data.expected_cashier === null ? 'Передача кассы за ' + formattedDay(cashierDay) + ' ещё не записана. ' : 'Для переноса остатка не хватает передачи кассы на ' + formattedDay(l.cash_flow.missing_day) + '. ') + (data.cashier_error || '')
    : 'Получено от кассира за ' + formattedDay(cashierDay) + ': ' + money(data.expected_cashier) + '. Учёт начат с ' + formattedDay(l.cash_flow.first_day);
  $('cashier-transfer-total').textContent = data.expected_cashier === null ? 'Нет данных' : money(data.expected_cashier);
  $('opening-cash-total').textContent = l.cash_flow.opening_balance !== null ? money(l.cash_flow.opening_balance) : 'Не рассчитано';
  $('other-receipts-total').textContent = money(l.cash_flow.other_receipts);
  $('cash-opening-note').textContent = l.cash_opening ? 'Начальный остаток: ' + money(l.cash_opening.amount) + ' на ' + formattedDay(l.cash_opening.day) + '.'
    : 'Начальный остаток ещё не задан. Выберите первый день достоверного учёта и укажите подтверждённую сумму.';
  const unpaidDay = l.accruals.filter(r => r.work_day === data.date).reduce((sum, r) => sum + Number(r.debt), 0);
  $('payroll-draft-total').textContent = money(confirmed ? unpaidDay : data.payroll.draft_total);
  $('payroll-state').textContent = confirmed ? 'Осталось выплатить за выбранную смену' : 'Предварительно · ' + data.payroll.unknown_count + ' не рассчитаны';
  $('payroll-paid-total').textContent = money(l.salary_paid_on_day);
  $('salary-unallocated-note').textContent = 'Без привязки к начислениям: ' + money(l.salary_unallocated_on_day) + '. Эти расходы не погашают долг сотрудника.';
  options($('salary-accrual'), l.accruals.filter(row => Number(row.debt) > 0).map(row => ({id:row.id,label:row.name + ' · ' + row.work_day + ' · долг ' + money(row.debt)})), 'Выберите сотрудника');
  $('payroll-debt-total').textContent = money(l.salary_debt);
  $('manual-debt-total').textContent = money(l.manual_debt_total);
  const debtTotal = Number(l.salary_debt) + Number(l.manual_debt_total);
  $('all-debt-total').textContent = money(debtTotal);
  $('debt-card').classList.toggle('is-owed', debtTotal > 0);
  $('debt-card-note').textContent = debtTotal > 0
    ? 'Долги к оплате на конец дня.'
    : 'Непогашенных долгов на конец дня нет.';
  $('finance-cash-total').textContent = hasCashierData ? money(l.cash_balance) : '—';
  $('day-outflows').textContent = money(Number(l.cash_flow.salary_paid) + Number(l.cash_flow.other_outflows));
  const reserves = data.reserves;
  let reservesKnown = 0;
  for (const account of ['dividends', 'usd', 'shoh']) {
    const value = reserves[account].balance;
    if (value !== null) reservesKnown += 1;
    const text = value === null ? 'Не задан' : account === 'usd' ? number.format(Number(value)) + ' USD' : money(value);
    // Подотчёт Шоха показан дважды: карточкой в своей секции и строкой резервов.
    const target = account === 'shoh' ? $('shoh-balance-rail') : $(account + '-balance');
    if (target) target.textContent = text;
  }
  $('reserves-lines').hidden = reservesKnown === 0;
  $('reserves-empty').hidden = reservesKnown > 0;
  $('monthly-balance').textContent = money(reserves.monthly.total);
  $('monthly-note').textContent = 'Текущие ставки реестра; не остаток долга за выбранный месяц';
  const journal = $('finance-journal'); journal.replaceChildren();
  const breakdown = {};
  async function mutateHandover(method, day, body) {
    const response = await fetch('/api/accountant/handover/' + encodeURIComponent(day), {method, headers: {'Content-Type':'application/json'}, body: body ? JSON.stringify(body) : undefined});
    if (!response.ok) { const result = await response.json(); throw new Error(result.detail || 'Не удалось изменить приход.'); }
    await loadDay(); message(method === 'DELETE' ? 'Приход удалён.' : 'Приход изменён.');
  }
  async function mutateOperation(item, body) {
    const response = await fetch('/api/accountant/operations/' + encodeURIComponent(item.operation) + '/' + item.id, {
      method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)
    });
    if (!response.ok) { const result = await response.json(); throw new Error(result.detail || 'Не удалось изменить операцию.'); }
    await loadDay(); message('Операция изменена.');
  }
  async function deleteOperation(item) {
    const response = await fetch('/api/accountant/operations/' + encodeURIComponent(item.operation) + '/' + item.id +
      '?date=' + encodeURIComponent(item.day), {method: 'DELETE'});
    if (!response.ok) { const result = await response.json(); throw new Error(result.detail || 'Не удалось удалить операцию.'); }
    await loadDay(); message('Операция удалена.');
  }
  function editHandover(item, total, actions) {
    total.replaceChildren();
    const input = document.createElement('input'); input.type = 'number'; input.min = '0'; input.step = '0.01'; input.value = item.amount; input.className = 'operation-edit-input';
    const save = node('button', 'operation-edit', 'Сохранить'); save.type = 'button'; save.addEventListener('click', async () => { try { await mutateHandover('PUT', item.day, {date:item.day, amount:input.value, note:'Исправлено вручную'}); } catch (error) { message(error.message, true); } });
    total.append(input, actions); actions.replaceChildren(save);
    input.focus();
  }
  function editOperation(item, cells, actions) {
    const typeCell = cells[1], nameCell = cells[2], totalCell = cells[3];
    const categorySelect = document.createElement('select');
    catalog.forEach(group => categorySelect.add(new Option(group.label, group.code)));
    categorySelect.value = catalog.find(group => group.items.some(entry => entry.code === item.item_code))?.code || '';
    const typeSelect = document.createElement('select');
    const fillTypes = () => {
      const group = catalog.find(entry => entry.code === categorySelect.value);
      options(typeSelect, (group?.items || []).map(entry => ({id: entry.code, label: entry.label})), 'Выберите тип');
      typeSelect.value = item.item_code || '';
    };
    categorySelect.addEventListener('change', fillTypes); fillTypes();
    const nameInput = document.createElement('input'); nameInput.value = item.description.split(' · ').slice(1).join(' · ') || item.description;
    const amountInput = document.createElement('input'); amountInput.type = 'number'; amountInput.min = '0'; amountInput.step = '0.01'; amountInput.value = item.amount;
    const save = node('button', 'operation-edit', 'Сохранить'); save.type = 'button';
    save.addEventListener('click', async () => {
      try {
        await mutateOperation(item, {date: item.day, item_code: typeSelect.value, note: nameInput.value, amount: amountInput.value});
      } catch (error) { message(error.message, true); }
    });
    cells[0].replaceChildren(categorySelect); typeCell.replaceChildren(typeSelect);
    nameCell.replaceChildren(nameInput); totalCell.replaceChildren(amountInput, actions);
    actions.replaceChildren(save);
  }
  const addRow = (category, type, name, amount, unit, cashEffect, item = null) => {
    const tr = node('tr');
    const cells = [node('td', '', category), node('td', '', type), node('td', '', name)];
    tr.append(...cells);
    const total = node('td', '', number.format(Number(amount)) + ' ' + unit);
    total.append(node('small', '', cashEffect)); cells.push(total); tr.append(total); journal.append(tr);
    // На телефоне таблица разворачивается в карточки, подписи берутся отсюда.
    ['Категория', 'Тип', 'Наименование', 'Сумма'].forEach((label, index) => { cells[index].dataset.label = label; });
    if (item && item.id !== null && ['movement', 'salary_payment', 'reserve_transfer'].includes(item.operation)) {
      const actions = node('div', 'operation-actions');
      if (item.operation !== 'reserve_transfer') {
        tr.querySelectorAll('td').forEach(cell => cell.addEventListener('dblclick', () => editOperation(item, cells, actions)));
        const edit = node('button', 'operation-edit', 'Изменить'); edit.type = 'button';
        edit.addEventListener('click', () => editOperation(item, cells, actions)); actions.append(edit);
      }
      const remove = node('button', 'operation-delete', 'Удалить'); remove.type = 'button';
      remove.addEventListener('click', async () => {
        if (!confirm('Удалить эту финансовую операцию?')) return;
        try { await deleteOperation(item); } catch (error) { message(error.message, true); }
      });
      actions.append(remove);
      total.append(actions);
    }
    if (type === 'От кассира' && data.manual_handover && item) {
      const actions = node('div', 'operation-actions');
      const edit = node('button', 'operation-edit', 'Изменить'); edit.type = 'button';
      edit.addEventListener('click', () => editHandover(item, total, actions));
      const remove = node('button', 'operation-delete', '×'); remove.type = 'button'; remove.title = 'Удалить приход';
      remove.addEventListener('click', async () => { if (!confirm('Удалить приход за этот день?')) return; await mutateHandover('DELETE', item.day); });
      actions.append(edit, remove); total.append(actions);
    }
  };
  l.movements.forEach(item => {
    let info = itemInfo(item.item_code);
    if (item.type === 'opening') info = {category: 'Приходы', label: 'Остаток на начало дня'};
    if (item.type === 'auto_cashier') info = {category: 'Приходы', label: 'От кассира'};
    if (item.type === 'reserve_transfer') info = {category: 'Резервы', label: 'Отложено в сейф'};
    if (item.type === 'procurement_advance') info = {category: 'Закуп', label: 'Выдача под отчёт'};
    const opening = item.type === 'opening';
    const incoming = item.type === 'auto_cashier' || item.type === 'other_receipt';
    addRow(info.category, info.label, item.description, item.amount, 'сум', opening ? 'Перенос с прошлого дня' : incoming ? 'В кассу' : 'Из кассы', item);
    if (!incoming && !opening) breakdown[info.category] = (breakdown[info.category] || 0) + Number(item.amount);
  });
  l.debts_created_today.forEach(item => {
    const info = itemInfo(item.item_code);
    addRow(info.category, 'Начислен расход', item.description, item.total, 'сум', 'Обязательство; оплаты показаны отдельными строками');
  });
  const debtList = $('manual-debt-list'); debtList.replaceChildren();
  if (!l.manual_debts.length) debtList.append(emptyState('✓', 'Неоплаченных расходов нет.'));
  l.manual_debts.forEach(item => {
    const row=node('div','manual-debt-row');
    row.append(node('span','',item.description + ' · ' + formattedDay(item.day)), node('strong','',money(item.debt)));
    debtList.append(row);
  });
  options($('debt-select'), l.manual_debts.map(item => ({id:item.id,label:item.description + ' · осталось ' + money(item.debt)})), 'Выберите долг');
  for (const account of ['dividends', 'usd', 'shoh']) {
    const label = {dividends:'Дивиденды',usd:'Реальные USD',shoh:'Подотчёт Шоха'}[account];
    data.reserves[account].entries.filter(e => e.kind !== 'transfer' && e.id !== null).forEach(e => {
      const type = e.kind === 'opening' ? 'Начальный остаток' : e.kind === 'deposit' ? 'Поступление' : account === 'shoh' ? 'Принята закупка' : 'Выдача';
      addRow(label, type, e.note, e.amount, account === 'usd' ? 'USD' : 'сум', 'Не списывает сумовую кассу');
    });
  }
  if (!journal.children.length) {
    const tr = node('tr', 'is-empty-row'), td = node('td', 'is-empty');
    td.colSpan = 4;
    td.append(emptyState('▤', 'За этот день операций ещё нет. Записи из формы выше появятся в этом списке.'));
    tr.append(td); journal.append(tr);
  }
  const dayOut = Number(l.cash_flow.salary_paid) + Number(l.cash_flow.other_outflows);
  $('journal-sum').textContent = dayOut ? 'списано ' + money(dayOut) : 'списаний нет';
  const target = $('expense-breakdown'); target.replaceChildren();
  Object.entries(breakdown).forEach(([label, value]) => {const row=node('div','rail-line');row.append(node('span','',label),node('b','',money(value)));target.append(row);});
  if (!target.children.length) target.append(emptyState('◔', 'Расходов за день ещё нет.', 'compact'));
  updateButtons();
}

function status(text) { const box = $('connection'); if (box) box.textContent = text; }

// День, который сейчас на экране. Перезагрузку того же дня (после записи)
// делаем на месте: раньше раскладка пряталась, страница схлопывалась, и
// бухгалтер после «Записать» внизу журнала оказывался в самом верху.
let shownDay = null;

async function loadDay() {
  const day = selectedDay();
  const sequence = ++requestNo;
  current = null; updateButtons();
  if (day !== shownDay) $("finance-layout").hidden = true;
  if (!day || !$('accountant-date').checkValidity()) { $('entrances-download').disabled = true; message('Выберите сегодняшний или прошедший день.', true); return; }
  $("finance-layout").setAttribute("aria-busy", "true");
  $('entrances-day').textContent = formattedDay(day);
  const isToday = day === today, isYesterday = day === previousDay(today);
  $('accountant-today').classList.toggle('is-active', isToday);
  $('accountant-today').setAttribute('aria-pressed', String(isToday));
  $('accountant-yesterday').classList.toggle('is-active', isYesterday);
  $('accountant-yesterday').setAttribute('aria-pressed', String(isYesterday));
  $('accountant-next').disabled = day >= today;
  $('entrances-download').disabled = false;
  $('all-employees-link').href = '/accountant/employees?date=' + encodeURIComponent(day);
  $('employees-menu').href = '/accountant/employees?date=' + encodeURIComponent(day);
  try {
    const response = await fetch('/api/accountant/day?date=' + encodeURIComponent(day), {cache: 'no-store'}), data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить данные.');
    if (sequence !== requestNo) return;
    current = data;
    renderStaff(data); renderLedger(data); renderShift(data); renderShoh(data); renderSalary(data); renderChecks(data); renderDividends(data);
    // Строки смены создаются после renderLedger, поэтому состояние кнопок и
    // полей пересчитываем в конце — иначе новые поля остаются активными.
    updateButtons();
    $("finance-layout").hidden = false; shownDay = day; message('');
    status('Данные за ' + formattedDay(day));
  } catch (error) { if (sequence === requestNo) { message(error.message, true); status('Данные не загрузились'); } }
  finally { if (RetroState.shouldReleaseBusy(sequence, requestNo)) $('finance-layout').setAttribute('aria-busy', 'false'); }
}
function submit(id, endpoint, body, success) {
  const form = $(id);
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (!form.reportValidity() || saving || !current || current.date !== selectedDay()) return;
    saving = true; updateButtons();
    const button = form.querySelector('button[type="submit"]'); button.disabled = true;
    try {
      const payload = body(new FormData(form));
      const target = typeof endpoint === 'function' ? endpoint(new FormData(form)) : endpoint;
      const response = await RetroFinancialWrite(target, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось сохранить. Проверьте поля.');
      form.reset(); if (id === 'other-expense-form') categoryChanged(); await loadDay(); message(success);
      // Следующая строка: курсор в первое поле, без прыжка экрана.
      form.querySelector('select:not([disabled]), input:not([type=hidden]):not([disabled])')?.focus({preventScroll: true});
    } catch (error) { message(error.message, true); } finally { saving = false; updateButtons(); }
  });
}
const incomeCodes = ['income_cashier', 'income_other'];
submit('other-expense-form', f => special[f.get('item_code')] ? '/api/accountant/reserves' : incomeCodes.includes(f.get('item_code')) ? '/api/accountant/incomes' : '/api/accountant/expenses', f => {
  const entry = special[f.get('item_code')];
  if (incomeCodes.includes(f.get('item_code'))) return {date:selectedDay(), item_code:f.get('item_code'), note:f.get('note'), amount:f.get('amount')};
  return entry ? {date: selectedDay(), amount: f.get('amount'), note: f.get('note'), account: entry.account, kind: entry.kind}
    : {date: selectedDay(), amount: f.get('amount'), paid_amount: f.get('paid_amount') === '' ? null : f.get('paid_amount'), item_code: f.get('item_code'), note: f.get('note')};
}, 'Операция сохранена. Остатки пересчитаны.');
submit('payroll-confirm-form', '/api/accountant/payroll/confirm', f => ({date:selectedDay(),approver:f.get('approver')}), 'Начисления сохранены.');
submit('salary-payment-form', '/api/accountant/salary-payments', f => ({date:selectedDay(),accrual_id:Number(f.get('accrual_id')),amount:f.get('amount')}), 'Выплата зарплаты сохранена, долг уменьшен.');
submit('debt-payment-form', '/api/accountant/debts/pay', f => ({date:selectedDay(),debt_id:Number(f.get('debt_id')),amount:f.get('amount')}), 'Выплата по долгу сохранена.');
submit('reserve-opening-form', '/api/accountant/reserves', f => ({date:selectedDay(),account:f.get('account'),kind:'opening',amount:f.get('amount'),note:f.get('note')}), 'Начальный остаток сохранён на выбранную дату.');
submit('cash-opening-form', '/api/accountant/cash-opening', f => ({date:selectedDay(),amount:f.get('amount'),note:f.get('note')}), 'Начальный остаток бухгалтера сохранён.');
submit('handover-form', '/api/accountant/handover', f => ({date:selectedDay(),amount:f.get('amount'),note:f.get('note')}), 'Приход от кассира сохранён.');
// «Выдать всем» закрывает долги по одной выплате за раз: серверу нужен свой
// accrual_id на каждую, а частичный успех лучше полного отката вслепую.
$('pay-all').addEventListener('click', async () => {
  if (!current || saving) return;
  const owed = current.ledger.accruals.filter(row => Number(row.debt) > 0);
  if (!owed.length) return;
  if (!confirm('Выдать ' + owed.length + ' сотрудникам на ' + money(owed.reduce((s, r) => s + Number(r.debt), 0)) + '?')) return;
  saving = true; updateButtons();
  let done = 0;
  try {
    for (const row of owed) { await payAccrual(row.id, row.debt); done += 1; }
    message('Выдано ' + done + ' сотрудникам.');
  } catch (error) { message('Выдано ' + done + ' из ' + owed.length + '. ' + error.message, true); }
  finally { saving = false; await loadDay(); }
});
$('expense-category').addEventListener('change', categoryChanged);
$('dividends-fill-button').addEventListener('click', prefillDividends);
$('expense-item').addEventListener('change', expenseImpact);
$('expense-paid').addEventListener('input', updateButtons);
document.querySelectorAll('[data-open]').forEach(link => link.addEventListener('click', () => { $(link.dataset.open).open = true; }));
$('accountant-date').addEventListener('change', loadDay);
$('accountant-refresh').addEventListener('click', loadDay);
$('accountant-prev').addEventListener('click', () => { $('accountant-date').value = previousDay(selectedDay()); loadDay(); });
$('accountant-next').addEventListener('click', () => { const day = nextDay(selectedDay()); if (day <= today) { $('accountant-date').value = day; loadDay(); } });
$('accountant-today').addEventListener('click', () => { $('accountant-date').value = today; loadDay(); });
$('accountant-yesterday').addEventListener('click', () => { $('accountant-date').value = previousDay(today); loadDay(); });
$('entrances-download').addEventListener('click', async () => {
  const day = selectedDay(); if (!day) return; $('entrances-download').disabled = true;
  try {
    const response = await fetch('/api/accountant/employees/export?scope=late&date=' + encodeURIComponent(day), {cache: 'no-store'});
    if (!response.ok) throw new Error('Не удалось скачать файл входов.');
    const blob = await response.blob(), url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url; link.download = 'Retro-late-' + day + '.xlsx'; document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    message('Список опоздавших скачан.');
  } catch (error) { message(error.message, true); } finally { $('entrances-download').disabled = false; }
});
(async () => {
  try {
    const catalogRequest = fetch('/api/accountant/expenses/catalog', {cache: 'no-store'});
    catalogRequest.catch(() => {}); // The config request may fail before we await the catalog.
    today = (await globalThis.RetroConfig).today;
    const requested = new URLSearchParams(location.search).get('date');
    $('accountant-date').max = today;
    $('accountant-date').value = requested && requested <= today ? requested : previousDay(today);
    const dayRequest = loadDay();
    const catalogResponse = await catalogRequest;
    if (!catalogResponse.ok) throw new Error('Не удалось загрузить наименования затрат.');
    catalog = (await catalogResponse.json()).groups;
    catalog.push({code: 'reserves', label:'Резервы и подотчёт', items:Object.entries(special).map(([code,value])=>({code,label:value.label}))});
    catalog.forEach(group => $('expense-category').add(new Option(group.label, group.code)));
    categoryChanged();
    await dayRequest;
  } catch (error) { message(error.message, true); }
})();
