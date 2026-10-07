const $ = id => document.getElementById(id);
const money = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
const rateOfficial = new Intl.NumberFormat('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const rateRestaurant = new Intl.NumberFormat('ru-RU', {minimumFractionDigits: 1, maximumFractionDigits: 1});
const count = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 0});
const colors = ['#24594b','#91a786','#d2b77b','#b2c3aa','#81968c','#ddd2b6','#6b8074'];
const demo = new URLSearchParams(location.search).get('demo') === '1';
let config, snapshot = null, financeData = null, receiptData = null, generation = 0, controller;
// 5a: выдачи Шоху из кассы, доллары в сейф, передача бухгалтеру дня.
let shokhData = null, usdData = null, usdRate = null, handoverRecord = null, handoverBusy = false;
// Готовые числа «К передаче» и «Касса за день» с сервера (/day, /summary): экран их не пересчитывает.
let summary = null;
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
const SKELETON_LISTS = {payments: ['day', 9, 'payment'], 'expense-list': ['expenses', 1, 'item'], 'receipt-list': ['receipts', 1, 'item']};
// Что гаснет, пока часть перечитывается: «К передаче» считается из всех.
const SECTIONS = {day: ['metrics', 'payments-section', 'handover-panel'], expenses: ['expenses-section', 'handover-panel'],
  shokh: ['expenses-section', 'handover-panel'], receipts: ['receipts-section', 'handover-panel'], usd: ['usd-card'], rate: ['usd-card']};
