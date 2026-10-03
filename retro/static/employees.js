/* «Сотрудники» (1a): карточки-фильтры сверху, один реестр по группам,
   правка в боковой панели. Сотрудники на окладе — вторым разделом того же
   реестра, правятся в той же панели. */
const $ = id => document.getElementById(id);
const L = globalThis.EmployeesLogic;
const money = value => new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2}).format(Number(value || 0));
const sum = value => money(value) + ' сум';
const statuses = {on_time: 'Вовремя', late: 'Опоздал', missing: 'Не пришёл', unlinked: 'Нет привязки', unavailable: 'Нет данных',
  manual_present: 'Был · вручную', manual_absent: 'Не был · вручную'};
// «На месте» — пришёл по турникету или отмечен вручную; «не пришёл» — так же.
const PRESENT = new Set(['on_time', 'late', 'manual_present']);
const ABSENT = new Set(['missing', 'manual_absent']);
const NO_SOURCE = new Set(['unlinked', 'unavailable']);
let today, current, monthPaid = {}, requestNo = 0;
// UI-состояние живёт отдельно от данных API: карточки-фильтры, вкладка группы, поиск и открытый drawer.
// sel: id сменного | 'new' | 'm:<id>' сотрудника на окладе | 'm:new'.
const ui = {filter: 'all', tab: 'all', q: '', sel: null, draft: null, confirm: false, feedback: '', fbErr: false,
  history: null};
const SALARY_TAB = '__salary';

const formattedDay = day => new Intl.DateTimeFormat('ru-RU', {weekday: 'short', day: 'numeric', month: 'long',
  timeZone: 'Asia/Tashkent'}).format(new Date(day + 'T12:00:00+05:00'));
const arrivalText = value => value ? new Intl.DateTimeFormat('ru-RU', {hour: '2-digit', minute: '2-digit',
  hour12: false, timeZone: 'Asia/Tashkent'}).format(new Date(value)) : '—';
// Минуты от полуночи по Ташкенту — нужны, чтобы показать «+N мин» после 10:00.
function arrivalMinutes(value) {
  if (!value) return null;
  const parts = new Intl.DateTimeFormat('en-GB', {hour: '2-digit', minute: '2-digit', hour12: false,
    timeZone: 'Asia/Tashkent'}).formatToParts(new Date(value));
  const hour = Number(parts.find(p => p.type === 'hour').value);
  const minute = Number(parts.find(p => p.type === 'minute').value);
  return hour * 60 + minute;
}
const initials = name => name.trim().split(/\s+/).map(word => word[0] || '').slice(0, 2).join('').toUpperCase();
function plural(count, forms) {
  const tail = Math.abs(count) % 100, unit = tail % 10;
  if (tail > 10 && tail < 20) return forms[2];
  if (unit > 1 && unit < 5) return forms[1];
  return unit === 1 ? forms[0] : forms[2];
}
function text(tag, className, value) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (value != null) item.textContent = value;
  return item;
}
function el(tag, className, attrs) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  Object.assign(item, attrs || {});
  return item;
}
function message(value, error = false) {
  const item = $('employees-message');
  item.textContent = value;
  // Успех — только тостом, ошибка — ещё и строкой над реестром.
  item.hidden = !value || !error;
  item.setAttribute('role', error ? 'alert' : 'status');
  if (value) globalThis.RetroToast?.show(value, error ? 'error' : 'ok');
}
function status(value) { const box = $('connection'); if (box) box.textContent = value; }
// Состояние турникета — строкой в шапке, а не баннером над страницей.
function attendanceHealth(value) {
  const states = {
    ok: 'Hikvision синхронизирован',
    starting: 'Hikvision подключается — отсутствие входа пока не считается прогулом',
    stale: 'Данные Hikvision устарели — отсутствие входа не считается прогулом',
    not_configured: 'Проходы Hikvision смоделированы'
  };
  return states[value?.status || 'starting'] || 'Hikvision недоступен — отсутствие входа не считается прогулом';
}
const pill = value => text('span', 'emp-pill st-' + value, statuses[value] || value);

// Начисление — готовое число сервера (payable): там же учтены исключения и
// уже начисленные суммы. Формулу во frontend не дублируем (спека §1).
// null — не начисляется (нет ставки, привязки или данных), 0 — не пришёл.
function payFor(row) {
  if (row.payable == null || row.payable === '') return null;
  const pay = Number(row.payable);
  return Number.isFinite(pay) ? pay : null;
}
// rate из API приходит строкой ('180000') либо null — приводим к числу для расчётов и сумм.
function people() {
  return (current?.employees || []).map(row => ({
    ...row,
    rate: row.rate == null || row.rate === '' ? null : Number(row.rate),
    min: arrivalMinutes(row.first_entry),
    pay: payFor(row)
  }));
}
function salaried() {
  return (current?.monthly_employees || []).slice().sort((a, b) => Number(a.id) - Number(b.id)).map(row => {
    const salary = Number(row.salary || 0), paid = Number(monthPaid[String(row.id)] || 0);
    return {...row, salary, paid, rest: salary - paid};
  });
}
function groupOrder() {
  const known = (current?.groups || []).map(item => item.name);
  const seen = new Set(known);
  people().forEach(row => { if (row.group && !seen.has(row.group)) { seen.add(row.group); known.push(row.group); } });
  return known;
}
const editable = () => current && current.date === today;
const needsAttention = p => p.rate == null || NO_SOURCE.has(p.status);

function loadDay() { return fetchDay(); }
// Отклик (busy.js): при перечитывании реестр и карточки гаснут, а не пропадают.
const B = globalThis.RetroBusy;
function reloadDay() {
  const work = fetchDay();
  if (!B) return work;
  B.section($('employees-stats'), work);
  return B.section(document.querySelector('.emp-layout'), work);
}

// ── Отрисовка ────────────────────────────────────────────────────────────────
function render() {
  const all = people();
  const present = all.filter(p => PRESENT.has(p.status));
  const late = all.filter(p => p.status === 'late');
  const missing = all.filter(p => ABSENT.has(p.status));
  const noRate = all.filter(p => p.rate == null);
  const unlinked = all.filter(p => NO_SOURCE.has(p.status));
  const attention = all.filter(needsAttention);
  const accrued = all.reduce((total, p) => total + (p.pay || 0), 0);

  const health = current.attendance?.status;
  $('employees-demo').hidden = health !== 'not_configured';
  renderStats({accrued, present, late, missing, attention, noRate, unlinked, total: all.length});
  renderTabs(all);
  renderRegistry(all);
  renderMonthly(salaried());
  renderAside(all, {attention});
}

