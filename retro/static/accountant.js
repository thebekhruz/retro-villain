/* «Финансы дня» и «Зарплата · день» — экраны бухгалтера (ТЗ 02.10).
   «Финансы дня» (data-page="finance") — операции дня и дэшборд бухгалтера,
   «Сохранить и сдать отчёт» и закрытие месяца. «Зарплата · день»
   (data-page="salary-day") — выдача сменным за вчерашнюю смену.
   Выбранная дата — день выплат P. Смена, которую выдают, — вчерашняя (S = P − 1).
   Расчёты без DOM лежат в accountant-logic.js; здесь только сборка экрана и
   запись: каждая денежная запись идёт через RetroFinancialWrite (ключ
   идемпотентности), чтобы двойной клик не выдал деньги дважды. */
const $ = id => document.getElementById(id);
const PAGE = document.body.dataset.page || 'finance';
// Элемент есть не на каждой странице: подписываемся, только если он есть.
const on = (id, type, fn) => { const el = $(id); if (el) el.addEventListener(type, fn); return el; };
const L = globalThis.AccountantLogic;
const number = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
const fmt = value => number.format(Number(value || 0));
const money = value => fmt(value) + ' сум';
const dm = iso => iso ? iso.slice(8, 10) + '.' + iso.slice(5, 7) : '';

let today = null, requestNo = 0, catalog = [], index = {}, busy = false;
let view = null;           // всё, что нарисовано сейчас: данные и модели
let shiftTab = 'all', focusKey = null, allIssues = false;
const expanded = new Set();

// Особые операции журнала: не расход из кассы, а резерв или подотчёт.
const special = {
  reserve_dividends_transfer: {account: 'dividends', kind: 'transfer', label: 'Отложить в сейф'},
  reserve_dividends_withdrawal: {account: 'dividends', kind: 'withdrawal', label: 'Выдать собственнику из сейфа'},
  reserve_usd_deposit: {account: 'usd', kind: 'deposit', label: 'Поступили реальные USD'},
  reserve_usd_withdrawal: {account: 'usd', kind: 'withdrawal', label: 'Выданы реальные USD'},
};
const incomeCodes = ['income_other'];

const STATUS = {
  on_time: ['Вовремя', 'on_time'], late: ['Опоздал', 'late'], missing: ['Не пришёл', 'missing'],
  manual_present: ['Был · вручную', 'manual_present'], manual_absent: ['Не был · вручную', 'manual_absent'],
  unlinked: ['Нет привязки', 'unlinked'], unavailable: ['Нет данных', 'unlinked'],
};

/* ── мелочи ─────────────────────────────────────────────────────────── */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) if (child !== null && child !== undefined && child !== false)
    el.append(child instanceof Node ? child : String(child));
  return el;
}
const parse = value => Number(String(value || '').replace(/[\s  ]/g, '').replace(',', '.')) || 0;
function longDay(iso, weekday) {
  return new Intl.DateTimeFormat('ru-RU', {...(weekday ? {weekday: 'short'} : {}), day: 'numeric', month: 'long', timeZone: 'UTC'})
    .format(new Date(iso + 'T12:00:00Z'));
}
function message(text, error = false) {
  const box = $('accountant-message');
  box.textContent = text; box.hidden = !text; box.classList.toggle('is-error', error);
  box.setAttribute('role', error ? 'alert' : 'status');
  if (text) globalThis.RetroToast?.show(text, error ? 'error' : 'ok');
}
function status(text) { const box = $('connection'); if (box) box.textContent = text; }
const selectedDay = () => $('accountant-date').value;
// Системные окна подтверждения переводчик страницы не видит: переводим сами.
const tr = text => (document.documentElement.lang === 'uz' && globalThis.RetroI18n ? RetroI18n.translate(text) || text : text);
const ask = text => confirm(tr(text));

function attendanceHealth(value) {
  const s = value?.status || 'starting';
  const states = {
    ok: 'Hikvision синхронизирован.',
    starting: 'Hikvision подключается; отсутствие входа пока не считается прогулом.',
    stale: 'Данные Hikvision устарели; отсутствие входа не считается прогулом.',
    not_configured: 'Hikvision не настроен; отсутствие входа не считается прогулом.',
  };
  return {ok: s === 'ok', text: states[s] || 'Hikvision недоступен; отсутствие входа не считается прогулом.'};
}

