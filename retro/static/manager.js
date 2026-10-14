/* Кабинет менеджера: сотрудники своих разделов (кухня, зал, уборка).
   Главная (T-450): цель «на терминале N из M», плитки — кого ещё снять,
   новый сотрудник, кто не дошёл до терминала, — и список: сначала без фото,
   потом остальные. Менеджер заводит человека (ставку назначит бухгалтер),
   фотографирует — фото сохраняется в карточке и уходит на терминал
   Hikvision. Логика без DOM — в manager-logic.js. */
const $ = id => document.getElementById(id);
const L = () => globalThis.ManagerLogic;
const NETWORK = 'Нет связи с панелью. Проверьте интернет и повторите.';
const CAMERA = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 8.5A2.5 2.5 0 0 1 6.5 6h1.2l1.4-2h5.8l1.4 2h1.2A2.5 2.5 0 0 1 20 8.5v8A2.5 2.5 0 0 1 17.5 19h-11A2.5 2.5 0 0 1 4 16.5z"/><circle cx="12" cy="12.5" r="3.4"/></svg>';

/* Отклик и ожидание (busy.js): нажатие отвечает сразу, повторное не проходит. */
const passthrough = (el, work) => Promise.resolve(typeof work === 'function' ? work() : work);
const Busy = globalThis.RetroBusy || {button: passthrough, silent: fn => fn()};

const state = {home: null, group: '', role: '', card: null, queue: false, preview: null,
  preparing: false, uploading: false, sending: false, saving: false,
  form: {role: '', type: 'shift'}};
const photoBusy = () => state.preparing || state.uploading || state.sending;

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

/* Успех — только тостом: строка сверху сдвигала бы экран. */
function done(text) {
  message('');
  globalThis.RetroToast?.show(text, 'ok');
}

function show(screen) {
  for (const name of ['home', 'card', 'form']) $('screen-' + name).hidden = name !== screen;
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
    image.onerror = () => { box.replaceChildren(L().initials(card.name)); box.classList.add('is-empty'); };
    box.append(image);
  }
  return box;
}

function nameLine(card) {
  const title = node('span', 'shokh-purchase-title');
  const name = node('span', '', card.name);
  name.dataset.i18n = 'off';
  title.append(name);
  // «временный · 08.10–10.10» (T-434): период — с сервера.
  const kind = L().typeTag(card);
  if (kind) title.append(node('span', 'shokh-flag', kind));
  return title;
}

/* Человек без фото — плитка: инициалы, имя, должность, значок камеры. */
function personTile(card) {
  const tile = node('button', 'mgr-person-tile');
  tile.type = 'button';
  const initials = node('span', 'mgr-initials', L().initials(card.name));
  initials.dataset.i18n = 'off';
  initials.setAttribute('aria-hidden', 'true');
  const name = node('span', 'mgr-person-tile-name', card.name);
  name.dataset.i18n = 'off';
  const cam = node('span', 'mgr-cam');
  cam.innerHTML = CAMERA;
  tile.append(initials, name, node('span', 'mgr-person-tile-role', L().roleLine(card)), cam);
  const kind = L().typeTag(card);
  if (kind) tile.append(node('span', 'shokh-flag', kind));
  if (card.can_photo === false) tile.disabled = true;
  tile.addEventListener('click', () => openCard(card));
  return tile;
}

/* С фото или «не на терминале» — строка: фото, имя, должность; метка только
   там, где она что-то говорит (ошибка, ждёт отправки, ручная отметка). */
function personRow(card) {
  const row = node('button', 'shokh-purchase mgr-row');
  row.type = 'button';
  const body = node('span', 'shokh-purchase-body');
  body.append(nameLine(card), node('span', 'shokh-note', L().roleLine(card)));
  row.append(thumb(card), body);
  const tag = L().rowTag(card);
  if (!L().isDone(card) || tag.tone === 'idle') {
    const tone = {ok: 'shokh-ok', warn: 'shokh-flag', error: 'shokh-flag is-error', idle: 'shokh-flag is-idle'}[tag.tone];
    row.append(node('span', 'mgr-tag ' + tone, tag.tone === 'idle' ? 'вручную' : tag.text));
  }
  row.addEventListener('click', () => openCard(card));
  return row;
}