function statCard({key, label, accent, value, unit, total, footer, progress, dot, filterable}) {
  const active = filterable && ui.filter === key;
  const tag = filterable ? 'button' : 'div';
  const card = el(tag, 'emp-stat' + (accent ? ' emp-stat--' + accent : '') + (active ? ' is-active' : ''),
    filterable ? {type: 'button'} : {});
  if (filterable) {
    card.dataset.filter = key;
    card.setAttribute('aria-pressed', String(active));
    card.addEventListener('click', () => { ui.filter = ui.filter === key ? 'all' : key; render(); });
  }
  const head = text('span', 'emp-stat-label', label);
  if (dot) head.append(el('i', 'emp-stat-dot is-' + dot));
  card.append(head);
  const figure = text('div', 'emp-stat-value', null);
  figure.append(text('strong', null, String(value)));
  if (total != null) figure.append(text('small', null, '/ ' + total));
  if (unit) figure.append(text('small', null, unit));
  card.append(figure);
  if (progress != null) {
    const track = text('div', 'emp-stat-bar', null);
    track.append(el('span', null, {style: 'width:' + progress + '%'}));
    card.append(track);
  } else if (footer != null) {
    card.append(text('div', 'emp-stat-foot', footer));
  }
  return card;
}
// «Нет привязки» и «Нет данных» — разные причины: считаем их порознь.
function attentionFooter(s) {
  const unlinked = s.unlinked.filter(p => p.status === 'unlinked').length;
  const unavailable = s.unlinked.length - unlinked;
  return 'Без ставки ' + s.noRate.length + ' · без привязки ' + unlinked + (unavailable ? ' · нет данных ' + unavailable : '');
}
function renderStats(s) {
  const box = $('employees-stats');
  const pct = Math.round(s.present.length / (s.total || 1) * 100);
  box.replaceChildren(
    statCard({label: 'К начислению за день', accent: 'accrued', value: money(s.accrued), unit: 'сум',
      // Реестр ниже считает и окладников («Реестр 77 сотрудников»): здесь
      // подписываем, кто есть кто, чтобы 60 и 77 не спорили.
      footer: s.total + ' ' + plural(s.total, ['сменный', 'сменных', 'сменных'])
        + ((current?.monthly_employees || []).length ? ' · ' + current.monthly_employees.length + ' на окладе' : '')}),
    statCard({key: 'present', filterable: true, label: 'На месте', value: s.present.length, total: s.total,
      progress: pct}),
    statCard({key: 'late', filterable: true, accent: 'late', label: 'Опоздали', value: s.late.length, dot: 'late',
      footer: 'Вход после 10:00'}),
    statCard({key: 'missing', filterable: true, label: 'Не пришли', value: s.missing.length, dot: 'idle',
      footer: 'Начисление 0 сум'}),
    statCard({key: 'attention', filterable: true, accent: 'attention', label: 'Требуют внимания', value: s.attention.length,
      dot: 'gold', footer: attentionFooter(s)})
  );
}
function renderTabs(all) {
  const box = $('employees-tabs');
  const monthly = current?.monthly_employees || [];
  const tabs = [{key: 'all', label: 'Все', count: all.length + monthly.length}];
  groupOrder().forEach(name => {
    const count = all.filter(p => p.group === name).length;
    if (count) tabs.push({key: name, label: name, count});
  });
  if (monthly.length) tabs.push({key: SALARY_TAB, label: 'На окладе', count: monthly.length});
  box.replaceChildren(...tabs.map(tab => {
    const button = el('button', 'emp-tab' + (ui.tab === tab.key ? ' is-active' : ''), {type: 'button', role: 'tab'});
    button.setAttribute('aria-selected', String(ui.tab === tab.key));
    button.append(text('span', null, tab.label), text('small', null, String(tab.count)));
    button.addEventListener('click', () => { ui.tab = tab.key; render(); });
    return button;
  }));
}
// Без регистра, «ё» = «е», латиницей тоже: «Shoxrux» находит «Шохрух».
const queryMatches = (name, role) => L.matchesQuery(ui.q, name, role);
function matches(row) {
  if (ui.tab !== 'all' && row.group !== ui.tab) return false;
  if (!queryMatches(row.name, row.role)) return false;
  if (ui.filter === 'present') return PRESENT.has(row.status);
  if (ui.filter === 'late') return row.status === 'late';
  if (ui.filter === 'missing') return ABSENT.has(row.status);
  if (ui.filter === 'attention') return needsAttention(row);
  return true;
}
function whoCell(name, role, warn, noHik) {
  const who = text('div', 'emp-cell-who', null);
  who.append(text('span', 'emp-avatar' + (warn ? ' is-warn' : ''), initials(name)));
  const idBox = text('div', 'emp-who-text', null);
  const nameLine = text('div', 'emp-who-name', null);
  nameLine.append(text('span', null, name));
  // «⊘ Hik» — не проходит турникет, это нормально (отмечают вручную);
  // «Нет привязки» — должен проходить, но ID не привязан: это другая беда.
  if (noHik) {
    const tag = text('span', 'emp-nohik', '⊘ Hik');
    tag.title = 'Нет в Hikvision · отмечается вручную';
    nameLine.append(tag);
  }
  idBox.append(nameLine);
  if (role != null) idBox.append(text('div', 'emp-who-role', role));
  who.append(idBox);
  return who;
}
function registryRow(row) {
  const line = el('div', 'emp-row' + (ui.sel === row.employee_id ? ' is-selected' : ''), {tabIndex: 0});
  line.dataset.busyKey = 'emp:' + row.employee_id;
  line.setAttribute('role', 'button');
  line.setAttribute('aria-label', row.name + ' — изменить');
  line.addEventListener('click', () => openDrawer(row.employee_id));
  line.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openDrawer(row.employee_id); } });
  // Первый вход
  const entry = text('div', 'emp-cell-entry', null);
  const time = text('div', 'emp-entry-time', arrivalText(row.first_entry));
  if (row.status === 'late') time.classList.add('is-late');
  else if (row.min == null) time.classList.add('is-none');
  entry.append(time);
  if (row.status === 'late' && row.min != null) entry.append(text('div', 'emp-entry-late', '+' + (row.min - 600) + ' мин'));
  // Статус
  const statusCell = text('div', 'emp-cell-status', null);
  statusCell.append(pill(row.status));
  // Ставка
  const rate = text('div', 'emp-cell-num emp-cell-rate' + (row.rate == null ? ' is-missing' : ''),
    row.rate == null ? 'Нет ставки' : money(row.rate));
  // Начислено
  const pay = text('div', 'emp-cell-num emp-cell-pay' + (row.pay ? '' : ' is-none'),
    row.pay == null ? '—' : money(row.pay));
  // Время и статус — в общей обёртке: на компьютере она прозрачна для сетки
  // (display:contents), на телефоне собирает их в одну строку под именем.
  const meta = text('div', 'emp-cell-meta', null);
  meta.append(entry, statusCell);
  if (row.rate != null) rate.prepend(text('span', 'emp-m-label', 'ставка '));
  line.append(whoCell(row.name, row.role, false, row.manual_attendance), meta, rate, pay);
  return line;
}
function renderRegistry(all) {
  const container = $('employees-groups');
  container.replaceChildren();
  const salaryOnly = ui.tab === SALARY_TAB;
  const shown = salaryOnly ? [] : all.filter(matches);
  const salaryShown = salaryRowsShown();
  const count = shown.length + salaryShown.length;
  $('employees-shown').textContent = count + ' ' + plural(count, ['сотрудник', 'сотрудника', 'сотрудников']);
  const filterNames = {present: 'На месте', late: 'Опоздали', missing: 'Не пришли', attention: 'Требуют внимания'};
  const clear = $('employees-clear-filter');
  if (ui.filter !== 'all') {
    clear.hidden = false;
    clear.textContent = filterNames[ui.filter] || '';
    clear.append(text('span', 'emp-filter-x', '×'));
  } else clear.hidden = true;
  $('employees-groups').previousElementSibling.hidden = salaryOnly;

  if (!count) {
    const empty = text('div', 'empty-state', null);
    empty.append(text('p', null, 'Никого не нашли под текущими фильтрами.'));
    const reset = text('button', 'text-link', 'Сбросить фильтры');
    reset.type = 'button';
    reset.addEventListener('click', () => { ui.filter = 'all'; ui.tab = 'all'; ui.q = ''; $('employees-search').value = ''; render(); });
    empty.append(reset);
    container.append(empty);
    return;
  }
  groupOrder().forEach(name => {
    const rows = shown.filter(p => p.group === name).sort((a, b) => a.name.localeCompare(b.name, 'ru'));
    if (!rows.length) return;
    const head = text('div', 'emp-group-head', null);
    const groupTotal = rows.reduce((total, p) => total + (p.pay || 0), 0);
    const left = text('div', 'emp-group-name', null);
    left.append(text('strong', null, name), text('span', null, rows.length + ' чел.'));
    head.append(left, text('span', 'emp-group-total', sum(groupTotal)));
    container.append(head);
    rows.forEach(row => container.append(registryRow(row)));
  });
}