async function write(url, body, method = 'POST') {
  const response = await RetroFinancialWrite(url, {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  let result = {};
  try { result = await response.json(); } catch { /* 204 */ }
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось сохранить. Проверьте поля.');
  return result;
}
async function remove(operation, id, day) {
  const response = await fetch('/api/accountant/operations/' + encodeURIComponent(operation) + '/' + id + '?date=' + encodeURIComponent(day), {method: 'DELETE'});
  if (!response.ok) { let detail = 'Не удалось удалить операцию.'; try { detail = (await response.json()).detail || detail; } catch { /* нет тела */ } throw new Error(detail); }
}
/* Запись и перечитывание дня. fb — кто начал действие и где показать отклик
   (busy.js): {button} — спиннер и ✓ на кнопке, {field} — «сохраняю…» в поле,
   {row} — строка в работе, {rows} — несколько строк, {collapse} — удалённая
   строка сворачивается. Пока идёт перечитывание, день слегка гаснет. */
const B = globalThis.RetroBusy;
// Ключ строки смены для busy.js: row.key меняется с «e<сотрудник>» на
// «a<начисление>», когда смену подтверждают, — берём сотрудника и день.
const stable = row => (row.employeeId ?? row.key) + '@' + row.day;
const dayKey = suffix => (view ? view.data.date : selectedDay()) + ':' + suffix;
async function run(action, success, fb = {}) {
  if (busy) return false;
  busy = true; document.body.classList.add('fd-busy');
  // Новое действие — старые красные поля больше не про текущие данные.
  B?.clear('', 'error');
  let work = Promise.resolve().then(action);
  globalThis.RetroSave?.track(work);
  if (B) {
    (fb.rows || []).forEach(line => B.row(line, work).catch(() => {}));
    // Поле само подсвечивает свою строку по итогу; строку «в работе» ставим
    // только действиям кнопками.
    if (fb.field) work = B.field(fb.field, work, {row: fb.row, restore: fb.restore});
    else if (fb.row) work = B.row(fb.row, work, {collapse: fb.collapse});
    if (fb.button) work = B.button(fb.button, work, {done: !fb.collapse});
  }
  let failure = null;
  try { await work; } catch (error) { failure = error; }
  // Запись прошла, а перечитать не вышло — это покажет loadDay; форму всё
  // равно очищаем, иначе повтор записал бы деньги второй раз.
  try { await reload(); } catch { /* сообщение уже выведено */ }
  busy = false; document.body.classList.remove('fd-busy'); renderAll();
  if (failure) { message(failure.message, true); return false; }
  if (success) message(success);
  return true;
}
const reload = () => (B ? B.section($('finance-layout'), loadDay()) : loadDay());

/* ── секции сворачиваются; состояние помнит браузер ───────────────────── */
function sectionState(name) { try { return localStorage.getItem('fd-sec:' + name) !== '0'; } catch { return true; } }
function setSection(name, open) {
  const button = document.querySelector('.fd-sec-toggle[data-section="' + name + '"]');
  const section = button.closest('.fd-sec');
  section.classList.toggle('is-closed', !open);
  button.setAttribute('aria-expanded', String(open));
  try { localStorage.setItem('fd-sec:' + name, open ? '1' : '0'); } catch { /* приватный режим */ }
}
document.querySelectorAll('.fd-sec-toggle').forEach(button => {
  setSection(button.dataset.section, sectionState(button.dataset.section));
  button.addEventListener('click', () => setSection(button.dataset.section, button.getAttribute('aria-expanded') !== 'true'));
});

/* ── Смена ──────────────────────────────────────────────────────────── */
function noteStrip(text, tag, action) {
  return h('div', {class: 'fd-strip is-block'}, h('span', {class: 'fd-strip-tag', text: tag}),
    h('span', {class: 'fd-strip-text', text}), action || null);
}

// Запрет на весь день бывает только в двух случаях: нет данных кассира или
// сервер отказал в начислении всей смены (за день уже есть зарплата без
// сотрудника). Остальные причины — у отдельных строк, их видно в строке.
const dayBlocks = {};
// Режим проверки (ACCOUNTANT_CHECK_MODE): сервер пускает выдачу без
// данных кассира, поэтому экран не должен запирать её раньше сервера.
let checkMode = false;
const cashMissing = () => view.data.ledger.cash_balance === null && !checkMode;
function payDisabledReason() {
  const {data, board} = view;
  if (cashMissing())
    return 'Нет данных кассира за ' + longDay(data.date) + ' — выдачу записать нельзя.';
  if (dayBlocks[board.S] && !board.confirmed) return dayBlocks[board.S];
  return null;
}
const BLOCK_TEXT = {
  rate: 'Нет ставки — смена не начисляется',
  unlinked: 'Нет привязки к Hikvision — вход не виден',
  hikvision: 'Входы Hikvision за этот день ещё не пришли',
  hikvision_gap: 'Данных Hikvision за этот день нет — отметьте «был / не был»',
  unknown: 'Начисление не рассчитано',
};
/* Выгрузка Hikvision идёт только вперёд и назад не достраивается: за день
   раньше её начала входов не будет никогда. Ждать нечего — такую смену
   отмечают руками, и текст блокировки обязан это говорить. */
function hikvisionGap() {
  const from = (view.staff?.attendance || view.data.attendance)?.covered_from;
  return !!from && view.board.S < from.slice(0, 10);
}
const blockText = row => BLOCK_TEXT[row.block === 'hikvision' && hikvisionGap() ? 'hikvision_gap' : row.block];
function rowLock(row) {
  if (cashMissing()) return payDisabledReason();
  if (row.block) return blockText(row);
  if (row.own && !row.accrualId && dayBlocks[view.board.S]) return dayBlocks[view.board.S];
  return null;
}
// «Выдать пришедшим»: своя смена, кому ещё ничего не выдано (частичных и
// прошлые смены выдают строкой).
const payable = () => view.board.handOut.filter(row => !rowLock(row));

function renderShift() {
  const {board, staff, data} = view;
  $('shift-title').textContent = 'Смена ' + longDay(board.S);
  const lock = payDisabledReason();

  /* Полосы-сводки сняты по решению PM (T-397): три баннера над таблицей
     отодвигали саму смену вниз. Счётчики «без ставки» и «без привязки
     Hikvision» никуда не делись — они в правой карточке «Проверки», а причина
     по каждому человеку написана в его же строке. Остаются только полосы,
     которые объясняют неработающие кнопки: состояние Hikvision и запрет
     выдачи. Их молчание и было багом T-393. */
  const strips = $('shift-strips'); strips.replaceChildren();
  const health = attendanceHealth(staff?.attendance || data.attendance);
  if (!health.ok) strips.append(noteStrip(health.text, 'Hikvision'));
  // Прихода нет — его подтверждают на «Финансах дня».
  if (lock) strips.append(noteStrip(lock, 'Выдача закрыта',
    data.ledger.cash_balance === null ? h('a', {class: 'fd-strip-btn', href: '/accountant?date=' + data.date, text: 'Подтвердить приход →'}) : null));

  const tabs = $('shift-tabs'); tabs.replaceChildren();
  L.boardTabs(board.rows).forEach(([key, label, count]) => {
    tabs.append(h('button', {type: 'button', class: 'fd-tab' + (shiftTab === key ? ' is-active' : '') + (key === 'err' ? ' is-err' : ''),
      'aria-pressed': String(shiftTab === key), onclick: () => { shiftTab = key; renderShift(); }}, label, h('small', {text: String(count)})));
  });

  const box = $('shift-rows'); box.replaceChildren();
  const shown = board.rows.filter(r => L.boardMatch(r, shiftTab));
  shown.forEach(row => box.append(shiftRow(row, rowLock(row))));
  if (!shown.length) box.append(h('div', {class: 'fd-empty', text: board.rows.length ? 'В этом срезе никого нет.' : 'Реестр смены пуст.'}));

  const t = board.totals;
  $('shift-accrued').textContent = fmt(t.accrued);
  $('shift-paid').textContent = fmt(t.paid);
  $('shift-paid-count').textContent = 'выдано ' + t.paidCount + ' из ' + t.payableCount;
  const errors = board.rows.filter(r => r.kind === 'err' || r.kind === 'warn').length;
  $('shift-sum').textContent = 'выдано ' + t.paidCount + ' · ' + money(t.paid) + (errors ? ' · ' + errors + ' ' + L.plural(errors, 'ошибка', 'ошибки', 'ошибок') : '');

  const n = payable().length;
  // «Все получили» — только когда было что получать; выдача закрыта (нет кассы) —
  // не «все получили», а «нельзя»: причина в подсказке.
  const waiting = board.handOut.length;
  $('pay-all-label').textContent = n ? 'Выдать пришедшим · ' + n : lock && waiting ? 'Выдать пришедшим · ' + waiting
    : board.toPay.length ? 'Выдать пришедшим' : t.payableCount ? 'Все пришедшие получили' : 'Выдавать пока некому';
  $('pay-all').disabled = !n || busy;
  $('pay-all').title = n ? 'Выдать ставку всем пришедшим, кому ещё ничего не выдано'
    : lock || (board.toPay.length ? 'Всем пришедшим уже выдано. Остаток частичных выдач и долги прошлых смен — в их строках.' : 'Всем пришедшим уже выдано.');
}

function shiftRow(row, lock) {
  const flagged = focusKey === 'row:' + row.key || (focusKey === 'todo' && row.kind === 'todo' && row.own);
  const line = h('div', {class: 'fd-row fd-shift-cols is-' + row.kind + (flagged ? ' is-focus' : ''), 'data-key': row.key,
    'data-busy-key': 'shift:' + dayKey(stable(row))});

  const on = row.paid > 0;
  const canPay = !lock && row.accrued > 0 && row.debt > 0;
  const canUndo = !lock && row.paidToday > 0;
  const active = canPay || canUndo;
  // Причина запрета — на самой кнопке. Выключенная кнопка не ловит наведение,
  // поэтому держим её живой через aria-disabled: подсказка видна, а клик
  // вместо тишины отвечает, почему выдать нельзя.
  const why = lock || (row.accrued === 0 ? 'Входа нет — начисление 0 сум, выдавать нечего' : 'Выдавать по этой строке нечего');
  const check = h('button', {type: 'button', class: 'fd-cb' + (on ? ' is-on' : '') + (row.kind === 'err' ? ' is-err' : '') + (active ? '' : ' is-locked'),
    title: active ? (on ? 'Выдано' : 'Отметить выдачу') : why,
    'aria-label': (on ? 'Выдано: ' : 'Отметить выдачу: ') + row.name,
    'aria-disabled': String(!active), 'aria-pressed': String(on), text: on ? '✓' : '',
    'data-busy-key': 'cb:' + dayKey(stable(row))});
  check.addEventListener('click', () => {
    if (!active) { message(why, true); return; }
    const fb = {button: check, row: line};
    if (row.debt > 0 && row.accrued > 0) setPaid(row, row.paidToday + row.debt, null, fb);
    else if (row.paidToday > 0) setPaid(row, 0, null, fb);
  });

  const who = h('div', {class: 'fd-who'},
    h('div', {class: 'fd-who-line'}, h('span', {class: 'fd-who-name', text: row.name}),
      row.noHik ? h('span', {class: 'fd-nohik', title: 'Не зарегистрирован в Hikvision', text: '⊘ без Hikvision'}) : null),
    h('div', {class: 'fd-who-role'}, h('span', {text: row.role}), row.own ? null : h('span', {text: ' · смена ' + dm(row.day)})));

  const time = h('div', {class: 'fd-time'});
  if (row.own) {
    time.append(h('div', {class: 'fd-time-main' + (row.status === 'late' ? ' is-late' : row.time ? '' : ' is-none'), text: row.time || '—'}));
    if (row.noHik) time.append(h('div', {class: 'fd-time-sub is-nohik', text: 'нет данных'}));
    else if (row.late) time.append(h('div', {class: 'fd-time-sub', text: '+' + row.late + ' мин'}));
  } else time.append(h('div', {class: 'fd-time-main is-none', text: '—'}));

  const [label, cls] = STATUS[row.status] || ['—', 'unlinked'];
  // День раньше начала выгрузки отмечают так же, как человека без Hikvision:
  // входов не будет, и без отметки строка не начислится никогда. Пока выгрузка
  // просто отстаёт, отметку не предлагаем — данные ещё придут.
  const markable = row.manualAttendance || (row.status === 'unavailable' && hikvisionGap());
  const toggleable = markable && row.own && !row.accrualId;
  const pill = h(toggleable ? 'button' : 'span', {class: 'fd-pill ' + cls + (row.manualAttendance ? ' is-manual' : '') + (toggleable ? ' is-toggle' : ''),
    title: toggleable ? (row.manualAttendance ? 'Нет в Hikvision. Нажмите, чтобы отметить: был / не был'
        : 'Данных Hikvision за этот день нет. Нажмите, чтобы отметить: был / не был')
      : row.manualAttendance ? 'Отмечено вручную · смена уже начислена, отметку не изменить'
      : row.noHik ? 'Не зарегистрирован в Hikvision' : 'Данные Hikvision',
    type: toggleable ? 'button' : null, text: label, 'data-busy-key': toggleable ? 'pill:' + dayKey(stable(row)) : null});
  if (toggleable) pill.addEventListener('click', () => run(() => write('/api/accountant/manual-attendance',
    {date: view.board.S, employee_id: row.employeeId, present: row.status !== 'manual_present'}),
    row.name + ': ' + (row.status === 'manual_present' ? 'не был' : 'был'), {button: pill, row: line}));
  const statusCell = h('div', {class: 'fd-status'}, pill);

  const accrued = h('div', {class: 'fd-acc num' + (row.accrued ? '' : ' is-zero')},
    h('span', {class: 'fd-m-label', text: 'Начислено '}),
    // У заблокированной строки причина уже написана под кнопкой исправления.
    row.accrued === null ? (row.block === 'rate' ? 'Нет ставки' : '—') : fmt(row.accrued));

  const input = h('input', {class: 'fd-pay-input', inputmode: 'numeric', placeholder: '—', autocomplete: 'off',
    'aria-label': 'Выдано сегодня · ' + row.name, 'data-busy-key': 'pay:' + dayKey(stable(row))});
  input.value = row.paidToday ? fmt(row.paidToday) : '';
  input.disabled = !!lock || !(row.accrued > 0 || row.paidToday > 0);
  if (lock) input.title = lock;
  else if (row.accrued === 0 && !row.paidToday) input.title = 'Входа нет — начисление 0 сум';
  // Отклонённое значение в поле не остаётся: ошибка — в сообщении, а поле
  // возвращается к сохранённой выдаче (и «несохранённый» набор busy.js тоже
  // снимаем, иначе перерисовка вернула бы 999 999 рядом с ✓).
  const restore = () => {
    B?.clear(input.dataset.busyKey);
    input.classList.remove('is-bad');
    input.value = row.paidToday ? fmt(row.paidToday) : '';
  };
  const commit = () => {
    const raw = input.value.trim();
    // «abc» — не ноль: без проверки такая опечатка снимала бы выдачу.
    if (raw && !/^[\d\s\u00a0\u202f.,]+$/.test(raw)) {
      message('Сумма — только цифрами: ' + raw, true); restore(); return;
    }
    const value = parse(raw);
    // Больше долга сервер не примет — говорим сразу и сколько можно.
    const most = row.paidToday + Math.max(0, row.debt);
    if (value > most) {
      message('Больше долга: ' + row.name + ' можно выдать ещё ' + money(Math.max(0, row.debt)) + ' (итого за сегодня до ' + money(most) + ').', true);
      restore(); return;
    }
    if (value === row.paidToday) { input.value = row.paidToday ? fmt(row.paidToday) : ''; return; }
    setPaid(row, value, input, {field: input, row: line});
  };
  input.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); input.blur(); } });
  input.addEventListener('change', commit);
  let note = row.note;
  if (!note && row.paidBefore > 0) note = 'раньше выдано ' + fmt(row.paidBefore);
  const pay = row.block ? blockedCell(row, line) : h('div', {class: 'fd-pay'}, input, h('div', {class: 'fd-pay-note is-' + row.kind, text: note}));
  line.append(check, who, h('div', {class: 'fd-meta'}, time, statusCell), accrued, pay);
  return line;
}

