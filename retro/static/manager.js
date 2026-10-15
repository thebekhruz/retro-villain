/* Кабинет менеджера — телефонное приложение по образцу телефона директора
   (T-459): вкладки внизу — «Сегодня», «Съёмка», «Добавить», «Команда» — и
   карточка сотрудника поверх них. Менеджер заводит людей своих разделов
   (ставку только предлагает — T-453), фотографирует (фото уходит на терминал
   Hikvision) и удаляет (T-452). Логика без DOM — в manager-logic.js. */
const $ = id => document.getElementById(id);
const L = () => globalThis.ManagerLogic;
const NETWORK = 'Нет связи с панелью. Проверьте интернет и повторите.';

/* Отклик и ожидание (busy.js): нажатие отвечает сразу, повторное не проходит. */
const passthrough = (el, work) => Promise.resolve(typeof work === 'function' ? work() : work);
const Busy = globalThis.RetroBusy || {button: passthrough, silent: fn => fn()};

const TABS = ['home', 'shoot', 'add', 'team', 'card'];
const state = {home: null, tab: 'home', back: 'home', group: '', filter: 'all', card: null, queue: false,
  preview: null, preparing: false, uploading: false, sending: false, saving: false,
  form: {role: '', type: 'shift'}};
const photoBusy = () => state.preparing || state.uploading || state.sending;

function node(tag, cls, text) {
  const element = document.createElement(tag);
  if (cls) element.className = cls;
  if (text !== undefined) element.textContent = text;
  return element;
}

