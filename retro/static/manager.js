/* Кабинет менеджера (ТЗ 09.10, М-01…М-04): найти человека в общей базе,
   завести сменного или временного сотрудника и увидеть, что он добавлен в
   Hikvision. Логика без DOM — в manager-logic.js. */
const $ = id => document.getElementById(id);
const L = () => globalThis.ManagerLogic;
const DRAFT_KEY = 'retro-manager-draft';
const NETWORK = 'Нет связи с панелью. Проверьте интернет и повторите — второй карточки не будет.';

/* Отклик и ожидание (busy.js): нажатие отвечает сразу, повторное не проходит. */
const passthrough = (el, work) => Promise.resolve(typeof work === 'function' ? work() : work);
const Busy = globalThis.RetroBusy || {button: passthrough, silent: fn => fn()};

let state = {home: null, card: null, cardFrom: 'home', sending: false, draft: null};

function node(tag, cls, text) {
  const element = document.createElement(tag);
  if (cls) element.className = cls;
  if (text !== undefined) element.textContent = text;
  return element;
}

function message(text, error = false) {
  const box = $('mgr-message');
  box.textContent = text; box.hidden = !text;
  box.classList.toggle('is-error', error);
  box.setAttribute('role', error ? 'alert' : 'status');
  if (text) globalThis.RetroToast?.show(text, error ? 'error' : 'ok');
}

function show(screen) {
  for (const name of ['home', 'form', 'card']) $('screen-' + name).hidden = name !== screen;
  window.scrollTo(0, 0);
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch('/api/manager' + path, {cache: 'no-store', ...options,
      headers: {accept: 'application/json', 'Content-Type': 'application/json', ...(options.headers || {})}});
  } catch {
    const error = new Error(NETWORK);
    error.network = true;
    throw error;
  }
  let payload = null;
  try { payload = await response.json(); } catch {}
  if (response.status === 401) { window.location.assign('/login'); }
  if (!response.ok) {
    const error = new Error(typeof payload?.detail === 'string' ? payload.detail : 'Не удалось выполнить запрос. Повторите.');
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return {status: response.status, data: payload};
}

/* ── Главная ───────────────────────────────────────────────────────────── */
function personRow(card, onOpen) {
  const row = node('button', 'shokh-purchase mgr-row');
  row.type = 'button';
  const thumb = node('span', 'shokh-thumb is-empty', L().initials(card.name));
  thumb.setAttribute('aria-hidden', 'true');
  thumb.dataset.i18n = 'off';
  const body = node('span', 'shokh-purchase-body');
  const title = node('span', 'shokh-purchase-title');
  const name = node('span', '', card.name);
  name.dataset.i18n = 'off';
  title.append(name);
  if (card.employment_type === 'temporary') title.append(node('span', 'shokh-flag', 'временный'));
  if (card.kind === 'monthly') title.append(node('span', 'shokh-flag is-idle', 'на окладе'));
  const sub = node('span', 'shokh-note', [card.role, card.direction].filter(Boolean).join(' · '));
  body.append(title, sub);
  row.append(thumb, body);
  const tag = L().hikvisionTag(card);
  if (tag) {
    const tone = {ok: 'shokh-ok', warn: 'shokh-flag', error: 'shokh-flag is-error', idle: 'shokh-flag is-idle'}[tag.tone];
    row.append(node('span', 'mgr-tag ' + tone, tag.text));
  }
  row.addEventListener('click', () => onOpen(card));
  return row;
}

function renderHome(data) {
  state.home = data;
  $('home-sub').textContent = data.directions.map(item => item.name).join(' · ') + ' · ' + data.login;
  $('home-hikvision').textContent = data.hikvision.configured
    ? 'Hikvision подключён: новый сотрудник уходит на устройство сразу.'
    : 'Hikvision не подключён: карточки сохраняются и ждут отправки.';
  const mine = $('mine');
  mine.removeAttribute('aria-busy');
  $('mine-count').textContent = data.mine.length ? String(data.mine.length) : '';
  if (!data.mine.length) {
    mine.replaceChildren(node('p', 'shokh-note', 'Вы ещё никого не добавили. Сначала найдите человека в базе — вдруг он уже есть.'));
    return;
  }
  mine.replaceChildren(...data.mine.map(card => personRow(card, item => openCard(item, 'home'))));
}

async function loadHome() {
  try {
    const {data} = await api('/home');
    renderHome(data);
  } catch (error) {
    $('mine').removeAttribute('aria-busy');
    $('mine').replaceChildren(node('p', 'shokh-note', 'Не удалось загрузить список. Обновите страницу.'));
    message(error.message, true);
  }
}

/* ── Поиск ─────────────────────────────────────────────────────────────── */
let searchTimer = 0, searchSeq = 0;

function searchNote(text) {
  $('search-note').textContent = text || '';
  $('search-note').hidden = !text;
}