/* Строка, которую не начислить: своя причина и прямое исправление. После
   исправления перечитываем день — строка становится к выдаче сама. */
function blockedCell(row, line) {
  const cell = h('div', {class: 'fd-pay fd-fix'});
  const note = h('div', {class: 'fd-pay-note is-blocked', text: blockText(row)});
  if (row.block === 'rate') {
    const open = h('button', {type: 'button', class: 'fd-fix-btn', text: 'Указать ставку'});
    open.addEventListener('click', () => {
      const input = h('input', {class: 'fd-pay-input fd-money', inputmode: 'numeric', placeholder: 'Ставка, сум',
        'aria-label': 'Ставка за смену · ' + row.name, 'data-money-hint': 'off', autocomplete: 'off',
        'data-busy-key': 'rate:' + dayKey(stable(row))});
      const ok = h('button', {type: 'button', class: 'fd-debt-ok', title: 'Сохранить ставку', 'aria-label': 'Сохранить ставку', text: '✓'});
      const save = () => {
        const rate = parse(input.value);
        if (!rate) { message('Укажите ставку за смену.', true); input.focus(); return; }
        run(() => write('/api/accountant/employees/' + row.employeeId,
          // Пустая ставка заполняется сервером и за прошлые дни без ставки —
          // вчерашняя строка после этого начисляется.
          {rate: String(rate), reason: 'Ставка указана в «Финансах дня»'}, 'PATCH'), 'Ставка сохранена: ' + row.name,
          {field: input, row: line});
      };
      ok.addEventListener('click', save);
      input.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); save(); } if (event.key === 'Escape') renderShift(); });
      cell.replaceChildren(h('span', {class: 'fd-debt-edit'}, input, ok), note);
      input.focus();
    });
    cell.append(open, note);
  } else if (row.block === 'unlinked') {
    const manual = h('button', {type: 'button', class: 'fd-fix-btn', text: 'Отмечать вручную', 'data-busy-key': 'manual:' + dayKey(stable(row))});
    manual.addEventListener('click', () => run(() => write('/api/accountant/employees/' + row.employeeId,
      {rate: row.rate === null ? null : String(row.rate), reason: 'Нет привязки Hikvision — присутствие отмечается вручную', manual_attendance: true}, 'PATCH'),
      row.name + ': присутствие отмечается вручную', {button: manual, row: line}));
    cell.append(manual, note);
  } else cell.append(note);
  return cell;
}

async function confirmShift(employeeIds) {
  if (view.board.confirmed) return;
  const reason = payDisabledReason();
  if (reason) throw new Error(reason);
  let approver = 'бухгалтер';
  try { const config = await globalThis.RetroConfig; if (config?.user) approver = config.user; } catch { /* по умолчанию */ }
  // Подтверждение частичное и повторяемое: сервер начисляет тех, кого можно
  // посчитать, в том числе тех, кого только что исправили.
  const body = {date: view.board.S, approver};
  if (employeeIds) body.employee_ids = employeeIds;
  try { await write('/api/accountant/payroll/confirm', body); }
  catch (error) {
    if (/зарплата без сотрудника/.test(error.message)) dayBlocks[view.board.S] = error.message;
    throw error;
  }
  await loadDay();
}
function accrualFor(row) {
  if (row.accrualId) return view.board.rows.find(r => r.accrualId === row.accrualId);
  return view.board.rows.find(r => r.employeeId === row.employeeId && r.day === row.day && r.accrualId);
}
async function payAccrual(accrualId, amount) {
  await write('/api/accountant/salary-payments', {date: view.data.date, accrual_id: Number(accrualId), amount: String(amount)});
}
// Поле «Выдано сегодня» — итог за сегодня. Больше прежнего — доплачиваем
// разницу; меньше — снимаем сегодняшние выплаты и записываем новую сумму.
function setPaid(row, target, input, fb = {}) {
  if (target < row.paidToday && !ask('Изменить выдачу ' + row.name + ': ' + money(row.paidToday) + ' → ' + money(target) + '?')) {
    if (input) input.value = row.paidToday ? fmt(row.paidToday) : '';
    return;
  }
  run(async () => {
    // Одна строка — начисляем только её; «Выдать пришедшим» начисляет всех.
    if (!row.accrualId) await confirmShift([row.employeeId]);
    const fresh = accrualFor(row);
    if (!fresh) throw new Error('Начисление не найдено — обновите страницу.');
    if (target < fresh.paidToday) {
      for (const payment of fresh.todayPayments) await remove('salary_payment', payment.id, view.data.date);
      if (target > 0) await payAccrual(fresh.accrualId, target);
    } else if (target > fresh.paidToday) {
      await payAccrual(fresh.accrualId, target - fresh.paidToday);
    }
  // restore: сервер отказал — ошибка остаётся (сообщение и рамка поля), а в поле
  // после перерисовки то, что реально сохранено, а не отклонённая сумма.
  }, target ? 'Выдано: ' + row.name + ' · ' + money(target) : 'Выдача снята: ' + row.name, {...fb, restore: true});
}
on('pay-all', 'click', () => {
  if (!view || busy) return;
  const rows = payable();
  if (!rows.length) return;
  const total = rows.reduce((s, r) => s + r.debt, 0);
  if (!ask('Выдать ' + rows.length + ' ' + L.plural(rows.length, 'сотруднику', 'сотрудникам', 'сотрудникам') + ' ' + money(total) + '?')) return;
  let done = 0;
  const lines = rows.map(r => document.querySelector('[data-busy-key="' + CSS.escape('shift:' + dayKey(stable(r))) + '"]')).filter(Boolean);
  run(async () => {
    const keys = rows.map(r => ({employeeId: r.employeeId, day: r.day, accrualId: r.accrualId}));
    if (rows.some(r => !r.accrualId)) await confirmShift();
    for (const key of keys) {
      const fresh = accrualFor(key);
      if (fresh && fresh.debt > 0) { await payAccrual(fresh.accrualId, fresh.debt); done += 1; }
    }
  }, null, {button: $('pay-all'), rows: lines}).then(ok => { if (ok) message('Выдано ' + done + ' ' + L.plural(done, 'сотруднику', 'сотрудникам', 'сотрудникам') + '.'); });
});

