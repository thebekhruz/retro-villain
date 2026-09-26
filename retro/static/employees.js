const $ = id => document.getElementById(id);
const money = value => new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2}).format(Number(value || 0));
const sum = value => money(value) + ' сум';
const statuses = {on_time: 'Вовремя', late: 'Опоздал', missing: 'Не пришёл', unlinked: 'Нет привязки', unavailable: 'Нет данных'};
let today, current, requestNo = 0;
// UI-состояние живёт отдельно от данных API: карточки-фильтры, вкладка группы, поиск и открытый drawer.
const ui = {filter: 'all', tab: 'all', q: '', sel: null, draft: null, confirm: false, feedback: '', fbErr: false};

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
  item.hidden = !value;
  item.setAttribute('role', error ? 'alert' : 'status');
}
function status(value) { const box = $('connection'); if (box) box.textContent = value; }
function attendanceHealth(value) {
  const states = {
    ok: 'Hikvision синхронизирован.',
    starting: 'Hikvision подключается; отсутствие входа пока не считается прогулом.',
    stale: 'Данные Hikvision устарели; отсутствие входа не считается прогулом.',
    not_configured: 'Проходы Hikvision смоделированы; реальный турникет ресторана пока не подключён.'
  };
  return states[value?.status || 'starting'] || 'Hikvision недоступен; отсутствие входа не считается прогулом.';
}

