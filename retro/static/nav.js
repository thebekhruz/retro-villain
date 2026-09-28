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

  const ICONS = {cashier: '▤', accountant: '◫', director: '◉', founder: '⌁', shokh: '◇'};

  globalThis.RetroConfig = fetch('/api/config', {headers: {accept: 'application/json'}})
    .then(response => {
      if (!response.ok) throw new Error('Не удалось определить настройки сервера.');
      return response.json();
    });

  if (!nav) return;

  function link(module) {
    const anchor = document.createElement('a');
    anchor.href = module.path;
    anchor.className = 'nav-link';
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
