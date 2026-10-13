(function (root) {
  async function logout(request = root.fetch.bind(root), navigate = url => root.location.assign(url)) {
    const response = await request('/api/session/logout', {method: 'POST'});
    if (!response.ok) throw new Error('Не удалось завершить сессию.');
    navigate('/login');
  }

  function bind() {
    const button = root.document?.getElementById('logout');
    if (!button) return;
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        await logout();
      } catch (error) {
        button.disabled = false;
        button.textContent = 'Повторить выход';
        button.title = error.message;
      }
    });
  }

  /* Кто вошёл (T-446): имя из учётной записи рядом с «Выйти»; без имени —
     логин. Меню (nav.js) уже запросило настройки — берём его ответ; в
     кабинете менеджера меню нет, там запрашиваем сами. */
  async function showUser() {
    const button = root.document?.getElementById('logout');
    if (!button || button.parentElement?.querySelector('.top-user')) return;
    let config = null;
    try {
      config = await (root.RetroConfig || root.fetch('/api/config', {headers: {accept: 'application/json'}})
        .then(response => (response.ok ? response.json() : null)));
    } catch { return; }
    const name = config?.user_name;
    if (!name || config.user === 'local') return;
    const chip = root.document.createElement('span');
    chip.className = 'top-user';
    chip.dataset.i18n = 'off';
    chip.textContent = name;
    if (config.user && config.user !== name) chip.title = config.user;
    button.before(chip);
  }

  function start() {
    bind();
    showUser();
  }

  root.RetroLogout = {logout, bind, showUser};
  if (root.document?.readyState === 'loading') {
    root.document.addEventListener('DOMContentLoaded', start, {once: true});
  } else {
    start();
  }
})(globalThis);
