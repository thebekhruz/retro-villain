const $ = id => document.getElementById(id);
const money = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
const rateOfficial = new Intl.NumberFormat('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const rateRestaurant = new Intl.NumberFormat('ru-RU', {minimumFractionDigits: 1, maximumFractionDigits: 1});
const count = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 0});
// Цвет кассы iiko: точка у строки и её отрезок в полосе долей совпадают.
const colors = ['#24594b','#91a786','#c9a45f','#b2c3aa','#6b8074','#ddd2b6'];
const ADVANCE_COLOR = '#d8b977';
const demo = new URLSearchParams(location.search).get('demo') === '1';
let config, snapshot = null, financeData = null, receiptData = null, generation = 0, controller;
// 5a: выдачи Шоху из кассы, доллары в сейф, передача бухгалтеру дня.
let shokhData = null, usdData = null, usdRate = null, handoverRecord = null, handoverBusy = false;
// Авто-строка зарплаты кассира в правке (id расхода) — сумму можно поменять за день.
let editingExpense = null;
// Готовые числа «К передаче» и «Касса за день» с сервера (/day, /summary): экран их не пересчитывает.
let summary = null;
// Выручка по кассам iiko на экране и какие кассы раскрыты (живут при обновлении дня).
let revenueView = null;
const openGroups = new Set(), allTypes = new Set();
const openPaymentDetails = new Set();
// Реестр предоплат: день, период, вкладка, группировка, строка в правке.
let prepayData = null, prepayPeriod = null, prepayTab = 'received', prepayEditing = null;
let prepayRequest = null, prepayLoadedGeneration = -1, prepayReadyGeneration = -1;
let prepayNeedsRefresh = false;
const usdFormat = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});

/* ── Отклик и ожидание (busy.js, T-393; docs/feedback-principles.md) ─────
   Экран собирается из шести частей, и приходят они порознь (iiko может
   отвечать секундами). Первая загрузка дня — скелет на месте каждой ещё не
   пришедшей цифры и строки, размеры те же. Повторная (после записи, «↻») —
   раздел гаснет, данные остаются. Кнопка, начавшая действие, крутится сама. */
const pass = (el, work) => Promise.resolve(typeof work === 'function' ? work() : work);
const Busy = globalThis.RetroBusy || {button: pass, row: pass, section: pass, flash() {}};
let waiting = new Set(CashierLogic.PARTS);
// Списки: пока часть грузится впервые — строки-заглушки в форме настоящих.
const SKELETON_LISTS = {payments: ['day', 2, 'payment'], 'prepay-rows': ['prepay', 2, 'prepay'],
  'expense-list': ['expenses', 1, 'item'], 'receipt-list': ['receipts', 1, 'item']};
// Что гаснет, пока часть перечитывается: «К передаче» считается из всех.
const SECTIONS = {day: ['payments-section', 'handover-panel'], expenses: ['expenses-section', 'handover-panel'],
  shokh: ['expenses-section', 'handover-panel'], receipts: ['receipts-section', 'handover-panel'], usd: ['usd-card', 'handover-panel'], rate: ['usd-card', 'handover-panel'],
  prepay: ['prepay-content']};
function skeletonRows(count, kind) {
  const bar = width => { const node = document.createElement('span'); node.className = 'rm-skel'; node.style.width = width; return node; };
  return Array.from({length: count}, (_, i) => {
    const row = document.createElement('div');
    row.className = ({payment: 'payment-row', prepay: 'prepay-row'}[kind] || 'expense-item') + ' cashier-skel-row';
    row.setAttribute('aria-hidden', 'true');
    const name = bar([38, 26, 44, 30, 34, 22, 40][i % 7] + '%');
    if (kind === 'prepay') {
      const who = document.createElement('span'); who.className = 'prepay-who'; who.append(name);
      row.append(who, bar('64px'), bar('48px'), bar('56px'), bar('80px'), bar('56px'));
    } else if (kind === 'payment') {
      const dot = document.createElement('span'); dot.className = 'payment-dot';
      row.append(dot, name, bar('36px'), bar([72, 64, 80][i % 3] + 'px'));
    } else row.append(name, bar('64px'));
    return row;
  });
}
function paintWaiting() {
  for (const [id, on] of Object.entries(CashierLogic.skeletonMap(waiting))) $(id).classList.toggle('is-skel', on);
  for (const [id, [part]] of Object.entries(SKELETON_LISTS)) {
    if (!waiting.has(part)) $(id).querySelectorAll('.cashier-skel-row').forEach(node => node.remove());
  }
  // Плашка смены: на телефоне у неё своя строка — держим место скелетом, чтобы экран не съезжал.
  $('shift-pill').classList.toggle('is-skel', waiting.has('day'));
  if (waiting.has('day')) $('shift-pill').hidden = false;
  else if (!snapshot) $('shift-pill').hidden = true;
  $('payment-empty').hidden = waiting.has('day') || Boolean(snapshot && revenueView?.total !== 0);
  $('prepay-empty').hidden = prepayEmptyHidden();
  // Выручка ждёт фоновые предоплаты: вместо «—» и фразы об этом — скелет числа.
  if (revenuePending()) $('revenue').classList.add('is-skel');
  $('metrics').setAttribute('aria-busy', String(waiting.has('day')));
  $('prepay-content').setAttribute('aria-busy', String(waiting.has('prepay')));
}
/* Новый день: всё, что перечислено, грузится впервые. */
function expect(parts) {
  waiting = new Set(parts);
  for (const [id, [part, rows, kind]] of Object.entries(SKELETON_LISTS)) {
    if (waiting.has(part) && !$(id).children.length) $(id).append(...skeletonRows(rows, kind));
  }
  paintWaiting();
}
function revenuePending() {
  return Boolean(snapshot && snapshot.prepayments_deferred && revenueView && revenueView.total === null);
}
function arrive(part, current) { if (current === generation && waiting.delete(part)) paintWaiting(); }
function dim(part, work) { for (const id of SECTIONS[part]) Busy.section($(id), work); return work; }
/* Часть экрана: впервые за день — скелет до ответа, повторно — раздел гаснет. */
function loadPart(part, current, work) {
  if (!waiting.has(part)) dim(part, work);
  const done = () => arrive(part, current);
  Promise.resolve(work).then(done, done);
  return work;
}
/* Только что записанная строка коротко подсвечивается зелёным. */
function flashKey(key) {
  const node = document.querySelector(`[data-busy-key="${CSS.escape(key)}"]`);
  if (node) Busy.flash(node);
}
/* × у строки: строка «в работе», после ответа плавно сворачивается, список
   перечитывается (раздел гаснет, а не пустеет). */
function removeRow(button, url, {part, reload, done, fail}) {
  const current = generation, line = button.closest('[data-busy-key]');
  const work = Busy.row(line, request(url, undefined, {method: 'DELETE'}), {collapse: true})
    .then(async () => {
      if (current !== generation) return false;
      await loadPart(part, current, reload(current));
      await refreshSummary();
      if (current === generation) done();
      return true;
    })
    .catch(error => { if (current === generation) fail(error.message); return false; });
  Busy.button(button, work, {done: false});
  return work;
}