/* ── Журнал ─────────────────────────────────────────────────────────── */
function renderJournal() {
  const {journal} = view;
  const box = $('finance-journal'); box.replaceChildren();
  const cash = !cashMissing();
  journal.rows.forEach(row => {
    const rowKey = 'jr:' + dayKey(row.debtId || row.group || (row.ops || []).map(op => op.operation + op.id).join(',') || row.name);
    const line = h('div', {class: 'fd-row fd-jr-cols fd-jr-row is-' + row.kind, 'data-busy-key': rowKey});
    const cat = h('span', {class: 'fd-jr-cat', text: row.cat});
    const name = h('span', {class: 'fd-jr-name', title: row.title || null}, h('span', {}, h('span', {text: row.name}), row.note ? h('span', {text: ' · ' + row.note}) : null));
    if (row.kind === 'auto') name.append(h('span', {class: 'fd-auto', title: 'Создано автоматически', text: 'Авто'}));
    if (row.kind === 'carried') name.append(h('span', {class: 'fd-carried', text: 'долг с ' + dm(row.since)}));
    const amount = h('span', {class: 'num fd-jr-amount'}, h('span', {class: 'fd-m-label', text: 'Сумма '}),
      (row.kind === 'income' ? '+' : '') + fmt(row.amount) + (row.unit === 'USD' ? ' USD' : ''));
    const paid = h('span', {class: 'num fd-jr-paid'}, h('span', {class: 'fd-m-label', text: 'Оплачено '}), row.paid === null ? '—' : fmt(row.paid));
    const debt = h('span', {class: 'num fd-jr-debt'}, h('span', {class: 'fd-m-label', text: 'Долг '}));
    if (row.debt > 0 && row.debtId) {
      const button = h('button', {type: 'button', class: 'fd-debt-btn', title: 'Оплатить долг', text: fmt(row.debt)});
      button.disabled = !cash;
      button.addEventListener('click', () => debtEditor(debt, row, line));
      debt.append(button);
      line.classList.add('has-debt');
    } else debt.append(h('span', {class: 'fd-dash', text: '—'}));
    const x = h('span', {class: 'fd-x-cell'});
    if (row.ops && row.ops.length) {
      x.append(h('button', {type: 'button', class: 'fd-x', 'aria-label': 'Удалить строку', title: 'Удалить', text: '×', onclick: event => {
        const text = row.kind === 'debt' ? 'Удалить запись целиком: долг ' + money(row.amount) + (row.paid ? ' и оплату этого дня ' + money(row.paid) : '') + '?' : 'Удалить эту операцию?';
        if (!ask(text)) return;
        run(async () => { for (const op of row.ops) await remove(op.operation, op.id, view.data.date); }, 'Операция удалена.',
          {button: event.currentTarget, row: line, collapse: true});
      }}));
    }
    else if (row.kind === 'reserve') x.title = 'Операцию сейфа не удалить: исправьте встречной записью';
    else if (row.kind === 'auto') x.title = 'Авто-строка: удаляют сами выплаты';
    if (row.children) {
      line.classList.add('is-expandable');
      const open = expanded.has(row.group);
      name.prepend(h('button', {type: 'button', class: 'fd-expand' + (open ? ' is-open' : ''), 'aria-expanded': String(open),
        'aria-label': 'Показать выплаты', text: '›', onclick: () => { open ? expanded.delete(row.group) : expanded.add(row.group); renderJournal(); }}));
    }
    line.append(cat, name, amount, paid, debt, x);
    box.append(line);
    if (row.children && expanded.has(row.group)) row.children.forEach(child => {
      const childLine = h('div', {class: 'fd-row fd-jr-cols fd-jr-child', 'data-busy-key': 'jrc:' + dayKey(child.id)});
      box.append(childLine);
      childLine.append(h('span', {class: 'fd-jr-cat'}), h('span', {class: 'fd-jr-name', text: child.name}),
        h('span', {class: 'num fd-jr-amount', text: fmt(child.amount)}), h('span', {class: 'num fd-jr-paid', text: fmt(child.amount)}), h('span', {class: 'num fd-jr-debt fd-dash', text: '—'}),
        h('span', {class: 'fd-x-cell'}, h('button', {type: 'button', class: 'fd-x', 'aria-label': 'Удалить выплату', title: 'Удалить выплату', text: '×', onclick: event => {
          if (!ask('Удалить выплату ' + child.name + ' · ' + money(child.amount) + '?')) return;
          run(() => remove('salary_payment', child.id, view.data.date), 'Выплата удалена.', {button: event.currentTarget, row: childLine, collapse: true});
        }})));
    });
  });
  if (!journal.rows.length) box.append(h('div', {class: 'fd-empty', text: 'За этот день операций ещё нет.'}));
  $('journal-total').textContent = money(journal.total);
  const n = journal.rows.filter(r => r.kind !== 'carried').length;
  $('journal-sum').textContent = n + ' ' + L.plural(n, 'операция', 'операции', 'операций') + ' · ' + money(journal.total);
  updateNewRow();
}
function debtEditor(cell, row, line) {
  const input = h('input', {class: 'fd-debt-input', inputmode: 'numeric', 'aria-label': 'Оплатить долг', autocomplete: 'off',
    'data-busy-key': 'debt:' + dayKey(row.debtId)});
  input.value = fmt(row.debt);
  const ok = h('button', {type: 'button', class: 'fd-debt-ok', title: 'Записать оплату', 'aria-label': 'Записать оплату', text: '✓'});
  const save = () => {
    const value = parse(input.value);
    if (!value) { renderJournal(); return; }
    if (value > row.debt) { message('Оплата больше долга: осталось ' + money(row.debt) + '.', true); return; }
    run(() => write('/api/accountant/debts/pay', {date: view.data.date, debt_id: row.debtId, amount: String(value)}), 'Оплата долга записана.',
      {field: input, row: line});
  };
  ok.addEventListener('click', save);
  input.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); save(); } if (event.key === 'Escape') renderJournal(); });
  cell.replaceChildren(h('span', {class: 'fd-m-label', text: 'Оплатить '}), h('span', {class: 'fd-debt-edit'}, input, ok));
  input.focus(); input.select();
}

function fillCatalog() {
  const select = $('expense-item');
  const keep = select.value;
  select.replaceChildren(new Option('Категория', ''));
  catalog.forEach(group => {
    const items = group.items.filter(item => !['income_opening', 'income_cashier', 'salary_monthly'].includes(item.code));
    if (!items.length) return;
    const og = document.createElement('optgroup');
    og.label = tr(L.GROUP_SHORT[group.code] || group.label);
    // Дивиденды в сейф — строка категории «Дивиденды / переводы» (Функционал §3.7).
    if (group.code === 'distributions') og.append(new Option(special.reserve_dividends_transfer.label, 'reserve_dividends_transfer'));
    items.forEach(item => og.append(new Option(item.label, item.code)));
    select.append(og);
  });
  const og = document.createElement('optgroup'); og.label = tr('Резервы и сейф');
  Object.entries(special).filter(([code]) => code !== 'reserve_dividends_transfer').forEach(([code, item]) => og.append(new Option(item.label, code)));
  select.append(og);
  if (keep) select.value = keep;
}
function updateNewRow() {
  const code = $('expense-item').value, reserve = special[code], income = incomeCodes.includes(code);
  const paidInput = $('expense-paid');
  paidInput.disabled = !!reserve || income;
  if (paidInput.disabled) paidInput.value = '';
  const amount = parse($('expense-amount').value), paidRaw = paidInput.value.trim(), paid = paidRaw === '' ? amount : parse(paidRaw);
  $('expense-debt').textContent = amount && paid < amount ? fmt(amount - paid) : '';
  paidInput.classList.toggle('is-bad', amount > 0 && paid > amount);
  const cashNeeded = !income && !(reserve && reserve.kind !== 'transfer') && paid > 0;
  $('other-expense-form').querySelector('button').disabled = busy || (cashNeeded && view && cashMissing());
}
['expense-item', 'expense-amount', 'expense-paid'].forEach(id => on(id, 'input', updateNewRow));
// Подписи групп в списке — атрибуты, их переводчик страницы не трогает:
// после смены языка пересобираем список сами.
document.querySelectorAll('[data-lang]').forEach(button => button.addEventListener('click', () => setTimeout(() => { if (catalog.length) fillCatalog(); }, 0)));
/* Строка нового расхода: «+» и общая «Сохранить» отправляют её одинаково.
   Возвращает, записалось ли (false — ошибка уже показана). */
function submitExpense(button) {
  if (!view) return Promise.resolve(false);
  const code = $('expense-item').value, note = $('expense-note').value.trim();
  const amount = parse($('expense-amount').value), paidRaw = $('expense-paid').value.trim();
  if (!code) { message('Выберите категорию расхода.', true); $('expense-item').focus(); return Promise.resolve(false); }
  if (!amount) { message('Укажите сумму.', true); $('expense-amount').focus(); return Promise.resolve(false); }
  if (!note) { message('Укажите, за что или кому.', true); $('expense-note').focus(); return Promise.resolve(false); }
  const paid = paidRaw === '' ? null : parse(paidRaw);
  if (paid !== null && paid > amount) { message('Оплачено больше суммы расхода.', true); return Promise.resolve(false); }
  const day = view.data.date, reserve = special[code];
  const request = reserve ? ['/api/accountant/reserves', {date: day, amount: String(amount), note, account: reserve.account, kind: reserve.kind}]
    : incomeCodes.includes(code) ? ['/api/accountant/incomes', {date: day, item_code: code, note, amount: String(amount)}]
    : ['/api/accountant/expenses', {date: day, item_code: code, note, amount: String(amount), paid_amount: paid === null ? null : String(paid)}];
  return run(() => write(...request), paid !== null && paid < amount ? 'Записано. Неоплаченная часть ушла в долги.' : 'Записано. Остаток пересчитан.',
    {button: button || $('other-expense-form').querySelector('button[type=submit]')}).then(ok => {
    if (!ok) return false;
    ['expense-note', 'expense-amount', 'expense-paid'].forEach(id => { $(id).value = ''; });
    updateNewRow();
    $('expense-note').focus({preventScroll: true});
    return true;
  });
}
on('other-expense-form', 'submit', event => { event.preventDefault(); submitExpense(event.submitter); });
// Выбранная категория без суммы — ещё не данные: «несохранённым» считаем
// набранную сумму или текст.
globalThis.RetroSave?.register($('other-expense-form'), () => submitExpense(),
  {dirty: () => !!($('expense-amount')?.value.trim() || $('expense-note')?.value.trim() || $('expense-paid')?.value.trim())});

/* ── Дэшборд бухгалтера (правая колонка «Финансов дня») ───────────────── */
function railLine(label, value, cls) {
  return h('div', {class: cls}, h('span', {text: label}), h('b', {text: value}));
}
const flowOf = () => view.data.ledger.day_flow || {};
const handoverStatus = () => (view.data.ledger.cash_flow || {}).handover_status || 'none';
// Приход дня в остатке: подтверждён бухгалтером или записан им самим.
const handoverCounted = () => ['confirmed', 'accountant'].includes(handoverStatus());

/* Подтверждение суммы от кассира — обязательное (ТЗ 02.10). Поле «Получено»
   по умолчанию = переданное кассиром (или расчёт iiko). Пока сумма не
   подтверждена, она не входит в остаток, а отчёт дня не сдаётся. */
