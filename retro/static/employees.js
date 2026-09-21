const $ = id => document.getElementById(id);
const money = value => new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2}).format(Number(value || 0)) + ' сум';
const statuses = {on_time: 'Вовремя', late: 'Опоздал', missing: 'Не пришёл', unlinked: 'Нет привязки', unavailable: 'Нет данных'};
const columns = ['Имя', 'Роль', 'Зарплата / ставка', 'Группа', 'Статус', 'Действия'];
const formattedDay = day => new Intl.DateTimeFormat('ru-RU', {day: 'numeric', month: 'long', year: 'numeric',
  timeZone: 'Asia/Tashkent'}).format(new Date(day + 'T12:00:00+05:00'));
let today, current, requestNo = 0;

function text(tag, className, value) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  item.textContent = value;
  return item;
}
// Единое пустое состояние — такое же, как в журнале бухгалтера.
function emptyState(glyph, value) {
  const box = text('div', 'empty-state', '');
  const sign = text('span', '', glyph);
  sign.setAttribute('aria-hidden', 'true');
  box.append(sign, text('p', '', value));
  return box;
}
// Склонение по-русски: «1 сотрудник», «2 сотрудника», «5 сотрудников».
function plural(count, forms) {
  const tail = Math.abs(count) % 100, unit = tail % 10;
  if (tail > 10 && tail < 20) return forms[2];
  if (unit > 1 && unit < 5) return forms[1];
  return unit === 1 ? forms[0] : forms[2];
}
function stat(label, value, flagged) {
  const card = text('div', 'employees-stat' + (flagged && value > 0 ? ' is-flagged' : ''), '');
  card.append(text('span', '', label), text('strong', '', String(value)));
  return card;
}
function message(value, error = false) {
  const item = $('employees-message');
  item.textContent = value;
  item.hidden = !value;
  item.setAttribute('role', error ? 'alert' : 'status');
}
function options(select, items, placeholder) {
  const old = select.value;
  select.replaceChildren(new Option(placeholder, ''));
  items.forEach(item => select.add(new Option(item.label, item.id)));
  if (items.some(item => String(item.id) === old)) select.value = old;
}
function attendanceHealth(value) {
  const status = value?.status || 'starting';
  const states = {
    ok: 'Hikvision синхронизирован.',
    starting: 'Hikvision подключается; отсутствие входа пока не считается прогулом.',
    stale: 'Данные Hikvision устарели; отсутствие входа не считается прогулом.',
    not_configured: 'Hikvision не настроен; отсутствие входа не считается прогулом.'
  };
  return states[status] || 'Hikvision недоступен; отсутствие входа не считается прогулом.';
}
function render(data) {
  // formattedDay уже отдаёт «19 сентября 2026 г.» — точку в конце не добавляем.
  const health = attendanceHealth(data.attendance);
  $('employees-summary').textContent = 'Статусы и расчёт за ' + formattedDay($('employees-date').value) + '. ' + health;
  $('employees-attendance-status').textContent = health;
  $('employees-stats').replaceChildren(
    stat('Всего в реестре', data.roster_count, false),
    stat('Опоздали после 10:00', data.payroll.late_count, true),
    stat('Не пришли', data.payroll.missing_count, true),
    stat('Без привязки Hikvision', data.payroll.unlinked_count, false),
    stat('Нет данных источника', data.payroll.unavailable_count, true),
    stat('Без ставки', data.missing_rates, true)
  );
  const container = $('employees-groups');
  container.replaceChildren();
  if (!data.employees.length) {
    container.append(emptyState('◫', 'В реестре пока нет сотрудников. Добавьте первого кнопкой «Добавить сотрудника» — роль, ставку и группу можно будет поправить прямо в списке.'));
    return;
  }
  const roles = [...new Set(data.employees.map(row => row.role))].sort((left, right) => left.localeCompare(right, 'ru'));
  roles.forEach(role => {
    const details = document.createElement('details');
    details.className = 'employee-group';
    details.open = true;
    const summary = document.createElement('summary');
    const people = data.employees.filter(row => row.role === role);
    summary.append(text('strong', '', role), text('span', '', people.length + ' чел.'));
    details.append(summary);
    const table = document.createElement('table');
    table.className = 'employee-roster-table';
    table.innerHTML = '<thead><tr><th scope="col">Имя</th><th scope="col">Роль</th><th scope="col">Зарплата / ставка</th><th scope="col">Группа</th><th scope="col">Статус</th><th scope="col">Действия</th></tr></thead>';
    const body = document.createElement('tbody');
    people.sort((left, right) => left.name.localeCompare(right.name, 'ru')).forEach(row => {
      const tr = document.createElement('tr');
      const values = [row.name, row.role, row.rate === null ? 'Нет ставки' : money(row.rate), row.group,
        statuses[row.status] || row.status];
      values.forEach((value, index) => {
        const cell = document.createElement('td');
        cell.dataset.label = columns[index];
        if (index === 4) cell.append(text('span', 'staff-status ' + row.status, value));
        else if (index === 2 && row.rate === null) cell.append(text('span', 'roster-missing', value));
        else cell.textContent = value;
        if (index < 4) cell.addEventListener('dblclick', () => editCell(row, cell, ['name', 'role', 'rate', 'group'][index], data.groups.map(item => item.name)));
        tr.append(cell);
      });
      const actions = document.createElement('td');
      actions.dataset.label = columns[5];
      const edit = text('button', 'edit-monthly', 'Изменить');
      edit.type = 'button';
      edit.addEventListener('click', () => editEmployeeRow(row, tr, data.groups.map(item => item.name)));
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'employee-delete'; remove.textContent = 'Удалить';
      remove.title = 'Удалить сотрудника';
      remove.addEventListener('click', () => confirmDelete(row, tr, actions));
      actions.append(edit, remove); tr.append(actions);
      body.append(tr);
    });
    table.append(body);
    const scroll = text('div', 'employee-roster-scroll', '');
    scroll.append(table);
    details.append(scroll);
    container.append(details);
  });
  renderMonthly(data.monthly_employees || []);
}
function editEmployeeRow(row, tableRow, groups) {
  if (tableRow.querySelector('input,select')) return;
  const cells = [...tableRow.querySelectorAll('td')];
  const fields = ['name', 'role', 'rate', 'group'];
  fields.forEach((field, index) => {
    const input = field === 'group' ? document.createElement('select') : document.createElement('input');
    if (field === 'group') groups.forEach(group => input.add(new Option(group, group)));
    else input.type = field === 'rate' ? 'number' : 'text';
    if (field === 'rate') { input.min = '0'; input.step = '0.01'; }
    input.value = row[field] || ''; input.className = 'employee-cell-input';
    cells[index].replaceChildren(input);
  });
  const save = text('button', 'edit-monthly', 'Сохранить'); save.type = 'button';
  const cancel = text('button', 'employee-delete', 'Отмена'); cancel.type = 'button';
  cancel.addEventListener('click', loadDay);
  save.addEventListener('click', async () => {
    const values = fields.map((field, index) => cells[index].querySelector('input,select').value.trim());
    const payload = {name: values[0], role: values[1], rate: values[2] || null,
      group: values[3], reason: 'Изменение в реестре сотрудников'};
    try {
      const response = await fetch('/api/accountant/employees/' + encodeURIComponent(row.employee_id), {
        method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || 'Не удалось сохранить сотрудника.');
      await loadDay(); message('Изменение сотрудника сохранено.');
    } catch (error) { message(error.message, true); }
  });
  cells[5].replaceChildren(save, cancel); cells[0].querySelector('input').focus();
}
function renderMonthly(people) {
  const container = $('monthly-employees');
  container.replaceChildren();
  if (!people.length) {
    container.append(text('p', 'accountant-help', 'Сотрудников с месячным окладом пока нет.'));
    return;
  }
  const table = document.createElement('table');
  table.className = 'employee-roster-table monthly-roster-table';
  table.innerHTML = '<thead><tr><th>Имя</th><th>Должность</th><th>Оклад</th><th>График</th><th>На карту</th><th>Наличные</th><th>Авансы</th><th>Остаток</th><th>Действия</th></tr></thead>';
  const body = document.createElement('tbody');
  people.forEach(row => {
    const tr = document.createElement('tr');
    [row.name, row.role, money(row.salary), row.schedule, money(row.card), money(row.cash),
      money(row.advances), money(row.remaining)].forEach(value => tr.append(text('td', '', value)));
    const actions = document.createElement('td');
    const edit = text('button', 'edit-monthly', 'Изменить');
    edit.type = 'button';
    edit.addEventListener('click', () => editMonthlyRow(row, tr));
    const remove = text('button', 'employee-delete', 'Удалить');
    remove.type = 'button';
    remove.addEventListener('click', () => confirmMonthlyDelete(row, actions));
    actions.append(edit, remove); tr.append(actions); body.append(tr);
  });
  table.append(body); container.append(table);
}
async function saveMonthly(row, values) {
  const response = await fetch('/api/accountant/monthly-employees/' + encodeURIComponent(row.id), {
    method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || 'Не удалось сохранить месячную зарплату.');
  await loadDay(); message('Месячный оклад сохранён.');
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
  const actions = cells[8];
  const save = text('button', 'edit-monthly', 'Сохранить'); save.type = 'button';
  const cancel = text('button', 'employee-delete', 'Отмена'); cancel.type = 'button';
  cancel.addEventListener('click', loadDay);
  save.addEventListener('click', async () => {
    const values = Object.fromEntries(fields.map((field, index) => [field, cells[index].querySelector('input').value]));
    try { await saveMonthly(row, values); } catch (error) { message(error.message, true); }
  });
  actions.replaceChildren(save, cancel);
  cells[0].querySelector('input').focus();
}
function confirmMonthlyDelete(row, actions) {
  if (actions.querySelector('.employee-delete-confirm')) return;
  const menu = document.createElement('div'); menu.className = 'employee-delete-confirm';
  menu.append(text('span', '', 'Удалить сотрудника?'));
  const keep = text('button', '', 'Оставить'); keep.type = 'button';
  const remove = text('button', 'is-danger', 'Удалить'); remove.type = 'button';
  keep.addEventListener('click', loadDay);
  remove.addEventListener('click', async () => {
    const response = await fetch('/api/accountant/monthly-employees/' + encodeURIComponent(row.id), {method: 'DELETE'});
    if (!response.ok) return message('Не удалось удалить сотрудника.', true);
    await loadDay(); message('Сотрудник удалён.');
  });
  menu.append(keep, remove); actions.replaceChildren(menu);
}
function confirmDelete(row, tableRow, actions) {
  if (actions.querySelector('.employee-delete-confirm')) return;
  const menu = document.createElement('div'); menu.className = 'employee-delete-confirm';
  menu.append(text('span', '', 'Удалить сотрудника?'));
  const keep = document.createElement('button'); keep.type = 'button'; keep.textContent = 'Оставить';
  const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'is-danger'; remove.textContent = 'Удалить';
  menu.append(keep, remove); actions.replaceChildren(menu);
  keep.addEventListener('click', () => { actions.replaceChildren(); actions.append(tableRow.querySelector('.employee-delete') || makeDeleteButton(row, tableRow, actions)); });
  remove.addEventListener('click', async () => {
    remove.disabled = true;
    try {
      const response = await fetch('/api/accountant/employees/' + encodeURIComponent(row.employee_id), {method: 'DELETE'});
      if (!response.ok) { const result = await response.json(); throw new Error(result.detail || 'Не удалось удалить сотрудника.'); }
      await loadDay(); message('Сотрудник удалён.');
    } catch (error) { message(error.message, true); }
  });
}
function makeDeleteButton(row, tableRow, actions) {
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'employee-delete'; button.textContent = 'Удалить';
  button.title = 'Удалить сотрудника'; button.addEventListener('click', () => confirmDelete(row, tableRow, actions));
  return button;
}
async function saveEmployee(row, field, value) {
  const updated = {name: row.name, role: row.role, rate: row.rate, group: row.group,
    reason: 'Изменение в реестре сотрудников'};
  updated[field] = value;
  const response = await fetch('/api/accountant/employees/' + encodeURIComponent(row.employee_id), {
    method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(updated)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось сохранить сотрудника.');
  await loadDay(); message('Изменение сотрудника сохранено.');
}
function editCell(row, cell, field, groups) {
  if (cell.querySelector('input')) return;
  const input = field === 'group' ? document.createElement('select') : document.createElement('input');
  if (field === 'group') groups.forEach(group => input.add(new Option(group, group)));
  input.type = field === 'rate' ? 'number' : 'text';
  input.min = field === 'rate' ? '0' : undefined;
  input.step = field === 'rate' ? '0.01' : undefined;
  input.value = field === 'rate' ? row.rate || '' : row[field];
  input.className = 'employee-cell-input';
  cell.replaceChildren(input); input.focus(); if (input.select) input.select();
  let finished = false;
  const finish = async save => {
    if (finished) return; finished = true;
    const value = input.value.trim();
    if (!save || (field !== 'rate' && !value) || (field === 'rate' && value && Number(value) <= 0)) {
      await loadDay(); return;
    }
    try { await saveEmployee(row, field, field === 'rate' ? value || null : value); }
    catch (error) { message(error.message, true); await loadDay(); }
  };
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter') finish(true);
    if (event.key === 'Escape') finish(false);
  });
  input.addEventListener('blur', () => finish(true));
}
function addEmployeeRow(data) {
  if (!data || document.querySelector('.employee-add-row')) return;
  const row = document.createElement('form'); row.className = 'employee-add-row';
  row.setAttribute('aria-label', 'Новый сотрудник');
  row.innerHTML = '<input name="name" required maxlength="160" placeholder="Имя" aria-label="Имя">' +
    '<input name="role" required maxlength="80" placeholder="Роль" aria-label="Роль">' +
    '<input name="rate" type="number" min="0" step="0.01" placeholder="Зарплата / ставка" aria-label="Зарплата или ставка, сум">' +
    '<select name="group" required aria-label="Группа"><option value="">Группа</option></select>' +
    '<button class="button primary" type="submit">Добавить</button>' +
    '<button class="button secondary" type="button" data-cancel>Отмена</button>';
  data.groups.forEach(group => row.elements.group.add(new Option(group.name, group.name)));
  $('employees-groups').prepend(row);
  row.addEventListener('submit', async event => {
    event.preventDefault(); if (!row.reportValidity()) return;
    const fields = new FormData(row);
    try {
      const response = await fetch('/api/accountant/employees', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: fields.get('name'), role: fields.get('role'), rate: fields.get('rate') || null, group: fields.get('group')})});
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось добавить сотрудника.');
      await loadDay(); message('Новый сотрудник добавлен.');
    } catch (error) { message(error.message, true); }
  });
  row.querySelector('[data-cancel]').addEventListener('click', () => row.remove());
  row.elements.name.focus();
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
    const response = await fetch('/api/accountant/monthly-employees', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.fromEntries(new FormData(row)))
    });
    const result = await response.json();
    if (!response.ok) return message(result.detail || 'Не удалось добавить сотрудника.', true);
    await loadDay(); message('Сотрудник с месячной зарплатой добавлен.');
  });
}
async function loadDay() {
  const day = $('employees-date').value;
  if (!day || !$('employees-date').checkValidity()) { message('Выберите сегодняшний или прошедший день.', true); return; }
  const sequence = ++requestNo;
  $('back-accountant').href = '/accountant?date=' + encodeURIComponent(day);
  try {
    const response = await fetch('/api/accountant/day?date=' + encodeURIComponent(day), {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить сотрудников.');
    if (sequence !== requestNo) return;
    current = data;
    render(data);
    message('');
  } catch (error) { if (sequence === requestNo) message(error.message, true); }
}
$('employees-date').addEventListener('change', loadDay);
$('employees-refresh').addEventListener('click', loadDay);
$('employees-add').addEventListener('click', () => addEmployeeRow(current));
$('monthly-add').addEventListener('click', addMonthlyRow);
$('employees-download').addEventListener('click', async () => {
  const day = $('employees-date').value;
  if (!day || !$('employees-date').checkValidity()) return;
  const button = $('employees-download');
  button.disabled = true;
  try {
    const response = await fetch('/api/accountant/employees/export?scope=all&date=' + encodeURIComponent(day), {cache: 'no-store'});
    if (!response.ok) throw new Error('Не удалось скачать файл сотрудников.');
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = 'Retro-employees-' + day + '.xlsx';
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    message('Полный список скачан.');
  } catch (error) { message(error.message, true); } finally { button.disabled = false; }
});
(async () => {
  try {
    const response = await fetch('/api/config', {cache: 'no-store'});
    if (!response.ok) throw new Error('Не удалось определить текущую дату.');
    today = (await response.json()).today;
    const requested = new URLSearchParams(location.search).get('date');
    $('employees-date').max = today;
    $('employees-date').value = requested && requested <= today ? requested : today;
    await loadDay();
  } catch (error) { message(error.message, true); }
})();