function message(text, error = false) {
  $('message').textContent = text;
  $('message').hidden = !text;
  $('message').setAttribute('role', error ? 'alert' : 'status');
  globalThis.RetroToast?.show(text, error ? 'error' : 'ok');
}
/* «чт, 24 сентября» — как подпись даты в макете. */
function shortDay(day) {
  return new Intl.DateTimeFormat('ru-RU', {weekday:'short', day:'numeric', month:'long', timeZone:'Asia/Tashkent'}).format(new Date(day + 'T12:00:00+05:00'));
}
function formattedDay(day) {
  return new Intl.DateTimeFormat('ru-RU', {day:'numeric', month:'long', year:'numeric', timeZone:'Asia/Tashkent'}).format(new Date(day + 'T12:00:00+05:00'));
}
function previousDay(day) {
  const value = new Date(day + 'T12:00:00Z');
  value.setUTCDate(value.getUTCDate()-1);
  return value.toISOString().slice(0,10);
}
function offsetDay(day, offset) {
  const value = new Date(day + 'T12:00:00Z');
  value.setUTCDate(value.getUTCDate() - offset);
  return value.toISOString().slice(0,10);
}
function clearUsdRate(day) {
  usdRate = null;
  $('usd-restaurant').textContent = '—';
  $('usd-rate-tile').removeAttribute('title');
  $('usd-status').textContent = '';
  $('usd-status').classList.remove('is-error');
  clearUsd();
}
async function loadUsdRate(day, current, signal) {
  if (demo) { $('usd-status').textContent = 'В демонстрационном режиме курс не загружается.'; return; }
  try {
    const data = await RetroState.responseJson(
      await request(`/api/cashier/usd-rate?date=${encodeURIComponent(day)}`, signal));
    if (current !== generation) return;
    usdRate = Number(data.restaurant_rate);
    $('usd-restaurant').textContent = rateRestaurant.format(Number(data.restaurant_rate));
    $('usd-rate-tile').title = 'Курс ЦБ ' + rateOfficial.format(Number(data.official_rate)) + ' ('
      + formattedDay(data.source_date) + '), курс Retro на 1,5% ниже';
    $('usd-status').textContent = '';
    showUsd();
    await refreshSummary();
  } catch (error) {
    if (current === generation && error.name !== 'AbortError') {
      $('usd-status').textContent = error.message;
      $('usd-status').classList.add('is-error');
    }
  }
}
function clockOf(stamp, day) {
  if (!stamp) return null;
  const text = String(stamp), time = text.slice(11, 16);
  return !day || text.slice(0, 10) === day ? time : text.slice(8, 10) + '.' + text.slice(5, 7) + ' ' + time;
}
function clearUsd() {
  usdData = null;
  $('usd-deposits').replaceChildren();
  $('usd-safe').textContent = '—';
  showUsdHint();
}
function showUsdHint(text = '', error = false) {
  const hint = $('usd-hint'), value = CashierLogic.parseAmount($('usd-amount').value);
  hint.classList.toggle('is-error', error);
  if (text) { hint.textContent = text; return; }
  hint.textContent = value && usdRate ? '≈ ' + money.format(Math.round(value * usdRate)) + ' сум по курсу Retro' : '';
}
function showUsd() {
  const data = usdData;
  $('usd-deposits').replaceChildren();
  if (!data) return;
  for (const item of data.deposits) {
    const row = document.createElement('div'); row.className = 'usd-deposit'; row.dataset.busyKey = 'cash-usd:' + item.id;
    const time = document.createElement('span'); time.className = 'usd-deposit-time rm-num';
    time.textContent = clockOf(item.created_at, data.date) || 'за день';
    const value = document.createElement('strong'); value.className = 'rm-num';
    value.textContent = usdFormat.format(Number(item.amount)) + ' USD';
    const remove = document.createElement('button'); remove.className = 'usd-remove'; remove.type = 'button';
    remove.textContent = '×'; remove.setAttribute('aria-label', `Удалить взнос ${usdFormat.format(Number(item.amount))} USD`);
    remove.disabled = demo;
    remove.addEventListener('click', () => removeRow(remove,
      `/api/cashier/usd-deposits/${item.id}?date=${encodeURIComponent(data.date)}`, {part: 'usd',
        reload: current => loadUsd(data.date, current, controller.signal),
        done: () => showUsdHint('Взнос удалён'),
        fail: text => { showUsdHint(text, true); globalThis.RetroToast?.show(text, 'error'); }}));
    row.append(time, value, remove); $('usd-deposits').append(row);
  }
  $('usd-safe').textContent = data.safe_balance === null ? 'не задан' : usdFormat.format(Number(data.safe_balance)) + ' USD';
}
async function loadUsd(day, current, signal) {
  if (demo) return;
  try {
    const data = await RetroState.responseJson(
      await request(`/api/cashier/usd-deposits?date=${encodeURIComponent(day)}`, signal));
    if (current !== generation) return;
    usdData = data; showUsd();
  } catch (error) {
    if (current === generation && error.name !== 'AbortError') showUsdHint(error.message, true);
  }
}
$('usd-amount').addEventListener('input', () => showUsdHint());
$('usd-form').addEventListener('submit', event => {
  event.preventDefault();
  const day = $('report-date').value, value = CashierLogic.parseAmount($('usd-amount').value);
  if (demo || !day) return;
  if (!value) { showUsdHint('Введите сумму в долларах.', true); $('usd-amount').focus(); return; }
  Busy.button($('usd-add'), putUsd(day, value));
});
async function putUsd(day, value) {
  const current = generation;
  try {
    const data = await RetroState.responseJson(await request('/api/cashier/usd-deposits', undefined, {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({date:day, amount:String(value)})}));
    if (current !== generation) return false;
    usdData = data; $('usd-amount').value = ''; $('usd-amount').dispatchEvent(new Event('input', {bubbles: true})); showUsd();
    await refreshSummary();
    if (current !== generation) return false;
    if (data.deposit) flashKey('cash-usd:' + data.deposit.id);
    const text = 'Положено в сейф: ' + usdFormat.format(value) + ' USD. Бухгалтер видит в «Резервах».';
    showUsdHint(text); globalThis.RetroToast?.show(text);
    return true;
  } catch (error) {
    if (current === generation) { showUsdHint(error.message, true); globalThis.RetroToast?.show(error.message, 'error'); }
    return false;
  }
}
async function request(url, signal, options = {}) {
  const sender = options.method === 'POST' ? RetroFinancialWrite : fetch;
  const response = await sender(url, {...options, signal, cache:'no-store'});
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; } catch {}
    throw new Error(typeof detail === 'string' ? detail : 'Не удалось загрузить отчёт. Проверьте дату и попробуйте снова.');
  }
  return response;
}
function clearSnapshot() {
  snapshot = null;
  summary = null;
  handoverRecord = null;
  $('shift-pill').hidden = true;
  $('download').disabled = true;
  for (const id of ['revenue','receipts','average']) $(id).textContent = '—';
  $('revenue-check').hidden = true;
  revenueView = null;
  $('payments').replaceChildren();
  $('revenue-bar').replaceChildren();
  $('payments-note').hidden = true;
  $('payment-empty').hidden = false;
  $('payment-empty').querySelector('p').textContent = 'Здесь появятся оплаты за выбранный день';
  clearPrepay();
  showStatus();
  showHandover();
}
function clearFinance() {
  financeData = null;
  receiptData = null;
  $('expense-list').replaceChildren();
  $('handover-expenses').textContent = '—';
  $('expense-feedback').textContent = '';
  $('expense-feedback').classList.remove('is-error');
  $('receipt-list').replaceChildren();
  $('handover-receipts').textContent = '—';
  $('receipt-feedback').textContent = '';
  $('receipt-feedback').classList.remove('is-error');
  shokhData = null; editingExpense = null;
  $('shokh-gives').replaceChildren();
  $('shokh-pocket').textContent = '';
  $('shokh-pocket-note').hidden = true;
  $('handover-feedback').textContent = '';
  showHandover();
}
/* Числа карточки передачи — только с сервера (одна формула на всех: кассир,
   бухгалтер, учредитель, XLSX). Пока их нет — прочерк, а не своя оценка. */
/* null с сервера — «посчитать нельзя», и это не ноль: Number(null) === 0
   показал бы передачу нулём при неизвестных предоплатах. */
const summaryValue = key => (summary && snapshot && summary.snapshot_id === snapshot.snapshot_id
  && summary[key] !== null && summary[key] !== undefined ? Number(summary[key]) : null);
function currentHandover() { return summaryValue('handover'); }
let summaryRetry = -1;
/* После записи кассира (расход, поступление, выдача Шоху) — свежие итоги с
   сервера по тому же снимку iiko. Снимок истёк — перечитываем день. */
