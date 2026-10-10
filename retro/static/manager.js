/* Кабинет менеджера: сотрудники своих разделов (кухня, зал, уборка). Заводит
   их бухгалтер; менеджер выбирает человека из списка и фотографирует — фото
   сохраняется в карточке и уходит на терминал Hikvision. Логика без DOM —
   в manager-logic.js. */
const $ = id => document.getElementById(id);
const L = () => globalThis.ManagerLogic;
const NETWORK = 'Нет связи с панелью. Проверьте интернет и повторите.';

/* Отклик и ожидание (busy.js): нажатие отвечает сразу, повторное не проходит. */
const passthrough = (el, work) => Promise.resolve(typeof work === 'function' ? work() : work);
const Busy = globalThis.RetroBusy || {button: passthrough, silent: fn => fn()};

const state = {home: null, filter: 'all', card: null, preview: null, uploading: false, sending: false};

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
  for (const name of ['home', 'card']) $('screen-' + name).hidden = name !== screen;
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

/* ── Список ────────────────────────────────────────────────────────────── */
function thumb(card) {
  const box = node('span', card.photo ? 'shokh-thumb mgr-thumb' : 'shokh-thumb is-empty', card.photo ? undefined : L().initials(card.name));
  box.setAttribute('aria-hidden', 'true');
  box.dataset.i18n = 'off';
  if (card.photo) {
    const image = node('img');
    image.src = card.photo.url; image.alt = ''; image.loading = 'lazy';
    box.append(image);
  }
  return box;
}

function personRow(card) {
  const row = node('button', 'shokh-purchase mgr-row');
  row.type = 'button';
  const body = node('span', 'shokh-purchase-body');
  const title = node('span', 'shokh-purchase-title');
  const name = node('span', '', card.name);
  name.dataset.i18n = 'off';
  title.append(name);
  // «временный · 08.10–10.10» (T-434): период — с сервера.
  const kind = L().typeTag(card);
  if (kind) title.append(node('span', 'shokh-flag', kind));
  body.append(title, node('span', 'shokh-note', L().roleLine(card)));
  const tag = L().rowTag(card);
  const tone = {ok: 'shokh-ok', warn: 'shokh-flag', error: 'shokh-flag is-error', idle: 'shokh-flag is-idle'}[tag.tone];
  row.append(thumb(card), body, node('span', 'mgr-tag ' + tone, tag.text));
  row.addEventListener('click', () => openCard(card));
  return row;
}

function renderList() {
  const cards = state.home?.employees || [];
  const counts = L().summary(cards);
  $('count-all').textContent = String(counts.total);
  $('count-nophoto').textContent = String(counts.noPhoto);
  $('count-problem').textContent = String(counts.problems);
  $('count-problem').parentElement.hidden = !counts.problems && state.filter !== 'problem';
  $('progress-text').textContent = counts.total
    ? `Фото есть у ${counts.withPhoto} из ${counts.total}` : 'В ваших разделах пока нет сотрудников';
  $('progress-bar').style.width = counts.total ? Math.round(counts.withPhoto * 100 / counts.total) + '%' : '0';
  document.querySelectorAll('.mgr-filter').forEach(button => {
    const on = button.dataset.filter === state.filter;
    button.classList.toggle('is-on', on);
    button.setAttribute('aria-pressed', String(on));
  });
  const query = $('search').value;
  const shown = L().filterCards(cards, state.filter, query);
  const list = $('list');
  list.removeAttribute('aria-busy');
  list.replaceChildren(...shown.map(personRow));
  const note = $('list-note');
  let text = '';
  if (!cards.length) text = 'Сотрудников ваших разделов пока нет. Их заводит бухгалтер в «Сотрудниках».';
  else if (!shown.length && L().collapse(query)) text = `Никого не нашли по «${L().collapse(query)}». Если человека нет в списке — попросите бухгалтера завести карточку.`;
  else if (!shown.length && state.filter === 'nophoto') text = 'У всех есть фото.';
  else if (!shown.length) text = 'Ошибок нет.';
  note.textContent = text;
  note.hidden = !text;
}

function renderHome(data) {
  state.home = data;
  $('home-sub').textContent = [data.directions.join(' · '), data.login].filter(Boolean).join(' · ');
  renderList();
}

async function loadHome() {
  try {
    const {data} = await api('/home');
    renderHome(data);
  } catch (error) {
    $('list').removeAttribute('aria-busy');
    $('list').replaceChildren(node('p', 'shokh-note', 'Не удалось загрузить список. Обновите страницу.'));
    message(error.message, true);
  }
}

/* ── Сотрудник ─────────────────────────────────────────────────────────── */
function paintStep(id, view) {
  const step = $(id);
  step.hidden = !view;
  if (!view) return;
  step.className = 'shokh-save-step is-' + view.step;
  $(id + '-title').textContent = view.title;
}

