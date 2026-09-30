const $ = id => document.getElementById(id);
// Наличные — целые сумы: тийинов в кармане нет (сервер считает их через
// shokh.store.cash_amount). С тийинами — только цена за единицу и итог
// накладной iiko.
const money = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 0});
const exact = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
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

function message(text, error = false, {toast = true} = {}) {
  const box = $('shokh-message');
  box.textContent = text; box.hidden = !text;
  box.classList.toggle('is-error', error);
  box.setAttribute('role', error ? 'alert' : 'status');
  if (toast) globalThis.RetroToast?.show(text, error ? 'error' : 'ok');
}

/* Отклик и ожидание (T-393, docs/feedback-principles.md): каждое нажатие
   отвечает, ожидание видно на том, что его начало. busy.js подключён первым;
   без него действия просто выполняются. */
const passthrough = (el, work) => Promise.resolve(typeof work === 'function' ? work() : work);
const Busy = globalThis.RetroBusy || {button: passthrough, row: passthrough, section: passthrough,
  track: passthrough, flash() {}, silent: fn => fn()};
/** Перезапустить короткую анимацию-отклик (класс снимается сам). */
function pulse(element, cls) {
  if (!element || !element.classList) return;
  element.classList.remove(cls);
  void element.offsetWidth;
  element.classList.add(cls);
  element.addEventListener('animationend', () => element.classList.remove(cls), {once: true});
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
  // Скелеты из разметки заменены данными — раздел больше не «загружается».
  $('home-balance').removeAttribute('aria-busy');
  $('home-purchases').removeAttribute('aria-busy');
  state.lastPocket = data.pocket;
  $('home-date').textContent = 'Закуп · ' + new Intl.DateTimeFormat('ru-RU',
    {weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC'})
    .format(new Date(data.date + 'T12:00:00Z'));
  // Подотчёт ещё не заведён — цифры нет, и придумывать её нельзя.
  const unknown = data.pocket === null;
  $('home-pocket').textContent = unknown ? 'Не задан' : money.format(Number(data.pocket));
  $('home-pocket-note').textContent = unknown ? 'бухгалтер ещё не выдал подотчёт' : 'сум';
  // Строка под суммой, как в макете: откуда деньги и сколько уже ушло.
  // Под суммой, как в макете и §3.6: сколько выдали сегодня (кассир и
  // бухгалтер) и сколько потрачено. Числа и доля — с сервера: формула одна.
  $('home-pocket-sub').textContent = 'Выдано сегодня ' + money.format(Number(data.given_today || 0)) +
    ' · потрачено ' + money.format(Number(data.spent_day ?? data.spent_today ?? 0));

  const share = data.reported_percent ?? null;
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

/* Главная не загрузилась с первого раза: скелет не должен крутиться вечно. */
function homeFailed() {
  $('home-balance').removeAttribute('aria-busy');
  $('home-pocket').textContent = '—';
  $('home-pocket-sub').textContent = '';
  $('home-reported').textContent = 'Нет данных';
  $('home-purchases-title').textContent = 'Сегодня';
  $('home-purchases').removeAttribute('aria-busy');
  $('home-purchases').replaceChildren(node('p', 'shokh-note', 'Не удалось загрузить покупки. Нажмите «Обновить данные».'));
}

function letterThumb(row) {
  return node('span', 'shokh-thumb is-empty', row.item.slice(0, 1).toUpperCase());
}

/* Проверка / повтор отправки одной покупки в iiko: кнопка крутится, строка
   «в работе», итог — ✓ и зелёная вспышка или текст ошибки. Ключи
   data-busy-key переносят состояние на строку после перерисовки списка. */
function checkIiko(row, button, item) {
  const retry = ['rejected', 'pending'].includes(row.iiko.status);
  const work = (async () => {
    try {
      const result = await api('/purchase/' + row.id + (retry ? '/retry' : '/sync'),
        retry ? {method: 'POST'} : {});
      await loadHome();
      const synced = result.purchase.iiko.status === 'synced';
      if (synced) globalThis.RetroToast?.show('iiko подтвердил накладную.', 'ok');
      else message(result.purchase.iiko.error || 'iiko ещё не подтвердил накладную.', true);
      return synced;
    } catch (error) { message(error.message, true); return false; }
  })();
  return Busy.button(button, Busy.row(item, work));
}

function renderPurchases(container, rows) {
  container.replaceChildren();
  if (!rows.length) {
    container.append(node('p', 'shokh-note', 'Покупок пока нет. Начните закуп кнопкой ниже.'));
    return;
  }
  [...rows].reverse().forEach(row => {
    const item = node('div', 'shokh-purchase');
    item.dataset.busyKey = 'shokh:buy:' + row.id;
    if (row.has_photo) {
      // Пока фото грузится — мерцающая плашка; не открылось — буква, а не
      // значок битой картинки.
      const image = document.createElement('img');
      image.className = 'shokh-thumb is-loading'; image.alt = '';
      image.loading = 'lazy'; image.decoding = 'async';
      image.addEventListener('load', () => image.classList.remove('is-loading'), {once: true});
      image.addEventListener('error', () => image.replaceWith(letterThumb(row)), {once: true});
      image.src = '/api/shokh/photo/' + row.id;
      item.append(image);
    } else {
      item.append(letterThumb(row));
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
    if (row.iiko && row.iiko.status === 'manual') {
      body.append(node('small', 'shokh-warning', 'Нет в iiko · накладную проведёт бухгалтер'));
    } else if (row.iiko && row.iiko.status !== 'legacy') {
      const synced = row.iiko.status === 'synced';
      body.append(node('small', synced ? 'shokh-ok' : 'shokh-warning', synced
        ? 'iiko · накладная № ' + (row.iiko.number || '—')
        : row.iiko.status === 'rejected' ? 'iiko отклонил: ' + row.iiko.error
        : 'Ожидает подтверждения iiko. Не вводите покупку повторно.'));
      if (!synced) {
        const retry = ['rejected', 'pending'].includes(row.iiko.status);
        const check = node('button', 'shokh-secondary', retry ? 'Повторить отправку' : 'Проверить iiko');
        check.type = 'button';
        check.dataset.busyKey = 'shokh:iiko:' + row.id;
        check.addEventListener('click', () => checkIiko(row, check, item));
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
    updateStart();
    return true;
  } catch (error) {
    if (!state.home) homeFailed();
    message(error.message, true);
    return false;
  }
}
/* Первая загрузка — скелет из разметки; повторная — прежние данные на месте,
   но гаснут, пока идут новые. */
function refreshHome() {
  if (!state.home) return loadHome();
  return Busy.section($('home-balance'), Busy.section($('home-purchases-card'), loadHome()));
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
  if (step === 'point') return !!(state.draft.point || '').trim() && !!state.draft.supplierId && !!state.draft.storageId;
  if (step === 'item') return (state.draft.custom ? !!(state.draft.item || '').trim() && !!state.draft.unit
    : !!state.draft.productId) && (!state.expectedPhoto || !!state.photoFile);
  return L().stepReady(step, state.draft);
}

/* Чего не хватает, чтобы пройти шаг: текст подсказки и поле, которое
   подсветить. Неготовое «Далее» не молчит — оно говорит это. */
function missing(step) {
  const draft = state.draft;
  if (step === 'point') {
    if (!(draft.point || '').trim()) return ['Выберите точку', 'point-list'];
    if (!draft.supplierId && !draft.storageId) return ['Выберите поставщика и склад iiko', 'iiko-supplier'];
    if (!draft.supplierId) return ['Выберите поставщика iiko', 'iiko-supplier'];
    if (!draft.storageId) return ['Выберите склад поступления', 'iiko-storage'];
  }
  if (step === 'item') {
    if (draft.custom && !(draft.item || '').trim()) return ['Введите название нового товара', 'custom-name'];
    if (!draft.custom && !draft.productId) return ['Выберите товар из списка iiko', 'item-search'];
    if (state.expectedPhoto && !state.photoFile) return ['Прикрепите прежнее фото покупки', 'photo-empty'];
  }
  if (step === 'amount' || step === 'confirm') {
    const problem = L().amountProblem(draft);
    if (problem) return [problem, 'price-input'];
    if (!L().number(draft.quantity)) return ['Укажите количество.', 'qty-input'];
    if (!L().number(draft.price)) return ['Укажите цену.', 'price-input'];
  }
  return null;
}

/* «Далее» всегда нажимается: готово — ведёт дальше, нет — объясняет. */
function setNext(ok) {
  const next = $('flow-next');
  next.disabled = false;
  next.setAttribute('aria-disabled', String(!ok));
  if (ok) hideHint();
}

let hintTimer = null;
function showStatus(kind) {
  $('flow-status').hidden = !kind;
  $('flow-hint').hidden = kind !== 'hint';
  $('save-status').hidden = kind !== 'save';
}
function hideHint() {
  clearTimeout(hintTimer);
  if (!$('flow-hint').hidden) showStatus(null);
}
function explain(step) {
  const found = missing(step);
  if (!found) return;
  const [text, id] = found;
  $('flow-hint').textContent = text;
  showStatus('hint');
  pulse($('flow-hint'), 'is-in');
  pulse($('flow-next'), 'shokh-nudge');
  const target = $(id);
  if (target && !target.closest('[hidden]')) {
    target.scrollIntoView({block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
    pulse(target, 'shokh-need');
    if (target.tagName === 'INPUT') target.focus({preventScroll: true});
  }
  clearTimeout(hintTimer);
  hintTimer = setTimeout(hideHint, 5000);
}

function renderStep() {
  const step = state.step, draft = state.draft;
  for (const name of L().STEPS) $('step-' + name).hidden = name !== step;
  // Смена шага видна: новый шаг въезжает с той стороны, куда шли.
  if (state.shownStep !== step) {
    const back = state.shownStep && L().STEPS.indexOf(step) < L().STEPS.indexOf(state.shownStep);
    const panel = $('step-' + step);
    panel.classList.toggle('is-back', !!back);
    if (state.shownStep) pulse(panel, 'is-entering');
    state.shownStep = step;
    hideHint();
  }
  $('flow-step-no').textContent = String(L().STEPS.indexOf(step) + 1);
  $('flow-step-name').textContent = STEP_NAMES[step];
  renderSegments();
  setNext(ready(step));
  $('flow-next').textContent = step !== 'confirm' ? 'Далее'
    : state.saveFailed ? 'Повторить сохранение' : state.draft.custom ? 'Сохранить' : 'Сохранить в iiko';
  if (step !== 'confirm' || !state.saveFailed) { if (!$('save-status').hidden) showStatus(null); }

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
  persistDraft();
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
      applyPointDefaults(point);
      renderPoints(); renderSelectors();
      setNext(ready('point'));
      // Точку выбрали — сразу к товару, как в макете. Поставщик и склад —
      // с прошлой накладной этой точки; если точка новая, подсказываем,
      // что выбрать для накладной iiko.
      if (ready('point')) { state.step = L().nextStep('point'); renderStep(); } else explain('point');
    });
    box.append(button);
  });
}

/* Поставщик и склад прошлой накладной этой точки — если они ещё есть в
   справочнике iiko. */
function applyPointDefaults(point) {
  const known = (state.catalog.point_defaults || {})[point];
  // Точка новая — поставщика выбирают для неё, а не берут с прошлой точки:
  // иначе накладная тихо ушла бы чужому поставщику. Склад обычно тот же.
  if (!known) { state.draft.supplierId = ''; return; }
  state.draft.supplierId = state.catalog.suppliers.some(row => row.id === known.supplier_id) ? known.supplier_id : '';
  if (state.catalog.storages.some(row => row.id === known.storage_id)) state.draft.storageId = known.storage_id;
}

function renderItems() {
  const custom = !!state.draft.custom;
  // «+ Новый товар»: вместо списка iiko — название и единица.
  for (const id of ['item-search', 'item-list-title', 'item-list', 'item-add-custom']) $(id).hidden = custom;
  $('item-custom').hidden = !custom;
  $('item-chosen').hidden = custom || !state.draft.item;
  if (custom) { renderCustom(); return; }
  const query = $('item-search').value.trim().toLowerCase();
  const box = $('item-list'); box.replaceChildren();
  const matches = L().searchItems(state.catalog.items, query, 10, state.draft.point);
  // Без запроса — сначала то, что Шох уже покупал (как «Часто покупаете» в макете).
  $('item-list-title').textContent = query ? 'Найдено в iiko'
    : matches.some(row => Number(row.times) > 0) ? 'Часто покупаете' : 'Товары iiko';
  matches.forEach(row => {
    const button = node('button', 'shokh-item' + (state.draft.item === row.item ? ' is-active' : '') + (row.custom ? ' is-custom' : ''));
    button.type = 'button';
    // Как в макете: под названием — обычная цена, если она уже есть в истории.
    // Обычная цена — ориентир в целых сумах: «обычно 33 333 / шт».
    const itemName = node('span', 'shokh-item-name', row.item);
    itemName.dataset.i18n = 'off';
    button.append(itemName,
      node('small', '', row.usual_price != null
        ? 'обычно ' + money.format(Number(row.usual_price)) + ' / ' + row.unit
        : row.custom ? 'нет в iiko · ' + row.unit : 'Арт. ' + row.code + ' · ' + row.unit));
    button.addEventListener('click', () => {
      // Товар не из справочника, который уже брали: тот же «Новый товар».
      if (row.custom) { startCustom(row.item, row.unit); return; }
      state.draft.custom = false;
      state.draft.item = row.item;
      state.draft.productId = row.id;
      state.draft.unitId = row.unit_id;
      state.draft.unit = state.catalog.units.includes(row.unit) ? row.unit : state.draft.unit;
      $('item-search').value = '';
      chooseItem();
    });
    box.append(button);
  });
  if (!matches.length) box.append(node('p', 'shokh-note', 'Товар не найден в iiko. Нажмите «+ Новый товар» ниже.'));
  // Товара нет в списке — «+ Новый товар» с тем, что уже набрали в поиске.
  const typed = $('item-search').value.trim();
  const label = $('item-add-custom');
  label.replaceChildren('+ Новый товар');
  if (typed) { const name = node('span', '', ' «' + typed + '»'); name.dataset.i18n = 'off'; label.append(name); }
  const chosen = node('span', '', state.draft.item);
  chosen.dataset.i18n = 'off';
  $('item-chosen').replaceChildren('Выбрано: ', chosen);
}

async function chooseItem() {
  renderItems();
  pulse($('item-chosen'), 'is-in');
  setNext(ready('item'));
  // Обычную цену спрашиваем у сервера: она считается по истории этого товара.
  state.usual = null;
  state.usual = usualFor(state.draft);
  persistDraft();
}

/* Обычная цена по истории: товар iiko — по id, новый товар — по названию. */
function usualFor(draft) {
  const fold = value => String(value || '').trim().toLowerCase().replace(/ё/g, 'е');
  const found = draft.custom
    ? state.catalog.items.find(row => row.custom && fold(row.item) === fold(draft.item))
    : state.catalog.items.find(row => row.id === draft.productId);
  return found && found.usual_price != null ? found.usual_price : null;
}

/* ── «+ Новый товар» ───────────────────────────────────────────────────── */
function customUnits() {
  const units = state.catalog.custom_units;
  return Array.isArray(units) && units.length ? units : ['кг', 'шт', 'л', 'уп', 'пучок'];
}
function startCustom(name, unit) {
  const draft = state.draft;
  draft.custom = true; draft.productId = null; draft.unitId = null;
  draft.item = String(name || '').trim().slice(0, 120);
  draft.unit = customUnits().includes(unit) ? unit : (customUnits().includes(draft.unit) ? draft.unit : customUnits()[0]);
  $('custom-name').value = draft.item;
  $('item-search').value = '';
  state.usual = usualFor(draft);
  renderItems();
  setNext(ready('item'));
  pulse($('item-custom'), 'is-in');
  if (!draft.item) $('custom-name').focus({preventScroll: true});
  persistDraft();
}
function renderCustom() {
  const draft = state.draft;
  if (document.activeElement !== $('custom-name') && $('custom-name').value !== draft.item) $('custom-name').value = draft.item || '';
  const box = $('custom-units'); box.replaceChildren();
  customUnits().forEach(unit => {
    const on = draft.unit === unit;
    const button = node('button', 'shokh-switch-btn' + (on ? ' is-on' : ''), unit);
    button.type = 'button';
    button.setAttribute('aria-pressed', String(on));
    button.addEventListener('click', () => { draft.unit = unit; renderCustom(); setNext(ready('item')); persistDraft(); });
    box.append(button);
  });
  // Такой товар в iiko уже есть — лучше выбрать его: тогда накладная уйдёт сама.
  const fold = value => String(value || '').trim().toLowerCase().replace(/ё/g, 'е');
  const twin = draft.item && state.catalog.items.find(row => !row.custom && fold(row.item) === fold(draft.item));
  $('custom-known').hidden = !twin;
  if (twin) {
    const name = node('b', '', twin.item); name.dataset.i18n = 'off';
    $('custom-known').replaceChildren('В iiko уже есть ', name, ' — лучше выберите его из списка.');
  }
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
  // «Итого» — наличные в целых сумах; итог накладной с тийинами — строкой ниже.
  const cash = L().cashTotal(draft), invoice = L().total(draft);
  $('amount-total').textContent = cash === null ? '0' : money.format(cash);
  $('amount-formula').textContent = quantityLabel(draft.quantity) + ' ' + draft.unit +
    ' × ' + (draft.price ? exact.format(Number(draft.price)) : '0') + ' сум';
  // «За всё»: показываем цену за единицу, а если сумма не делится ровно —
  // честно говорим, какой итог уйдёт в накладную.
  const fit = state.priceFit, derived = $('price-derived');
  // Итог с тийинами при цене «за 1»: наличными — целые сумы, а в накладную
  // уходит точный итог. Говорим об этом, чтобы цифры не спорили.
  const tiyins = !fit && invoice !== null && cash !== invoice;
  derived.hidden = !fit && !tiyins;
  derived.classList.toggle('is-adjusted', (!!fit && !fit.exact) || tiyins);
  if (fit) {
    derived.textContent = fit.exact
      ? 'Цена за 1 ' + draft.unit + ': ' + exact.format(Number(fit.price)) + ' сум'
      : 'Ровно ' + exact.format(fit.entered) + ' сум на ' + quantityLabel(draft.quantity) + ' ' + draft.unit +
        ' не делится — в накладную уйдёт ' + exact.format(fit.total) + ' сум (' +
        exact.format(Number(fit.price)) + ' за 1 ' + draft.unit + ')';
  } else if (tiyins) {
    derived.textContent = 'В накладную уйдёт ' + exact.format(invoice) + ' сум, наличными — ' + money.format(cash) + ' сум';
  }
  const problem = L().amountProblem(draft);
  const hint = L().priceHint(draft, state.usual);
  const text = problem || (hint.kind === 'empty' && state.usual != null
    ? 'Обычно ' + money.format(Number(state.usual)) + ' сум за ' + draft.unit : hint.text);
  $('price-hint').textContent = text;
  $('price-hint').className = 'shokh-price-hint is-' + (problem ? 'error' : hint.kind);
  setNext(L().stepReady('amount', draft));
  persistDraft();
}

/* Количество в подписях — как на главной: «2,125», а не «2.125». Непонятное
   число оставляем как ввели: подсказка под ценой объяснит, что не так. */
function quantityLabel(value) {
  const text = String(value == null ? '' : value).replace(/[\s\u00a0\u202f]/g, '');
  const parsed = L().number(text);
  // Лишние знаки после запятой не округляем в подписи: иначе «1,2345» выглядело
  // бы как принятое «1,235», а сохранить его нельзя.
  const exact = /^\d+([.,]\d{0,3})?$/.test(text);
  return parsed === null || !exact ? (String(value || '').trim() || '0') : quantityText.format(parsed);
}

function renderConfirm() {
  const draft = state.draft, total = L().cashTotal(draft);
  $('confirm-point').textContent = draft.point || (state.catalog.suppliers.find(s => s.id === draft.supplierId)?.name || '');
  $('confirm-iiko').textContent = (state.catalog.suppliers.find(s => s.id === draft.supplierId)?.name || '') + ' / ' + (state.catalog.storages.find(s => s.id === draft.storageId)?.name || '');
  $('confirm-item').textContent = draft.item;
  $('confirm-formula').textContent = quantityLabel(draft.quantity) + ' ' + draft.unit + ' × ' +
    exact.format(Number(draft.price)) + ' сум';
  $('confirm-total').textContent = money.format(total) + ' сум';
  const after = L().pocketAfter(state.lastPocket, draft);
  $('confirm-after').textContent = after === null ? 'Подотчёт не задан' : money.format(after) + ' сум';
  $('confirm-after').classList.toggle('is-negative', after !== null && after < 0);

  const warnings = [];
  if (draft.custom) warnings.push('Товара нет в справочнике iiko — накладную проведёт бухгалтер.');
  if (state.priceFit && !state.priceFit.exact) warnings.push('Введено ' + exact.format(state.priceFit.entered) +
    ' сум, но ровно на ' + quantityLabel(draft.quantity) + ' ' + draft.unit + ' не делится: в накладную уйдёт ' +
    exact.format(state.priceFit.total) + ' сум.');
  if (!draft.hasPhoto) warnings.push('Без фото: бухгалтер отметит покупку как непроверенную.');
  if (L().priceHint(draft, state.usual).kind === 'above') warnings.push('Цена выше обычной больше чем на 10% — бухгалтер увидит.');
  if (after !== null && after < 0) warnings.push('Записали больше, чем выдано под отчёт.');
  // Каждое предупреждение — своей строкой: так их видно по отдельности и
  // переводчик узнаёт каждое целиком.
  $('confirm-warning').replaceChildren(...warnings.map(text => node('span', '', text)));

  const photo = $('confirm-photo');
  $('confirm-photo-box').hidden = !state.photoFile;
  if (state.photoFile) photo.src = URL.createObjectURL(state.photoFile);
}

function resetDraft() {
  state.draft = {point: state.draft.point, item: '', unit: state.draft.unit,
    quantity: '', price: '', priceMode: 'unit', priceInput: '', hasPhoto: false, productId: null, unitId: null,
    custom: false, supplierId: state.draft.supplierId, storageId: state.draft.storageId, operationId: crypto.randomUUID()};
  state.photoFile = null; state.usual = null; state.priceFit = null; state.saveFailed = false;
  $('qty-input').value = ''; $('price-input').value = '';
  $('item-search').value = ''; $('custom-name').value = '';
  $('photo-empty').hidden = false; $('photo-filled').hidden = true;
  $('photo-input').value = ''; $('photo-error').hidden = true;
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

/* ── Сохранение в iiko ─────────────────────────────────────────────────── */
/* Покупка уходит одним запросом, но для Шоха это два этапа: фото летит по
   мобильной сети, потом сервер создаёт приходную накладную в iiko (секунды).
   Оба этапа видны над кнопками, процент — ещё и на самом фото; кнопка
   заперта до ответа, повторного сохранения нет. */
const MAX_PHOTO_BYTES = 6 * 1024 * 1024;  // как store.MAX_PHOTO_BYTES на сервере
let saveSlowTimer = null;

function saveStep(id, kind) {
  const step = $(id);
  step.className = 'shokh-save-step is-' + kind;
}
function photoProgress(share) {
  const percent = Math.round(Math.max(0, Math.min(1, share)) * 100) + '%';
  $('save-photo-pct').textContent = percent;
  $('confirm-photo-pct').textContent = percent;
  $('confirm-photo-bar').style.width = percent;
  $('save-bar').style.width = percent;
}
function startSaving(withPhoto) {
  const next = $('flow-next');
  next.classList.add('is-saving', 'rm-lock');
  next.setAttribute('aria-busy', 'true');
  next.setAttribute('aria-disabled', 'true');
  next.replaceChildren(node('span', 'shokh-spin'), node('span', '', withPhoto ? 'Отправляем фото…'
    : state.draft.custom ? 'Сохраняем…' : 'Сохраняем в iiko…'));
  // Уйти с экрана посреди записи нельзя: итог должен прийти сюда.
  $('flow-back').disabled = true; $('flow-close').disabled = true;
  const box = $('save-status');
  box.classList.remove('is-error');
  box.classList.toggle('is-waiting', !withPhoto);
  $('save-photo').hidden = !withPhoto;
  saveStep('save-photo', 'active');
  saveStep('save-iiko', withPhoto ? 'wait' : 'active');
  $('save-iiko-label').textContent = state.draft.custom ? 'Запись для бухгалтера · без накладной' : 'Приходная накладная iiko';
  $('save-slow').hidden = true; $('save-error').hidden = true;
  $('confirm-photo-box').classList.toggle('is-uploading', withPhoto);
  $('confirm-photo-state').hidden = !withPhoto;
  $('confirm-photo-text').textContent = 'Отправляем фото…';
  photoProgress(0);
  showStatus('save');
  pulse(box, 'is-in');
  clearTimeout(saveSlowTimer);
  saveSlowTimer = setTimeout(() => { $('save-slow').hidden = false; }, 6000);
}
function photoSent() {
  photoProgress(1);
  saveStep('save-photo', 'ok');
  saveStep('save-iiko', 'active');
  $('save-photo-pct').textContent = '';
  $('save-status').classList.add('is-waiting');
  $('confirm-photo-box').classList.remove('is-uploading');
  $('confirm-photo-box').classList.add('is-sent');
  $('confirm-photo-text').textContent = 'Фото отправлено';
  $('confirm-photo-pct').textContent = '';
  const label = $('flow-next').lastElementChild;
  if (label) label.textContent = state.draft.custom ? 'Сохраняем…' : 'Сохраняем в iiko…';
}
function stopSaving() {
  clearTimeout(saveSlowTimer);
  const next = $('flow-next');
  next.classList.remove('is-saving', 'rm-lock');
  next.removeAttribute('aria-busy');
  $('flow-back').disabled = false; $('flow-close').disabled = false;
  $('confirm-photo-box').classList.remove('is-uploading', 'is-sent');
  $('confirm-photo-state').hidden = true;
}
function saveFailed(text, photoStage) {
  const box = $('save-status');
  box.classList.remove('is-waiting');
  box.classList.add('is-error');
  saveStep(photoStage ? 'save-photo' : 'save-iiko', 'error');
  $('save-photo-pct').textContent = '';
  if (photoStage) saveStep('save-iiko', 'wait');
  $('save-slow').hidden = true;
  const reason = node('b', '', text);
  $('save-error').replaceChildren(reason, node('span', '', 'Повторите сохранение этой же формы: ключ покупки сохранён.'));
  $('save-error').hidden = false;
  showStatus('save');
  pulse(box, 'is-in');
}

/* XHR, а не fetch: только он сообщает, сколько фото уже ушло. */
function sendPurchase(form, {onUpload, onSent}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/shokh/purchase');
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.upload.addEventListener('progress', event => { if (event.lengthComputable) onUpload(event.loaded / event.total); });
    xhr.upload.addEventListener('load', onSent);
    xhr.addEventListener('load', () => {
      let payload = null;
      try { payload = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status >= 200 && xhr.status < 300 && payload) { onSent(); resolve(payload); return; }
      const error = new Error(typeof payload?.detail === 'string' ? payload.detail
        : xhr.status === 401 ? 'Войдите в панель заново.' : 'Не удалось сохранить покупку.');
      error.status = xhr.status;
      reject(error);
    });
    xhr.addEventListener('error', () => reject(Object.assign(new Error('Нет связи с сервером. Проверьте интернет.'), {status: 0})));
    xhr.send(form);
  });
}

/* Итог на экране «Записано»: подтверждён iiko — золотая галочка; нет —
   янтарный «!» и кнопка проверки тут же. */
function renderDoneIiko(purchase) {
  const iiko = purchase.iiko || {};
  // Товар не из справочника: накладной нет и ждать нечего — записано.
  const manual = iiko.status === 'manual';
  const synced = iiko.status === 'synced' || manual;
  state.donePurchase = purchase;
  $('screen-done').classList.toggle('is-uncertain', !synced);
  $('done-mark').textContent = synced ? '✓' : '!';
  pulse($('done-mark'), 'is-in');
  $('done-title').textContent = synced ? 'Записано!' : 'Покупка записана, iiko не подтверждён';
  $('done-iiko').textContent = manual ? 'Нет в iiko — бухгалтер заведёт товар и проведёт накладную'
    : synced ? 'Приходная накладная iiko № ' + iiko.number
    : (iiko.error || 'Проверьте статус на главной. Не вводите эту покупку повторно.');
  const check = $('done-check');
  check.hidden = synced;
  check.textContent = ['rejected', 'pending'].includes(iiko.status) ? 'Повторить отправку' : 'Проверить iiko';
}

async function submitPurchase() {
  applyPrice();
  if (state.submitting) return;
  if (!ready('point') || !ready('item') || !ready('amount')) { explain(state.step); return; }
  state.submitting = true;
  const draft = state.draft;
  const form = new FormData();
  form.append('point', draft.point); form.append('item', String(draft.item || '').trim());
  // Сервер не понимает пробелов-разделителей в числе — отправляем чистое.
  form.append('unit', draft.unit);
  form.append('quantity', String(draft.quantity).replace(/[\s  ]/g, '').replace(',', '.'));
  form.append('price', draft.price);
  form.append('operation_id', draft.operationId);
  // Новый товар: без товара iiko и без накладной, с пометкой для бухгалтера.
  if (draft.custom) form.append('off_catalog', 'true');
  else { form.append('product_id', draft.productId); form.append('unit_id', draft.unitId); }
  form.append('supplier_id', draft.supplierId); form.append('storage_id', draft.storageId);
  form.append('date', state.home.date);
  if (state.tripId !== null) form.append('trip_id', String(state.tripId));
  const withPhoto = !!state.photoFile;
  if (withPhoto) form.append('photo', state.photoFile);
  startSaving(withPhoto);
  let sent = !withPhoto;
  try {
    sessionStorage.setItem('shokh-pending-operation', draft.operationId);
    sessionStorage.setItem('shokh-pending-draft', JSON.stringify({draft, date: state.home.date, tripId: state.tripId, tripStartedAt: state.tripStartedAt}));
    const request = sendPurchase(form, {
      onUpload: share => { if (withPhoto && !sent) photoProgress(share); },
      onSent: () => { if (!sent) { sent = true; photoSent(); } },
    });
    const result = await Busy.track(request);
    sessionStorage.removeItem('shokh-pending-operation');
    sessionStorage.removeItem('shokh-pending-draft');
    // Покупка записана — черновика больше нет. Историю (частые товары,
    // обычная цена, поставщик точки) берём свежую для следующей покупки.
    dropDraft();
    refreshHistory();
    state.expectedPhoto = false;
    state.saveFailed = false;
    stopSaving();
    showStatus(null);
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
    renderDoneIiko(result.purchase);
    show('done');
    message('');
  } catch (error) {
    stopSaving();
    state.saveFailed = true;
    renderStep();
    saveFailed(error.message, !sent);
    // Тост здесь лишний: итог уже над кнопкой; строка сверху — для истории.
    message(error.message, true, {toast: false});
  }
  finally { state.submitting = false; }
}

function showSummary(data) {
  $('sum-time').textContent = L().clock(data.trip.minutes);
  $('sum-count').textContent = String(data.purchases.length);
  $('sum-total').textContent = money.format(Number(data.spent));
  renderPurchases($('sum-list'), data.purchases);
  state.tripId = null; state.tripStartedAt = null;
  show('summary');
  refreshHome();
}

async function finishTrip() {
  stopTimer(); dropDraft();
  if (state.tripId === null) { show('home'); refreshHome(); return true; }
  try {
    showSummary(await api('/trip/' + state.tripId + '/finish', {method: 'POST'}));
    return true;
  } catch (error) { message(error.message, true); return false; }
}

const tr = text => (document.documentElement.lang === 'uz' && globalThis.RetroI18n
  ? globalThis.RetroI18n.translate(text) || text : text);

/* × во время закупа (и «Назад» на первом шаге): есть покупки — закуп
   завершается и показывается итог, нет — отменяется. Несохранённую покупку
   просто так не бросаем: спрашиваем, и если сервер её всё-таки записал —
   говорим, что она в журнале. */
async function closeFlow() {
  if (state.submitting) return false;
  const key = sessionStorage.getItem('shokh-pending-operation');
  if (key) {
    if (!confirm(tr('Покупка не сохранена. Выйти без неё?'))) return false;
    try {
      const found = await api('/operation/' + encodeURIComponent(key));
      if (found.purchase) message('Покупка уже записана — она в списке «Сегодня».');
    } catch {}
    sessionStorage.removeItem('shokh-pending-operation');
    sessionStorage.removeItem('shokh-pending-draft');
    state.saveFailed = false; state.expectedPhoto = false;
  }
  stopTimer(); hideHint(); dropDraft();
  if (state.tripId === null) { show('home'); refreshHome(); return true; }
  try {
    const data = await api('/trip/' + state.tripId + '/close', {method: 'POST'});
    if (!data.cancelled) { showSummary(data); return true; }
    state.tripId = null; state.tripStartedAt = null;
  } catch (error) { message(error.message, true); }
  show('home'); refreshHome();
  return true;
}

/* ── Черновик закупа (F5 посреди шагов) ────────────────────────────────── */
/* Слабая связь на базаре: страница перезагрузилась — точка, поставщик,
   товар, количество и цена возвращаются. Фото в localStorage не кладём —
   его прикрепляют заново. Хранилище может быть недоступно — тогда молча
   работаем без черновика. */
const DRAFT_LAST = 'shokh-draft-last';
function persistDraft() {
  if (state.screen !== 'flow' || state.tripId === null || !state.home || state.submitting) return;
  try {
    const snapshot = L().draftSnapshot({tripId: state.tripId, tripStartedAt: state.tripStartedAt,
      date: state.home.date, step: state.step, draft: state.draft}, Date.now());
    localStorage.setItem(L().draftKey(state.tripId), JSON.stringify(snapshot));
    localStorage.setItem(DRAFT_LAST, String(state.tripId));
  } catch { /* хранилище недоступно — без черновика */ }
}
function dropDraft() {
  try {
    const last = localStorage.getItem(DRAFT_LAST);
    if (last) localStorage.removeItem(L().draftKey(last));
    if (state.tripId !== null) localStorage.removeItem(L().draftKey(state.tripId));
    localStorage.removeItem(DRAFT_LAST);
  } catch { /* нечего чистить */ }
}
function restoreDraft() {
  let saved = null;
  try {
    const last = localStorage.getItem(DRAFT_LAST);
    saved = last ? JSON.parse(localStorage.getItem(L().draftKey(last)) || 'null') : null;
  } catch { saved = null; }
  const found = L().restorableDraft(saved, state.home, Date.now());
  if (!found) { if (saved) dropDraft(); return false; }
  resetDraft();
  Object.assign(state.draft, found.draft);
  if (!state.draft.operationId) state.draft.operationId = crypto.randomUUID();
  const hadPhoto = !!found.draft.hasPhoto;
  state.draft.hasPhoto = false;
  state.tripId = found.tripId; state.tripStartedAt = found.tripStartedAt;
  // Было фото — возвращаемся на шаг товара: снимок нужно прикрепить заново.
  const steps = L().STEPS;
  state.step = hadPhoto && steps.indexOf(found.step) > steps.indexOf('item') ? 'item' : found.step;
  state.shownStep = null;
  $('qty-input').value = state.draft.quantity || '';
  $('price-input').value = state.draft.priceInput || '';
  $('custom-name').value = state.draft.custom ? state.draft.item || '' : '';
  $('point-other').value = state.draft.point && !state.catalog.points.includes(state.draft.point) ? state.draft.point : '';
  state.usual = usualFor(state.draft);
  renderSelectors(); renderPoints(); show('flow'); renderStep(); startTimer();
  message(hadPhoto ? 'Черновик восстановлен. Прикрепите фото заново.' : 'Черновик восстановлен.', false);
  return true;
}

/* Свежая история после покупки: частые товары, обычная цена и поставщик
   точки — без повторной загрузки всего справочника iiko. Фоном: не вышло —
   останется прежняя, а при следующем открытии страницы придёт новая. */
async function refreshHistory() {
  try {
    const fresh = await Busy.silent(() => api('/history'));
    if (!fresh || !Array.isArray(fresh.history)) return;
    state.catalog.items = L().withHistory(state.catalog.items, fresh.history);
    state.catalog.point_defaults = fresh.point_defaults || {};
  } catch { /* останется прежняя история */ }
}

/* ── События ───────────────────────────────────────────────────────────── */
$('start-purchase').addEventListener('click', () => {
  // Прошлая покупка не сохранилась — кнопка возвращает к ней (с тем же
  // ключом, без дубля), а не молчит выключенной.
  if (sessionStorage.getItem('shokh-pending-operation')) { Busy.button($('start-purchase'), reloadData(), {done: false}); return; }
  if (!state.catalogReady || !state.home) return;
  // Закуп открывается на сервере: кнопка крутится, пока он не ответил.
  Busy.button($('start-purchase'), (async () => {
    dropDraft();
    resetDraft();
    state.draft.point = '';
    state.step = 'point'; state.shownStep = null;
    const started = await beginTrip();
    if (!started) return false;
    renderSelectors(); renderPoints(); renderStep(); show('flow');
    return true;
  })(), {done: false});
});
$('flow-close').addEventListener('click', () => Busy.button($('flow-close'), closeFlow(), {done: false}));
// «Назад» на первом шаге — как ×, а не мёртвая кнопка.
$('flow-back').addEventListener('click', () => {
  if (state.step === 'point') { Busy.button($('flow-back'), closeFlow(), {done: false}); return; }
  state.step = L().previousStep(state.step); renderStep();
});
$('flow-next').addEventListener('click', () => {
  if (state.submitting) return;
  if (state.step === 'confirm') { submitPurchase(); return; }
  if (!ready(state.step)) { explain(state.step); return; }
  state.step = L().nextStep(state.step); renderStep();
});
$('point-other').addEventListener('input', event => {
  state.draft.point = event.target.value.trim();
  // Своя точка вместо плитки: плитка больше не выбрана. Точку уже знаем —
  // подставляем её поставщика и склад.
  if ((state.catalog.point_defaults || {})[state.draft.point]) { applyPointDefaults(state.draft.point); renderSelectors(); }
  renderPoints();
  setNext(ready('point'));
  persistDraft();
});
$('item-back-point').addEventListener('click', () => { state.step = 'point'; renderPoints(); renderStep(); });
$('item-search').addEventListener('input', renderItems);
$('item-add-custom').addEventListener('click', () => startCustom($('item-search').value, state.draft.unit));
$('custom-name').addEventListener('input', event => {
  // Как набрали, с пробелами: обрезка на ходу съедала пробел между словами.
  // Края обрезает сервер.
  state.draft.item = event.target.value;
  state.usual = usualFor(state.draft);
  renderCustom(); setNext(ready('item')); persistDraft();
});
$('custom-cancel').addEventListener('click', () => {
  Object.assign(state.draft, {custom: false, item: '', productId: null, unitId: null});
  renderItems(); setNext(ready('item')); persistDraft();
  $('item-search').focus({preventScroll: true});
});
/* Камера телефона даёт снимки по 3–8 МБ: большое фото ужимаем на телефоне
   (до 1600 px, JPEG), чтобы оно быстро ушло по мобильной сети и влезло в
   6 МБ сервера. Не открылось в браузере (HEIC в Chrome) — отправляем как есть. */
const PHOTO_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif'];  // как store.ALLOWED_PHOTO_TYPES
const PHOTO_SHRINK_BYTES = 1.5 * 1024 * 1024;
async function preparePhoto(file) {
  if (file.size <= PHOTO_SHRINK_BYTES && PHOTO_TYPES.includes(file.type)) return file;
  try {
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close?.();
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.82));
    if (!blob) return file;
    return new File([blob], String(file.name || 'photo').replace(/\.[^.]*$/, '') + '.jpg', {type: 'image/jpeg'});
  } catch { return file; }
}
function photoError(text) {
  const error = $('photo-error');
  error.textContent = text;
  error.hidden = false;
  pulse(error, 'is-in');
}
$('photo-input').addEventListener('change', async event => {
  const input = event.target;
  const picked = input.files && input.files[0];
  const error = $('photo-error');
  if (!picked) return;  // отменили выбор — прежнее фото остаётся
  const file = await preparePhoto(picked);
  // Сервер не примет не картинку и фото больше 6 МБ — говорим сразу, а не после сохранения.
  if (!PHOTO_TYPES.includes(file.type)) {
    photoError('Это не фото. Сфотографируйте товар или чек камерой.');
    input.value = '';
    return;
  }
  if (file.size > MAX_PHOTO_BYTES) {
    photoError('Фото больше 6 МБ — переснимите поменьше.');
    input.value = '';
    return;
  }
  error.hidden = true;
  state.photoFile = file;
  state.draft.hasPhoto = true;
  $('photo-empty').hidden = true;
  $('photo-filled').hidden = false;
  // Снимок с камеры декодируется заметное время: до этого — мерцание,
  // потом метка «Фото прикреплено».
  const filled = $('photo-filled'), preview = $('photo-preview');
  filled.classList.add('is-loading');
  filled.classList.remove('is-ready');
  preview.addEventListener('load', () => {
    filled.classList.remove('is-loading');
    filled.classList.add('is-ready');
    pulse($('photo-ok'), 'is-in');
  }, {once: true});
  preview.src = URL.createObjectURL(file);
  setNext(ready(state.step));
  persistDraft();
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
  pulse($('amount-total'), 'shokh-bump');
});
/* Кнопки количества: число в поле и итог коротко «подпрыгивают». */
function setQuantity(next) {
  state.draft.quantity = next ? String(Math.round(next * 1000) / 1000) : '';
  $('qty-input').value = state.draft.quantity;
  renderAmount();
  pulse($('qty-input'), 'shokh-bump');
  pulse($('amount-total'), 'shokh-bump');
}
$('qty-minus').addEventListener('click', () => {
  setQuantity(Math.max(0, (L().number(state.draft.quantity) || 0) - 1));
});
$('qty-plus').addEventListener('click', () => {
  setQuantity((L().number(state.draft.quantity) || 0) + 1);
});
$('done-more').addEventListener('click', () => {
  resetDraft();
  state.step = 'item';
  renderStep(); show('flow'); startTimer();
});
$('done-check').addEventListener('click', () => {
  const check = $('done-check'), purchase = state.donePurchase;
  if (!purchase) return;
  const retry = ['rejected', 'pending'].includes(purchase.iiko?.status);
  Busy.button(check, (async () => {
    try {
      const result = await api('/purchase/' + purchase.id + (retry ? '/retry' : '/sync'), retry ? {method: 'POST'} : {});
      const synced = result.purchase.iiko?.status === 'synced';
      if (synced) {
        globalThis.RetroToast?.show('iiko подтвердил накладную.', 'ok');
        // ✓ на кнопке виден, потом экран становится «Сохранено в iiko».
        setTimeout(() => renderDoneIiko(result.purchase), 900);
      } else {
        renderDoneIiko(result.purchase);
        message(result.purchase.iiko?.error || 'iiko ещё не подтвердил накладную.', true);
      }
      return synced;
    } catch (error) { message(error.message, true); return false; }
  })());
});
$('done-finish').addEventListener('click', () => Busy.button($('done-finish'), finishTrip(), {done: false}));
$('summary-home').addEventListener('click', () => { show('home'); refreshHome(); });

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
// Поставщик — это накладная iiko, а не точка: выбранную точку он не меняет.
$('iiko-supplier').addEventListener('change', event => {
  state.draft.supplierId = event.target.value;
  setNext(ready('point'));
  persistDraft();
});
$('iiko-storage').addEventListener('change', event => {
  state.draft.storageId = event.target.value;
  setNext(ready('point'));
  persistDraft();
});

async function loadCatalog() {
  // Уже загруженные справочники остаются в работе, пока идут свежие.
  const had = state.catalogReady;
  setIiko('loading', had ? 'Обновляем справочники iiko…' : 'Загружаем справочники iiko…',
    had ? 'Обновляем справочники iiko — обычно до 10 секунд…' : 'Загружаем справочники iiko — обычно до 10 секунд…');
  updateStart();
  try {
    const catalog = await api('/catalog');
    // История покупок → товары iiko: «Часто покупаете», обычная цена.
    catalog.items = L().withHistory(catalog.items, catalog.history);
    state.catalog = catalog;
    state.catalogReady = catalog.source === 'iiko' && catalog.can_create;
    setIiko(state.catalogReady ? 'ok' : 'error', state.catalogReady
      ? 'Подключено · товаров: ' + catalog.items.length + ' · складов: ' + catalog.storages.length
      : 'Сохранение в iiko недоступно. Проверьте подключение и права на приходные накладные.');
  } catch (error) {
    state.catalogReady = false;
    setIiko('error', error.message);
  }
  updateStart();
  return state.catalogReady;
}
/* Кнопка «Новый закуп»: пока её нельзя нажать, подпись говорит почему. */
function updateStart() {
  const pending = !!sessionStorage.getItem('shokh-pending-operation');
  const open = state.catalogReady && !!state.home && !pending;
  $('start-purchase').disabled = !open && !(pending && state.catalogReady && !!state.home);
  $('start-purchase').classList.toggle('is-waiting', !open && !pending && state.iiko === 'loading');
  $('start-purchase-sub').textContent = open ? 'Точка, товар, фото и цена'
    : pending ? 'Прошлая покупка не сохранена — нажмите, чтобы продолжить'
    : state.iiko === 'error' ? 'Нет связи с iiko — нажмите «Обновить данные»'
    : 'Ждём справочники iiko…';
}
/* Карточка «Связь с iiko»: ждём — золотая точка и бегущая полоса, дольше
   трёх секунд — объясняем, что так и должно быть; итог — зелёная или
   красная точка. */
let iikoSlowTimer = null;
function setIiko(kind, text, slowText = '') {
  state.iiko = kind;
  const card = $('iiko-card');
  card.classList.toggle('is-loading', kind === 'loading');
  card.classList.toggle('is-error', kind === 'error');
  card.classList.toggle('is-ok', kind === 'ok');
  if (kind === 'loading') card.setAttribute('aria-busy', 'true'); else card.removeAttribute('aria-busy');
  $('iiko-status').textContent = text;
  clearTimeout(iikoSlowTimer);
  if (kind === 'loading') {
    iikoSlowTimer = setTimeout(() => {
      if (state.iiko === 'loading' && slowText) $('iiko-status').textContent = slowText;
    }, 3000);
  }
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
  message('');
  await recoverPending();
  const [home, catalog] = await Promise.allSettled([refreshHome(), loadCatalog()]);
  if (state.recovery && state.catalogReady && state.home) {
    const saved = state.recovery; state.recovery = null;
    state.draft = saved.draft; state.home.date = saved.date;
    state.tripId = saved.tripId; state.tripStartedAt = saved.tripStartedAt;
    state.expectedPhoto = !!saved.draft.hasPhoto;
    state.draft.hasPhoto = false; state.photoFile = null;
    state.step = 'item'; state.shownStep = null;
    $('qty-input').value = state.draft.quantity;
    // Черновик до переключателя «за всё» хранил только цену за единицу.
    if (!state.draft.priceMode) state.draft.priceMode = 'unit';
    if (state.draft.priceInput == null) state.draft.priceInput = state.draft.price || '';
    $('price-input').value = state.draft.priceInput;
    renderSelectors(); renderPoints(); renderStep(); show('flow'); startTimer();
    if (state.expectedPhoto) message('Прикрепите прежнее фото и повторите сохранение этой покупки.', true);
  } else if (state.catalogReady && state.home && state.screen === 'home' && !sessionStorage.getItem('shokh-pending-operation')) {
    restoreDraft();
  }
  // false — кнопка без ✓: что-то не загрузилось, текст ошибки уже на экране.
  return home.value === true && catalog.value === true;
}
/* «Обновить данные»: кнопка в работе, пока идут и главная, и справочники
   iiko; повторное нажатие в это время не проходит. Первая загрузка — так же. */
function reloadAll() { return Busy.button($('reload-data'), reloadData()); }
$('reload-data').addEventListener('click', reloadAll);
reloadAll();
// Главная обновляется сама раз в минуту («Функционал» §1): выдачу бухгалтера
// или кассира Шох видит без нажатий. Фоном — без верхней полосы загрузки.
setInterval(() => {
  if (state.screen === 'home' && state.home && document.visibilityState === 'visible') Busy.silent(() => loadHome());
}, 60000);

// «‹ Панель» — только тем, кому открыт ещё какой-то модуль. У самого Шоха
// других модулей нет, и ссылка вела бы его по кругу обратно в закуп.
// Служебный запрос — без полосы загрузки.
fetch('/api/config', {headers: {accept: 'application/json'}, retroBusy: false})
  .then(response => (response.ok ? response.json() : null))
  .then(config => {
    // Сервер отдаёт только открытые этой учётной записи модули.
    const others = config && Array.isArray(config.modules)
      && config.modules.some(module => module.path !== '/shokh');
    if (others) document.getElementById('shokh-back').hidden = false;
  })
  .catch(() => {});
