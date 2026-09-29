/* Ведомость месяца (2b): одна сетка «сотрудник × день» для помесячных и
   сменных, итоги по дням из кассы и проверки месяца. Считает payroll-logic.js,
   здесь только отрисовка и запись выплат. */
const $ = id => document.getElementById(id);
const number = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
const fmt = value => number.format(Number(value || 0));
const money = value => fmt(value) + ' сум';
const L = globalThis.PayrollLogic;
let today, current, pending = null, requestNo = 0, saving = false;
let shownMonth = null, focus = null, checksOpen = false;
// Отклик (busy.js): ячейка, которую нажали, крутится сама; ведомость при
// перечитывании гаснет, а не пропадает. Ключи — с месяцем: в другом месяце
// те же id означают другие ячейки.
const B = globalThis.RetroBusy;
const cellKey = (kind, personId, day) => kind + ':' + ($('month-input').value || '') + ':' + personId + ':' + day;

function message(value, error = false) {
  const box = $('payroll-message');
  box.textContent = value; box.hidden = !value || !error;
  box.setAttribute('role', error ? 'alert' : 'status');
  if (value) globalThis.RetroToast?.show(value, error ? 'error' : 'ok');
}
function node(tag, cls, value) {
  const element = document.createElement(tag);
  if (cls) element.className = cls;
  if (value !== undefined && value !== null) element.textContent = value;
  return element;
}
const monthDate = month => new Date(month + '-01T12:00:00Z');
const monthName = month => new Intl.DateTimeFormat('ru-RU', {month: 'long', timeZone: 'UTC'}).format(monthDate(month));
const monthYear = month => {
  const name = monthName(month);
  return name[0].toUpperCase() + name.slice(1) + ' ' + month.slice(0, 4);
};
const dayNumber = day => String(Number(day.slice(8, 10)));
const WEEKDAYS = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
const weekday = day => WEEKDAYS[new Date(day + 'T12:00:00Z').getUTCDay()];
const dm = day => day.slice(8, 10) + '.' + day.slice(5, 7);
// Сумма ячейки: только цифры (null — не сумма, её не записываем).
const parse = value => L.parseAmount(value);
const STATUS = {on_time: 'вовремя', late: 'опоздал', missing: 'не пришёл', manual_present: 'был · вручную',
  manual_absent: 'не был · вручную', unlinked: 'нет привязки', unavailable: 'нет данных'};