let confirmEditing = false, confirmDirty = false;
const confirmDefault = () => { const {cash} = view; return cash.cashier !== null ? cash.cashier : cash.expected; };
function renderCashConfirm(handover) {
  const {cash} = view, form = $('cash-confirm'), done = $('cash-confirmed');
  if (!form) return;
  const state = handoverStatus();
  const needs = !view.data.closed && (cash.changed || state === 'pending' || state === 'none');
  form.hidden = !(needs || confirmEditing);
  form.classList.toggle('is-changed', cash.changed);
  if (!form.hidden && !confirmDirty) { const value = confirmDefault(); $('cash-confirm-amount').value = value === null ? '' : fmt(value); }
  $('cash-confirm-note').textContent = cash.changed
    ? 'Касса изменилась после подтверждения: было ' + money(cash.confirmedCalc) + ', сейчас ' + money(cash.calculation) + ' — подтвердите снова.'
    : cash.confirmedAt ? 'Исправление: расчёт ' + money(cash.calculation) + '.'
    : state === 'accountant' ? 'Исправление записанного прихода ' + money(cash.cashier) + '.'
    : handover.source === 'cashier' ? 'Кассир передал ' + money(cash.cashier) + '. Пересчитайте и подтвердите — до этого сумма не в остатке.'
    : cash.cashier !== null ? 'По отчёту iiko ' + money(cash.cashier) + '. Пересчитайте и подтвердите — до этого сумма не в остатке.'
    : cash.expected !== null ? 'Кассир ещё не нажал «Передать» — по расчёту ' + money(cash.expected) + '.'
    : 'Кассир ещё не передал кассу. Впишите, сколько получено, и подтвердите.';
  $('cash-confirm-submit').disabled = busy;
  const manual = state === 'accountant' && !cash.confirmedAt;
  done.hidden = !(cash.confirmedAt || manual) || !form.hidden;
  done.classList.toggle('is-short', cash.shortfall > 0);
  done.replaceChildren(h('span', {text: cash.shortfall > 0
      ? '⚠ Получено на ' + money(cash.shortfall) + ' меньше расчёта (' + money(cash.calculation) + ')'
      : manual ? (cash.unchecked ? 'Приход записан бухгалтером · кассир в панели не работал — сверки нет' : 'Приход записан бухгалтером')
      : '✓ Сумма от кассира подтверждена · ' + cash.confirmedAt}),
    ...(view.data.closed ? [] : [' ', h('button', {type: 'button', class: 'fd-link-btn', text: 'Изменить', onclick: () => { confirmEditing = true; confirmDirty = false; renderCashConfirm(handover); $('cash-confirm-amount').focus(); }})]));
}
const confirmValid = raw => !!raw && /^[\d\s  .,]+$/.test(raw);
on('cash-confirm-amount', 'input', () => { confirmDirty = true; $('cash-confirm-amount').classList.remove('is-bad'); });
// Ушли из поля с неверной суммой — ошибка остаётся в сообщении, а в поле
// возвращается последнее верное значение. Нажатие «Подтвердить» — не уход.
let confirmPressing = false;
on('cash-confirm-submit', 'pointerdown', () => { confirmPressing = true; setTimeout(() => { confirmPressing = false; }, 800); });
on('cash-confirm-amount', 'blur', () => {
  const input = $('cash-confirm-amount');
  if (!view || confirmPressing || !input.value.trim() || confirmValid(input.value.trim())) return;
  message('Укажите полученную сумму цифрами.', true);
  confirmDirty = false; input.classList.remove('is-bad');
  const value = confirmDefault();
  input.value = value === null ? '' : fmt(value);
});
function confirmCash() {
  confirmPressing = false;
  if (!view) return Promise.resolve(false);
  const input = $('cash-confirm-amount'), raw = input.value.trim();
  if (!confirmValid(raw)) { message('Укажите полученную сумму цифрами.', true); input.classList.add('is-bad'); input.focus(); return Promise.resolve(false); }
  const amount = parse(raw), day = view.data.date;
  // Недостачу в сообщении берём из ответа сервера — то же число, что в
  // карточке, «Проверках», у учредителя и в Excel.
  let saved = null;
  return run(async () => { saved = await write('/api/accountant/handover/confirm', {date: day, amount: String(amount)}); }, null,
    {button: $('cash-confirm-submit')}).then(ok => {
    confirmDirty = false;
    if (!ok) { renderAll(); return false; }
    confirmEditing = false; renderAll();
    const short = Number(saved?.handover?.shortfall || 0);
    message('Получено от кассира: ' + money(amount) + '.' + (short > 0 ? ' Не хватает ' + money(short) + ' — это видно в проверках.' : ''));
    return true;
  });
}
on('cash-confirm', 'submit', event => { event.preventDefault(); confirmCash(); });
globalThis.RetroSave?.register($('cash-confirm'), () => confirmCash(), {dirty: () => confirmDirty});

/* «На начало дня» = конец вчерашнего (ТЗ 02.10, п. 1). По щелчку — из какого
   дня цифра пришла и из чего сложилась. */
let breakdownOpen = false;
const MONTH_NAMES = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];
const monthTitle = month => MONTH_NAMES[Number(month.slice(5, 7)) - 1] + ' ' + month.slice(0, 4);
const HANDOVER_TEXT = {confirmed: 'подтверждено', accountant: 'записал бухгалтер',
  pending: 'не подтверждено · в остаток не вошло', none: 'передачи нет'};
function renderBreakdown() {
  const box = $('fd-opening-breakdown');
  if (!box) return;
  box.hidden = !breakdownOpen;
  if (!breakdownOpen) return;
  const info = view.data.ledger.opening_breakdown || {}, prev = info.previous, anchor = info.anchor;
  box.replaceChildren();
  if (anchor) {
    const closed = String(anchor.kind || '').startsWith('closure:');
    box.append(h('p', {class: 'fd-bd-title', text: closed
      ? 'Остаток на конец закрытого месяца (' + monthTitle(anchor.kind.slice(8)) + ') — перенесён на 1-е число'
      : 'Начальный остаток бухгалтера на ' + longDay(view.data.date)}),
      railLine('= На начало дня', fmt(anchor.amount), 'fd-bd-line is-total'));
    return;
  }
  if (!prev || prev.opening === null) {
    box.append(h('p', {class: 'fd-bd-title', text: 'Вчера остаток не посчитан' + (prev && prev.missing ? ': нет передачи кассы за ' + dm(prev.missing) : '') + '.'}));
    return;
  }
  const line = (label, value, sign, extra) => railLine(label + (extra ? ' · ' + extra : ''), (sign || '') + fmt(value), 'fd-bd-line');
  box.append(h('p', {class: 'fd-bd-title', text: 'Из дня ' + longDay(prev.day, true) + ' — конец вчерашнего дня:'}),
    line('Начало ' + dm(prev.day), prev.opening),
    line('+ От кассира', prev.handover_counted, '', HANDOVER_TEXT[prev.handover_status]));
  if (prev.handover_status === 'pending') box.append(h('p', {class: 'fd-bd-warn'},
    'Передача кассира за ' + dm(prev.day) + ' — ' + money(prev.handover) + ' — не подтверждена и в остаток не вошла. ',
    h('button', {type: 'button', class: 'fd-link-btn', text: 'Открыть ' + dm(prev.day) + ' →', onclick: () => go(prev.day)})));
  if (Number(prev.receipts)) box.append(line('+ Прочие поступления', prev.receipts));
  [['− Сменным', prev.salary], ['− Оклады', prev.monthly], ['− Шоху на закуп', prev.shoh],
    ['− Прочие расходы', prev.other], ['− В сейф', prev.transfers]].forEach(([label, value]) => {
    if (Number(value)) box.append(line(label, value));
  });
  box.append(railLine('= Конец ' + dm(prev.day) + ' → начало сегодня', fmt(prev.closing), 'fd-bd-line is-total'),
    h('a', {class: 'fd-bd-link', href: '/api/accountant/reconciliation/export?month=' + prev.day.slice(0, 7), download: '',
      text: 'Сверка за месяц по дням · Excel ↓'}));
}

function renderRail() {
  if (!$('fd-cash')) return;
  const {cash, data} = view;
  const handover = data.cashier_handover || {}, flow = flowOf(), cf = data.ledger.cash_flow || {};
  const day = data.date, missing = 'нет данных', state = handoverStatus();
  $('finance-hero-title').textContent = day === today ? 'Касса сегодня' : 'Касса на конец ' + longDay(day);
  $('finance-cash-total').textContent = cash.end === null ? '—' : fmt(cash.end);
  $('finance-cash-total').classList.toggle('is-negative', cash.end !== null && cash.end < 0);
  const lines = $('fd-cash-lines'); lines.replaceChildren();
  const opening = h('button', {type: 'button', class: 'fd-cash-line fd-cash-open' + (breakdownOpen ? ' is-open' : ''), 'aria-expanded': String(breakdownOpen),
    title: 'Откуда эта цифра', onclick: () => { breakdownOpen = !breakdownOpen; renderRail(); }},
    h('span', {}, h('i', {class: 'fd-chev-sm', 'aria-hidden': 'true', text: '›'}), ' На начало дня'),
    h('b', {text: cash.opening === null ? missing : fmt(cash.opening)}));
  const shown = cash.cashier !== null ? cash.cashier : cash.expected;
  const counted = ['confirmed', 'accountant'].includes(state);
  lines.append(opening,
    railLine('+ От кассира · касса ' + dm(day) + (state === 'confirmed' ? ' · получено ' + (cash.confirmedAt || '')
      : state === 'accountant' ? ' · записано' : shown !== null ? ' · ожидается, не в остатке' : ' · ожидается'),
      counted ? fmt(flow.handover_counted) : shown !== null ? fmt(shown) : '—', 'fd-cash-line' + (counted ? '' : ' is-expected')));
  if (Number(flow.receipts)) lines.append(railLine('+ Прочие поступления', fmt(flow.receipts), 'fd-cash-line'));
  lines.append(railLine('− Зарплаты · сменные и оклады', fmt(Number(flow.salary || 0) + Number(flow.monthly || 0)), 'fd-cash-line'),
    railLine('− Выдано Шоху', fmt(flow.shoh), 'fd-cash-line'),
    railLine('− Прочие расходы' + (Number(flow.transfers) ? ' и сейф' : ''), fmt(Number(flow.other || 0) + Number(flow.transfers || 0)), 'fd-cash-line'));
  renderBreakdown();
  // Прогноз расхода на сегодня — по вчерашнему дню (ТЗ 02.10, п. 3).
  const prev = (data.ledger.opening_breakdown || {}).previous;
  const forecast = prev ? Number(prev.outflows || 0) : null;
  $('fd-forecast').textContent = forecast === null ? '—' : '≈ ' + money(forecast);
  $('fd-forecast-sub').textContent = forecast === null ? 'вчерашних данных нет'
    : 'как вчера, ' + dm(prev.day) + ' · уже ушло ' + fmt(flow.outflows);
  const pocket = data.shoh_pocket && data.shoh_pocket.pocket !== null && data.shoh_pocket.pocket !== undefined ? Number(data.shoh_pocket.pocket) : null;
  $('fd-shoh').textContent = pocket === null ? '—' : money(pocket);
  $('fd-cash-set').hidden = cash.opening !== null || !!data.closed;
  renderCashConfirm(handover);
  $('fd-cash').classList.toggle('is-focus', focusKey === 'cash');

  const shiftDebt = Number(data.ledger.salary_debt) + view.board.totals.unconfirmedDebt;
  const expDebt = Number(data.ledger.manual_debt_total);
  $('all-debt-total').textContent = money(shiftDebt + view.monthly.remain + expDebt);
  $('fd-debt-lines').replaceChildren(
    railLine('Сменные · не выдано', fmt(shiftDebt), 'fd-debt-line'),
    railLine('Оклады · остаток месяца', fmt(view.monthly.remain), 'fd-debt-line'),
    railLine('Расходы · не оплачено', fmt(expDebt), 'fd-debt-line'));
  const res = data.reserves;
  $('usd-balance').textContent = res.usd.balance === null ? 'не задан' : fmt(res.usd.balance) + ' USD';
  $('dividends-balance').textContent = res.dividends.balance === null ? 'не задан' : money(res.dividends.balance);
  renderChecks(); renderDividends();
}

