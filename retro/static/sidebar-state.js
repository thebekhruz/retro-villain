/* Свёрнутое меню слева (T-442). Читается до первого кадра (скрипт без defer):
   иначе страница сперва рисуется с широким меню и тут же «прыгает». Кнопку
   рисует nav.js — только на страницах с <aside class="sidebar" data-collapsible>. */
try {
  if (localStorage.getItem('retro:sidebar') === 'collapsed') document.documentElement.classList.add('sidebar-collapsed');
} catch {}
