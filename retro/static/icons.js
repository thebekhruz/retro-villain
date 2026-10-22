/** Векторные иконки панели (T-448).
 *
 *  Раньше иконками служили символы шрифта: ▤ ◫ ◉ ⌁ ◇ ◎ ◍ ⌖ ↻ ✎ × ‹ › ⌄.
 *  На каждой системе они рисовались своим шрифтом — разной толщины, высоты
 *  и наклона. Теперь у всех иконок один рисунок: контур 1,75 на сетке 24,
 *  скруглённые концы, цвет — от текста (currentColor), размер — 1em.
 *
 *  Символы в разметке и в скриптах модулей остались как есть: этот файл
 *  находит элемент, в котором нет ничего, кроме такого символа, и заменяет
 *  его на SVG. Так подхватываются и строки, которые модули дорисовывают
 *  позже (×, ✎, ‹ ›). Символ внутри фразы («100 USD × 11 600», «A → B»)
 *  не трогаем: иконкой считается только элемент-значок — кнопка, элемент с
 *  aria-hidden или значок меню. Явно задать иконку можно атрибутом data-icon. */
(() => {
  const NS = 'http://www.w3.org/2000/svg';
  // Рисунки — простые фигуры на сетке 24×24, без заливки.
  const PATHS = {
    x: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    'chevron-left': '<path d="M14.5 6l-6 6 6 6"/>',
    'chevron-right': '<path d="M9.5 6l6 6-6 6"/>',
    'chevron-down': '<path d="M6 9.5l6 6 6-6"/>',
    plus: '<path d="M12 5.5v13M5.5 12h13"/>',
    minus: '<path d="M5.5 12h13"/>',
    check: '<path d="M5.5 12.5l4.2 4.2L18.5 8"/>',
    pencil: '<path d="M14.6 5.6l3.8 3.8M4.5 19.5l1-4.6L15.9 4.5a1.9 1.9 0 0 1 2.7 0l.9.9a1.9 1.9 0 0 1 0 2.7L9.1 18.5z"/>',
    refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.3-5.4"/><path d="M19.5 4.5V9H15"/>',
    download: '<path d="M12 4.5v10.5M7.5 11l4.5 4.5 4.5-4.5M5 19.5h14"/>',
    'arrow-right': '<path d="M5 12h14M13.5 6.5L19 12l-5.5 5.5"/>',
    external: '<path d="M8 16L16.5 7.5M9.5 7.5h7v7"/>',
    alert: '<path d="M10.3 4.9a2 2 0 0 1 3.4 0l7 12.1A2 2 0 0 1 19 20H5a2 2 0 0 1-1.7-3z"/><path d="M12 10v3.6M12 16.6h.01"/>',
    pin: '<path d="M12 21s-6.5-5.7-6.5-11a6.5 6.5 0 0 1 13 0c0 5.3-6.5 11-6.5 11z"/><circle cx="12" cy="10" r="2.3"/>',
    // Модули: касса — купюра, бухгалтер — калькулятор, директор — график,
    // учредитель — портфель, закуп — сумка, менеджер — камера, пользователи — люди.
    cashier: '<rect x="3" y="6.5" width="18" height="11" rx="2"/><circle cx="12" cy="12" r="2.4"/><path d="M6.5 12h.01M17.5 12h.01"/>',
    accountant: '<rect x="5" y="3" width="14" height="18" rx="2.2"/><path d="M8.5 7.5h7M8.5 11.5h.01M12 11.5h.01M15.5 11.5h.01M8.5 15h.01M12 15h.01M15.5 15h.01"/>',
    director: '<path d="M4 19.5h16M7.5 16v-3.5M12 16V8M16.5 16v-6"/>',
    founder: '<rect x="3.5" y="7.5" width="17" height="12" rx="2"/><path d="M9 7.5V6a1.5 1.5 0 0 1 1.5-1.5h3A1.5 1.5 0 0 1 15 6v1.5M3.5 12.5h17"/>',
    shokh: '<path d="M5.8 8.5h12.4l-1 11H6.8z"/><path d="M9 8.5V7a3 3 0 0 1 6 0v1.5"/>',
    manager: '<path d="M4 8.8A1.6 1.6 0 0 1 5.6 7.2H8l1.5-2.2h5L16 7.2h2.4A1.6 1.6 0 0 1 20 8.8v8.6a1.6 1.6 0 0 1-1.6 1.6H5.6A1.6 1.6 0 0 1 4 17.4z"/><circle cx="12" cy="12.8" r="3.1"/>',
    users: '<circle cx="9.5" cy="8.5" r="3.2"/><path d="M3.5 19.5a6 6 0 0 1 12 0M15.5 5.6a3.1 3.1 0 0 1 0 5.8M17.5 14.4a5.6 5.6 0 0 1 3 5.1"/>',
    analytics: '<path d="M4 19.5h16"/><path d="M5 15.5l4.5-5 3.5 3 6-7"/>',
  };
  // Символ-иконка → рисунок. Значки модулей узнаём по символу из разметки и nav.js.
  const GLYPHS = {
    '×': 'x', '✕': 'x', '‹': 'chevron-left', '›': 'chevron-right', '⌄': 'chevron-down',
    '✎': 'pencil', '↻': 'refresh', '+': 'plus', '−': 'minus', '✓': 'check', '↓': 'download',
    '→': 'arrow-right', '↗': 'external', '⌖': 'pin', '⚠': 'alert',
    '▤': 'cashier', '◫': 'accountant', '◉': 'director', '⌁': 'founder', '◇': 'shokh', '◎': 'manager', '◍': 'users',
    '◬': 'analytics',
  };

  function svg(name) {
    const node = document.createElementNS(NS, 'svg');
    node.setAttribute('viewBox', '0 0 24 24');
    node.setAttribute('aria-hidden', 'true');
    node.setAttribute('focusable', 'false');
    node.setAttribute('class', 'ri ri-' + name);
    // Рисунки — постоянные строки этого файла, данных пользователя в них нет.
    node.innerHTML = PATHS[name];
    return node;
  }

  /* Элемент-значок: внутри только символ-иконка, и сам элемент — кнопка,
     декоративный (aria-hidden) или значок меню. */
  function glyphOf(el) {
    if (el.dataset.icon && PATHS[el.dataset.icon]) return el.dataset.icon;
    if (el.childNodes.length !== 1 || el.firstChild.nodeType !== Node.TEXT_NODE) return null;
    const name = GLYPHS[el.firstChild.nodeValue.trim()];
    if (!name) return null;
    // .fo-mark и ячейки «Проверка передачи» у учредителя — значки статуса (✓, ⚠).
    const iconish = el.matches('button, .nav-icon, .location-icon, .fo-mark, .fo-row.is-check > span')
      || el.getAttribute('aria-hidden') === 'true' || el.closest('button, summary');
    return iconish ? name : null;
  }

  function upgrade(root) {
    if (!(root instanceof Element) || root.closest('svg, [data-icon-off], .brand-mark')) return;
    for (const el of [root, ...root.querySelectorAll('*')]) {
      if (el instanceof SVGElement || el.closest('[data-icon-off], .brand-mark')) continue;
      const name = glyphOf(el);
      if (!name || el.querySelector(':scope > svg.ri')) continue;
      el.replaceChildren(svg(name));
      el.classList.add('has-ri');
    }
  }

  globalThis.RetroIcons = {svg, upgrade, names: Object.keys(PATHS)};

  const start = () => {
    upgrade(document.body);
    // Модули дорисовывают строки и кнопки на ходу — подхватываем и их.
    new MutationObserver(records => {
      for (const record of records) {
        if (record.type === 'characterData') {
          if (record.target.parentElement) upgrade(record.target.parentElement);
          continue;
        }
        for (const node of record.addedNodes) {
          if (node.nodeType === Node.ELEMENT_NODE) upgrade(node);
          else if (node.nodeType === Node.TEXT_NODE && node.parentElement) upgrade(node.parentElement);
        }
      }
    }).observe(document.body, {childList: true, subtree: true, characterData: true});
  };
  if (document.body) start();
  else document.addEventListener('DOMContentLoaded', start, {once: true});
})();
