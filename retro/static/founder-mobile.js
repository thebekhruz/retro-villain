/* Общее для телефона на обеих страницах учредителя (макет «2а», ноябрь 2026).
 *
 * Нет связи с iiko (`configured: false` в /api/config): вверху одна плашка
 * «Нет связи с iiko…», а повторы технической фразы «Подключение iiko ещё не
 * настроено. Нужен файл build/.env» в блоках на телефоне скрыты. Во вкладках
 * отчёта дня вместо неё — короткое «Нет данных iiko». На компьютере фраза
 * остаётся как есть: этот файл там ничего не делает. Только показ. */
(() => {
  const phone = matchMedia('(max-width: 900px)');
  const plaque = document.getElementById('fo-iiko-off');
  const TECH = 'build/.env';
  let off = false;

  /** Узлы с технической фразой: прячем; в отчёте дня — коротко, чтобы вкладка не была пустой. */
  function quiet(root) {
    if (!off || !phone.matches) return;
    root.querySelectorAll('p, small, .fo-empty, .fo-hint, #fo-acc-line').forEach(node => {
      if (!node.textContent.includes(TECH) || node.dataset.iikoQuiet) return;
      node.dataset.iikoQuiet = '1';
      if (node.closest('#fo-rep-body')) node.textContent = 'Нет данных iiko';
      else if (node.id === 'fo-acc-line') node.textContent = 'Замечания: нет данных iiko';
      else node.classList.add('fo-iiko-repeat');
    });
  }

  Promise.resolve(globalThis.RetroConfig).then(config => {
    if (!config || config.configured !== false || !plaque) return;
    off = true;
    plaque.hidden = false;
    document.body.classList.add('fo-no-iiko');
    const main = document.querySelector('main') || document.body;
    quiet(main);
    new MutationObserver(() => quiet(main)).observe(main, {childList: true, subtree: true, characterData: true});
  }).catch(() => {});
})();