function refreshSummary() {
  if (!snapshot) return Promise.resolve(false);
  const current = generation, day = snapshot.date, id = snapshot.snapshot_id;
  const work = request(`/api/cashier/summary?date=${encodeURIComponent(day)}&snapshot_id=${id}`, controller.signal)
    .then(response => response.json())
    .then(data => { if (current === generation) { summary = data; showHandover(); } return true; })
    .catch(error => {
      if (current !== generation || error.name === 'AbortError') return false;
      if (summaryRetry !== current) { summaryRetry = current; load(); }
      else handoverMessage(error.message, true);
      return false;
    });
  Busy.section($('handover-panel'), work);
  return work;
}
function showHandover() {
  $('download').disabled = !snapshot || snapshot.stale || snapshot.refreshing || !financeData || !receiptData;
  const text = key => { const value = summaryValue(key); return value === null ? '—' : money.format(value); };
  $('demo-cash').textContent = text('demo_cash');
  // Возврат аванса гонит смену в минус, и разностная оценка предоплат
  // перестаёт работать. День при этом валиден: выручка и чеки считаются по
  // продажам. Показываем «требует проверки», а не ноль и не прочерк.
  const issue = snapshot && snapshot.prepayment_issue ? snapshot.prepayment_issue : null;
  const unknown = Boolean(issue) || (snapshot && snapshot.cash_prepayment === null);
  $('cash-prepay').textContent = unknown ? 'требует проверки' : text('cash_prepayment');
  $('prepay-issue').hidden = !issue;
  $('prepay-issue').textContent = issue ? issue + ' Выручка и чеки за день верны.' : '';
  $('handover-expenses').textContent = text('expenses');
  $('handover-shokh').textContent = text('shokh');
  $('handover-receipts').textContent = text('receipts');
  $('handover-usd').textContent = text('usd_deduction');
  // «100 USD × 11 600» под строкой не пишем: оба множителя видны в карточке
  // «Доллары в сейф» — взнос строкой и курс Retro.
  const usdIssue = summary?.usd_issue;
  $('handover-usd-issue').hidden = !usdIssue;
  $('handover-usd-issue').textContent = usdIssue || '';
  // Нулевой шаг формулы приглушён: внимание — шагам, где есть деньги.
  for (const [id, key] of [['demo-cash', 'demo_cash'], ['cash-prepay', 'cash_prepayment'],
    ['handover-receipts', 'receipts'], ['handover-expenses', 'expenses'], ['handover-shokh', 'shokh'],
    ['handover-usd', 'usd_deduction']]) {
    const value = summaryValue(key);
    $(id).closest('.handover-rule').classList.toggle('is-zero', value === 0 && !(id === 'cash-prepay' && unknown));
  }
  const result = currentHandover();
  $('handover').textContent = result === null ? '—' : money.format(result);
  $('handover-number').classList.toggle('is-negative', result !== null && result < 0);
  showHandoverAction(result);
  paintWaiting();
}
/* Кнопка «Передать бухгалтеру» и отметка «Передано в 21:40». */
function showHandoverAction(result) {
  const record = handoverRecord, view = CashierLogic.handoverView(record, result);
  const live = Boolean(snapshot && !snapshot.demo && !snapshot.stale && !snapshot.refreshing);
  const ready = !demo && live && result !== null && result >= 0 && !handoverBusy;
  const confirmed = view.state === 'confirmed';
  $('handover-action').hidden = view.state !== 'none';
  $('handover-button').disabled = !ready;
  $('handover-done').hidden = view.state === 'none';
  $('handover-diff').hidden = view.state !== 'diff' && !(confirmed && view.difference !== null);
  // «Отменить» — только пока бухгалтер не подтвердил получение (Функционал 5a).
  $('handover-undo').hidden = view.state === 'accountant' || confirmed;
  $('handover-undo').disabled = handoverBusy;
  $('handover-again').hidden = confirmed;
  $('handover-again').disabled = !ready;
  // «Передано: …» больше не про текущую сумму, раз она изменилась.
  if (view.state === 'diff' && !$('handover-feedback').classList.contains('is-error')) $('handover-feedback').textContent = '';
  if (!record) return;
  const day = snapshot ? snapshot.date : $('report-date').value;
  const at = clockOf(record.handed_at, day);
  if (confirmed) {
    const when = clockOf(record.confirmed_at, day);
    $('handover-done-text').textContent = 'Бухгалтер подтвердил: получено ' + money.format(Number(record.amount)) + ' сум'
      + (when ? ' в ' + when : '') + (view.shortfall > 0 ? ' · недостача ' + money.format(view.shortfall) : '');
    if (view.difference !== null) {
      const sign = view.difference > 0 ? '+' : '−';
      $('handover-diff-text').textContent = 'После подтверждения сумма изменилась на ' + sign
        + money.format(Math.abs(view.difference)) + ' сум. Передачу уже не изменить — скажите бухгалтеру.';
    }
    return;
  }
  $('handover-done-text').textContent = view.state === 'accountant'
    ? 'Бухгалтер записал приход ' + money.format(Number(record.amount)) + ' сум' + (at ? ' в ' + at : '')
    : 'Передано бухгалтеру' + (at ? ' в ' + at : '') + (view.state === 'diff' ? ' · ' + money.format(Number(record.amount)) + ' сум' : '');
  if (view.state === 'diff') {
    const sign = view.difference > 0 ? '+' : '−';
    $('handover-diff-text').textContent = 'С тех пор сумма изменилась на ' + sign + money.format(Math.abs(view.difference)) + ' сум.';
    $('handover-again').textContent = (view.difference > 0 ? 'Передать разницу · +' : 'Исправить передачу · −') + money.format(Math.abs(view.difference));
  }
}
function handoverMessage(text, error = false) {
  // Удачное — всплывающим сообщением, как в макете; под кнопкой остаются только ошибки.
  $('handover-feedback').textContent = error ? text : '';
  $('handover-feedback').classList.toggle('is-error', error);
  if (text) globalThis.RetroToast?.show(text, error ? 'error' : 'ok');
}
/* Итог передачи виден на месте: отметка «Передано в 21:40» (или снова кнопка
   после отмены) вспыхивает, пока кнопка, начавшая действие, крутилась. */
async function handOver() {
  const day = $('report-date').value, current = generation, amount = currentHandover();
  if (demo || !snapshot || amount === null || handoverBusy) return false;
  handoverBusy = true; showHandover();
  let ok = false;
  try {
    const data = await RetroState.responseJson(await request('/api/cashier/handover', undefined, {method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({date:day, snapshot_id:snapshot.snapshot_id, expected:amount.toFixed(2)})}));
    if (current !== generation) return false;
    handoverRecord = data.handover; ok = true;
    handoverMessage('Передано бухгалтеру: ' + money.format(Number(data.handover.amount)) + ' сум.');
  } catch (error) {
    if (current !== generation) return false;
    handoverMessage(error.message, true);
    // Сумма разошлась или снимок устарел — показываем свежие цифры, передать можно снова.
    await Promise.all([loadPart('expenses', current, loadExpenses(day, current, controller.signal)),
      loadPart('receipts', current, loadReceipts(day, current, controller.signal)),
      loadPart('shokh', current, loadShokh(day, current, controller.signal))]);
    await refreshSummary();
    if (/iiko|Обновите/.test(error.message)) load({refresh:true});
  } finally {
    handoverBusy = false;
    if (current === generation) showHandover();
  }
  if (ok && current === generation) Busy.flash($('handover-done'));
  return ok;
}
async function undoHandover() {
  const day = $('report-date').value, current = generation;
  if (demo || handoverBusy) return false;
  handoverBusy = true; showHandover();
  let ok = false;
  try {
    await request(`/api/cashier/handover?date=${encodeURIComponent(day)}`, undefined, {method:'DELETE'});
    if (current !== generation) return false;
    handoverRecord = null; ok = true;
    handoverMessage('Передача отменена. Бухгалтер снова видит сумму как ожидаемую.');
  } catch (error) {
    if (current === generation) handoverMessage(error.message, true);
  } finally {
    handoverBusy = false;
    if (current === generation) showHandover();
  }
  if (ok && current === generation) Busy.flash($('handover-action'));
  return ok;
}
for (const id of ['handover-button', 'handover-again']) $(id).addEventListener('click', event => Busy.button(event.currentTarget, handOver(), {done: false}));
$('handover-undo').addEventListener('click', event => Busy.button(event.currentTarget, undoHandover(), {done: false}));

/* ── Выдать на закуп (Шоху) ───────────────────────────────────────────────
   Выдача уходит из передачи бухгалтеру своим шагом формулы и попадает в
   подотчёт Шоха. Рядом с полем — сколько у него уже на руках. */
