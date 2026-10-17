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

  const ICONS = {cashier: '▤', accountant: '◫', director: '◉', founder: '⌁', shokh: '◇', manager: '◎', users: '◍'};

  globalThis.RetroConfig = fetch('/api/config', {headers: {accept: 'application/json'}})
    .then(response => {
      if (!response.ok) throw new Error('Не удалось определить настройки сервера.');
      return response.json();
    });

  if (!nav) return;

  /* Меню сворачивается в полосу значков, и рабочей области достаётся вся
     ширина (T-442). Кнопка — круглая стрелка на правом краю меню (T-449):
     «‹» свернуть, «›» развернуть. Выбор помнит браузер; на планшете и
     телефоне меню и так узкое, кнопку там прячет style.css. */
  const sidebar = document.querySelector('.sidebar[data-collapsible]');
  if (sidebar) {
    const root = document.documentElement;
    sidebar.id = sidebar.id || 'sidebar';
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'sidebar-toggle';
    toggle.setAttribute('aria-controls', sidebar.id);
    toggle.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path class="sidebar-toggle-arrow" d="M14 7l-5 5 5 5"/></svg>';
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
    sidebar.append(toggle);
  }

  /* Меню справа (T-502): переключатель внизу меню, в строке языка. Положение
     ставит sidebar-state.js до первого кадра; здесь только кнопка. Подпись
     одна и та же, состояние — в aria-pressed. На телефоне кнопку прячет
     style.css: там меню — полоса сверху. */
  const account = sidebar && sidebar.querySelector('.side-account');
  const side = globalThis.RetroSidebar;
  if (account && side) {
    const place = document.createElement('button');
    place.type = 'button';
    place.className = 'side-place';
    place.setAttribute('aria-label', 'Меню справа');
    place.title = 'Меню справа';
    place.innerHTML = '<svg class="ri" viewBox="0 0 24 24" aria-hidden="true">'
      + '<path class="side-place-fill" d="M14.5 4.5H18a2.5 2.5 0 0 1 2.5 2.5v10a2.5 2.5 0 0 1-2.5 2.5h-3.5z"/>'
      + '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M14.5 4.5v15"/></svg>';
    const syncPlace = () => place.setAttribute('aria-pressed', String(side.current() === 'right'));
    place.addEventListener('click', () => {
      side.choose(side.current() === 'right' ? 'left' : 'right');
      syncPlace();
    });
    syncPlace();
    const lang = account.querySelector('.side-lang');
    if (lang) lang.after(place); else account.prepend(place);
  }

  /* Состояние данных внизу меню (T-455): в полосе значков видна только точка —
     текст уходит в подсказку. Кабинет учредителя показывает в меню копию
     строки из своей телефонной шапки (data-status-of). */
  if (sidebar) {
    const watch = (source, target) => {
      const copy = () => {
        if (target !== source) {
          target.textContent = source.textContent;
          target.className = source.className;
        }
        target.title = target.textContent.trim();
      };
      copy();
      new MutationObserver(copy).observe(source, {childList: true, characterData: true, subtree: true,
        attributes: true, attributeFilter: ['class']});
    };
    sidebar.querySelectorAll('.side-status .top-status').forEach(status => {
      const id = status.dataset.statusOf;
      const source = id ? document.getElementById(id) : status;
      if (source) watch(source, status);
    });
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
