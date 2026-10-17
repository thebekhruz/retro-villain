/* Свёрнутое меню слева (T-442). Читается до первого кадра (скрипт без defer):
   иначе страница сперва рисуется с широким меню и тут же «прыгает». Кнопку
   рисует nav.js — только на страницах с <aside class="sidebar" data-collapsible>. */
try {
  if (localStorage.getItem('retro:sidebar') === 'collapsed') document.documentElement.classList.add('sidebar-collapsed');
} catch {}

/* Меню справа (T-502, ТЗ 16.10 п. 3.3). Заказчик сравнивает оба положения,
   поэтому по умолчанию меню остаётся слева, а справа его ставит переключатель
   внизу меню (nav.js). Выбор помнит браузер этого устройства. Класс на <html>
   ставится здесь же, до первого кадра, — меню не перепрыгивает слева направо
   при каждой загрузке. На телефоне (≤650) меню — полоса сверху, и класс
   ни на что не влияет (style.css). */
globalThis.RetroSidebar = (() => {
  const KEY = 'retro:sidebar-side';
  const CLASS = 'sidebar-right';
  const root = () => document.documentElement;

  // Приватный режим и запрет данных сайта роняют сам доступ к хранилищу.
  function saved() {
    try { return globalThis.localStorage.getItem(KEY) === 'right' ? 'right' : 'left'; } catch { return 'left'; }
  }
  function current() {
    return root().classList.contains(CLASS) ? 'right' : 'left';
  }
  function apply(side) {
    const next = side === 'right' ? 'right' : 'left';
    root().classList.toggle(CLASS, next === 'right');
    return next;
  }
  // Не запомнили — меню всё равно переезжает, только до следующей загрузки.
  function choose(side) {
    const next = apply(side);
    try { globalThis.localStorage.setItem(KEY, next); } catch {}
    return next;
  }

  apply(saved());
  return {KEY, CLASS, saved, current, apply, choose};
})();