function showShokh(data) {
  shokhData = data;
  $('shokh-gives').replaceChildren();
  const pocket = data.pocket === null || data.pocket === undefined ? null : Number(data.pocket);
  $('shokh-pocket-note').hidden = pocket === null;
  $('shokh-pocket').textContent = pocket === null ? '' : money.format(pocket);
  for (const item of data.gives) {
    const row = document.createElement('div'); row.className = 'expense-item is-shokh'; row.dataset.busyKey = 'cash-give:' + item.id;
    const name = document.createElement('span'); name.className = 'expense-item-name';
    const at = clockOf(item.created_at, data.date);
    name.textContent = 'Шоху на закуп' + (at ? ' · ' + at : '');
    const value = document.createElement('span'); value.className = 'expense-item-value'; value.textContent = money.format(Number(item.amount));
    const remove = document.createElement('button'); remove.className = 'expense-remove'; remove.type = 'button';
    remove.textContent = '×'; remove.setAttribute('aria-label', `Удалить выдачу Шоху ${money.format(Number(item.amount))}`);
    remove.addEventListener('click', () => removeRow(remove,
      `/api/cashier/shokh/${item.id}?date=${encodeURIComponent(data.date)}`, {part: 'shokh',
        reload: current => loadShokh(data.date, current, controller.signal),
        done: () => entryFeedback('expense', 'Выдача Шоху удалена'),
        fail: text => entryFeedback('expense', text, true)}));
    row.append(name, value, remove); $('shokh-gives').append(row);
  }
  showHandover();
}
async function loadShokh(day, current, signal) {
  if (demo) { showShokh({date:day, gives:[], total:'0', pocket:null}); return; }
  try {
    const data = await RetroState.responseJson(await request(`/api/cashier/shokh?date=${encodeURIComponent(day)}`, signal));
    if (current === generation) showShokh(data);
  } catch (error) {
    if (current === generation && error.name !== 'AbortError') {
      shokhData = null; showHandover();
      $('expense-feedback').textContent = error.message;
      $('expense-feedback').classList.add('is-error');
    }
  }
}
$('shokh-form').addEventListener('submit', event => {
  event.preventDefault();
  const day = $('report-date').value, value = CashierLogic.parseAmount($('shokh-amount').value);
  if (demo || !day) return;
  if (!value) { entryFeedback('expense', 'Укажите сумму на закуп.', true); $('shokh-amount').focus(); return; }
  Busy.button($('shokh-give'), giveShokh(day, value));
});
async function giveShokh(day, value) {
  const current = generation;
  entryFeedback('expense', '');
  try {
    const data = await RetroState.responseJson(await request('/api/cashier/shokh', undefined, {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({date:day, amount:String(value)})}));
    if (current !== generation) return false;
    $('shokh-amount').value = ''; $('shokh-amount').dispatchEvent(new Event('input', {bubbles: true}));
    showShokh(data);
    await refreshSummary();
    if (current !== generation) return false;
    if (data.give) flashKey('cash-give:' + data.give.id);
    const text = 'Выдано на закуп ' + money.format(value) + ' сум.';
    entryFeedback('expense', text); globalThis.RetroToast?.show(text);
    return true;
  } catch (error) {
    if (current === generation) { entryFeedback('expense', error.message, true); globalThis.RetroToast?.show(error.message, 'error'); }
    return false;
  }
}
/* Строка под формой расходов / поступлений: итог последнего действия. */
function entryFeedback(kind, text, error = false) {
  const node = $(kind + '-feedback');
  node.textContent = text;
  node.classList.toggle('is-error', error);
}
function showReceipts(data) {
  receiptData = data;
  $('receipt-list').replaceChildren();
  for (const item of data.receipts) {
    const row = document.createElement('div'); row.className = 'expense-item'; row.dataset.busyKey = 'cash-rcp:' + item.id;
    const name = document.createElement('span'); name.className = 'expense-item-name'; name.textContent = item.description;
    const value = document.createElement('span'); value.className = 'expense-item-value'; value.textContent = money.format(Number(item.amount));
    const remove = document.createElement('button'); remove.className = 'expense-remove'; remove.type = 'button';
    remove.textContent = '×'; remove.setAttribute('aria-label', `Удалить поступление «${item.description}»`);
    remove.addEventListener('click', () => removeEntry('receipt', item.id, data.date, remove));
    row.append(name, value, remove); $('receipt-list').append(row);
  }
  showHandover();
}
async function loadReceipts(day, current, signal) {
  if (demo) { showReceipts({date:day, receipts:[], total:'0'}); return; }
  try {
    const data = await (await request(`/api/cashier/receipts?date=${encodeURIComponent(day)}`, signal)).json();
    if (current === generation) showReceipts(data);
  } catch (error) {
    if (current === generation && error.name !== 'AbortError') {
      receiptData = null; showHandover();
      $('receipt-feedback').textContent = error.message;
      $('receipt-feedback').classList.add('is-error');
      globalThis.RetroToast?.show(error.message, 'error');
    }
  }
}
function showExpenses(data) {
  financeData = data;
  $('expense-list').replaceChildren();
  for (const item of data.expenses) {
    const row = document.createElement('div'); row.className = 'expense-item';
    if (item.id != null) row.dataset.busyKey = 'cash-exp:' + item.id;
    const name = document.createElement('span'); name.className = 'expense-item-name'; name.textContent = item.description;
    const value = document.createElement('span'); value.className = 'expense-item-value'; value.textContent = money.format(Number(item.amount));
    if (item.automatic) {
      // Авто-строка (зарплата кассира): удалить нельзя, сумму за день — можно.
      const marker = document.createElement('small'); marker.className = 'expense-automatic'; marker.textContent = 'авто';
      marker.title = 'Добавляется автоматически каждый день';
      name.append(' ', marker);
      if (editingExpense === item.id) row.append(name, automaticEditor(item, data.date));
      else {
        const edit = document.createElement('button'); edit.className = 'expense-edit'; edit.type = 'button';
        edit.textContent = '✎'; edit.setAttribute('aria-label', `Изменить сумму «${item.description}»`);
        edit.disabled = demo;
        edit.addEventListener('click', () => { editingExpense = item.id; showExpenses(financeData); });
        row.append(name, value, edit);
      }
    } else {
      const remove = document.createElement('button'); remove.className = 'expense-remove'; remove.type = 'button';
      remove.textContent = '×'; remove.setAttribute('aria-label', `Удалить расход «${item.description}»`);
      remove.addEventListener('click', () => removeEntry('expense', item.id, data.date, remove));
      row.append(name, value, remove);
    }
    $('expense-list').append(row);
  }
  showHandover();
}
/* Правка суммы авто-строки: поле на месте суммы, ✓ сохраняет, Esc и × отменяют. */
function automaticEditor(item, day) {
  const form = document.createElement('form'); form.className = 'expense-edit-form';
  const input = document.createElement('input'); input.type = 'text'; input.inputMode = 'numeric'; input.autocomplete = 'off';
  input.value = String(Number(item.amount)); input.setAttribute('aria-label', `Сумма «${item.description}», сум`);
  const save = document.createElement('button'); save.type = 'submit'; save.className = 'expense-save-edit';
  save.textContent = '✓'; save.setAttribute('aria-label', 'Сохранить сумму');
  const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'expense-remove';
  cancel.textContent = '×'; cancel.setAttribute('aria-label', 'Отменить правку');
  const close = () => { editingExpense = null; showExpenses(financeData); };
  cancel.addEventListener('click', close);
  input.addEventListener('keydown', event => { if (event.key === 'Escape') close(); });
  form.addEventListener('submit', event => {
    event.preventDefault();
    const text = input.value.replace(/[\s ]/g, '');
    const value = /^0+([.,]0{1,2})?$/.test(text) ? 0 : CashierLogic.parseAmount(input.value);
    if (value === null) { entryFeedback('expense', 'Укажите сумму: 0 или больше.', true); input.focus(); return; }
    Busy.button(save, saveAutomatic(item.id, day, value));
  });
  form.append(input, save, cancel);
  requestAnimationFrame(() => { input.focus({preventScroll: true}); input.select(); });
  return form;
}
async function saveAutomatic(id, day, value) {
  const current = generation;
  entryFeedback('expense', '');
  try {
    await RetroState.responseJson(await request(`/api/cashier/expenses/${id}`, undefined, {method: 'PATCH',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({date: day, amount: String(value)})}));
    if (current !== generation) return false;
    editingExpense = null;
    await loadPart('expenses', current, loadExpenses(day, current, controller.signal));
    await refreshSummary();
    if (current !== generation) return false;
    flashKey('cash-exp:' + id);
    entryFeedback('expense', 'Сумма сохранена'); globalThis.RetroToast?.show('Сумма сохранена');
    return true;
  } catch (error) {
    if (current === generation) { entryFeedback('expense', error.message, true); globalThis.RetroToast?.show(error.message, 'error'); }
    return false;
  }
}
async function loadExpenses(day, current, signal) {
  if (demo) {
    showExpenses({date:day, expenses:[], total:'0', expense_policy_configured:true});
    $('expense-feedback').textContent = 'В демонстрационном режиме расходы не сохраняются.';
    return;
  }
  try {
    const data = await (await request(`/api/cashier/expenses?date=${encodeURIComponent(day)}`, signal)).json();
    if (current === generation) showExpenses(data);
  } catch (error) {
    if (current === generation && error.name !== 'AbortError') {
      financeData = null; showHandover();
      $('expense-feedback').textContent = error.message;
      $('expense-feedback').classList.add('is-error');
      globalThis.RetroToast?.show(error.message, 'error');
    }
  }
}
function showStatus() {
  const status = $('connection');
  if (!config) return;
  const base = demo ? 'Демонстрация' : config.configured ? 'iiko подключён' : 'iiko не подключён';
  if (!snapshot || !snapshot.fetched_at) { status.textContent = base; return; }
  const time = new Intl.DateTimeFormat('ru-RU',{hour:'2-digit',minute:'2-digit',timeZone:'Asia/Tashkent'}).format(new Date(snapshot.fetched_at));
  const source = snapshot.demo ? 'Демонстрация · пример' : snapshot.source === 'database' ? 'Сохранённый отчёт iiko ·' : base + ' · обновлено';
  // Фоновое обновление iiko — здесь и крутящейся «↻», без плашки, которая сдвигала бы экран.
  const tail = snapshot.refreshing ? ' · обновляем iiko…' : snapshot.stale ? ' · требуют обновления' : '';
  status.textContent = `${source} ${time}${tail}`;
}
/* Сверяем оплаты по кассам + полученные авансы с дневным итогом. */
function showCheck(node, view) {
  const known = Boolean(view && view.match !== null);
  node.hidden = !known;
  if (!known) return;
  node.classList.toggle('is-off', !view.match);
  node.title = view.match ? 'Сумма по кассам сходится с итогом iiko' : 'Сумма по кассам не сходится с итогом iiko';
  if (view.match) { node.replaceChildren('✓'); return; }
  const diff = document.createElement('span'); diff.className = 'rm-num';
  diff.textContent = view.difference === null ? '' : (view.difference > 0 ? '+' : '−') + money.format(Math.abs(view.difference));
  node.replaceChildren('Расхождение ', diff);
}
const percentText = value => value === null ? '' : value.toLocaleString('ru-RU', {minimumFractionDigits: 1, maximumFractionDigits: 1}) + '%';
/* Доли касс одной полосой: отрезок того же цвета, что точка у строки;
   точная доля — в подсказке при наведении, а не текстом в каждой строке. */
