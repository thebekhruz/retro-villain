const $ = id => document.getElementById(id);
const money = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
const L = () => globalThis.ShokhLogic;

// Черновик покупки живёт только до отправки: сервер — единственный источник
// истории, поэтому ничего не кешируем между закупами.
let state = {
  screen: 'home', step: 'point', tripId: null, tripStartedAt: null,
  draft: {point: '', item: '', unit: 'кг', quantity: '', price: '', hasPhoto: false},
  photoFile: null, usual: null, catalog: {points: [], units: ['кг'], items: []},
  home: null, timer: null, lastPocket: null
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
  const sender = options.method === 'POST' ? RetroFinancialWrite : fetch;
  const response = await sender('/api/shokh' + path, {cache: 'no-store', ...options});
  let payload = null;
  try { payload = await response.json(); } catch {}
  if (!response.ok) {
    throw new Error(typeof payload?.detail === 'string' ? payload.detail : 'Не получилось. Попробуйте ещё раз.');
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

  const share = L().reportedShare(data.pocket, data.pending);
  $('home-reported').textContent = share === null
    ? 'Подотчёт не заведён'
    : 'Отчитались за ' + share + '% выданных денег';
  $('home-reported-bar').style.width = (share || 0) + '%';

  $('home-pending-card').hidden = Number(data.pending) <= 0;
  $('home-pending-note').textContent = 'На ' + money.format(Number(data.pending)) +
    ' сум — бухгалтер ещё не принял накладные. Эти деньги уже не на руках.';

  $('home-spent').textContent = Number(data.spent_today) > 0
    ? money.format(Number(data.spent_today)) + ' сум' : '';
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
    title.append(node('span', '', row.item));
    if (row.price_above_usual) title.append(node('span', 'shokh-flag', 'дороже обычного'));
    if (row.accepted_at) title.append(node('span', 'shokh-ok', 'принято'));
    body.append(title, node('div', 'shokh-note',
      row.point + ' · ' + row.quantity + ' ' + row.unit + ' × ' + money.format(Number(row.price))));
    item.append(body, node('strong', 'rm-num', money.format(Number(row.total))));
    container.append(item);
  });
}

async function loadHome() {
  try {
    const data = await api('/home');
    renderHome(data);
    message('');
  } catch (error) { message(error.message, true); }
}

/* ── Флоу ──────────────────────────────────────────────────────────────── */
const STEP_NAMES = {point: 'Точка', item: 'Товар', amount: 'Количество и цена', confirm: 'Проверка'};

function renderSegments() {
  const box = $('flow-segments'); box.replaceChildren();
  const index = L().STEPS.indexOf(state.step);
  L().STEPS.forEach((_, position) => {
    box.append(node('div', 'shokh-segment' + (position <= index ? ' is-on' : '')));
  });
}