// ── Сотрудники на окладе — раздел того же реестра ─────────────────────────
function salaryRowsShown() {
  // Карточки дня (на месте / опоздали…) — про проходы, у окладов их нет.
  if (ui.filter !== 'all') return [];
  if (ui.tab !== 'all' && ui.tab !== SALARY_TAB) return [];
  return salaried().filter(row => queryMatches(row.name, row.role));
}
function renderMonthly() {
  const container = $('monthly-employees');
  const all = salaried(), rows = salaryRowsShown();
  const section = $('monthly-section');
  container.replaceChildren();
  const filtered = ui.filter !== 'all' || (ui.tab !== 'all' && ui.tab !== SALARY_TAB) || !!ui.q.trim();
  section.hidden = !rows.length && filtered;
  $('monthly-count').textContent = all.length + ' чел.';
  $('monthly-total').textContent = 'фонд ' + sum(all.reduce((total, row) => total + row.salary, 0));
  const month = current?.date ? new Intl.DateTimeFormat('ru-RU', {month: 'long', timeZone: 'Asia/Tashkent'})
    .format(new Date(current.date + 'T12:00:00+05:00')) : '';
  $('monthly-paid-head').textContent = month ? 'Выдано за ' + month : 'Выдано в месяце';
  if (!all.length) { container.append(text('p', 'emp-salary-empty', 'Сотрудников на окладе пока нет — добавьте кнопкой «На оклад».')); return; }
  rows.forEach(row => {
    const key = 'm:' + row.id;
    const line = el('div', 'emp-salary-row' + (ui.sel === key ? ' is-selected' : ''), {tabIndex: 0});
    line.dataset.busyKey = 'emp:' + key;
    line.setAttribute('role', 'button');
    line.setAttribute('aria-label', row.name + ' — изменить');
    line.addEventListener('click', () => openMonthly(row.id));
    line.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openMonthly(row.id); } });
    const rest = text('div', 'emp-cell-num emp-salary-rest' + (row.rest < 0 ? ' is-over' : row.rest === 0 ? ' is-closed' : ''), null);
    rest.append(text('span', null, row.rest < 0 ? '−' + money(-row.rest) : money(row.rest)));
    if (row.rest < 0) rest.append(text('small', null, 'переплата'));
    const salary = text('div', 'emp-cell-num emp-salary-salary', null);
    salary.append(text('span', 'emp-m-label', 'оклад '), money(row.salary));
    const paid = text('div', 'emp-cell-num emp-cell-pay emp-salary-paid' + (row.paid ? '' : ' is-none'), null);
    paid.append(text('span', 'emp-m-label', 'выдано '), money(row.paid));
    rest.prepend(text('span', 'emp-m-label', 'осталось'));
    line.append(whoCell(row.name, null, false, row.no_hikvision), text('div', 'emp-salary-role', row.role || '—'), salary, paid, rest);
    container.append(line);
  });
}

// ── Боковая панель (drawer) ──────────────────────────────────────────────────
function openDrawer(id) {
  const row = people().find(p => p.employee_id === id);
  if (!row) return;
  ui.sel = id;
  ui.confirm = false; ui.feedback = ''; ui.fbErr = false;
  ui.draft = shiftDraft(row);
  loadHistory('shift', id);
  render(); revealDrawer();
}
const shiftDraft = row => ({name: row.name, role: row.role, rate: L.formatAmount(row.rate), group: row.group, reason: '',
  manual: !!row.manual_attendance, hikId: row.hikvision_id || '', groupTouched: false});
const monthlyDraft = row => ({name: row.name, role: row.role, salary: L.formatAmount(row.salary), schedule: row.schedule || '',
  noHik: !!row.no_hikvision, reason: ''});