function renderBar(parts) {
  const bar = $('revenue-bar');
  bar.replaceChildren(...parts.filter(part => part.total > 0).map(part => {
    const piece = document.createElement('span');
    piece.style.setProperty('--color', part.color);
    piece.style.flexGrow = String(part.total);
    piece.title = part.name + (part.percent === null ? '' : ' · ' + percentText(part.percent));
    // Названия касс — из iiko, как в строках таблицы; переводчик их не трогает.
    if (!part.translate) piece.dataset.i18n = 'off';
    return piece;
  }));
}
/* Кассы iiko аккордеоном (ТЗ 3.1): по умолчанию всё свёрнуто — сверка с iiko
   идёт по итогам касс; раскрытая касса показывает типы с деньгами, нулевые —
   по «показать все». */
function renderGroups() {
  const view = revenueView, list = $('payments');
  list.replaceChildren();
  if (!view || view.total === 0) { renderBar([]); return; }
  const parts = [];
  view.groups.forEach((group, index) => {
    const color = colors[index % colors.length];
    parts.push({name: group.name, total: group.total, percent: group.percent, color, translate: view.fallback});
    const box = document.createElement('div'); box.className = 'pay-group';
    const open = openGroups.has(group.name) || view.groups.length === 1;
    box.classList.toggle('is-open', open);
    const head = document.createElement('button'); head.type = 'button'; head.className = 'payment-row pay-group-head';
    head.setAttribute('aria-expanded', String(open));
    const chevron = document.createElement('span'); chevron.className = 'rm-section-chevron pay-group-chevron';
    chevron.setAttribute('aria-hidden', 'true'); chevron.textContent = '⌄';
    head.style.setProperty('--color', color);
    const name = document.createElement('span'); name.className = 'payment-name';
    const dot = document.createElement('span'); dot.className = 'pay-group-dot'; dot.setAttribute('aria-hidden', 'true');
    const label = document.createElement('span'); label.className = 'payment-label'; label.textContent = group.name;
    if (!view.fallback) label.dataset.i18n = 'off';
    name.append(dot, label);
    const value = document.createElement('strong'); value.className = 'payment-value'; value.textContent = money.format(group.total);
    if (!group.total) head.classList.add('is-zero');
    head.append(chevron, name, value);
    head.addEventListener('click', () => {
      if (openGroups.has(group.name)) openGroups.delete(group.name); else openGroups.add(group.name);
      renderGroups();
    });
    const body = document.createElement('div'); body.className = 'pay-group-body'; body.hidden = !open;
    const showAll = allTypes.has(group.name);
    for (const type of group.types) {
      if (type.zero && !showAll) continue;
      body.append(typeRow(type, color));
    }
    if (group.hidden) {
      const more = document.createElement('button'); more.type = 'button'; more.className = 'pay-group-more';
      const text = document.createElement('span'); text.textContent = showAll ? 'Скрыть типы без оплат' : 'Показать все типы';
      const extra = document.createElement('small'); extra.className = 'rm-num'; extra.textContent = showAll ? '' : '+' + group.hidden;
      more.append(text, extra);
      more.addEventListener('click', () => {
        if (allTypes.has(group.name)) allTypes.delete(group.name); else allTypes.add(group.name);
        renderGroups();
      });
      body.append(more);
    }
    box.append(head, body); list.append(box);
  });
  if (view.unassignedTotal !== 0) {
    const percent = view.total > 0 && view.unassignedTotal !== null ? Math.round(view.unassignedTotal / view.total * 1000) / 10 : null;
    const advances = typeRow({name: 'unassigned', label: 'Предоплаты · способ не указан', isPrepayment: true,
      total: view.unassignedTotal, advances: view.unassigned, percent}, ADVANCE_COLOR);
    advances.id = 'received-payment-row';
    advances.classList.add('is-advance');
    parts.push({name: 'Предоплаты · способ не указан', total: view.unassignedTotal || 0, percent, color: ADVANCE_COLOR, translate: true});
    list.append(advances);
  }
  renderBar(parts);
}
function typeRow(type, color) {
  const row = document.createElement('div'); row.className = 'payment-row pay-type'; row.style.setProperty('--color', color);
  const dot = document.createElement('span'); dot.className = 'payment-dot';
  const name = document.createElement('span'); name.className = 'payment-name';
  const label = document.createElement('span'); label.className = 'payment-label'; label.textContent = type.label;
  if (!type.isCash && !type.isPrepayment) label.dataset.i18n = 'off';
  name.append(label);
  // В iiko наличные называются «Демо»; на экране — «Наличные», и строка
  // подсвечена: именно она уходит бухгалтеру первой строкой формулы.
  if (type.isCash) row.classList.add('is-cash');
  if (type.zero) row.classList.add('is-zero');
  const value = document.createElement('strong'); value.className = 'payment-value';
  value.textContent = type.total === null ? '—' : money.format(type.total);
  row.append(dot, name, value);
  if (!type.advances?.length) return row;
  const detail = paymentDisclosure(row, 'method:' + type.name);
  detail.dataset.paymentMethod = type.name;
  if (!type.isPrepayment) {
    const paid = typeRow({label: 'Оплаты счетов', isPrepayment: true, total: type.paid, percent: null}, color);
    paid.classList.add('pay-detail-row'); detail.append(paid);
    const received = typeRow({label: 'Полученные предоплаты', isPrepayment: true, total: type.received, percent: null}, color);
    received.classList.add('pay-detail-row');
    const deposits = paymentDisclosure(received, 'advances:' + type.name);
    deposits.classList.add('pay-advances');
    deposits.append(...type.advances.map(paymentAdvanceRow));
    detail.append(deposits);
  } else detail.append(...type.advances.map(paymentAdvanceRow));
  return detail;
}

function paymentDisclosure(row, key) {
  const detail = document.createElement('details'); detail.className = 'pay-detail';
  detail.open = openPaymentDetails.has(key);
  const head = document.createElement('summary'); head.className = row.className;
  head.style.cssText = row.style.cssText;
  head.append(...row.childNodes);
  const arrow = head.querySelector('.payment-dot');
  arrow.className = 'pay-detail-chevron'; arrow.textContent = '›'; arrow.setAttribute('aria-hidden', 'true');
  detail.append(head);
  detail.addEventListener('toggle', () => {
    if (!detail.isConnected) return;
    if (detail.open) openPaymentDetails.add(key); else openPaymentDetails.delete(key);
  });
  return detail;
}

function paymentAdvanceRow(entry) {
  const row = document.createElement('div'); row.className = 'pay-advance-row';
  const info = document.createElement('span');
  const label = document.createElement('span'); label.dataset.i18n = 'off';
  label.textContent = entry.guest || (entry.order_number ? '№' + entry.order_number : '—');
  const stamp = document.createElement('small'); stamp.textContent = stampText(entry.received_at);
  info.append(label, stamp);
  const amount = document.createElement('strong'); amount.className = 'rm-num'; amount.textContent = money.format(entry.amount);
  row.append(info, amount);
  return row;
}

function refreshPaymentGroups() {
  if (!snapshot) return;
  revenueView = CashierLogic.revenueGroups(snapshot, prepayData);
  renderGroups();
}

/* ── Реестр предоплат (ТЗ 3.2) ────────────────────────────────────────────
   Сумма и время аванса — из iiko; кто внёс, телефон, на какую дату и способ
   вписывает кассир (✎ у строки), там же — отметка возврата. */
const dayMonth = day => day ? String(day).slice(8, 10) + '.' + String(day).slice(5, 7) : '';
function stampText(stamp) {
  if (!stamp) return '';
  const text = String(stamp);
  return dayMonth(text.slice(0, 10)) + ' ' + text.slice(11, 16);
}
function clearPrepay() {
  prepayData = null; prepayPeriod = null; prepayEditing = null;
  $('prepay-rows').replaceChildren();
  $('prepay-note').hidden = true;
  showPrepay();
}
async function loadPrepay(day, current, signal, refresh = false) {
  const baseId = snapshot?.snapshot_id;
  try {
    const data = await RetroState.responseJson(
      await request(`/api/cashier/prepayments?date=${encodeURIComponent(day)}&demo=${demo}&refresh=${refresh}`,
        signal, {retroBusy: false}));
    if (current !== generation || signal.aborted) return;
    prepayData = data;
    // The registry supplies its coherent daily revision (including export id).
    // A later day response must not be replaced by an older registry request.
    if (data.snapshot?.date === day && snapshot?.snapshot_id === baseId) show(data.snapshot);
  } catch (error) {
    if (current !== generation || signal.aborted || error.name === 'AbortError') return;
    prepayData = {date: day, received: [], credited: [], received_total: null, credited_total: null, issue: error.message, failed: true};
    if (snapshot?.snapshot_id === baseId && snapshot?.prepayments_deferred)
      show({...snapshot, prepayments_deferred: false, prepayments_issue: error.message});
  }
  refreshPaymentGroups();
  showPrepay();
}
function resetPrepayLoading(refresh = false) {
  prepayRequest?.controller.abort(); prepayRequest = null;
  prepayLoadedGeneration = -1; prepayReadyGeneration = -1;
  prepayNeedsRefresh = refresh;
  waiting.delete('prepay'); paintWaiting();
}
/* Реестр читается в фоне после основных блоков и их отрисовки. Раскрытие
   до этого момента показывает ожидание, но не запускает источник раньше. */
