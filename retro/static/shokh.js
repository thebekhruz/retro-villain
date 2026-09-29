const $ = id => document.getElementById(id);
const money = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
const quantityText = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 3});
const L = () => globalThis.ShokhLogic;

// Сервер хранит историю. Незавершённая отправка сохраняет ключ и черновик
// в этой вкладке, чтобы после перезагрузки продолжить ту же покупку.
let state = {
  screen: 'home', step: 'point', tripId: null, tripStartedAt: null,
  draft: {point: '', item: '', unit: 'кг', quantity: '', price: '', priceMode: 'unit', priceInput: '', hasPhoto: false},
  photoFile: null, usual: null, catalog: {points: [], units: ['кг'], items: []},
  home: null, timer: null, lastPocket: null, catalogReady: false, submitting: false, recovery: null, expectedPhoto: false
};

function message(text, error = false) {
  const box = $('shokh-message');
  box.textContent = text; box.hidden = !text;
  box.classList.toggle('is-error', error);
  box.setAttribute('role', error ? 'alert' : 'status');
  globalThis.RetroToast?.show(text, error ? 'error' : 'ok');
}
function node(tag, cls, text) {
  const element = document.createElement(tag);
  if (cls) element.className = cls;
  if (text !== undefined) element.textContent = text;
  return element;
}
function show(screen) {
  state.screen = screen;
  for (const name of ['home', 'flow', 'done', 'summary']) {
    $('screen-' + name).hidden = name !== screen;
  }
  document.body.classList.toggle('is-dark-screen', screen === 'done');
  window.scrollTo(0, 0);
}
async function api(path, options = {}) {
  const sender = fetch; // Purchase identity is an explicit UUID, independent of multipart boundaries.
  const response = await sender('/api/shokh' + path, {cache: 'no-store', ...options});
  let payload = null;
  try { payload = await response.json(); } catch {}
  if (!response.ok) {
    throw new Error(typeof payload?.detail === 'string' ? payload.detail : (response.status === 401 ? 'Войдите в панель заново.' : 'Не удалось загрузить данные. Нажмите «Обновить данные».'));
  }
  return payload;
}