function openMonthly(id) {
  const row = salaried().find(p => p.id === id);
  if (!row) return;
  ui.sel = 'm:' + id; ui.confirm = false; ui.feedback = ''; ui.fbErr = false;
  ui.draft = monthlyDraft(row);
  loadHistory('monthly', id);
  render(); revealDrawer();
}
function openNew(kind = 'shift', keep = {}) {
  const groups = groupOrder();
  ui.confirm = false; ui.feedback = ''; ui.fbErr = false; ui.history = null;
  // Переключение «За смену / Оклад» не стирает уже набранные имя и должность.
  const name = keep.name || '', role = keep.role || '';
  if (kind === 'salary') { ui.sel = 'm:new'; ui.draft = {name, role, salary: '', schedule: '', noHik: false, reason: ''}; }
  else {
    ui.sel = 'new';
    ui.draft = {name, role, rate: '', group: L.groupForRole(role) || groups[0] || '', reason: '', manual: false, groupTouched: false};
  }
  render(); revealDrawer();
  const nameInput = document.querySelector('.emp-drawer input[name=name]');
  if (nameInput) nameInput.focus({preventScroll: true});
}
// На телефоне и планшете панель — лист поверх реестра (см. employees.css):
// место в списке не теряется, лист прокручиваем к началу.
function revealDrawer() {
  const aside = $('employees-aside');
  if (aside && overlayMode()) aside.scrollTop = 0;
}
const overlayMode = () => matchMedia('(max-width:1180px)').matches;
function closeDrawer() {
  // Набранное в карточке не теряем молча (ТЗ 02.10, п. 6).
  const question = 'Есть несохранённые изменения. Закрыть без сохранения?';
  const asked = document.documentElement.lang === 'uz' && globalThis.RetroI18n ? RetroI18n.translate(question) || question : question;
  if (drawerDirty() && !confirm(asked)) return;
  ui.sel = null; ui.draft = null; ui.confirm = false; ui.feedback = ''; ui.history = null; render();
}
/* Есть ли в открытой карточке несохранённое: новое — любое набранное поле,
   правка — отличие от того, что сейчас в реестре. */
function drawerDirty() {
  if (ui.sel == null || !ui.draft) return false;
  const d = ui.draft, monthly = String(ui.sel).startsWith('m:');
  if (ui.sel === 'new' || ui.sel === 'm:new') return ['name', 'role', 'rate', 'salary'].some(key => String(d[key] || '').trim());
  const base = monthly ? salaried().find(p => 'm:' + p.id === ui.sel) : people().find(p => p.employee_id === ui.sel);
  if (!base) return false;
  const origin = monthly ? monthlyDraft(base) : shiftDraft(base);
  const keys = monthly ? ['name', 'role', 'salary', 'schedule', 'noHik', 'reason'] : ['name', 'role', 'rate', 'group', 'reason', 'manual', 'hikId'];
  return keys.some(key => String(d[key] ?? '') !== String(origin[key] ?? ''));
}
// «Сохранить» в шапке страницы сохраняет открытую карточку.
globalThis.RetroSave?.register($('employees-aside'), () => (String(ui.sel).startsWith('m:') ? saveMonthly() : saveDraft()),
  {dirty: drawerDirty});
function setDraft(key, value) { ui.draft[key] = value; ui.feedback = ''; }