function renderCard() {
  const card = state.card;
  const src = state.preview || card.photo?.url || '';
  $('card-photo').hidden = !src;
  if (src) $('card-photo').src = src; else $('card-photo').removeAttribute('src');
  $('card-initials').textContent = src ? '' : L().initials(card.name);
  $('card-avatar').classList.toggle('has-photo', !!src);
  $('card-name').textContent = card.name;
  $('card-role').textContent = L().roleLine(card);
  paintStep('step-photo', L().photoStep(card, state.uploading));
  const person = L().hikvisionStep(card, state.sending);
  paintStep('step-hik', person);
  $('step-hik-no').textContent = person.step === 'ok' && person.number ? 'ID ' + person.number : '';
  const face = person.step === 'ok' ? L().faceStep(card, state.sending) : null;
  paintStep('step-face', face);
  const note = person.note || face?.note || '';
  $('card-hik-note').textContent = note;
  $('card-hik-note').hidden = !note;
  const busy = state.uploading || state.sending;
  $('card-status').classList.toggle('is-waiting', busy);
  $('card-status').classList.toggle('is-error', person.step === 'error' || face?.step === 'error');
  $('card-retry').hidden = busy || !card.can_retry;
  $('card-shoot-label').textContent = card.photo || state.preview ? 'Переснять фото' : 'Сфотографировать';
  $('card-shoot').disabled = busy || card.can_photo === false;
  $('card-avatar').disabled = busy || card.can_photo === false;
}

function openCard(card) {
  state.card = card;
  state.preview = null;
  state.uploading = false;
  state.sending = false;
  renderCard();
  show('card');
}

function replaceCard(card) {
  if (state.card && state.card.id === card.id) state.card = card;
  const list = state.home?.employees;
  const index = list ? list.findIndex(item => item.id === card.id) : -1;
  if (index >= 0) list[index] = card;
}

/* Фото с камеры телефона: ужимаем до ~640 px JPEG — терминалу нужно небольшое
   фото лица, а по сотовой связи большое грузилось бы долго. */
async function shrinkPhoto(file, side = 640) {
  let source;
  try { source = await createImageBitmap(file, {imageOrientation: 'from-image'}); } catch { source = null; }
  if (!source) {
    source = await new Promise((resolve, reject) => {
      const image = new Image();
      const url = URL.createObjectURL(file);
      image.onload = () => { URL.revokeObjectURL(url); resolve(image); };
      image.onerror = () => { URL.revokeObjectURL(url); reject(new Error('photo')); };
      image.src = url;
    });
  }
  const width = source.width, height = source.height;
  if (!width || !height) throw new Error('photo');
  const scale = Math.min(1, side / Math.max(width, height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);
  canvas.getContext('2d').drawImage(source, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL('image/jpeg', 0.86);
}

async function sendToHikvision() {
  const card = state.card;
  state.sending = true;
  renderCard();
  try {
    const {data} = await api(`/employees/${card.id}/hikvision`, {method: 'POST'});
    replaceCard(data.employee);
    return true;
  } catch (error) {
    message(error.network
      ? 'Нет связи с панелью — не знаем, дошло ли до Hikvision. Нажмите «Отправить ещё раз»: второго человека не будет.'
      : error.message, true);
    return false;
  } finally {
    state.sending = false;
    if (state.card && state.card.id === card.id) renderCard();
    renderList();
  }
}

/* Снятое фото сразу видно в круге; сохраняем его в карточке и отправляем на
   терминал. Не дошло — фото на экране остаётся, можно повторить. */
async function uploadPhoto(photo) {
  const card = state.card;
  state.preview = photo;
  state.uploading = true;
  renderCard();
  try {
    const {data} = await api(`/employees/${card.id}/photo`, {method: 'PUT', body: JSON.stringify({image: photo})});
    replaceCard(data.employee);
  } catch (error) {
    state.preview = null;
    message(error.network ? 'Нет связи — фото не сохранилось. Сфотографируйте ещё раз.' : error.message, true);
    return false;
  } finally {
    state.uploading = false;
    if (state.card && state.card.id === card.id) renderCard();
  }
  state.preview = null;
  renderList();
  if (state.card.can_retry) await sendToHikvision();
  return true;
}

async function photoTaken() {
  const file = $('photo-input').files[0];
  $('photo-input').value = '';
  if (!file || !state.card) return;
  let photo;
  try {
    photo = await shrinkPhoto(file);
  } catch {
    message('Не удалось открыть фото. Сфотографируйте ещё раз.', true);
    return;
  }
  await Busy.button($('card-shoot'), uploadPhoto(photo), {done: false});
}

/* ── События ───────────────────────────────────────────────────────────── */
$('search').addEventListener('input', renderList);
document.querySelectorAll('.mgr-filter').forEach(button => {
  button.addEventListener('click', () => { state.filter = button.dataset.filter; renderList(); });
});
$('card-close').addEventListener('click', () => { show('home'); renderList(); });
$('card-shoot').addEventListener('click', () => $('photo-input').click());
$('card-avatar').addEventListener('click', () => $('photo-input').click());
$('photo-input').addEventListener('change', photoTaken);
$('card-retry').addEventListener('click', () => Busy.button($('card-retry'), sendToHikvision(), {done: false}));

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