// Начисление за смену считаем как в прототипе: нет ставки/привязки — не начисляется, не пришёл — 0.
function payFor(row) {
  if (row.rate == null || row.rate === '') return null;
  if (row.status === 'missing') return 0;
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
function groupOrder() {
  const known = (current?.groups || []).map(item => item.name);
  const seen = new Set(known);
  people().forEach(row => { if (row.group && !seen.has(row.group)) { seen.add(row.group); known.push(row.group); } });
  return known;
}
const editable = () => current && current.date === today;

function loadDay() { return fetchDay(); }

// ── Отрисовка ────────────────────────────────────────────────────────────────
function render() {
  const all = people();
  const present = all.filter(p => p.status === 'on_time' || p.status === 'late');
  const late = all.filter(p => p.status === 'late');
  const missing = all.filter(p => p.status === 'missing');
  const noRate = all.filter(p => p.rate == null);
  const unlinked = all.filter(p => p.status === 'unlinked' || p.status === 'unavailable');
  const attention = all.filter(p => p.rate == null || p.status === 'unlinked' || p.status === 'unavailable');
  const accrued = all.reduce((total, p) => total + (p.pay || 0), 0);
  const total = all.length || 1;

  $('employees-attendance-status').textContent = attendanceHealth(current.attendance);
  renderStats({accrued, present, late, missing, attention, noRate, unlinked, total: all.length});
  renderTabs(all);
  renderRegistry(all);
  renderAside(all, {attention});
  renderMonthly(current.monthly_employees || []);
}

function statCard({key, label, accent, value, unit, footer, progress, filterable}) {
  const active = filterable && ui.filter === key;
  const tag = filterable ? 'button' : 'div';
  const card = el(tag, 'emp-stat' + (accent ? ' emp-stat--' + accent : '') + (active ? ' is-active' : ''),
    filterable ? {type: 'button'} : {});
  if (filterable) {
    card.dataset.filter = key;
    card.addEventListener('click', () => { ui.filter = ui.filter === key ? 'all' : key; render(); });
  }
  card.append(text('span', 'emp-stat-label', label));
  const figure = text('div', 'emp-stat-value', null);
  figure.append(text('strong', null, String(value)));
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
    statCard({key: 'present', filterable: true, label: 'На месте', value: s.present.length + ' / ' + s.total,
      progress: pct}),
    statCard({key: 'late', filterable: true, accent: 'late', label: 'Опоздали', value: s.late.length,
      footer: 'Вход после 10:00'}),
    statCard({key: 'missing', filterable: true, label: 'Не пришли', value: s.missing.length,
      footer: 'Начисление 0 сум'}),
    statCard({key: 'attention', filterable: true, accent: 'attention', label: 'Требуют внимания', value: s.attention.length,
      footer: 'Без ставки ' + s.noRate.length + ' · без привязки ' + s.unlinked.length})
  );
}
function renderTabs(all) {
  const box = $('employees-tabs');
  const tabs = [{key: 'all', label: 'Все', count: all.length}];
  groupOrder().forEach(name => {
    const count = all.filter(p => p.group === name).length;
    if (count) tabs.push({key: name, label: name, count});
  });
  box.replaceChildren(...tabs.map(tab => {
    const button = el('button', 'emp-tab' + (ui.tab === tab.key ? ' is-active' : ''), {type: 'button', role: 'tab'});
    button.append(text('span', null, tab.label), text('small', null, String(tab.count)));
    button.addEventListener('click', () => { ui.tab = tab.key; render(); });
    return button;
  }));
}
function matches(row) {
  const q = ui.q.trim().toLowerCase();
  if (ui.tab !== 'all' && row.group !== ui.tab) return false;
  if (q && !(row.name.toLowerCase().includes(q) || (row.role || '').toLowerCase().includes(q))) return false;
  if (ui.filter === 'present') return row.status === 'on_time' || row.status === 'late';
  if (ui.filter === 'late') return row.status === 'late';
  if (ui.filter === 'missing') return row.status === 'missing';
  if (ui.filter === 'attention') return row.rate == null || row.status === 'unlinked' || row.status === 'unavailable';
  return true;
}
function registryRow(row) {
  const line = el('div', 'emp-row' + (ui.sel === row.employee_id ? ' is-selected' : ''), {});
  line.addEventListener('click', () => openDrawer(row.employee_id));
  // Сотрудник
  const who = text('div', 'emp-cell-who', null);
  who.append(text('span', 'emp-avatar', initials(row.name)));
  const idBox = text('div', 'emp-who-text', null);
  idBox.append(text('div', 'emp-who-name', row.name), text('div', 'emp-who-role', row.role));
  who.append(idBox);
  // Первый вход
  const entry = text('div', 'emp-cell-entry', null);
  const time = text('div', 'emp-entry-time', arrivalText(row.first_entry));
  if (row.status === 'late') time.classList.add('is-late');
  else if (row.min == null) time.classList.add('is-none');
  entry.append(time);
  if (row.status === 'late' && row.min != null) entry.append(text('div', 'emp-entry-late', '+' + (row.min - 600) + ' мин'));
  // Статус
  const statusCell = text('div', 'emp-cell-status', null);
  statusCell.append(text('span', 'staff-status ' + row.status, statuses[row.status] || row.status));
  // Ставка
  const rate = text('div', 'emp-cell-num' + (row.rate == null ? ' is-missing' : ''),
    row.rate == null ? 'Нет ставки' : money(row.rate));
  // Начислено
  const pay = text('div', 'emp-cell-num emp-cell-pay' + (row.pay ? '' : ' is-none'),
    row.pay == null ? '—' : money(row.pay));
  line.append(who, entry, statusCell, rate, pay);
  return line;
}
function renderRegistry(all) {
  const container = $('employees-groups');
  container.replaceChildren();
  const shown = all.filter(matches);
  $('employees-shown').textContent = shown.length + ' ' + plural(shown.length, ['сотрудник', 'сотрудника', 'сотрудников']);
  const filterNames = {present: 'На месте', late: 'Опоздали', missing: 'Не пришли', attention: 'Требуют внимания'};
  const clear = $('employees-clear-filter');
  if (ui.filter !== 'all') {
    clear.hidden = false;
    clear.textContent = filterNames[ui.filter] || '';
    clear.append(text('span', 'emp-filter-x', '×'));
  } else clear.hidden = true;

  if (!shown.length) {
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

// ── Боковая панель (drawer) ──────────────────────────────────────────────────
function openDrawer(id) {
  const row = people().find(p => p.employee_id === id);
  if (!row) return;
  ui.sel = id;
  ui.confirm = false; ui.feedback = ''; ui.fbErr = false;
  ui.draft = {name: row.name, role: row.role, rate: row.rate == null ? '' : String(row.rate), group: row.group, reason: ''};
  render();
}
function openNew() {
  const groups = groupOrder();
  ui.sel = 'new'; ui.confirm = false; ui.feedback = ''; ui.fbErr = false;
  ui.draft = {name: '', role: '', rate: '', group: groups[0] || '', reason: ''};
  render();
  const nameInput = document.querySelector('.emp-drawer input[name=name]');
  if (nameInput) nameInput.focus();
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
  box.append(drawerCard());
}
function attentionCard(attention) {
  const card = text('section', 'panel emp-side-card', null);
  card.append(text('p', 'eyebrow emp-side-eyebrow', 'ТРЕБУЮТ ВНИМАНИЯ'), text('h2', 'emp-side-title', 'Не начисляется'));
  if (!attention.length) { card.append(text('p', 'accountant-help', 'Все привязаны и со ставкой.')); return card; }
  attention.forEach(row => {
    const item = el('button', 'emp-side-row', {type: 'button'});
    item.addEventListener('click', () => openDrawer(row.employee_id));
    item.append(text('span', 'emp-avatar is-warn', initials(row.name)));
    const info = text('span', 'emp-side-info', null);
    info.append(text('span', 'emp-side-name', row.name),
      text('span', 'emp-side-reason', row.rate == null ? 'Нет ставки' : 'Нет привязки Hikvision'));
    item.append(info, text('span', 'emp-side-chevron', '›'));
    card.append(item);
  });
  return card;
}
function groupSummaryCard(all) {
  const card = text('section', 'panel emp-side-card', null);
  card.append(text('p', 'eyebrow emp-side-eyebrow', 'ПО ГРУППАМ · НА МЕСТЕ'));
  groupOrder().forEach(name => {
    const rows = all.filter(p => p.group === name);
    if (!rows.length) return;
    const present = rows.filter(p => p.status === 'on_time' || p.status === 'late').length;
    const totalPay = rows.reduce((total, p) => total + (p.pay || 0), 0);
    const line = text('div', 'emp-summary-row', null);
    line.append(text('span', 'emp-summary-name', name),
      text('span', 'emp-summary-presence', present + '/' + rows.length),
      text('strong', 'emp-summary-total', money(totalPay)));
    card.append(line);
  });
  return card;
}
function drawerCard() {
  const isEdit = ui.sel !== 'new';
  const selP = isEdit ? people().find(p => p.employee_id === ui.sel) : null;
  const d = ui.draft;
  const card = text('section', 'panel emp-side-card emp-drawer', null);

  const head = text('div', 'emp-drawer-head', null);
  head.append(text('p', 'eyebrow emp-side-eyebrow', isEdit ? 'СОТРУДНИК' : 'НОВЫЙ СОТРУДНИК'));
  const close = el('button', 'emp-drawer-close', {type: 'button', textContent: '×', title: 'Закрыть'});
  close.addEventListener('click', closeDrawer);
  head.append(close);
  card.append(head);

  if (selP) {
    const idRow = text('div', 'emp-drawer-id', null);
    idRow.append(text('span', 'emp-avatar is-lg', initials(selP.name)));
    const box = text('div', null, null);
    box.append(text('div', 'emp-drawer-name', selP.name), text('div', 'emp-drawer-sub', selP.role + ' · ' + selP.group));
    idRow.append(box);
    card.append(idRow);

    const facts = text('div', 'emp-drawer-facts', null);
    const top = text('div', 'emp-drawer-facts-top', null);
    top.append(text('span', 'staff-status ' + selP.status, statuses[selP.status] || selP.status),
      text('span', 'emp-drawer-entry', selP.min == null ? 'Входа нет' : 'Первый вход ' + arrivalText(selP.first_entry)));
    facts.append(top);
    const payRow = text('div', 'emp-drawer-pay', null);
    payRow.append(text('span', null, 'К начислению'),
      text('strong', selP.pay ? '' : 'is-none', selP.pay == null ? '—' : sum(selP.pay)));
    facts.append(payRow);
    const notes = {on_time: 'Пришёл до 10:00 — ставка начисляется полностью.', late: 'Опоздание не уменьшает ставку.',
      missing: 'Входа нет — начисление 0 сум.', unlinked: 'Нет привязки Hikvision — начисление заблокировано.',
      unavailable: 'Нет данных источника — начисление заблокировано.'};
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
    value: d.rate, placeholder: 'Не указана'});
  rateInput.addEventListener('input', event => setDraft('rate', event.target.value));
  form.append(fieldLabel('Ставка за смену, сум', rateInput));

  if (isEdit) {
    const reasonInput = el('input', null, {name: 'reason', value: d.reason,
      placeholder: 'Например, новая ставка с сентября', maxLength: 200});
    reasonInput.addEventListener('input', event => setDraft('reason', event.target.value));
    form.append(fieldLabel('Причина изменения', reasonInput));
  }

  const feedback = text('p', 'emp-drawer-feedback', ui.feedback);
  feedback.style.color = ui.fbErr ? '#aa4656' : '#4f795c';
  form.append(feedback);

  const actions = text('div', 'emp-drawer-actions', null);
  const save = el('button', 'button primary', {type: 'button', textContent: isEdit ? 'Сохранить' : 'Добавить в реестр'});
  save.disabled = !editable();
  save.addEventListener('click', () => saveDraft());
  const cancel = el('button', 'button secondary', {type: 'button', textContent: 'Отмена'});
  cancel.addEventListener('click', closeDrawer);
  actions.append(save, cancel);
  form.append(actions);
  card.append(form);

  if (!editable()) card.append(text('p', 'emp-drawer-note', 'Изменения доступны только на сегодняшнюю дату.'));

  if (isEdit && editable()) card.append(deleteBlock());
  return card;
}
function deleteBlock() {
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
  remove.addEventListener('click', () => doDelete());
  box.append(keep, remove);
  return box;
}

// ── Запись ───────────────────────────────────────────────────────────────────
async function saveDraft() {
  const d = ui.draft, name = d.name.trim();
  if (!name) { ui.feedback = 'Укажите имя.'; ui.fbErr = true; return render(); }
  if (!d.role.trim()) { ui.feedback = 'Укажите должность.'; ui.fbErr = true; return render(); }
  if (d.rate !== '' && Number(d.rate) <= 0) { ui.feedback = 'Ставка должна быть больше нуля.'; ui.fbErr = true; return render(); }
  // API ждёт ставку строкой (rate: str | None) — число pydantic не принимает.
  const rate = d.rate === '' ? null : String(d.rate).trim();
  const isEdit = ui.sel !== 'new';
  if (isEdit && !d.reason.trim()) { ui.feedback = 'Укажите причину — она попадёт в историю изменений.'; ui.fbErr = true; return render(); }
  try {
    if (isEdit) {
      const payload = {name, role: d.role.trim(), rate, group: d.group, reason: d.reason.trim()};
      const response = await fetch('/api/accountant/employees/' + encodeURIComponent(ui.sel), {
        method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось сохранить.');
      await fetchDay(); message('Изменение сохранено с сегодняшнего дня.');
    } else {
      const payload = {name, role: d.role.trim(), rate, group: d.group};
      // Создание — через RetroFinancialWrite: у сотрудника есть ставка, и повтор
      // после потерянного ответа иначе завёл бы второго человека.
      const response = await RetroFinancialWrite('/api/accountant/employees', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось добавить.');
      ui.sel = null; ui.draft = null;
      await fetchDay(); message('Новый сотрудник добавлен.');
    }
  } catch (error) { ui.feedback = error.message; ui.fbErr = true; render(); }
}
async function doDelete() {
  try {
    const response = await fetch('/api/accountant/employees/' + encodeURIComponent(ui.sel), {method: 'DELETE'});
    if (!response.ok) { const result = await response.json(); throw new Error(result.detail || 'Не удалось удалить.'); }
    ui.sel = null; ui.draft = null; ui.confirm = false;
    await fetchDay(); message('Сотрудник удалён.');
  } catch (error) { ui.feedback = error.message; ui.fbErr = true; render(); }
}

// ── Месячная зарплата (без изменений логики) ─────────────────────────────────
function renderMonthly(list) {
  const container = $('monthly-employees');
  container.replaceChildren();
  if (!list.length) { container.append(text('p', 'accountant-help', 'Сотрудников с месячным окладом пока нет.')); return; }
  const table = document.createElement('table');
  table.className = 'employee-roster-table monthly-roster-table';
  table.innerHTML = '<thead><tr><th>Имя</th><th>Должность</th><th>Оклад</th><th>График</th><th>На карту</th><th>Наличные</th><th>Авансы</th><th>Остаток</th><th>Действия</th></tr></thead>';
  const body = document.createElement('tbody');
  list.forEach(row => {
    const tr = document.createElement('tr');
    [row.name, row.role, sum(row.salary), row.schedule, sum(row.card), sum(row.cash), sum(row.advances), sum(row.remaining)]
      .forEach(value => tr.append(text('td', '', value)));
    const actions = document.createElement('td');
    const edit = text('button', 'edit-monthly', 'Изменить'); edit.type = 'button';
    edit.addEventListener('click', () => editMonthlyRow(row, tr));
    const remove = text('button', 'employee-delete', 'Удалить'); remove.type = 'button';
    remove.addEventListener('click', () => confirmMonthlyDelete(row, actions));
    actions.append(edit, remove); tr.append(actions); body.append(tr);
  });
  table.append(body); container.append(table);
}
function editMonthlyRow(row, tableRow) {
  if (tableRow.querySelector('input')) return;
  const fields = ['name', 'role', 'salary', 'schedule', 'card', 'cash', 'advances', 'remaining'];
  const cells = [...tableRow.querySelectorAll('td')];
  fields.forEach((field, index) => {
    const input = document.createElement('input');
    const numeric = !['name', 'role', 'schedule'].includes(field);
    input.type = numeric ? 'number' : 'text';
    if (numeric) { input.min = '0'; input.step = '0.01'; }
    input.value = row[field]; input.name = field; input.className = 'employee-cell-input';
    cells[index].replaceChildren(input);
  });
  const save = text('button', 'edit-monthly', 'Сохранить'); save.type = 'button';
  const cancel = text('button', 'employee-delete', 'Отмена'); cancel.type = 'button';
  cancel.addEventListener('click', fetchDay);
  save.addEventListener('click', async () => {
    const values = Object.fromEntries(fields.map((field, index) => [field, cells[index].querySelector('input').value]));
    try {
      const response = await fetch('/api/accountant/monthly-employees/' + encodeURIComponent(row.id), {
        method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || 'Не удалось сохранить месячную зарплату.');
      await fetchDay(); message('Месячный оклад сохранён.');
    } catch (error) { message(error.message, true); }
  });
  cells[8].replaceChildren(save, cancel); cells[0].querySelector('input').focus();
}
function confirmMonthlyDelete(row, actions) {
  if (actions.querySelector('.employee-delete-confirm')) return;
  const menu = document.createElement('div'); menu.className = 'employee-delete-confirm';
  menu.append(text('span', '', 'Удалить сотрудника?'));
  const keep = text('button', '', 'Оставить'); keep.type = 'button';
  const remove = text('button', 'is-danger', 'Удалить'); remove.type = 'button';
  keep.addEventListener('click', fetchDay);
  remove.addEventListener('click', async () => {
    const response = await fetch('/api/accountant/monthly-employees/' + encodeURIComponent(row.id), {method: 'DELETE'});
    if (!response.ok) return message('Не удалось удалить сотрудника.', true);
    await fetchDay(); message('Сотрудник удалён.');
  });
  menu.append(keep, remove); actions.replaceChildren(menu);
}
function addMonthlyRow() {
  if (document.querySelector('.monthly-add-row')) return;
  const row = document.createElement('form'); row.className = 'employee-add-row monthly-add-row';
  row.innerHTML = '<input name="name" required maxlength="160" placeholder="Имя">' +
    '<input name="role" required maxlength="80" placeholder="Должность">' +
    '<input name="salary" type="number" min="0" step="0.01" required placeholder="Оклад">' +
    '<input name="schedule" maxlength="160" placeholder="График">' +
    '<input name="card" type="number" min="0" step="0.01" value="0" aria-label="На карту">' +
    '<input name="cash" type="number" min="0" step="0.01" value="0" aria-label="Наличные">' +
    '<input name="advances" type="number" min="0" step="0.01" value="0" aria-label="Авансы">' +
    '<input name="remaining" type="number" min="0" step="0.01" value="0" aria-label="Остаток">' +
    '<button class="button primary" type="submit">Добавить</button>' +
    '<button class="button secondary" type="button" data-cancel>Отмена</button>';
  $('monthly-employees').prepend(row); row.elements.name.focus();
  row.querySelector('[data-cancel]').addEventListener('click', () => row.remove());
  row.addEventListener('submit', async event => {
    event.preventDefault(); if (!row.reportValidity()) return;
    const response = await RetroFinancialWrite('/api/accountant/monthly-employees', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(Object.fromEntries(new FormData(row)))});
    const result = await response.json();
    if (!response.ok) return message(result.detail || 'Не удалось добавить сотрудника.', true);
    await fetchDay(); message('Сотрудник с месячной зарплатой добавлен.');
  });
}

// ── День / загрузка ──────────────────────────────────────────────────────────
const shift = (day, delta) => { const d = new Date(day + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + delta); return d.toISOString().slice(0, 10); };
let day;
function setDay(next) { if (!next || next > today) return; day = next; fetchDay(); }
function paintDayControls() {
  $('day-label').textContent = day ? formattedDay(day) : '—';
  $('day-next').disabled = !day || day >= today;
  const yesterday = shift(today, -1);
  $('day-today').classList.toggle('is-active', day === today);
  $('day-yesterday').classList.toggle('is-active', day === yesterday);
}
async function fetchDay() {
  if (!day) return;
  const sequence = ++requestNo;
  paintDayControls();
  status('Загрузка данных за ' + formattedDay(day));
  try {
    const response = await fetch('/api/accountant/staff?date=' + encodeURIComponent(day), {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить сотрудников.');
    if (sequence !== requestNo) return;
    current = data;
    if (data.date !== today && ui.sel != null) closeDrawer();
    render();
    message('');
    status('Данные за ' + formattedDay(day));
  } catch (error) { if (sequence === requestNo) { message(error.message, true); status('Данные не загрузились'); } }
}

$('day-prev').addEventListener('click', () => setDay(shift(day, -1)));
$('day-next').addEventListener('click', () => setDay(shift(day, 1)));
$('day-today').addEventListener('click', () => setDay(today));
$('day-yesterday').addEventListener('click', () => setDay(shift(today, -1)));
$('employees-search').addEventListener('input', event => { ui.q = event.target.value; renderRegistry(people()); });
$('employees-clear-filter').addEventListener('click', () => { ui.filter = 'all'; render(); });
$('employees-add').addEventListener('click', openNew);
$('monthly-add').addEventListener('click', addMonthlyRow);
$('employees-download').addEventListener('click', async () => {
  if (!day) return;
  const button = $('employees-download');
  button.disabled = true;
  try {
    const response = await fetch('/api/accountant/employees/export?scope=all&date=' + encodeURIComponent(day), {cache: 'no-store'});
    if (!response.ok) throw new Error('Не удалось скачать файл сотрудников.');
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url; link.download = 'Retro-employees-' + day + '.xlsx';
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    message('Полный список скачан.');
  } catch (error) { message(error.message, true); } finally { button.disabled = false; }
});

(async () => {
  try {
    today = (await globalThis.RetroConfig).today;
    const requested = new URLSearchParams(location.search).get('date');
    day = requested && requested <= today ? requested : today;
    await fetchDay();
  } catch (error) { message(error.message, true); }
})();
