const $ = id => document.getElementById(id);
const money = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
const rateOfficial = new Intl.NumberFormat('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const rateRestaurant = new Intl.NumberFormat('ru-RU', {minimumFractionDigits: 1, maximumFractionDigits: 1});
const count = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 0});
const colors = ['#24594b','#91a786','#d2b77b','#b2c3aa','#81968c','#ddd2b6','#6b8074'];
const demo = new URLSearchParams(location.search).get('demo') === '1';
let config, snapshot = null, financeData = null, receiptData = null, generation = 0, controller;

function message(text, error = false) {
  $('message').textContent = text;
  $('message').hidden = !text;
  $('message').setAttribute('role', error ? 'alert' : 'status');
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
  $('usd-day').textContent = formattedDay(day);
  $('usd-official').textContent = '—';
  $('usd-restaurant').textContent = '—';
  $('usd-source-day').textContent = 'Дата действия курса ЦБ: —';
  $('usd-status').textContent = 'Загружаем курс ЦБ…';
  $('usd-status').classList.remove('is-error');
  $('usd-balance').value = '';
  $('usd-balance-status').textContent = '';
}
async function loadUsdRate(day, current, signal) {
  if (demo) { $('usd-status').textContent = 'В демонстрационном режиме курс не загружается.'; return; }
  try {
    const data = await RetroState.responseJson(
      await request(`/api/cashier/usd-rate?date=${encodeURIComponent(day)}`, signal));
    if (current !== generation) return;
    $('usd-official').textContent = rateOfficial.format(Number(data.official_rate));
    $('usd-restaurant').textContent = rateRestaurant.format(Number(data.restaurant_rate));
    $('usd-source-day').textContent = 'Курс ЦБ действует с ' + formattedDay(data.source_date);
    $('usd-status').textContent = '';
    const balance = await RetroState.responseJson(
      await request(`/api/cashier/usd-balance?date=${encodeURIComponent(day)}`, signal));
    if (current !== generation) return;
    $('usd-balance').value = balance.amount ?? '';
  } catch (error) {
    if (current === generation && error.name !== 'AbortError') {
      $('usd-status').textContent = error.message;
      $('usd-status').classList.add('is-error');
    }
  }
}
$('usd-balance-save').addEventListener('click', async () => {
  try {
    const data = await RetroState.responseJson(await request('/api/cashier/usd-balance', undefined, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({date:$('report-date').value, amount:$('usd-balance').value})}));
    $('usd-balance').value = data.amount;
    $('usd-balance-status').textContent = 'Сохранено';
  } catch (error) { $('usd-balance-status').textContent = error.message; }
});
async function request(url, signal, options = {}) {
  const response = await fetch(url, {...options, signal, cache:'no-store'});
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; } catch {}
    throw new Error(typeof detail === 'string' ? detail : 'Не удалось загрузить отчёт. Проверьте дату и попробуйте снова.');
  }
  return response;
}
function clearSnapshot() {
  snapshot = null;
  $('download').disabled = true;
  for (const id of ['revenue','receipts','average','payment-total']) $(id).textContent = '—';
  $('payments').replaceChildren();
  $('composition').replaceChildren();
  $('payment-empty').hidden = false;
  $('payment-empty').querySelector('p').textContent = 'Здесь появятся оплаты за выбранный день';
  $('updated').textContent = 'Данные ещё не загружены';
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
  showHandover();
}
function showHandover() {
  const demoAmount = snapshot ? Number(snapshot.payments.find(p => p.name === 'Демо')?.amount || 0) : null;
  const cashPrepay = snapshot ? Number(snapshot.cash_prepayment || 0) : null;
  $('demo-cash').textContent = demoAmount === null ? '—' : money.format(demoAmount);
  $('cash-prepay').textContent = cashPrepay === null ? '—' : money.format(cashPrepay);
  $('total-inflow').textContent = snapshot && receiptData ?
    money.format(Number(snapshot.revenue) + Number(snapshot.new_prepayment || 0) + Number(receiptData.total)) : '—';
  if (!financeData || !receiptData || demoAmount === null) {
    $('handover').textContent = '—';
    $('handover-number').classList.remove('is-negative');
    return;
  }
  const result = demoAmount + cashPrepay + Number(receiptData.total) - Number(financeData.total);
  $('handover').textContent = money.format(result);
  $('handover-number').classList.toggle('is-negative', result < 0);
}
function showReceipts(data) {
  receiptData = data;
  $('receipt-list').replaceChildren();
  $('receipts-empty').hidden = data.receipts.length > 0;
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
      $('receipt-feedback').textContent = error.message;
      $('receipt-feedback').classList.add('is-error');
    }
  }
}
function showExpenses(data) {
  financeData = data;
  $('expense-list').replaceChildren();
  $('expenses-empty').hidden = data.expenses.length > 0;
  $('expense-total').textContent = money.format(Number(data.total));
  $('handover-expenses').textContent = money.format(Number(data.total));
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
    showExpenses({date:day, expenses:[], total:'0'});
    $('expense-feedback').textContent = 'В демонстрационном режиме расходы не сохраняются.';
    return;
  }
  try {
    const data = await (await request(`/api/cashier/expenses?date=${encodeURIComponent(day)}`, signal)).json();
    if (current === generation) showExpenses(data);
  } catch (error) {
    if (current === generation && error.name !== 'AbortError') {
      $('expense-feedback').textContent = error.message;
      $('expense-feedback').classList.add('is-error');
    }
  }
}
function show(data) {
  snapshot = data;
  $('revenue').textContent = money.format(Number(data.revenue));
  $('receipts').textContent = count.format(data.receipt_count);
  $('average').textContent = data.average_receipt === null ? '—' : money.format(Number(data.average_receipt));
  $('payment-total').textContent = money.format(Number(data.payment_total));
  $('payment-empty').hidden = data.receipt_count > 0 || Number(data.revenue) !== 0;
  $('payment-empty').querySelector('p').textContent = 'За этот день продаж нет';
  const positiveTotal = data.payments.reduce((sum,p) => sum + Math.max(0,Number(p.amount)),0);
  data.payments.forEach((payment,i) => {
    const color = colors[i % colors.length];
    const row = document.createElement('div'); row.className = 'payment-row'; row.style.setProperty('--color',color);
    const dot = document.createElement('span'); dot.className = 'payment-dot';
    const name = document.createElement('span'); name.className = 'payment-name'; name.textContent = payment.name;
    const value = document.createElement('span'); value.className = 'payment-value'; value.textContent = money.format(Number(payment.amount));
    const share = document.createElement('small');
    const ratio = Number(data.revenue) > 0 ? Number(payment.amount) / Number(data.revenue) * 100 : null;
    share.textContent = ratio === null ? '' : money.format(ratio) + '% продаж';
    value.append(share); row.append(dot,name,value); $('payments').append(row);
    if (positiveTotal > 0 && Number(payment.amount) > 0) {
      const segment = document.createElement('span');segment.style.setProperty('--color',color);segment.style.width = (Number(payment.amount)/positiveTotal*100)+'%';$('composition').append(segment);
    }
  });
  $('download').disabled = false;
  $('source-title').textContent = data.demo ? 'Демонстрационные данные' : 'Источник: iikoWeb';
  const time = new Intl.DateTimeFormat('ru-RU',{hour:'2-digit',minute:'2-digit',timeZone:'Asia/Tashkent'}).format(new Date(data.fetched_at));
  $('updated').textContent = `${data.demo ? 'Пример сформирован' : 'Обновлено'} в ${time} · Ташкент`;
  showHandover();
}
async function load() {
  const current = ++generation;
  controller?.abort(); controller = new AbortController();
  $('refresh').disabled = false; document.body.classList.remove('loading'); $('metrics').setAttribute('aria-busy','false');
  clearSnapshot(); clearFinance(); message('');
  const day = $('report-date').value;
  if (!config || !day || !$('report-date').checkValidity()) { message('Выберите корректную дату.', true); return; }
  clearUsdRate(day);
  $('period-label').textContent = formattedDay(day);
  $('export-date').textContent = formattedDay(day);
  $('expenses-day').textContent = '· ' + formattedDay(day);
  $('today').classList.toggle('active', day === config.today);
  $('yesterday').classList.toggle('active', day === previousDay(config.today));
  document.querySelectorAll('.date-chip').forEach(button => button.classList.toggle('active', button.dataset.date === day));
  loadExpenses(day, current, controller.signal);
  loadReceipts(day, current, controller.signal);
  loadUsdRate(day, current, controller.signal);
  if (!config.configured && !demo) return;
  $('metrics').setAttribute('aria-busy','true');document.body.classList.add('loading');
  $('refresh').disabled = true;message('Загружаем отчёт из ' + (demo ? 'демонстрационного примера…' : 'iiko…'));
  try {
    const response = await request(`/api/cashier/day?date=${encodeURIComponent(day)}&demo=${demo}`, controller.signal);
    const data = await response.json();
    if (current !== generation) return;
    show(data);message('');
  } catch (error) {
    if (current === generation && error.name !== 'AbortError') message(error.message, true);
  } finally {
    if (current === generation) { $('refresh').disabled = false;document.body.classList.remove('loading');$('metrics').setAttribute('aria-busy','false'); }
  }
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
    $('expense-description').value = ''; $('expense-amount').value = '';
    await loadExpenses(day, current, controller.signal);
    $('expense-feedback').textContent = 'Расход сохранён';
  } catch (error) {
    if (current === generation) {
      $('expense-feedback').textContent = error.message;
      $('expense-feedback').classList.add('is-error');
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
    $('receipt-description').value = ''; $('receipt-amount').value = '';
    await loadReceipts(day, current, controller.signal);
    $('receipt-feedback').textContent = 'Поступление сохранено';
  } catch (error) {
    if (current === generation) {
      $('receipt-feedback').textContent = error.message;
      $('receipt-feedback').classList.add('is-error');
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
$('refresh').addEventListener('click',load);
$('today').addEventListener('click',()=>{if(config){$('report-date').value=config.today;load();}});
$('yesterday').addEventListener('click',()=>{if(config){$('report-date').value=previousDay(config.today);load();}});
function addRecentDateButtons() {
  const container = document.querySelector('.quick-days');
  for (let offset = 2; offset <= 8; offset++) {
    const day = offsetDay(config.today, offset);
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'chip date-chip'; button.dataset.date = day;
    button.textContent = new Intl.DateTimeFormat('ru-RU', {day:'numeric', month:'short', timeZone:'Asia/Tashkent'}).format(new Date(day + 'T12:00:00+05:00')).replace('.', '');
    button.addEventListener('click', () => { $('report-date').value = day; load(); });
    container.append(button);
  }
}
$('download').addEventListener('click',async()=>{
  if (!snapshot) return;
  const data = snapshot, current = generation;
  $('download').disabled=true;
  try {
    const response = await request(`/api/cashier/export?date=${data.date}&snapshot_id=${data.snapshot_id}`);
    const blob = await response.blob();
    if(current !== generation) return;
    const url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href=url; link.download=`${data.demo?'DEMO-':''}Retro-${data.date}.xlsx`;
    document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
    message('Отчёт скачан за '+formattedDay(data.date));
  } catch(error) { if(current===generation) message(error.message,true); }
  finally {if(current===generation) $('download').disabled=!snapshot;}
});
(async()=>{
  try {
    config = await (await request('/api/config')).json();
    $('report-date').max=config.today;$('report-date').value=config.today;
    addRecentDateButtons();
    $('demo-banner').hidden=!demo;$('setup').hidden=config.configured||demo;
    if (demo) {
      for (const input of $('expense-form').querySelectorAll('input, button')) input.disabled = true;
      for (const input of $('receipt-form').querySelectorAll('input, button')) input.disabled = true;
      $('expense-feedback').textContent = 'В демонстрационном режиме расходы не сохраняются.';
      $('receipt-feedback').textContent = 'В демонстрационном режиме поступления не сохраняются.';
    }
    $('connection').textContent=demo?'Демонстрация':config.configured?'iiko настроен':'iiko не подключён';
    $('connection').classList.toggle('connected',config.configured&&!demo);
    await load();
  } catch(error){message(error.message,true);}
})();