function ensurePrepay(refresh = false) {
  if (refresh) {
    prepayNeedsRefresh = true;
    prepayLoadedGeneration = -1;
    prepayRequest?.controller.abort(); prepayRequest = null;
    arrive('prepay', generation);
  }
  const day = $('report-date').value;
  if (config && !config.configured && !demo) return;
  if (prepayReadyGeneration !== generation || snapshot?.date !== day) {
    if (prepaySection.open) expect([...waiting, 'prepay']);
    return;
  }
  if (!refresh && prepayRequest?.current === generation) return prepayRequest.work;
  if (!refresh && prepayLoadedGeneration === generation && !prepayData?.failed) return;
  prepayRequest?.controller.abort();
  const active = {current: generation, controller: new AbortController()};
  prepayRequest = active;
  if (!prepayData) expect([...waiting, 'prepay']);
  active.work = loadPrepay(day, active.current, active.controller.signal, prepayNeedsRefresh).finally(() => {
    if (prepayRequest !== active) return;
    prepayRequest = null; prepayLoadedGeneration = active.current;
    arrive('prepay', active.current); showPrepay();
  });
  prepayNeedsRefresh = false;
  if (prepayData) dim('prepay', active.work);
  return active.work;
}
function loadPrepayPeriod() {
  const from = $('prepay-from').value, to = $('prepay-to').value, current = generation;
  if (!from || !to) return Promise.resolve(false);
  const work = request(`/api/cashier/prepayments?start=${encodeURIComponent(from)}&end=${encodeURIComponent(to)}&demo=${demo}`,
    controller.signal).then(response => RetroState.responseJson(response))
    .then(data => { if (current === generation) { prepayPeriod = data; showPrepay(); } return true; })
    .catch(error => {
      if (current === generation && error.name !== 'AbortError') {
        prepayPeriod = {start: from, end: to, rows: [], issue: error.message, failed: true}; showPrepay();
      }
      return false;
    });
  Busy.section($('prepay-section'), work);
  return work;
}
/* Способы для подсказки в правке — типы оплат iiko за день («Демо» — «Наличные»). */
function prepayMethods() {
  let list = $('prepay-methods');
  if (!list) { list = document.createElement('datalist'); list.id = 'prepay-methods'; document.body.append(list); }
  const names = new Set(['Наличные']);
  for (const group of revenueView?.groups || []) for (const type of group.types) names.add(type.label);
  list.replaceChildren(...[...names].map(name => { const option = document.createElement('option'); option.value = name; return option; }));
}
function prepayRows() {
  if (prepayTab === 'period') return prepayPeriod ? prepayPeriod.rows : null;
  return prepayData ? prepayData[prepayTab] : null;
}
function showPrepay() {
  const day = snapshot?.date || $('report-date').value, today = config && day === config.today;
  // На прошедший день «сегодня» неправда — подпись меняется вместе с датой.
  const labels = {received: today ? 'Получено сегодня' : 'Получено за день', credited: today ? 'Зачтено сегодня' : 'Зачтено за день'};
  for (const [key, text] of Object.entries(labels)) {
    const tab = document.querySelector(`.prepay-tabs [data-tab="${key}"] .prepay-tab-name`);
    if (tab.dataset.label !== text) { tab.dataset.label = text; tab.replaceChildren(text); }
  }
  $('prepay-count-received').textContent = prepayData && !prepayData.failed ? count.format(prepayData.received.length) : '—';
  $('prepay-count-credited').textContent = prepayData && !prepayData.failed ? count.format(prepayData.credited.length) : '—';
  for (const tab of document.querySelectorAll('.prepay-tabs [data-tab]')) {
    const active = tab.dataset.tab === prepayTab;
    tab.classList.toggle('is-active', active); tab.setAttribute('aria-selected', String(active));
  }
  $('prepay-period').hidden = prepayTab !== 'period';
  const rows = prepayRows(), list = $('prepay-rows');
  if (!waiting.has('prepay')) list.replaceChildren();
  const known = Array.isArray(rows);
  if (known && rows.length) {
    if (prepayTab === 'period') {
      for (const group of CashierLogic.prepaymentGroups(rows)) {
        const head = document.createElement('div'); head.className = 'prepay-group-head';
        const title = document.createElement('span');
        title.textContent = group.title || 'Без имени';
        if (group.title) title.dataset.i18n = 'off';
        const total = document.createElement('strong'); total.className = 'rm-num'; total.textContent = money.format(group.total);
        head.append(title, total); list.append(head);
        for (const row of group.rows) list.append(...prepayLine(row, 'period'));
      }
    } else for (const row of rows) list.append(...prepayLine(row, prepayTab));
  }
  const empty = {received: 'Предоплат за этот день нет.', credited: 'Зачтённых предоплат за этот день нет.',
    period: prepayPeriod ? 'За этот период предоплат нет.' : 'Выберите период и нажмите «Показать».'}[prepayTab];
  $('prepay-empty').textContent = empty;
  $('prepay-empty').hidden = prepayEmptyHidden();
  const issue = prepayTab === 'period' ? prepayPeriod?.issue : prepayData?.issue;
  $('prepay-note').hidden = !issue;
  $('prepay-note').textContent = issue || '';
  const total = known ? rows.reduce((sum, row) => sum + (CashierLogic.prepaymentAmount(row, prepayTab) || 0), 0) : null;
  const totalLabel = {received: 'Итого получено', credited: 'Итого зачтено', period: 'Итого за период'}[prepayTab];
  if ($('prepay-total-label').dataset.label !== totalLabel) {
    $('prepay-total-label').dataset.label = totalLabel; $('prepay-total-label').replaceChildren(totalLabel);
  }
  $('prepay-total').textContent = total === null ? '—' : money.format(total);
  // Итог под одной строкой повторил бы её сумму — показываем его от двух строк.
  $('prepay-total').closest('.cashier-total').hidden = !known || rows.length < 2;
}
/* «Предоплат нет» — только когда реестр пришёл и он пуст (или период ещё не выбран). */
function prepayEmptyHidden() {
  if (prepayTab === 'period') return Boolean(prepayPeriod && prepayPeriod.rows.length);
  const rows = prepayRows();
  return waiting.has('prepay') || prepayData?.failed || !Array.isArray(rows) || rows.length > 0;
}
function prepayLine(row, tab) {
  const line = document.createElement('div'); line.className = 'prepay-row'; line.dataset.busyKey = 'prepay:' + row.order_number;
  const status = CashierLogic.prepaymentStatus(row, config?.today);
  const who = document.createElement('span'); who.className = 'prepay-who';
  const guest = document.createElement('strong');
  if (row.guest) { guest.textContent = row.guest; guest.dataset.i18n = 'off'; }
  else { guest.textContent = '—'; guest.classList.add('is-empty'); guest.title = 'Кто внёс — не указано'; }
  const meta = document.createElement('small');
  if (row.phone) { const phone = document.createElement('span'); phone.textContent = row.phone; phone.dataset.i18n = 'off'; meta.append(phone, ' · '); }
  const order = document.createElement('span'); order.textContent = 'заказ';
  meta.append(order, ' ', String(row.order_number).startsWith('iiko-') ? '—' : '№' + row.order_number);
  who.append(guest, meta);
  // У каждого факта своя подпись: на узком экране строка становится карточкой
  // и подписи видны, в широкой таблице их заменяет шапка.
  const fact = (cls, label, value, empty = false, raw = false) => {
    const node = document.createElement('span'); node.className = 'prepay-cell ' + cls + (empty ? ' is-empty' : '');
    const name = document.createElement('span'); name.className = 'prepay-cell-label'; name.textContent = label;
    const text = document.createElement('span'); text.className = 'prepay-cell-value'; text.textContent = value;
    if (raw) text.dataset.i18n = 'off';
    node.append(name, text); return node;
  };
  const received = fact('prepay-received', 'Получено', row.received_at ? stampText(row.received_at) : '—', !row.received_at);
  // Способ из iiko называет наличные «Демо» — на экране они «Наличные», как в выручке.
  const methodText = String(row.method_shown || '').split(', ').filter(Boolean)
    .map(name => name === CashierLogic.CASH_PAYMENT ? 'Наличные' : name).join(', ');
  const method = fact('prepay-method', 'Способ', methodText || 'не указан', !methodText, Boolean(methodText) && methodText !== 'Наличные');
  const amount = CashierLogic.prepaymentAmount(row, tab);
  const sum = fact('prepay-amount rm-num', 'Сумма', amount === null ? '—' : money.format(amount));
  const pill = document.createElement('span'); pill.className = 'rm-pill prepay-status is-' + status.key;
  pill.append(status.text);
  if (status.key === 'credited' && status.day) pill.append(' ' + dayMonth(status.day));
  const edit = document.createElement('button'); edit.type = 'button'; edit.className = 'prepay-edit';
  edit.textContent = '✎'; edit.setAttribute('aria-label', 'Вписать, кто и на какую дату');
  edit.setAttribute('aria-expanded', String(prepayEditing === row.order_number));
  edit.disabled = demo;
  edit.addEventListener('click', () => { prepayEditing = prepayEditing === row.order_number ? null : row.order_number; showPrepay(); });
  const state = document.createElement('span'); state.className = 'prepay-cell prepay-state'; state.append(pill);
  line.append(who, received, method, sum, state, edit);
  if (prepayEditing !== row.order_number) return [line];
  line.classList.add('is-editing');
  return [line, prepayEditor(row)];
}
function prepayEditor(row) {
  const form = document.createElement('form'); form.className = 'prepay-editor';
  const field = (label, input) => {
    const wrap = document.createElement('label'); wrap.className = 'prepay-field';
    const text = document.createElement('span'); text.textContent = label;
    wrap.append(text, input); return wrap;
  };
  const input = (name, value, attrs = {}) => {
    const node = document.createElement('input'); node.name = name; node.value = value || '';
    for (const [key, val] of Object.entries(attrs)) node.setAttribute(key, val);
    return node;
  };
  const guest = input('guest', row.guest, {maxlength: 120, placeholder: 'Имя гостя или компания', autocomplete: 'off'});
  const phone = input('phone', row.phone, {maxlength: 40, type: 'tel', placeholder: '+998', autocomplete: 'off'});
  // Способ внесения, определённый по iiko, кассир не переписывает — поле только для чтения.
  const fromIiko = (row.received_methods || []).map(name => name === CashierLogic.CASH_PAYMENT ? 'Наличные' : name).join(', ');
  const method = fromIiko
    ? input('method', fromIiko, {readonly: '', 'aria-readonly': 'true', title: 'Способ внесения — из iiko'})
    : input('method', row.method, {maxlength: 60, list: 'prepay-methods', placeholder: 'Наличные, карта…', autocomplete: 'off'});
  const refund = document.createElement('label'); refund.className = 'prepay-refund';
  const box = document.createElement('input'); box.type = 'checkbox'; box.name = 'refunded'; box.checked = Boolean(row.refunded);
  const refundText = document.createElement('span'); refundText.textContent = 'Возврат предоплаты';
  refund.append(box, refundText);
  const actions = document.createElement('div'); actions.className = 'prepay-editor-actions';
  const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'button secondary'; cancel.textContent = 'Отмена';
  const save = document.createElement('button'); save.type = 'submit'; save.className = 'button primary'; save.textContent = 'Сохранить';
  actions.append(cancel, save);
  const feedback = document.createElement('p'); feedback.className = 'expense-feedback'; feedback.setAttribute('role', 'status');
  form.append(field('Кто', guest), field('Телефон', phone),
    field(fromIiko ? 'Способ · из iiko' : 'Способ', method), refund, actions, feedback);
  cancel.addEventListener('click', () => { prepayEditing = null; showPrepay(); });
  form.addEventListener('submit', event => {
    event.preventDefault();
    Busy.button(save, savePrepay(row.order_number, {guest: guest.value, phone: phone.value, // «На дату» скрыта (13.10.2026): записанную раньше дату не стираем.
      event_day: row.event_day || null,
      method: fromIiko ? row.method || '' : method.value, refunded: box.checked}, feedback));
  });
  requestAnimationFrame(() => guest.focus({preventScroll: true}));
  return form;
}
async function savePrepay(order, body, feedback) {
  const current = generation;
  try {
    const row = await RetroState.responseJson(await request(`/api/cashier/prepayments/${encodeURIComponent(order)}`, undefined, {
      method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}));
    if (current !== generation) return false;
    const swap = list => list && list.forEach((item, index) => { if (item.order_number === order) list[index] = row; });
    swap(prepayData?.received); swap(prepayData?.credited); swap(prepayPeriod?.rows);
    prepayEditing = null; refreshPaymentGroups(); showPrepay(); flashKey('prepay:' + order);
    globalThis.RetroToast?.show('Предоплата сохранена');
    return true;
  } catch (error) {
    if (current === generation) { feedback.textContent = error.message; feedback.classList.add('is-error'); }
    return false;
  }
}
for (const tab of document.querySelectorAll('.prepay-tabs [data-tab]')) tab.addEventListener('click', () => {
  prepayTab = tab.dataset.tab; prepayEditing = null;
  if (prepayTab === 'period' && !prepayPeriod && snapshot) {
    // По умолчанию — две недели назад и две вперёд: видно и полученное, и ближайшие события.
    // Раньше начала учёта (02.10) сервер даты не принимает.
    const start = $('prepay-from').min;
    if (!$('prepay-from').value) $('prepay-from').value = offsetDay(snapshot.date, 14) < start ? start : offsetDay(snapshot.date, 14);
    if (!$('prepay-to').value) $('prepay-to').value = offsetDay(snapshot.date, -14);
    loadPrepayPeriod();
  }
  showPrepay();
});
$('prepay-period').addEventListener('submit', event => {
  event.preventDefault();
  if (!$('prepay-period').reportValidity()) return;
  prepayEditing = null;
  Busy.button($('prepay-show'), loadPrepayPeriod());
});
const prepaySection = $('prepay-section');
// Новый вход всегда без запроса реестра, даже если раньше его оставили открытым.
prepaySection.open = false;
function showPrepayToggle() {
  $('prepay-toggle-label').replaceChildren(prepaySection.open ? 'Свернуть' : 'Развернуть');
}
showPrepayToggle();
prepaySection.addEventListener('toggle', () => {
  showPrepayToggle();
  ensurePrepay();
});

