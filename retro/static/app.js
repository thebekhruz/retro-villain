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
// Выручка по кассам iiko на экране и какие кассы раскрыты (живут при обновлении дня).
let revenueView = null;
const openGroups = new Set(), allTypes = new Set();
// Реестр предоплат: день, период, вкладка, группировка, строка в правке.
let prepayData = null, prepayPeriod = null, prepayTab = 'received', prepayBy = 'people', prepayEditing = null;
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
const SECTIONS = {day: ['metrics', 'payments-section', 'handover-panel'], expenses: ['expenses-section', 'handover-panel'],
  shokh: ['expenses-section', 'handover-panel'], receipts: ['receipts-section', 'handover-panel'], usd: ['usd-card'], rate: ['usd-card'],
  prepay: ['prepay-section']};
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
  if (waiting.has('day')) $('payment-empty').hidden = true;
  else if (!snapshot) $('payment-empty').hidden = false;
  $('prepay-empty').hidden = prepayEmptyHidden();
  $('expenses-empty').hidden = waiting.has('expenses') || waiting.has('shokh')
    || Boolean(financeData && financeData.expenses.length) || Boolean(shokhData && shokhData.gives.length);
  $('receipts-empty').hidden = waiting.has('receipts') || Boolean(receiptData && receiptData.receipts.length);
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
  for (const id of ['revenue','receipts','average','payment-total','real-cash','prepay-credited']) $(id).textContent = '—';
  $('real-cash').classList.remove('is-note');
  $('real-cash-unit').hidden = false;
  showRealCashSub(null);
  for (const id of ['revenue-check','payment-check']) $(id).hidden = true;
  revenueView = null;
  $('payments').replaceChildren();
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
  $('cash-prepay').textContent = unknown ? 'требует проверки' : text('cash_prepayment');
  $('prepay-issue').hidden = !issue;
  $('prepay-issue').textContent = issue ? issue + ' Выручка и чеки за день верны.' : '';
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
/* «= iiko ✓» — сумма по кассам сошлась с итогом iiko за день; иначе красная разница. */
function showCheck(node, view) {
  const known = Boolean(view && !view.fallback && view.total !== null);
  node.hidden = !known;
  if (!known) return;
  node.classList.toggle('is-off', !view.match);
  if (view.match) { node.replaceChildren('= iiko ✓'); return; }
  const diff = document.createElement('span'); diff.className = 'rm-num';
  diff.textContent = view.difference === null ? '' : (view.difference > 0 ? '+' : '−') + money.format(Math.abs(view.difference));
  node.replaceChildren('≠ iiko ', diff);
}
/* Подпись реальной кассы: из чего она получилась (ТЗ 3.3). */
function showRealCashSub(view) {
  const node = $('real-cash-sub'), label = text => { const span = document.createElement('span'); span.textContent = text; return span; };
  const figure = value => { const b = document.createElement('b'); b.className = 'rm-num'; b.textContent = money.format(value); return b; };
  if (!view) { node.replaceChildren(label('Выручка без предоплат, зачтённых сегодня')); return; }
  if (view.realCash === null) { node.replaceChildren(label('iiko не отдал зачёт предоплат')); return; }
  if (!view.redeemed) { node.replaceChildren(label('Зачтённых предоплат нет — равна выручке')); return; }
  node.replaceChildren(label('Выручка'), ' ', figure(view.total), ' ', label('− зачтено предоплат'), ' ', figure(view.redeemed));
}
const percentText = value => value === null ? '' : value.toLocaleString('ru-RU', {minimumFractionDigits: 1, maximumFractionDigits: 1}) + '%';
/* Кассы iiko аккордеоном (ТЗ 3.1): по умолчанию всё свёрнуто — сверка с iiko
   идёт по итогам касс; раскрытая касса показывает типы с деньгами, нулевые —
   по «показать все». */
