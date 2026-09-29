/* «Финансы дня» — экран бухгалтера по макету 2a.
   Выбранная дата — день выплат P. Смена, которую выдают, — вчерашняя (S = P − 1).
   Расчёты без DOM лежат в accountant-logic.js; здесь только сборка экрана и
   запись: каждая денежная запись идёт через RetroFinancialWrite (ключ
   идемпотентности), чтобы двойной клик не выдал деньги дважды. */
const $ = id => document.getElementById(id);
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
  if (B) {
    (fb.rows || []).forEach(line => B.row(line, work).catch(() => {}));
    // Поле само подсвечивает свою строку по итогу; строку «в работе» ставим
    // только действиям кнопками.
    if (fb.field) work = B.field(fb.field, work, {row: fb.row});
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
function strip(parts, action) {
  return h('div', {class: 'fd-strip'}, h('span', {class: 'fd-strip-tag', text: '⊘ Без Hikvision'}),
    h('span', {class: 'fd-strip-text'}, ...parts.filter(Boolean).flatMap((part, i) => i ? [' ', h('span', {text: part})] : [h('span', {text: part})])), action || null);
}
function noteStrip(text, tag, action) {
  return h('div', {class: 'fd-strip is-block'}, h('span', {class: 'fd-strip-tag', text: tag}),
    h('span', {class: 'fd-strip-text', text}), action || null);
}

// Запрет на весь день бывает только в двух случаях: нет данных кассира или
// сервер отказал в начислении всей смены (за день уже есть зарплата без
// сотрудника). Остальные причины — у отдельных строк, их видно в строке.
const dayBlocks = {};
function payDisabledReason() {
  const {data, board} = view;
  if (data.ledger.cash_balance === null) return 'Нет данных кассира за ' + longDay(data.date) + ' — выдачу записать нельзя.';
  if (dayBlocks[board.S] && !board.confirmed) return dayBlocks[board.S];
  return null;
}
const BLOCK_TEXT = {
  rate: 'Нет ставки — смена не начисляется',
  unlinked: 'Нет привязки к Hikvision — вход не виден',
  hikvision: 'Входы Hikvision за этот день ещё не пришли',
  unknown: 'Начисление не рассчитано',
};
function rowLock(row) {
  if (view.data.ledger.cash_balance === null) return payDisabledReason();
  if (row.block) return BLOCK_TEXT[row.block];
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

  // Полосы: сотрудники без Hikvision, состояние Hikvision и причина запрета.
  const strips = $('shift-strips'); strips.replaceChildren();
  const noHik = board.own.filter(r => r.noHik);
  if (noHik.length) {
    const roles = [...new Set(noHik.map(r => r.role).filter(Boolean))].join(', ');
    const absent = noHik.filter(r => r.status === 'manual_absent').length;
    const n = noHik.length;
    const parts = [n + ' ' + L.plural(n, 'сотрудник не зарегистрирован', 'сотрудника не зарегистрированы', 'сотрудников не зарегистрированы') + ' в Hikvision'
        + (roles ? '' : '.'), roles ? '(' + roles + ').' : null,
      noHik.some(r => !r.accrualId) ? 'Время входа неизвестно — нажмите на статус, чтобы отметить «был / не был».' : 'Смена начислена — отметки закрыты.',
      absent ? 'Отмечено отсутствие: ' + absent + '.' : null];
    strips.append(strip(parts, h('button', {type: 'button', class: 'fd-strip-btn', text: 'Показать', onclick: () => { shiftTab = 'nohik'; renderShift(); }})));
  }
  const blocker = view.blocker;
  if (blocker) {
    const parts = [];
    if (blocker.rate) parts.push(blocker.rate + ' без ставки');
    if (blocker.unlinked) parts.push(blocker.unlinked + ' без привязки Hikvision');
    if (blocker.hikvision) parts.push(blocker.hikvision + ' без данных Hikvision');
    if (blocker.unknown) parts.push(blocker.unknown + ' не рассчитано');
    const payableLeft = board.own.some(r => !r.block);
    // Каждая часть — отдельный узел: так её переводит словарь, а не склейка.
    const text = h('span', {class: 'fd-strip-text'}, ...parts.flatMap((part, i) => i ? [' · ', h('span', {text: part})] : [h('span', {text: part})]),
      ' ', h('span', {text: payableLeft ? '— остальным можно выдавать.' : '— выдавать пока некому.'}));
    strips.append(h('div', {class: 'fd-strip is-block'}, h('span', {class: 'fd-strip-tag', text: 'Не начислено'}), text,
      h('button', {type: 'button', class: 'fd-strip-btn', text: 'Показать', onclick: () => { shiftTab = 'err'; renderShift(); }})));
  }
  const health = attendanceHealth(staff?.attendance || data.attendance);
  if (!health.ok) strips.append(noteStrip(health.text, 'Hikvision'));
  if (lock) strips.append(noteStrip(lock, 'Выдача закрыта',
    data.ledger.cash_balance === null ? h('button', {type: 'button', class: 'fd-strip-btn', text: 'Ввести приход', onclick: openTools}) : null));

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
  // Выдача закрыта (нет кассы) — не «все получили», а «нельзя»: причина в подсказке.
  const waiting = board.handOut.length;
  $('pay-all-label').textContent = n ? 'Выдать пришедшим · ' + n : lock && waiting ? 'Выдать пришедшим · ' + waiting : 'Все пришедшие получили';
  $('pay-all').disabled = !n || busy;
  $('pay-all').title = n ? 'Выдать ставку всем пришедшим, кому ещё ничего не выдано'
    : lock || (board.toPay.length ? 'Всем пришедшим уже выдано. Остаток частичных выдач и долги прошлых смен — в их строках.' : 'Всем пришедшим уже выдано.');
}

function shiftRow(row, lock) {
  const flagged = focusKey === 'row:' + row.key || (focusKey === 'todo' && row.kind === 'todo' && row.own);
  const line = h('div', {class: 'fd-row fd-shift-cols is-' + row.kind + (flagged ? ' is-focus' : ''), 'data-key': row.key,
    'data-busy-key': 'shift:' + dayKey(stable(row))});

  const on = row.paid > 0;
  const check = h('button', {type: 'button', class: 'fd-cb' + (on ? ' is-on' : '') + (row.kind === 'err' ? ' is-err' : ''),
    title: on ? 'Выдано' : 'Отметить выдачу', 'aria-label': (on ? 'Выдано: ' : 'Отметить выдачу: ') + row.name,
    'aria-pressed': String(on), text: on ? '✓' : '', 'data-busy-key': 'cb:' + dayKey(stable(row))});
  const canPay = !lock && row.accrued > 0 && row.debt > 0;
  const canUndo = !lock && row.paidToday > 0;
  check.disabled = !(canPay || canUndo);
  // Выключенная галочка объясняет, почему её не нажать.
  if (check.disabled) check.title = lock || (row.accrued === 0 ? 'Входа нет — начисление 0 сум, выдавать нечего' : 'Выдано полностью в прошлые дни');
  check.addEventListener('click', () => {
    const fb = {button: check, row: line};
    if (row.debt > 0 && row.accrued > 0) setPaid(row, row.paidToday + row.debt, null, fb);
    else if (row.paidToday > 0) setPaid(row, 0, null, fb);
  });

  const who = h('div', {class: 'fd-who'},
    h('div', {class: 'fd-who-line'}, h('span', {class: 'fd-who-name', text: row.name}),
      row.noHik ? h('span', {class: 'fd-nohik', title: 'Не зарегистрирован в Hikvision — присутствие отмечается вручную', text: '⊘ без Hikvision'}) : null),
    h('div', {class: 'fd-who-role'}, h('span', {text: row.role}), row.own ? null : h('span', {text: ' · смена ' + dm(row.day)})));

  const time = h('div', {class: 'fd-time'});
  if (row.own) {
    time.append(h('div', {class: 'fd-time-main' + (row.status === 'late' ? ' is-late' : row.time ? '' : ' is-none'), text: row.time || '—'}));
    if (row.noHik) time.append(h('div', {class: 'fd-time-sub is-nohik', text: 'нет данных'}));
    else if (row.late) time.append(h('div', {class: 'fd-time-sub', text: '+' + row.late + ' мин'}));
  } else time.append(h('div', {class: 'fd-time-main is-none', text: '—'}));

  const [label, cls] = STATUS[row.status] || ['—', 'unlinked'];
  const toggleable = row.noHik && row.own && !row.accrualId;
  const pill = h(toggleable ? 'button' : 'span', {class: 'fd-pill ' + cls + (row.noHik ? ' is-manual' : '') + (toggleable ? ' is-toggle' : ''),
    title: row.noHik ? (toggleable ? 'Нет в Hikvision. Нажмите, чтобы отметить: был / не был' : 'Отмечено вручную · смена уже начислена, отметку не изменить') : 'Данные Hikvision',
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
  const commit = () => {
    const raw = input.value.trim();
    // «abc» — не ноль: без проверки такая опечатка снимала бы выдачу.
    if (raw && !/^[\d\s\u00a0\u202f.,]+$/.test(raw)) {
      message('Сумма — только цифрами: ' + raw, true); input.classList.add('is-bad'); input.focus(); return;
    }
    const value = parse(raw);
    // Больше долга сервер не примет — говорим сразу и сколько можно.
    const most = row.paidToday + Math.max(0, row.debt);
    if (value > most) {
      message('Больше долга: ' + row.name + ' можно выдать ещё ' + money(Math.max(0, row.debt)) + ' (итого за сегодня до ' + money(most) + ').', true);
      input.focus(); return;
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
  const note = h('div', {class: 'fd-pay-note is-blocked', text: BLOCK_TEXT[row.block]});
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
  }, target ? 'Выдано: ' + row.name + ' · ' + money(target) : 'Выдача снята: ' + row.name, fb);
}
$('pay-all').addEventListener('click', () => {
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
  const cash = view.data.ledger.cash_balance !== null;
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
  $('other-expense-form').querySelector('button').disabled = busy || (cashNeeded && view && view.data.ledger.cash_balance === null);
}
['expense-item', 'expense-amount', 'expense-paid'].forEach(id => $(id).addEventListener('input', updateNewRow));
// Подписи групп в списке — атрибуты, их переводчик страницы не трогает:
// после смены языка пересобираем список сами.
document.querySelectorAll('[data-lang]').forEach(button => button.addEventListener('click', () => setTimeout(() => { if (catalog.length) fillCatalog(); }, 0)));
$('other-expense-form').addEventListener('submit', event => {
  event.preventDefault();
  if (!view) return;
  const code = $('expense-item').value, note = $('expense-note').value.trim();
  const amount = parse($('expense-amount').value), paidRaw = $('expense-paid').value.trim();
  if (!code) { message('Выберите категорию расхода.', true); $('expense-item').focus(); return; }
  if (!amount) { message('Укажите сумму.', true); $('expense-amount').focus(); return; }
  if (!note) { message('Укажите, за что или кому.', true); $('expense-note').focus(); return; }
  const paid = paidRaw === '' ? null : parse(paidRaw);
  if (paid !== null && paid > amount) { message('Оплачено больше суммы расхода.', true); return; }
  const day = view.data.date, reserve = special[code];
  const request = reserve ? ['/api/accountant/reserves', {date: day, amount: String(amount), note, account: reserve.account, kind: reserve.kind}]
    : incomeCodes.includes(code) ? ['/api/accountant/incomes', {date: day, item_code: code, note, amount: String(amount)}]
    : ['/api/accountant/expenses', {date: day, item_code: code, note, amount: String(amount), paid_amount: paid === null ? null : String(paid)}];
  run(() => write(...request), paid !== null && paid < amount ? 'Записано. Неоплаченная часть ушла в долги.' : 'Записано. Остаток пересчитан.',
    {button: event.submitter || event.target.querySelector('button[type=submit]')}).then(ok => {
    if (!ok) return;
    ['expense-note', 'expense-amount', 'expense-paid'].forEach(id => { $(id).value = ''; });
    updateNewRow();
    $('expense-note').focus({preventScroll: true});
  });
});

/* ── Шох ────────────────────────────────────────────────────────────── */
function renderShoh() {
  const {shoh, data} = view;
  const unknown = 'не задан';
  $('shoh-start').textContent = shoh.known ? fmt(shoh.start) : unknown;
  $('shoh-given').textContent = fmt(shoh.given);
  $('shoh-spent').textContent = fmt(shoh.spent);
  $('shoh-spent-sub').textContent = shoh.count + ' ' + L.plural(shoh.count, 'покупка', 'покупки', 'покупок')
    + (shoh.reported !== null && shoh.reported !== undefined ? ' · отчитался за ' + shoh.reported + '%' : '');
  $('shoh-balance').textContent = shoh.hand === null ? '—' : fmt(shoh.hand);
  $('shoh-balance').classList.toggle('is-negative', shoh.hand !== null && shoh.hand < 0);
  $('shoh-sum').textContent = shoh.hand === null ? 'начальный остаток не задан' : 'на руках ' + money(shoh.hand) + ' · ' + shoh.count + ' ' + L.plural(shoh.count, 'покупка', 'покупки', 'покупок');
  $('shoh-give-form').querySelector('button[type=submit]').disabled = busy || data.ledger.cash_balance === null;

  const gives = $('shoh-gives'); gives.replaceChildren();
  shoh.gives.forEach(give => gives.append(h('div', {class: 'fd-give', 'data-busy-key': 'give:' + dayKey(give.id || give.time)},
    give.time ? h('span', {class: 'fd-give-time', text: give.time}) : null,
    h('span', {class: 'fd-give-label', text: give.fromKassa ? 'Выдал кассир · уже вычтено из передачи кассы'
      : give.fromJournal ? 'Строка журнала «Закуп · Шох» · в остатке' : 'Выдано бухгалтером · попадает в журнал и остаток'}),
    h('strong', {class: 'num', text: money(give.amount)}),
    give.fromKassa ? h('span', {class: 'fd-x-gap'}) : h('button', {type: 'button', class: 'fd-x', text: '×', title: 'Удалить выдачу', 'aria-label': 'Удалить выдачу Шоху', onclick: event => {
      if (!ask('Удалить выдачу Шоху ' + money(give.amount) + '?')) return;
      const button = event.currentTarget;
      run(() => remove('movement', give.id, data.date), 'Выдача Шоху удалена.', {button, row: button.closest('.fd-give'), collapse: true});
    }}))));

  const flagged = shoh.buys.filter(b => b.flags.length).length;
  const normal = shoh.buys.filter(b => !b.flags.length && b.accepted_at === null);
  $('shoh-buys-count').textContent = shoh.buys.length ? (flagged ? flagged + ' ' + L.plural(flagged, 'требует проверки', 'требуют проверки', 'требуют проверки') : 'всё в норме') : '';
  $('shoh-buys-count').classList.toggle('is-warn', flagged > 0);
  $('shoh-accept-all').hidden = !normal.length;
  $('shoh-accept-all').textContent = 'Принять все в норме · ' + normal.length;

  const box = $('shoh-buys'); box.replaceChildren();
  shoh.buys.forEach(buy => {
    const focus = focusKey === 'buy:' + buy.id;
    const line = h('div', {class: 'fd-row fd-buy-cols' + (buy.flags.length ? ' is-flagged' : '') + (focus ? ' is-focus' : ''), 'data-key': 'buy:' + buy.id,
      'data-busy-key': 'buy:' + dayKey(buy.id)});
    const item = h('span', {class: 'fd-buy-item'});
    if (buy.has_photo) {
      const photo = h('a', {class: 'fd-thumb', href: '/api/accountant/shokh/photo/' + buy.id, target: '_blank', rel: 'noopener', title: 'Фото приложено'});
      const img = h('img', {src: '/api/accountant/shokh/photo/' + buy.id, alt: '', loading: 'lazy'});
      img.addEventListener('error', () => { photo.replaceChildren('фото'); photo.classList.add('is-text'); });
      photo.append(img); item.append(photo);
    } else item.append(h('span', {class: 'fd-thumb is-none', title: 'Без фото', text: '⊘'}));
    item.append(h('span', {class: 'fd-buy-name', text: buy.item}));
    const qty = Number(buy.quantity);
    const price = h('span', {class: 'num fd-buy-price'}, h('span', {class: buy.price_above_usual ? 'is-above' : '', text: fmt(buy.price)}),
      buy.usual_price !== null ? h('small', {text: 'обычно ' + fmt(buy.usual_price)}) : null);
    const check = h('span', {class: 'fd-buy-check'});
    if (buy.accepted_at !== null) check.append(h('span', {class: 'fd-accepted', text: 'принято бухгалтером'}));
    else if (buy.flags.length) {
      check.append(h('span', {class: 'fd-flag', text: buy.flags.map(f => f.t).join(' · ')}));
      const accept = h('button', {type: 'button', class: 'fd-accept', title: 'Проверено, принять', text: 'Принять', 'data-busy-key': 'accept:' + dayKey(buy.id)});
      accept.disabled = busy || (buy.iiko && !['synced', 'legacy'].includes(buy.iiko.status));
      accept.addEventListener('click', () => run(() => write('/api/accountant/shokh/purchases/' + buy.id + '/accept', {date: data.date}), 'Покупка принята: ' + buy.item,
        {button: accept, row: line}));
      check.append(accept);
    } else check.append(h('span', {class: 'fd-norm', text: '✓ в норме'}));
    line.append(h('span', {class: 'fd-buy-meta'}, h('span', {class: 'fd-muted', text: (buy.created_at || '').slice(11, 16)}),
      h('span', {class: 'fd-muted fd-buy-point', text: buy.point})), item,
      h('span', {class: 'num fd-buy-qty', text: (Number.isInteger(qty) ? qty : fmt(qty)) + ' ' + buy.unit}), price,
      h('strong', {class: 'num fd-buy-total', text: fmt(buy.total)}), check);
    box.append(line);
  });
  if (!shoh.buys.length) box.append(h('div', {class: 'fd-empty is-left', text: 'Шох ещё не вносил покупки за этот день.'}));
  renderTransfers();
  $('shoh-foot').replaceChildren('Наличными ' + fmt(shoh.spent) + ' · перечислением ' + fmt(shoh.trSum) + ' · Закуп за день: ',
    h('strong', {text: money(shoh.spent + shoh.trSum)}));
}

/* Перечисления поставщику вносит бухгалтер: безнал, мимо кассы и подотчёта. */
function renderTransfers() {
  const {shoh, data} = view;
  const box = $('shoh-transfers'); box.replaceChildren();
  shoh.trs.forEach(t => box.append(h('div', {class: 'fd-row fd-tr-cols fd-tr-row', 'data-busy-key': 'tr:' + dayKey(t.id)},
    h('span', {class: 'fd-tr-supplier', text: t.supplier}), h('span', {class: 'fd-tr-item', text: t.item}),
    h('span', {class: 'fd-tr-point', text: t.point}), h('strong', {class: 'num fd-tr-amount', text: fmt(t.amount)}),
    h('button', {type: 'button', class: 'fd-x', text: '×', title: 'Удалить перечисление', 'aria-label': 'Удалить перечисление', onclick: event => {
      if (!ask('Удалить перечисление ' + t.supplier + ' · ' + money(t.amount) + '?')) return;
      const button = event.currentTarget;
      run(async () => {
        const response = await fetch('/api/accountant/supplier-transfers/' + t.id + '?date=' + encodeURIComponent(data.date), {method: 'DELETE'});
        if (!response.ok) { let detail = 'Не удалось удалить перечисление.'; try { detail = (await response.json()).detail || detail; } catch { /* нет тела */ } throw new Error(detail); }
      }, 'Перечисление удалено.', {button, row: button.closest('.fd-tr-row'), collapse: true});
    }}))));
  // Точки закупа: из справочника сервера, а пока его нет — из покупок дня.
  const select = $('transfer-point'), keep = select.value;
  const raw = data.procurement_points || [];
  const points = [...new Set((raw.length ? raw.map(p => typeof p === 'string' ? p : p.name || p.point) : ['RETRO', ...shoh.buys.map(b => b.point)]).filter(Boolean))];
  select.replaceChildren(...points.map(point => new Option(point, point)));
  if (points.includes(keep)) select.value = keep;
}
$('transfer-form').addEventListener('submit', event => {
  event.preventDefault();
  if (!view) return;
  const supplier = $('transfer-supplier').value.trim(), item = $('transfer-item').value.trim();
  const amount = parse($('transfer-amount').value);
  if (!supplier || !amount) { message('Укажите поставщика и сумму.', true); ($('transfer-supplier').value ? $('transfer-amount') : $('transfer-supplier')).focus(); return; }
  run(() => write('/api/accountant/supplier-transfers', {date: view.data.date, supplier, item, point: $('transfer-point').value, amount: String(amount)}),
    'Перечисление записано.', {button: $('transfer-form').querySelector('button[type=submit]')}).then(ok => { if (ok) ['transfer-supplier', 'transfer-item', 'transfer-amount'].forEach(id => { $(id).value = ''; }); });
});
$('shoh-quick').addEventListener('click', event => {
  const button = event.target.closest('button[data-amount]');
  if (!button) return;
  $('shoh-give-amount').value = fmt(button.dataset.amount);
  $('shoh-give-amount').focus();
});
$('shoh-give-form').addEventListener('submit', event => {
  event.preventDefault();
  const amount = parse($('shoh-give-amount').value);
  if (!amount) { message('Укажите сумму для Шоха.', true); $('shoh-give-amount').focus(); return; }
  run(() => write('/api/accountant/procurement', {date: view.data.date, recipient: 'Шох', purpose: 'закуп за день', amount: String(amount)}),
    'Выдано Шоху ' + money(amount) + '. Баланс и журнал обновлены.', {button: $('shoh-give-form').querySelector('button[type=submit]')}).then(ok => { if (ok) $('shoh-give-amount').value = ''; });
});
$('shoh-accept-all').addEventListener('click', () => {
  const list = view.shoh.buys.filter(b => !b.flags.length && b.accepted_at === null);
  if (!list.length || !ask('Принять ' + list.length + ' ' + L.plural(list.length, 'покупку', 'покупки', 'покупок') + ' в норме?')) return;
  const lines = list.map(buy => document.querySelector('[data-busy-key="' + CSS.escape('buy:' + dayKey(buy.id)) + '"]')).filter(Boolean);
  run(async () => { for (const buy of list) await write('/api/accountant/shokh/purchases/' + buy.id + '/accept', {date: view.data.date}); }, 'Покупки приняты.',
    {button: $('shoh-accept-all'), rows: lines});
});

/* ── Оклады ─────────────────────────────────────────────────────────── */
function renderSalary() {
  const {monthly, data} = view;
  const box = $('salary-today'); box.replaceChildren();
  monthly.today.forEach(pay => {
    const focus = focusKey === 'month:' + pay.employeeId;
    const left = pay.left < 0 ? 'переплата ' + fmt(-pay.left) : pay.left === 0 ? 'оклад закрыт' : 'осталось ' + fmt(pay.left);
    const line = h('div', {class: 'fd-mo-row' + (pay.left < 0 ? ' is-over' : '') + (focus ? ' is-focus' : ''), 'data-key': 'month:' + pay.employeeId,
      'data-busy-key': 'mo:' + dayKey(pay.id)});
    box.append(line);
    line.append(
      h('div', {class: 'fd-mo-who'}, h('div', {class: 'fd-who-name', text: pay.name}), h('div', {class: 'fd-who-role', text: pay.role})),
      h('strong', {class: 'num', text: fmt(pay.amount)}),
      h('span', {class: 'num fd-mo-left' + (pay.left < 0 ? ' is-over' : pay.left === 0 ? ' is-closed' : ''), text: left}),
      h('button', {type: 'button', class: 'fd-x', text: '×', title: 'Удалить выплату', 'aria-label': 'Удалить выплату оклада', onclick: event => {
        if (!ask('Удалить выплату оклада ' + pay.name + ' · ' + money(pay.amount) + '?')) return;
        run(() => remove('movement', pay.id, data.date), 'Выплата оклада удалена.', {button: event.currentTarget, row: line, collapse: true});
      }}));
  });
  if (!monthly.today.length) box.append(h('div', {class: 'fd-mo-empty', text: 'Сегодня оклады не выдавались.'}));
  $('salary-sum').textContent = monthly.today.length ? monthly.today.length + ' ' + L.plural(monthly.today.length, 'выплата', 'выплаты', 'выплат') + ' · ' + money(monthly.todaySum) : 'не выдавались';

  const select = $('salary-employee'), keep = select.value;
  select.replaceChildren(new Option('Кому выдаём оклад', ''));
  monthly.people.forEach(p => select.add(new Option(p.name + ' · ' + (p.left < 0 ? 'переплата ' + fmt(-p.left) : 'осталось ' + fmt(p.left)), String(p.id))));
  if (keep) select.value = keep;
  salaryHint();
}
let salaryConfirm = false;
function salaryHint() {
  if (!view) return;
  const person = view.monthly.people.find(p => String(p.id) === $('salary-employee').value);
  const amount = parse($('salary-amount').value), hint = $('salary-hint'), button = $('salary-submit');
  hint.className = 'fd-mo-hint'; button.classList.remove('is-danger'); button.textContent = 'Записать выплату';
  $('salary-amount').classList.remove('is-bad');
  button.disabled = busy || view.data.ledger.cash_balance === null;
  if (!person) { hint.textContent = 'Выберите сотрудника — покажем, сколько осталось по окладу.'; return; }
  const left = Math.max(0, person.left);
  hint.replaceChildren(h('span', {text: 'Оклад ' + fmt(person.salary) + ' · выдано ' + fmt(person.paid) + ' · осталось ' + fmt(left)}));
  hint.classList.add('is-info');
  if (amount > left) {
    hint.append(' ', h('span', {text: '— сумма больше остатка на ' + fmt(amount - left)}));
    hint.classList.add('is-bad'); $('salary-amount').classList.add('is-bad');
    if (salaryConfirm) { button.textContent = 'Всё равно записать'; button.classList.add('is-danger'); }
  }
}
['salary-employee', 'salary-amount'].forEach(id => $(id).addEventListener('input', () => { salaryConfirm = false; salaryHint(); }));
$('salary-payment-form').addEventListener('submit', event => {
  event.preventDefault();
  const person = view.monthly.people.find(p => String(p.id) === $('salary-employee').value);
  const amount = parse($('salary-amount').value);
  if (!person || !amount) { message('Выберите сотрудника и сумму.', true); return; }
  if (amount > Math.max(0, person.left) && !salaryConfirm) { salaryConfirm = true; message(''); salaryHint(); $('salary-amount').focus(); return; }
  salaryConfirm = false;
  run(() => write('/api/accountant/monthly-payments', {date: view.data.date, employee_id: person.id, amount: String(amount)}),
    'Записано: ' + person.name + ', ' + money(amount) + '. Ведомость и остаток обновлены.', {button: $('salary-submit')}).then(ok => {
    if (ok) { $('salary-employee').value = ''; $('salary-amount').value = ''; salaryHint(); }
  });
});

/* ── Правая колонка ────────────────────────────────────────────────── */
function railLine(label, value, cls) {
  return h('div', {class: cls}, h('span', {text: label}), h('b', {text: value}));
}
/* Подтверждение суммы от кассира (Функционал 2a, «Нет в макете»). Поле
   «Получено» по умолчанию = переданное кассиром (или расчёт iiko). После
   подтверждения остаток считается от полученного, а недостача — ошибка. */
let confirmEditing = false, confirmDirty = false;
function renderCashConfirm(handover) {
  const {cash} = view, form = $('cash-confirm'), done = $('cash-confirmed');
  const handed = cash.cashier !== null ? cash.cashier : cash.expected;
  // Подтверждать нужно передачу кассира; свою ручную запись бухгалтер уже ввёл сам.
  const needs = !cash.confirmedAt && (handover.source === 'cashier' || (cash.cashier === null && cash.expected !== null));
  form.hidden = !(needs || confirmEditing) || handed === null;
  if (!form.hidden && !confirmDirty) $('cash-confirm-amount').value = fmt(cash.confirmedAt ? cash.cashier : handed);
  $('cash-confirm-note').textContent = cash.confirmedAt ? 'Исправление: расчёт ' + money(cash.calculation) + '.'
    : handover.source !== 'cashier' && cash.cashier !== null ? 'Исправление записанного прихода ' + money(cash.cashier) + '.'
    : handover.source === 'cashier' ? 'Кассир передал ' + money(cash.cashier) + '. Пересчитайте и подтвердите.'
    : 'Кассир ещё не нажал «Передать» — по расчёту ' + money(cash.expected) + '.';
  $('cash-confirm-submit').disabled = busy;
  // Записанный вручную приход тоже можно исправить отсюда — «Изменить».
  const manual = !cash.confirmedAt && cash.cashier !== null && handover.source !== 'cashier';
  done.hidden = !(cash.confirmedAt || manual) || !form.hidden;
  done.classList.toggle('is-short', cash.shortfall > 0);
  done.replaceChildren(h('span', {text: cash.shortfall > 0
      ? '⚠ Получено на ' + money(cash.shortfall) + ' меньше расчёта (' + money(cash.calculation) + ')'
      : manual ? 'Приход записан бухгалтером' : '✓ Сумма от кассира подтверждена'}), ' ',
    h('button', {type: 'button', class: 'fd-link-btn', text: 'Изменить', onclick: () => { confirmEditing = true; confirmDirty = false; renderCashConfirm(handover); $('cash-confirm-amount').focus(); }}));
}
$('cash-confirm-amount').addEventListener('input', () => { confirmDirty = true; $('cash-confirm-amount').classList.remove('is-bad'); });
$('cash-confirm').addEventListener('submit', event => {
  event.preventDefault();
  if (!view) return;
  const input = $('cash-confirm-amount'), raw = input.value.trim();
  if (!raw || !/^[\d\s\u00a0\u202f.,]+$/.test(raw)) { message('Укажите полученную сумму цифрами.', true); input.classList.add('is-bad'); input.focus(); return; }
  const amount = parse(raw), day = view.data.date;
  const calc = view.cash.confirmedAt ? view.cash.calculation : (view.cash.cashier !== null ? view.cash.cashier : view.cash.expected);
  const note = calc !== null && amount < calc ? ' Не хватает ' + money(calc - amount) + ' — это видно в проверках.' : '';
  run(() => write('/api/accountant/handover/confirm', {date: day, amount: String(amount)}), 'Получено от кассира: ' + money(amount) + '.' + note,
    {button: $('cash-confirm-submit')}).then(ok => { if (ok) { confirmEditing = false; confirmDirty = false; renderAll(); } });
});

function renderRail() {
  const {cash, data, board, monthly, shoh} = view;
  const handover = data.cashier_handover || {};
  const day = data.date, missing = 'нет данных';
  $('finance-cash-total').textContent = cash.end === null ? '—' : fmt(cash.end);
  $('finance-cash-total').classList.toggle('is-negative', cash.end !== null && cash.end < 0);
  const lines = $('fd-cash-lines'); lines.replaceChildren();
  lines.append(railLine('На начало дня', cash.opening === null ? missing : fmt(cash.opening), 'fd-cash-line'),
    railLine('+ От кассира · касса ' + dm(day) + (cash.cashier === null ? ' · ожидается' : cash.confirmedAt ? ' · получено ' + cash.confirmedAt
      : handover.source === 'cashier' ? ' · передано ' + (cash.handedAt || '') + ' · подтвердите' : cash.handedAt ? ' · получено ' + cash.handedAt : ''),
      cash.cashier !== null ? fmt(cash.cashier) : cash.expected !== null ? fmt(cash.expected) : '—', 'fd-cash-line' + (cash.cashier === null ? ' is-expected' : '')));
  if (cash.receipts) lines.append(railLine('+ Прочие поступления', fmt(cash.receipts), 'fd-cash-line'));
  // Выдачи сегодня — и за вчерашнюю смену, и долги прошлых смен.
  lines.append(railLine('− Сменным · выдано сегодня', fmt(cash.shift), 'fd-cash-line'),
    railLine('− Оклады частями', fmt(cash.monthly), 'fd-cash-line'),
    railLine('− Выдано Шоху на закуп', fmt(cash.shoh), 'fd-cash-line'),
    railLine('− Прочие расходы', fmt(cash.other), 'fd-cash-line'));
  $('fd-cash-set').hidden = cash.opening !== null;
  renderCashConfirm(handover);
  $('fd-cash').classList.toggle('is-focus', focusKey === 'cash');

  const shiftDebt = Number(data.ledger.salary_debt) + board.totals.unconfirmedDebt;
  const expDebt = Number(data.ledger.manual_debt_total);
  $('all-debt-total').textContent = money(shiftDebt + monthly.remain + expDebt);
  const debts = $('fd-debt-lines'); debts.replaceChildren(
    railLine('Сменные · не выдано', fmt(shiftDebt), 'fd-debt-line'),
    railLine('Оклады · остаток месяца', fmt(monthly.remain), 'fd-debt-line'),
    railLine('Расходы · не оплачено', fmt(expDebt), 'fd-debt-line'));

  const res = data.reserves;
  $('shoh-balance-rail').textContent = shoh.hand === null ? 'не задан' : money(shoh.hand);
  $('usd-balance').textContent = res.usd.balance === null ? 'не задан' : fmt(res.usd.balance) + ' USD';
  $('dividends-balance').textContent = res.dividends.balance === null ? 'не задан' : money(res.dividends.balance);
  renderChecks(); renderDividends();
}

function renderChecks() {
  const issues = view.issues, box = $('finance-checks'); box.replaceChildren();
  const errors = issues.filter(i => i.lvl !== 'todo').length;
  const badge = $('checks-badge');
  badge.textContent = errors ? errors + ' ' + L.plural(errors, 'ошибка', 'ошибки', 'ошибок') : 'чисто';
  badge.classList.toggle('is-clean', !errors);
  if (!issues.length) { box.append(h('p', {class: 'fd-checks-ok', text: 'Ошибок не найдено.'})); return; }
  (allIssues ? issues : issues.slice(0, 5)).forEach(issue => box.append(h('button', {type: 'button', class: 'fd-check', onclick: () => focusOn(issue.target)},
    h('span', {class: 'fd-dot is-' + issue.lvl}),
    h('span', {class: 'fd-check-body'}, h('span', {class: 'fd-check-text', text: issue.text}), issue.sub ? h('span', {class: 'fd-check-sub', text: issue.sub}) : null))));
  if (issues.length > 5) box.append(h('button', {type: 'button', class: 'fd-check-more', text: allIssues ? 'Свернуть' : 'Показать все · ' + issues.length,
    onclick: () => { allIssues = !allIssues; renderChecks(); }}));
}
// Щелчок по проверке открывает нужную секцию и подсвечивает виновную строку.
function focusOn(target) {
  focusKey = target;
  if (!target) return;
  const section = target.startsWith('row:') || target === 'todo' || target === 'shift' || target === 'blocked' ? 'shift'
    : target.startsWith('buy:') || target === 'shoh' ? 'shoh' : target.startsWith('month:') ? 'salary' : null;
  if (section) setSection(section, true);
  if (target.startsWith('row:') || target === 'todo') shiftTab = 'all';
  if (target === 'blocked') shiftTab = 'err';
  renderAll();
  const el = target.startsWith('row:') ? document.querySelector('[data-key="' + target.slice(4) + '"]')
    : target === 'todo' ? document.querySelector('.fd-row.is-focus')
    : target.startsWith('buy:') || target.startsWith('month:') ? document.querySelector('[data-key="' + target + '"]')
    : target === 'cash' ? $('fd-cash') : target === 'dividends' ? $('dividends-card')
    : target === 'shoh' ? $('shoh-section') : target === 'shift' ? $('shift-section')
    : target === 'blocked' ? document.querySelector('#shift-rows .fd-row.is-blocked') : null;
  if (target === 'blocked') document.querySelectorAll('#shift-rows .fd-row.is-blocked').forEach(n => n.classList.add('is-focus'));
  // Выплат сегодня нет — подсвечиваем форму с выбранным сотрудником.
  if (target.startsWith('month:') && !el) { $('salary-employee').value = target.slice(6); salaryHint(); $('salary-payment-form').classList.add('is-focus'); $('salary-payment-form').scrollIntoView({block: 'center', behavior: 'smooth'}); }
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
$('dividends-fill-button').addEventListener('click', () => {
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
$('fd-open-tools').addEventListener('click', openTools);
$('fd-cash-set').addEventListener('click', openTools);
$('fd-tools-close').addEventListener('click', () => $('fd-tools').close());
$('fd-tools').addEventListener('click', event => { if (event.target === $('fd-tools')) $('fd-tools').close(); });
// Суммы показываем с разрядами, когда поле отпустили: 5000000 → 5 000 000.
// Так ошибку в нулях видно сразу, а общая подсказка под полем не ломает строку.
document.addEventListener('change', event => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || !input.classList.contains('fd-money')) return;
  const raw = input.value.trim();
  if (raw && /^[\d\s\u00a0\u202f.,]+$/.test(raw)) input.value = fmt(parse(raw));
});
function toolForm(id, url, body, success) {
  $(id).addEventListener('submit', event => {
    event.preventDefault();
    const form = $(id);
    if (!form.reportValidity() || !view) return;
    const note = $('fd-tools-msg'); note.hidden = true;
    // Окно модальное: сообщение страницы под ним не видно, поэтому ошибку
    // повторяем внутри окна.
    run(() => write(url, body(new FormData(form))), success, {button: form.querySelector('button[type=submit], button:not([type])')}).then(ok => {
      if (ok) { form.reset(); $('fd-tools').close(); return; }
      note.textContent = $('accountant-message').textContent; note.hidden = !note.textContent;
    });
  });
}
toolForm('handover-form', '/api/accountant/handover', f => ({date: selectedDay(), amount: String(parse(f.get('amount'))), note: f.get('note')}), 'Приход от кассира сохранён.');
toolForm('cash-opening-form', '/api/accountant/cash-opening', f => ({date: selectedDay(), amount: String(parse(f.get('amount'))), note: f.get('note')}), 'Начальный остаток бухгалтера сохранён.');
toolForm('reserve-opening-form', '/api/accountant/reserves', f => ({date: selectedDay(), account: f.get('account'), kind: 'opening', amount: String(parse(f.get('amount'))), note: f.get('note')}), 'Начальный остаток сохранён на выбранную дату.');

/* ── Загрузка дня ───────────────────────────────────────────────────── */
function renderAll() {
  if (!view) return;
  renderShift(); renderJournal(); renderShoh(); renderSalary(); renderRail();
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
    const [data, staff, buys] = await Promise.all([
      get('/api/accountant/day?date=' + day),
      get('/api/accountant/staff?date=' + S).catch(() => null),
      get('/api/accountant/shokh/purchases?date=' + day).catch(() => ({purchases: []})),
    ]);
    if (sequence !== requestNo) return;
    const board = L.shiftBoard({payday: day, staff, accruals: data.ledger.accruals, movements: data.ledger.movements});
    const blocker = L.shiftBlocker(staff, board);
    const monthly = L.monthlyBoard(data);
    const shoh = L.shohBoard(data.reserves.shoh, buys.purchases, data.ledger.movements, data.supplier_transfers, data.cashier_shokh_gives, data.shoh_pocket);
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
      // Скелет обещает, что данные вот-вот будут; при отказе он только путает.
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
  $('accountant-date').value = day;
  focusKey = null; expanded.clear(); confirmEditing = false; confirmDirty = false;
  const url = new URL(location.href); url.searchParams.set('date', day); history.replaceState(null, '', url);
  message('');
  reload().catch(() => {});
}
$('accountant-prev').addEventListener('click', () => go(L.shiftIso(selectedDay(), -1)));
$('accountant-next').addEventListener('click', () => go(L.shiftIso(selectedDay(), 1)));
$('accountant-today').addEventListener('click', () => go(today));
$('accountant-date').addEventListener('change', () => go(selectedDay()));
// Подпись дня открывает системный выбор даты: на телефоне — колесо, на ПК — календарь.
$('fd-date-label').addEventListener('click', event => {
  const input = $('accountant-date');
  if (event.target === input) return;
  event.preventDefault();
  try { input.showPicker(); } catch { input.focus(); }
});
$('fd-export').addEventListener('click', () => {
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
    const catalogRequest = fetch('/api/accountant/expenses/catalog', {cache: 'no-store'});
    catalogRequest.catch(() => {});
    today = (await globalThis.RetroConfig).today;
    const requested = new URLSearchParams(location.search).get('date');
    $('accountant-date').max = today;
    const valid = requested && /^\d{4}-\d{2}-\d{2}$/.test(requested) && requested <= today;
    $('accountant-date').value = valid ? requested : today;
    // Адрес с будущей или кривой датой не должен расходиться с показанным днём.
    if (requested && !valid) { const url = new URL(location.href); url.searchParams.set('date', today); history.replaceState(null, '', url); }
    const catalogResponse = await catalogRequest;
    if (!catalogResponse.ok) throw new Error('Не удалось загрузить наименования затрат.');
    catalog = (await catalogResponse.json()).groups;
    index = L.catalogIndex(catalog.concat([{code: 'reserves', label: 'Резервы', items: Object.entries(special).map(([code, v]) => ({code, label: v.label}))}]));
    fillCatalog();
    await loadDay();
  } catch (error) { message(error.message, true); }
})();
