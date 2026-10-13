/** Меню модулей: только то, что открыто этой учётной записи.
 *
 *  Раньше в разметке каждой страницы были перечислены все модули, а сервер
 *  помечал чужие закрытыми — пункт гас и получал замок. Кассир всё равно
 *  видел, какие окна есть у бухгалтера, директора и учредителя. Теперь в
 *  разметке только свой модуль (с подменю), а остальные пункты, если они
 *  открыты — у администратора, — дорисовываются по ответу сервера. Чужих
 *  модулей сервер не отдаёт вовсе. */
(() => {
  const nav = document.querySelector('.sidebar nav');

  const ICONS = {cashier: '▤', accountant: '◫', director: '◉', founder: '⌁', shokh: '◇', manager: '◎'};

  globalThis.RetroConfig = fetch('/api/config', {headers: {accept: 'application/json'}})
    .then(response => {
      if (!response.ok) throw new Error('Не удалось определить настройки сервера.');
      return response.json();
    });

  if (!nav) return;

  /* Меню сворачивается в полосу значков, и рабочей области достаётся вся
     ширина (T-442). Выбор помнит браузер; на планшете и телефоне меню и так
     узкое, кнопку там прячет style.css. */
  const sidebar = document.querySelector('.sidebar[data-collapsible]');
  const topbar = document.querySelector('.topbar');
  if (sidebar && topbar) {
    const root = document.documentElement;
    sidebar.id = sidebar.id || 'sidebar';
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'sidebar-toggle';
    toggle.setAttribute('aria-controls', sidebar.id);
    toggle.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/>'
      + '<path d="M9.5 4.5v15"/><path class="sidebar-toggle-arrow" d="M15.5 9.5 13 12l2.5 2.5"/></svg>';
    const sync = () => {
      const collapsed = root.classList.contains('sidebar-collapsed');
      const text = collapsed ? 'Развернуть меню' : 'Свернуть меню';
      toggle.setAttribute('aria-expanded', String(!collapsed));
      toggle.setAttribute('aria-label', text);
      toggle.title = text;
    };
    // Ширина анимируется только по нажатию: при загрузке и при изменении
    // размера окна меню и рабочая область встают на место сразу.
    let settle = 0;
    toggle.addEventListener('click', () => {
      root.classList.add('sidebar-animate');
      clearTimeout(settle);
      settle = setTimeout(() => root.classList.remove('sidebar-animate'), 320);
      const collapsed = root.classList.toggle('sidebar-collapsed');
      try { localStorage.setItem('retro:sidebar', collapsed ? 'collapsed' : 'open'); } catch {}
      sync();
    });
    sync();
    topbar.prepend(toggle);
  }

  function link(module) {
    const anchor = document.createElement('a');
    anchor.href = module.path;
    anchor.className = 'nav-link';
    // В свёрнутом меню подписи не видно — название остаётся в подсказке.
    if (sidebar) anchor.title = module.name;
    const icon = document.createElement('span');
    icon.className = 'nav-icon';
    icon.textContent = ICONS[module.id] || '·';
    anchor.append(icon, ' ' + module.name);
    return anchor;
  }

  globalThis.RetroConfig
    .then(config => {
      if (!config || !Array.isArray(config.modules)) return;
      // Свой модуль уже в разметке — вместе с подменю он остаётся как есть,
      // чтобы меню не перерисовывалось и не прыгало при загрузке.
      const active = nav.querySelector('.nav-active');
      const activePath = active ? new URL(active.href, location.origin).pathname : null;
      const subnav = nav.querySelector('.rm-subnav');
      const items = [];
      for (const module of config.modules) {
        if (module.path === activePath) {
          items.push(active);
          if (subnav) items.push(subnav);
        } else {
          items.push(link(module));
        }
      }
      if (active && !items.includes(active)) items.unshift(active, ...(subnav ? [subnav] : []));
      nav.replaceChildren(...items);
    })
    .catch(() => {});
})();