function renderGroups() {
  const view = revenueView, list = $('payments');
  list.replaceChildren();
  if (!view || view.total === 0) return;
  let colorIndex = 0;
  for (const group of view.groups) {
    const box = document.createElement('div'); box.className = 'pay-group';
    const open = openGroups.has(group.name) || view.groups.length === 1;
    box.classList.toggle('is-open', open);
    const head = document.createElement('button'); head.type = 'button'; head.className = 'payment-row pay-group-head';
    head.setAttribute('aria-expanded', String(open));
    const chevron = document.createElement('span'); chevron.className = 'rm-section-chevron pay-group-chevron';
    chevron.setAttribute('aria-hidden', 'true'); chevron.textContent = '⌄';
    const name = document.createElement('span'); name.className = 'payment-name';
    const label = document.createElement('span'); label.className = 'payment-label'; label.textContent = group.name;
    label.dataset.i18n = 'off';
    name.append(label);
    const share = document.createElement('span'); share.className = 'payment-share'; share.textContent = percentText(group.percent);
    const value = document.createElement('strong'); value.className = 'payment-value'; value.textContent = money.format(group.total);
    if (!group.total) head.classList.add('is-zero');
    head.append(chevron, name, share, value);
    head.addEventListener('click', () => {
      if (openGroups.has(group.name)) openGroups.delete(group.name); else openGroups.add(group.name);
      renderGroups();
    });
    const body = document.createElement('div'); body.className = 'pay-group-body'; body.hidden = !open;
    const showAll = allTypes.has(group.name);
    for (const type of group.types) {
      const color = colors[colorIndex++ % colors.length];
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
  }
}
function typeRow(type, color) {
  const row = document.createElement('div'); row.className = 'payment-row pay-type'; row.style.setProperty('--color', color);
  const dot = document.createElement('span'); dot.className = 'payment-dot';
  const name = document.createElement('span'); name.className = 'payment-name';
  const label = document.createElement('span'); label.className = 'payment-label'; label.textContent = type.label;
  if (!type.isCash) label.dataset.i18n = 'off';
  name.append(label);
  if (type.isCash) {
    // В iiko наличные называются «Демо»; кассиру понятнее «Наличные».
    const source = document.createElement('small'); source.className = 'payment-source'; source.textContent = '«Демо» в iiko';
    const tag = document.createElement('span'); tag.className = 'payment-tag'; tag.textContent = '→ бухгалтеру';
    name.append(source, tag);
    row.classList.add('is-cash');
  }
  // Часть суммы типа — зачтённая сегодня предоплата: в реальную кассу дня она не входит.
  if (type.redeemed) {
    const note = document.createElement('small'); note.className = 'payment-source pay-type-redeemed';
    const words = document.createElement('span'); words.textContent = 'в т.ч. зачтено предоплат';
    const figure = document.createElement('span'); figure.className = 'rm-num'; figure.textContent = money.format(type.redeemed);
    note.append(words, ' ', figure);
    name.append(note);
  }
  if (type.zero) row.classList.add('is-zero');
  const share = document.createElement('span'); share.className = 'payment-share'; share.textContent = percentText(type.percent);
  const value = document.createElement('strong'); value.className = 'payment-value';
  value.textContent = type.total === null ? '—' : money.format(type.total);
  row.append(dot, name, share, value);
  return row;
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
  $('prepay-received').textContent = '—';
  $('prepay-note').hidden = true;
  showPrepay();
}
async function loadPrepay(day, current, signal) {
  try {
    const data = await RetroState.responseJson(
      await request(`/api/cashier/prepayments?date=${encodeURIComponent(day)}&demo=${demo}`, signal));
    if (current !== generation) return;
    prepayData = data;
  } catch (error) {
    if (current !== generation || error.name === 'AbortError') return;
    prepayData = {date: day, received: [], credited: [], received_total: null, credited_total: null, issue: error.message, failed: true};
  }
  showPrepay();
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
    const card = $(`prepay-${key}-label`);
    if (card.dataset.label !== text) { card.dataset.label = text; card.replaceChildren(text); }
    const tab = document.querySelector(`.prepay-tabs [data-tab="${key}"] .prepay-tab-name`);
    if (tab.dataset.label !== text) { tab.dataset.label = text; tab.replaceChildren(text); }
  }
  $('prepay-received').textContent = prepayData && prepayData.received_total !== null ? money.format(Number(prepayData.received_total)) : '—';
  $('prepay-count-received').textContent = prepayData ? count.format(prepayData.received.length) : '0';
  $('prepay-count-credited').textContent = prepayData ? count.format(prepayData.credited.length) : '0';
  for (const tab of document.querySelectorAll('.prepay-tabs [data-tab]')) {
    const active = tab.dataset.tab === prepayTab;
    tab.classList.toggle('is-active', active); tab.setAttribute('aria-selected', String(active));
  }
  $('prepay-period').hidden = prepayTab !== 'period';
  for (const button of document.querySelectorAll('.prepay-by [data-by]')) {
    const active = button.dataset.by === prepayBy;
    button.classList.toggle('is-active', active); button.setAttribute('aria-pressed', String(active));
  }
  const rows = prepayRows(), list = $('prepay-rows');
  if (!waiting.has('prepay')) list.replaceChildren();
  const known = Array.isArray(rows);
  if (known && rows.length) {
    if (prepayTab === 'period') {
      for (const group of CashierLogic.prepaymentGroups(rows, prepayBy)) {
        const head = document.createElement('div'); head.className = 'prepay-group-head';
        const title = document.createElement('span');
        title.textContent = prepayBy === 'dates' ? (group.title ? dayMonth(group.title) : 'Без даты') : (group.title || 'Кто — не указано');
        if (prepayBy !== 'dates' && group.title) title.dataset.i18n = 'off';
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
}
/* «Предоплат нет» — только когда реестр пришёл и он пуст (или период ещё не выбран). */
function prepayEmptyHidden() {
  if (prepayTab === 'period') return Boolean(prepayPeriod && prepayPeriod.rows.length);
  const rows = prepayRows();
  return waiting.has('prepay') || !Array.isArray(rows) || rows.length > 0;
}
function prepayLine(row, tab) {
  const line = document.createElement('div'); line.className = 'prepay-row'; line.dataset.busyKey = 'prepay:' + row.order_number;
  const status = CashierLogic.prepaymentStatus(row, config?.today);
  const who = document.createElement('span'); who.className = 'prepay-who';
  const guest = document.createElement('strong');
  if (row.guest) { guest.textContent = row.guest; guest.dataset.i18n = 'off'; }
  else { guest.textContent = 'Кто — не указано'; guest.classList.add('is-empty'); }
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
  const eventDay = fact('prepay-event', 'На дату', row.event_day ? dayMonth(row.event_day) : 'не указана', !row.event_day);
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
  line.append(who, received, eventDay, method, sum, state, edit);
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
  const eventDay = input('event_day', row.event_day, {type: 'date'});
  const method = input('method', row.method, {maxlength: 60, list: 'prepay-methods', placeholder: 'Наличные, карта…', autocomplete: 'off'});
  const refund = document.createElement('label'); refund.className = 'prepay-refund';
  const box = document.createElement('input'); box.type = 'checkbox'; box.name = 'refunded'; box.checked = Boolean(row.refunded);
  const refundText = document.createElement('span'); refundText.textContent = 'Возврат предоплаты';
  refund.append(box, refundText);
  const actions = document.createElement('div'); actions.className = 'prepay-editor-actions';
  const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'button secondary'; cancel.textContent = 'Отмена';
  const save = document.createElement('button'); save.type = 'submit'; save.className = 'button primary'; save.textContent = 'Сохранить';
  actions.append(cancel, save);
  const feedback = document.createElement('p'); feedback.className = 'expense-feedback'; feedback.setAttribute('role', 'status');
  form.append(field('Кто', guest), field('Телефон', phone), field('На дату', eventDay), field('Способ', method), refund, actions, feedback);
  cancel.addEventListener('click', () => { prepayEditing = null; showPrepay(); });
  form.addEventListener('submit', event => {
    event.preventDefault();
    Busy.button(save, savePrepay(row.order_number, {guest: guest.value, phone: phone.value, event_day: eventDay.value || null,
      method: method.value, refunded: box.checked}, feedback));
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
    prepayEditing = null; showPrepay(); flashKey('prepay:' + order);
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
for (const button of document.querySelectorAll('.prepay-by [data-by]')) button.addEventListener('click', () => {
  prepayBy = button.dataset.by; showPrepay();
});
$('prepay-period').addEventListener('submit', event => {
  event.preventDefault();
  if (!$('prepay-period').reportValidity()) return;
  prepayEditing = null;
  Busy.button($('prepay-show'), loadPrepayPeriod());
});
$('prepay-open').addEventListener('click', event => {
  event.preventDefault();
  $('prepay-section').scrollIntoView({behavior: 'smooth', block: 'start'});
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
  const view = CashierLogic.revenueGroups(data);
  revenueView = view;
  const amountText = value => value === null ? '—' : money.format(value);
  $('revenue').textContent = amountText(view.total);
  $('receipts').textContent = view.receipts === null ? '—' : count.format(view.receipts);
  // Средний чек — целыми сумами: тийины не в обороте, а «146 428,57»
  // читается дольше и обещает точность, которой нет.
  $('average').textContent = view.average === null ? '—' : count.format(Math.round(view.average));
  for (const id of ['revenue-check', 'payment-check']) showCheck($(id), view);
  // Реальная касса: без зачёта предоплат её не посчитать — пишем прямо, не ноль.
  const realUnknown = view.realCash === null;
  $('real-cash').textContent = realUnknown ? 'требует проверки' : money.format(view.realCash);
  $('real-cash').classList.toggle('is-note', realUnknown);
  $('real-cash-unit').hidden = realUnknown;
  showRealCashSub(view);
  $('prepay-credited').textContent = amountText(view.redeemed);
  $('payment-total').textContent = amountText(view.total);
  $('payment-empty').hidden = view.total !== 0;
  $('payment-empty').querySelector('p').textContent = 'За этот день продаж нет';
  $('payments-note').hidden = !view.fallback;
  $('payments-note').textContent = view.fallback
    ? 'iiko не отдал кассы и зачёт предоплат — показаны только оплаты продаж. Обновите день.' : '';
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
  $('day-prev').disabled = day <= '2026-10-05';
  loadPart('expenses', current, loadExpenses(day, current, signal));
  loadPart('receipts', current, loadReceipts(day, current, signal));
  loadPart('rate', current, loadUsdRate(day, current, signal));
  loadPart('shokh', current, loadShokh(day, current, signal));
  loadPart('usd', current, loadUsd(day, current, signal));
  if (!config.configured && !demo) { arrive('day', current); arrive('prepay', current); return true; }
  $('refresh').disabled = true;
  let registry = false;
  const startRegistry = () => {
    registry = true;
    loadPart('prepay', current, loadPrepay(day, current, signal));
  };
  try {
    const endpoint = `/api/cashier/day?date=${encodeURIComponent(day)}&demo=${demo}&allow_stale=true`;
    const first = request(`${endpoint}&refresh=${options.refresh === true}`, signal).then(response => response.json());
    if (!waiting.has('day')) dim('day', first);
    let data = await first;
    if (current !== generation) return false;
    show(data); arrive('day', current);
    // Реестр предоплат читает тот же снимок дня, поэтому — после него;
    // после фонового обновления iiko перечитываем и его.
    const polling = data.refreshing;
    startRegistry();
    // Фоновое обновление iiko: цифры уже на экране, опрос тихий — без полосы
    // сверху; идёт ли он, видно по «↻» и строке статуса.
    for (let attempt = 0; data.refreshing && attempt < 45; attempt++) {
      await waitForRefresh(signal);
      data = await (await request(`${endpoint}&refresh=false`, signal, {retroBusy: false})).json();
      if (current !== generation) return false;
      show(data);
    }
    if (polling) loadPart('prepay', current, loadPrepay(day, current, signal));
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
      if (!registry) arrive('prepay', current);
    }
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
  if (next < '2026-10-05' || (offset > 0 && config && next > config.today)) return;
  $('report-date').value = next;
  load();
}
$('day-prev').addEventListener('click', () => stepDay(-1));
$('day-next').addEventListener('click', () => stepDay(1));