async function postJson(url, body, idempotent) {
  const options = {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
  const response = idempotent ? await RetroFinancialWrite(url, options) : await fetch(url, options);
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось записать выплату.');
  return result;
}

/* ── Выплаты прямо из ячеек ─────────────────────────────────────────── */
async function payShift(person, cell, box) {
  if (saving) return;
  saving = true;
  B?.clear('', 'error');
  // Деньги уходят из кассы сегодня — выплата сегодняшней датой (для вчерашней
  // смены это и есть «смена + 1»). Задним числом — в «Финансах дня».
  const date = L.payday(cell.day, today);
  try {
    const write = postJson('/api/accountant/salary-payments',
      {date, accrual_id: Number(cell.accrualId), amount: String(cell.debt)}, true);
    B?.button(box, write);
    await write;
    await load();
    message('Выдано: ' + person.name + ' · ' + money(cell.debt) + ' · смена ' + dm(cell.day));
  } catch (error) { message(error.message, true); }
  finally { saving = false; }
}

/* Неподтверждённая вчерашняя смена: как в «Финансах дня», первая выдача
   подтверждает смену выбранного сотрудника, потом выдаёт начисление.
   Сервер отказал (нет ставки, неполный Hikvision) — показываем его причину,
   ячейка остаётся «к выдаче». */
async function payPending(person, cell, box) {
  if (saving) return;
  saving = true;
  B?.clear('', 'error');
  const date = L.payday(cell.day, today);
  // Подтверждение, перечитывание и выдача — одно действие для человека:
  // ячейка крутится, пока не закончится всё.
  let release = null;
  if (B) B.button(box, new Promise((resolve, reject) => { release = {resolve, reject}; })).catch(() => {});
  try {
    let approver = 'бухгалтер';
    try { const config = await globalThis.RetroConfig; if (config?.user) approver = config.user; } catch { /* по умолчанию */ }
    // Начисляем только этого человека: препятствия у других его не держат.
    const result = await postJson('/api/accountant/payroll/confirm', {date: cell.day, approver, employee_ids: [Number(person.id)]}, true);
    const blocked = (result.blockers || []).find(item => Number(item.employee_id) === Number(person.id));
    if (blocked) throw new Error(blockerMessage(person.name, cell.day, blocked.code || blocked.reason));
    await load();
    const fresh = (current.shift || []).find(row => row.employee_id === person.id)?.cells?.[cell.day];
    if (!fresh || !fresh.accrual_id) throw new Error('Смена ' + dm(cell.day) + ': начисления для «' + person.name + '» нет.');
    const debt = Number(fresh.debt || 0);
    if (debt > 0) await postJson('/api/accountant/salary-payments', {date, accrual_id: Number(fresh.accrual_id), amount: String(debt)}, true);
    await load();
    release?.resolve(true);
    message('Начислено и выдано: ' + person.name + ' · ' + money(debt) + ' · смена ' + dm(cell.day));
  } catch (error) {
    release?.reject(error);
    // Сначала перерисовка (она гасит старые сообщения), потом причина отказа.
    await load().catch(() => {});
    message(error.message, true);
  } finally { saving = false; }
}

/* Почему человеку не начислить смену — и что сделать. */
const BLOCKER_FIX = {
  missing_rate: 'нет ставки — укажите её в «Сотрудниках»',
  unlinked: 'нет привязки Hikvision — отметьте «был» вручную в «Финансах дня»',
  unavailable: 'данные Hikvision за день неполные — дождитесь синхронизации',
  unknown: 'начисление не посчитать — проверьте в «Финансах дня»'};
function blockerMessage(name, day, code) {
  return 'Не начислено ' + dm(day) + ': ' + name + ' — ' + (BLOCKER_FIX[code] || (typeof code === 'string' && code) || BLOCKER_FIX.unknown);
}

/* Отмена выдачи за смену: нажатие на «✓» (или «!») удаляет выплаты по
   начислению после подтверждения. Начисление остаётся — ячейка снова
   «к выдаче» / «✕», деньги возвращаются в остаток кассы дня выдачи. */
async function cancelShift(person, cell, box) {
  if (saving) return;
  const total = cell.payments.reduce((sum, item) => sum + item.amount, 0);
  const days = [...new Set(cell.payments.map(item => dm(item.day)))].join(', ');
  if (!await confirmAt(box, 'Отменить выдачу за смену ' + dm(cell.day) + ': ' + person.name + ' · ' + money(total) +
    ' (выдано ' + days + ')? Деньги вернутся в остаток кассы.', {keep: 'Оставить', yes: 'Отменить выдачу'})) return;
  if (saving) return;
  saving = true;
  B?.clear('', 'error');
  const work = (async () => {
    for (const item of cell.payments) {
      await send('/api/accountant/operations/salary_payment/' + encodeURIComponent(item.id) +
        '?date=' + encodeURIComponent(item.day), 'DELETE');
    }
  })();
  if (B) Promise.resolve(B.button(box, work)).catch(() => {});
  try {
    await work;
    await load();
    message('Выдача отменена: ' + person.name + ' · ' + money(total) + ' · смена ' + dm(cell.day));
  } catch (error) {
    await load().catch(() => {});
    message(error.message, true);
  } finally { saving = false; }
}

async function send(url, method, body) {
  const response = await fetch(url, {method, headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
  if (!response.ok) {
    let detail = method === 'DELETE' ? 'Не удалось удалить выплату.' : 'Не удалось изменить выплату.';
    try { const result = await response.json(); if (typeof result.detail === 'string') detail = result.detail; } catch { /* без тела */ }
    throw new Error(detail);
  }
}

/* Подтверждение удаления — строкой у ячейки, как «Удалить сотрудника?» в
   макете, а не системным окном. */
let popover = null;
function closePopover(answer = false) { if (popover) popover.done(answer); }
function confirmAt(anchor, question, labels = {}) {
  closePopover(false);
  return new Promise(resolve => {
    const box = node('div', 'pr-confirm');
    box.setAttribute('role', 'alertdialog');
    const actions = node('div', 'pr-confirm-actions');
    const keep = node('button', 'pr-confirm-keep', labels.keep || 'Оставить');
    const yes = node('button', 'pr-confirm-yes', labels.yes || 'Удалить');
    keep.type = yes.type = 'button';
    actions.append(keep, yes);
    box.append(node('span', 'pr-confirm-q', question), actions);
    document.body.append(box);
    const place = () => {
      const r = anchor.getBoundingClientRect(), w = box.offsetWidth, h = box.offsetHeight;
      const top = r.bottom + h + 8 > innerHeight ? r.top - h - 6 : r.bottom + 6;
      box.style.top = Math.max(8, top) + 'px';
      box.style.left = Math.min(Math.max(8, r.right - w), innerWidth - w - 8) + 'px';
    };
    const onKey = event => { if (event.key === 'Escape') done(false); };
    function done(answer) {
      box.remove(); popover = null;
      window.removeEventListener('scroll', place, true); window.removeEventListener('resize', place);
      document.removeEventListener('keydown', onKey);
      resolve(answer);
    }
    popover = {done};
    place();
    window.addEventListener('scroll', place, true); window.addEventListener('resize', place);
    document.addEventListener('keydown', onKey);
    keep.addEventListener('click', () => done(false));
    yes.addEventListener('click', () => done(true));
    // Фокус — после текущего нажатия: иначе Enter, которым закончили ввод в
    // ячейке, тут же «нажимал» бы «Оставить».
    setTimeout(() => { if (box.isConnected) keep.focus({preventScroll: true}); }, 0);
  });
}

/* Ячейка оклада — итог выплат человеку за день. Новое число: больше —
   доплата разницы; меньше — уменьшаем последние выплаты дня; пусто или 0 —
   удаляем выплаты дня (после подтверждения). */
async function editMonthly(person, cell, input) {
  const raw = input.value.trim();
  const next = parse(raw);
  // Вернуть сохранённое и снять пометку «набрано, не записано»: иначе при
  // следующей перерисовке отменённое число вернулось бы в ячейку.
  const reset = () => {
    input.value = cell.amount ? fmt(cell.amount) : '';
    if (input.dataset.busyKey) B?.clear(input.dataset.busyKey);
  };
  // «1,5 млн», «abc», «-5» — не сумма: не записываем и не удаляем, а объясняем.
  if (next === null) {
    reset(); message('Оклад за ' + dm(cell.day) + ': «' + raw + '» — не сумма. Введите цифрами, например 500 000.', true); return;
  }
  if (next === cell.amount) { reset(); return; }
  const plan = L.monthlyEditPlan(cell.ops || [], cell.amount, next);
  if (!plan || (next < cell.amount && !cell.ops)) {
    reset(); message('Выплаты за ' + dm(cell.day) + ' изменились — обновите страницу и повторите.', true); return;
  }
  if (next === 0 && !await confirmAt(input, 'Удалить выплату оклада за ' + dm(cell.day) + ': ' + person.name + ' · ' + money(cell.amount) + '?')) {
    reset(); return;
  }
  // Больше оклада — только после явного «Всё равно записать» (переплата
  // потом видна красным и в проверках).
  const over = L.overpayAfter(person, cell, next);
  if (over > 0 && !await confirmAt(input, 'Больше оклада: ' + person.name + ' получит ' + money(person.paid - cell.amount + next) +
    ' из ' + money(person.salary) + ', переплата ' + money(over) + '.', {keep: 'Отмена', yes: 'Всё равно записать'})) {
    reset(); return;
  }
  if (saving) { reset(); return; }
  saving = true;
  B?.clear('', 'error');
  const line = input.closest('.pr-row');
  const work = (async () => {
    for (const step of plan) {
      if (step.action === 'add') {
        await postJson('/api/accountant/monthly-payments', {date: cell.day, employee_id: Number(person.id), amount: String(step.amount)}, true);
      } else if (step.action === 'update') {
        await send('/api/accountant/operations/movement/' + encodeURIComponent(step.id), 'PUT',
          {date: cell.day, item_code: 'salary_monthly', note: '', amount: String(step.amount)});
      } else {
        await send('/api/accountant/operations/movement/' + encodeURIComponent(step.id) + '?date=' + encodeURIComponent(cell.day), 'DELETE');
      }
    }
  })();
  // «Сохраняю…» в самой ячейке, затем галочка и вспышка строки; при отказе
  // ячейка красная, а набранное число остаётся — можно поправить и повторить.
  if (B) B.field(input, work, {row: line}).catch(() => {});
  try {
    await work;
    await load();
    if (next > cell.amount) message('Оклад за ' + dm(cell.day) + ': ' + person.name + ' · ' + (cell.amount
      ? '+' + fmt(next - cell.amount) + ', всего ' + money(next) : money(next)));
    else if (next === 0) message('Выплата оклада за ' + dm(cell.day) + ' удалена: ' + person.name);
    else message('Оклад за ' + dm(cell.day) + ' изменён: ' + person.name + ' · ' + fmt(cell.amount) + ' → ' + money(next));
  } catch (error) {
    await load().catch(() => {});
    if (!B) reset();
    message(error.message, true);
  } finally { saving = false; }
}

/* ── Сетка ──────────────────────────────────────────────────────────── */
function hikChip() {
  const chip = node('span', 'pr-hik', '⊘ Hik');
  chip.title = 'Не зарегистрирован в Hikvision';
  return chip;
}
function whoCell(name, role, noHik) {
  const who = node('div', 'pr-c pr-c-name');
  const sub = node('span', 'pr-role', role);
  if (noHik) sub.append(' ', hikChip());
  who.append(node('span', 'pr-name', name), sub);
  return who;
}
const isFocused = (row, day) => !!focus && focus.row === row && (day === undefined || focus.day === day);

function headRow(days) {
  const head = node('div', 'pr-row is-head');
  head.append(node('div', 'pr-c pr-c-name', 'Сотрудник'), node('div', 'pr-c pr-c-sum', 'Сумма'));
  const yesterday = L.addDays(today, -1);
  days.forEach(day => {
    const future = day > today;
    const cell = node(future ? 'div' : 'button', 'pr-day' + (day === today ? ' is-today' : '') +
      (day === yesterday ? ' is-shift' : '') + (future ? ' is-future' : ''));
    cell.dataset.day = day;
    cell.append(node('strong', '', dayNumber(day)), node('span', '', day === today ? 'сегодня' : weekday(day)));
    if (!future) {
      cell.type = 'button';
      const payday = L.shiftScreenDay(day, today);
      cell.title = 'Смена ' + dm(day) + ' · выдача ' + dm(payday) + ' — открыть в «Финансах дня»';
      cell.addEventListener('click', () => { location.href = '/accountant?date=' + encodeURIComponent(payday); });
    }
    head.append(cell);
  });
  head.append(node('div', 'pr-c pr-c-paid', 'Выдано'), node('div', 'pr-c pr-c-rest', 'Осталось'));
  return head;
}
function sectionRow(text) {
  const row = node('div', 'pr-section');
  row.append(node('strong', '', text));
  return row;
}
function monthlyRow(person) {
  const key = 'm' + person.id;
  const line = node('div', 'pr-row is-monthly' + (isFocused(key) ? ' is-focus' : ''));
  line.dataset.row = key;
  line.dataset.busyKey = cellKey('row', key, '');
  const sum = node('div', 'pr-c pr-c-sum rm-num', fmt(person.salary));
  if (person.gone) { sum.textContent = '—'; sum.title = 'Сотрудника нет в реестре — выплаты за месяц сохранены'; }
  line.append(whoCell(person.name, person.role, person.noHik), sum);
  person.cells.forEach(cell => {
    const cls = 'pr-m' + (cell.over ? ' is-over' : cell.amount ? ' is-filled' : '') +
      (cell.future ? ' is-future' : '') + (cell.today ? ' is-today' : '');
    // Будущий день закрыт; у удалённого из реестра — только чтение.
    if (cell.future || person.gone) { line.append(node('div', cls + (person.gone ? ' is-gone' : ''), cell.amount ? fmt(cell.amount) : '')); return; }
    const input = node('input', cls);
    input.dataset.busyKey = cellKey('pm', person.id, cell.day);
    input.value = cell.amount ? fmt(cell.amount) : '';
    input.inputMode = 'numeric';
    input.autocomplete = 'off';
    input.setAttribute('aria-label', person.name + ' · оклад за ' + dm(cell.day));
    input.title = person.name + ' · ' + dm(cell.day) + (cell.amount ? ' · выдано ' + money(cell.amount) + ' · изменить или очистить' : ' · выдать часть оклада');
    // Показанное (сохранённое или набранное, но не записанное) — цифрами без пробелов.
    input.addEventListener('focus', () => { const shown = parse(input.value); if (shown !== null) input.value = shown ? String(shown) : ''; input.select(); });
    input.addEventListener('blur', () => { if (parse(input.value) === cell.amount) input.value = cell.amount ? fmt(cell.amount) : ''; });
    input.addEventListener('keydown', event => { if (event.key === 'Enter') input.blur(); if (event.key === 'Escape') { input.value = String(cell.amount || ''); input.blur(); } });
    input.addEventListener('change', () => editMonthly(person, cell, input));
    line.append(input);
  });
  const paid = node('div', 'pr-c pr-c-paid rm-num' + (person.over ? ' is-over' : ''), fmt(person.paid));
  const rest = node('div', 'pr-c pr-c-rest rm-num' + (person.over ? ' is-over' : person.closed ? ' is-closed' : ''));
  rest.append(node('span', '', person.gone ? '—' : person.over ? '−' + fmt(-person.rest) : fmt(person.rest)));
  if (person.over || person.closed) rest.append(node('small', '', person.over ? 'переплата' : 'закрыт'));
  line.append(paid, rest);
  return line;
}
function shiftCellTitle(person, cell) {
  const raw = person.source[cell.day] || pending?.[cell.day]?.[person.id] || null;
  const status = raw ? STATUS[raw.status] || raw.status : '';
  const parts = [person.name, dm(cell.day)];
  if (status) parts.push(status);
  if (cell.paid) parts.push('выдано ' + money(cell.paid));
  if (cell.kind === 'blocked') parts.push('не начислено: ' + (BLOCKER_FIX[cell.blocker] || BLOCKER_FIX.unknown));
  else if (cell.kind === 'pending') parts.push('к выдаче ' + money(cell.debt) + ' — нажмите: смена подтвердится и выдача запишется сегодня, ' + dm(L.payday(cell.day, today)));
  else if (cell.payable) parts.push('к выдаче ' + money(cell.debt) + ' — нажмите: выдача запишется сегодня, ' + dm(L.payday(cell.day, today)));
  else if (cell.cancellable) parts.push('нажмите, чтобы отменить выдачу');
  if (cell.rate != null && (cell.kind === 'odd' || cell.kind === 'nopass')) parts.push('ставка дня ' + money(cell.rate));
  return parts.join(' · ');
}
function shiftRow(person) {
  const key = 's' + person.id;
  const line = node('div', 'pr-row is-shift' + (isFocused(key) ? ' is-focus' : ''));
  line.dataset.row = key;
  line.dataset.busyKey = cellKey('row', key, '');
  line.append(whoCell(person.name, personRole(person), person.noHik || !!noHik[person.id]),
    person.rate == null ? node('div', 'pr-c pr-c-sum is-norate', 'нет ставки') : node('div', 'pr-c pr-c-sum rm-num', fmt(person.rate)));
  person.cells.forEach(cell => {
    const interactive = cell.payable || cell.cancellable || cell.kind === 'pending' || cell.kind === 'blocked';
    const box = node(interactive ? 'button' : 'div', 'pr-s is-' + cell.kind + (cell.late ? ' is-late' : '') +
      (cell.day === today ? ' is-today' : '') + (isFocused(key, cell.day) ? ' is-focus' : ''), cell.text);
    if (cell.kind !== 'future' && cell.kind !== 'empty') box.title = shiftCellTitle(person, cell);
    box.dataset.busyKey = cellKey('ps', person.id, cell.day);
    if (interactive) {
      box.type = 'button';
      box.addEventListener('click', () => (cell.kind === 'pending' ? payPending(person, cell, box)
        : cell.kind === 'blocked' ? message(blockerMessage(person.name, cell.day, cell.blocker), true)
        : cell.payable ? payShift(person, cell, box) : cancelShift(person, cell, box)));
    }
    line.append(box);
  });
  line.append(node('div', 'pr-c pr-c-paid rm-num', fmt(person.paid)),
    node('div', 'pr-c pr-c-rest rm-num' + (person.rest > 0 ? ' is-owed' : ' is-closed'), fmt(person.rest)));
  return line;
}
// Должность сменного приходит только в /staff; в ведомости есть группа.
function personRole(person) { return person.role || person.group; }

function renderSheet(data) {
  const grid = $('sheet-grid'), days = data.days || [];
  const monthly = L.monthlyRows(data, {today});
  const shift = L.shiftRows(data, {today, pending}).map(row => {
    const source = (data.shift || []).find(p => p.employee_id === row.id);
    return {...row, source: source?.cells || {}, role: roles[row.id] || ''};
  });
  // Ввод продолжается после записи: Tab из сохранённой ячейки уводит в
  // соседнюю, а перерисовка не должна выбивать из неё фокус и набранное.
  const active = document.activeElement;
  const kept = active && grid.contains(active) && active.dataset.busyKey
    ? {key: active.dataset.busyKey, value: active.value, input: active.tagName === 'INPUT'} : null;
  grid.replaceChildren();
  const empty = !monthly.length && !shift.length;
  $('sheet-empty').hidden = !empty;
  $('sheet-scroll').hidden = empty;
  if (empty) return;
  grid.style.setProperty('--days', String(days.length));
  grid.append(headRow(days));
  if (monthly.length) {
    grid.append(sectionRow('Помесячные · оклад выдаётся частями'));
    monthly.forEach(person => grid.append(monthlyRow(person)));
  }
  // Пометка «ждёт начисления» в легенде — только когда такие ячейки есть.
  $('legend-blocked').hidden = !shift.some(row => row.cells.some(cell => cell.kind === 'blocked'));
  if (shift.length) {
    grid.append(sectionRow('Сменные · колонка = день смены, выдача на следующий день'));
    shift.forEach(person => grid.append(shiftRow(person)));
  } else if (monthly.length) grid.append(sectionRow('Сменные · за этот месяц смен в ведомости нет'));
  const foot = node('div', 'pr-row is-foot');
  // Подпись занимает колонку имени, а колонка суммы пустая: так на телефоне,
  // где закреплено только имя, подпись строки остаётся на месте при прокрутке.
  foot.append(node('div', 'pr-c pr-c-label', 'Выдано из кассы за день'), node('div', 'pr-c pr-c-sum pr-c-sumfoot'));
  let grand = 0;
  L.dayTotals(data).forEach(item => {
    grand += item.amount;
    foot.append(node('div', 'pr-t rm-num' + (item.day === today ? ' is-today' : ''), item.amount ? fmt(item.amount) : ''));
  });
  foot.append(node('div', 'pr-c pr-c-grand rm-num', money(grand)));
  grid.append(foot);
  if (kept) {
    const again = [...grid.querySelectorAll('[data-busy-key]')].find(el => el.dataset.busyKey === kept.key);
    if (again) {
      again.focus({preventScroll: true});
      if (kept.input && again.tagName === 'INPUT') again.value = kept.value;
    }
  }
}
let roles = {}, noHik = {};

function renderTotals(data, checks) {
  const t = L.totals(data);
  $('kpi-fund').textContent = fmt(t.monthlyFund);
  $('kpi-mpaid').textContent = fmt(t.monthlyPaid);
  $('kpi-mrest').textContent = fmt(t.monthlyRest);
  // Переплата не прячется в «осталось»: отдельной красной строкой.
  $('kpi-mover').hidden = !t.monthlyOver;
  $('kpi-mover').textContent = t.monthlyOver ? 'переплата −' + fmt(t.monthlyOver) : '';
  $('kpi-spaid').textContent = fmt(t.paid);
  $('kpi-issues').textContent = String(checks.filter(item => item.lvl !== 'todo').length);
}

function renderChecks(checks) {
  const box = $('checks-list'), limit = 9;
  box.replaceChildren();
  if (!checks.length) {
    box.append(node('p', 'pr-checks-empty', 'Замечаний нет: переплат, выдач без входа и невыданных смен в этом месяце не нашли.'));
  }
  (checksOpen ? checks : checks.slice(0, limit)).forEach(item => {
    const row = node('button', 'pr-check');
    row.type = 'button';
    row.append(node('i', 'pr-dot is-' + item.lvl));
    const texts = node('span', 'pr-check-text');
    texts.append(node('strong', '', item.text), node('small', '', item.sub));
    row.append(texts);
    row.addEventListener('click', () => focusOn(item));
    box.append(row);
  });
  const more = $('checks-more');
  more.hidden = checks.length <= limit;
  more.textContent = checksOpen ? 'Свернуть' : 'Показать все · ' + checks.length;
}

function focusOn(item) {
  focus = {row: item.row, day: item.day};
  renderSheet(current);
  // Проверка дня без строки (остаток в минусе) — подсвечиваем колонку дня.
  if (!item.row && item.day) {
    const head = document.querySelector('.pr-row.is-head .pr-day[data-day="' + item.day + '"]');
    head?.classList.add('is-focus');
    $('sheet-scroll').scrollIntoView({block: 'start', behavior: 'smooth'});
    scrollToDay(item.day, true);
    return;
  }
  const line = document.querySelector('.pr-row[data-row="' + item.row + '"]');
  if (!line) return;
  line.scrollIntoView({block: 'center', behavior: 'smooth'});
  if (item.day) scrollToDay(item.day, true);
}

/* Горизонтальная прокрутка — только внутри карточки сетки. */
function stickyRight() {
  const rest = document.querySelector('.pr-row.is-head .pr-c-rest');
  const paid = document.querySelector('.pr-row.is-head .pr-c-paid');
  if (!rest || getComputedStyle(rest).position !== 'sticky') return 0;
  return rest.offsetWidth + paid.offsetWidth;
}
function stickyLeft() {
  const name = document.querySelector('.pr-row.is-head .pr-c-name');
  const sum = document.querySelector('.pr-row.is-head .pr-c-sum');
  if (!name) return 0;
  return name.offsetWidth + (getComputedStyle(sum).position === 'sticky' ? sum.offsetWidth : 0);
}
function scrollToDay(day, center) {
  const scroller = $('sheet-scroll');
  const head = scroller.querySelector('.pr-day[data-day="' + day + '"]');
  if (!head) return;
  const left = stickyLeft(), right = stickyRight();
  const visible = scroller.clientWidth - left - right;
  const target = center
    ? head.offsetLeft - left - (visible - head.offsetWidth) / 2
    : head.offsetLeft + head.offsetWidth - left - visible;
  // Выравниваем по границе колонки: крайний день не прячется наполовину
  // под закреплённым именем.
  const first = scroller.querySelector('.pr-row.is-head .pr-day');
  const width = head.offsetWidth || 1, base = first ? first.offsetLeft - left : 0;
  const index = Math.round((head.offsetLeft - (first ? first.offsetLeft : 0)) / width);
  // Как в макете: на широком экране первой видна неделя назад (D−6), и
  // сегодняшний день стоит последним перед «Выдано».
  if (!center && visible >= width * 6.5) { scroller.scrollLeft = Math.max(0, base + (index - 6) * width); return; }
  const steps = Math.max(0, Math.ceil((target - base) / width - 0.001));
  scroller.scrollLeft = Math.max(0, base + steps * width);
}

/* Открытые дни — вчерашний неподтверждённый и начисленные частично: по ним
   нужны строки /staff (проход, ставка, blocker), чтобы показать, кто «к выдаче»,
   а кто ждёт начисления. Вчерашний /staff заодно даёт должности. */
async function loadPending(data) {
  const yesterday = L.addDays(today, -1);
  const closed = new Set(data.confirmed_days || []);
  const days = new Set((data.partial_days || []).filter(day => day <= today));
  if ((data.days || []).includes(yesterday) && !closed.has(yesterday)) days.add(yesterday);
  const staff = async day => {
    const response = await fetch('/api/accountant/staff?date=' + encodeURIComponent(day), {cache: 'no-store'});
    return response.ok ? response.json() : null;
  };
  const next = {};
  try {
    const wanted = [...new Set([yesterday, ...days])];
    const answers = await Promise.all(wanted.map(day => staff(day).catch(() => null)));
    roles = {}; noHik = {};
    answers.forEach((answer, index) => {
      if (!answer) return;
      const day = wanted[index];
      (answer.employees || []).forEach(row => {
        if (!roles[row.employee_id]) roles[row.employee_id] = row.role;
        // «⊘ Hik» — отсутствие регистрации, включая ещё не начисленные строки.
        if (row.hikvision_registered === false || row.status === 'unlinked' || row.manual_attendance) noHik[row.employee_id] = true;
      });
      if (days.has(day)) next[day] = Object.fromEntries((answer.employees || []).map(row => [row.employee_id, row]));
    });
  } catch { /* без открытых дней сетка всё равно рисуется */ }
  pending = next;
}

/* Перечитывание: ведомость на экране гаснет, пока идут новые данные;
   в самый первый раз вместо неё стоит скелет. */
function load() {
  const body = $('payroll-body');
  return B && !body.hidden ? B.section(body, loadMonth()) : loadMonth();
}
async function loadMonth() {
  let month = $('month-input').value;
  const sequence = ++requestNo;
  // Пустой, кривой или будущий месяц из календаря не открываем: остаёмся на
  // показанном (иначе заголовок сменился бы, а сетка осталась старой).
  const latest = today.slice(0, 7);
  if (!/^\d{4}-\d{2}$/.test(month) || month > latest) {
    const back = shownMonth || latest;
    message(!month ? 'Выберите месяц.' : 'Будущий месяц ещё не открыт — показываем ' + monthYear(back).toLowerCase() + '.', true);
    $('month-input').value = back;
    if (shownMonth) return;
    month = back;
  }
  const fresh = month !== shownMonth;
  if (fresh) { focus = null; checksOpen = false; }
  const name = monthName(month);
  $('month-title').replaceChildren('Зарплаты · ' + name, node('span', '', '.'));
  $('crumb-month').textContent = 'Зарплаты · ' + name;
  $('month-label').textContent = monthYear(month);
  $('month-next').disabled = month >= today.slice(0, 7);
  try {
    const response = await fetch('/api/accountant/payroll/month?month=' + encodeURIComponent(month), {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить ведомость.');
    await loadPending(data);
    if (sequence !== requestNo) return;
    current = data;
    const keep = $('sheet-scroll').scrollLeft;
    const checks = L.checks(data, {today, pending});
    renderTotals(data, checks);
    renderSheet(data);
    renderChecks(checks);
    $('payroll-body').hidden = false;
    $('payroll-skeleton').hidden = true;
    if (fresh) {
      const days = data.days || [];
      scrollToDay(days.includes(today) ? today : days[days.length - 1], false);
    } else $('sheet-scroll').scrollLeft = keep;
    shownMonth = month;
    // Месяц — в адресе: F5 и ссылка открывают тот же месяц.
    try {
      const url = new URL(location.href);
      if (month === latest) url.searchParams.delete('month'); else url.searchParams.set('month', month);
      history.replaceState(null, '', url);
    } catch { /* без адреса — не страшно */ }
    $('payroll-message').hidden = true;
    $('connection').textContent = 'Ведомость за ' + monthYear(month).toLowerCase();
  } catch (error) {
    if (sequence === requestNo) {
      message(error.message, true); $('connection').textContent = 'Данные не загрузились';
      if ($('payroll-body').hidden) $('payroll-skeleton').hidden = true;
    }
  }
}

$('month-input').addEventListener('change', load);
$('month-prev').addEventListener('click', () => {
  $('month-input').value = L.shiftMonth($('month-input').value, -1); load();
});
$('month-next').addEventListener('click', () => {
  const next = L.shiftMonth($('month-input').value, 1);
  if (next <= today.slice(0, 7)) { $('month-input').value = next; load(); }
});
$('checks-more').addEventListener('click', () => {
  checksOpen = !checksOpen;
  renderChecks(L.checks(current, {today, pending}));
});
$('kpi-issues-card').addEventListener('click', () => $('checks-title').scrollIntoView({block: 'start', behavior: 'smooth'}));
$('payroll-download').addEventListener('click', () => {
  const button = $('payroll-download');
  if (!$('month-input').value) return;
  // Кнопка в работе, пока файл не начал скачиваться.
  const work = download();
  if (B) B.button(button, work); else { button.disabled = true; work.finally(() => { button.disabled = false; }); }
});
async function download() {
  const month = $('month-input').value;
  try {
    const response = await fetch('/api/accountant/payroll/month/export?month=' + encodeURIComponent(month), {cache: 'no-store'});
    if (!response.ok) throw new Error('Не удалось скачать ведомость.');
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url; link.download = 'Retro-payroll-' + month + '.xlsx';
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    message('Ведомость скачана.');
    return true;
  } catch (error) { message(error.message, true); return false; }
}
(async () => {
  try {
    today = (await globalThis.RetroConfig).today;
    const requested = new URLSearchParams(location.search).get('month');
    $('month-input').max = today.slice(0, 7);
    $('month-input').value = requested && /^\d{4}-\d{2}$/.test(requested) && requested <= today.slice(0, 7) ? requested : today.slice(0, 7);
    await load();
  } catch (error) { message(error.message, true); }
})();
