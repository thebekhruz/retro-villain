const $ = id => document.getElementById(id);
const money = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
const decimal = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });
const logic = globalThis.DirectorLogic;

/** На телефоне показываем все показатели в карточке рядом с названием. */
const phone = matchMedia('(max-width:700px)');

/** Сколько позиций показываем до «Показать все». На телефоне каждая
 *  позиция — карточка, и двух с половиной десятков уже слишком много. */
function previewRows() { return phone.matches ? 12 : 25; }

/** Сколько опоздавших видно до нажатия. Остальные сворачиваются: смена
 *  начинается в 10:00, и в плохой день список уезжает за экран. */
const LATE_PREVIEW = 5;

const view = { snapshot: null, group: 'all', sort: 'revenue', query: '', expanded: false };

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

/** Название из iiko или имя сотрудника: переводчик его не трогает, иначе
 *  «Шашлык сет на 6 человек» превращается в «… на 6 kishi». */
function dataName(tag, value) {
  const node = text(tag, '', value);
  node.dataset.i18n = 'off';
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
  name.append(dataName('strong', row.name), text('span', '', noteText));
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
  first.setAttribute('aria-busy', 'false');
  second.setAttribute('aria-busy', 'false');
  if (!bad.length) second.append(text('p', 'empty-state', 'Заметных позиций с низкой маржой нет'));
  bad.forEach((row, index) => second.append(highlightRow(row, index, sums(row.revenue),
    'прибыль ' + sums(row.profit))));
}

function menuRow(row, groupTotals) {
  const tr = document.createElement('tr');
  const first = document.createElement('td');
  const cell = text('div', 'item-cell');
  const share = text('div', 'item-share');
  const fill = document.createElement('span');
  fill.style.width = (logic.shareOf(row, groupTotals) * 100).toFixed(1) + '%';
  share.append(fill);
  cell.append(dataName('strong', row.name), share);
  first.append(cell);
  // Подпись колонки едет с ячейкой: на телефоне таблица разворачивается
  // в карточки, и шапка там не видна.
  const cells = financialCells(row);
  const last = document.createElement('td');
  last.dataset.label = 'Маржа';
  last.append(marginCell(row.margin));
  tr.append(first, ...cells, last);
  return tr;
}

function detailLine(label, value) {
  const line = text('small', 'metric-detail');
  line.append(text('span', '', label), text('span', '', value));
  return line;
}

function zeroParts(row) {
  return row.breakdown ? ['chef', 'tasting', 'other_zero'].map(key => row.breakdown[key]) : null;
}

function financialCells(row) {
  const parts = zeroParts(row);
  const zeroCost = parts ? parts.reduce((sum, part) => sum + part.cost, 0) : null;
  const zeroQuantity = parts ? parts.reduce((sum, part) => sum + part.quantity, 0) : null;
  const cells = [
    ['Количество', decimal.format(row.quantity)],
    ['Выручка', money.format(Math.round(row.revenue))],
    ['Себестоимость', money.format(Math.round(row.cost))],
    ['Прибыль продаж', row.breakdown ? money.format(Math.round(row.breakdown.sales.profit)) : '—'],
    ['Расход без выручки', zeroCost === null ? '—' : money.format(Math.round(zeroCost))],
    ['Прибыль итоговая', money.format(Math.round(row.profit))],
  ].map(([label, value]) => {
    const cell = text('td', 'metric-cell');
    cell.dataset.label = label;
    cell.append(text('span', 'metric-label', label), text('strong', 'metric-main', value));
    return cell;
  });
  if (row.breakdown) {
    cells[0].append(detailLine('Продажи', decimal.format(row.breakdown.sales.quantity)),
      detailLine('Без выручки', decimal.format(zeroQuantity)));
    cells[2].append(detailLine('Продажи', money.format(Math.round(row.breakdown.sales.cost))));
    [['chef', 'Счёт Шефа'], ['tasting', 'Дегустация'], ['other_zero', 'Прочее']].forEach(([key, label]) => {
      const part = row.breakdown[key];
      if (part.quantity || part.cost) cells[4].append(detailLine(label,
        decimal.format(part.quantity) + ' · ' + sums(part.cost)));
    });
  }
  return cells;
}

