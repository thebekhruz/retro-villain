/** Переключение языка панели: русский и узбекский.
 *
 *  Разметка не размечается ключами: сопоставление идёт по самой русской
 *  строке, как она написана на экране. Так перевод цепляет и то, что
 *  рисуют скрипты модулей — таблицы, статусы, пустые состояния, — не
 *  переписывая каждый из них.
 *
 *  Оригинал запоминается на узле, поэтому возврат на русский точный, а не
 *  обратным переводом. */
(() => {
  const STORAGE_KEY = 'retro:lang';
  const RU_ORIGINAL = Symbol('оригинал');
  const SKIP = new Set(['SCRIPT', 'STYLE', 'CODE', 'PRE']);
  const ATTRIBUTES = ['placeholder', 'aria-label', 'title'];

  function dictionary() {
    return globalThis.RetroDictionaryUz || {};
  }

  /* Часть подписей скрипты склеивают на ходу: «Касса за 20 сентября»,
     «Опоздавших после 10:00: 0 · автоматический штраф не начисляется.
     Hikvision не настроен…». Целиком такая строка в словаре лежать не может
     — между знакомыми кусками стоят дата, сумма или счётчик. Поэтому, когда
     совпадения по всей строке нет, идём по ней слева направо и переводим
     самые длинные знакомые куски, а данные между ними оставляем как есть.

     Кусок берём только целыми словами: иначе «мар» нашлось бы в
     «Маркетинге». Ключи короче трёх букв не трогаем — из двух букв состоят
     предлоги, и они цеплялись бы где попало. Три буквы нужны ради «сум»:
     единица стоит после суммы, то есть всегда отдельным словом. */
  const LETTER = /[\p{L}\p{N}]/u;
  const SHORTEST = 3;
  let buckets = null;
  let bucketsFor = null;

  function byFirstLetter() {
    const dict = dictionary();
    if (bucketsFor !== dict) {
      bucketsFor = dict;
      buckets = new Map();
      for (const key of Object.keys(dict)) {
        if (key.length < SHORTEST) continue;
        const bucket = buckets.get(key[0]) || [];
        bucket.push(key);
        buckets.set(key[0], bucket);
      }
      for (const bucket of buckets.values()) bucket.sort((a, b) => b.length - a.length);
    }
    return buckets;
  }

  function pieces(value) {
    const dict = dictionary();
    const index = byFirstLetter();
    let out = '';
    let at = 0;
    let changed = false;
    while (at < value.length) {
      const opening = at === 0 || !LETTER.test(value[at - 1]);
      if (opening) {
        const bucket = index.get(value[at]);
        const hit = bucket && bucket.find(key => value.startsWith(key, at)
          && !LETTER.test(value[at + key.length] || ' '));
        if (hit) {
          out += dict[hit];
          at += hit.length;
          changed = true;
          continue;
        }
      }
      out += value[at];
      at += 1;
    }
    return changed ? out : null;
  }

  /* Даты модули собирают в браузере: «Касса за 22 сентября 2026 г.».
     Словарём их не взять — строка каждый день новая, а просить у браузера
     узбекский формат нельзя: в Chrome для узбекского нет названий месяцев,
     и вместо «22-sentabr» выходит «2026 M09 22». Поэтому заменяем месяцы
     сами, а «г.» после года в узбекском не пишут. */
  const MONTHS = [
    ['января', 'yanvar'], ['февраля', 'fevral'], ['марта', 'mart'],
    ['апреля', 'aprel'], ['июня', 'iyun'], ['июля', 'iyul'],
    ['августа', 'avgust'], ['сентября', 'sentabr'], ['октября', 'oktabr'],
    ['ноября', 'noyabr'], ['декабря', 'dekabr'],
    ['янв.', 'yan'], ['февр.', 'fev'], ['мар.', 'mar'], ['апр.', 'apr'],
    ['июн.', 'iyun'], ['июл.', 'iyul'], ['авг.', 'avg'], ['сент.', 'sen'],
    ['окт.', 'okt'], ['нояб.', 'noy'], ['дек.', 'dek'],
    ['мая', 'may'],
    ['янв', 'yan'], ['февр', 'fev'], ['апр', 'apr'], ['июн', 'iyun'],
    ['июл', 'iyul'], ['авг', 'avg'], ['сент', 'sen'], ['окт', 'okt'],
    ['нояб', 'noy'], ['дек', 'dek'],
  ].map(([ru, uz]) => [
    // Без границ слова «мар» нашлось бы в «марте» и в «Маркетинге».
    new RegExp('(?<![А-Яа-яЁё])' + ru.replace('.', '\\.') + '(?![А-Яа-яЁё])', 'g'),
    uz,
  ]);

  function dates(value) {
    if (!/\d/.test(value)) return null;
    let next = value;
    for (const [pattern, uz] of MONTHS) next = next.replace(pattern, uz);
    next = next.replace(/\s*г\./g, '');
    return next === value ? null : next;
  }

  /* Строки с числом внутри, у которых в узбекском другой порядок слов:
     «39% выручки Retro» → «Retro tushumining 39%». Кусками их не собрать,
     поэтому словарь кладёт рядом шаблоны: регулярное выражение на всю
     строку и замену с $1, $2… */
  function template(key) {
    for (const [pattern, replacement] of globalThis.RetroTemplatesUz || []) {
      if (pattern.test(key)) return key.replace(pattern, replacement);
    }
    return null;
  }

  /* Дни недели, месяцы без числа и единицы после чисел. Их рисуют скрипты
     вокруг данных («Пн», «сб, 26 сентября», «5,9 млн», «31 из ≈147 чеков»),
     и словарь их не видит: ключ короче трёх букв или склеен с цифрой. */
  const RU_BEFORE = '(?<![А-Яа-яЁё])';
  const RU_AFTER = '(?![А-Яа-яЁё])';
  const word = (ru, flags = 'g') => new RegExp(RU_BEFORE + ru.replace(/\./g, '\\.') + RU_AFTER, flags);
  const WORDS = [
    ['понедельник', 'dushanba'], ['вторник', 'seshanba'], ['среда', 'chorshanba'], ['четверг', 'payshanba'],
    ['пятница', 'juma'], ['суббота', 'shanba'], ['воскресенье', 'yakshanba'],
    ['Понедельник', 'Dushanba'], ['Вторник', 'Seshanba'], ['Среда', 'Chorshanba'], ['Четверг', 'Payshanba'],
    ['Пятница', 'Juma'], ['Суббота', 'Shanba'], ['Воскресенье', 'Yakshanba'],
    ['в понедельник', 'dushanba kuni'], ['во вторник', 'seshanba kuni'], ['в среду', 'chorshanba kuni'],
    ['в четверг', 'payshanba kuni'], ['в пятницу', 'juma kuni'], ['в субботу', 'shanba kuni'],
    ['в воскресенье', 'yakshanba kuni'],
    ['кг', 'kg'], ['шт', 'dona'],
    ['Пн', 'Du'], ['Вт', 'Se'], ['Ср', 'Ch'], ['Чт', 'Pa'], ['Пт', 'Ju'], ['Сб', 'Sh'], ['Вс', 'Ya'],
    ['пн', 'du'], ['вт', 'se'], ['ср', 'ch'], ['чт', 'pa'], ['пт', 'ju'], ['сб', 'sh'], ['вс', 'ya'],
    ['январь', 'yanvar'], ['февраль', 'fevral'], ['март', 'mart'], ['апрель', 'aprel'], ['май', 'may'],
    ['июнь', 'iyun'], ['июль', 'iyul'], ['август', 'avgust'], ['сентябрь', 'sentabr'], ['октябрь', 'oktabr'],
    ['ноябрь', 'noyabr'], ['декабрь', 'dekabr'],
    ['ЯНВАРЬ', 'YANVAR'], ['ФЕВРАЛЬ', 'FEVRAL'], ['МАРТ', 'MART'], ['АПРЕЛЬ', 'APREL'], ['МАЙ', 'MAY'],
    ['ИЮНЬ', 'IYUN'], ['ИЮЛЬ', 'IYUL'], ['АВГУСТ', 'AVGUST'], ['СЕНТЯБРЬ', 'SENTABR'], ['ОКТЯБРЬ', 'OKTABR'],
    ['НОЯБРЬ', 'NOYABR'], ['ДЕКАБРЬ', 'DEKABR'],
  ].sort((a, b) => b[0].length - a[0].length).map(([ru, uz]) => [word(ru), uz]);
  const UNITS = [
    [/(\d)\s*млрд(?![А-Яа-яЁё])/g, '$1 mlrd'],
    [/(\d)\s*млн(?![А-Яа-яЁё])/g, '$1 mln'],
    [/(\d)\s*тыс\.?(?![А-Яа-яЁё])/g, '$1 ming'],
    [/(\d)\s*шт(?![А-Яа-яЁё])/g, '$1 dona'],
    [/(\d)\s*кг(?![А-Яа-яЁё])/g, '$1 kg'],
    [/(\d)\s*(чеков|чека|чек)(?![А-Яа-яЁё])/g, '$1 chek'],
    [/(\d)\s*(позиций|позиции|позиция)(?![А-Яа-яЁё])/g, '$1 pozitsiya'],
    [/(\d)\s*(дней|дня|день)(?![А-Яа-яЁё])/g, '$1 kun'],
    [/(\d)\s*(недель|недели|неделя)(?![А-Яа-яЁё])/g, '$1 hafta'],
    [/(\d)\s*(сотрудников|сотрудника|сотрудник)(?![А-Яа-яЁё])/g, '$1 xodim'],
    [/(\d)\s+из\s+(≈?\d)/g, '$1 / $2'],
    [/(^|[\s(])маржа(?![А-Яа-яЁё])/g, '$1marja'],
  ];

  function words(value) {
    let next = value;
    for (const [pattern, uz] of UNITS) next = next.replace(pattern, uz);
    for (const [pattern, uz] of WORDS) next = next.replace(pattern, uz);
    return next === value ? null : next;
  }

  function translate(value) {
    const key = value.trim();
    if (!key) return null;
    const whole = dictionary()[key] || template(key) || pieces(key);
    // Пробелы вокруг строки сохраняем: в разметке они держат отступы.
    const next = whole ? value.replace(key, whole) : value;
    const dated = dates(next);
    const polished = words(dated || next);
    return polished || dated || (whole ? next : null);
  }

  /* Ответы AI и прочий текст, который пишет сервер, переводчик не трогает:
     по кускам из него выходила смесь двух языков. Такие узлы помечены
     data-i18n="off". */
  const OFF = '[data-i18n="off"]';

  function applyToTextNode(node, toUzbek) {
    if (!node.nodeValue || !node.nodeValue.trim()) return;
    if (node.parentElement && SKIP.has(node.parentElement.tagName)) return;
    if (node.parentElement && node.parentElement.closest(OFF)) return;
    if (toUzbek) {
      if (node[RU_ORIGINAL] === undefined) {
        const next = translate(node.nodeValue);
        if (next === null) return;
        node[RU_ORIGINAL] = node.nodeValue;
        node.nodeValue = next;
      }
      return;
    }
    if (node[RU_ORIGINAL] !== undefined) {
      node.nodeValue = node[RU_ORIGINAL];
      node[RU_ORIGINAL] = undefined;
    }
  }

  function applyToAttributes(element, toUzbek) {
    if (element.closest(OFF)) return;
    for (const name of ATTRIBUTES) {
      if (!element.hasAttribute(name)) continue;
      // Ключи dataset не терпят дефисов: 'aria-label' ронял весь проход,
      // и страница оставалась русской начиная с первого такого элемента.
      const mark = 'ru' + name.replace(/[^a-z]/gi, '');
      if (toUzbek) {
        if (element.dataset[mark] !== undefined) continue;
        const next = translate(element.getAttribute(name));
        if (next === null) continue;
        element.dataset[mark] = element.getAttribute(name);
        element.setAttribute(name, next);
      } else if (element.dataset[mark] !== undefined) {
        element.setAttribute(name, element.dataset[mark]);
        delete element.dataset[mark];
      }
    }
  }

  // Модуль сменил подсказку уже переведённого элемента («Передано … при
  // расчёте …»): старый русский оригинал больше не верен. Наша собственная
  // запись перевода тоже приходит сюда — её узнаём по совпадению и пропускаем.
  function retranslateAttribute(element, name) {
    if (!(element instanceof Element) || element.closest(OFF)) return;
    const mark = 'ru' + name.replace(/[^a-z]/gi, '');
    const value = element.getAttribute(name);
    if (element.dataset[mark] !== undefined) {
      if (value === (translate(element.dataset[mark]) ?? element.dataset[mark])) return;
      delete element.dataset[mark];
    }
    if (value !== null) applyToAttributes(element, true);
  }

  function walk(root, toUzbek) {
    if (root.nodeType === Node.TEXT_NODE) {
      applyToTextNode(root, toUzbek);
      return;
    }
    if (root.nodeType !== Node.ELEMENT_NODE) return;
    if (SKIP.has(root.tagName)) return;
    applyToAttributes(root, toUzbek);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const texts = [];
    while (walker.nextNode()) texts.push(walker.currentNode);
    texts.forEach(node => applyToTextNode(node, toUzbek));
    root.querySelectorAll('[placeholder],[aria-label],[title]')
      .forEach(element => applyToAttributes(element, toUzbek));
  }

  let current = 'ru';

  let titleRu = null;

  function apply(language) {
    current = language === 'uz' ? 'uz' : 'ru';
    document.documentElement.lang = current;
    // Заголовок вкладки тоже подпись: «Финансы дня · Retro Milliy».
    if (titleRu === null) titleRu = document.title;
    document.title = current === 'uz' ? (translate(titleRu) || titleRu) : titleRu;
    walk(document.body, current === 'uz');
    document.querySelectorAll('[data-lang]').forEach(button => {
      const active = button.dataset.lang === current;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
    });
  }

  function choose(language) {
    try { localStorage.setItem(STORAGE_KEY, language); } catch { /* приватный режим */ }
    apply(language);
  }

  function saved() {
    try { return localStorage.getItem(STORAGE_KEY) || 'ru'; } catch { return 'ru'; }
  }

  function start() {
    document.querySelectorAll('[data-lang]').forEach(button => {
      button.addEventListener('click', () => choose(button.dataset.lang));
    });
    apply(saved());
    // Модули дорисовывают таблицы и статусы после ответа сервера: новые
    // узлы переводим по мере появления, иначе половина экрана остаётся
    // русской.
    new MutationObserver(records => {
      if (current !== 'uz') return;
      for (const record of records) {
        record.addedNodes.forEach(node => walk(node, true));
        if (record.type === 'characterData') applyToTextNode(record.target, true);
        if (record.type === 'attributes') retranslateAttribute(record.target, record.attributeName);
      }
    }).observe(document.body, {
      childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ATTRIBUTES,
    });
  }

  globalThis.RetroI18n = { apply, choose, saved, translate };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
