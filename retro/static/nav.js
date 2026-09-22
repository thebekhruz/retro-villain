/** Меню модулей: закрытый модуль не должен открываться.
 *
 *  Раньше по нему можно было нажать, и вместо панели человек получал голый
 *  ответ сервера с отказом — страница без шапки, без выхода и без пути
 *  назад. Право входа знает только сервер, поэтому спрашиваем его и гасим
 *  пункты, в которые не пустят: ссылка перестаёт быть ссылкой, под пальцем
 *  появляется замок, а по нажатию рядом объясняется причина. */
(() => {
  const nav = document.querySelector('.sidebar nav');
  if (!nav) return;

  function lock(link, reason) {
    link.classList.add('is-locked');
    link.setAttribute('aria-disabled', 'true');
    link.removeAttribute('href');
    link.title = reason;
    const mark = document.createElement('span');
    mark.className = 'nav-lock';
    mark.setAttribute('aria-hidden', 'true');
    mark.textContent = '⃠';
    link.append(mark);
    link.addEventListener('click', event => {
      event.preventDefault();
      say(reason);
    });
  }

  let timer = 0;
  function say(reason) {
    let hint = document.getElementById('nav-lock-hint');
    if (!hint) {
      hint = document.createElement('p');
      hint.id = 'nav-lock-hint';
      hint.className = 'nav-lock-hint';
      hint.setAttribute('role', 'status');
      nav.insertAdjacentElement('afterend', hint);
    }
    hint.textContent = reason;
    hint.classList.add('is-shown');
    clearTimeout(timer);
    timer = setTimeout(() => hint.classList.remove('is-shown'), 4000);
  }

  fetch('/api/config', { headers: { accept: 'application/json' } })
    .then(response => (response.ok ? response.json() : null))
    .then(config => {
      if (!config || !Array.isArray(config.modules)) return;
      const byPath = new Map(config.modules.map(module => [module.path, module]));
      nav.querySelectorAll('a[href]').forEach(link => {
        const module = byPath.get(new URL(link.href, location.origin).pathname);
        if (!module || module.available) return;
        lock(link, module.reason || 'Нет доступа');
      });
    })
    .catch(() => {});
})();
