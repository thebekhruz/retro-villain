const $ = id => document.getElementById(id);
const number = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
const money = value => number.format(Number(value || 0)) + ' сум';
let today, current, requestNo = 0, saving = false;

function message(value, error = false) {
  const box = $('payroll-message');
  box.textContent = value; box.hidden = !value;
  box.setAttribute('role', error ? 'alert' : 'status');
}
function node(tag, cls, value) {
  const element = document.createElement(tag);
  if (cls) element.className = cls;
  if (value !== undefined) element.textContent = value;
  return element;
}
const monthLabel = month => new Intl.DateTimeFormat('ru-RU', {month: 'long', year: 'numeric', timeZone: 'UTC'})
  .format(new Date(month + '-01T12:00:00Z')).replace(/\s*г\.$/, '');
const dayNumber = day => String(Number(day.slice(8, 10)));
const weekdayShort = day => new Intl.DateTimeFormat('ru-RU', {weekday: 'short', timeZone: 'UTC'})
  .format(new Date(day + 'T12:00:00Z'));
const isWeekend = day => [0, 6].includes(new Date(day + 'T12:00:00Z').getUTCDay());

const CELL_TEXT = {paid: '✓', partial: '½', missing: 'н/я', empty: '', owed: '', late: ''};

async function payAccrual(accrualId, amount, day) {
  const response = await RetroFinancialWrite('/api/accountant/salary-payments', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    // Выдают в день смены или позже; раньше рабочего дня сервер не пропустит.
    body: JSON.stringify({date: day, accrual_id: Number(accrualId), amount: String(amount)})
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось записать выплату.');
}

function renderSheet(data) {
  const grid = $('sheet-grid'), rows = PayrollLogic.sheet(data), days = data.days;
  grid.replaceChildren();
  $('sheet-empty').hidden = rows.length > 0;
  $('sheet-scroll').hidden = rows.length === 0;
  if (!rows.length) return;
  // Ширину задаём переменной: колонок тридцать с лишним, и они должны совпадать
  // в шапке, строках и подвале.
  grid.style.setProperty('--days', String(days.length));

  const head = node('div', 'sheet-row is-head');
  head.append(node('div', 'sheet-name', 'Сотрудник'), node('div', 'sheet-rate num', 'Ставка'));
  days.forEach(day => {
    const cell = node('button', 'sheet-day' + (isWeekend(day) ? ' is-weekend' : ''));
    cell.type = 'button';
    cell.title = 'Открыть ' + day.split('-').reverse().join('.') + ' в «Финансах дня»';
    cell.append(node('strong', '', dayNumber(day)), node('span', '', weekdayShort(day)));
    cell.addEventListener('click', () => { location.href = '/accountant?date=' + encodeURIComponent(day); });
    head.append(cell);
  });
  head.append(node('div', 'sheet-total num', 'Выдано'), node('div', 'sheet-total num', 'Осталось'));
  grid.append(head);

  rows.forEach(person => {
    const line = node('div', 'sheet-row');
    const who = node('div', 'sheet-name');
    who.append(node('span', 'sheet-person', person.name), node('span', 'sheet-group', person.group || ''));
    line.append(who, node('div', 'sheet-rate num rm-num', number.format(Number(person.rate))));
    person.cells.forEach(cell => {
      const box = node('button', 'sheet-cell is-' + cell.kind, CELL_TEXT[cell.kind]);
      box.type = 'button';
      if (cell.kind === 'empty') {
        box.disabled = true;
        box.title = 'Смены не было';
      } else if (cell.kind === 'missing') {
        box.disabled = true;
        box.title = 'Не пришёл — начисление 0 сум';
      } else if (cell.debt > 0) {
        box.title = person.name + ' · ' + cell.day.split('-').reverse().join('.') +
          ' · долг ' + money(cell.debt) + '. Нажмите, чтобы выдать.';
        box.addEventListener('click', async () => {
          if (saving) return;
          // Выплата записывается на день смены, а не на сегодня: ячейка
          // принадлежит этому дню, и в его же остатке уйдут деньги.
          const shown = cell.day.split('-').reverse().join('.');
          if (!confirm('Выдать ' + person.name + ' за смену ' + shown + ' — ' + money(cell.debt) +
            '?\nВыплата будет записана на ' + shown + ' и уменьшит остаток этого дня.')) return;
          saving = true;
          try { await payAccrual(cell.accrualId, cell.debt, cell.day); await load(); message('Выдано: ' + person.name + ' · ' + money(cell.debt)); }
          catch (error) { message(error.message, true); }
          finally { saving = false; }
        });
      } else {
        box.disabled = true;
        box.title = 'Выдано ' + money(cell.paid);
      }
      line.append(box);
    });
    line.append(node('div', 'sheet-total num rm-num', number.format(Number(person.paid))),
      node('div', 'sheet-total num rm-num' + (Number(person.debt) > 0 ? ' is-owed' : ''),
        number.format(Number(person.debt))));
    grid.append(line);
  });

  const foot = node('div', 'sheet-row is-foot');
  foot.append(node('div', 'sheet-name', 'Выдано из кассы за день'), node('div', 'sheet-rate', ''));
  PayrollLogic.dayTotals(data).forEach(item => {
    foot.append(node('div', 'sheet-day-total num rm-num' + (item.amount ? '' : ' is-zero'),
      item.amount ? number.format(item.amount) : '·'));
  });
  const totals = PayrollLogic.totals(data);
  foot.append(node('div', 'sheet-total num rm-num', number.format(totals.paid)),
    node('div', 'sheet-total num rm-num', number.format(totals.debt)));
  grid.append(foot);
}

function renderTotals(data) {
  const t = PayrollLogic.totals(data);
  $('kpi-accrued').textContent = number.format(t.accrued);
  $('kpi-accrued-foot').textContent = t.people + ' сотрудников со сменами';
  $('kpi-paid').textContent = number.format(t.paid);
  $('kpi-paid-foot').textContent = t.accrued ? Math.round(t.paid / t.accrued * 100) + '% начисленного' : '';
  $('kpi-debt').textContent = number.format(t.debt);
  $('kpi-debt-foot').textContent = t.owing ? 'у ' + t.owing + ' сотрудников' : 'долгов нет';
  $('kpi-monthly').textContent = number.format(t.monthlyPaid);
  $('kpi-monthly-foot').textContent = 'фонд по реестру ' + money(t.monthlyFund);
}

function renderMonthly(data) {
  const body = $('monthly-rows');
  body.replaceChildren();
  (data.monthly || []).forEach(person => {
    const row = node('tr');
    [person.name, person.role, money(person.salary), money(person.card),
      money(person.cash), money(person.advances), money(person.remaining)]
      .forEach(value => row.append(node('td', '', value)));
    body.append(row);
  });
  if (!(data.monthly || []).length) {
    const row = node('tr'), cell = node('td', '', 'Помесячных сотрудников в реестре нет.');
    cell.colSpan = 7; row.append(cell); body.append(row);
  }
  // Предупреждение приходит от самой модели реестра — не выдаём его за сверку.
  $('monthly-note').textContent = (data.monthly || [])[0]?.warning
    || 'Оклады ведутся вручную и не разложены по дням: выплаты по ним видны в «Операциях за день».';
}

async function load() {
  const month = $('month-input').value;
  const sequence = ++requestNo;
  if (!month) { message('Выберите месяц.', true); return; }
  $('payroll-body').hidden = true;
  $('month-title').textContent = monthLabel(month);
  $('month-title').append(node('span', '', '.'));
  $('crumb-month').textContent = 'Зарплаты · ' + monthLabel(month);
  $('month-next').disabled = month >= today.slice(0, 7);
  $('day-link').href = '/accountant?date=' + encodeURIComponent(
    month === today.slice(0, 7) ? today : month + '-01');
  try {
    const response = await fetch('/api/accountant/payroll/month?month=' + encodeURIComponent(month), {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить ведомость.');
    if (sequence !== requestNo) return;
    current = data;
    renderTotals(data); renderSheet(data); renderMonthly(data);
    $('payroll-body').hidden = false;
    message('');
    $('connection').textContent = 'Ведомость за ' + monthLabel(month);
  } catch (error) {
    if (sequence === requestNo) { message(error.message, true); $('connection').textContent = 'Данные не загрузились'; }
  }
}

$('month-input').addEventListener('change', load);
$('month-prev').addEventListener('click', () => {
  $('month-input').value = PayrollLogic.shiftMonth($('month-input').value, -1); load();
});
$('month-next').addEventListener('click', () => {
  const next = PayrollLogic.shiftMonth($('month-input').value, 1);
  if (next <= today.slice(0, 7)) { $('month-input').value = next; load(); }
});
(async () => {
  try {
    today = (await globalThis.RetroConfig).today;
    const requested = new URLSearchParams(location.search).get('month');
    $('month-input').max = today.slice(0, 7);
    $('month-input').value = requested && requested <= today.slice(0, 7) ? requested : today.slice(0, 7);
    await load();
  } catch (error) { message(error.message, true); }
})();
