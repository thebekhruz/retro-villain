/* Профиль телефонных экранов (T-461, T-463): кружок с буквой в шапке открывает
   лист — кто вошёл, язык, ссылки страницы и «Выйти». Общий для менеджера,
   телефона директора и кабинета учредителя. Имя и роль — из /api/config
   (меню nav.js уже запросило их); страница может уточнить подпись сама. */
(() => {
  const open = document.getElementById('profile-open');
  const sheet = document.getElementById('profile-sheet');
  if (!open || !sheet) return;
  const ROLES = {manager: 'Менеджер', director: 'Директор', founder: 'Учредитель', accountant: 'Бухгалтер',
    cashier: 'Кассир', shokh: 'Закуп', admin: 'Администратор', all: 'Администратор'};

  function show() {
    if (typeof sheet.showModal === 'function') { if (!sheet.open) sheet.showModal(); } else sheet.setAttribute('open', '');
  }
  function hide() {
    if (typeof sheet.close === 'function') sheet.close(); else sheet.removeAttribute('open');
  }
  open.addEventListener('click', show);
  document.getElementById('profile-close')?.addEventListener('click', hide);
  // Нажатие на затемнение вокруг листа закрывает его (по самому листу — нет).
  sheet.addEventListener('click', event => {
    if (event.target !== sheet) return;
    const box = sheet.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) hide();
  });

  const config = globalThis.RetroConfig
    || fetch('/api/config', {headers: {accept: 'application/json'}}).then(response => (response.ok ? response.json() : null));
  Promise.resolve(config).then(data => {
    if (!data) return;
    // Разделы по флагам сервера (T-522): `[data-feature="kpi"]` спрятан в
    // разметке и показывается, только когда модуль включён в /api/config.
    document.querySelectorAll('[data-feature]').forEach(element => {
      element.hidden = data[element.dataset.feature] === false;
    });
    if (data.user === 'local') return;
    const name = String(data.user_name || data.user || '').trim();
    const mark = (Array.from(name)[0] || '').toUpperCase();
    const fill = (selector, text) => document.querySelectorAll(selector).forEach(node => {
      if (!node.textContent.trim()) node.textContent = text;
    });
    fill('[data-profile-mark]', mark);
    fill('[data-profile-name]', name);
    fill('[data-profile-role]', ROLES[data.role] || '');
  }).catch(() => {});

  globalThis.RetroProfile = {show, hide};
})();