function fieldLabel(labelText, input) {
  const label = text('label', 'emp-field', null);
  label.append(text('span', null, labelText), input);
  return label;
}
function renderAside(all, ctx) {
  const box = $('employees-aside');
  box.replaceChildren();
  document.body.classList.toggle('emp-drawer-open', ui.sel != null);
  if (ui.sel == null) { box.append(attentionCard(ctx.attention), groupSummaryCard(all)); return; }
  box.append(String(ui.sel).startsWith('m:') ? monthlyDrawer() : drawerCard());
}
function attentionCard(attention) {
  const card = text('section', 'emp-side-card', null);
  card.append(text('p', 'emp-side-eyebrow is-gold', 'ТРЕБУЮТ ВНИМАНИЯ'), text('h2', 'emp-side-title', 'Не начисляется'));
  if (!attention.length) { card.append(text('p', 'emp-side-empty', 'Все привязаны и со ставкой.')); return card; }
  attention.forEach(row => {
    const item = el('button', 'emp-side-row', {type: 'button'});
    item.addEventListener('click', () => openDrawer(row.employee_id));
    item.append(text('span', 'emp-avatar is-warn is-sm', initials(row.name)));
    const info = text('span', 'emp-side-info', null);
    // Все причины сразу: «Нет ставки · нет привязки Hikvision».
    info.append(text('span', 'emp-side-name', row.name), text('span', 'emp-side-reason', L.attentionReasons(row).join(' · ')));
    item.append(info, text('span', 'emp-side-chevron', '›'));
    card.append(item);
  });
  return card;
}
function groupSummaryCard(all) {
  const card = text('section', 'emp-side-card', null);
  card.append(text('p', 'emp-side-eyebrow', 'ПО ГРУППАМ · НА МЕСТЕ'));
  groupOrder().forEach(name => {
    const rows = all.filter(p => p.group === name);
    if (!rows.length) return;
    const present = rows.filter(p => PRESENT.has(p.status)).length;
    const totalPay = rows.reduce((total, p) => total + (p.pay || 0), 0);
    const line = text('div', 'emp-summary-row', null);
    line.append(text('span', 'emp-summary-name', name),
      text('span', 'emp-summary-presence', present + '/' + rows.length),
      text('strong', 'emp-summary-total', money(totalPay)));
    card.append(line);
  });
  return card;
}
function drawerHead(eyebrow) {
  const head = text('div', 'emp-drawer-head', null);
  head.append(text('p', 'emp-side-eyebrow', eyebrow));
  const close = el('button', 'emp-drawer-close', {type: 'button', textContent: '×', title: 'Закрыть'});
  close.setAttribute('aria-label', 'Закрыть');
  close.addEventListener('click', closeDrawer);
  head.append(close);
  return head;
}
// Новый сотрудник: сразу выбрать, как ему платят — за смену или окладом.
function kindSwitch(kind) {
  const box = text('div', 'emp-kind', null);
  box.setAttribute('role', 'group');
  box.setAttribute('aria-label', 'Как платим');
  [['shift', 'За смену'], ['salary', 'Оклад в месяц']].forEach(([key, label]) => {
    const button = el('button', 'emp-kind-btn' + (kind === key ? ' is-active' : ''), {type: 'button', textContent: label});
    button.setAttribute('aria-pressed', String(kind === key));
    button.addEventListener('click', () => { if (kind !== key) openNew(key, {name: ui.draft?.name, role: ui.draft?.role}); });
    box.append(button);
  });
  return box;
}
function drawerActions(saveLabel, onSave, disabled) {
  const actions = text('div', 'emp-drawer-actions', null);
  const save = el('button', 'emp-btn emp-btn--primary', {type: 'button', textContent: saveLabel});
  save.disabled = !!disabled;
  // Панель перерисовывается целиком: ключ переносит спиннер и ✓ на новую кнопку.
  save.dataset.busyKey = 'emp-save';
  save.addEventListener('click', () => { const work = onSave(); if (B) B.button(save, work); });
  const cancel = el('button', 'emp-btn', {type: 'button', textContent: 'Отмена'});
  cancel.addEventListener('click', closeDrawer);
  actions.append(save, cancel);
  return actions;
}
function feedbackLine() {
  const feedback = text('p', 'emp-drawer-feedback' + (ui.fbErr ? ' is-error' : ''), ui.feedback);
  return feedback;
}
function drawerCard() {
  const isEdit = ui.sel !== 'new';
  const selP = isEdit ? people().find(p => p.employee_id === ui.sel) : null;
  const d = ui.draft;
  const card = text('section', 'emp-side-card emp-drawer', null);
  card.append(drawerHead(isEdit ? 'СОТРУДНИК' : 'НОВЫЙ СОТРУДНИК'));
  if (!isEdit) card.append(kindSwitch('shift'));

  if (selP) {
    const idRow = text('div', 'emp-drawer-id', null);
    idRow.append(text('span', 'emp-avatar is-lg', initials(selP.name)));
    const box = text('div', null, null);
    box.append(text('div', 'emp-drawer-name', selP.name), text('div', 'emp-drawer-sub', selP.role + ' · ' + selP.group));
    idRow.append(box);
    card.append(idRow);

    const facts = text('div', 'emp-drawer-facts', null);
    const top = text('div', 'emp-drawer-facts-top', null);
    top.append(pill(selP.status),
      text('span', 'emp-drawer-entry', selP.min == null ? 'Входа нет' : 'Первый вход ' + arrivalText(selP.first_entry)));
    facts.append(top);
    const payRow = text('div', 'emp-drawer-pay', null);
    payRow.append(text('span', null, 'К начислению'),
      text('strong', selP.pay ? '' : 'is-none', selP.pay == null ? '—' : sum(selP.pay)));
    facts.append(payRow);
    const notes = {on_time: 'Пришёл до 10:00 — ставка начисляется полностью.', late: 'Опоздание не уменьшает ставку.',
      missing: 'Входа нет — начисление 0 сум.',
      unlinked: 'Нет привязки Hikvision — начисление заблокировано. Если турникет ему не нужен, включите «Нет в Hikvision».',
      unavailable: 'Нет данных источника — начисление заблокировано.',
      manual_present: 'Нет в Hikvision — отмечен «был» вручную, ставка начисляется.',
      manual_absent: 'Нет в Hikvision — отмечен «не был» вручную, начисление 0 сум.'};
    facts.append(text('p', 'emp-drawer-note', selP.rate == null ? 'Ставка не указана — не начисляется.' : notes[selP.status] || ''));
    card.append(facts);
  }

  const form = text('div', 'emp-drawer-form', null);
  const nameInput = el('input', null, {name: 'name', value: d.name, placeholder: 'Фамилия Имя', maxLength: 160});
  nameInput.addEventListener('input', event => setDraft('name', event.target.value));
  form.append(fieldLabel('Имя', nameInput));

  const roleInput = el('input', null, {name: 'role', value: d.role, placeholder: 'Должность', maxLength: 80,
    autocomplete: 'off'});
  roleInput.setAttribute('list', 'emp-roles');
  const groupSelect = el('select', null, {name: 'group'});
  roleInput.addEventListener('input', event => {
    setDraft('role', event.target.value);
    // Группа определяется по должности, пока её не выбрали руками.
    // Незнакомая должность («сомелье») — группу выбирают из списка.
    const guess = L.groupForRole(event.target.value);
    if (!d.groupTouched && guess && guess !== d.group) { d.group = guess; groupSelect.value = guess; }
  });
  form.append(fieldLabel('Должность', roleInput));
  const datalist = el('datalist', null, {id: 'emp-roles'});
  [...new Set(people().map(p => p.role).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'ru'))
    .forEach(role => datalist.append(new Option(role, role)));
  form.append(datalist);

  groupOrder().forEach(name => groupSelect.add(new Option(name, name, false, name === d.group)));
  if (d.group && !groupOrder().includes(d.group)) groupSelect.add(new Option(d.group, d.group, false, true));
  groupSelect.addEventListener('change', event => { setDraft('group', event.target.value); d.groupTouched = true; });
  form.append(fieldLabel('Группа', groupSelect));

  form.append(fieldLabel('Ставка за смену, сум', moneyInput('rate', d.rate, 'Не указана')));

  form.append(manualToggle('Нет в Hikvision · отмечать вручную', d.manual, value => setDraft('manual', value),
    d.manual ? 'Турникет не нужен: по умолчанию «был», в «Финансах дня» можно отметить «не был».'
      : selP?.hikvision_registered ? 'Выключено: день берётся из Hikvision.'
        : isEdit ? 'Выключено: должен проходить турникет, а ID Hikvision не привязан — укажите его ниже.'
          : 'Выключено: должен проходить турникет, а ID Hikvision не привязан — начисление заблокировано.', !editable()));
  // Сняли «Нет в Hikvision» — привязать человека к устройству можно прямо
  // здесь: номер сотрудника на устройстве (employeeNo), без синхронизации.
  if (isEdit && !d.manual) {
    const hikInput = el('input', null, {name: 'hikvision_id', value: d.hikId, placeholder: 'Номер на устройстве, например 1024',
      maxLength: 32, autocomplete: 'off', inputMode: 'text', spellcheck: false});
    hikInput.disabled = !editable();
    hikInput.addEventListener('input', event => setDraft('hikId', event.target.value));
    form.append(fieldLabel('ID в Hikvision', hikInput));
  }

  if (isEdit) {
    const reasonInput = el('input', null, {name: 'reason', value: d.reason,
      placeholder: 'Например, новая ставка с сентября', maxLength: 200});
    reasonInput.addEventListener('input', event => setDraft('reason', event.target.value));
    form.append(fieldLabel('Причина изменения', reasonInput));
  }
  form.append(feedbackLine(), drawerActions('Сохранить', () => saveDraft(), !editable()));
  card.append(form);

  if (!editable()) card.append(text('p', 'emp-drawer-note', 'Изменения доступны только на сегодняшнюю дату.'));

  if (isEdit && editable()) card.append(deleteBlock(doDelete));
  if (isEdit) card.append(historyBlock());
  return card;
}
// Сумма в поле — текстом: «180 000» и «180 000,50» принимаются как есть
// (поле number на iPhone такое молча обнуляло), при уходе с поля — разряды.
function moneyInput(name, value, placeholder) {
  const input = el('input', 'emp-money', {name, value, placeholder, inputMode: 'numeric', autocomplete: 'off'});
  input.addEventListener('input', event => setDraft(name, event.target.value));
  input.addEventListener('change', () => {
    const parsed = L.parseAmount(input.value);
    // Панель могли закрыть раньше, чем поле потеряло фокус.
    if (parsed.value && ui.draft && input.isConnected) { input.value = L.formatAmount(parsed.value); ui.draft[name] = input.value; }
  });
  return input;
}
// Переключатель-галочка: состояние видно сразу, запись — кнопкой «Сохранить».
function manualToggle(label, on, onChange, hint, disabled) {
  const box = text('div', 'emp-toggle-box', null);
  const button = el('button', 'emp-toggle' + (on ? ' is-on' : ''), {type: 'button', name: 'manual'});
  button.setAttribute('role', 'switch');
  button.setAttribute('aria-checked', String(on));
  button.disabled = !!disabled;
  if (disabled) button.title = 'Изменения доступны только на сегодняшнюю дату.';
  button.append(el('span', 'emp-toggle-box-mark'), text('span', null, label));
  const note = text('p', 'emp-toggle-hint', hint);
  button.addEventListener('click', () => { onChange(!on); render(); });
  box.append(button, note);
  return box;
}
function monthlyDrawer() {
  const isEdit = ui.sel !== 'm:new';
  const row = isEdit ? salaried().find(p => 'm:' + p.id === ui.sel) : null;
  const d = ui.draft;
  const card = text('section', 'emp-side-card emp-drawer', null);
  card.append(drawerHead(isEdit ? 'СОТРУДНИК НА ОКЛАДЕ' : 'НОВЫЙ СОТРУДНИК'));
  if (!isEdit) card.append(kindSwitch('salary'));
  if (row) {
    const idRow = text('div', 'emp-drawer-id', null);
    idRow.append(text('span', 'emp-avatar is-lg', initials(row.name)));
    const box = text('div', null, null);
    // Должность и пометка — отдельными узлами: переводчик берёт их по одному.
    const sub = text('div', 'emp-drawer-sub', null);
    sub.append(text('span', null, row.role || '—'), ' · ', text('span', null, 'на окладе'));
    box.append(text('div', 'emp-drawer-name', row.name), sub);
    idRow.append(box);
    card.append(idRow);
    const facts = text('div', 'emp-drawer-facts', null);
    [['Оклад в месяц', sum(row.salary)], [$('monthly-paid-head').textContent, sum(row.paid)],
      [row.rest < 0 ? 'Переплата' : 'Осталось выдать', sum(Math.abs(row.rest))]].forEach(([label, value], index) => {
      const line = text('div', 'emp-drawer-line' + (index === 2 && row.rest < 0 ? ' is-over' : ''), null);
      line.append(text('span', null, label), text('strong', null, value));
      facts.append(line);
    });
    facts.append(text('p', 'emp-drawer-note', 'Части оклада выдаются в «Финансах дня» или прямо в ячейках «Зарплаты · месяц».'));
    card.append(facts);
  }
  const form = text('div', 'emp-drawer-form', null);
  const nameInput = el('input', null, {name: 'name', value: d.name, placeholder: 'Фамилия Имя', maxLength: 160});
  nameInput.addEventListener('input', event => setDraft('name', event.target.value));
  const roleInput = el('input', null, {name: 'role', value: d.role, placeholder: 'Должность', maxLength: 80});
  roleInput.addEventListener('input', event => setDraft('role', event.target.value));
  const scheduleInput = el('input', null, {name: 'schedule', value: d.schedule, placeholder: 'Например, 5/2, с 9:00', maxLength: 160});
  scheduleInput.addEventListener('input', event => setDraft('schedule', event.target.value));
  form.append(fieldLabel('Имя', nameInput), fieldLabel('Должность', roleInput),
    fieldLabel('Оклад в месяц, сум', moneyInput('salary', d.salary, 'Например, 6 000 000')),
    fieldLabel('График (необязательно)', scheduleInput),
    manualToggle('Нет в Hikvision', d.noHik, value => setDraft('noHik', value),
      'Пометка «⊘ Hik» в реестре и ведомости. На оклад не влияет.'));
  if (isEdit) {
    const reasonInput = el('input', null, {name: 'reason', value: d.reason,
      placeholder: 'Например, повышение с октября', maxLength: 200});
    reasonInput.addEventListener('input', event => setDraft('reason', event.target.value));
    form.append(fieldLabel('Причина изменения', reasonInput));
  }
  form.append(feedbackLine(), drawerActions('Сохранить', () => saveMonthly()));
  card.append(form);
  if (isEdit) card.append(deleteBlock(doDeleteMonthly), historyBlock());
  return card;
}
function deleteBlock(onDelete) {
  const box = text('div', 'emp-drawer-delete', null);
  if (!ui.confirm) {
    const ask = el('button', 'emp-delete-link', {type: 'button', textContent: 'Удалить из реестра'});
    ask.addEventListener('click', () => { ui.confirm = true; render(); });
    box.append(ask);
    return box;
  }
  const question = text('div', 'emp-delete-q', null);
  question.append(text('strong', null, 'Удалить сотрудника?'),
    text('span', null, 'Из новых списков уйдёт. Прошлые дни, выплаты и ведомость сохранятся.'));
  box.append(question);
  const keep = el('button', 'emp-delete-keep', {type: 'button', textContent: 'Оставить'});
  keep.addEventListener('click', () => { ui.confirm = false; render(); });
  const remove = el('button', 'emp-delete-yes', {type: 'button', textContent: 'Удалить'});
  remove.dataset.busyKey = 'emp-delete';
  remove.addEventListener('click', () => { const work = onDelete(); if (B) B.button(remove, work, {done: false}); });
  box.append(keep, remove);
  return box;
}

