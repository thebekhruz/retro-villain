/* «Сотрудники» (1a) без DOM: разбор сумм из поля, поиск по имени и группа
   по должности. То, в чём легко ошибиться, проверяется тестом
   (tests/js/employees-logic.test.mjs), а не глазами. */
(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.EmployeesLogic = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {

  /* Сумма из поля: «180 000», «180000», «180 000,50», «180000.5».
     '' — «не указано» (ставку можно не задавать), иначе строка для API
     («180000» / «180000.5») или ошибка: bad — не число, zero — ноль. */
  function parseAmount(text) {
    const raw = String(text == null ? '' : text).trim();
    if (!raw) return {value: null};
    const compact = raw.replace(/[\s  ]/g, '');
    if (!/^\d+(?:[.,]\d{1,2})?$/.test(compact)) return {error: 'bad'};
    const value = compact.replace(',', '.').replace(/^0+(?=\d)/, '');
    if (!(Number(value) > 0)) return {error: 'zero'};
    return {value};
  }
  const money = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
  // В поле — с разрядами, как на экране: 180000 → «180 000».
  function formatAmount(value) {
    if (value == null || value === '') return '';
    const n = Number(value);
    return Number.isFinite(n) ? money.format(n).replace(/ | /g, ' ') : String(value);
  }

  /* Поиск: без регистра, «ё» = «е», и кириллица находится латиницей
     (Alijon → Алижон, Shoxrux / Shokhrukh → Шохрух) — бухгалтер набирает
     имя как ему удобнее. */
  const CYR = {а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ж: 'j', з: 'z', и: 'i', й: 'y', к: 'k', л: 'l',
    м: 'm', н: 'n', о: 'o', п: 'p', р: 'r', с: 's', т: 't', у: 'u', ф: 'f', х: 'x', ц: 's', ч: 'ch', ш: 'sh',
    щ: 'sh', ъ: '', ы: 'i', ь: '', э: 'e', ю: 'yu', я: 'ya', ў: 'o', қ: 'q', ғ: 'g', ҳ: 'x'};
  const plain = value => String(value || '').toLowerCase().replace(/ё/g, 'е').replace(/\s+/g, ' ').trim();
  function latinKey(value) {
    // Для латиницы «ё» — это «yo» (Ихтиёр → Ixtiyor), а не «е».
    const cyr = String(value || '').toLowerCase().replace(/\s+/g, ' ').trim().replace(/ё/g, 'yo')
      .replace(/[а-яўқғҳ]/g, ch => CYR[ch] ?? ch);
    return cyr.replace(/[ʻʼ’‘'`]/g, '').replace(/kh/g, 'x').replace(/zh/g, 'j').replace(/ts/g, 's')
      .replace(/(^|[^sc])h/g, '$1x').replace(/ye/g, 'e').replace(/w/g, 'v');
  }
  function matchesQuery(query, ...fields) {
    const q = plain(query);
    if (!q) return true;
    const lq = latinKey(q);
    return fields.some(field => {
      const text = plain(field);
      return text.includes(q) || (lq && latinKey(field).includes(lq));
    });
  }

  /* Группа по должности — как на сервере (roster.group_for): новому
     сотруднику группа подставляется сама, пока её не выбрали руками.
     Незнакомая должность — null: группу выбирают из списка. */
  const GROUPS = {менеджер: 'Управление', хостес: 'Встреча гостей', официант: 'Обслуживание зала',
    ранер: 'Обслуживание зала', бармен: 'Бар', няня: 'Присмотр за детьми', техперсонал: 'Уборка', охрана: 'Охрана'};
  function groupForRole(role) {
    const normalized = String(role || '').toLowerCase().split(/\s+/).filter(Boolean).join(' ');
    if (!normalized) return null;
    if (normalized.startsWith('повар') || normalized === 'кондитер') return 'Кухня';
    return GROUPS[normalized] || null;
  }

  /* Почему сменному не начисляется — всё сразу, а не первое попавшееся:
     «Нет ставки · нет привязки Hikvision». */
  function attentionReasons(row) {
    const reasons = [];
    if (row.rate == null || row.rate === '') reasons.push('Нет ставки');
    if (row.status === 'unlinked') reasons.push(reasons.length ? 'нет привязки Hikvision' : 'Нет привязки Hikvision');
    if (row.status === 'unavailable') reasons.push(reasons.length ? 'нет данных Hikvision' : 'Нет данных Hikvision');
    return reasons;
  }

  return {parseAmount, formatAmount, latinKey, matchesQuery, groupForRole, attentionReasons};
});
