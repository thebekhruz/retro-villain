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
  const code = $("expense-item").value, item = special[code];
  $('expense-paid-label').hidden = !!item;
  $('expense-paid').disabled = !!item;
  $("expense-currency").textContent = item?.account === "usd" ? "USD" : "сум";
  $("expense-impact").textContent = item ? item.impact : 'Пустое поле «Оплачено сейчас» означает полную оплату. Ноль или меньшая сумма создадут долг; деньги уменьшатся только на фактически оплаченное.';
  updateButtons();
}
function updateButtons() {
  const ready = current && current.date === selectedDay() && !saving;
  document.querySelectorAll(".finance-layout form button[type=submit]").forEach(b => b.disabled = !ready);
  if (!ready) return;
  const entry = special[$("expense-item").value];
  const needsCash = !entry || entry.kind === "transfer";
  const unpaid = !entry && $('expense-paid').value === '0';
  $("other-expense-form").querySelector("button").disabled = needsCash && !unpaid && current.ledger.cash_balance === null;
  $('debt-payment-form').querySelector('button').disabled = !current.ledger.manual_debts.length || current.ledger.cash_balance === null;
  $('cash-opening-form').querySelector('button').disabled = current.ledger.cash_opening !== null || current.ledger.cash_flow.first_day !== selectedDay();
  $("monthly-plan-form").querySelector("button").disabled = current.reserves.monthly.plan !== null;
}
function categoryChanged() {
  const group = catalog.find(g => g.code === $("expense-category").value);
  options($("expense-item"), (group?.items || []).map(i => ({id:i.code,label:i.label})), "Выберите тип");
  expenseImpact();
}

function previousDay(day) { const d = new Date(day + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() - 1); return d.toISOString().slice(0, 10); }
function formattedDay(day) { return new Intl.DateTimeFormat('ru-RU', {day: 'numeric', month: 'long', year: 'numeric', timeZone: 'Asia/Tashkent'}).format(new Date(day + 'T12:00:00+05:00')); }
function message(value, error = false) { const n = $('accountant-message'); n.textContent = value; n.hidden = !value; n.setAttribute('role', error ? 'alert' : 'status'); }
function node(tag, cls, value) { const n = document.createElement(tag); if (cls) n.className = cls; if (value !== undefined) n.textContent = value; return n; }
function options(select, items, placeholder) { const old = select.value; select.replaceChildren(new Option(placeholder, '')); items.forEach(x => select.add(new Option(x.label, String(x.id)))); if (items.some(x => String(x.id) === old)) select.value = old; }
const selectedDay = () => $('accountant-date').value;

function renderStaff(data) {
  const rows = data.employees.filter(row => row.status === 'late'), container = $('late-list');
  $('staff-summary').textContent = rows.length + ' опоздали после 10:00 · без автоматического штрафа. Проходы демонстрационные.';
  container.replaceChildren();
  if (!rows.length) container.append(node('p', 'accountant-help', 'За этот день опоздавших нет.'));
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
  $('expected-cashier').textContent = l.cash_flow.missing_day
    ? (data.expected_cashier === null ? 'Нет данных кассира за этот день. ' : 'Для переноса остатка не хватает кассового отчёта за ' + formattedDay(l.cash_flow.missing_day) + '. ') + (data.cashier_error || 'Откройте эту дату и обновите её.')
    : 'Кассовый приход за ' + formattedDay(data.date) + ': ' + money(data.expected_cashier) + '. Учёт начат с ' + formattedDay(l.cash_flow.first_day);
  $('cashier-transfer-total').textContent = data.expected_cashier === null ? 'Нет данных' : money(data.expected_cashier);
  $('opening-cash-total').textContent = hasCashierData ? money(l.cash_flow.opening_balance) : 'Не рассчитано';
  $('cash-opening-note').textContent = l.cash_opening ? 'Начальный остаток: ' + money(l.cash_opening.amount) + ' на ' + formattedDay(l.cash_opening.day) + '.'
    : l.cash_flow.first_day ? 'Начальный остаток ещё не задан. Пока расчёт начинается с нуля на ' + formattedDay(l.cash_flow.first_day) + '.'
      : 'Сначала загрузите первый день кассового отчёта.';
  const unpaidDay = l.accruals.filter(r => r.work_day === data.date).reduce((sum, r) => sum + Number(r.debt), 0);
  $('payroll-draft-total').textContent = money(confirmed ? unpaidDay : data.payroll.draft_total);
  $('payroll-state').textContent = confirmed ? 'Осталось выплатить за выбранную смену' : 'Предварительно · ' + data.payroll.unknown_count + ' не рассчитаны';
  $('payroll-paid-total').textContent = money(l.salary_recorded_on_day);
  $('payroll-debt-total').textContent = money(l.salary_debt);
  $('manual-debt-total').textContent = money(l.manual_debt_total);
  $('all-debt-total').textContent = money(Number(l.salary_debt) + Number(l.manual_debt_total));
  $('finance-cash-total').textContent = hasCashierData ? money(l.cash_balance) : '—';
  $('day-outflows').textContent = money(Number(l.cash_flow.salary_paid) + Number(l.cash_flow.other_outflows));
  const reserves = data.reserves;
  for (const account of ['dividends', 'usd', 'shoh']) {
    const value = reserves[account].balance;
    $(account + '-balance').textContent = value === null ? 'Остаток не задан' : account === 'usd' ? number.format(Number(value)) + ' USD' : money(value);
  }
  $('monthly-balance').textContent = reserves.monthly.balance === null ? 'План не задан' : money(reserves.monthly.balance);
  $('monthly-note').textContent = reserves.monthly.month + ' · выплачено по месячным окладам: ' + money(reserves.monthly.paid);
  const journal = $('finance-journal'); journal.replaceChildren();
  const breakdown = {};
  const addRow = (category, type, name, amount, unit, cashEffect) => {
    const tr = node('tr');
    tr.append(node('td', '', category), node('td', '', type), node('td', '', name));
    const total = node('td', '', number.format(Number(amount)) + ' ' + unit);
    total.append(node('small', '', cashEffect)); tr.append(total); journal.append(tr);
  };
  l.movements.forEach(item => {
    let info = itemInfo(item.item_code);
    if (item.type === 'auto_cashier') info = {category: 'Приход', label: 'От кассира'};
    if (item.type === 'reserve_transfer') info = {category: 'Резервы', label: 'Отложено в сейф'};
    if (item.type === 'procurement_advance') info = {category: 'Закуп', label: 'Выдача под отчёт'};
    addRow(info.category, info.label, item.description, item.amount, 'сум', item.type === 'auto_cashier' ? 'В кассу' : 'Из кассы');
    if (item.type !== 'auto_cashier') breakdown[info.category] = (breakdown[info.category] || 0) + Number(item.amount);
  });
  l.debts_created_today.forEach(item => {
    const info = itemInfo(item.item_code);
    addRow(info.category, 'Начислен расход', item.description, item.total, 'сум', 'Обязательство; оплаты показаны отдельными строками');
  });
  const debtList = $('manual-debt-list'); debtList.replaceChildren();
  if (!l.manual_debts.length) debtList.append(node('p', 'accountant-help', 'Неоплаченных расходов нет.'));
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
  if (!journal.children.length) { const tr=node('tr'),td=node('td','','Пока нет операций за этот день.');td.colSpan=4;tr.append(td);journal.append(tr); }
  const target = $('expense-breakdown'); target.replaceChildren();
  Object.entries(breakdown).forEach(([label, value]) => {const row=node('div','rail-line');row.append(node('span','',label),node('b','',money(value)));target.append(row);});
  if (!target.children.length) target.append(node('p','','Расходов за день ещё нет.'));
  updateButtons();
}

