/* «Пользователи» (T-445): администратор заводит людей в панель, меняет роль,
   номер для входа по SMS, пароль и направления менеджера, выключает и
   удаляет. Записи, которые ещё в переменных окружения, переносятся в базу
   кнопкой. Правила (что нельзя сохранить и почему) — на сервере; экран
   показывает его ответ словами. */
(() => {
  const $ = id => document.getElementById(id);
  const tr = text => (document.documentElement.lang === 'uz' && globalThis.RetroI18n ? RetroI18n.translate(text) || text : text);
  const B = globalThis.RetroBusy;

  let view = null, editing = null, busy = false;

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') el.className = value;
      else if (key === 'text') el.textContent = value;
      else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
      else el.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat()) if (child !== null && child !== undefined && child !== false)
      el.append(child instanceof Node ? child : String(child));
    return el;
  }
  function message(text, error = false) {
    const box = $('us-message');
    box.textContent = text; box.hidden = !text; box.classList.toggle('is-error', error);
    if (text) globalThis.RetroToast?.show(text, error ? 'error' : 'ok');
  }
  function formMessage(text) {
    const box = $('us-form-message');
    box.textContent = text || ''; box.hidden = !text;
  }

  async function request(url, method = 'GET', body) {
    const options = {method, headers: {accept: 'application/json'}};
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const response = await fetch(url, options);
    let result = {};
    try { result = await response.json(); } catch { /* пустой ответ */ }
    if (!response.ok) {
      const detail = result.detail;
      // Ошибка проверки полей от FastAPI приходит списком.
      throw new Error(typeof detail === 'string' ? detail : 'Проверьте поля формы.');
    }
    return result;
  }

  async function load() {
    view = await request('/api/users');
    render();
  }

  async function act(work, success, button) {
    if (busy) return false;
    busy = true;
    let promise = Promise.resolve().then(work);
    if (B && button) promise = B.button(button, promise);
    try {
      view = await promise;
      render();
      if (success) message(success);
      return true;
    } catch (error) {
      if ($('us-dialog').open) formMessage(error.message); else message(error.message, true);
      return false;
    } finally {
      busy = false;
    }
  }

  /* ── Таблица ───────────────────────────────────────────────────────── */
  function signIn(user) {
    if (user.password && user.phone) return 'Пароль и SMS';
    if (user.phone) return 'Только SMS';
    return 'Пароль';
  }
  function statusPill(user) {
    if (user.source === 'env') return h('span', {class: 'fd-pill us-env-pill', text: 'В переменных'});
    return user.active
      ? h('span', {class: 'fd-pill on_time', text: 'Включена'})
      : h('span', {class: 'fd-pill missing', text: 'Выключена'});
  }
  function rowFor(user) {
    const action = user.source === 'env'
      ? h('button', {class: 'fd-btn fd-btn-light us-row-btn', type: 'button', text: 'Перенести в базу',
                     onclick: event => act(() => request('/api/users/' + encodeURIComponent(user.login) + '/import', 'POST'),
                                           '«' + user.login + '» перенесена в базу.', event.currentTarget)})
      : h('button', {class: 'fd-btn fd-btn-light us-row-btn', type: 'button', text: 'Изменить',
                     onclick: () => openDialog(user)});
    return h('div', {class: 'fd-row us-cols' + (user.active ? '' : ' is-off')},
      h('span', {class: 'us-login'}, h('b', {'data-i18n': 'off', text: user.login}), user.me ? h('small', {text: 'это вы'}) : null),
      h('span', {'data-label': 'Роль', text: user.role_name}),
      h('span', {'data-label': 'Телефон', class: 'us-phone', text: user.phone || '—'}),
      h('span', {'data-label': 'Вход', text: signIn(user)}),
      h('span', {'data-label': 'Направления',
                 text: user.role === 'manager' ? (user.directions.length ? user.directions.join(', ') : 'Весь ресторан') : '—'}),
      h('span', {'data-label': 'Статус'}, statusPill(user)),
      h('span', {class: 'us-row-action'}, action));
  }
  function render() {
    const rows = $('us-rows');
    rows.setAttribute('aria-busy', 'false');
    rows.replaceChildren(...(view.users.length
      ? view.users.map(rowFor)
      : [h('div', {class: 'fd-empty', text: 'Учётных записей пока нет.'})]));
    const active = view.users.filter(user => user.active).length;
    $('us-count').textContent = view.users.length + ' · ' + tr('включено') + ' ' + active;
    $('us-env').hidden = !view.users.some(user => user.source === 'env');
    const select = $('us-role');
    if (!select.options.length) {
      for (const role of view.roles) select.append(h('option', {value: role.id, text: role.name}));
      $('us-direction-list').replaceChildren(...view.directions.map(name =>
        h('label', {class: 'us-check'}, h('input', {type: 'checkbox', value: name}), ' ' + name)));
    }
    if (!view.sms) $('us-password-hint').textContent = 'Без пароля человек входит только по номеру и коду из SMS. Вход по SMS на сервере сейчас выключен.';
  }

  /* ── Форма ─────────────────────────────────────────────────────────── */
  function syncDirections() {
    $('us-directions').hidden = $('us-role').value !== 'manager';
  }
  function syncPassword() {
    const removing = $('us-remove-password').checked;
    $('us-password').disabled = removing;
    if (removing) $('us-password').value = '';
  }
  function openDialog(user) {
    editing = user || null;
    formMessage('');
    $('us-form').reset();
    $('us-dialog-kicker').textContent = tr(user ? 'УЧЁТНАЯ ЗАПИСЬ' : 'НОВЫЙ ЧЕЛОВЕК');
    $('us-dialog-title').textContent = user ? user.login : tr('Добавить в панель');
    $('us-login').value = user ? user.login : '';
    $('us-login').readOnly = Boolean(user);
    $('us-role').value = user ? user.role : 'manager';
    $('us-phone').value = user && user.phone ? user.phone : '';
    $('us-password-label').textContent = tr(user && user.password ? 'Новый пароль' : 'Пароль');
    $('us-password').placeholder = tr(user && user.password ? 'пусто — не менять' : 'не меньше 8 знаков');
    $('us-remove-password-row').hidden = !(user && user.password);
    $('us-active-row').hidden = !user || user.me;
    $('us-active').checked = user ? user.active : true;
    $('us-delete').hidden = !user || user.me;
    // Себе роль администратора не снять — список ролей заблокирован.
    $('us-role').disabled = Boolean(user && user.me);
    for (const box of $('us-direction-list').querySelectorAll('input'))
      box.checked = Boolean(user && user.directions.includes(box.value));
    syncDirections(); syncPassword();
    $('us-dialog').showModal();
    (user ? $('us-phone') : $('us-login')).focus();
  }
  function closeDialog() { $('us-dialog').close(); editing = null; }

  function formBody() {
    const directions = [...$('us-direction-list').querySelectorAll('input:checked')].map(box => box.value);
    const body = {role: $('us-role').value, phone: $('us-phone').value.trim(), directions};
    const password = $('us-password').value;
    if (editing) {
      if ($('us-remove-password').checked) body.remove_password = true;
      else if (password) body.password = password;
      if (!editing.me) body.active = $('us-active').checked;
      if (editing.me) delete body.role;
    } else {
      body.login = $('us-login').value.trim();
      if (password) body.password = password;
    }
    return body;
  }

  async function save(event) {
    event.preventDefault();
    const body = formBody();
    if (!editing && !body.login) { formMessage(tr('Введите логин.')); return; }
    if (!body.phone && !body.password && !(editing && editing.password && !body.remove_password)) {
      formMessage(tr('Нужен пароль или номер телефона: иначе войти нечем.'));
      return;
    }
    const url = editing ? '/api/users/' + encodeURIComponent(editing.login) : '/api/users';
    const done = await act(() => request(url, editing ? 'PATCH' : 'POST', body),
                           editing ? 'Изменения сохранены.' : '«' + body.login + '» добавлена.', $('us-save'));
    if (done) closeDialog();
  }

  async function remove() {
    if (!editing) return;
    if (!confirm(tr('Удалить учётную запись') + ' «' + editing.login + '»? ' + tr('Войти по ней больше нельзя.'))) return;
    const login = editing.login;
    const done = await act(() => request('/api/users/' + encodeURIComponent(login), 'DELETE'),
                           '«' + login + '» удалена.', $('us-delete'));
    if (done) closeDialog();
  }

  async function importAll(event) {
    const pending = view.users.filter(user => user.source === 'env');
    await act(async () => {
      let result = view;
      for (const user of pending)
        result = await request('/api/users/' + encodeURIComponent(user.login) + '/import', 'POST');
      return result;
    }, 'Перенесено в базу: ' + pending.length + '.', event.currentTarget);
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('us-add').addEventListener('click', () => openDialog(null));
    $('us-close').addEventListener('click', closeDialog);
    $('us-cancel').addEventListener('click', closeDialog);
    $('us-form').addEventListener('submit', save);
    $('us-delete').addEventListener('click', remove);
    $('us-role').addEventListener('change', syncDirections);
    $('us-remove-password').addEventListener('change', syncPassword);
    $('us-import-all').addEventListener('click', importAll);
    load().catch(error => {
      $('us-rows').replaceChildren(h('div', {class: 'fd-empty', text: error.message}));
      message(error.message, true);
    });
  });
})();
