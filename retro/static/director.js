const $ = id => document.getElementById(id);
const money = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
const decimal = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });
const logic = globalThis.DirectorLogic;

/** Телефонная раскладка: список блюд превращается в карточки, часть
 *  показателей прячется под тап. Поворот экрана слушаем, иначе после
 *  альбомной ориентации страница остаётся в чужом режиме. */
const phone = matchMedia('(max-width:700px)');

/** Сколько позиций показываем до «Показать все». На телефоне каждая
 *  позиция — карточка, и двух с половиной десятков уже слишком много. */
function previewRows() { return phone.matches ? 12 : 25; }

const view = { snapshot: null, group: 'all', sort: 'revenue', query: '', expanded: false, open: new Set() };

async function request(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = new Error(body.detail || 'Не удалось получить данные.');
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function sums(value) { return money.format(Math.round(value)) + ' сум'; }

function percent(value) {
  return value === null ? '—' : decimal.format(value) + '%';
}

function text(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

/** Цвет маржи: зелёный — норма, охра — ниже трети, красный — торгуем в минус.
 *  Пороги грубые намеренно: точное значение рядом цифрой. */
function marginCell(value) {
  const cell = text('span', 'margin-value');
  if (value === null) {
    cell.classList.add('is-none');
    cell.textContent = '—';
    return cell;
  }
  if (value < 0) cell.classList.add('is-loss');
  else if (value < 33) cell.classList.add('is-low');
  cell.append(text('span', 'margin-dot'), document.createTextNode(percent(value)));
  return cell;
}

function highlightRow(row, index, valueText, noteText) {
  const node = text('div', 'highlight-row');
  const name = text('div', 'highlight-name');
  name.append(text('strong', '', row.name), text('span', '', noteText));
  const value = text('div', 'highlight-value');
  value.append(text('strong', '', valueText), text('span', '', percent(row.margin)));
  node.append(text('span', 'highlight-rank', String(index + 1)), name, value);
  return node;
}

function renderHighlights(group) {
  const top = logic.locomotives(group, 3);
  const bad = logic.drains(group, 3);
  const first = $('locomotives');
  first.replaceChildren();
  if (!top.length) first.append(text('p', 'empty-state', 'Прибыльных позиций за период нет'));
  top.forEach((row, index) => first.append(highlightRow(row, index, sums(row.profit),
    'выручка ' + sums(row.revenue))));
  const second = $('drains');
  second.replaceChildren();
  if (!bad.length) second.append(text('p', 'empty-state', 'Заметных позиций с низкой маржой нет'));
  bad.forEach((row, index) => second.append(highlightRow(row, index, sums(row.revenue),
    'прибыль ' + sums(row.profit))));
}

function menuRow(row, groupTotals) {
  const tr = document.createElement('tr');
  // Подробности по позиции на телефоне открываются тапом: в свёрнутом виде
  // карточка — это название, выручка и маржа, то есть три вопроса из трёх.
  if (phone.matches) {
    tr.tabIndex = 0;
    tr.setAttribute('role', 'button');
    const open = view.open.has(row.name);
    tr.classList.toggle('is-open', open);
    tr.setAttribute('aria-expanded', String(open));
    tr.setAttribute('aria-label', row.name + ', подробности');
    const toggle = () => {
      if (view.open.has(row.name)) view.open.delete(row.name);
      else view.open.add(row.name);
      const next = view.open.has(row.name);
      tr.classList.toggle('is-open', next);
      tr.setAttribute('aria-expanded', String(next));
    };
    tr.addEventListener('click', toggle);
    tr.addEventListener('keydown', event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      toggle();
    });
  }
  const first = document.createElement('td');
  const cell = text('div', 'item-cell');
  const share = text('div', 'item-share');
  const fill = document.createElement('span');
  fill.style.width = (logic.shareOf(row, groupTotals) * 100).toFixed(1) + '%';
  share.append(fill);
  cell.append(text('strong', '', row.name), share);
  first.append(cell);
  // Подпись колонки едет с ячейкой: на телефоне таблица разворачивается
  // в карточки, и шапка там не видна.
  const cells = [
    ['Продано', decimal.format(row.quantity)],
    ['Выручка', money.format(Math.round(row.revenue))],
    ['Себестоимость', money.format(Math.round(row.cost))],
    ['Прибыль', money.format(Math.round(row.profit))],
  ].map(([label, value]) => {
    const cell = text('td', '', value);
    cell.dataset.label = label;
    return cell;
  });
  const last = document.createElement('td');
  last.dataset.label = 'Маржа';
  last.append(marginCell(row.margin));
  tr.append(first, ...cells, last);
  return tr;
}

function renderMenu() {
  const snapshot = view.snapshot;
  if (!snapshot) return;
  const group = snapshot.item_metrics[view.group] || {};
  const groupTotals = logic.totals(group);
  const ranked = logic.search(logic.rank(group, view.sort), view.query);
  const shown = view.expanded ? ranked : ranked.slice(0, previewRows());
  const body = $('menu-rows');
  body.replaceChildren();
  shown.forEach(row => body.append(menuRow(row, groupTotals)));
  $('menu-empty').hidden = ranked.length > 0;
  // Выручку считаем по тому, что сейчас в списке: при поиске итог группы
  // рядом с двумя найденными строками только путает.
  const shownRevenue = ranked.reduce((total, row) => total + row.revenue, 0);
  const positions = logic.plural(ranked.length, 'позиция', 'позиции', 'позиций');
  $('menu-count').textContent = ranked.length
    ? (shown.length < ranked.length
        ? 'Показано ' + shown.length + ' из ' + ranked.length + ' · '
        : ranked.length + ' ' + positions + ' · ')
      + 'выручка ' + sums(shownRevenue)
    : '';
  const more = $('menu-more');
  more.hidden = ranked.length <= previewRows();
  more.textContent = view.expanded ? 'Свернуть список' : 'Показать все позиции';
  renderHighlights(group);
}

function renderWaiters(metrics) {
  const rows = logic.waiters(metrics);
  const body = $('waiter-rows');
  body.replaceChildren();
  rows.forEach(row => {
    const tr = document.createElement('tr');
    const name = document.createElement('td');
    name.append(text('strong', '', row.name));
    const last = document.createElement('td');
    last.dataset.label = 'Маржа';
    last.append(marginCell(row.margin));
    const cells = [
      ['Позиций продано', decimal.format(row.quantity)],
      ['Выручка', money.format(Math.round(row.revenue))],
      ['Прибыль', money.format(Math.round(row.profit))],
    ].map(([label, value]) => {
      const cell = text('td', '', value);
      cell.dataset.label = label;
      return cell;
    });
    tr.append(name, ...cells, last);
    body.append(tr);
  });
  $('waiters-empty').hidden = rows.length > 0;
}

function renderSnapshot(snapshot) {
  view.snapshot = snapshot;
  view.expanded = false;
  const all = logic.totals(snapshot.item_metrics.all || {});
  $('report-period').textContent = logic.periodLabel(snapshot.period_start, snapshot.period_end);
  $('cash').textContent = money.format(Math.round(logic.amount(snapshot.cash_total)));
  // В трёх узких плитках «сум» у каждого числа не помещается и рвёт строку:
  // единица измерения стоит один раз, у главной цифры над ними.
  $('retro').textContent = money.format(Math.round(logic.totals(snapshot.item_metrics.retro || {}).revenue));
  $('oxbridge').textContent = money.format(Math.round(logic.totals(snapshot.item_metrics.oxbridge || {}).revenue));
  $('banquet').textContent = money.format(Math.round(logic.totals(snapshot.item_metrics.banquet || {}).revenue));
  $('yandex').textContent = money.format(Math.round(logic.amount(snapshot.yandex_revenue)));
  $('margin-percent').textContent = all.margin === null ? '—' : decimal.format(all.margin);
  $('margin-fill').style.width = Math.max(0, Math.min(100, all.margin || 0)) + '%';
  $('gross-profit').textContent = sums(all.profit);
  $('cost-total').textContent = sums(all.cost);
  $('positions').textContent = String(all.positions);
  renderMenu();
  renderWaiters(snapshot.waiter_metrics || {});
}

function showSetup(message) {
  $('setup').hidden = false;
  // Текст подсказки постоянный, а ответ сервера показываем отдельной строкой:
  // из него видно, чего именно не хватает.
  $('setup-reason').textContent = 'Сервер отвечает: ' + message;
  $('connection').textContent = 'Меню не настроено';
}

async function loadSnapshot() {
  $('state').textContent = 'Загружаем данные…';
  try {
    const snapshot = await request('/api/director/today');
    $('setup').hidden = true;
    renderSnapshot(snapshot);
    $('state').textContent = 'Данные за период получены';
    $('connection').textContent = 'iiko отвечает';
  } catch (error) {
    $('state').textContent = error.message;
    if (error.status === 503) showSetup(error.message);
    else $('connection').textContent = 'Нет данных';
  }
}

async function loadAttendance() {
  try {
    const data = await request('/api/director/attendance');
    const status = data.attendance?.status || 'starting';
    const labels = {ok: 'Hikvision · актуально', starting: 'Hikvision · подключение',
      stale: 'Hikvision · данные устарели', not_configured: 'Hikvision · не настроен'};
    $('attendance-note').textContent = labels[status] || 'Hikvision · нет связи';
    $('arrived').textContent = String(data.arrived_count);
    $('late').textContent = String(data.late_count);
    $('attendance-date').textContent = logic.dayLabel(data.date);
    const late = (data.employees || []).filter(row => row.status === 'late');
    const target = $('attendance-list');
    target.replaceChildren();
    if (!late.length) {
      target.append(text('p', 'empty-state', 'Опоздавших сегодня нет'));
      return;
    }
    target.append(text('p', 'attendance-title', 'Опоздали'));
    late.slice(0, 5).forEach(row => {
      const item = text('div', 'attendance-row');
      const time = row.first_entry
        ? new Date(row.first_entry).toLocaleTimeString('ru-RU',
          { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Tashkent' })
        : '—';
      item.append(text('b', '', row.name), text('span', '', time));
      target.append(item);
    });
    if (late.length > 5) target.append(text('p', 'attendance-more', 'И ещё ' + (late.length - 5)));
  } catch (error) {
    $('arrived').textContent = '—';
    $('late').textContent = '—';
    $('attendance-note').textContent = error.message;
  }
}

const created = new Intl.DateTimeFormat('ru-RU',
  { dateStyle: 'short', timeStyle: 'short', timeZone: 'Asia/Tashkent' });

function reportCard(report) {
  const link = document.createElement('a');
  link.className = 'report-link';
  link.href = '/api/director/reports/' + report.id + '/pdf';
  const body = text('div', 'report-body');
  // Сводку список отдаёт отдельным полем, вместе с временем формирования.
  const summary = report.analysis_summary || (report.analysis && report.analysis.summary) || '';
  body.append(text('strong', '', logic.periodLabel(report.period_start, report.period_end)),
    text('span', '', summary));
  if (report.created_at) body.append(text('span', 'report-created', created.format(new Date(report.created_at))));
  link.append(text('span', 'report-icon', '▤'), body, text('span', 'report-format', 'PDF'));
  return link;
}

async function loadReports() {
  const target = $('reports');
  try {
    const data = await request('/api/director/reports');
    target.replaceChildren();
    if (!data.reports.length) {
      target.append(text('p', 'empty-state', 'Сохранённых отчётов пока нет'));
      return;
    }
    data.reports.forEach(report => target.append(reportCard(report)));
  } catch (error) {
    target.replaceChildren(text('p', 'empty-state', error.message));
  }
}

$('refresh').addEventListener('click', () => { loadSnapshot(); loadAttendance(); });

$('generate').addEventListener('click', async () => {
  const button = $('generate');
  button.disabled = true;
  button.classList.add('is-busy');
  $('state').textContent = 'Собираем данные iiko и готовим разбор…';
  try {
    await request('/api/director/reports', { method: 'POST' });
    $('state').textContent = 'Отчёт сохранён в архиве';
    await loadReports();
  } catch (error) {
    $('state').textContent = error.message;
  } finally {
    button.disabled = false;
    button.classList.remove('is-busy');
  }
});

/** После смены направления или сортировки список начинается заново, и
 *  оставлять человека на середине прежнего — значит показать ему чужие
 *  строки. На телефоне это заметнее всего. */
function scrollToList() {
  if (!phone.matches) return;
  const panel = document.querySelector('.menu-panel');
  if (!panel) return;
  const top = panel.getBoundingClientRect().top + window.scrollY - 8;
  window.scrollTo({ top, behavior: 'smooth' });
}

phone.addEventListener('change', () => { view.open.clear(); renderMenu(); });

document.querySelectorAll('.ops-tab[data-group]').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.ops-tab[data-group]').forEach(other => {
      const active = other === tab;
      other.classList.toggle('is-active', active);
      other.setAttribute('aria-selected', String(active));
    });
    view.group = tab.dataset.group;
    view.expanded = false;
    renderMenu();
    scrollToList();
  });
});

document.querySelectorAll('.chip[data-sort]').forEach(chip => {
  chip.addEventListener('click', () => {
    document.querySelectorAll('.chip[data-sort]').forEach(other =>
      other.classList.toggle('active', other === chip));
    view.sort = chip.dataset.sort;
    renderMenu();
    scrollToList();
  });
});

$('menu-query').addEventListener('input', event => {
  view.query = event.target.value;
  renderMenu();
});

$('menu-more').addEventListener('click', () => {
  view.expanded = !view.expanded;
  renderMenu();
});

loadSnapshot();
loadAttendance();
loadReports();