async function runSearch() {
  const query = L().collapse($('search').value);
  const seq = ++searchSeq;
  const results = $('search-results');
  if (query.length < 2) {
    results.hidden = true;
    results.replaceChildren();
    searchNote(query.length === 1 ? 'Наберите ещё хотя бы одну букву.' : '');
    return;
  }
  try {
    const {data} = await api('/search?q=' + encodeURIComponent(query));
    if (seq !== searchSeq) return;
    results.replaceChildren(...data.results.map(card => personRow(card, item => openCard(item, 'home'))));
    results.hidden = !data.results.length;
    if (!data.results.length) searchNote(`Никого не нашли по «${query}». Новый человек — нажмите «Новый сотрудник».`);
    else searchNote(data.more ? 'Показаны первые 20 — уточните запрос.' : '');
  } catch (error) {
    if (seq === searchSeq) searchNote(error.message);
  }
}

/* ── Новый сотрудник ───────────────────────────────────────────────────── */
function readDraft() {
  try { return JSON.parse(sessionStorage.getItem(DRAFT_KEY) || 'null'); } catch { return null; }
}
function keepDraft() {
  try { sessionStorage.setItem(DRAFT_KEY, JSON.stringify(state.draft)); } catch { /* приватный режим */ }
}
function dropDraft() {
  try { sessionStorage.removeItem(DRAFT_KEY); } catch { /* приватный режим */ }
}

function fieldError(field, text) {
  const input = $('f-' + field);
  const box = $('f-' + field + '-error');
  if (!box) { message(text, true); return; }
  box.textContent = text || '';
  box.hidden = !text;
  if (text) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid');
}

function fillRoles() {
  const direction = (state.home?.directions || []).find(item => item.name === $('f-direction').value);
  $('role-options').replaceChildren(...(direction?.roles || []).map(role => new Option(role, role)));
}

function setType(type) {
  state.draft.type = type;
  document.querySelectorAll('.mgr-type [data-type]').forEach(button => {
    const on = button.dataset.type === type;
    button.classList.toggle('is-on', on);
    button.setAttribute('aria-pressed', String(on));
  });
}

function openForm() {
  const directions = state.home?.directions || [];
  if (!directions.length) { message('Направления не заданы — обратитесь к администратору панели.', true); return; }
  // Незаконченная отправка в этой вкладке продолжается с тем же ключом:
  // если первая дошла, сервер вернёт ту же карточку.
  const saved = readDraft();
  const typed = L().collapse($('search').value);
  state.draft = saved && saved.key ? saved : {
    key: L().newKey(), name: typed && !L().checkText(typed).error ? typed : '', role: '',
    direction: directions[0].name, type: 'shift'};
  $('f-direction').replaceChildren(...directions.map(item => new Option(item.name, item.name)));
  $('f-direction').value = directions.some(item => item.name === state.draft.direction)
    ? state.draft.direction : directions[0].name;
  $('f-name').value = state.draft.name || '';
  $('f-role').value = state.draft.role || '';
  fillRoles();
  setType(state.draft.type || 'shift');
  fieldError('name', ''); fieldError('role', '');
  $('similar').hidden = true;
  show('form');
}

function closeForm() {
  // Ввод не терялся бы при случайном «×», но и не висел вечно: черновик
  // остаётся только у незавершённой отправки (ключ уже ушёл на сервер).
  if (!state.draft?.sent) dropDraft();
  show('home');
}

function syncDraft() {
  state.draft.name = $('f-name').value;
  state.draft.role = $('f-role').value;
  state.draft.direction = $('f-direction').value;
}

function liveCheck(field) {
  // Латиница видна сразу, пока набирают; пустое поле — только при сохранении.
  const value = $('f-' + field).value;
  const result = value.trim() ? L().checkText(value, field) : {};
  fieldError(field, result.error || '');
}

function renderSimilar(matches) {
  $('similar-list').replaceChildren(...matches.map(card => personRow(card, item => openCard(item, 'form'))));
  $('similar').hidden = false;
  $('similar').scrollIntoView({block: 'nearest', behavior: 'smooth'});
}