function renderChecks() {
  const box = $('finance-checks');
  if (!box) return;
  const issues = view.issues; box.replaceChildren();
  const state = L.checksBadge(issues), badge = $('checks-badge');
  badge.textContent = state.text;
  badge.classList.toggle('is-clean', state.tone === 'clean');
  badge.classList.toggle('is-warn', state.tone === 'warn');
  if (!issues.length) { box.append(h('p', {class: 'fd-checks-ok', text: 'Ошибок не найдено.'})); return; }
  (allIssues ? issues : issues.slice(0, 5)).forEach(issue => box.append(h('button', {type: 'button', class: 'fd-check', onclick: () => focusOn(issue.target)},
    h('span', {class: 'fd-dot is-' + issue.lvl}),
    h('span', {class: 'fd-check-body'}, h('span', {class: 'fd-check-text', text: issue.text}), issue.sub ? h('span', {class: 'fd-check-sub', text: issue.sub}) : null))));
  if (issues.length > 5) box.append(h('button', {type: 'button', class: 'fd-check-more', text: allIssues ? 'Свернуть' : 'Показать все · ' + issues.length,
    onclick: () => { allIssues = !allIssues; renderChecks(); }}));
}
// Щелчок по проверке: смены — на «Зарплате · день», оклады — на «Зарплате ·
// месяц», Шох — на «Балансе Шохруха»; своё — подсвечиваем здесь.
function focusOn(target) {
  if (!target) return;
  const day = view ? view.data.date : selectedDay();
  const shift = target.startsWith('row:') || target === 'todo' || target === 'shift' || target === 'blocked';
  if (shift && PAGE !== 'salary-day') { location.href = '/accountant/salary-day?date=' + day; return; }
  if (target.startsWith('month:')) { location.href = '/accountant/payroll?employee=' + target.slice(6); return; }
  if (target.startsWith('buy:') || target === 'shoh') { location.href = '/accountant/shoh?date=' + day; return; }
  focusKey = target;
  if (shift) {
    setSection('shift', true);
    if (target.startsWith('row:') || target === 'todo') shiftTab = 'all';
    if (target === 'blocked') shiftTab = 'blocked';
  }
  renderAll();
  const el = target.startsWith('row:') ? document.querySelector('[data-key="' + target.slice(4) + '"]')
    : target === 'todo' ? document.querySelector('.fd-row.is-focus')
    : target === 'cash' ? $('fd-cash') : target === 'dividends' ? $('dividends-card')
    : target === 'shift' ? $('shift-section')
    : target === 'blocked' ? document.querySelector('#shift-rows .fd-row.is-blocked') : null;
  if (target === 'blocked') document.querySelectorAll('#shift-rows .fd-row.is-blocked').forEach(n => n.classList.add('is-focus'));
  if (el) el.scrollIntoView({block: 'center', behavior: 'smooth'});
  setTimeout(() => { if (focusKey === target) { focusKey = null; document.querySelectorAll('.is-focus').forEach(n => n.classList.remove('is-focus')); } }, 4000);
}

function renderDividends() {
  const week = view.data.dividends_week, card = $('dividends-card');
  card.hidden = !week;
  if (!week) return;
  card.classList.toggle('is-focus', focusKey === 'dividends');
  const target = week.target === null ? null : Number(week.target), set = Number(week.collected);
  $('dividends-week').textContent = dm(week.start) + '–' + dm(week.end);
  $('dividends-target').textContent = target === null ? 'не задано' : money(target);
  const source = week.target_source;
  const when = source && source.changed_at ? new Date(source.changed_at) : null;
  const stamp = when ? new Intl.DateTimeFormat('ru-RU', {day: '2-digit', month: '2-digit', timeZone: 'Asia/Tashkent'}).format(when)
    + ' в ' + new Intl.DateTimeFormat('ru-RU', {hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Tashkent'}).format(when) : '';
  $('dividends-changed').textContent = !source ? 'Учредитель ещё не поставил сумму на неделю.' : source.inherited ? 'как на прошлой неделе' : 'изменено основателем ' + stamp;
  $('dividends-fill').style.width = target ? Math.min(100, set / target * 100) + '%' : '0%';
  $('dividends-pace').hidden = !target;
  $('dividends-pace').style.left = target ? Math.min(100, Number(week.pace) / target * 100) + '%' : '0';
  $('dividends-set').textContent = 'Отложено ' + fmt(set);
  $('dividends-left').textContent = target ? 'осталось ' + fmt(week.left) : '';
  const s = $('dividends-status');
  s.textContent = target === null ? '' : week.done ? 'Недельная сумма собрана' : week.behind ? 'Отстаём от плана на ' + fmt(Math.round(Number(week.pace) - set)) : 'Идём по плану';
  s.classList.toggle('is-behind', Boolean(week.behind) && !week.done);
  $('dividends-today').textContent = Number(week.collected_today) ? 'Сегодня внесено в операции: ' + fmt(week.collected_today) : 'Сегодня ещё не вносили';
  const days = Number(week.days_left || 0);
  const suggest = Number(week.suggest_today || 0);
  $('dividends-hint').textContent = target && Number(week.left) > 0 && suggest > 0
    ? 'Рекомендуем сегодня ' + fmt(suggest) + ' · до выдачи ' + days + ' ' + L.plural(days, 'день', 'дня', 'дней')
    : target ? 'Выдача ' + longDay(week.payout_day) : '';
  $('dividends-fill-button').hidden = !(target && Number(week.left) > 0);
}
on('dividends-fill-button', 'click', () => {
  const week = view?.data.dividends_week;
  if (!week) return;
  setSection('journal', true);
  $('expense-item').value = 'reserve_dividends_transfer';
  $('expense-note').value = 'Дивиденды в сейф';
  $('expense-amount').value = Number(week.suggest_today) > 0 ? fmt(Math.round(Number(week.suggest_today))) : '';
  updateNewRow();
  $('other-expense-form').scrollIntoView({block: 'center', behavior: 'smooth'});
  $('expense-amount').focus({preventScroll: true});
  // Куда ушла сумма — видно по вспышке строки ввода.
  B?.flash($('other-expense-form'));
});


/* ── Разовые действия: начальные остатки и ручной приход ───────────── */
function openTools() {
  const dialog = $('fd-tools');
  if (!dialog) return;
  $('fd-tools-msg').hidden = true;
  const l = view?.data.ledger;
  if (l) {
    $('cash-opening-note').textContent = l.cash_opening
      ? 'Начальный остаток уже задан: ' + money(l.cash_opening.amount) + ' на ' + longDay(l.cash_opening.day) + '.'
      : 'Подтверждённый остаток на начало учёта. Задаётся один раз, дальше переносится сам.';
    $('cash-opening-form').querySelector('button').disabled = l.cash_opening !== null;
  }
  if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
}
on('fd-open-tools', 'click', openTools);
on('fd-cash-set', 'click', openTools);
on('fd-tools-close', 'click', () => { if (globalThis.RetroSave?.confirmLeave() ?? true) $('fd-tools').close(); });
on('fd-tools', 'click', event => { if (event.target === $('fd-tools')) $('fd-tools').close(); });
// Суммы показываем с разрядами, когда поле отпустили: 5000000 → 5 000 000.
document.addEventListener('change', event => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || !input.classList.contains('fd-money')) return;
  const raw = input.value.trim();
  if (raw && /^[\d\s  .,]+$/.test(raw)) input.value = fmt(parse(raw));
});
function toolForm(id, url, body, success) {
  const form = $(id);
  if (!form) return;
  const submit = button => {
    if (!form.reportValidity() || !view) return Promise.resolve(false);
    const note = $('fd-tools-msg'); note.hidden = true;
    // Окно модальное: сообщение страницы под ним не видно, поэтому ошибку
    // повторяем внутри окна.
    return run(() => write(url, body(new FormData(form))), success, {button: button || form.querySelector('button[type=submit], button:not([type])')}).then(ok => {
      if (ok) { form.reset(); $('fd-tools').close(); return true; }
      note.textContent = $('accountant-message').textContent; note.hidden = !note.textContent;
      return false;
    });
  };
  form.addEventListener('submit', event => { event.preventDefault(); submit(event.submitter); });
  globalThis.RetroSave?.register(form, () => submit());
}
toolForm('handover-form', '/api/accountant/handover', f => ({date: selectedDay(), amount: String(parse(f.get('amount'))), note: f.get('note')}), 'Приход от кассира сохранён.');
toolForm('cash-opening-form', '/api/accountant/cash-opening', f => ({date: selectedDay(), amount: String(parse(f.get('amount'))), note: f.get('note')}), 'Начальный остаток бухгалтера сохранён.');
toolForm('reserve-opening-form', '/api/accountant/reserves', f => ({date: selectedDay(), account: f.get('account'), kind: 'opening', amount: String(parse(f.get('amount'))), note: f.get('note')}), 'Начальный остаток сохранён на выбранную дату.');

