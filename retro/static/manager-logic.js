/* Кабинет менеджера без DOM: проверка кириллицы на экране, статус Hikvision
   словами и куда отнести ошибку сервера. Правила кириллицы — те же, что на
   сервере (retro/modules/accountant/names.py): экран подсказывает сразу, а
   сервер всё равно проверяет сам. Тесты — tests/js/manager-logic.test.mjs. */
(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ManagerLogic = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {

  const CYRILLIC = 'абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳ';
  const isCyrillic = ch => ch.trim() !== '' && CYRILLIC.includes(ch.toLowerCase());
  const isLatin = ch => /^[A-Za-z]$/.test(ch);
  // Латинские буквы, которые на экране не отличить от кириллических.
  const HOMOGLYPHS = {a: 'а', e: 'е', o: 'о', p: 'р', c: 'с', x: 'х', y: 'у', A: 'А', B: 'В', E: 'Е',
    K: 'К', M: 'М', H: 'Н', O: 'О', P: 'Р', C: 'С', T: 'Т', X: 'Х'};
  const FIELDS = {
    name: {nominative: 'Имя', typed: 'набрано', prepositional: 'имени', extra: ' -', extraText: 'пробел и дефис',
      limit: 160, empty: 'Укажите имя сотрудника.'},
    role: {nominative: 'Должность', typed: 'набрана', prepositional: 'должности', extra: ' -.,()/№0123456789',
      extraText: 'цифры, пробел, дефис и скобки', limit: 80, empty: 'Укажите должность.'},
  };

  const collapse = value => String(value == null ? '' : value).split(/\s+/).filter(Boolean).join(' ');

  /* {value} — можно сохранять (пробелы уже нормализованы), {error} — что не так. */
  function checkText(raw, field = 'name') {
    const rule = FIELDS[field];
    const text = collapse(raw);
    if (!text) return {error: rule.empty};
    if (text.length > rule.limit) return {error: `${rule.nominative} длиннее ${rule.limit} знаков.`};
    const chars = Array.from(text);
    if (!chars.some(isCyrillic)) {
      if (chars.some(isLatin)) return {error: `${rule.nominative} ${rule.typed} латиницей — наберите кириллицей.`};
      return {error: `${rule.nominative} без букв — наберите кириллицей.`};
    }
    for (const ch of chars) {
      if (isCyrillic(ch) || rule.extra.includes(ch)) continue;
      if (isLatin(ch)) {
        const twin = HOMOGLYPHS[ch];
        return {error: twin
          ? `В ${rule.prepositional} «${text}» латинская «${ch}» вместо кириллической «${twin}» — наберите кириллицей.`
          : `В ${rule.prepositional} «${text}» латинская буква «${ch}» — наберите кириллицей.`};
      }
      if (/\p{L}/u.test(ch)) {
        return {error: `В ${rule.prepositional} «${text}» буква «${ch}» не из русской или узбекской кириллицы.`};
      }
      return {error: `В ${rule.prepositional} «${text}» недопустимый знак «${ch}»: можно буквы кириллицы, ${rule.extraText}.`};
    }
    return {value: text};
  }

  /* Ошибка сервера — к какому полю формы она относится. */
  function fieldOfError(detail) {
    const text = String(detail || '');
    if (/^(В имени|Имя |Укажите имя)/.test(text)) return 'name';
    if (/^(В должности|Должность|Укажите должность)/.test(text)) return 'role';
    if (/направлен/i.test(text)) return 'direction';
    return null;
  }

  /* Статус Hikvision в строке списка: короткая плашка. */
  function hikvisionTag(card) {
    const state = card && card.hikvision ? card.hikvision.state : null;
    if (state === 'sent') return {text: 'В Hikvision', tone: 'ok'};
    if (state === 'pending') return {text: 'Ждёт отправки', tone: 'warn'};
    if (state === 'error') return {text: 'Ошибка Hikvision', tone: 'error'};
    if (state === 'manual') return {text: 'Без Hikvision', tone: 'idle'};
    if (state === 'none') return {text: 'Нет в Hikvision', tone: 'idle'};
    return null;
  }

  /* Строка «Hikvision» в карточке: состояние шага и подписи.
     step: active — отправляем; ok — добавлен; error — сбой; wait — ждёт. */
  function hikvisionStep(card, sending) {
    const h = card && card.hikvision;
    if (sending) return {step: 'active', title: 'Отправляем в Hikvision…', number: null, note: null};
    if (!h) return {step: 'wait', title: 'На окладе — Hikvision ведёт бухгалтер', number: null, note: null};
    if (h.state === 'sent') return {step: 'ok', title: 'Добавлен в Hikvision', number: h.employee_no, note: null};
    if (h.state === 'manual') return {step: 'wait', title: 'Без Hikvision — отмечает бухгалтер', number: null, note: null};
    if (h.state === 'none') return {step: 'wait', title: 'Нет в Hikvision', number: null, note: null};
    if (h.state === 'error') return {step: 'error', title: 'Не добавлен в Hikvision', number: h.employee_no,
      note: h.message || 'Карточка сохранена. Отправьте ещё раз.'};
    return {step: 'wait', title: 'Ожидает отправки в Hikvision', number: h.employee_no,
      note: h.message || 'Карточка сохранена и ждёт отправки.'};
  }

  function initials(name) {
    const parts = collapse(name).split(' ').filter(Boolean);
    return (parts.slice(0, 2).map(part => Array.from(part)[0]).join('') || '·').toUpperCase();
  }

  /* Ключ запроса «Сохранить»: живёт до успешного ответа, поэтому повтор после
     обрыва связи находит ту же карточку. */
  function newKey(random = globalThis.crypto) {
    if (random && typeof random.randomUUID === 'function') return random.randomUUID();
    return 'k' + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  }

  return {checkText, fieldOfError, hikvisionTag, hikvisionStep, initials, newKey, collapse};
});