function renderProfitSummary(total) {
  const parts = zeroParts(total);
  $('sales-profit').textContent = total.breakdown ? sums(total.breakdown.sales.profit) : '—';
  $('internal-cost').textContent = parts ? sums(parts.reduce((sum, part) => sum + part.cost, 0)) : '—';
  $('summary-revenue').textContent = sums(total.revenue);
  $('summary-profit').textContent = sums(total.profit);
  const details = $('internal-detail');
  details.replaceChildren();
  if (total.breakdown) {
    [['chef', 'Счёт Шефа'], ['tasting', 'Дегустация'], ['other_zero', 'Прочее без выручки']].forEach(([key, label]) => {
      details.append(detailLine(label, sums(total.breakdown[key].cost)));
    });
  }
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
    name.append(dataName('strong', row.name));
    const last = document.createElement('td');
    last.dataset.label = 'Маржа';
    last.append(marginCell(row.margin));
    const cells = financialCells(row);
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
  // Пустой список исключённых групп раньше давал «исключены группы: .» — теперь
  // эта часть появляется только когда что-то действительно исключено.
  const excludedGroups = Object.entries(snapshot.excluded_revenue || {});
  $('cash-note').textContent = 'Продажи до исключений меню. Меню: ' + sums(snapshot.menu_revenue ?? all.revenue)
    + (excludedGroups.length ? '; исключены группы: ' + excludedGroups.map(([name, amount]) => name + ' ' + sums(amount)).join(', ') : '')
    + '. Вне банкетной выборки: ' + sums(snapshot.scope_excluded_revenue || 0) + '. Яндекс: оплаты ' + sums(snapshot.yandex_revenue) + ', меню ' + sums(snapshot.yandex_menu_revenue || 0) + '.';
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
  renderProfitSummary(all);
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

let snapshotController = null, snapshotRequest = 0;

/** Период спрашиваем у общего контрола: он же проверяет границы, поэтому
 *  сюда доходит только диапазон, который сервер примет. */
function periodQuery() {
  const chosen = period ? period.state() : null;
  if (!chosen) return '';
  return '?start=' + encodeURIComponent(chosen.start) + '&end=' + encodeURIComponent(chosen.end);
}

// ── Отклик и ожидание (busy.js, T-393) ─────────────────────────────────
const Busy = globalThis.RetroBusy;
const busyButton = (button, work, opts) => (Busy ? Busy.button(button, work, opts) : Promise.resolve(work));

/** Разделы с цифрами периода. Пока считается новый период, прежние цифры
 *  на экране чужие — они гаснут (не стираются); управление живое. */
const DATA_SECTIONS = '.workspace > .shift, .workspace > .profit-summary, .workspace > .highlights, .workspace > .menu-panel, .workspace > details.fold';

function skel(className = 'rm-skel is-val') { return text('span', className); }

/** Строки-скелеты списка (топы, опоздавшие, архив). */
function skeletonLines(target, rows) {
  const lines = [];
  for (let index = 0; index < rows; index += 1) {
    const line = text('div', 'dr-skel-line');
    const body = text('span');
    body.append(skel('rm-skel'), skel('rm-skel'));
    line.append(skel('rm-skel is-dot'), body, skel('rm-skel is-end'));
    lines.push(line);
  }
  target.replaceChildren(...lines);
  target.setAttribute('aria-busy', 'true');
}

/** Первая загрузка: вместо «—» полосы в строке каждой цифры, в таблице и
 *  топах — строки-скелеты. Всё это заменят данные при отрисовке. */
function paintSkeleton() {
  ['cash', 'retro', 'oxbridge', 'banquet', 'yandex', 'margin-percent', 'gross-profit', 'cost-total', 'positions',
    'summary-revenue', 'sales-profit', 'internal-cost', 'summary-profit', 'arrived', 'late']
    .forEach(id => $(id).replaceChildren(skel()));
  $('report-period').replaceChildren(skel());
  skeletonLines($('locomotives'), 3);
  skeletonLines($('drains'), 3);
  skeletonLines($('attendance-list'), 2);
  skeletonLines($('reports'), 2);
  const rows = [];
  for (let index = 0; index < 6; index += 1) {
    const row = document.createElement('tr');
    row.className = 'dr-skel-tr';
    row.setAttribute('aria-hidden', 'true');
    const cell = document.createElement('td');
    cell.colSpan = 8;
    const line = text('div', 'dr-skel-row');
    line.append(skel('rm-skel'), skel('rm-skel'), skel('rm-skel'), skel('rm-skel'));
    cell.append(line);
    row.append(cell);
    rows.push(row);
  }
  $('menu-rows').replaceChildren(...rows);
}

/** Данные не пришли — прочерк вместо вечной полосы. */
function dashSkeletons(ids) {
  ids.forEach(id => { const node = $(id); if (node && node.querySelector('.rm-skel')) node.textContent = '—'; });
}

function stateText(message, kind) {
  const node = $('state');
  node.textContent = message;
  node.classList.toggle('is-working', kind === 'working');
  node.classList.toggle('is-error', kind === 'error');
}

/** Вернёт true, если новый отчёт на экране, false — если нет (для ✓ кнопки). */
async function loadSnapshot(refresh = false) {
  if (period && !period.valid()) {
    stateText('Поправьте даты периода.', 'error');
    return false;
  }
  snapshotController?.abort();
  snapshotController = new AbortController();
  const requestId = ++snapshotRequest;
  const first = !view.snapshot;
  stateText(first ? 'Считаем период в iiko…' : 'Пересчитываем период в iiko — на экране прежние цифры…', 'working');
  let finish;
  const done = new Promise(resolve => { finish = resolve; });
  if (Busy && !first) document.querySelectorAll(DATA_SECTIONS).forEach(section => Busy.section(section, done));
  try {
    const query = periodQuery();
    const refreshParam = refresh === true ? (query ? '&refresh=1' : '?refresh=1') : '';
    const snapshot = await request('/api/director/report' + query + refreshParam,
                                   {signal: snapshotController.signal});
    if (requestId !== snapshotRequest) return false;
    $('setup').hidden = true;
    renderSnapshot(snapshot);
    stateText('Данные за период получены · «Обновить» проверит изменения iiko');
    $('connection').textContent = 'iiko отвечает';
    return true;
  } catch (error) {
    if (requestId !== snapshotRequest || error.name === 'AbortError') return false;
    stateText(error.message + (view.snapshot ? ' На экране прежний отчёт за ' + logic.periodLabel(view.snapshot.period_start, view.snapshot.period_end) + '; обновление не выполнено.' : ''), 'error');
    if (!view.snapshot) {
      dashSkeletons(['cash', 'retro', 'oxbridge', 'banquet', 'yandex', 'margin-percent', 'gross-profit', 'cost-total',
        'positions', 'summary-revenue', 'sales-profit', 'internal-cost', 'summary-profit', 'report-period']);
      ['locomotives', 'drains'].forEach(id => { $(id).replaceChildren(); $(id).setAttribute('aria-busy', 'false'); });
      $('menu-rows').replaceChildren();
    }
    if (error.status === 503) showSetup(error.message);
    else $('connection').textContent = 'Нет данных';
    return false;
  } finally {
    finish();
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
    target.setAttribute('aria-busy', 'false');
    if (!late.length) {
      target.append(text('p', 'empty-state', 'Опоздавших сегодня нет'));
      return true;
    }
    target.append(text('p', 'attendance-title', 'Опоздали'));
    target.classList.remove('is-unfolded');
    late.forEach((row, index) => {
      const item = text('div', 'attendance-row');
      const time = row.first_entry
        ? new Date(row.first_entry).toLocaleTimeString('ru-RU',
          { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Tashkent' })
        : '—';
      item.append(dataName('b', row.name), text('span', '', time));
      if (index >= LATE_PREVIEW) item.classList.add('is-folded');
      target.append(item);
    });
    if (late.length > LATE_PREVIEW) {
      const hidden = late.length - LATE_PREVIEW;
      const more = text('button', 'attendance-more', 'И ещё ' + hidden);
      more.type = 'button';
      more.setAttribute('aria-expanded', 'false');
      more.addEventListener('click', () => {
        const opened = target.classList.toggle('is-unfolded');
        more.setAttribute('aria-expanded', String(opened));
        more.textContent = opened ? 'Свернуть' : 'И ещё ' + hidden;
      });
      target.append(more);
    }
  } catch (error) {
    $('arrived').textContent = '—';
    $('late').textContent = '—';
    $('attendance-note').textContent = error.message;
    $('attendance-list').replaceChildren();
    $('attendance-list').setAttribute('aria-busy', 'false');
    return false;
  }
  return true;
}

const created = new Intl.DateTimeFormat('ru-RU',
  { dateStyle: 'short', timeStyle: 'short', timeZone: 'Asia/Tashkent' });

function reportCard(report) {
  const link = document.createElement('a');
  link.className = 'report-link';
  link.href = '/api/director/reports/' + report.id + '/pdf';
  link.dataset.busyKey = 'dir-report:' + report.id;
  link.addEventListener('click', downloadPdf);
  const body = text('div', 'report-body');
  // Сводку список отдаёт отдельным полем, вместе с временем формирования.
  const summary = report.analysis_summary || (report.analysis && report.analysis.summary) || '';
  // Сводку пишет AI по-русски: переводчик по кускам делал из неё смесь языков.
  const summaryNode = text('span', '', summary);
  summaryNode.dataset.i18n = 'off';
  body.append(text('strong', '', logic.periodLabel(report.period_start, report.period_end)), summaryNode);
  if (report.created_at) body.append(text('span', 'report-created', created.format(new Date(report.created_at))));
  link.append(text('span', 'report-icon', '▤'), body, text('span', 'report-format', 'PDF'));
  return link;
}

/** PDF скачиваем сами: карточка «в работе», пока файл не пришёл, потом
 *  вспыхивает; ошибка — тостом, а не пустой вкладкой с текстом ответа. */
function downloadPdf(event) {
  const link = event.currentTarget;
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || !Busy) return;
  event.preventDefault();
  if (link.getAttribute('aria-busy') === 'true') return;
  const work = (async () => {
    const response = await fetch(link.href);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || 'Не удалось скачать PDF.');
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const file = document.createElement('a');
    file.href = url;
    file.download = 'Retro-director-report.pdf';
    document.body.append(file);
    file.click();
    file.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  })();
  Busy.row(link, work).catch(error => globalThis.RetroToast?.show(error.message, 'error'));
}

async function loadReports() {
  const target = $('reports');
  try {
    const data = await request('/api/director/reports');
    target.replaceChildren();
    target.setAttribute('aria-busy', 'false');
    if (!data.reports.length) {
      target.append(text('p', 'empty-state', 'Сохранённых отчётов пока нет'));
      return;
    }
    data.reports.forEach(report => target.append(reportCard(report)));
  } catch (error) {
    target.replaceChildren(text('p', 'empty-state', error.message));
    target.setAttribute('aria-busy', 'false');
  }
}

$('refresh').addEventListener('click', () => {
  const work = Promise.all([loadSnapshot(true), loadAttendance()]).then(([snapshot]) => snapshot);
  busyButton($('refresh'), work);
});

/** Отчёт с разбором AI собирается долго (iiko за период + модель + PDF).
 *  Всё это время видно, что идёт работа и сколько уже прошло: кнопка
 *  крутится, строка состояния считает время, в архиве стоит карточка
 *  «формируется». Готовый отчёт встаёт первым и вспыхивает; ошибка остаётся
 *  карточкой с «Повторить». */
function minutes(ms) {
  const seconds = Math.floor(ms / 1000);
  return Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
}

function pendingCard(label) {
  const card = text('div', 'report-link is-pending');
  card.setAttribute('role', 'status');
  const body = text('div', 'report-body');
  const note = text('span', 'report-note', 'Собираем данные iiko и пишем разбор…');
  const bar = text('span', 'report-progress');
  bar.append(document.createElement('i'));
  const title = text('strong', '', label);
  body.append(title, note, bar);
  const clock = text('span', 'report-format report-clock', '0:00');
  clock.dataset.i18n = 'off';
  card.append(text('span', 'report-icon report-spin'), body, clock);
  return {card, note, clock};
}

function generate() {
  const button = $('generate');
  if (period && !period.valid()) { stateText('Поправьте даты периода.', 'error'); return; }
  const chosen = period ? period.state() : null;
  const label = chosen ? logic.periodLabel(chosen.start, chosen.end) : '';
  const list = $('reports');
  list.querySelectorAll('.report-link.is-pending, .report-link.is-failed').forEach(node => node.remove());
  list.querySelectorAll(':scope > .empty-state').forEach(node => node.remove());
  const {card, note, clock} = pendingCard(label);
  list.prepend(card);
  const started = Date.now();
  const tick = () => {
    const elapsed = Date.now() - started;
    clock.textContent = minutes(elapsed);
    stateText('Формируем отчёт · ' + minutes(elapsed), 'working');
    if (elapsed > 20000) note.textContent = 'AI пишет разбор — обычно до двух минут, страницу можно не трогать.';
  };
  tick();
  const timer = setInterval(tick, 1000);
  const work = request('/api/director/reports' + periodQuery(), { method: 'POST' });
  busyButton(button, work);
  work.then(async report => {
    clearInterval(timer);
    stateText('Отчёт сохранён в архиве · ' + minutes(Date.now() - started));
    globalThis.RetroToast?.show('Отчёт готов · PDF в архиве');
    await loadReports();
    const saved = report && report.id ? list.querySelector('[data-busy-key="dir-report:' + report.id + '"]') : list.querySelector('.report-link');
    if (saved && Busy) Busy.flash(saved);
  }, error => {
    clearInterval(timer);
    stateText(error.message, 'error');
    card.classList.remove('is-pending');
    card.classList.add('is-failed');
    card.setAttribute('role', 'alert');
    note.textContent = error.message;
    card.querySelector('.report-progress')?.remove();
    const icon = card.querySelector('.report-spin');
    icon.className = 'report-icon report-fail';
    icon.textContent = '!';
    const retry = text('button', 'report-retry', 'Повторить');
    retry.type = 'button';
    retry.addEventListener('click', generate);
    clock.replaceWith(retry);
  });
}

$('generate').addEventListener('click', generate);

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

phone.addEventListener('change', () => { renderMenu(); });

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

let period = null;

(async function start() {
  paintSkeleton();
  let today = new Date().toISOString().slice(0, 10);
  try {
    const config = await globalThis.RetroConfig;
    if (config && config.today) today = config.today;
  } catch (error) {
    $('state').textContent = error.message;
  }
  period = RetroPeriod.mount({
    host: $('period-host'), today: today, modes: ['range'], preset: '10',
    // Обработчику контрол передаёт выбранный период, и он попадал в
    // аргумент «перечитать мимо кеша»: смена периода каждый раз лезла в iiko.
    onChange: () => loadSnapshot(),
    // Чип периода крутится, пока iiko считает новый период.
    busy: true,
  });
  loadSnapshot();
  loadAttendance();
  loadReports();
})();