function chip(label, count, on, pick) {
  const button = node('button', 'mgr-filter' + (on ? ' is-on' : ''));
  button.type = 'button';
  button.setAttribute('aria-pressed', String(on));
  button.append(label + ' ', node('b', '', String(count)));
  button.addEventListener('click', () => { pick(); renderList(); });
  return button;
}

/* Чипы разделов и должностей: строятся из списка, со счётом людей. */
function renderPlaces(cards) {
  const groups = L().groupsOf(cards);
  if (state.group && !groups.some(item => item.key === state.group)) { state.group = ''; state.role = ''; }
  $('groups').hidden = groups.length < 2;
  $('groups').replaceChildren(chip('Все разделы', cards.length, !state.group, () => { state.group = ''; state.role = ''; }),
    ...groups.map(item => chip(item.label, item.count, state.group === item.key, () => { state.group = item.key; state.role = ''; })));
  const roles = state.group ? L().rolesOf(cards, state.group) : [];
  if (state.role && !roles.some(item => item.key === state.role)) state.role = '';
  $('roles').hidden = roles.length < 2;
  $('roles').replaceChildren(...(roles.length < 2 ? [] : [
    chip('Все должности', roles.reduce((sum, item) => sum + item.count, 0), !state.role, () => { state.role = ''; }),
    ...roles.map(item => chip(item.label, item.count, state.role === item.key, () => { state.role = item.key; }))]));
  // Выбранный чип в длинной строке — в поле зрения, страницу не двигаем.
  for (const id of ['groups', 'roles']) {
    const row = $(id), on = row.querySelector('.is-on');
    if (on && (on.offsetLeft < row.scrollLeft || on.offsetLeft + on.offsetWidth > row.scrollLeft + row.clientWidth)) {
      row.scrollLeft = Math.max(0, on.offsetLeft - 16);
    }
  }
}

/* Кто сейчас на экране: раздел, должность, поиск. Очередь съёмки идёт по нему же. */
function shownCards() {
  return L().filterCards(state.home?.employees || [], {group: state.group, role: state.role}, $('search').value);
}

function renderGoal(cards) {
  const goal = L().goal(cards);
  $('goal-done').textContent = goal.total ? String(goal.done) : '0';
  $('goal-total').textContent = goal.total ? `из ${goal.total}` : '';
  $('goal-text').textContent = !goal.total ? 'В ваших разделах пока никого нет'
    : goal.done === goal.total ? 'Все на терминале с фото' : 'на терминале с фото';
  $('progress-bar').style.width = goal.percent + '%';
}

function renderTiles(cards) {
  const counts = L().summary(cards.filter(card => L().inPlace(card, state)));
  const left = counts.noPhoto;
  const main = $('tile-shoot');
  main.classList.toggle('is-calm', !left);
  main.disabled = !left;
  $('tile-shoot-num').textContent = left ? String(left) : '';
  $('tile-shoot-label').textContent = left ? 'без фото' : 'Фото есть у всех';
  $('tile-shoot-go').textContent = left ? (left > 1 ? 'Снимать по очереди' : 'Сфотографировать') : 'Новых снимайте сразу после добавления';
  const attention = cards.filter(card => L().inPlace(card, state) && !L().needsPhoto(card) && !L().isDone(card)).length;
  $('tile-problem').hidden = !attention;
  $('tile-problem-num').textContent = String(attention);
  document.querySelector('.mgr-bento').classList.toggle('no-alert', !attention);
}

