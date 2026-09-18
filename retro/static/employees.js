const $ = id => document.getElementById(id);
const money = value => new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2}).format(Number(value || 0)) + ' сум';
const statuses = {on_time: 'Вовремя', late: 'Опоздал', missing: 'Не пришёл', unlinked: 'Нет привязки'};
let today, current, requestNo = 0;

function text(tag, className, value) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  item.textContent = value;
  return item;
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
function render(data) {
  $('employees-summary').textContent = data.roster_count + ' сотрудников · ' + data.payroll.late_count +
    ' опоздали · ' + data.payroll.missing_count + ' не пришли · ' +
    data.payroll.unlinked_count + ' без демопривязки · ' + data.missing_rates + ' без ставки.';
  const container = $('employees-groups');
  container.replaceChildren();
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
    table.innerHTML = '<thead><tr><th>Имя</th><th>Роль</th><th>Зарплата / ставка</th><th>Группа</th><th>Статус</th><th>Действия</th></tr></thead>';
    const body = document.createElement('tbody');
    people.sort((left, right) => left.name.localeCompare(right.name, 'ru')).forEach(row => {
      const tr = document.createElement('tr');
      const values = [row.name, row.role, row.rate === null ? 'Нет ставки' : money(row.rate), row.group,
        statuses[row.status] || row.status];
      values.forEach((value, index) => {
        const cell = document.createElement('td'); cell.textContent = value;
        if (index < 4) cell.addEventListener('dblclick', () => editCell(row, cell, ['name', 'role', 'rate', 'group'][index], data.groups.map(item => item.name)));
        tr.append(cell);
      });
      const actions = document.createElement('td');
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'employee-delete'; remove.textContent = 'Удалить';
      remove.title = 'Удалить сотрудника';
      remove.addEventListener('click', () => confirmDelete(row, tr, actions));
      actions.append(remove); tr.append(actions);
      body.append(tr);
    });
    table.append(body); details.append(table);
    container.append(details);
  });
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
  row.innerHTML = '<input name="name" required maxlength="160" placeholder="Имя">' +
    '<input name="role" required maxlength="80" placeholder="Роль">' +
    '<input name="rate" type="number" min="0" step="0.01" placeholder="Зарплата / ставка">' +
    '<select name="group" required><option value="">Группа</option></select>' +
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
    link.download = 'Retro-employees-' + day + '-DEMO.xlsx';
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    message('Полный список скачан. Проходы демонстрационные.');
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
