const $ = id => document.getElementById(id);
const number = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
const money = value => number.format(Number(value || 0)) + ' сум';
let today, current, requestNo = 0, catalog = [], saving = false;
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
  if (!ready) return;
  const code = $("expense-item").value, entry = special[code], income = incomeCodes.includes(code);
  const needsCash = !entry && !income || entry?.kind === "transfer";
  const unpaid = !entry && $('expense-paid').value === '0';
  $("other-expense-form").querySelector("button").disabled = needsCash && !unpaid && current.ledger.cash_balance === null;
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
function formattedDay(day) { return new Intl.DateTimeFormat('ru-RU', {day: 'numeric', month: 'long', year: 'numeric', timeZone: 'Asia/Tashkent'}).format(new Date(day + 'T12:00:00+05:00')); }
function message(value, error = false) { const n = $('accountant-message'); n.textContent = value; n.hidden = !value; n.setAttribute('role', error ? 'alert' : 'status'); }
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
      const entry = row.first_entry ? new Date(row.first_entry).toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Tashkent'}) : '—';
      detail.append(node('span', '', 'Вход: ' + entry + (row.exception ? ' · разовое разрешение' : '')), node('strong', '', row.payable === null ? 'Не рассчитано' : money(row.payable)));
      card.append(identity, node('span', 'staff-status late', 'Опоздал'), detail); container.append(card);
  });
}
function renderLedger(data) {
  const l = data.ledger, confirmed = l.payroll_confirmed, hasCashierData = l.cash_balance !== null;
  const cashierDay = previousDay(data.date);
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
  $('payroll-paid-total').textContent = money(l.salary_recorded_on_day);
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
    $(account + '-balance').textContent = value === null ? 'Не задан' : account === 'usd' ? number.format(Number(value)) + ' USD' : money(value);
  }
  $('reserves-lines').hidden = reservesKnown === 0;
  $('reserves-empty').hidden = reservesKnown > 0;
  $('monthly-balance').textContent = money(reserves.monthly.total);
  $('monthly-note').textContent = reserves.monthly.month + ' · сумма ставок сотрудников из файла «ЗП»';
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
    const category = itemInfo(item.item_code).category;
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
  const target = $('expense-breakdown'); target.replaceChildren();
  Object.entries(breakdown).forEach(([label, value]) => {const row=node('div','rail-line');row.append(node('span','',label),node('b','',money(value)));target.append(row);});
  if (!target.children.length) target.append(emptyState('◔', 'Расходов за день ещё нет.', 'compact'));
  updateButtons();
}

async function loadDay() {
  const day = selectedDay();
  if (!day || !$('accountant-date').checkValidity()) { $('entrances-download').disabled = true; message('Выберите сегодняшний или прошедший день.', true); return; }
  const sequence = ++requestNo;
  current = null; updateButtons();
  $("finance-layout").setAttribute("aria-busy", "true");
  $('entrances-day').textContent = formattedDay(day);
  const isToday = day === today, isYesterday = day === previousDay(today);
  $('accountant-today').classList.toggle('active', isToday);
  $('accountant-today').setAttribute('aria-pressed', String(isToday));
  $('accountant-yesterday').classList.toggle('active', isYesterday);
  $('accountant-yesterday').setAttribute('aria-pressed', String(isYesterday));
  $('entrances-download').disabled = false;
  $('all-employees-link').href = '/accountant/employees?date=' + encodeURIComponent(day);
  $('employees-menu').href = '/accountant/employees?date=' + encodeURIComponent(day);
  try {
    const response = await fetch('/api/accountant/day?date=' + encodeURIComponent(day), {cache: 'no-store'}), data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить данные.');
    if (sequence !== requestNo) return;
    current = data; renderStaff(data); renderLedger(data); message('');
  } catch (error) { if (sequence === requestNo) message(error.message, true); }
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
      const response = await fetch(target, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось сохранить. Проверьте поля.');
      form.reset(); if (id === 'other-expense-form') categoryChanged(); await loadDay(); message(success);
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
submit('debt-payment-form', '/api/accountant/debts/pay', f => ({date:selectedDay(),debt_id:Number(f.get('debt_id')),amount:f.get('amount')}), 'Выплата по долгу сохранена.');
submit('reserve-opening-form', '/api/accountant/reserves', f => ({date:selectedDay(),account:f.get('account'),kind:'opening',amount:f.get('amount'),note:f.get('note')}), 'Начальный остаток сохранён на выбранную дату.');
submit('cash-opening-form', '/api/accountant/cash-opening', f => ({date:selectedDay(),amount:f.get('amount'),note:f.get('note')}), 'Начальный остаток бухгалтера сохранён.');
submit('handover-form', '/api/accountant/handover', f => ({date:selectedDay(),amount:f.get('amount'),note:f.get('note')}), 'Приход от кассира сохранён.');
$('expense-category').addEventListener('change', categoryChanged);
$('expense-item').addEventListener('change', expenseImpact);
$('expense-paid').addEventListener('input', updateButtons);
document.querySelectorAll('[data-open]').forEach(link => link.addEventListener('click', () => { $(link.dataset.open).open = true; }));
$('accountant-date').addEventListener('change', loadDay);
$('accountant-refresh').addEventListener('click', loadDay);
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
    const response = await fetch('/api/config', {cache: 'no-store'});
    if (!response.ok) throw new Error('Не удалось определить текущую дату.');
    today = (await response.json()).today;
    const requested = new URLSearchParams(location.search).get('date');
    $('accountant-date').max = today;
    $('accountant-date').value = requested && requested <= today ? requested : previousDay(today);
    const catalogResponse = await fetch('/api/accountant/expenses/catalog', {cache: 'no-store'});
    if (!catalogResponse.ok) throw new Error('Не удалось загрузить наименования затрат.');
    catalog = (await catalogResponse.json()).groups;
    catalog.push({code: 'reserves', label:'Резервы и подотчёт', items:Object.entries(special).map(([code,value])=>({code,label:value.label}))});
    catalog.forEach(group => $('expense-category').add(new Option(group.label, group.code)));
    categoryChanged();
    await loadDay();
  } catch (error) { message(error.message, true); }
})();
