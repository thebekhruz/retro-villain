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

  /* Должности внутри группы — второй ряд чипов под группами (ТЗ 09.10, Б-06):
     в «Кухне» — повар миллий, тандыр, турк, холодный цех… Набор не придуман,
     это должности из реестра как их записали; одна должность — с точностью до
     регистра, пробелов и «ё». В группе меньше двух должностей — ряда нет:
     выбирать не из чего. Общая логика «Сотрудников» и «Зарплаты · день». */
  const roleKey = role => plain(role);
  function roleLabel(role) {
    const text = String(role || '').replace(/\s+/g, ' ').trim();
    return text ? text[0].toUpperCase() + text.slice(1) : '';
  }
  function groupRoles(rows, group) {
    const roles = new Map();
    (rows || []).forEach(row => {
      const key = roleKey(row.role);
      if (row.group !== group || !key) return;
      const item = roles.get(key) || {key, label: roleLabel(row.role), count: 0};
      item.count += 1;
      roles.set(key, item);
    });
    const list = [...roles.values()].sort((a, b) => a.label.localeCompare(b.label, 'ru'));
    return list.length > 1 ? list : [];
  }
  // Пустой фильтр — все должности.
  const matchesRole = (filter, role) => !filter || roleKey(role) === filter;

  /* Почему сменному не начисляется — всё сразу, а не первое попавшееся:
     «Нет ставки · нет привязки Hikvision». */
  function attentionReasons(row) {
    const reasons = [];
    if (row.rate == null || row.rate === '') reasons.push('Нет ставки');
    if (row.status === 'unlinked') reasons.push(reasons.length ? 'нет привязки Hikvision' : 'Нет привязки Hikvision');
    if (row.status === 'unavailable') reasons.push(reasons.length ? 'нет данных Hikvision' : 'Нет данных Hikvision');
    return reasons;
  }

  /* Временный сотрудник (T-434): пометка «временный · 08.10–10.10» («с 08.10»,
     «по 10.10»). Период подписывает сервер (work_period) — одна подпись на
     всех экранах и в Excel. Сменному — пусто. */
  function typeTag(row) {
    if (!row || row.employment_type !== 'temporary') return '';
    return row.work_period ? 'временный · ' + row.work_period : 'временный';
  }
  const dm = day => day.slice(8, 10) + '.' + day.slice(5, 7);
  /* Период «с — по» из полей даты: обе границы необязательны, «по» не раньше
     «с». Текст — тот же, что у сервера. */
  function periodError(from, to) {
    return from && to && to < from ? 'Период работы: «по» (' + dm(to) + ') раньше, чем «с» (' + dm(from) + ').' : '';
  }
  /* Что отправить серверу: у сменного период всегда пустой — переключили
     временного обратно в сменные, и даты, оставшиеся в полях, не уйдут. */
  function periodPayload(draft) {
    const temporary = draft.type === 'temporary';
    return {employment_type: temporary ? 'temporary' : 'shift',
      work_from: temporary && draft.from ? draft.from : null, work_to: temporary && draft.to ? draft.to : null};
  }

  return {parseAmount, formatAmount, latinKey, matchesQuery, groupForRole, groupRoles, matchesRole, attentionReasons,
    typeTag, periodError, periodPayload};
});