function renderStep() {
  const step = state.step, draft = state.draft;
  for (const name of L().STEPS) $('step-' + name).hidden = name !== step;
  $('flow-step-no').textContent = String(L().STEPS.indexOf(step) + 1);
  $('flow-step-name').textContent = STEP_NAMES[step];
  renderSegments();
  $('flow-back').disabled = step === 'point';
  $('flow-next').disabled = !L().stepReady(step, draft);
  $('flow-next').textContent = step === 'confirm' ? 'Записать покупку' : 'Далее';

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

function renderPoints() {
  const box = $('point-list'); box.replaceChildren();
  state.catalog.points.forEach(point => {
    const button = node('button', 'shokh-point' + (state.draft.point === point ? ' is-active' : ''));
    button.type = 'button';
    button.append(node('span', 'shokh-point-mark', point.slice(0, 2).toUpperCase()),
      node('span', 'shokh-point-name', point));
    button.addEventListener('click', () => {
      state.draft.point = point;
      $('point-other').value = '';
      // Точку выбрали — сразу к товару, как в макете.
      state.step = 'item'; renderStep();
    });
    box.append(button);
  });
}

function renderItems() {
  const query = $('item-search').value.trim().toLowerCase();
  const box = $('item-list'); box.replaceChildren();
  const matches = state.catalog.items.filter(row => !query || row.item.toLowerCase().includes(query));
  $('item-list-title').textContent = query ? 'Найдено' : 'Частые товары';
  matches.slice(0, 10).forEach(row => {
    const button = node('button', 'shokh-item' + (state.draft.item === row.item ? ' is-active' : ''));
    button.type = 'button';
    button.append(node('span', 'shokh-item-name', row.item),
      node('small', '', 'брали ' + row.times + ' раз · ' + row.unit));
    button.addEventListener('click', () => {
      state.draft.item = row.item;
      state.draft.unit = state.catalog.units.includes(row.unit) ? row.unit : state.draft.unit;
      $('item-search').value = '';
      chooseItem();
    });
    box.append(button);
  });
  if (!matches.length && !query) box.append(node('p', 'shokh-note', 'Истории пока нет — впишите товар вручную.'));
  // Своего товара в списке нет — предлагаем добавить введённое как есть.
  const custom = $('item-search').value.trim();
  const exact = state.catalog.items.some(row => row.item.toLowerCase() === custom.toLowerCase());
  $('item-add-custom').hidden = !custom || exact;
  $('item-add-custom').textContent = custom ? 'Добавить «' + custom + '»' : '';
  $('item-chosen').hidden = !state.draft.item;
  $('item-chosen').textContent = 'Выбрано: ' + state.draft.item;
}

async function chooseItem() {
  renderItems();
  $('flow-next').disabled = !L().stepReady('item', state.draft);
  // Обычную цену спрашиваем у сервера: она считается по истории этого товара.
  state.usual = null;
  try {
    const found = state.catalog.items.find(row => row.item === state.draft.item);
    state.usual = found && found.usual_price !== undefined ? found.usual_price : null;
  } catch { state.usual = null; }
}

function renderUnits() {
  const box = $('unit-switch'); box.replaceChildren();
  state.catalog.units.forEach(unit => {
    const button = node('button', 'shokh-switch-btn' + (state.draft.unit === unit ? ' is-on' : ''), unit);
    button.type = 'button';
    button.addEventListener('click', () => { state.draft.unit = unit; renderUnits(); renderAmount(); });
    box.append(button);
  });
  const quick = $('qty-quick'); quick.replaceChildren();
  [1, 5, 10].forEach(value => {
    const button = node('button', 'shokh-quick-btn', String(value));
    button.type = 'button';
    button.addEventListener('click', () => { state.draft.quantity = String(value); $('qty-input').value = String(value); renderAmount(); });
    quick.append(button);
  });
}

function renderAmount() {
  const draft = state.draft;
  const total = L().total(draft);
  $('amount-total').textContent = total === null ? '0' : money.format(total);
  $('amount-formula').textContent = (draft.quantity || '0') + ' ' + draft.unit +
    ' × ' + (draft.price ? money.format(Number(draft.price)) : '0') + ' сум';
  $('usual-price').textContent = state.usual == null ? '' : 'обычно ' + money.format(Number(state.usual));
  const hint = L().priceHint(draft, state.usual);
  $('price-hint').textContent = hint.text;
  $('price-hint').className = 'shokh-price-hint is-' + hint.kind;
  $('flow-next').disabled = !L().stepReady('amount', draft);
}

function renderConfirm() {
  const draft = state.draft, total = L().total(draft);
  $('confirm-point').textContent = draft.point;
  $('confirm-item').textContent = draft.item;
  $('confirm-formula').textContent = draft.quantity + ' ' + draft.unit + ' × ' +
    money.format(Number(draft.price)) + ' сум';
  $('confirm-total').textContent = money.format(total) + ' сум';
  const after = L().pocketAfter(state.lastPocket, draft);
  $('confirm-after').textContent = after === null ? 'Подотчёт не задан' : money.format(after) + ' сум';
  $('confirm-after').classList.toggle('is-negative', after !== null && after < 0);

  const warnings = [];
  if (!draft.hasPhoto) warnings.push('Без фото: бухгалтер отметит покупку как непроверенную.');
  if (L().priceHint(draft, state.usual).kind === 'above') warnings.push('Цена выше обычной — бухгалтер проверит.');
  if (after !== null && after < 0) warnings.push('Записали больше, чем выдано под отчёт.');
  $('confirm-warning').textContent = warnings.join(' ');

  const photo = $('confirm-photo');
  photo.hidden = !state.photoFile;
  if (state.photoFile) photo.src = URL.createObjectURL(state.photoFile);
}

function resetDraft() {
  state.draft = {point: state.draft.point, item: '', unit: state.draft.unit,
    quantity: '', price: '', hasPhoto: false};
  state.photoFile = null; state.usual = null;
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
  } catch (error) { message(error.message, true); }
}