/* ── Состояние дня: закрыт месяц, сдан отчёт, незакрытый прошлый месяц ── */
function stamp(iso) {
  if (!iso) return '';
  return iso.slice(8, 10) + '.' + iso.slice(5, 7) + '.' + iso.slice(0, 4) + ' ' + iso.slice(11, 16);
}
const closedLabel = closed => 'Закрыто · ' + stamp(closed.closed_at) + ' · ' + closed.closed_by;
function renderStatus() {
  const box = $('fd-status');
  if (!box) return;
  const {data} = view, closed = data.closed, report = data.day_report, hint = data.month_close || {};
  const chip = (cls, text, extra) => h('span', {class: 'fd-chip ' + cls}, text, extra || null);
  box.replaceChildren();
  if (closed) box.append(chip('is-closed', '🔒 ' + closedLabel(closed), h('small', {text: ' · ' + closed.name + ' — дни только для чтения'})));
  if (PAGE === 'finance') {
    if (report) box.append(report.changed
      ? chip('is-warn', 'Отчёт изменён после сдачи ' + stamp(report.submitted_at).slice(11) + ' — сдайте заново')
      : chip('is-ok', '✓ Отчёт сдан · ' + stamp(report.submitted_at) + ' · ' + report.submitted_by));
    else if (!closed) box.append(chip('is-muted', 'Отчёт за день не сдан'));
  }
  const open = hint.open_month;
  if (open && !closed && open.last_day !== data.date) box.append(h('button', {type: 'button', class: 'fd-chip is-alert',
    onclick: () => go(open.last_day)}, open.name + ' не закрыт · открыть ' + dm(open.last_day) + ' →'));
  box.hidden = !box.childElementCount;
  const closeButton = $('fd-close-month');
  if (closeButton) closeButton.hidden = !hint.close_month;
  const submit = $('fd-submit');
  if (submit) {
    submit.hidden = !!closed;
    submit.lastChild.textContent = report && !report.changed ? 'Сохранить и сдать отчёт заново' : 'Сохранить и сдать отчёт';
  }
}
// Закрытый месяц только для чтения: поля и кнопки записи гасим, сервер всё
// равно откажет («Месяц закрыт»), но экран не должен звать к записи.
const WRITABLE = '#finance-layout input, #finance-layout select, #finance-layout button, #finance-layout textarea';
function applyReadOnly() {
  const closed = !!view?.data.closed;
  document.body.classList.toggle('is-readonly', closed);
  if (!closed) return;
  document.querySelectorAll(WRITABLE).forEach(el => {
    if (el.classList.contains('fd-sec-toggle') || el.classList.contains('fd-expand')
        || el.classList.contains('fd-check') || el.classList.contains('fd-check-more') || el.classList.contains('fd-cash-open')
        || el.closest('#fd-opening-breakdown')) return;
    el.dataset.readonly = '1'; el.disabled = true;
  });
}
// Перед перерисовкой возвращаем то, что погасил закрытый месяц: дальше
// каждая секция сама решит, что ей можно.
function releaseReadOnly() {
  document.querySelectorAll('[data-readonly]').forEach(el => { delete el.dataset.readonly; el.disabled = false; });
}

/* ── «Сохранить и сдать отчёт» (ТЗ 02.10 + требование Амира) ────────── */
async function submitReport() {
  if (!view || busy) return false;
  if (!await globalThis.RetroSave.saveAll({quiet: true})) return false;
  if (view.data.closed) { message('Месяц закрыт — отчёт за этот день уже в закрытом месяце.', true); return false; }
  // Сумма от кассира — обязательное подтверждение: предлагаем подтвердить
  // ту, что в поле, одним вопросом, а не молча.
  if (!handoverCounted()) {
    const raw = $('cash-confirm-amount')?.value.trim();
    if (!confirmValid(raw)) {
      message('Сначала впишите и подтвердите сумму, полученную от кассира: без этого отчёт не сдаётся.', true);
      focusOn('cash'); $('cash-confirm-amount')?.focus();
      return false;
    }
    if (!ask('Получено от кассира ' + money(parse(raw)) + '? Подтвердить сумму и сдать отчёт.')) return false;
    if (!await confirmCash()) return false;
  }
  if (view.data.ledger.cash_balance === null) {
    message('Остаток на конец дня не посчитан' + (view.data.ledger.cash_flow.missing_day ? ': нет передачи кассы за ' + dm(view.data.ledger.cash_flow.missing_day) : '') + '. Отчёт не сдан.', true);
    return false;
  }
  const day = view.data.date;
  const checks = view.issues.map(i => ({lvl: i.lvl, text: i.text, sub: i.sub || null}));
  const ok = await run(() => write('/api/accountant/day-report', {date: day, checks}), null, {button: $('fd-submit')});
  if (ok) message('Сохранено. Отчёт за ' + longDay(day) + ' сдан — его видит учредитель.');
  return ok;
}
on('fd-submit', 'click', () => { submitReport(); });