function renderList() {
  const cards = state.home?.employees || [];
  renderPlaces(cards);
  renderGoal(cards);
  renderTiles(cards);
  const query = $('search').value;
  const shown = shownCards();
  const list = $('list');
  list.removeAttribute('aria-busy');
  list.replaceChildren(...L().sections(shown).map(section => {
    const box = node('section', 'mgr-section is-' + section.key);
    box.id = 'section-' + section.key;
    const head = node('h3', 'mgr-section-head');
    head.append(node('span', '', section.title), node('b', '', String(section.cards.length)));
    const body = node('div', section.key === 'nophoto' ? 'mgr-grid' : 'shokh-purchases mgr-rows');
    body.append(...section.cards.map(section.key === 'nophoto' ? personTile : personRow));
    box.append(head, body);
    return box;
  }));
  const note = $('list-note');
  let text = '';
  if (!cards.length) text = 'Сотрудников ваших разделов пока нет. Добавьте первого: «Новый сотрудник».';
  else if (!shown.length && L().collapse(query)) text = `Никого не нашли по «${L().collapse(query)}». Если человека нет в списке, добавьте его: «Новый сотрудник».`;
  note.textContent = text;
  note.hidden = !text;
}

function renderHome(data) {
  state.home = data;
  $('device-photo-note').textContent = data.photo_message || '';
  $('device-photo-note').hidden = !data.photo_message;
  $('home-sub').textContent = data.directions.join(' · ');
  const name = data.name || data.login || '';
  const mark = (Array.from(name.trim())[0] || '').toUpperCase();
  $('account-mark').textContent = mark;
  $('sheet-mark').textContent = mark;
  $('account-name').textContent = name;
  $('account-dirs').textContent = data.directions.join(' · ');
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

/* Следующий без фото — в том же порядке, что на главной. */
function nextCard() {
  return state.card ? L().nextToShoot(shownCards(), state.card.id) : null;
}

function renderCard() {
  const card = state.card;
  const src = state.preview || card.photo?.url || '';
  $('card-photo').hidden = !src;
  $('card-photo').onerror = () => {
    $('card-photo').hidden = true;
    $('card-initials').textContent = L().initials(card.name);
    $('card-hik-note').textContent = 'Не удалось загрузить фото. Обновите страницу.';
    $('card-hik-note').hidden = false;
  };
  if (src) $('card-photo').src = src; else $('card-photo').removeAttribute('src');
  $('card-initials').textContent = src ? '' : L().initials(card.name);
  $('card-avatar').classList.toggle('has-photo', !!src);
  $('card-name').textContent = card.name;
  $('card-role').textContent = L().roleLine(card);
  paintStep('step-photo', L().photoStep(card, state.uploading));
  if (state.preparing) paintStep('step-photo', {step: 'active', title: 'Подготавливаем фото…'});
  else if (state.uploading && card.hikvision.state !== 'manual') {
    paintStep('step-photo', {step: 'active', title: 'Сохраняем фото и отправляем в Hikvision…'});
  }
  const person = L().hikvisionStep(card, state.sending);
  paintStep('step-hik', person);
  $('step-hik-no').textContent = person.step === 'ok' && person.number ? 'ID ' + person.number : '';
  const face = state.uploading ? null : person.step === 'ok' ? L().faceStep(card, state.sending) : null;
  paintStep('step-face', face);
  const note = person.note || face?.note || '';
  $('card-hik-note').textContent = note;
  $('card-hik-note').hidden = !note;
  const busy = photoBusy();
  const finished = !busy && L().isDone(card);
  $('card-status').classList.toggle('is-waiting', busy);
  $('card-status').classList.toggle('is-error', person.step === 'error' || face?.step === 'error');
  $('card-retry').hidden = busy || !card.can_retry;
  $('card-hint').hidden = finished;
  $('card-done').hidden = !finished || card.hikvision.state === 'manual';
  // Очередь: сколько ещё без фото среди тех, что на экране.
  const left = shownCards().filter(item => L().needsPhoto(item) && item.id !== card.id).length;
  $('card-queue').hidden = !state.queue;
  $('card-queue').textContent = left ? `ещё без фото: ${left}` : 'последний без фото';
  // Фото уже есть и дальше есть кого снимать — главная кнопка «Дальше».
  const next = !busy && card.photo ? nextCard() : null;
  $('card-next').hidden = !next;
  $('card-next-name').textContent = next ? next.name : '';
  document.querySelector('.mgr-photo-actions').classList.toggle('has-next', !!next);
  $('card-gallery').textContent = next ? 'Из галереи' : 'Выбрать из галереи';
  $('card-shoot-label').textContent = card.photo || state.preview ? 'Переснять фото' : 'Сфотографировать';
  $('card-shoot').disabled = busy || card.can_photo === false;
  $('card-gallery').disabled = busy || card.can_photo === false;
  $('card-avatar').disabled = busy || card.can_photo === false;
  $('card-close').disabled = busy;
  $('card-delete').hidden = card.can_photo === false;
  $('card-delete').disabled = busy;
}

function openCard(card, {queue = state.queue} = {}) {
  if (photoBusy()) return;
  state.card = card;
  state.queue = queue;
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

/* Фото с камеры или из галереи: центральный квадрат 1:1, не больше 640×640.
   Сначала учитываем поворот снимка, затем обрезаем края: пропорции лица
   остаются прежними и в карточке, и в квадратном превью Hikvision. */
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
  const cropSide = Math.min(width, height);
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = Math.min(side, cropSide);
  try {
    canvas.getContext('2d').drawImage(source,
      (width - cropSide) / 2, (height - cropSide) / 2, cropSide, cropSide,
      0, 0, canvas.width, canvas.height);
    return canvas.toDataURL('image/jpeg', 0.86);
  } finally {
    source.close?.();
  }
}

async function sendToHikvision() {
  if (!state.card || photoBusy()) return false;
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
async function uploadPhoto(card, photo) {
  state.preview = photo;
  state.uploading = true;
  renderCard();
  try {
    const {data} = await api(`/employees/${card.id}/photo?send=true`, {method: 'PUT', body: JSON.stringify({image: photo})});
    replaceCard(data.employee);
    return true;
  } catch (error) {
    // Ответ мог потеряться после сохранения: обновляем карточку, не обещая,
    // что запись не произошла, и не отправляем запрос для другого сотрудника.
    try { replaceCard((await api(`/employees/${card.id}`)).data.employee); } catch {}
    message(error.network ? 'Связь прервалась — результат отправки неизвестен. Откройте карточку снова и проверьте статус фото.' : error.message, true);
    return false;
  } finally {
    state.uploading = false;
    state.preview = null;
    if (state.card && state.card.id === card.id) renderCard();
    renderList();
  }
}

async function photoTaken(event) {
  const input = event.currentTarget;
  const file = input.files[0];
  input.value = '';
  if (!file || !state.card || photoBusy() || state.card.can_photo === false) return;
  // Привязка к человеку ДО первого await, включая декодирование снимка.
  const card = state.card;
  state.preparing = true;
  renderCard();
  try {
    const photo = await shrinkPhoto(file);
    state.preparing = false;
    await Busy.button($(input.id === 'gallery-input' ? 'card-gallery' : 'card-shoot'),
      uploadPhoto(card, photo), {done: false});
  } catch {
    message('Не удалось открыть фото. Выберите другой снимок или сфотографируйте ещё раз.', true);
  } finally {
    state.preparing = false;
    if (state.card?.id === card.id) renderCard();
  }
}

/* ── Новый сотрудник ───────────────────────────────────────────────────── */
function fieldError(id, text) {
  $(id).textContent = text || '';
  $(id).hidden = !text;
}

function renderRoles() {
  const roles = state.home?.roles || [];
  const picked = L().collapse(state.form.role).toLowerCase();
  $('f-roles').replaceChildren(...roles.map(item => {
    const button = node('button', 'mgr-role', item.role);
    button.type = 'button';
    button.setAttribute('role', 'radio');
    const on = L().roleKeyOf(item) === picked;
    button.setAttribute('aria-checked', String(on));
    button.addEventListener('click', () => {
      state.form.role = item.role;
      fieldError('f-role-error', '');
      renderRoles();
    });
    return button;
  }));
  const chosen = roles.find(item => L().roleKeyOf(item) === picked);
  $('f-role-group').textContent = chosen ? 'Раздел: ' + chosen.group : '';
  $('f-role-group').hidden = !chosen;
}

function setType(type) {
  state.form.type = type;
  document.querySelectorAll('.mgr-type [data-type]').forEach(button => {
    const on = button.dataset.type === type;
    button.classList.toggle('is-on', on);
    button.setAttribute('aria-pressed', String(on));
  });
  if ($('f-period')) $('f-period').hidden = type !== 'temporary';
}

function openForm() {
  if (photoBusy()) return;
  state.form = {role: '', type: 'shift'};
  $('employee-form').reset();
  fieldError('f-name-error', '');
  fieldError('f-role-error', '');
  $('similar').hidden = true;
  setType('shift');
  renderRoles();
  show('form');
  $('f-name').focus({preventScroll: true});
}

function similarRow(match) {
  const card = (state.home?.employees || []).find(item => item.id === match.id);
  const row = node('button', 'shokh-purchase mgr-row');
  row.type = 'button';
  const body = node('span', 'shokh-purchase-body');
  const name = node('span', 'shokh-purchase-title');
  const text = node('span', '', match.name);
  text.dataset.i18n = 'off';
  name.append(text);
  body.append(name, node('span', 'shokh-note', [match.role, match.group].filter(Boolean).join(' · ')));
  row.append(card ? thumb(card) : node('span', 'shokh-thumb is-empty', L().initials(match.name)), body);
  if (card) {
    row.append(node('span', 'mgr-tag shokh-ok', 'Это он'));
    row.addEventListener('click', () => { show('home'); openCard(card, {queue: false}); });
  } else {
    row.disabled = true;
  }
  return row;
}

async function saveEmployee(confirm = false) {
  if (state.saving) return;
  const form = {name: $('f-name').value, role: state.form.role, type: state.form.type,
    from: $('f-from')?.value || '', to: $('f-to')?.value || ''};
  const errors = L().formErrors(form);
  fieldError('f-name-error', errors.name);
  fieldError('f-role-error', errors.role || errors.period);
  if (Object.keys(errors).length) {
    (errors.name ? $('f-name') : $('f-roles')).scrollIntoView({block: 'center', behavior: 'smooth'});
    return;
  }
  state.saving = true;
  try {
    const temporary = form.type === 'temporary';
    const {data} = await api('/employees', {method: 'POST', body: JSON.stringify({
      name: form.name, role: form.role, employment_type: form.type,
      work_from: temporary && form.from ? form.from : null, work_to: temporary && form.to ? form.to : null,
      confirm_new: confirm})});
    const card = data.employee;
    const list = state.home.employees;
    list.push(card);
    list.sort((a, b) => a.name.localeCompare(b.name, 'ru'));
    renderList();
    done('Сотрудник добавлен. Сфотографируйте его: фото уйдёт на терминал.');
    openCard(card, {queue: false});
  } catch (error) {
    if (error.status === 409 && Array.isArray(error.payload?.matches)) {
      $('similar-list').replaceChildren(...error.payload.matches.map(similarRow));
      $('similar').hidden = false;
      $('similar').scrollIntoView({block: 'nearest', behavior: 'smooth'});
      return;
    }
    // Имя или должность не прошли проверку кириллицы — подпись у поля.
    if (error.status === 422 && /имен|ФИО|фамил/i.test(error.message)) fieldError('f-name-error', error.message);
    else if (error.status === 422 || error.status === 403) fieldError('f-role-error', error.message);
    else message(error.message, true);
  } finally {
    state.saving = false;
  }
}

/* ── Удалить сотрудника (T-452) ────────────────────────────────────────── */
function openSheet(id) {
  const sheet = $(id);
  if (typeof sheet.showModal === 'function') sheet.showModal(); else sheet.setAttribute('open', '');
}
function closeSheet(id) {
  const sheet = $(id);
  if (typeof sheet.close === 'function') sheet.close(); else sheet.removeAttribute('open');
}

function askDelete() {
  if (!state.card || photoBusy()) return;
  $('delete-name').textContent = state.card.name;
  openSheet('delete-sheet');
}

/* Удаляем у всех сразу: сервер стирает человека из реестра бухгалтера.
   Уже удалён (404) — тоже убираем из списка. */
async function deleteEmployee() {
  const card = state.card;
  if (!card) return;
  try {
    await api(`/employees/${card.id}`, {method: 'DELETE'});
  } catch (error) {
    if (error.status !== 404) { message(error.message, true); return; }
  }
  closeSheet('delete-sheet');
  if (state.home) state.home.employees = state.home.employees.filter(item => item.id !== card.id);
  state.card = null;
  state.queue = false;
  show('home');
  renderList();
  done(`Сотрудник удалён: ${card.name}.`);
}

/* ── Учётная запись ────────────────────────────────────────────────────── */
const openAccount = () => openSheet('account-sheet');
const closeAccount = () => closeSheet('account-sheet');

/* ── События ───────────────────────────────────────────────────────────── */
$('search').addEventListener('input', renderList);
$('tile-shoot').addEventListener('click', () => {
  const first = L().nextToShoot(shownCards());
  if (first) openCard(first, {queue: true});
});
$('tile-add').addEventListener('click', openForm);
$('tile-problem').addEventListener('click', () => {
  $('section-attention')?.scrollIntoView({block: 'start', behavior: 'smooth'});
});
$('card-close').addEventListener('click', () => { if (!photoBusy()) { state.queue = false; show('home'); renderList(); } });
$('card-next').addEventListener('click', () => { const next = nextCard(); if (next) openCard(next, {queue: true}); });
$('card-shoot').addEventListener('click', () => $('photo-input').click());
$('card-avatar').addEventListener('click', () => $('photo-input').click());
$('card-gallery').addEventListener('click', () => $('gallery-input').click());
$('photo-input').addEventListener('change', photoTaken);
$('gallery-input').addEventListener('change', photoTaken);
$('card-retry').addEventListener('click', () => Busy.button($('card-retry'), sendToHikvision(), {done: false}));
$('form-close').addEventListener('click', () => { show('home'); renderList(); });
$('form-save').addEventListener('click', () => Busy.button($('form-save'), saveEmployee(false), {done: false}));
$('similar-new').addEventListener('click', () => Busy.button($('similar-new'), saveEmployee(true), {done: false}));
$('employee-form').addEventListener('submit', event => { event.preventDefault(); $('form-save').click(); });
$('f-name').addEventListener('input', () => { fieldError('f-name-error', ''); $('similar').hidden = true; });
document.querySelectorAll('.mgr-type [data-type]').forEach(button => {
  button.addEventListener('click', () => setType(button.dataset.type));
});
$('account-open').addEventListener('click', openAccount);
$('account-close').addEventListener('click', closeAccount);
$('card-delete').addEventListener('click', askDelete);
$('delete-cancel').addEventListener('click', () => closeSheet('delete-sheet'));
$('delete-confirm').addEventListener('click', () => Busy.button($('delete-confirm'), deleteEmployee(), {done: false}));
// Нажатие на затемнение вокруг листа закрывает его (по самому листу — нет).
for (const id of ['account-sheet', 'delete-sheet']) {
  $(id).addEventListener('click', event => {
    if (event.target !== event.currentTarget) return;
    const box = event.currentTarget.getBoundingClientRect();
    const inside = event.clientX >= box.left && event.clientX <= box.right && event.clientY >= box.top && event.clientY <= box.bottom;
    if (!inside) closeSheet(id);
  });
}

loadHome();

// «Вся панель» — только тем, кому открыт ещё какой-то модуль (администратору).
fetch('/api/config', {headers: {accept: 'application/json'}, retroBusy: false})
  .then(response => (response.ok ? response.json() : null))
  .then(config => {
    const others = config && Array.isArray(config.modules)
      && config.modules.some(module => module.path !== '/manager');
    if (others) $('mgr-back').hidden = false;
  })
  .catch(() => {});
