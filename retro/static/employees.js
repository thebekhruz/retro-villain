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
  const hadGroups = container.querySelector('details') !== null;
  const opened = new Set([...container.querySelectorAll('details[open]')].map(item => item.dataset.group));
  container.replaceChildren();
  data.groups.forEach(group => {
    const details = document.createElement('details');
    details.className = 'employee-group';
    details.dataset.group = group.name;
    details.open = !hadGroups || opened.has(group.name);
    const summary = document.createElement('summary');
    summary.append(text('strong', '', group.name), text('span', '', group.count + ' чел. · ' + money(group.draft_total)));
    details.append(summary);
    const list = text('div', 'staff-list', '');
    data.employees.filter(row => row.group === group.name).forEach(row => {
      const card = text('article', 'staff-row is-' + row.status, '');
      const identity = document.createElement('div');
      identity.append(text('div', 'staff-name', row.name),
        text('div', 'staff-role', row.role + (row.rate === null ? ' · Нет ставки' : ' · ' + money(row.rate))));
      const time = row.first_entry
        ? new Date(row.first_entry).toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Tashkent'})
        : '—';
      const line = text('div', 'staff-line', '');
      line.append(text('span', '', 'Вход: ' + time + (row.exception ? ' · разовое разрешение' : '')),
        text('strong', '', row.payable === null ? 'Не рассчитано' : money(row.payable)));
      card.append(identity, text('span', 'staff-status ' + row.status, statuses[row.status] || row.status), line);
      list.append(card);
    });
    details.append(list);
    container.append(details);
  });
  options($('edit-employee'), data.employees.map(row => ({
    id: row.employee_id, label: row.name + ' · ' + row.role + (row.rate === null ? ' · нет ставки' : '')
  })), 'Выберите сотрудника');
  options($('edit-group'), data.groups.map(group => ({id: group.name, label: group.name})), 'Выберите группу');
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
$('edit-employee').addEventListener('change', () => {
  const person = current?.employees.find(item => String(item.employee_id) === $('edit-employee').value);
  $('employee-form').elements.rate.value = person?.rate ?? '';
  $('edit-group').value = person?.group ?? '';
});
$('employee-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const fields = new FormData(form);
    const response = await fetch('/api/accountant/employees/' + encodeURIComponent(fields.get('employee_id')), {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({rate: fields.get('rate') || null, group: fields.get('group'), reason: fields.get('reason')})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось сохранить исправление.');
    form.reset();
    await loadDay();
    message('Изменение сохранено. Подтверждённые начисления прошлых дней не изменены.');
  } catch (error) { message(error.message, true); } finally { button.disabled = false; }
});
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