// ── История изменений (1a: «запись в истории») ──────────────────────────────
async function loadHistory(kind, id) {
  const key = kind + ':' + id;
  ui.history = {key, rows: null, error: false};
  try {
    const url = (kind === 'monthly' ? '/api/accountant/monthly-employees/' : '/api/accountant/employees/') +
      encodeURIComponent(id) + '/history';
    const response = await fetch(url, {cache: 'no-store'});
    if (!response.ok) throw new Error();
    const data = await response.json();
    if (ui.history?.key !== key) return;
    ui.history.rows = data.history || [];
  } catch { if (ui.history?.key === key) ui.history.error = true; }
  if (ui.history?.key === key) renderAside(people(), {attention: people().filter(needsAttention)});
}
const historyActions = {create: 'Добавлен', update: 'Изменение', manual: 'Hikvision', hikvision: 'Hikvision', delete: 'Удалён'};
function historyWhen(value) {
  const at = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : value + '+05:00');
  return Number.isNaN(at.getTime()) ? value : new Intl.DateTimeFormat('ru-RU', {day: 'numeric', month: 'short',
    hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Tashkent'}).format(at);
}
function historyBlock() {
  const box = text('div', 'emp-history', null);
  box.append(text('p', 'emp-side-eyebrow', 'ИСТОРИЯ ИЗМЕНЕНИЙ'));
  const state = ui.history;
  const unit = String(ui.sel).startsWith('m:') ? 'Оклад' : 'Ставка';
  if (!state || state.rows == null) {
    box.append(text('p', 'emp-history-empty', state?.error ? 'Историю не удалось загрузить.' : 'Загружаем историю…'));
    return box;
  }
  if (!state.rows.length) { box.append(text('p', 'emp-history-empty', 'Изменений пока не было.')); return box; }
  const list = text('ol', 'emp-history-list', null);
  state.rows.slice(0, 20).forEach(row => {
    const item = text('li', 'emp-history-item', null);
    const head = text('div', 'emp-history-head', null);
    head.append(text('strong', null, historyActions[row.action] || 'Изменение'),
      text('span', null, historyWhen(row.changed_at) + (row.changed_by ? ' · ' + row.changed_by : '')));
    item.append(head, text('div', 'emp-history-reason', row.reason));
    const changes = [];
    if ((row.old_rate || '') !== (row.new_rate || '') && row.action !== 'manual' && row.action !== 'hikvision') {
      const fmt = value => value == null ? 'нет' : money(value);
      changes.push(row.action === 'create' ? unit + ' ' + fmt(row.new_rate)
        : row.action === 'delete' ? unit + ' был' + (unit === 'Ставка' ? 'а ' : ' ') + fmt(row.old_rate)
          : unit + ' ' + fmt(row.old_rate) + ' → ' + fmt(row.new_rate));
    }
    if (row.old_group && row.new_group && row.old_group !== row.new_group) changes.push('Группа ' + row.old_group + ' → ' + row.new_group);
    if (row.details) changes.push(...row.details.split('; '));
    // Каждое изменение — отдельным узлом: переводчик берёт их по одному.
    if (changes.length) {
      const line = text('div', 'emp-history-change', null);
      changes.forEach((change, index) => { if (index) line.append(' · '); line.append(text('span', null, change)); });
      item.append(line);
    }
    list.append(item);
  });
  box.append(list);
  return box;
}

// ── Запись ───────────────────────────────────────────────────────────────────
// false — «не удалось»: так кнопка в busy.js не покажет ✓.
const fail = value => { ui.feedback = value; ui.fbErr = true; render(); return false; };
// Сохранённую строку реестра подсвечиваем — видно, что именно изменилось.
const flashRow = key => B?.flash(document.querySelector('[data-busy-key="' + CSS.escape(key) + '"]'));
const amountError = {bad: 'Сумма — только цифры, например 180 000.', zero: 'Ставка должна быть больше нуля.'};
async function saveDraft() {
  const d = ui.draft, name = d.name.trim();
  if (!name) return fail('Укажите имя.');
  if (!d.role.trim()) return fail('Укажите должность.');
  if (!d.group) return fail('Выберите группу.');
  const parsed = L.parseAmount(d.rate);
  if (parsed.error) return fail(amountError[parsed.error]);
  // API ждёт ставку строкой (rate: str | None) — число pydantic не принимает.
  const rate = parsed.value;
  const isEdit = ui.sel !== 'new';
  if (isEdit && !d.reason.trim()) return fail('Укажите причину — она попадёт в историю изменений.');
  try {
    if (isEdit) {
      const id = ui.sel;
      const before = people().find(p => p.employee_id === id);
      const payload = {name, role: d.role.trim(), rate, group: d.group, reason: d.reason.trim()};
      if (!before || !!before.manual_attendance !== d.manual) payload.manual_attendance = d.manual;
      // Номер Hikvision отправляем, только если его поменяли: пусто — снять привязку.
      const hikId = (d.hikId || '').trim();
      if (!d.manual && hikId !== (before?.hikvision_id || '')) {
        if (hikId && !/^[0-9A-Za-z_-]{1,32}$/.test(hikId)) return fail('ID в Hikvision — номер сотрудника на устройстве: цифры и латиница, до 32 знаков.');
        payload.hikvision_id = hikId || null;
      }
      const response = await fetch('/api/accountant/employees/' + encodeURIComponent(id), {
        method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось сохранить.');
      await reloadDay();
      // Панель остаётся открытой с новыми данными: причина очищена, история
      // пополнилась — следующая правка не унаследует прежнюю причину.
      const fresh = people().find(p => p.employee_id === id);
      if (ui.sel === id && fresh) { ui.draft = shiftDraft(fresh); loadHistory('shift', id); render(); }
      message('Изменение сохранено с сегодняшнего дня.');
      flashRow('emp:' + id);
    } else {
      const payload = {name, role: d.role.trim(), rate, group: d.group, manual_attendance: d.manual};
      // Создание — через RetroFinancialWrite: у сотрудника есть ставка, и повтор
      // после потерянного ответа иначе завёл бы второго человека.
      const response = await RetroFinancialWrite('/api/accountant/employees', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось добавить.');
      ui.sel = null; ui.draft = null;
      await reloadDay(); message('Новый сотрудник добавлен.');
      const newId = result?.employee?.id;
      if (newId != null) flashRow('emp:' + newId);
    }
    return true;
  } catch (error) { return fail(error.message); }
}
async function doDelete() {
  try {
    const response = await fetch('/api/accountant/employees/' + encodeURIComponent(ui.sel), {method: 'DELETE'});
    if (!response.ok) { const result = await response.json().catch(() => ({})); throw new Error(result.detail || 'Не удалось удалить.'); }
    ui.sel = null; ui.draft = null; ui.confirm = false; ui.history = null;
    await reloadDay(); message('Сотрудник удалён. Прошлые дни сохранены.');
    return true;
  } catch (error) { return fail(error.message); }
}
async function saveMonthly() {
  const d = ui.draft, name = d.name.trim(), role = d.role.trim();
  if (!name) return fail('Укажите имя.');
  if (!role) return fail('Укажите должность.');
  const parsed = L.parseAmount(d.salary);
  if (parsed.error === 'bad') return fail('Оклад — только цифры, например 6 000 000.');
  if (!parsed.value) return fail('Оклад должен быть больше нуля.');
  const isEdit = ui.sel !== 'm:new';
  const id = isEdit ? Number(String(ui.sel).slice(2)) : null;
  // Поля ручного реестра (карта, наличные, авансы, остаток) на экране не
  // показываем — при правке отправляем их как были, у нового — нули.
  const before = isEdit ? (current.monthly_employees || []).find(p => p.id === id) || {} : {};
  const payload = {name, role, salary: parsed.value, schedule: d.schedule.trim(), no_hikvision: !!d.noHik,
    reason: (d.reason || '').trim(),
    card: before.card ?? '0', cash: before.cash ?? '0', advances: before.advances ?? '0', remaining: before.remaining ?? '0'};
  try {
    const options = {method: isEdit ? 'PATCH' : 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)};
    const response = isEdit
      ? await fetch('/api/accountant/monthly-employees/' + encodeURIComponent(id), options)
      : await RetroFinancialWrite('/api/accountant/monthly-employees', options);
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось сохранить оклад.');
    if (!isEdit) { ui.sel = null; ui.draft = null; }
    await reloadDay();
    if (isEdit && ui.sel === 'm:' + id) {
      const fresh = salaried().find(p => p.id === id);
      if (fresh) { ui.draft = monthlyDraft(fresh); loadHistory('monthly', id); render(); }
    }
    message(isEdit ? 'Оклад сохранён.' : 'Сотрудник на окладе добавлен.');
    flashRow('emp:m:' + (isEdit ? id : result?.employee?.id));
    return true;
  } catch (error) { return fail(error.message); }
}
async function doDeleteMonthly() {
  const id = Number(String(ui.sel).slice(2));
  try {
    const response = await fetch('/api/accountant/monthly-employees/' + encodeURIComponent(id), {method: 'DELETE'});
    if (!response.ok) { const result = await response.json().catch(() => ({})); throw new Error(result.detail || 'Не удалось удалить сотрудника.'); }
    ui.sel = null; ui.draft = null; ui.confirm = false; ui.history = null;
    await reloadDay(); message('Сотрудник удалён. Прошлые дни сохранены.');
    return true;
  } catch (error) { return fail(error.message); }
}

// ── День / загрузка ──────────────────────────────────────────────────────────
const shift = (day, delta) => { const d = new Date(day + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + delta); return d.toISOString().slice(0, 10); };
let day;
function setDay(next) {
  if (!next || next > today) return;
  day = next;
  // День — в адресе: F5 и ссылка открывают тот же день.
  try { const url = new URL(location.href); if (day === today) url.searchParams.delete('date'); else url.searchParams.set('date', day);
    history.replaceState(null, '', url); } catch { /* адрес не меняем */ }
  reloadDay();
}
function paintDayControls() {
  $('day-label').textContent = day ? formattedDay(day) : '—';
  $('day-next').disabled = !day || day >= today;
  const yesterday = shift(today, -1);
  $('day-today').classList.toggle('is-active', day === today);
  $('day-yesterday').classList.toggle('is-active', day === yesterday);
  $('day-today').setAttribute('aria-pressed', String(day === today));
  $('day-yesterday').setAttribute('aria-pressed', String(day === yesterday));
}
// Сколько выдано каждому на окладе с начала месяца по выбранный день.
async function fetchMonthPaid(date) {
  try {
    const response = await fetch('/api/accountant/payroll/month?month=' + encodeURIComponent(date.slice(0, 7)), {cache: 'no-store'});
    if (!response.ok) return {};
    const data = await response.json();
    const paid = {};
    Object.entries(data.monthly_cells || {}).forEach(([id, cells]) => {
      paid[id] = Object.entries(cells).reduce((total, [cellDay, amount]) => total + (cellDay <= date ? Number(amount) : 0), 0);
    });
    return paid;
  } catch { return {}; }
}
async function fetchDay() {
  if (!day) return;
  const sequence = ++requestNo;
  paintDayControls();
  // Пока идёт день, в шапке остаётся прежняя подпись, а реестр гаснет
  // (reloadDay); самая первая загрузка — скелет в разметке.
  try {
    const [response, paid] = await Promise.all([
      fetch('/api/accountant/staff?date=' + encodeURIComponent(day), {cache: 'no-store'}), fetchMonthPaid(day)]);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить сотрудников.');
    if (sequence !== requestNo) return;
    current = data; monthPaid = paid;
    if (data.date !== today && ui.sel != null && !String(ui.sel).startsWith('m:')) closeDrawer();
    render();
    $('employees-message').hidden = true;
    // Как у «Финансов дня»: в шапке — за какой день данные. Состояние Hikvision
    // добавляем, только когда с ним что-то не так; демо-режим назван плашкой.
    const health = current.attendance?.status;
    status(health === 'ok' || health === 'not_configured' ? 'Данные за ' + formattedDay(day)
      : attendanceHealth(current.attendance) + ' · ' + formattedDay(day));
  } catch (error) { if (sequence === requestNo) { message(error.message, true); status('Данные не загрузились'); } }
}

$('day-prev').addEventListener('click', () => setDay(shift(day, -1)));
$('day-next').addEventListener('click', () => setDay(shift(day, 1)));
$('day-today').addEventListener('click', () => setDay(today));
$('day-yesterday').addEventListener('click', () => setDay(shift(today, -1)));
$('employees-search').addEventListener('input', event => { ui.q = event.target.value; renderRegistry(people()); renderMonthly(); });
$('employees-clear-filter').addEventListener('click', () => { ui.filter = 'all'; render(); });
$('employees-add').addEventListener('click', () => openNew('shift'));
// Закрыть панель: ×, Esc, а на телефоне и планшете — касание мимо листа.
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape' || ui.sel == null) return;
  if (ui.confirm) { ui.confirm = false; render(); return; }
  closeDrawer();
});
$('employees-aside').addEventListener('click', event => {
  if (event.target === $('employees-aside') && ui.sel != null && overlayMode()) closeDrawer();
});
$('monthly-add').addEventListener('click', () => openNew('salary'));
$('employees-download').addEventListener('click', () => {
  if (!day) return;
  const button = $('employees-download');
  // Кнопка в работе, пока файл не начал скачиваться.
  const work = downloadAll();
  if (B) B.button(button, work); else { button.disabled = true; work.finally(() => { button.disabled = false; }); }
});
async function downloadAll() {
  try {
    const response = await fetch('/api/accountant/employees/export?scope=all&date=' + encodeURIComponent(day), {cache: 'no-store'});
    if (!response.ok) throw new Error('Не удалось скачать файл сотрудников.');
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url; link.download = 'Retro-employees-' + day + '.xlsx';
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    message('Полный список скачан.');
    return true;
  } catch (error) { message(error.message, true); return false; }
}

(async () => {
  try {
    today = (await globalThis.RetroConfig).today;
    const requested = new URLSearchParams(location.search).get('date');
    day = requested && requested <= today ? requested : today;
    await fetchDay();
  } catch (error) { message(error.message, true); }
})();
