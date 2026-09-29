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
const usdFormat = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});

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
  $('usd-status').textContent = 'Загружаем курс ЦБ…';
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
    const row = document.createElement('div'); row.className = 'usd-deposit';
    const time = document.createElement('span'); time.className = 'usd-deposit-time rm-num';
    time.textContent = clockOf(item.created_at, data.date) || 'за день';
    const note = document.createElement('span'); note.className = 'usd-deposit-note';
    note.textContent = 'в сейф' + (usdRate ? ' · ≈ ' + shortSum(Number(item.amount) * usdRate) : '');
    const value = document.createElement('strong'); value.className = 'rm-num';
    value.textContent = usdFormat.format(Number(item.amount)) + ' USD';
    const remove = document.createElement('button'); remove.className = 'usd-remove'; remove.type = 'button';
    remove.textContent = '×'; remove.setAttribute('aria-label', `Удалить взнос ${usdFormat.format(Number(item.amount))} USD`);
    remove.disabled = demo;
    remove.addEventListener('click', () => deleteUsd(item.id, data.date, remove));
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
$('usd-form').addEventListener('submit', async event => {
  event.preventDefault();
  const day = $('report-date').value, current = generation, value = CashierLogic.parseAmount($('usd-amount').value);
  if (demo || !day) return;
  if (!value) { showUsdHint('Введите сумму в долларах.', true); $('usd-amount').focus(); return; }
  const button = $('usd-add'); button.disabled = true;
  try {
    const data = await RetroState.responseJson(await request('/api/cashier/usd-deposits', undefined, {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({date:day, amount:String(value)})}));
    if (current !== generation) return;
    usdData = data; $('usd-amount').value = ''; showUsd();
    const text = 'Положено в сейф: ' + usdFormat.format(value) + ' USD. Бухгалтер видит в «Резервах».';
    showUsdHint(text); globalThis.RetroToast?.show(text);
  } catch (error) {
    if (current === generation) { showUsdHint(error.message, true); globalThis.RetroToast?.show(error.message, 'error'); }
  } finally { button.disabled = demo; }
});
async function deleteUsd(id, day, button) {
  const current = generation;
  button.disabled = true;
  try {
    await request(`/api/cashier/usd-deposits/${id}?date=${encodeURIComponent(day)}`, undefined, {method:'DELETE'});
    if (current === generation) { await loadUsd(day, current, controller.signal); showUsdHint('Взнос удалён'); }
  } catch (error) {
    if (current === generation) { showUsdHint(error.message, true); button.disabled = false; globalThis.RetroToast?.show(error.message, 'error'); }
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
  handoverRecord = null;
  $('shift-pill').hidden = true;
  $('download').disabled = true;
  for (const id of ['revenue','receipts','average','payment-total']) $(id).textContent = '—';
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
  $('shokh-pocket').textContent = '—';
  $('handover-feedback').textContent = '';
  showHandover();
}
/* Всё, что ушло из кассы наличными: расходы кассира и выдачи Шоху. Пока
   одна из частей не загрузилась, суммы нет — неполная цифра хуже прочерка. */
function cashOutTotal() {
  if (!financeData || !shokhData) return null;
  return Number(financeData.total) + Number(shokhData.total);
}
function currentHandover() {
  return CashierLogic.handover(snapshot, cashOutTotal(), receiptData && receiptData.total);
}
function showHandover() {
  $('download').disabled = !snapshot || snapshot.stale || snapshot.refreshing || !financeData || !receiptData;
  const demoAmount = CashierLogic.cashPayment(snapshot);
  const cashPrepay = snapshot ? Number(snapshot.cash_prepayment || 0) : null;
  $('demo-cash').textContent = demoAmount === null ? '—' : money.format(demoAmount);
  const prepayText = cashPrepay === null ? '—' : money.format(cashPrepay);
  // Предоплаты показаны и карточкой сверху, и строкой в расчёте передачи.
  $('cash-prepay').textContent = prepayText;
  $('card-prepay').textContent = prepayText;
  const inflow = CashierLogic.totalInflow(snapshot, receiptData && receiptData.total);
  $('total-inflow').textContent = inflow === null ? '—' : money.format(inflow);
  $('payments-inflow').textContent = inflow === null ? '—' : money.format(inflow);
  // Полоса в главной карточке: продажи против всего остального прихода.
  const sales = snapshot ? Number(snapshot.revenue || 0) : 0;
  const salesShare = inflow ? Math.max(0, Math.min(100, sales / inflow * 100)) : 0;
  const [salesBar, otherBar] = $('composition').children;
  salesBar.style.width = (inflow ? salesShare : 0) + '%';
  otherBar.style.width = (inflow ? 100 - salesShare : 0) + '%';
  // Предоплаты наличными — автоматическая строка в «В кассу», как в макете.
  $('receipt-auto').hidden = !snapshot;
  $('receipt-auto-value').textContent = prepayText;
  $('receipts-empty').hidden = Boolean(snapshot) || Boolean(receiptData && receiptData.receipts.length);
  const cashOut = cashOutTotal();
  $('expense-total').textContent = cashOut === null ? '—' : money.format(cashOut);
  $('handover-expenses').textContent = cashOut === null ? '—' : money.format(cashOut);
  const result = currentHandover();
  $('handover').textContent = result === null ? '—' : money.format(result);
  $('handover-number').classList.toggle('is-negative', result !== null && result < 0);
  showHandoverAction(result);
}
/* Кнопка «Передать бухгалтеру» и отметка «Передано в 21:40». */
function showHandoverAction(result) {
  const record = handoverRecord, view = CashierLogic.handoverView(record, result);
  const live = Boolean(snapshot && !snapshot.demo && !snapshot.stale && !snapshot.refreshing);
  const ready = !demo && live && result !== null && result >= 0 && !handoverBusy;
  $('handover-action').hidden = view.state !== 'none';
  $('handover-button').disabled = !ready;
  $('handover-done').hidden = view.state === 'none';
  $('handover-diff').hidden = view.state !== 'diff';
  $('handover-undo').hidden = view.state === 'accountant';
  $('handover-undo').disabled = handoverBusy;
  $('handover-again').disabled = !ready;
  // «Передано: …» больше не про текущую сумму, раз она изменилась.
  if (view.state === 'diff' && !$('handover-feedback').classList.contains('is-error')) $('handover-feedback').textContent = '';
  if (!record) return;
  const day = snapshot ? snapshot.date : $('report-date').value;
  const at = clockOf(record.handed_at, day);
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
async function handOver() {
  const day = $('report-date').value, current = generation, amount = currentHandover();
  if (demo || !snapshot || amount === null || handoverBusy) return;
  handoverBusy = true; showHandover();
  try {
    const data = await RetroState.responseJson(await request('/api/cashier/handover', undefined, {method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({date:day, snapshot_id:snapshot.snapshot_id, expected:amount.toFixed(2)})}));
    if (current !== generation) return;
    handoverRecord = data.handover;
    handoverMessage('Передано бухгалтеру: ' + money.format(Number(data.handover.amount)) + ' сум.');
  } catch (error) {
    if (current !== generation) return;
    handoverMessage(error.message, true);
    // Сумма разошлась или снимок устарел — показываем свежие цифры, передать можно снова.
    await Promise.all([loadExpenses(day, current, controller.signal), loadReceipts(day, current, controller.signal),
      loadShokh(day, current, controller.signal)]);
    if (/iiko|Обновите/.test(error.message)) load({refresh:true});
  } finally {
    handoverBusy = false;
    if (current === generation) showHandover();
  }
}
async function undoHandover() {
  const day = $('report-date').value, current = generation;
  if (demo || handoverBusy) return;
  handoverBusy = true; showHandover();
  try {
    await request(`/api/cashier/handover?date=${encodeURIComponent(day)}`, undefined, {method:'DELETE'});
    if (current !== generation) return;
    handoverRecord = null;
    handoverMessage('Передача отменена. Бухгалтер снова видит сумму как ожидаемую.');
  } catch (error) {
    if (current === generation) handoverMessage(error.message, true);
  } finally {
    handoverBusy = false;
    if (current === generation) showHandover();
  }
}
$('handover-button').addEventListener('click', handOver);
$('handover-again').addEventListener('click', handOver);
$('handover-undo').addEventListener('click', undoHandover);

/* ── Выдать Шоху из кассы ─────────────────────────────────────────────── */
function showShokh(data) {
  shokhData = data;
  $('shokh-gives').replaceChildren();
  $('shokh-pocket').textContent = data.pocket === null ? 'не задан' : money.format(Number(data.pocket));
  for (const item of data.gives) {
    const row = document.createElement('div'); row.className = 'expense-item is-shokh';
    const name = document.createElement('span'); name.className = 'expense-item-name';
    const at = clockOf(item.created_at, data.date);
    name.textContent = 'Шоху на закуп' + (at ? ' · ' + at : '');
    const value = document.createElement('span'); value.className = 'expense-item-value'; value.textContent = money.format(Number(item.amount));
    const remove = document.createElement('button'); remove.className = 'expense-remove'; remove.type = 'button';
    remove.textContent = '×'; remove.setAttribute('aria-label', `Удалить выдачу Шоху ${money.format(Number(item.amount))}`);
    remove.addEventListener('click', () => deleteShokh(item.id, data.date, remove));
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
$('shokh-form').addEventListener('submit', async event => {
  event.preventDefault();
  const day = $('report-date').value, current = generation, value = CashierLogic.parseAmount($('shokh-amount').value);
  if (demo || !day) return;
  $('expense-feedback').classList.remove('is-error');
  if (!value) {
    $('expense-feedback').textContent = 'Укажите сумму для Шоха.'; $('expense-feedback').classList.add('is-error');
    $('shokh-amount').focus(); return;
  }
  const button = $('shokh-give'); button.disabled = true;
  try {
    const data = await RetroState.responseJson(await request('/api/cashier/shokh', undefined, {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({date:day, amount:String(value)})}));
    if (current !== generation) return;
    $('shokh-amount').value = '';
    showShokh(data);
    const text = 'Выдано Шоху ' + money.format(value) + ' сум. Баланс Шоха и отчёт бухгалтера обновлены.';
    $('expense-feedback').textContent = text; globalThis.RetroToast?.show(text);
  } catch (error) {
    if (current === generation) {
      $('expense-feedback').textContent = error.message; $('expense-feedback').classList.add('is-error');
      globalThis.RetroToast?.show(error.message, 'error');
    }
  } finally { button.disabled = demo; }
});
async function deleteShokh(id, day, button) {
  const current = generation;
  button.disabled = true;
  try {
    await request(`/api/cashier/shokh/${id}?date=${encodeURIComponent(day)}`, undefined, {method:'DELETE'});
    if (current === generation) {
      await loadShokh(day, current, controller.signal);
      $('expense-feedback').textContent = 'Выдача Шоху удалена';
      $('expense-feedback').classList.remove('is-error');
    }
  } catch (error) {
    if (current === generation) {
      $('expense-feedback').textContent = error.message;
      $('expense-feedback').classList.add('is-error');
      button.disabled = false;
    }
  }
}
function showReceipts(data) {
  receiptData = data;
  $('receipt-list').replaceChildren();
  $('receipts-empty').hidden = data.receipts.length > 0 || Boolean(snapshot);
  $('receipt-total').textContent = money.format(Number(data.total));
  $('handover-receipts').textContent = money.format(Number(data.total));
  for (const item of data.receipts) {
    const row = document.createElement('div'); row.className = 'expense-item';
    const name = document.createElement('span'); name.className = 'expense-item-name'; name.textContent = item.description;
    const value = document.createElement('span'); value.className = 'expense-item-value'; value.textContent = money.format(Number(item.amount));
    const remove = document.createElement('button'); remove.className = 'expense-remove'; remove.type = 'button';
    remove.textContent = '×'; remove.setAttribute('aria-label', `Удалить поступление «${item.description}»`);
    remove.addEventListener('click', () => deleteReceipt(item.id, data.date, remove));
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
    const name = document.createElement('span'); name.className = 'expense-item-name'; name.textContent = item.description;
    const value = document.createElement('span'); value.className = 'expense-item-value'; value.textContent = money.format(Number(item.amount));
    if (item.automatic) {
      const marker = document.createElement('span'); marker.className = 'expense-automatic'; marker.textContent = 'Авто';
      marker.setAttribute('aria-label', 'Добавляется автоматически каждый день');
      row.append(name, value, marker);
    } else {
      const remove = document.createElement('button'); remove.className = 'expense-remove'; remove.type = 'button';
      remove.textContent = '×'; remove.setAttribute('aria-label', `Удалить расход «${item.description}»`);
      remove.addEventListener('click', () => deleteExpense(item.id, data.date, remove));
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
  status.textContent = `${source} ${time}${snapshot.stale ? ' · требуют обновления' : ''}`;
}
function show(data) {
  snapshot = data;
  if ('handover' in data) handoverRecord = data.handover;
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
  $('payment-total').textContent = money.format(Number(data.payment_total));
  $('payment-empty').hidden = data.receipt_count > 0 || Number(data.revenue) !== 0;
  $('payment-empty').querySelector('p').textContent = 'За этот день продаж нет';
  const positiveTotal = data.payments.reduce((sum,p) => sum + Math.max(0,Number(p.amount)),0);
  data.payments.forEach((payment,i) => {
    const color = colors[i % colors.length];
    const row = document.createElement('div'); row.className = 'payment-row'; row.style.setProperty('--color',color);
    const dot = document.createElement('span'); dot.className = 'payment-dot';
    const name = document.createElement('span'); name.className = 'payment-name';
    const label = document.createElement('span'); label.className = 'payment-label'; label.textContent = payment.name;
    name.append(label);
    const share = document.createElement('span'); share.className = 'payment-share';
    const ratio = positiveTotal > 0 ? Math.max(0, Number(payment.amount)) / positiveTotal * 100 : null;
    share.textContent = ratio === null ? '' : ratio.toLocaleString('ru-RU', {minimumFractionDigits:1, maximumFractionDigits:1}) + '%';
    const value = document.createElement('strong'); value.className = 'payment-value'; value.textContent = money.format(Number(payment.amount));
    // Способы без единой транзакции остаются в списке (видно, что их
    // проверяли), но гаснут и не спорят за внимание с теми, где были деньги.
    if (Number(payment.amount) === 0) row.classList.add('is-zero');
    if (payment.name === CashierLogic.CASH_PAYMENT) {
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
async function load(options = {}) {
  const current = ++generation;
  controller?.abort(); controller = new AbortController();
  const signal = controller.signal;
  $('refresh').disabled = false; document.body.classList.remove('loading'); $('metrics').setAttribute('aria-busy','false');
  const day = $('report-date').value;
  const keepSnapshot = snapshot?.date === day;
  if (!keepSnapshot) clearSnapshot();
  $('download').disabled = true;
  if (!keepSnapshot) clearFinance();
  message('');
  if (!config || !day || !$('report-date').checkValidity()) { message('Выберите корректную дату.', true); return; }
  clearUsdRate(day);
  $('report-date-text').textContent = shortDay(day);
  const isToday = day === config.today, isYesterday = day === previousDay(config.today);
  $('today').classList.toggle('is-active', isToday);
  $('today').setAttribute('aria-pressed', String(isToday));
  $('yesterday').classList.toggle('is-active', isYesterday);
  $('yesterday').setAttribute('aria-pressed', String(isYesterday));
  $('day-next').disabled = day >= config.today;
  loadExpenses(day, current, controller.signal);
  loadReceipts(day, current, controller.signal);
  loadUsdRate(day, current, controller.signal);
  loadShokh(day, current, controller.signal);
  loadUsd(day, current, controller.signal);
  if (!config.configured && !demo) return;
  $('metrics').setAttribute('aria-busy','true');
  if (!keepSnapshot) document.body.classList.add('loading');
  $('refresh').disabled = true;message(keepSnapshot ? 'Обновляем отчёт. На экране предыдущие данные…' : 'Загружаем отчёт…');
  try {
    const endpoint = `/api/cashier/day?date=${encodeURIComponent(day)}&demo=${demo}&allow_stale=true`;
    const response = await request(`${endpoint}&refresh=${options.refresh === true}`, signal);
    let data = await response.json();
    if (current !== generation) return;
    show(data);
    document.body.classList.remove('loading');
    if (data.refreshing) message('Обновляем iiko. На экране последние сохранённые данные…');
    for (let attempt = 0; data.refreshing && attempt < 45; attempt++) {
      await waitForRefresh(signal);
      data = await (await request(`${endpoint}&refresh=false`, signal)).json();
      if (current !== generation) return;
      show(data);
    }
    message(data.refresh_error || (data.refreshing ? 'Обновление продолжается. Повторите проверку позже.' :
      data.stale ? 'Показаны последние сохранённые данные. Требуется обновление iiko.' : ''), Boolean(data.refresh_error || data.stale));
  } catch (error) {
    if (current === generation && error.name !== 'AbortError') {
      if (snapshot?.date === day) {
        snapshot = {...snapshot, stale:true, refreshing:false};
        show(snapshot);
      }
      message(error.message + (snapshot?.date === day ? ' На экране предыдущие данные.' : ''), true);
    }
  } finally {
    if (current === generation) { $('refresh').disabled = false;document.body.classList.remove('loading');$('metrics').setAttribute('aria-busy','false'); }
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
$('expense-form').addEventListener('submit', async event => {
  event.preventDefault();
  const day = $('report-date').value, current = generation;
  if (demo || !day || !$('expense-form').reportValidity()) return;
  const button = $('expense-save'); button.disabled = true;
  $('expense-feedback').textContent = '';
  $('expense-feedback').classList.remove('is-error');
  try {
    await request('/api/cashier/expenses', undefined, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({date:day, description:$('expense-description').value, amount:$('expense-amount').value})
    });
    if (current !== generation) return;
    $('expense-description').value = ''; $('expense-amount').value = ''; $('expense-amount').dispatchEvent(new Event('input', {bubbles: true}));
    await loadExpenses(day, current, controller.signal);
    $('expense-feedback').textContent = 'Расход сохранён'; globalThis.RetroToast?.show('Расход сохранён'); $('expense-description').focus({preventScroll: true});
  } catch (error) {
    if (current === generation) {
      $('expense-feedback').textContent = error.message;
      $('expense-feedback').classList.add('is-error');
      globalThis.RetroToast?.show(error.message, 'error');
    }
  } finally { button.disabled = demo; }
});
$('receipt-form').addEventListener('submit', async event => {
  event.preventDefault();
  const day = $('report-date').value, current = generation;
  if (demo || !day || !$('receipt-form').reportValidity()) return;
  const button = $('receipt-save'); button.disabled = true;
  $('receipt-feedback').textContent = '';
  $('receipt-feedback').classList.remove('is-error');
  try {
    await request('/api/cashier/receipts', undefined, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({date:day, description:$('receipt-description').value, amount:$('receipt-amount').value})
    });
    if (current !== generation) return;
    $('receipt-description').value = ''; $('receipt-amount').value = ''; $('receipt-amount').dispatchEvent(new Event('input', {bubbles: true}));
    await loadReceipts(day, current, controller.signal);
    $('receipt-feedback').textContent = 'Поступление сохранено'; globalThis.RetroToast?.show('Поступление сохранено'); $('receipt-description').focus({preventScroll: true});
  } catch (error) {
    if (current === generation) {
      $('receipt-feedback').textContent = error.message;
      $('receipt-feedback').classList.add('is-error');
      globalThis.RetroToast?.show(error.message, 'error');
    }
  } finally { button.disabled = demo; }
});
async function deleteReceipt(id, day, button) {
  const current = generation;
  button.disabled = true;
  try {
    await request(`/api/cashier/receipts/${id}?date=${encodeURIComponent(day)}`, undefined, {method:'DELETE'});
    if (current === generation) {
      await loadReceipts(day, current, controller.signal);
      $('receipt-feedback').textContent = 'Поступление удалено';
      $('receipt-feedback').classList.remove('is-error');
    }
  } catch (error) {
    if (current === generation) {
      $('receipt-feedback').textContent = error.message;
      $('receipt-feedback').classList.add('is-error');
      button.disabled = false;
    }
  }
}
async function deleteExpense(id, day, button) {
  const current = generation;
  button.disabled = true;
  try {
    await request(`/api/cashier/expenses/${id}?date=${encodeURIComponent(day)}`, undefined, {method:'DELETE'});
    if (current === generation) {
      await loadExpenses(day, current, controller.signal);
      $('expense-feedback').textContent = 'Расход удалён';
      $('expense-feedback').classList.remove('is-error');
    }
  } catch (error) {
    if (current === generation) {
      $('expense-feedback').textContent = error.message;
      $('expense-feedback').classList.add('is-error');
      button.disabled = false;
    }
  }
}
$('report-date').addEventListener('change',load);
// Нажатие на подпись даты открывает календарь и там, где прозрачное поле
// поверх подписи само его не показывает.
$('report-date').addEventListener('click', event => { try { event.target.showPicker?.(); } catch {} });
$('refresh').addEventListener('click',()=>load({refresh:true}));
$('today').addEventListener('click',()=>{if(config){$('report-date').value=config.today;load();}});
$('yesterday').addEventListener('click',()=>{if(config){$('report-date').value=previousDay(config.today);load();}});
$('download').addEventListener('click',async()=>{
  if (!snapshot || snapshot.stale || snapshot.refreshing || !financeData || !receiptData) return;
  const data = snapshot, current = generation;
  $('download').disabled=true;
  try {
    const response = await request(`/api/cashier/export?date=${data.date}&snapshot_id=${data.snapshot_id}${financeData.revision ? "&expense_revision=" + financeData.revision : ""}${receiptData.revision ? "&receipt_revision=" + receiptData.revision : ""}`);
    const blob = await response.blob();
    if(current !== generation) return;
    const url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href=url; link.download=`${data.demo?'DEMO-':''}Retro-${data.date}.xlsx`;
    document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
    message('Отчёт скачан за '+formattedDay(data.date));
  } catch(error) { if(current===generation) message(error.message,true); }
  finally {if(current===generation) showHandover();}
});
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
  } catch(error){message(error.message,true);}
})();

// ── Дата-навигация ──────────────────────────────────────────────────────────
// Стрелки листают по одному дню; вперёд дальше сегодняшнего не уходим —
// отчёта за будущий день не существует.
function stepDay(offset) {
  const current = $('report-date').value;
  if (!current) return;
  const next = offsetDay(current, -offset);
  if (offset > 0 && config && next > config.today) return;
  $('report-date').value = next;
  load();
}
$('day-prev').addEventListener('click', () => stepDay(-1));
$('day-next').addEventListener('click', () => stepDay(1));