function skeletonRows(count, kind) {
  const bar = width => { const node = document.createElement('span'); node.className = 'rm-skel'; node.style.width = width; return node; };
  return Array.from({length: count}, (_, i) => {
    const row = document.createElement('div');
    row.className = (kind === 'payment' ? 'payment-row' : 'expense-item') + ' cashier-skel-row';
    row.setAttribute('aria-hidden', 'true');
    const name = bar([38, 26, 44, 30, 34, 22, 40][i % 7] + '%');
    if (kind === 'payment') {
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
  if (waiting.has('day')) $('payment-empty').hidden = true;
  else if (!snapshot) $('payment-empty').hidden = false;
  $('expenses-empty').hidden = waiting.has('expenses') || waiting.has('shokh')
    || Boolean(financeData && financeData.expenses.length) || Boolean(shokhData && shokhData.gives.length);
  $('receipts-empty').hidden = waiting.has('receipts') || waiting.has('day') || Boolean(snapshot)
    || Boolean(receiptData && receiptData.receipts.length);
  $('metrics').setAttribute('aria-busy', String(waiting.has('day')));
}
/* Новый день: всё, что перечислено, грузится впервые. */
function expect(parts) {
  waiting = new Set(parts);
  for (const [id, [part, rows, kind]] of Object.entries(SKELETON_LISTS)) {
    if (waiting.has(part) && !$(id).children.length) $(id).append(...skeletonRows(rows, kind));
  }
  paintWaiting();
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
      if (part !== 'usd') await refreshSummary();
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
  $('usd-official').textContent = '—';
  $('usd-restaurant').textContent = '—';
  $('usd-official-tile').removeAttribute('title');
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
    $('usd-official').textContent = rateOfficial.format(Number(data.official_rate));
    $('usd-restaurant').textContent = rateRestaurant.format(Number(data.restaurant_rate));
    $('usd-official-tile').title = 'Курс ЦБ действует с ' + formattedDay(data.source_date);
    $('usd-status').textContent = '';
    showUsd();
  } catch (error) {
    if (current === generation && error.name !== 'AbortError') {
      $('usd-status').textContent = error.message;
      $('usd-status').classList.add('is-error');
    }
  }
}
/* «≈ 1,5 млн» — сумы по курсу Retro рядом со взносом в долларах. */
function shortSum(value) {
  return value >= 1e6 ? (Math.round(value / 1e5) / 10).toLocaleString('ru-RU') + ' млн' : money.format(Math.round(value));
}
function clockOf(stamp, day) {
  if (!stamp) return null;
  const text = String(stamp), time = text.slice(11, 16);
  return !day || text.slice(0, 10) === day ? time : text.slice(8, 10) + '.' + text.slice(5, 7) + ' ' + time;
}
function clearUsd() {
  usdData = null;
  $('usd-deposits').replaceChildren();
  $('usd-today').textContent = '—';
  $('usd-safe').textContent = '—';
  showUsdHint();
}
function showUsdHint(text = '', error = false) {
  const hint = $('usd-hint'), value = CashierLogic.parseAmount($('usd-amount').value);
  hint.classList.toggle('is-error', error);
  if (text) { hint.textContent = text; return; }
  hint.textContent = value && usdRate ? '≈ ' + money.format(Math.round(value * usdRate)) + ' сум по курсу Retro'
    : 'Введите сумму и нажмите «В сейф»';
}
function showUsd() {
  const data = usdData;
  $('usd-deposits').replaceChildren();
  if (!data) return;
  for (const item of data.deposits) {
    const row = document.createElement('div'); row.className = 'usd-deposit'; row.dataset.busyKey = 'cash-usd:' + item.id;
    const time = document.createElement('span'); time.className = 'usd-deposit-time rm-num';
    time.textContent = clockOf(item.created_at, data.date) || 'за день';
    const note = document.createElement('span'); note.className = 'usd-deposit-note';
    note.textContent = 'в сейф' + (usdRate ? ' · ≈ ' + shortSum(Number(item.amount) * usdRate) : '');
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
    row.append(time, note, value, remove); $('usd-deposits').append(row);
  }
  const today = config && data.date === config.today;
  $('usd-today').textContent = usdFormat.format(Number(data.total)) + ' USD';
  // Новый узел, а не правка старого: переводчик переводит добавленные узлы.
  const label = today ? 'Сегодня в сейф' : 'За день в сейф';
  if ($('usd-today-label').dataset.label !== label) { $('usd-today-label').dataset.label = label; $('usd-today-label').replaceChildren(label); }
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
  for (const id of ['revenue','receipts','average','payment-total','payments-sales-total','payments-prepay-total']) $(id).textContent = '—';
  $('payments-sub').textContent = '';
  $('payments').replaceChildren();
  $('payments-bar').replaceChildren();
  $('payment-empty').hidden = false;
  $('payment-empty').querySelector('p').textContent = 'Здесь появятся оплаты за выбранный день';
  showStatus();
  showHandover();
}
function clearFinance() {
  financeData = null;
  receiptData = null;
  $('expense-list').replaceChildren();
  $('expenses-empty').hidden = false;
  $('expense-total').textContent = '—';
  $('handover-expenses').textContent = '—';
  $('expense-feedback').textContent = '';
  $('expense-feedback').classList.remove('is-error');
  $('receipt-list').replaceChildren();
  $('receipts-empty').hidden = false;
  $('receipt-total').textContent = '—';
  $('handover-receipts').textContent = '—';
  $('receipt-feedback').textContent = '';
  $('receipt-feedback').classList.remove('is-error');
  shokhData = null;
  $('shokh-gives').replaceChildren();
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
  for (const id of ['metrics', 'handover-panel']) Busy.section($(id), work);
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
  const totalPrepay = snapshot?.new_prepayment;
  const cashPrepay = snapshot?.cash_prepayment;
  const totalUnknown = Boolean(issue) || totalPrepay == null;
  const cardUnknown = totalUnknown || cashPrepay == null || Number(totalPrepay) < Number(cashPrepay);
  const prepayText = !snapshot ? '—'
    : totalUnknown ? 'требует проверки' : money.format(Number(totalPrepay));
  $('card-prepay-cash').textContent = !snapshot ? '—'
    : unknown || cashPrepay == null ? 'требует проверки' : money.format(Number(cashPrepay)) + ' сум';
  $('card-prepay-card').textContent = !snapshot ? '—'
    : cardUnknown ? 'требует проверки' : money.format(Number(totalPrepay) - Number(cashPrepay)) + ' сум';
  // Предоплаты показаны и карточкой сверху, и строкой в расчёте передачи.
  $('cash-prepay').textContent = unknown ? 'требует проверки' : text('cash_prepayment');
  $('card-prepay').textContent = prepayText;
  $('card-prepay').classList.toggle('is-note', Boolean(totalUnknown && snapshot));
  $('card-prepay-unit').hidden = totalUnknown;
  $('card-prepay-note').textContent = issue || 'iiko · оценка, не реестр авансов';
  $('prepay-issue').hidden = !issue;
  $('prepay-issue').textContent = issue ? issue + ' Выручка и чеки за день верны.' : '';
  const inflow = summaryValue('total_inflow');
  // Весь приход = продажи + предоплаты: без предоплат он тоже неизвестен.
  // Пустой прочерк здесь читается как «ещё грузится», поэтому пишем прямо.
  const inflowUnknown = Boolean(unknown && snapshot && inflow === null);
  $('total-inflow').textContent = inflowUnknown ? 'требует проверки' : text('total_inflow');
  $('total-inflow').classList.toggle('is-note', inflowUnknown);
  $('total-inflow-unit').hidden = inflowUnknown;
  $('payments-inflow').textContent = text('total_inflow');
  // Без отдельных поступлений эта строка повторяет «Итого с предоплатами».
  $('payments-inflow-row').hidden = !(summaryValue('receipts') > 0);
  // Полоса в главной карточке: продажи против всего остального прихода.
  const sales = summaryValue('sales') || 0;
  const salesShare = inflow ? Math.max(0, Math.min(100, sales / inflow * 100)) : 0;
  const [salesBar, otherBar] = $('composition').children;
  salesBar.style.width = (inflow ? salesShare : 0) + '%';
  otherBar.style.width = (inflow ? 100 - salesShare : 0) + '%';
  // Предоплаты наличными — автоматическая строка в «В кассу», как в макете.
  $('receipt-auto').hidden = !snapshot;
  $('receipt-auto-value').textContent = prepayText;
  $('receipts-empty').hidden = Boolean(snapshot) || Boolean(receiptData && receiptData.receipts.length);
  $('expense-total').textContent = text('cash_out');
  $('handover-expenses').textContent = text('cash_out');
  $('handover-receipts').textContent = text('receipts');
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

/* ── Выдачи Шоху из кассы ─────────────────────────────────────────────────
   Новую выдачу с кассы не записать (строка «Выдать Шоху» убрана); записанные
   раньше видны в расходах, их можно удалить, и они входят в передачу. */
function showShokh(data) {
  shokhData = data;
  $('shokh-gives').replaceChildren();
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
  $('expenses-empty').hidden = Boolean(financeData && financeData.expenses.length) || data.gives.length > 0;
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
/* Строка под формой расходов / поступлений: итог последнего действия. */
function entryFeedback(kind, text, error = false) {
  const node = $(kind + '-feedback');
  node.textContent = text;
  node.classList.toggle('is-error', error);
}
function showReceipts(data) {
  receiptData = data;
  $('receipt-list').replaceChildren();
  $('receipts-empty').hidden = data.receipts.length > 0 || Boolean(snapshot);
  $('receipt-total').textContent = money.format(Number(data.total));
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
  $('expenses-empty').hidden = data.expenses.length > 0 || Boolean(shokhData && shokhData.gives.length);
  for (const item of data.expenses) {
    const row = document.createElement('div'); row.className = 'expense-item';
    if (item.id != null) row.dataset.busyKey = 'cash-exp:' + item.id;
    const name = document.createElement('span'); name.className = 'expense-item-name'; name.textContent = item.description;
    const value = document.createElement('span'); value.className = 'expense-item-value'; value.textContent = money.format(Number(item.amount));
    if (item.automatic) {
      const marker = document.createElement('span'); marker.className = 'expense-automatic'; marker.textContent = 'Авто';
      marker.setAttribute('aria-label', 'Добавляется автоматически каждый день');
      row.append(name, value, marker);
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
  $('payments').replaceChildren();
  $('payments-bar').replaceChildren();
  $('revenue').textContent = money.format(Number(data.revenue));
  $('receipts').textContent = count.format(data.receipt_count);
  // Средний чек — целыми сумами: тийины не в обороте, а «146 428,57»
  // читается дольше и обещает точность, которой нет.
  $('average').textContent = data.average_receipt === null ? '—' : count.format(Math.round(Number(data.average_receipt)));
  $('payments-sub').textContent = `${count.format(data.receipt_count)} чеков · средний ${data.average_receipt === null ? '—' : count.format(Math.round(Number(data.average_receipt)))}`;
  const revenue = CashierLogic.revenueView(data);
  const amountText = amount => amount === null ? '—' : money.format(amount);
  $('payments-sales-total').textContent = amountText(revenue.salesTotal);
  $('payments-prepay-total').textContent = amountText(revenue.prepaymentTotal);
  $('payment-total').textContent = amountText(revenue.total);
  $('payment-empty').hidden = revenue.total !== 0;
  $('payment-empty').querySelector('p').textContent = 'За этот день продаж и предоплат нет';
  const positiveTotal = revenue.rows.every(p => p.amount !== null)
    ? revenue.rows.reduce((sum,p) => sum + Math.max(0,p.amount),0) : 0;
  revenue.rows.forEach((payment,i) => {
    const color = payment.kind === 'prepayment' ? (payment.name === 'Предоплаты наличными' ? '#d2b77b' : '#b2c3aa') : colors[i % colors.length];
    const row = document.createElement('div'); row.className = 'payment-row'; row.style.setProperty('--color',color);
    const dot = document.createElement('span'); dot.className = 'payment-dot';
    const name = document.createElement('span'); name.className = 'payment-name';
    const label = document.createElement('span'); label.className = 'payment-label'; label.textContent = payment.name;
    name.append(label);
    const share = document.createElement('span'); share.className = 'payment-share';
    const ratio = positiveTotal > 0 ? Math.max(0, Number(payment.amount)) / positiveTotal * 100 : null;
    share.textContent = ratio === null ? '' : ratio.toLocaleString('ru-RU', {minimumFractionDigits:1, maximumFractionDigits:1}) + '%';
    const value = document.createElement('strong'); value.className = 'payment-value'; value.textContent = amountText(payment.amount);
    // Способы без единой транзакции остаются в списке (видно, что их
    // проверяли), но гаснут и не спорят за внимание с теми, где были деньги.
    if (payment.amount === 0) row.classList.add('is-zero');
    if (payment.kind === 'prepayment') row.classList.add('cashier-pay-row--prepayment');
    if (payment.kind === 'sale' && payment.name === CashierLogic.CASH_PAYMENT) {
      // В iiko наличные называются «Демо»; кассиру понятнее «Наличные».
      label.textContent = 'Наличные';
      const source = document.createElement('small'); source.className = 'payment-source'; source.textContent = '«Демо» в iiko';
      const tag = document.createElement('span'); tag.className = 'payment-tag'; tag.textContent = '→ бухгалтеру';
      name.append(source, tag);
      row.classList.add('is-cash');
    }
    row.append(dot,name,share,value); $('payments').append(row);
    if (positiveTotal > 0 && Number(payment.amount) > 0) {
      const segment = document.createElement('span');segment.style.setProperty('--color',color);segment.style.width = (Number(payment.amount)/positiveTotal*100)+'%';$('payments-bar').append(segment);
    }
  });
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
  const signal = controller.signal;
  $('refresh').disabled = false;
  const keepSnapshot = snapshot?.date === day;
  if (!keepSnapshot) { clearSnapshot(); clearFinance(); }
  $('download').disabled = true;
  message('');
  // Другой день — прежние цифры не показываем даже приглушёнными: скелет до ответа.
  if (!keepSnapshot) { clearUsdRate(day); expect(CashierLogic.PARTS); }
  $('report-date-text').classList.remove('is-skel');
  $('report-date-text').textContent = shortDay(day);
  const isToday = day === config.today, isYesterday = day === previousDay(config.today);
  $('today').classList.toggle('is-active', isToday);
  $('today').setAttribute('aria-pressed', String(isToday));
  $('yesterday').classList.toggle('is-active', isYesterday);
  $('yesterday').setAttribute('aria-pressed', String(isYesterday));
  $('day-next').disabled = day >= config.today;
  $('day-prev').disabled = day <= '2026-10-02';
  loadPart('expenses', current, loadExpenses(day, current, signal));
  loadPart('receipts', current, loadReceipts(day, current, signal));
  loadPart('rate', current, loadUsdRate(day, current, signal));
  loadPart('shokh', current, loadShokh(day, current, signal));
  loadPart('usd', current, loadUsd(day, current, signal));
  if (!config.configured && !demo) { arrive('day', current); return true; }
  $('refresh').disabled = true;
  try {
    const endpoint = `/api/cashier/day?date=${encodeURIComponent(day)}&demo=${demo}&allow_stale=true`;
    const first = request(`${endpoint}&refresh=${options.refresh === true}`, signal).then(response => response.json());
    if (!waiting.has('day')) dim('day', first);
    let data = await first;
    if (current !== generation) return false;
    show(data); arrive('day', current);
    // Фоновое обновление iiko: цифры уже на экране, опрос тихий — без полосы
    // сверху; идёт ли он, видно по «↻» и строке статуса.
    for (let attempt = 0; data.refreshing && attempt < 45; attempt++) {
      await waitForRefresh(signal);
      data = await (await request(`${endpoint}&refresh=false`, signal, {retroBusy: false})).json();
      if (current !== generation) return false;
      show(data);
    }
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
    if (current === generation) { $('refresh').disabled = false; arrive('day', current); }
  }
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
    const response = await request(`/api/cashier/export?date=${data.date}&snapshot_id=${data.snapshot_id}${financeData.revision ? "&expense_revision=" + financeData.revision : ""}${receiptData.revision ? "&receipt_revision=" + receiptData.revision : ""}${shokhData && shokhData.revision ? "&shokh_revision=" + shokhData.revision : ""}`);
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