/* Ошибка — строкой под шапкой (как у директора), успех — зелёной строкой. */
let flashTimer = 0;
function message(text, error = true) {
  $('state').hidden = !text;
  $('state').textContent = text || '';
  $('state').classList.toggle('is-error', Boolean(text) && error);
}
function flash(text) {
  clearTimeout(flashTimer);
  $('flash').textContent = text;
  $('flash').hidden = false;
  flashTimer = setTimeout(() => { $('flash').hidden = true; }, 4500);
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

/* ── Вкладки ───────────────────────────────────────────────────────────── */
function greeting() {
  const name = L().collapse(state.home?.name || '');
  return name ? 'Салом, ' + name[0].toUpperCase() + name.slice(1) : 'Кабинет менеджера';
}
const TITLES = {shoot: 'Съёмка', add: 'Новый сотрудник', team: 'Команда', card: 'Сотрудник'};

function openTab(tab) {
  if (photoBusy()) return;
  if (tab !== 'card') { state.tab = tab; state.queue = false; }
  TABS.forEach(name => { $('tab-' + name).hidden = name !== tab; });
  // Карточка — поверх вкладок: внизу подсвечена та, откуда пришли.
  document.querySelectorAll('.dir-tabs .rm-tab').forEach(button => {
    const active = button.dataset.tab === state.tab;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-selected', String(active));
  });
  // Приветствие — только на «Сегодня» (как у директора, director-app.css).
  document.body.dataset.dirTab = tab;
  $('screen-title').textContent = tab === 'home' ? greeting() : TITLES[tab];
  if (tab === 'add') resetForm();
  message('');
  window.scrollTo({top: 0});
}

/* ── Строки людей ──────────────────────────────────────────────────────── */
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

/* Метка состояния — плашкой директора (.dir-pill): зелёная «Готово»,
   песочная «Нет фото / Ждёт отправки», красная «Ошибка». */
const PILL = {ok: 'on_time', warn: 'unlinked', error: 'late', idle: 'manual'};
// В строке плашка короче: длинная («Фото не проверено») сжимала имя до разрыва слова.
const SHORT = {'Фото не проверено': 'Не проверено', 'Ждёт отправки': 'Не отправлено', 'Фото есть': 'вручную'};
function personRow(card, {pill = true} = {}) {
  const row = node('button', 'dir-person');
  row.type = 'button';
  row.dataset.busyKey = 'mgr:' + card.id;
  const main = node('span', 'dir-person-main');
  const name = node('span', 'dir-person-name');
  const strong = node('strong', '', card.name);
  strong.dataset.i18n = 'off';
  name.append(strong);
  const kind = L().typeTag(card);
  if (kind) name.append(node('span', 'dir-waiter-tag is-top', kind));
  main.append(name, node('small', '', L().roleLine(card)));
  const tag = L().rowTag(card);
  row.append(thumb(card), main);
  if (pill) row.append(node('span', 'dir-pill ' + PILL[tag.tone], SHORT[tag.text] || tag.text));
  row.append(node('span', 'dir-chevron', '›'));
  if (card.can_photo === false) row.disabled = true;
  row.addEventListener('click', () => openCard(card));
  return row;
}

function fillList(id, cards, empty, options) {
  const list = $(id);
  list.removeAttribute('aria-busy');
  list.replaceChildren(...(cards.length ? cards.map(card => personRow(card, options)) : [node('p', 'dir-empty', empty)]));
}

/* ── Сегодня ───────────────────────────────────────────────────────────── */
function renderHome() {
  const cards = state.home?.employees || [];
  const goal = L().goal(cards);
  $('goal-done').textContent = String(goal.done);
  $('goal-total').textContent = goal.total ? `из ${goal.total}` : '';
  $('goal-text').textContent = !goal.total ? 'В ваших разделах пока никого нет'
    : goal.done === goal.total ? 'Все на терминале с фото' : 'на терминале с фото';
  $('progress-bar').style.width = goal.percent + '%';
  const counts = L().summary(cards);
  const attention = cards.filter(card => !L().needsPhoto(card) && !L().isDone(card)).length;
  $('kpi-nophoto-n').textContent = String(counts.noPhoto);
  $('kpi-attention-n').textContent = String(attention);
  $('kpi-attention').classList.toggle('is-bad', attention > 0);
  $('shoot-next').textContent = counts.noPhoto
    ? `Снимать по очереди · ${counts.noPhoto} без фото →` : 'Фото есть у всех. Новых снимайте сразу после добавления';
  $('shoot-next').disabled = !counts.noPhoto;
  // Предложенные ставки, которые ждут бухгалтера (T-453).
  const pending = cards.filter(card => card.rate?.state === 'proposed');
  $('proposals').hidden = !pending.length;
  $('proposals-badge').textContent = String(pending.length);
  $('proposals-list').replaceChildren(...pending.map(card => {
    const line = node('div');
    const text = node('span');
    const strong = node('strong', '', card.name);
    strong.dataset.i18n = 'off';
    text.append(strong, node('small', '', L().rateLine(card)));
    line.append(node('i'), text);
    return line;
  }));
  // Разделы — плитками «Команда сегодня» директора: нажатие открывает команду.
  $('sections').replaceChildren(...L().groupsOf(cards).map(group => {
    const button = node('button', 'dir-count');
    button.type = 'button';
    const missing = cards.filter(card => card.group === group.key && L().needsPhoto(card)).length;
    if (missing) button.classList.add('is-nohik');
    button.append(node('strong', '', String(group.count)), node('span', '', group.label));
    if (missing) button.append(node('small', 'dir-count-sub', `без фото: ${missing}`));
    button.addEventListener('click', () => { state.group = group.key; state.filter = 'all'; openTab('team'); renderTeam(); });
    return button;
  }));
}

/* ── Съёмка ────────────────────────────────────────────────────────────── */
function renderShoot() {
  const cards = state.home?.employees || [];
  const nophoto = cards.filter(L().needsPhoto);
  const attention = cards.filter(card => !L().needsPhoto(card) && !L().isDone(card));
  $('shoot-badge').textContent = String(nophoto.length);
  $('shoot-badge').classList.toggle('is-ok', !nophoto.length);
  // В «Без фото» плашка «Нет фото» у каждого — лишняя: раздел уже так называется.
  fillList('shoot-list', nophoto, 'Фото есть у всех.', {pill: false});
  $('attention-card').hidden = !attention.length;
  $('attention-badge').textContent = String(attention.length);
  fillList('attention-list', attention, '');
  $('shoot-start').hidden = !nophoto.length;
}

/* ── Команда ───────────────────────────────────────────────────────────── */
const FILTERS = [['all', 'Все'], ['nophoto', 'Без фото'], ['attention', 'Не на терминале'], ['done', 'С фото']];
function inFilter(card, filter) {
  if (filter === 'nophoto') return L().needsPhoto(card);
  if (filter === 'attention') return !L().needsPhoto(card) && !L().isDone(card);
  if (filter === 'done') return L().isDone(card);
  return true;
}
function chipRow(id, items, current, pick) {
  $(id).replaceChildren(...items.map(([key, label]) => {
    const button = node('button', key === current ? 'is-active' : '', label);
    button.type = 'button';
    button.setAttribute('aria-pressed', String(key === current));
    button.addEventListener('click', () => { pick(key); renderTeam(); });
    return button;
  }));
  const on = $(id).querySelector('.is-active');
  if (on && (on.offsetLeft < $(id).scrollLeft || on.offsetLeft + on.offsetWidth > $(id).scrollLeft + $(id).clientWidth)) {
    $(id).scrollLeft = Math.max(0, on.offsetLeft - 16);
  }
}
function renderTeam() {
  const cards = state.home?.employees || [];
  const groups = L().groupsOf(cards);
  if (state.group && !groups.some(group => group.key === state.group)) state.group = '';
  const inGroup = cards.filter(card => !state.group || card.group === state.group);
  chipRow('team-filters', FILTERS.map(([key, label]) => [key, `${label} ${inGroup.filter(card => inFilter(card, key)).length}`]),
    state.filter, key => { state.filter = key; });
  $('groups').hidden = groups.length < 2;
  chipRow('groups', [['', 'Все разделы'], ...groups.map(group => [group.key, group.label])], state.group,
    key => { state.group = key; });
  const query = $('search').value;
  const shown = L().filterCards(inGroup, {}, query).filter(card => inFilter(card, state.filter));
  const empty = !cards.length ? 'В ваших разделах пока никого нет. Добавьте первого: «+ Добавить».'
    : L().collapse(query) ? `Никого не нашли по «${L().collapse(query)}». Если человека нет в списке, добавьте его: «+ Добавить».`
      : 'Никого в этом списке.';
  fillList('team-list', shown, empty);
}

function renderList() {
  renderHome();
  renderShoot();
  renderTeam();
  if (state.tab === 'home' && !$('tab-home').hidden) $('screen-title').textContent = greeting();
}

async function loadHome() {
  try {
    const {data} = await api('/home');
    state.home = data;
    // Профиль (T-461): буква, имя и разделы — в кружке шапки и в листе.
    const name = data.name || data.login || '';
    const mark = (Array.from(name.trim())[0] || '').toUpperCase();
    $('profile-mark').textContent = mark;
    $('profile-sheet-mark').textContent = mark;
    $('profile-name').textContent = name;
    $('profile-dirs').textContent = [data.role === 'manager' ? 'Менеджер' : 'Администратор', ...data.directions].join(' · ');
    $('device-photo-note').textContent = data.photo_message || '';
    $('device-photo-note').hidden = !data.photo_message;
    renderList();
  } catch (error) {
    $('team-list').removeAttribute('aria-busy');
    message(error.message);
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

/* Следующий без фото — по порядку реестра (как в «Съёмке»). */
function nextCard() {
  return state.card ? L().nextToShoot(state.home?.employees || [], state.card.id) : null;
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
  $('card-name').textContent = card.name;
  $('card-role').textContent = L().roleLine(card);
  const rateText = L().rateLine(card);
  $('card-rate').textContent = rateText;
  $('card-rate').hidden = !rateText;
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
  $('card-hint').hidden = finished;
  $('card-done').hidden = !finished || card.hikvision.state === 'manual';
  // Очередь: сколько ещё без фото.
  const left = (state.home?.employees || []).filter(item => L().needsPhoto(item) && item.id !== card.id).length;
  $('card-queue').hidden = !state.queue;
  $('card-queue').textContent = left ? `ещё без фото: ${left}` : 'последний без фото';
  // Фото уже есть и дальше есть кого снимать — главная кнопка «Дальше».
  const next = !busy && card.photo ? nextCard() : null;
  $('card-next').hidden = !next;
  $('card-next-name').textContent = next ? next.name : '';
  $('card-shoot').hidden = !!next;
  $('card-reshoot').hidden = !next;
  $('card-shoot-label').textContent = card.photo || state.preview ? 'Переснять фото' : 'Сфотографировать';
  $('card-retry').hidden = busy || !card.can_retry;
  const locked = busy || card.can_photo === false;
  for (const id of ['card-shoot', 'card-reshoot', 'card-gallery', 'card-avatar']) $(id).disabled = locked;
  $('card-close').disabled = busy;
  $('delete-zone').hidden = card.can_photo === false;
  $('card-delete').disabled = busy;
}

function openCard(card, {queue = false} = {}) {
  if (photoBusy()) return;
  state.card = card;
  state.queue = queue;
  state.preview = null;
  state.uploading = false;
  state.sending = false;
  $('delete-confirm').hidden = true;
  $('delete-note').hidden = true;
  $('card-delete').hidden = false;
  renderCard();
  openTab('card');
}

function closeCard() {
  if (photoBusy()) return;
  const back = state.tab;
  state.card = null;
  state.queue = false;
  openTab(back);
  renderList();
}

function startQueue() {
  const first = L().nextToShoot(state.home?.employees || []);
  if (first) openCard(first, {queue: true});
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
      : error.message);
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
    message(error.network ? 'Связь прервалась — результат отправки неизвестен. Откройте карточку снова и проверьте статус фото.' : error.message);
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
    message('Не удалось открыть фото. Выберите другой снимок или сфотографируйте ещё раз.');
  } finally {
    state.preparing = false;
    if (state.card?.id === card.id) renderCard();
  }
}

/* Удаление (T-452) — как в редакторе директора: «Удалить сотрудника», под ним
   «Оставить / Да, удалить». Сервер стирает человека и из реестра бухгалтера. */
async function deleteEmployee() {
  const card = state.card;
  if (!card) return;
  try {
    await api(`/employees/${card.id}`, {method: 'DELETE'});
  } catch (error) {
    if (error.status !== 404) { message(error.message); return; }
  }
  if (state.home) state.home.employees = state.home.employees.filter(item => item.id !== card.id);
  closeCard();
  flash(`Сотрудник удалён: ${card.name}.`);
}

/* ── Новый сотрудник ───────────────────────────────────────────────────── */
function renderRoles() {
  const roles = state.home?.roles || [];
  const picked = L().collapse(state.form.role).toLowerCase();
  $('f-roles').replaceChildren(...roles.map(item => {
    const on = L().roleKeyOf(item) === picked;
    const button = node('button', on ? 'is-active' : '', item.role);
    button.type = 'button';
    button.setAttribute('role', 'radio');
    button.setAttribute('aria-checked', String(on));
    button.addEventListener('click', () => { state.form.role = item.role; $('form-error').textContent = ''; renderRoles(); });
    return button;
  }));
  const chosen = roles.find(item => L().roleKeyOf(item) === picked);
  $('f-role-group').textContent = chosen ? 'Раздел: ' + chosen.group : '';
  $('f-role-group').hidden = !chosen;
}

function setType(type) {
  state.form.type = type;
  document.querySelectorAll('#type-seg [data-type]').forEach(button => {
    const on = button.dataset.type === type;
    button.classList.toggle('is-active', on);
    button.setAttribute('aria-pressed', String(on));
  });
  if ($('f-period')) $('f-period').hidden = type !== 'temporary';
}

function resetForm() {
  state.form = {role: '', type: 'shift'};
  $('employee-form').reset();
  $('form-error').textContent = '';
  $('similar').hidden = true;
  setType('shift');
  renderRoles();
}

function similarRow(match) {
  const card = (state.home?.employees || []).find(item => item.id === match.id);
  if (card) return personRow(card);
  const row = node('div', 'dir-person');
  const main = node('span', 'dir-person-main');
  const strong = node('strong', '', match.name);
  strong.dataset.i18n = 'off';
  const name = node('span', 'dir-person-name');
  name.append(strong);
  main.append(name, node('small', '', [match.role, match.group].filter(Boolean).join(' · ')));
  row.append(node('span', 'shokh-thumb is-empty', L().initials(match.name)), main);
  return row;
}

async function saveEmployee(confirm = false) {
  if (state.saving) return;
  const form = {name: $('f-name').value, role: state.form.role, type: state.form.type,
    from: $('f-from')?.value || '', to: $('f-to')?.value || '', rate: $('f-rate').value};
  const errors = L().formErrors(form);
  const first = errors.name || errors.role || errors.rate || errors.period || '';
  $('form-error').textContent = first;
  if (first) return;
  state.saving = true;
  try {
    const temporary = form.type === 'temporary';
    const {data} = await api('/employees', {method: 'POST', body: JSON.stringify({
      name: form.name, role: form.role, employment_type: form.type,
      work_from: temporary && form.from ? form.from : null, work_to: temporary && form.to ? form.to : null,
      proposed_rate: L().rateDigits(form.rate).value, confirm_new: confirm})});
    const card = data.employee;
    state.home.employees.push(card);
    state.home.employees.sort((a, b) => a.name.localeCompare(b.name, 'ru'));
    renderList();
    // Карточка нового человека открывается сразу — с «Сфотографировать».
    state.tab = 'team';
    openCard(card);
  } catch (error) {
    if (error.status === 409 && Array.isArray(error.payload?.matches)) {
      $('similar-list').replaceChildren(...error.payload.matches.map(similarRow));
      $('similar').hidden = false;
      $('similar').scrollIntoView({block: 'nearest', behavior: 'smooth'});
      return;
    }
    $('form-error').textContent = error.message;
  } finally {
    state.saving = false;
  }
}

/* ── События ───────────────────────────────────────────────────────────── */
document.querySelectorAll('.dir-tabs .rm-tab').forEach(button => {
  button.addEventListener('click', () => { if (!photoBusy()) { state.card = null; openTab(button.dataset.tab); renderList(); } });
});
$('shoot-next').addEventListener('click', startQueue);
$('shoot-start').addEventListener('click', startQueue);
$('kpi-nophoto').addEventListener('click', () => openTab('shoot'));
$('kpi-attention').addEventListener('click', () => { state.filter = 'attention'; state.group = ''; openTab('team'); renderTeam(); });
$('team-add').addEventListener('click', () => openTab('add'));
$('form-close').addEventListener('click', () => openTab('home'));
$('search').addEventListener('input', renderTeam);
$('card-close').addEventListener('click', closeCard);
$('card-next').addEventListener('click', () => { const next = nextCard(); if (next) openCard(next, {queue: true}); });
for (const id of ['card-shoot', 'card-reshoot', 'card-avatar']) $(id).addEventListener('click', () => $('photo-input').click());
$('card-gallery').addEventListener('click', () => $('gallery-input').click());
$('photo-input').addEventListener('change', photoTaken);
$('gallery-input').addEventListener('change', photoTaken);
$('card-retry').addEventListener('click', () => Busy.button($('card-retry'), sendToHikvision(), {done: false}));
$('card-delete').addEventListener('click', () => {
  $('card-delete').hidden = true; $('delete-confirm').hidden = false; $('delete-note').hidden = false;
});
$('delete-cancel').addEventListener('click', () => {
  $('card-delete').hidden = false; $('delete-confirm').hidden = true; $('delete-note').hidden = true;
});
$('delete-yes').addEventListener('click', () => Busy.button($('delete-yes'), deleteEmployee(), {done: false}));
$('employee-form').addEventListener('submit', event => {
  event.preventDefault();
  Busy.button($('form-save'), saveEmployee(false), {done: false});
});
$('similar-new').addEventListener('click', () => Busy.button($('similar-new'), saveEmployee(true), {done: false}));
$('f-name').addEventListener('input', () => { $('form-error').textContent = ''; $('similar').hidden = true; });
document.querySelectorAll('#type-seg [data-type]').forEach(button => {
  button.addEventListener('click', () => setType(button.dataset.type));
});
// Ставка: «200000» → «200 000» по ходу ввода (T-453).
$('f-rate').addEventListener('input', event => {
  $('form-error').textContent = '';
  const input = event.currentTarget;
  const formatted = L().formatRate(input.value);
  if (formatted !== input.value) input.value = formatted;
});

// Дата под шапкой — как у директора: «Среда, 14 октября».
try {
  const today = new Intl.DateTimeFormat('ru-RU', {weekday: 'long', day: 'numeric', month: 'long', timeZone: 'Asia/Tashkent'})
    .format(new Date());
  $('today-label').textContent = today[0].toUpperCase() + today.slice(1);
} catch {}

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