function show(data) {
  snapshot = data;
  if ('handover' in data) handoverRecord = data.handover;
  if (data.summary) summary = data.summary;
  const shift = CashierLogic.shiftLabel(data.shift, data.date);
  $('shift-pill').hidden = !shift;
  if (shift) {
    $('shift-text').textContent = shift.text;
    $('shift-pill').classList.toggle('is-closed', !shift.open);
  }
  const view = CashierLogic.revenueGroups(data, prepayData);
  revenueView = view;
  const amountText = value => value === null ? '—' : money.format(value);
  $('revenue').textContent = amountText(view.total);
  $('receipts').textContent = view.receipts === null ? '—' : count.format(view.receipts);
  // Средний чек — целыми сумами: тийины не в обороте, а «146 428,57»
  // читается дольше и обещает точность, которой нет.
  $('average').textContent = view.average === null ? '—' : count.format(Math.round(view.average));
  showCheck($('revenue-check'), view);
  $('payment-empty').hidden = view.total !== 0;
  $('payment-empty').querySelector('p').textContent = 'За этот день оплат и новых предоплат нет';
  // Пока предоплаты грузятся в фоне, выручка — скелетом (revenuePending), без фразы.
  const notes = [];
  if (view.received === null && !data.prepayments_deferred)
    notes.push('Предоплаты за день недоступны — итоговая выручка требует проверки. Откройте реестр ещё раз.');
  if (view.fallback) notes.push('Разбивка по кассам iiko недоступна — оплаты показаны одним списком.');
  $('payments-note').hidden = !notes.length;
  $('payments-note').textContent = notes.join(' ');
  renderGroups();
  prepayMethods();
  $('download').disabled = Boolean(data.stale || data.refreshing);
  $('source-title').textContent = data.demo ? 'Демонстрационные данные' :
    data.source === 'database' ? 'Сохранённый отчёт · iikoWeb' : 'Источник: iikoWeb';
  showStatus();
  showHandover();
}
/* Загрузка дня. «↻» крутится, пока идёт загрузка (и фоновое обновление iiko),
   откуда бы её ни начали: стрелки, «Сегодня/Вчера», календарь или сама «↻»;
   ✓ — только когда обновить попросили ей. */
