/* «Сотрудники» (1a): карточки-фильтры сверху, один реестр по группам,
   правка в боковой панели. Сотрудники на окладе — вторым разделом того же
   реестра, правятся в той же панели. */
const $ = id => document.getElementById(id);
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
const ui = {filter: 'all', tab: 'all', q: '', sel: null, draft: null, confirm: false, feedback: '', fbErr: false};
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

// Начисление за смену считаем как в прототипе: нет ставки/привязки — не начисляется, не пришёл — 0.
function payFor(row) {
  if (row.rate == null || row.rate === '') return null;
  if (ABSENT.has(row.status)) return 0;
  if (row.status === 'unlinked' || row.status === 'unavailable') return null;
  const rate = Number(row.rate);
  return Number.isFinite(rate) ? rate : null;
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
function renderStats(s) {
  const box = $('employees-stats');
  const pct = Math.round(s.present.length / (s.total || 1) * 100);
  box.replaceChildren(
    statCard({label: 'К начислению за день', accent: 'accrued', value: money(s.accrued), unit: 'сум',
      footer: s.total + ' в реестре · ' + s.noRate.length + ' без ставки'}),
    statCard({key: 'present', filterable: true, label: 'На месте', value: s.present.length, total: s.total,
      progress: pct}),
    statCard({key: 'late', filterable: true, accent: 'late', label: 'Опоздали', value: s.late.length, dot: 'late',
      footer: 'Вход после 10:00'}),
    statCard({key: 'missing', filterable: true, label: 'Не пришли', value: s.missing.length, dot: 'idle',
      footer: 'Начисление 0 сум'}),
    statCard({key: 'attention', filterable: true, accent: 'attention', label: 'Требуют внимания', value: s.attention.length,
      dot: 'gold', footer: 'Без ставки ' + s.noRate.length + ' · без привязки ' + s.unlinked.length})
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
function queryMatches(name, role) {
  const q = ui.q.trim().toLowerCase();
  return !q || name.toLowerCase().includes(q) || (role || '').toLowerCase().includes(q);
}
function matches(row) {
  if (ui.tab !== 'all' && row.group !== ui.tab) return false;
  if (!queryMatches(row.name, row.role)) return false;
  if (ui.filter === 'present') return PRESENT.has(row.status);
  if (ui.filter === 'late') return row.status === 'late';
  if (ui.filter === 'missing') return ABSENT.has(row.status);
  if (ui.filter === 'attention') return needsAttention(row);
  return true;
}
function whoCell(name, role, warn) {
  const who = text('div', 'emp-cell-who', null);
  who.append(text('span', 'emp-avatar' + (warn ? ' is-warn' : ''), initials(name)));
  const idBox = text('div', 'emp-who-text', null);
  idBox.append(text('div', 'emp-who-name', name));
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
  line.append(whoCell(row.name, row.role), meta, rate, pay);
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
    line.append(whoCell(row.name, null), text('div', 'emp-salary-role', row.role || '—'), salary, paid, rest);
    container.append(line);
  });
}

// ── Боковая панель (drawer) ──────────────────────────────────────────────────
function openDrawer(id) {
  const row = people().find(p => p.employee_id === id);
  if (!row) return;
  ui.sel = id;
  ui.confirm = false; ui.feedback = ''; ui.fbErr = false;
  ui.draft = {name: row.name, role: row.role, rate: row.rate == null ? '' : String(row.rate), group: row.group, reason: ''};
  render(); revealDrawer();
}
function openMonthly(id) {
  const row = salaried().find(p => p.id === id);
  if (!row) return;
  ui.sel = 'm:' + id; ui.confirm = false; ui.feedback = ''; ui.fbErr = false;
  ui.draft = {name: row.name, role: row.role, salary: String(row.salary), schedule: row.schedule || ''};
  render(); revealDrawer();
}
function openNew(kind = 'shift') {
  const groups = groupOrder();
  ui.confirm = false; ui.feedback = ''; ui.fbErr = false;
  if (kind === 'salary') { ui.sel = 'm:new'; ui.draft = {name: '', role: '', salary: '', schedule: ''}; }
  else { ui.sel = 'new'; ui.draft = {name: '', role: '', rate: '', group: groups[0] || '', reason: ''}; }
  render(); revealDrawer();
  const nameInput = document.querySelector('.emp-drawer input[name=name]');
  if (nameInput) nameInput.focus({preventScroll: true});
}
// На телефоне и планшете панель стоит под реестром — подводим к ней экран.
function revealDrawer() {
  const drawer = document.querySelector('.emp-drawer');
  if (drawer && matchMedia('(max-width:900px)').matches) drawer.scrollIntoView({block: 'start', behavior: 'smooth'});
}
function closeDrawer() { ui.sel = null; ui.draft = null; ui.confirm = false; ui.feedback = ''; render(); }
function setDraft(key, value) { ui.draft[key] = value; ui.feedback = ''; }

function fieldLabel(labelText, input) {
  const label = text('label', 'emp-field', null);
  label.append(text('span', null, labelText), input);
  return label;
}
function renderAside(all, ctx) {
  const box = $('employees-aside');
  box.replaceChildren();
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
    info.append(text('span', 'emp-side-name', row.name),
      text('span', 'emp-side-reason', row.rate == null ? 'Нет ставки'
        : row.status === 'unavailable' ? 'Нет данных Hikvision' : 'Нет привязки Hikvision'));
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
    button.addEventListener('click', () => { if (kind !== key) openNew(key); });
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
      missing: 'Входа нет — начисление 0 сум.', unlinked: 'Нет привязки Hikvision — начисление заблокировано.',
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
  roleInput.addEventListener('input', event => setDraft('role', event.target.value));
  form.append(fieldLabel('Должность', roleInput));
  const datalist = el('datalist', null, {id: 'emp-roles'});
  [...new Set(people().map(p => p.role).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'ru'))
    .forEach(role => datalist.append(new Option(role, role)));
  form.append(datalist);

  const groupSelect = el('select', null, {name: 'group'});
  groupOrder().forEach(name => groupSelect.add(new Option(name, name, false, name === d.group)));
  groupSelect.addEventListener('change', event => setDraft('group', event.target.value));
  form.append(fieldLabel('Группа', groupSelect));

  const rateInput = el('input', null, {name: 'rate', type: 'number', min: '0', step: '0.01',
    value: d.rate, placeholder: 'Не указана', inputMode: 'numeric'});
  rateInput.addEventListener('input', event => setDraft('rate', event.target.value));
  form.append(fieldLabel('Ставка за смену, сум', rateInput));

  if (isEdit) {
    const reasonInput = el('input', null, {name: 'reason', value: d.reason,
      placeholder: 'Например, новая ставка с сентября', maxLength: 200});
    reasonInput.addEventListener('input', event => setDraft('reason', event.target.value));
    form.append(fieldLabel('Причина изменения', reasonInput));
  }
  form.append(feedbackLine(), drawerActions(isEdit ? 'Сохранить' : 'Добавить в реестр', () => saveDraft(), !editable()));
  card.append(form);

  if (!editable()) card.append(text('p', 'emp-drawer-note', 'Изменения доступны только на сегодняшнюю дату.'));

  if (isEdit && editable()) card.append(deleteBlock(doDelete));
  return card;
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
  const salaryInput = el('input', null, {name: 'salary', type: 'number', min: '0', step: '0.01', value: d.salary,
    placeholder: 'Например, 6000000', inputMode: 'numeric'});
  salaryInput.addEventListener('input', event => setDraft('salary', event.target.value));
  const scheduleInput = el('input', null, {name: 'schedule', value: d.schedule, placeholder: 'Например, 5/2, с 9:00', maxLength: 160});
  scheduleInput.addEventListener('input', event => setDraft('schedule', event.target.value));
  form.append(fieldLabel('Имя', nameInput), fieldLabel('Должность', roleInput),
    fieldLabel('Оклад в месяц, сум', salaryInput), fieldLabel('График (необязательно)', scheduleInput),
    feedbackLine(), drawerActions(isEdit ? 'Сохранить' : 'Добавить на оклад', () => saveMonthly()));
  card.append(form);
  if (isEdit) card.append(deleteBlock(doDeleteMonthly));
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
  box.append(text('span', 'emp-delete-q', 'Удалить сотрудника?'));
  const keep = el('button', 'emp-delete-keep', {type: 'button', textContent: 'Оставить'});
  keep.addEventListener('click', () => { ui.confirm = false; render(); });
  const remove = el('button', 'emp-delete-yes', {type: 'button', textContent: 'Удалить'});
  remove.dataset.busyKey = 'emp-delete';
  remove.addEventListener('click', () => { const work = onDelete(); if (B) B.button(remove, work, {done: false}); });
  box.append(keep, remove);
  return box;
}

// ── Запись ───────────────────────────────────────────────────────────────────
// false — «не удалось»: так кнопка в busy.js не покажет ✓.
const fail = value => { ui.feedback = value; ui.fbErr = true; render(); return false; };
// Сохранённую строку реестра подсвечиваем — видно, что именно изменилось.
const flashRow = key => B?.flash(document.querySelector('[data-busy-key="' + CSS.escape(key) + '"]'));
async function saveDraft() {
  const d = ui.draft, name = d.name.trim();
  if (!name) return fail('Укажите имя.');
  if (!d.role.trim()) return fail('Укажите должность.');
  if (d.rate !== '' && Number(d.rate) <= 0) return fail('Ставка должна быть больше нуля.');
  // API ждёт ставку строкой (rate: str | None) — число pydantic не принимает.
  const rate = d.rate === '' ? null : String(d.rate).trim();
  const isEdit = ui.sel !== 'new';
  if (isEdit && !d.reason.trim()) return fail('Укажите причину — она попадёт в историю изменений.');
  try {
    if (isEdit) {
      const payload = {name, role: d.role.trim(), rate, group: d.group, reason: d.reason.trim()};
      const response = await fetch('/api/accountant/employees/' + encodeURIComponent(ui.sel), {
        method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось сохранить.');
      const id = ui.sel;
      await reloadDay(); message('Изменение сохранено с сегодняшнего дня.');
      flashRow('emp:' + id);
    } else {
      const payload = {name, role: d.role.trim(), rate, group: d.group};
      // Создание — через RetroFinancialWrite: у сотрудника есть ставка, и повтор
      // после потерянного ответа иначе завёл бы второго человека.
      const response = await RetroFinancialWrite('/api/accountant/employees', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось добавить.');
      ui.sel = null; ui.draft = null;
      await reloadDay(); message('Новый сотрудник добавлен.');
      if (result && result.id != null) flashRow('emp:' + result.id);
    }
    return true;
  } catch (error) { return fail(error.message); }
}
async function doDelete() {
  try {
    const response = await fetch('/api/accountant/employees/' + encodeURIComponent(ui.sel), {method: 'DELETE'});
    if (!response.ok) { const result = await response.json(); throw new Error(result.detail || 'Не удалось удалить.'); }
    ui.sel = null; ui.draft = null; ui.confirm = false;
    await reloadDay(); message('Сотрудник удалён.');
    return true;
  } catch (error) { return fail(error.message); }
}
async function saveMonthly() {
  const d = ui.draft, name = d.name.trim(), role = d.role.trim();
  if (!name) return fail('Укажите имя.');
  if (!role) return fail('Укажите должность.');
  if (!(Number(d.salary) > 0)) return fail('Оклад должен быть больше нуля.');
  const isEdit = ui.sel !== 'm:new';
  const id = isEdit ? Number(String(ui.sel).slice(2)) : null;
  // Поля ручного реестра (карта, наличные, авансы, остаток) на экране не
  // показываем — при правке отправляем их как были, у нового — нули.
  const before = isEdit ? (current.monthly_employees || []).find(p => p.id === id) || {} : {};
  const payload = {name, role, salary: String(d.salary).trim(), schedule: d.schedule.trim(),
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
    message(isEdit ? 'Оклад сохранён.' : 'Сотрудник на окладе добавлен.');
    flashRow('emp:m:' + (isEdit ? id : result.id));
    return true;
  } catch (error) { return fail(error.message); }
}
async function doDeleteMonthly() {
  const id = Number(String(ui.sel).slice(2));
  try {
    const response = await fetch('/api/accountant/monthly-employees/' + encodeURIComponent(id), {method: 'DELETE'});
    if (!response.ok) { const result = await response.json().catch(() => ({})); throw new Error(result.detail || 'Не удалось удалить сотрудника.'); }
    ui.sel = null; ui.draft = null; ui.confirm = false;
    await reloadDay(); message('Сотрудник удалён.');
    return true;
  } catch (error) { return fail(error.message); }
}

// ── День / загрузка ──────────────────────────────────────────────────────────
const shift = (day, delta) => { const d = new Date(day + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + delta); return d.toISOString().slice(0, 10); };
let day;
function setDay(next) { if (!next || next > today) return; day = next; reloadDay(); }
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