/* ── Главная ───────────────────────────────────────────────────────────── */
function renderHome(data) {
  state.home = data;
  state.lastPocket = data.pocket;
  $('home-date').textContent = 'Закуп · ' + new Intl.DateTimeFormat('ru-RU',
    {weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC'})
    .format(new Date(data.date + 'T12:00:00Z'));
  // Подотчёт ещё не заведён — цифры нет, и придумывать её нельзя.
  const unknown = data.pocket === null;
  $('home-pocket').textContent = unknown ? 'Не задан' : money.format(Number(data.pocket));
  $('home-pocket-note').textContent = unknown ? 'бухгалтер ещё не выдал подотчёт' : 'сум';
  // Строка под суммой, как в макете: откуда деньги и сколько уже ушло.
  const issued = data.accounting_balance == null ? null : Number(data.accounting_balance);
  $('home-pocket-sub').textContent = [
    issued === null ? '' : 'Подотчёт ' + money.format(issued),
    'потрачено сегодня ' + money.format(Number(data.spent_today || 0)),
  ].filter(Boolean).join(' · ');

  const share = L().reportedShare(data.pocket, data.pending);
  $('home-reported').textContent = share === null
    ? 'Подотчёт не заведён'
    : 'Отчитались за ' + share + '% выданных денег';
  $('home-reported-bar').style.width = (share || 0) + '%';

  // Перечисления поставщикам за сегодня (поле transfers в ответе /home).
  const transfers = Array.isArray(data.transfers) ? data.transfers : [];
  $('home-transfers-card').hidden = !transfers.length;
  $('home-transfers').replaceChildren(...transfers.map(row => {
    // Поставщик, товар и точка — данные, переводчик их не трогает; «сум» — да.
    const item = node('div', 'shokh-transfer');
    // Пустой товар сервер отдаёт как «—»: в заголовке он лишний.
    const title = node('span', '', [row.supplier, row.item].filter(value => value && value !== '—').join(' · '));
    title.dataset.i18n = 'off';
    const sub = node('small');
    if (row.point) { const point = node('span', '', row.point); point.dataset.i18n = 'off'; sub.append(point); }
    if (row.amount != null) sub.append((row.point ? ' · ' : '') + money.format(Number(row.amount)) + ' сум');
    item.append(title, sub);
    return item;
  }));

  $('home-pending-card').hidden = Number(data.pending) <= 0;
  $('home-pending-note').textContent = 'На ' + money.format(Number(data.pending)) +
    ' сум — бухгалтер ещё не принял накладные. Эти деньги уже не на руках.';

  // Как в макете: «Сегодня · 4 покупки»; сколько потрачено — в шапке.
  const count = data.purchases.length;
  $('home-purchases-title').textContent = count
    ? 'Сегодня · ' + count + ' ' + purchasesWord(count) : 'Сегодня покупок нет';
  renderPurchases($('home-purchases'), data.purchases);
}

function renderPurchases(container, rows) {
  container.replaceChildren();
  if (!rows.length) {
    container.append(node('p', 'shokh-note', 'Покупок пока нет. Начните закуп кнопкой ниже.'));
    return;
  }
  [...rows].reverse().forEach(row => {
    const item = node('div', 'shokh-purchase');
    if (row.has_photo) {
      const image = document.createElement('img');
      image.className = 'shokh-thumb'; image.alt = '';
      image.src = '/api/shokh/photo/' + row.id;
      item.append(image);
    } else {
      item.append(node('span', 'shokh-thumb is-empty', row.item.slice(0, 1).toUpperCase()));
    }
    const body = node('div', 'shokh-purchase-body');
    const title = node('div', 'shokh-purchase-title');
    const itemName = node('span', '', row.item);
    itemName.dataset.i18n = 'off';  // название из iiko
    title.append(itemName);
    if (!row.has_photo) title.append(node('span', 'shokh-flag', 'нет фото'));
    if (row.price_above_usual) title.append(node('span', 'shokh-flag', 'дороже обычного'));
    if (row.accepted_at) title.append(node('span', 'shokh-ok', 'принято'));
    // Как в макете: «10 кг · RETRO». Количество без хвостовых нулей
    // («3.000» → «3»), точка закупа — данные, её не переводим.
    const sub = node('div', 'shokh-note', quantityText.format(Number(row.quantity)) + ' ' + row.unit + ' · ');
    const where = node('span', '', row.point); where.dataset.i18n = 'off';
    sub.append(where);
    body.append(title, sub);
    if (row.iiko && row.iiko.status !== 'legacy') {
      const synced = row.iiko.status === 'synced';
      body.append(node('small', synced ? 'shokh-ok' : 'shokh-warning', synced
        ? 'iiko · накладная № ' + (row.iiko.number || '—')
        : row.iiko.status === 'rejected' ? 'iiko отклонил: ' + row.iiko.error
        : 'Ожидает подтверждения iiko. Не вводите покупку повторно.'));
      if (!synced) {
        const retry = ['rejected', 'pending'].includes(row.iiko.status);
        const check = node('button', 'shokh-secondary', retry ? 'Повторить отправку' : 'Проверить iiko');
        check.type = 'button';
        check.addEventListener('click', async () => {
          check.disabled = true;
          try {
            const result = await api('/purchase/' + row.id + (retry ? '/retry' : '/sync'),
              retry ? {method: 'POST'} : {});
            await loadHome();
            if (result.purchase.iiko.status !== 'synced') message(result.purchase.iiko.error || 'iiko ещё не подтвердил накладную.', true);
          } catch (error) { message(error.message, true); }
          finally { check.disabled = false; }
        });
        body.append(check);
      }
    }
    item.append(body, node('strong', 'rm-num', money.format(Number(row.total))));
    container.append(item);
  });
}

async function loadHome() {
  try {
    const data = await api('/home');
    renderHome(data);
    $('start-purchase').disabled = !state.catalogReady || !!sessionStorage.getItem('shokh-pending-operation');
  } catch (error) { message(error.message, true); }
}

/* ── Флоу ──────────────────────────────────────────────────────────────── */
const STEP_NAMES = {point: 'Точка', item: 'Товар', amount: 'Сколько', confirm: 'Проверка'};

function renderSegments() {
  const box = $('flow-segments'); box.replaceChildren();
  const index = L().STEPS.indexOf(state.step);
  L().STEPS.forEach((_, position) => {
    box.append(node('div', 'shokh-segment' + (position <= index ? ' is-on' : '')));
  });
}

function ready(step) {
  if (step === 'point') return !!state.draft.supplierId && !!state.draft.storageId;
  if (step === 'item') return !!state.draft.productId && (!state.expectedPhoto || !!state.photoFile);
  return L().stepReady(step, state.draft);
}

function renderStep() {
  const step = state.step, draft = state.draft;
  for (const name of L().STEPS) $('step-' + name).hidden = name !== step;
  $('flow-step-no').textContent = String(L().STEPS.indexOf(step) + 1);
  $('flow-step-name').textContent = STEP_NAMES[step];
  renderSegments();
  $('flow-back').disabled = step === 'point';
  $('flow-next').disabled = !ready(step);
  $('flow-next').textContent = step === 'confirm' ? 'Сохранить в iiko' : 'Далее';

  if (step === 'item') {
    $('item-point-label').textContent = draft.point || '—';
    renderItems();
  }
  if (step === 'amount') {
    $('amount-point').textContent = draft.point;
    $('amount-item').textContent = draft.item;
    renderUnits();
    renderAmount();
  }
  if (step === 'confirm') renderConfirm();
}

function purchasesWord(count) {
  if (count % 10 === 1 && count % 100 !== 11) return 'покупка';
  if ([2, 3, 4].includes(count % 10) && ![12, 13, 14].includes(count % 100)) return 'покупки';
  return 'покупок';
}

function renderPoints() {
  const box = $('point-list'); box.replaceChildren();
  const today = (state.home && state.home.purchases) || [];
  state.catalog.points.forEach(point => {
    const button = node('button', 'shokh-point' + (state.draft.point === point ? ' is-active' : ''));
    button.type = 'button';
    button.setAttribute('aria-pressed', String(state.draft.point === point));
    // Под названием — сколько раз здесь уже покупали сегодня, как в макете.
    const count = today.filter(row => row.point === point).length;
    const name = node('span', 'shokh-point-name');
    const pointName = node('span', '', point);
    pointName.dataset.i18n = 'off';  // точка — данные, не перевод
    name.append(pointName);
    name.append(node('small', '', count ? 'сегодня: ' + count + ' ' + purchasesWord(count) : 'сегодня не были'));
    button.append(node('span', 'shokh-point-mark', point.slice(0, 2).toUpperCase()), name);
    button.addEventListener('click', () => {
      state.draft.point = point;
      $('point-other').value = '';
      renderPoints();
      $('flow-next').disabled = !ready('point');
      // Точку выбрали — сразу к товару, как в макете; но только когда
      // поставщик и склад iiko уже выбраны, иначе накладную не провести.
      if (ready('point')) { state.step = L().nextStep('point'); renderStep(); }
    });
    box.append(button);
  });
}

function renderItems() {
  const query = $('item-search').value.trim().toLowerCase();
  const box = $('item-list'); box.replaceChildren();
  const matches = state.catalog.items.filter(row => !query || row.item.toLowerCase().includes(query) || String(row.code).includes(query));
  $('item-list-title').textContent = query ? 'Найдено в iiko' : 'Товары iiko';
  matches.slice(0, 10).forEach(row => {
    const button = node('button', 'shokh-item' + (state.draft.item === row.item ? ' is-active' : ''));
    button.type = 'button';
    // Как в макете: под названием — обычная цена, если она уже есть в истории.
    const itemName = node('span', 'shokh-item-name', row.item);
    itemName.dataset.i18n = 'off';
    button.append(itemName,
      node('small', '', row.usual_price != null
        ? 'обычно ' + money.format(Number(row.usual_price)) + ' / ' + row.unit
        : 'Арт. ' + row.code + ' · ' + row.unit));
    button.addEventListener('click', () => {
      state.draft.item = row.item;
      state.draft.productId = row.id;
      state.draft.unitId = row.unit_id;
      state.draft.unit = state.catalog.units.includes(row.unit) ? row.unit : state.draft.unit;
      $('item-search').value = '';
      chooseItem();
    });
    box.append(button);
  });
  if (!matches.length) box.append(node('p', 'shokh-note', 'Товар не найден в iiko. Уточните название или добавьте его в справочник iiko.'));
  // Своего товара в списке нет — предлагаем добавить введённое как есть.
  const custom = $('item-search').value.trim();
  const exact = state.catalog.items.some(row => row.item.toLowerCase() === custom.toLowerCase());
  $('item-add-custom').hidden = true;
  $('item-add-custom').textContent = custom ? 'Добавить «' + custom + '»' : '';
  $('item-chosen').hidden = !state.draft.item;
  const chosen = node('span', '', state.draft.item);
  chosen.dataset.i18n = 'off';
  $('item-chosen').replaceChildren('Выбрано: ', chosen);
}

async function chooseItem() {
  renderItems();
  $('flow-next').disabled = !ready('item');
  // Обычную цену спрашиваем у сервера: она считается по истории этого товара.
  state.usual = null;
  try {
    const found = state.catalog.items.find(row => row.id === state.draft.productId);
    state.usual = found && found.usual_price !== undefined ? found.usual_price : null;
  } catch { state.usual = null; }
}

function renderUnits() {
  const box = $('unit-switch'); box.replaceChildren();
  // Единица приходит из карточки товара iiko и одна — это подпись, а не
  // переключатель, по ней нечего нажимать.
  box.append(node('span', 'shokh-switch-btn is-on', state.draft.unit));
  // Одной строкой: в узбекском порядок слов другой («1 kg uchun»).
  $('price-mode-unit').textContent = 'за 1 ' + state.draft.unit;
  const quick = $('qty-quick'); quick.replaceChildren();
  // Быстрые кнопки прибавляют, как «+1 кг / +5 кг / +10 кг» в макете.
  [1, 5, 10].forEach(value => {
    const button = node('button', 'shokh-quick-btn', '+' + value + ' ' + state.draft.unit);
    button.type = 'button';
    button.addEventListener('click', () => {
      const next = (L().number(state.draft.quantity) || 0) + value;
      state.draft.quantity = String(Math.round(next * 1000) / 1000);
      $('qty-input').value = state.draft.quantity; renderAmount();
    });
    quick.append(button);
  });
}

/* Цена, которая уйдёт на сервер. «За 1 кг» — то, что ввели. «За всё» —
   цена за единицу из введённой суммы, подобранная так, чтобы итог сервера
   (и накладной iiko) совпал с суммой, а если ровно нельзя — был ближайшим. */
function applyPrice() {
  const draft = state.draft;
  const typed = String(draft.priceInput || '').replace(/[\s\u00a0\u202f]/g, '');
  state.priceFit = null;
  if (draft.priceMode === 'total') {
    state.priceFit = typed ? L().priceFromTotal(draft.quantity, typed) : null;
    draft.price = state.priceFit ? state.priceFit.price : '';
  } else {
    draft.price = typed;
  }
}

function renderPriceMode() {
  $('price-mode').querySelectorAll('[data-mode]').forEach(button => {
    const on = button.dataset.mode === state.draft.priceMode;
    button.classList.toggle('is-on', on);
    button.setAttribute('aria-pressed', String(on));
  });
  $('price-input').setAttribute('aria-label', state.draft.priceMode === 'total' ? 'Сумма за всё' : 'Цена за единицу');
}

function renderAmount() {
  const draft = state.draft;
  applyPrice();
  renderPriceMode();
  const total = L().total(draft);
  $('amount-total').textContent = total === null ? '0' : money.format(total);
  $('amount-formula').textContent = (draft.quantity || '0') + ' ' + draft.unit +
    ' × ' + (draft.price ? money.format(Number(draft.price)) : '0') + ' сум';
  // «За всё»: показываем цену за единицу, а если сумма не делится ровно —
  // честно говорим, какой итог уйдёт в накладную.
  const fit = state.priceFit, derived = $('price-derived');
  derived.hidden = !fit;
  derived.classList.toggle('is-adjusted', !!fit && !fit.exact);
  if (fit) {
    derived.textContent = fit.exact
      ? 'Цена за 1 ' + draft.unit + ': ' + money.format(Number(fit.price)) + ' сум'
      : 'Ровно ' + money.format(fit.entered) + ' сум на ' + draft.quantity + ' ' + draft.unit +
        ' не делится — в накладную уйдёт ' + money.format(fit.total) + ' сум (' +
        money.format(Number(fit.price)) + ' за 1 ' + draft.unit + ')';
  }
  const problem = L().amountProblem(draft);
  const hint = L().priceHint(draft, state.usual);
  const text = problem || (hint.kind === 'empty' && state.usual != null
    ? 'Обычно ' + money.format(Number(state.usual)) + ' сум за ' + draft.unit : hint.text);
  $('price-hint').textContent = text;
  $('price-hint').className = 'shokh-price-hint is-' + (problem ? 'error' : hint.kind);
  $('flow-next').disabled = !L().stepReady('amount', draft);
}

function renderConfirm() {
  const draft = state.draft, total = L().total(draft);
  $('confirm-point').textContent = draft.point || (state.catalog.suppliers.find(s => s.id === draft.supplierId)?.name || '');
  $('confirm-iiko').textContent = (state.catalog.suppliers.find(s => s.id === draft.supplierId)?.name || '') + ' / ' + (state.catalog.storages.find(s => s.id === draft.storageId)?.name || '');
  $('confirm-item').textContent = draft.item;
  $('confirm-formula').textContent = draft.quantity + ' ' + draft.unit + ' × ' +
    money.format(Number(draft.price)) + ' сум';
  $('confirm-total').textContent = money.format(total) + ' сум';
  const after = L().pocketAfter(state.lastPocket, draft);
  $('confirm-after').textContent = after === null ? 'Подотчёт не задан' : money.format(after) + ' сум';
  $('confirm-after').classList.toggle('is-negative', after !== null && after < 0);

  const warnings = [];
  if (state.priceFit && !state.priceFit.exact) warnings.push('Введено ' + money.format(state.priceFit.entered) +
    ' сум, но ровно на ' + draft.quantity + ' ' + draft.unit + ' не делится: в накладную уйдёт ' +
    money.format(state.priceFit.total) + ' сум.');
  if (!draft.hasPhoto) warnings.push('Без фото: бухгалтер отметит покупку как непроверенную.');
  if (L().priceHint(draft, state.usual).kind === 'above') warnings.push('Цена выше обычной — бухгалтер проверит.');
  if (after !== null && after < 0) warnings.push('Записали больше, чем выдано под отчёт.');
  // Каждое предупреждение — своей строкой: так их видно по отдельности и
  // переводчик узнаёт каждое целиком.
  $('confirm-warning').replaceChildren(...warnings.map(text => node('span', '', text)));

  const photo = $('confirm-photo');
  photo.hidden = !state.photoFile;
  if (state.photoFile) photo.src = URL.createObjectURL(state.photoFile);
}

function resetDraft() {
  state.draft = {point: state.draft.point, item: '', unit: state.draft.unit,
    quantity: '', price: '', priceMode: 'unit', priceInput: '', hasPhoto: false, productId: null, unitId: null,
    supplierId: state.draft.supplierId, storageId: state.draft.storageId, operationId: crypto.randomUUID()};
  state.photoFile = null; state.usual = null; state.priceFit = null;
  $('qty-input').value = ''; $('price-input').value = '';
  $('item-search').value = '';
  $('photo-empty').hidden = false; $('photo-filled').hidden = true;
  $('photo-input').value = '';
}

function startTimer() {
  stopTimer();
  state.timer = setInterval(() => {
    const minutes = L().tripElapsedMinutes(state.tripStartedAt, new Date().toISOString());
    $('flow-timer').textContent = '⏱ ' + L().clock(minutes);
  }, 1000);
}
function stopTimer() { if (state.timer) { clearInterval(state.timer); state.timer = null; } }

async function beginTrip() {
  try {
    const data = await api('/trip', {method: 'POST'});
    state.tripId = data.trip_id;
    // Время начала — с сервера: продолженный закуп не начинает отсчёт с нуля.
    state.tripStartedAt = data.started_at || new Date().toISOString();
    startTimer();
    return true;
  } catch (error) { message(error.message, true); return false; }
}

async function submitPurchase() {
  applyPrice();
  if (state.submitting || !ready('point') || !ready('item') || !ready('amount')) return;
  state.submitting = true;
  const draft = state.draft;
  const form = new FormData();
  form.append('point', draft.point); form.append('item', draft.item);
  // Сервер не понимает пробелов-разделителей в числе — отправляем чистое.
  form.append('unit', draft.unit);
  form.append('quantity', String(draft.quantity).replace(/[\s\u00a0\u202f]/g, '').replace(',', '.'));
  form.append('price', draft.price);
  form.append('operation_id', draft.operationId);
  form.append('product_id', draft.productId); form.append('supplier_id', draft.supplierId);
  form.append('storage_id', draft.storageId); form.append('unit_id', draft.unitId);
  form.append('date', state.home.date);
  if (state.tripId !== null) form.append('trip_id', String(state.tripId));
  if (state.photoFile) form.append('photo', state.photoFile);
  $('flow-next').disabled = true;
  try {
    sessionStorage.setItem('shokh-pending-operation', draft.operationId);
    sessionStorage.setItem('shokh-pending-draft', JSON.stringify({draft, date: state.home.date, tripId: state.tripId, tripStartedAt: state.tripStartedAt}));
    const result = await api('/purchase', {method: 'POST', body: form});
    sessionStorage.removeItem('shokh-pending-operation');
    sessionStorage.removeItem('shokh-pending-draft');
    state.expectedPhoto = false;
    const before = state.lastPocket;
    $('done-more').textContent = '+ Ещё товар' + (draft.point ? ' · ' + draft.point : '');
    state.lastPocket = result.pocket;
    const doneName = node('span', '', result.purchase.item);
    doneName.dataset.i18n = 'off';
    $('done-item').replaceChildren(doneName, ' · ' + money.format(Number(result.purchase.total)) + ' сум');
    $('done-pocket').textContent = result.pocket === null ? '—' : money.format(Number(result.pocket)) + ' сум';
    $('done-before').textContent = before === null ? '' : 'было ' + money.format(Number(before));
    const minutes = L().tripElapsedMinutes(state.tripStartedAt, new Date().toISOString());
    $('done-trip').textContent = minutes === null ? '' : 'В закупе ' + L().clock(minutes);
    const synced = result.purchase.iiko?.status === 'synced';
    $('done-title').textContent = synced ? 'Сохранено в iiko' : 'Покупка записана, iiko не подтверждён';
    $('done-iiko').textContent = synced ? 'Приходная накладная № ' + result.purchase.iiko.number
      : (result.purchase.iiko?.error || 'Проверьте статус на главной. Не вводите эту покупку повторно.');
    show('done');
    message('');
  } catch (error) {
    message(error.message + ' Повторите сохранение этой же формы: ключ покупки сохранён.', true);
    $('flow-next').disabled = false;
  }
  finally { state.submitting = false; }
}

async function finishTrip() {
  stopTimer();
  if (state.tripId === null) { await loadHome(); show('home'); return; }
  try {
    const data = await api('/trip/' + state.tripId + '/finish', {method: 'POST'});
    const minutes = data.trip.minutes;
    $('sum-time').textContent = L().clock(minutes);
    $('sum-count').textContent = String(data.purchases.length);
    $('sum-total').textContent = money.format(Number(data.spent));
    renderPurchases($('sum-list'), data.purchases);
    renderHome(await api('/home'));
    state.tripId = null; state.tripStartedAt = null;
    show('summary');
  } catch (error) { message(error.message, true); }
}

/* ── События ───────────────────────────────────────────────────────────── */
$('start-purchase').addEventListener('click', async () => {
  resetDraft();
  state.draft.point = '';
  state.step = 'point';
  if (!state.catalogReady || !state.home) return;
  $('start-purchase').disabled = true;
  const started = await beginTrip();
  $('start-purchase').disabled = false;
  if (!started) return;
  renderSelectors(); renderPoints(); renderStep(); show('flow');
});
$('flow-close').addEventListener('click', async () => { stopTimer(); await loadHome(); show('home'); });
$('flow-back').addEventListener('click', () => { state.step = L().previousStep(state.step); renderStep(); });
$('flow-next').addEventListener('click', () => {
  if (state.step === 'confirm') { submitPurchase(); return; }
  if (!ready(state.step)) return;
  state.step = L().nextStep(state.step); renderStep();
});
$('point-other').addEventListener('input', event => {
  state.draft.point = event.target.value.trim();
  $('flow-next').disabled = !ready('point');
});
$('item-back-point').addEventListener('click', () => { state.step = 'point'; renderPoints(); renderStep(); });
$('item-search').addEventListener('input', renderItems);
$('item-add-custom').addEventListener('click', () => {
  state.draft.item = $('item-search').value.trim();
  $('item-search').value = '';
  chooseItem();
});
$('photo-input').addEventListener('change', event => {
  const file = event.target.files && event.target.files[0];
  state.photoFile = file || null;
  state.draft.hasPhoto = !!file;
  $('photo-empty').hidden = !!file;
  $('photo-filled').hidden = !file;
  if (file) $('photo-preview').src = URL.createObjectURL(file);
  $('flow-next').disabled = !ready(state.step);
});
$('qty-input').addEventListener('input', event => { state.draft.quantity = event.target.value; renderAmount(); });
$('price-input').addEventListener('input', event => { state.draft.priceInput = event.target.value; renderAmount(); });
// Переключатель оставляет введённое число и читает его по-новому, как в
// макете: ошиблись режимом — нажали другой, перепечатывать не нужно.
$('price-mode').addEventListener('click', event => {
  const button = event.target.closest('[data-mode]');
  if (!button || button.dataset.mode === state.draft.priceMode) return;
  state.draft.priceMode = button.dataset.mode;
  renderAmount();
});
$('qty-minus').addEventListener('click', () => {
  const current = L().number(state.draft.quantity) || 0;
  const next = Math.max(0, Math.round((current - 1) * 1000) / 1000);
  state.draft.quantity = next ? String(next) : '';
  $('qty-input').value = state.draft.quantity; renderAmount();
});
$('qty-plus').addEventListener('click', () => {
  const next = (L().number(state.draft.quantity) || 0) + 1;
  state.draft.quantity = String(Math.round(next * 1000) / 1000);
  $('qty-input').value = state.draft.quantity; renderAmount();
});
$('done-more').addEventListener('click', () => {
  resetDraft();
  state.step = 'item';
  renderStep(); show('flow'); startTimer();
});
$('done-finish').addEventListener('click', finishTrip);
$('summary-home').addEventListener('click', async () => { await loadHome(); show('home'); });

function renderSelectors() {
  for (const [id, rows, key] of [['iiko-supplier', state.catalog.suppliers, 'supplierId'],
                                ['iiko-storage', state.catalog.storages, 'storageId']]) {
    const select = $(id); select.replaceChildren();
    const empty = node('option', '', 'Выберите из iiko'); empty.value = ''; select.append(empty);
    // Названия поставщиков и складов — данные iiko, их не переводим.
    rows.forEach(row => { const option = node('option', '', row.name); option.value = row.id; option.dataset.i18n = 'off'; select.append(option); });
    select.value = state.draft[key] || '';
  }
}
$('iiko-supplier').addEventListener('change', event => {
  state.draft.supplierId = event.target.value;
  if (!$('point-other').value.trim()) state.draft.point = (state.catalog.suppliers.find(s => s.id === event.target.value)?.name || '').slice(0, 80);
  $('flow-next').disabled = !ready('point');
});
$('iiko-storage').addEventListener('change', event => {
  state.draft.storageId = event.target.value;
  $('flow-next').disabled = !ready('point');
});
async function loadCatalog() {
  state.catalogReady = false;
  $('start-purchase').disabled = true;
  $('iiko-status').textContent = 'Загружаем справочники iiko…';
  try {
    const catalog = await api('/catalog');
    state.catalog = catalog;
    state.catalogReady = catalog.source === 'iiko' && catalog.can_create;
    $('iiko-status').textContent = state.catalogReady
      ? 'Подключено · товаров: ' + catalog.items.length + ' · складов: ' + catalog.storages.length
      : 'Сохранение в iiko недоступно. Проверьте подключение и права на приходные накладные.';
    $('start-purchase').disabled = !state.catalogReady || !state.home || !!sessionStorage.getItem('shokh-pending-operation');
  } catch (error) { $('iiko-status').textContent = error.message; }
}
async function recoverPending() {
  const key = sessionStorage.getItem('shokh-pending-operation');
  if (!key) return;
  try {
    const result = await api('/operation/' + encodeURIComponent(key));
    if (result.purchase) {
      sessionStorage.removeItem('shokh-pending-operation');
      sessionStorage.removeItem('shokh-pending-draft');
      message('Предыдущая покупка найдена в журнале. Проверьте её статус iiko.');
    } else {
      state.recovery = JSON.parse(sessionStorage.getItem('shokh-pending-draft') || 'null');
      message('Ответ предыдущей отправки не получен. Восстановлена та же покупка с защитой от дубля.', true);
    }
  } catch (error) { message('Проверяем предыдущую покупку. ' + error.message, true); }
}
async function reloadData() {
  $('reload-data').disabled = true;
  message('');
  await recoverPending();
  await Promise.allSettled([loadHome(), loadCatalog()]);
  if (state.recovery && state.catalogReady && state.home) {
    const saved = state.recovery; state.recovery = null;
    state.draft = saved.draft; state.home.date = saved.date;
    state.tripId = saved.tripId; state.tripStartedAt = saved.tripStartedAt;
    state.expectedPhoto = !!saved.draft.hasPhoto;
    state.draft.hasPhoto = false; state.photoFile = null;
    state.step = 'item';
    $('qty-input').value = state.draft.quantity;
    // Черновик до переключателя «за всё» хранил только цену за единицу.
    if (!state.draft.priceMode) state.draft.priceMode = 'unit';
    if (state.draft.priceInput == null) state.draft.priceInput = state.draft.price || '';
    $('price-input').value = state.draft.priceInput;
    renderSelectors(); renderPoints(); renderStep(); show('flow'); startTimer();
    if (state.expectedPhoto) message('Прикрепите прежнее фото и повторите сохранение этой покупки.', true);
  }
  $('reload-data').disabled = false;
}
$('reload-data').addEventListener('click', reloadData);
reloadData();

// «‹ Панель» — только тем, кому открыт ещё какой-то модуль. У самого Шоха
// других модулей нет, и ссылка вела бы его по кругу обратно в закуп.
fetch('/api/config', {headers: {accept: 'application/json'}})
  .then(response => (response.ok ? response.json() : null))
  .then(config => {
    // Сервер отдаёт только открытые этой учётной записи модули.
    const others = config && Array.isArray(config.modules)
      && config.modules.some(module => module.path !== '/shokh');
    if (others) document.getElementById('shokh-back').hidden = false;
  })
  .catch(() => {});