async function submitPurchase() {
  const draft = state.draft;
  const form = new FormData();
  form.append('point', draft.point); form.append('item', draft.item);
  form.append('unit', draft.unit); form.append('quantity', draft.quantity);
  form.append('price', draft.price);
  if (state.tripId !== null) form.append('trip_id', String(state.tripId));
  if (state.photoFile) form.append('photo', state.photoFile);
  $('flow-next').disabled = true;
  try {
    const result = await api('/purchase', {method: 'POST', body: form});
    const before = state.lastPocket;
    state.lastPocket = result.pocket;
    $('done-item').textContent = result.purchase.item + ' · ' + money.format(Number(result.purchase.total)) + ' сум';
    $('done-pocket').textContent = result.pocket === null ? '—' : money.format(Number(result.pocket)) + ' сум';
    $('done-before').textContent = before === null ? '' : 'было ' + money.format(Number(before));
    const minutes = L().tripElapsedMinutes(state.tripStartedAt, new Date().toISOString());
    $('done-trip').textContent = minutes === null ? '' : 'В закупе ' + L().clock(minutes);
    show('done');
    message('');
  } catch (error) { message(error.message, true); $('flow-next').disabled = false; }
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
  await beginTrip();
  renderPoints(); renderStep(); show('flow');
});
$('flow-close').addEventListener('click', async () => { stopTimer(); await loadHome(); show('home'); });
$('flow-back').addEventListener('click', () => { state.step = L().previousStep(state.step); renderStep(); });
$('flow-next').addEventListener('click', () => {
  if (state.step === 'confirm') { submitPurchase(); return; }
  if (!L().stepReady(state.step, state.draft)) return;
  state.step = L().nextStep(state.step); renderStep();
});
$('point-other').addEventListener('input', event => {
  state.draft.point = event.target.value.trim();
  $('flow-next').disabled = !L().stepReady('point', state.draft);
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
});
$('qty-input').addEventListener('input', event => { state.draft.quantity = event.target.value; renderAmount(); });
$('price-input').addEventListener('input', event => { state.draft.price = event.target.value; renderAmount(); });
$('qty-minus').addEventListener('click', () => {
  const current = L().number(state.draft.quantity) || 0;
  const next = Math.max(0, Math.round((current - 1) * 100) / 100);
  state.draft.quantity = next ? String(next) : '';
  $('qty-input').value = state.draft.quantity; renderAmount();
});
$('qty-plus').addEventListener('click', () => {
  const next = (L().number(state.draft.quantity) || 0) + 1;
  state.draft.quantity = String(Math.round(next * 100) / 100);
  $('qty-input').value = state.draft.quantity; renderAmount();
});
$('done-more').addEventListener('click', () => {
  resetDraft();
  state.step = 'item';
  renderStep(); show('flow'); startTimer();
});
$('done-finish').addEventListener('click', finishTrip);
$('summary-home').addEventListener('click', async () => { await loadHome(); show('home'); });

(async () => {
  try {
    state.catalog = await api('/catalog');
    if (!state.catalog.units.length) state.catalog.units = ['кг'];
    state.draft.unit = state.catalog.units[0];
    await loadHome();
    show('home');
  } catch (error) { message(error.message, true); }
})();

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