async function save(confirmNew = false) {
  syncDraft();
  const name = L().checkText($('f-name').value, 'name');
  const role = L().checkText($('f-role').value, 'role');
  fieldError('name', name.error || '');
  fieldError('role', role.error || '');
  if (name.error || role.error) {
    $(name.error ? 'f-name' : 'f-role').focus();
    return false;
  }
  state.draft.sent = true;
  keepDraft();
  try {
    const {data} = await api('/employees', {method: 'POST', body: JSON.stringify({
      name: name.value, role: role.value, direction: state.draft.direction,
      employment_type: state.draft.type, request_key: state.draft.key, confirm_new: confirmNew})});
    dropDraft();
    state.draft = null;
    $('search').value = '';
    runSearch();
    openCard(data.employee, 'home');
    if (!data.created) message('Эта карточка уже была сохранена — показываем её.');
    if (data.employee.can_retry) await sendToHikvision();
    Busy.silent(() => loadHome());
    return true;
  } catch (error) {
    // Отказ сервера (4xx) — карточки нет; обрыв связи или 5xx — может быть,
    // поэтому ключ остаётся прежним.
    if (error.status && error.status < 500) state.draft.sent = false;
    keepDraft();
    if (error.status === 409 && error.payload?.matches) {
      renderSimilar(error.payload.matches);
      return false;
    }
    const field = L().fieldOfError(error.message);
    if (field === 'name' || field === 'role') fieldError(field, error.message);
    else message(error.message, true);
    return false;
  }
}

/* ── Карточка и Hikvision ──────────────────────────────────────────────── */
function stamp(iso) {
  return iso ? `${iso.slice(8, 10)}.${iso.slice(5, 7)} ${iso.slice(11, 16)}` : '';
}

function renderCard() {
  const card = state.card;
  $('card-name').textContent = card.name;
  $('card-role').textContent = card.role || '—';
  $('card-direction').textContent = card.direction && card.group && card.group !== card.direction
    ? card.direction + ' · ' + card.group : (card.direction || card.group || '—');
  $('card-type').textContent = card.employment_label || '—';
  $('card-author-row').hidden = !card.created_by;
  $('card-author').textContent = [card.created_by, stamp(card.created_at)].filter(Boolean).join(' · ');
  const view = L().hikvisionStep(card, state.sending);
  $('step-saved').hidden = !card.created_by;
  const step = $('step-hik');
  step.className = 'shokh-save-step is-' + view.step;
  $('step-hik-title').textContent = view.title;
  $('step-hik-no').textContent = view.step === 'ok' && view.number ? 'ID ' + view.number : '';
  $('card-hik-note').textContent = view.note || '';
  $('card-hik-note').hidden = !view.note;
  $('card-status').classList.toggle('is-waiting', state.sending);
  $('card-status').classList.toggle('is-error', view.step === 'error');
  $('card-retry').hidden = state.sending || !card.can_retry;
}

function openCard(card, from) {
  state.card = card;
  state.cardFrom = from;
  state.sending = false;
  renderCard();
  show('card');
}

async function sendToHikvision() {
  const card = state.card;
  state.sending = true;
  renderCard();
  try {
    const {data} = await api(`/employees/${card.id}/hikvision`, {method: 'POST'});
    if (state.card && state.card.id === card.id) state.card = data.employee;
    return data.employee.hikvision.state === 'sent';
  } catch (error) {
    message(error.network
      ? 'Нет связи с панелью — не знаем, дошло ли до Hikvision. Нажмите «Отправить ещё раз»: второго человека не будет.'
      : error.message, true);
    return false;
  } finally {
    state.sending = false;
    if (state.card && state.card.id === card.id) renderCard();
    Busy.silent(() => loadHome());
  }
}

/* ── События ───────────────────────────────────────────────────────────── */
$('search').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 250);
});
$('new-employee').addEventListener('click', openForm);
$('form-close').addEventListener('click', closeForm);
$('form-cancel').addEventListener('click', closeForm);
$('f-name').addEventListener('input', () => { liveCheck('name'); $('similar').hidden = true; });
$('f-role').addEventListener('input', () => liveCheck('role'));
$('f-direction').addEventListener('change', () => { syncDraft(); fillRoles(); });
document.querySelectorAll('.mgr-type [data-type]').forEach(button => {
  button.addEventListener('click', () => setType(button.dataset.type));
});
$('employee-form').addEventListener('submit', event => {
  event.preventDefault();
  Busy.button($('form-save'), save(), {done: false});
});
$('form-save').addEventListener('click', () => Busy.button($('form-save'), save(), {done: false}));
$('similar-new').addEventListener('click', () => Busy.button($('similar-new'), save(true), {done: false}));
$('card-retry').addEventListener('click', () => Busy.button($('card-retry'), sendToHikvision(), {done: false}));
$('card-close').addEventListener('click', () => show(state.cardFrom === 'form' ? 'form' : 'home'));
$('card-done').addEventListener('click', () => {
  // «Это он» из похожих: нового не заводим — черновик больше не нужен.
  if (state.cardFrom === 'form') { dropDraft(); state.draft = null; }
  show('home');
});

loadHome();

// «‹ Панель» — только тем, кому открыт ещё какой-то модуль (администратору).
fetch('/api/config', {headers: {accept: 'application/json'}, retroBusy: false})
  .then(response => (response.ok ? response.json() : null))
  .then(config => {
    const others = config && Array.isArray(config.modules)
      && config.modules.some(module => module.path !== '/manager');
    if (others) $('mgr-back').hidden = false;
  })
  .catch(() => {});