/* ── Закрытие месяца (ТЗ 02.10, п. 2) ─────────────────────────────────── */
let monthPreview = null;
const REMARK_TONE = {pending: 'err', absent: 'warn', negative: 'err', shifts: 'warn', debts: 'warn', reports: 'todo'};
async function openMonthClose(month) {
  const dialog = $('fd-month');
  if (!dialog || !await globalThis.RetroSave.saveAll({quiet: true})) return;
  monthPreview = null;
  $('fd-month-title').textContent = 'Загружаем итог месяца…';
  $('fd-month-msg').hidden = true;
  $('fd-month-body').replaceChildren(h('div', {class: 'rm-skel-row'}, h('span', {class: 'rm-skel', style: 'width:60%'})));
  $('fd-month-confirm').disabled = true;
  $('fd-month-excel').href = '/api/accountant/reconciliation/export?month=' + month;
  if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
  try {
    const response = await fetch('/api/accountant/month-close?month=' + month, {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Не удалось собрать итог месяца.');
    monthPreview = data;
    renderMonthPreview(data);
  } catch (error) {
    $('fd-month-msg').textContent = error.message; $('fd-month-msg').hidden = false;
    $('fd-month-title').textContent = 'Закрытие месяца';
  }
}
function renderMonthPreview(data) {
  $('fd-month-title').textContent = 'Закрыть ' + data.name.toLowerCase();
  const body = $('fd-month-body'); body.replaceChildren();
  const tile = (label, value, cls) => h('div', {class: 'fd-mc-tile' + (cls ? ' ' + cls : '')}, h('span', {text: label}), h('b', {text: value}));
  body.append(h('div', {class: 'fd-mc-tiles'},
    tile('Остаток кассы на ' + dm(data.last), data.closing_balance === null ? 'не посчитан' : money(data.closing_balance), 'is-hero'),
    tile('Начало месяца', data.opening === null ? '—' : money(data.opening)),
    tile('Приход от кассира', money(data.totals.handover)),
    tile('Расход за месяц', money(data.totals.outflows))));
  body.append(h('p', {class: 'fd-mc-sub', text: 'Долги на конец месяца'}),
    h('div', {class: 'fd-mc-tiles is-small'},
      tile('Сменным не выдано', money(data.debts.salary)),
      tile('Неоплаченные расходы', money(data.debts.expenses)),
      tile('Оклады · осталось', data.debts.monthly_left === null ? '—' : money(data.debts.monthly_left))),
    h('p', {class: 'fd-mc-note', text: 'Счёт Шохруха на ' + dm(data.last) + ': ' + (data.shoh_balance === null ? '—' : money(data.shoh_balance))
      + '. Сверьте с тем, что у Шоха на руках: после закрытия расходы за этот месяц не внести.'}));
  body.append(h('p', {class: 'fd-mc-sub', text: data.remarks.length ? 'Незакрытые замечания · ' + data.remarks.length : 'Замечаний нет'}));
  if (data.remarks.length) {
    const list = h('div', {class: 'fd-mc-remarks'});
    data.remarks.forEach(item => list.append(h('div', {class: 'fd-mc-remark'},
      h('span', {class: 'fd-dot is-' + (REMARK_TONE[item.kind] || 'warn')}),
      h('span', {class: 'fd-mc-remark-body'}, h('span', {text: item.text}),
        item.days.length ? h('span', {class: 'fd-mc-days'}, ...item.days.slice(0, 12).map(day => h('button', {type: 'button', class: 'fd-mc-day', text: dm(day),
          onclick: () => { $('fd-month').close(); go(day); }})), item.days.length > 12 ? h('span', {text: ' и ещё ' + (item.days.length - 12)}) : null) : null))));
    body.append(list);
  }
  // Сверка по дням: начало → приход → расход → конец (ТЗ 02.10, п. 1.4).
  const table = h('div', {class: 'fd-mc-table', role: 'table', 'aria-label': 'Сверка по дням'},
    h('div', {class: 'fd-mc-tr is-head', role: 'row'}, ...['День', 'Начало', 'Приход', 'Расход', 'Конец'].map(text => h('span', {role: 'columnheader', text}))));
  data.days.filter(day => day.first_day !== null).forEach(day => table.append(h('div', {class: 'fd-mc-tr' + (['pending', 'none'].includes(day.handover_status) ? ' is-flag' : ''), role: 'row'},
    h('span', {text: dm(day.day)}), h('span', {class: 'num', text: day.opening === null ? '—' : fmt(day.opening)}),
    h('span', {class: 'num', text: fmt(day.handover_counted) + (day.handover_status === 'pending' ? ' *' : '')}),
    h('span', {class: 'num', text: fmt(day.outflows)}), h('span', {class: 'num', text: day.closing === null ? '—' : fmt(day.closing)}))));
  body.append(h('details', {class: 'fd-mc-recon'}, h('summary', {text: 'Сверка по дням'}), table,
    h('p', {class: 'fd-mc-note', text: '* передача кассира не подтверждена — в остаток не вошла.'})));
  if (data.includes_earlier) body.append(h('p', {class: 'fd-mc-note', text: 'Вместе с этим месяцем закроются и все более ранние дни.'}));
  body.append(h('p', {class: 'fd-mc-warn', text: data.can_close
    ? 'После закрытия дни месяца станут только для чтения: операции, выдачи и зарплаты не добавить, не изменить и не удалить. Остаток на ' + dm(data.last) + ' станет началом 1-го числа. Отчёт сразу увидит учредитель.'
    : data.reason}));
  $('fd-month-confirm').disabled = !data.can_close;
}
on('fd-close-month', 'click', () => { if (view?.data.month_close?.close_month) openMonthClose(view.data.month_close.close_month); });
on('fd-month-close', 'click', () => $('fd-month').close());
on('fd-month-cancel', 'click', () => $('fd-month').close());
on('fd-month-confirm', 'click', () => {
  const data = monthPreview;
  if (!data || !data.can_close) return;
  if (!ask('Закрыть ' + data.name.toLowerCase() + '? Изменить дни месяца после этого будет нельзя.')) return;
  const button = $('fd-month-confirm');
  run(() => write('/api/accountant/month-close', {month: data.month}), null, {button}).then(ok => {
    if (!ok) { $('fd-month-msg').textContent = $('accountant-message').textContent; $('fd-month-msg').hidden = false; return; }
    $('fd-month').close();
    message(data.name + ' закрыт. Отчёт месяца ушёл учредителю.');
  });
});

/* ── «Зарплата · день»: итог выдано / осталось ─────────────────────────── */
function renderTotals() {
  if (!$('sd-accrued')) return;
  const {board, data} = view, t = board.totals;
  const left = board.own.reduce((s, r) => s + Math.max(0, r.debt || 0), 0);
  const older = board.other.reduce((s, r) => s + Math.max(0, r.debt || 0), 0);
  $('sd-accrued').textContent = money(t.accrued);
  $('sd-paid').textContent = money(t.paid);
  $('sd-left').textContent = money(left);
  $('sd-left-sub').textContent = older ? '+ прошлые смены ' + money(older) : 'смена ' + dm(board.S);
  $('sd-cash').textContent = data.ledger.cash_balance === null ? 'нет данных' : money(data.ledger.cash_balance);
  $('sd-cash-label').textContent = data.date === today ? 'Касса сегодня' : 'Касса на конец ' + dm(data.date);
}

/* ── Загрузка дня ───────────────────────────────────────────────────── */
function renderAll() {
  if (!view) return;
  releaseReadOnly();
  if ($('shift-section')) renderShift();
  if ($('journal-section')) renderJournal();
  renderRail(); renderTotals(); renderStatus(); applyReadOnly();
}
async function loadDay() {
  const day = selectedDay();
  const sequence = ++requestNo;
  if (!day || !today || day > today) { message('Выберите сегодняшний или прошедший день.', true); return; }
  const S = L.shiftIso(day, -1);
  $('fd-day-label').textContent = longDay(day, true);
  $('accountant-next').disabled = day >= today;
  $('accountant-today').classList.toggle('is-active', day === today);
  $('accountant-today').setAttribute('aria-pressed', String(day === today));
  $('finance-layout').setAttribute('aria-busy', 'true');
  try {
    const get = async url => { const r = await fetch(url, {cache: 'no-store'}); const j = await r.json(); if (!r.ok) throw new Error(j.detail || 'Не удалось загрузить данные.'); return j; };
    // Реестр вчерашней смены нужен только «Зарплате · день».
    const [data, staff] = await Promise.all([
      get('/api/accountant/day?date=' + day),
      PAGE === 'salary-day' ? get('/api/accountant/staff?date=' + S).catch(() => null) : Promise.resolve(null),
    ]);
    if (sequence !== requestNo) return;
    const board = L.shiftBoard({payday: day, staff, accruals: data.ledger.accruals, movements: data.ledger.movements});
    const blocker = PAGE === 'salary-day' ? L.shiftBlocker(staff, board) : null;
    const monthly = L.monthlyBoard(data);
    const pocket = data.shoh_pocket && data.shoh_pocket.pocket !== null && data.shoh_pocket.pocket !== undefined ? Number(data.shoh_pocket.pocket) : null;
    const shoh = {buys: [], hand: pocket};
    const cash = L.cashCard(data);
    const journal = L.journal(data, index);
    const issues = L.financeIssues({data, board, blocker, monthly, shoh, cash});
    view = {data, staff, board, blocker, monthly, shoh, cash, journal, issues};
    renderAll();
    $('finance-layout').hidden = false;
    $('fd-skeleton').hidden = true;
    if (!$('accountant-message').classList.contains('is-error')) message('');
    status('Данные за ' + longDay(day));
  } catch (error) {
    if (sequence === requestNo) {
      message(error.message, true); status('Данные не загрузились');
      if ($('finance-layout').hidden) $('fd-skeleton').hidden = true;
    }
    throw error;
  } finally {
    if (globalThis.RetroState?.shouldReleaseBusy ? RetroState.shouldReleaseBusy(sequence, requestNo) : true) $('finance-layout').setAttribute('aria-busy', 'false');
  }
}
function go(day) {
  if (!day || day > today) {
    // Будущий или пустой день: возвращаем поле к показанному дню и говорим почему.
    $('accountant-date').value = view ? view.data.date : today;
    if (day > today) message('Будущие дни недоступны: выберите сегодня или прошедший день.', true);
    return;
  }
  // Набранное, но не сохранённое, ушло бы в другой день — спрашиваем.
  if (view && day !== view.data.date && !(globalThis.RetroSave?.confirmLeave() ?? true)) {
    $('accountant-date').value = view.data.date;
    return;
  }
  $('accountant-date').value = day;
  focusKey = null; expanded.clear(); confirmEditing = false; confirmDirty = false; breakdownOpen = false;
  const url = new URL(location.href); url.searchParams.set('date', day); history.replaceState(null, '', url);
  message('');
  reload().catch(() => {});
}
on('accountant-prev', 'click', () => go(L.shiftIso(selectedDay(), -1)));
on('accountant-next', 'click', () => go(L.shiftIso(selectedDay(), 1)));
on('accountant-today', 'click', () => go(today));
on('accountant-date', 'change', () => go(selectedDay()));
// Подпись дня открывает системный выбор даты: на телефоне — колесо, на ПК — календарь.
on('fd-date-label', 'click', event => {
  const input = $('accountant-date');
  if (event.target === input) return;
  event.preventDefault();
  try { input.showPicker(); } catch { input.focus(); }
});
on('fd-export', 'click', () => {
  const button = $('fd-export');
  // Кнопка в работе, пока файл не начал скачиваться.
  const work = exportDay();
  if (B) B.button(button, work); else { button.disabled = true; work.finally(() => { button.disabled = false; }); }
});
async function exportDay() {
  const day = selectedDay();
  try {
    // «Проверки» считает экран — отдаём их серверу, чтобы лист в файле совпал с правой колонкой.
    const checks = view && view.data.date === day ? view.issues.map(i => ({lvl: i.lvl, text: i.text, sub: i.sub || null})) : [];
    const response = await fetch('/api/accountant/day/export', {method: 'POST', cache: 'no-store',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({date: day, checks})});
    if (!response.ok) throw new Error('Не удалось сформировать Excel.');
    const url = URL.createObjectURL(await response.blob()), link = h('a', {href: url, download: 'Retro-finance-' + day + '.xlsx'});
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
    message('Отчёт за день сохранён в Excel.');
    return true;
  } catch (error) { message(error.message, true); return false; }
}

(async () => {
  try {
    const catalogRequest = $('expense-item') ? fetch('/api/accountant/expenses/catalog', {cache: 'no-store'}) : null;
    catalogRequest?.catch(() => {});
    const config = await globalThis.RetroConfig;
    today = config.today;
    checkMode = !!config.check_mode;
    const requested = new URLSearchParams(location.search).get('date');
    $('accountant-date').max = today;
    const valid = requested && /^\d{4}-\d{2}-\d{2}$/.test(requested) && requested <= today;
    $('accountant-date').value = valid ? requested : today;
    // Адрес с будущей или кривой датой не должен расходиться с показанным днём.
    if (requested && !valid) { const url = new URL(location.href); url.searchParams.set('date', today); history.replaceState(null, '', url); }
    if (catalogRequest) {
      const catalogResponse = await catalogRequest;
      if (!catalogResponse.ok) throw new Error('Не удалось загрузить наименования затрат.');
      catalog = (await catalogResponse.json()).groups;
      index = L.catalogIndex(catalog.concat([{code: 'reserves', label: 'Резервы', items: Object.entries(special).map(([code, v]) => ({code, label: v.label}))}]));
      fillCatalog();
    }
    await loadDay();
  } catch (error) { message(error.message, true); }
})();