function renderScenarios(data) {
  const shortfall = Number(data.scenarios.shortfall), target = $('scenario-groups'); target.replaceChildren();
  $('scenario-shortfall').textContent = data.ledger.cash_balance === null ? 'Нет данных кассира: пока нельзя оценить, хватит ли денег на зарплату.'
    : shortfall > 0 ? 'На ' + (data.ledger.payroll_confirmed ? 'погашение долга' : 'предварительную зарплату') + ' не хватает ' + money(shortfall) + '.'
      : 'Денег с учётом переходящего остатка хватает на рассчитанную зарплату. Решение о сменах остаётся за финансовым отделом.';
  $('scenario-shortfall').classList.toggle('is-short', shortfall > 0);
  data.scenarios.groups.forEach(item => { const row = node('div', 'scenario-row'); row.append(node('span', '', item.group), node('strong', '', money(item.saving))); if (item.covers_shortfall) row.append(node('small', '', 'Покрыла бы недостачу будущей смены')); target.append(row); });
}
async function loadDay() {
  const day = selectedDay();
  if (!day || !$('accountant-date').checkValidity()) { $('entrances-download').disabled = true; message('Выберите сегодняшний или прошедший день.', true); return; }
  const sequence = ++requestNo;
  current = null; updateButtons();
  $("finance-layout").setAttribute("aria-busy", "true");
  $('entrances-day').textContent = formattedDay(day);
  $('accountant-today').classList.toggle('active', day === today);
  $('accountant-yesterday').classList.toggle('active', day === previousDay(today));
  $('entrances-download').disabled = false;
  $('all-employees-link').href = '/accountant/employees?date=' + encodeURIComponent(day);
  try {
    const response = await fetch('/api/accountant/day?date=' + encodeURIComponent(day), {cache: 'no-store'}), data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить данные.');
    if (sequence !== requestNo) return;
    current = data; renderStaff(data); renderLedger(data); renderScenarios(data); message('');
    $('finance-layout').setAttribute('aria-busy', 'false');
  } catch (error) { if (sequence === requestNo) message(error.message, true); }
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
submit('other-expense-form', f => special[f.get('item_code')] ? '/api/accountant/reserves' : '/api/accountant/expenses', f => {
  const entry = special[f.get('item_code')];
  return entry ? {date: selectedDay(), amount: f.get('amount'), note: f.get('note'), account: entry.account, kind: entry.kind}
    : {date: selectedDay(), amount: f.get('amount'), paid_amount: f.get('paid_amount') === '' ? null : f.get('paid_amount'), item_code: f.get('item_code'), note: f.get('note')};
}, 'Операция сохранена. Остатки пересчитаны.');
submit('debt-payment-form', '/api/accountant/debts/pay', f => ({date:selectedDay(),debt_id:Number(f.get('debt_id')),amount:f.get('amount')}), 'Выплата по долгу сохранена.');
submit('reserve-opening-form', '/api/accountant/reserves', f => ({date:selectedDay(),account:f.get('account'),kind:'opening',amount:f.get('amount'),note:f.get('note')}), 'Начальный остаток сохранён на выбранную дату.');
submit('cash-opening-form', '/api/accountant/cash-opening', f => ({date:selectedDay(),amount:f.get('amount'),note:f.get('note')}), 'Начальный остаток бухгалтера сохранён.');
submit('monthly-plan-form', '/api/accountant/monthly-plan', f => ({date:selectedDay(),amount:f.get('amount'),note:f.get('note')}), 'План наличных выплат за месяц сохранён.');
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
    link.href = url; link.download = 'Retro-late-' + day + '-DEMO.xlsx'; document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    message('Список опоздавших скачан. Проходы демонстрационные.');
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