function load(options = {}) {
  const work = loadDay(options);
  Busy.button($('refresh'), work, {done: options.refresh === true});
  return work;
}
async function loadDay(options = {}) {
  const day = $('report-date').value;
  // Будущий или пустой день (ввод с клавиатуры мимо календаря): остаёмся на
  // показанном дне, цифры не стираем — подпись даты и данные не расходятся.
  if (!config || !day || !$('report-date').checkValidity()) {
    if (config) $('report-date').value = snapshot?.date || config.today;
    message(day && config && day > config.today ? 'Будущий день недоступен: отчёта за него ещё нет.' : 'Выберите корректную дату.', true);
    return false;
  }
  const current = ++generation;
  controller?.abort(); controller = new AbortController();
  resetPrepayLoading(options.refresh === true);
  const signal = controller.signal;
  $('refresh').disabled = false;
  const keepSnapshot = snapshot?.date === day;
  if (!keepSnapshot) { clearSnapshot(); clearFinance(); }
  $('download').disabled = true;
  message('');
  // Другой день — прежние цифры не показываем даже приглушёнными: скелет до ответа.
  if (!keepSnapshot) { clearUsdRate(day); expect(CashierLogic.PARTS); }
  ensurePrepay();
  $('report-date-text').classList.remove('is-skel');
  $('report-date-text').textContent = shortDay(day);
  const isToday = day === config.today, isYesterday = day === previousDay(config.today);
  $('today').classList.toggle('is-active', isToday);
  $('today').setAttribute('aria-pressed', String(isToday));
  $('yesterday').classList.toggle('is-active', isYesterday);
  $('yesterday').setAttribute('aria-pressed', String(isYesterday));
  $('day-next').disabled = day >= config.today;
  $('day-prev').disabled = day <= '2026-10-02';
  const mainParts = Promise.allSettled([
    loadPart('expenses', current, loadExpenses(day, current, signal)),
    loadPart('receipts', current, loadReceipts(day, current, signal)),
    loadPart('rate', current, loadUsdRate(day, current, signal)),
    loadPart('shokh', current, loadShokh(day, current, signal)),
    loadPart('usd', current, loadUsd(day, current, signal)),
  ]);
  if (!config.configured && !demo) { arrive('day', current); arrive('prepay', current); return true; }
  $('refresh').disabled = true;
  try {
    const endpoint = `/api/cashier/day?date=${encodeURIComponent(day)}&demo=${demo}&allow_stale=true`;
    const first = request(`${endpoint}&refresh=${options.refresh === true}`, signal).then(response => response.json());
    if (!waiting.has('day')) dim('day', first);
    let data = await first;
    if (current !== generation) return false;
    show(data); arrive('day', current);
    const polling = data.refreshing;
    // Фоновое обновление iiko: цифры уже на экране, опрос тихий — без полосы
    // сверху; идёт ли он, видно по «↻» и строке статуса.
    for (let attempt = 0; data.refreshing && attempt < 45; attempt++) {
      await waitForRefresh(signal);
      data = await (await request(`${endpoint}&refresh=false`, signal, {retroBusy: false})).json();
      if (current !== generation) return false;
      show(data);
    }
    // Все основные блоки сначала показывают данные (либо свою ошибку).
    // Ждём также обновление снимка, чтобы не читать реестр дважды подряд.
    await mainParts;
    await afterDashboardPaint();
    if (current !== generation || signal.aborted) return false;
    prepayReadyGeneration = current;
    ensurePrepay(polling);
    message(data.refresh_error || (data.refreshing ? 'Обновление продолжается. Повторите проверку позже.' :
      data.stale ? 'Показаны последние сохранённые данные. Требуется обновление iiko.' : ''), Boolean(data.refresh_error || data.stale));
    return !data.refresh_error;
  } catch (error) {
    if (current === generation && error.name !== 'AbortError') {
      if (snapshot?.date === day) {
        snapshot = {...snapshot, stale:true, refreshing:false};
        show(snapshot);
      }
      message(error.message + (snapshot?.date === day ? ' На экране предыдущие данные.' : ''), true);
    }
    return false;
  } finally {
    if (current === generation) {
      $('refresh').disabled = false; arrive('day', current);
      if (prepayReadyGeneration !== current) arrive('prepay', current);
    }
  }
}
function afterDashboardPaint() {
  return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
}
function waitForRefresh(signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException('Отменено', 'AbortError')); return; }
    const abort = () => { clearTimeout(timer); reject(new DOMException('Отменено', 'AbortError')); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, 2000);
    signal.addEventListener('abort', abort, {once:true});
  });
}
/* Расходы и поступления: форма «+» и × у строки устроены одинаково. */
const ENTRIES = {
  expense: {form: 'expense-form', save: 'expense-save', description: 'expense-description', amount: 'expense-amount',
    url: '/api/cashier/expenses', part: 'expenses', key: 'cash-exp:', load: loadExpenses,
    saved: 'Расход сохранён', removed: 'Расход удалён'},
  receipt: {form: 'receipt-form', save: 'receipt-save', description: 'receipt-description', amount: 'receipt-amount',
    url: '/api/cashier/receipts', part: 'receipts', key: 'cash-rcp:', load: loadReceipts,
    saved: 'Поступление сохранено', removed: 'Поступление удалено'},
};
async function addEntry(kind) {
  const entry = ENTRIES[kind], day = $('report-date').value, current = generation;
  entryFeedback(kind, '');
  try {
    const response = await request(entry.url, undefined, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({date:day, description:$(entry.description).value, amount:$(entry.amount).value})
    });
    const item = await response.json().catch(() => null);
    if (current !== generation) return false;
    $(entry.description).value = ''; $(entry.amount).value = ''; $(entry.amount).dispatchEvent(new Event('input', {bubbles: true}));
    await loadPart(entry.part, current, entry.load(day, current, controller.signal));
    await refreshSummary();
    if (current !== generation) return false;
    if (item && item.id != null) flashKey(entry.key + item.id);
    entryFeedback(kind, entry.saved); globalThis.RetroToast?.show(entry.saved); $(entry.description).focus({preventScroll: true});
    return true;
  } catch (error) {
    if (current === generation) { entryFeedback(kind, error.message, true); globalThis.RetroToast?.show(error.message, 'error'); }
    return false;
  }
}
function removeEntry(kind, id, day, button) {
  const entry = ENTRIES[kind];
  return removeRow(button, `${entry.url}/${id}?date=${encodeURIComponent(day)}`, {part: entry.part,
    reload: current => entry.load(day, current, controller.signal),
    done: () => entryFeedback(kind, entry.removed),
    fail: text => { entryFeedback(kind, text, true); globalThis.RetroToast?.show(text, 'error'); }});
}
for (const [kind, entry] of Object.entries(ENTRIES)) {
  $(entry.form).addEventListener('submit', event => {
    event.preventDefault();
    if (demo || !$('report-date').value || !$(entry.form).reportValidity()) return;
    Busy.button($(entry.save), addEntry(kind));
  });
}
$('report-date').addEventListener('change',load);
// Нажатие на подпись даты открывает календарь и там, где прозрачное поле
// поверх подписи само его не показывает.
$('report-date').addEventListener('click', event => { try { event.target.showPicker?.(); } catch {} });
$('refresh').addEventListener('click',()=>load({refresh:true}));
$('today').addEventListener('click',()=>{if(config){$('report-date').value=config.today;load();}});
$('yesterday').addEventListener('click',()=>{if(config){$('report-date').value=previousDay(config.today);load();}});
/* Excel: кнопка крутится, пока файл не начал скачиваться, затем ✓. */
$('download').addEventListener('click', () => {
  if (!snapshot || snapshot.stale || snapshot.refreshing || !financeData || !receiptData) return;
  Busy.button($('download'), downloadReport());
});
async function downloadReport() {
  const data = snapshot, current = generation;
  try {
    const response = await request(`/api/cashier/export?date=${data.date}&snapshot_id=${data.snapshot_id}${financeData.revision ? "&expense_revision=" + financeData.revision : ""}${receiptData.revision ? "&receipt_revision=" + receiptData.revision : ""}${shokhData && shokhData.revision ? "&shokh_revision=" + shokhData.revision : ""}${usdData?.revision ? "&usd_revision=" + usdData.revision : ""}`);
    const blob = await response.blob();
    if (current !== generation) return false;
    const url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href=url; link.download=`${data.demo?'DEMO-':''}Retro-${data.date}.xlsx`;
    document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
    // Итог — ✓ на кнопке и всплывающее сообщение; плашка над карточками сдвигала бы экран.
    globalThis.RetroToast?.show('Отчёт скачан за ' + formattedDay(data.date));
    return true;
  } catch(error) { if (current === generation) message(error.message, true); return false; }
  finally { if (current === generation) showHandover(); }
}
// Первый кадр: скелет всего экрана, пока не пришли настройки (разметка
// держит его классом is-booting, пока не выполнился этот файл).
expect(CashierLogic.PARTS);
document.querySelector('.cashier-workspace').classList.remove('is-booting');
(async()=>{
  try {
    config = await globalThis.RetroConfig;
    $('report-date').max=config.today;$('report-date').value=config.today;
    $('demo-banner').hidden=!demo;$('setup').hidden=config.configured||demo;
    if (demo) {
      for (const input of $('expense-form').querySelectorAll('input, button')) input.disabled = true;
      for (const input of $('receipt-form').querySelectorAll('input, button')) input.disabled = true;
      for (const input of $('shokh-form').querySelectorAll('input, button')) input.disabled = true;
      for (const input of $('usd-form').querySelectorAll('input, button')) input.disabled = true;
      $('expense-feedback').textContent = 'В демонстрационном режиме расходы не сохраняются.';
      $('receipt-feedback').textContent = 'В демонстрационном режиме поступления не сохраняются.';
    }
    showStatus();
    $('connection').classList.toggle('connected',config.configured&&!demo);

    await load();
  } catch(error){ expect([]); message(error.message,true); }
})();

// ── Дата-навигация ──────────────────────────────────────────────────────────
// Стрелки листают по одному дню; вперёд дальше сегодняшнего не уходим —
// отчёта за будущий день не существует.
function stepDay(offset) {
  const current = $('report-date').value;
  if (!current) return;
  const next = offsetDay(current, -offset);
  if (next < '2026-10-02' || (offset > 0 && config && next > config.today)) return;
  $('report-date').value = next;
  load();
}
$('day-prev').addEventListener('click', () => stepDay(-1));
$('day-next').addEventListener('click', () => stepDay(1));
