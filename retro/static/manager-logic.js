/* Кабинет менеджера без DOM: поиск и фильтры по списку, разделы списка
   (сначала без фото), очередь съёмки, статусы фото и Hikvision словами.
   Менеджер заводит людей своих разделов (T-450) и фотографирует — фото
   уходит на терминал Hikvision. Тесты — tests/js/manager-logic.test.mjs. */
(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ManagerLogic = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {

  const collapse = value => String(value == null ? '' : value).split(/\s+/).filter(Boolean).join(' ');
  const plain = value => collapse(value).toLowerCase().replace(/ё/g, 'е');

  /* Должность в реестре бывает строчными («бармен») — на экране с заглавной. */
  function roleLine(card) {
    const role = collapse(card.role);
    return [role ? role[0].toUpperCase() + role.slice(1) : '', card.group].filter(Boolean).join(' · ');
  }

  function initials(name) {
    const parts = collapse(name).split(' ').filter(Boolean);
    return (parts.slice(0, 2).map(part => Array.from(part)[0]).join('') || '·').toUpperCase();
  }

  /* Всё ли сделано по человеку: фото есть и лежит на устройстве. Ручная
     отметка — на устройство не отправляем, достаточно фото. */
  function isDone(card) {
    if (!card.photo) return false;
    const h = card.hikvision || {};
    if (h.state === 'manual') return true;
    return h.state === 'sent' && (h.face || {}).state === 'sent';
  }
  function hasProblem(card) {
    if (card.photo_unknown) return true;
    const h = card.hikvision || {};
    return !!card.photo && (h.state === 'error' || h.state === 'pending'
      || ((h.face || {}).state === 'error') || ((h.face || {}).state === 'pending'));
  }

  /* Метка в строке списка: что ещё нужно сделать с человеком. */
  function rowTag(card) {
    const h = card.hikvision || {};
    if (card.photo_unknown) return {text: 'Фото не проверено', tone: 'warn'};
    if (!card.photo) return {text: 'Нет фото', tone: 'warn'};
    if (h.state === 'error' || (h.face || {}).state === 'error') return {text: 'Ошибка', tone: 'error'};
    if (isDone(card)) return {text: h.state === 'manual' ? 'Фото есть' : 'Готово', tone: h.state === 'manual' ? 'idle' : 'ok'};
    return {text: 'Ждёт отправки', tone: 'warn'};
  }

  /* Временный (T-434): «временный · 08.10–10.10» — период подписывает сервер.
     Сменному — пусто. */
  function typeTag(card) {
    if (!card || card.employment_type !== 'temporary') return '';
    return card.work_period ? 'временный · ' + card.work_period : 'временный';
  }

  function matches(card, query) {
    const words = plain(query).split(' ').filter(Boolean);
    const text = plain(card.name) + ' ' + plain(card.role) + ' ' + plain(card.group);
    return words.every(word => text.includes(word));
  }

  /* Фильтры списка: статус (все / без фото / ошибки), раздел (группа реестра)
     и должность внутри раздела. Пустое значение — без ограничения. */
  const roleKey = role => plain(role);
  function inPlace(card, filter = {}) {
    return (!filter.group || card.group === filter.group) && (!filter.role || roleKey(card.role) === filter.role);
  }
  function filterCards(cards, filter = {}, query = '') {
    const status = filter.status || 'all';
    return (cards || []).filter(card => inPlace(card, filter) && matches(card, query)
      && (status === 'nophoto' ? !card.photo && !card.photo_unknown : status === 'problem' ? hasProblem(card) : true));
  }

  /* Разделы и должности для чипов — только те, что есть в списке, со счётом. */
  function groupsOf(cards) {
    const counts = new Map();
    (cards || []).forEach(card => { if (card.group) counts.set(card.group, (counts.get(card.group) || 0) + 1); });
    return [...counts].map(([key, count]) => ({key, label: key, count}))
      .sort((a, b) => a.label.localeCompare(b.label, 'ru'));
  }
  function rolesOf(cards, group) {
    const roles = new Map();
    (cards || []).filter(card => card.group === group && collapse(card.role)).forEach(card => {
      const key = roleKey(card.role);
      const item = roles.get(key) || {key, label: collapse(card.role)[0].toUpperCase() + collapse(card.role).slice(1), count: 0};
      item.count += 1;
      roles.set(key, item);
    });
    return [...roles.values()].sort((a, b) => a.label.localeCompare(b.label, 'ru'));
  }

  /* Разделы списка (T-450): сначала без фото, затем «не на терминале»
     (ошибка, ждёт отправки, фото не проверено), затем с фото. Порядок внутри —
     как пришёл с сервера (по алфавиту). Пустые разделы не показываем. */
  const needsPhoto = card => !card.photo && !card.photo_unknown;
  function sections(cards) {
    const nophoto = [], attention = [], done = [];
    for (const card of cards || []) {
      if (needsPhoto(card)) nophoto.push(card);
      else if (!isDone(card)) attention.push(card);
      else done.push(card);
    }
    return [{key: 'nophoto', title: 'Без фото', cards: nophoto},
      {key: 'attention', title: 'Не на терминале', cards: attention},
      {key: 'done', title: 'С фото', cards: done}].filter(section => section.cards.length);
  }

  /* Очередь съёмки: следующий без фото в том же порядке, что и на экране,
     после текущего; дошли до конца — с начала. Чужих (can_photo=false) и
     текущего пропускаем. Никого — null. */
  function nextToShoot(cards, currentId = null) {
    const queue = (cards || []).filter(card => needsPhoto(card) && card.can_photo !== false);
    if (!queue.length) return null;
    const list = cards || [];
    const at = currentId == null ? -1 : list.findIndex(card => card.id === currentId);
    const after = queue.find(card => card.id !== currentId && list.indexOf(card) > at);
    return after || queue.find(card => card.id !== currentId) || null;
  }

  /* Цель в шапке: у скольких фото уже на терминале (или есть — у тех, кого
     отмечают вручную). */
  function goal(cards) {
    const list = cards || [];
    const done = list.filter(isDone).length;
    return {done, total: list.length, percent: list.length ? Math.round(done * 100 / list.length) : 0};
  }

  /* Должности формы «Новый сотрудник» — с сервера, уже по разделам менеджера. */
  function roleKeyOf(item) { return plain(item && item.role); }

  /* Ставка в форме (T-453): только цифры, пусто — без предложения.
     «200 000» → '200000'; что-то кроме цифр и пробелов — ошибка. */
  function rateDigits(value) {
    const text = collapse(value).replace(/\s/g, '');
    if (!text) return {value: null};
    if (!/^\d{1,10}$/.test(text) || Number(text) <= 0) return {error: 'Ставка — сумма в сумах цифрами, например 200 000.'};
    return {value: String(Number(text))};
  }
  function formatRate(value) {
    const digits = String(value == null ? '' : value).replace(/\D/g, '').replace(/^0+(?=\d)/, '');
    return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  }
  /* Ставка глазами менеджера: что показать в карточке. */
  function rateLine(card) {
    const rate = card && card.rate;
    if (!rate || rate.state !== 'proposed' || !rate.proposed) return '';
    return 'Ставка ' + formatRate(rate.proposed) + ' сум ждёт бухгалтера';
  }

  /* Форма: что не так, словами. Пусто — можно отправлять. */
  function formErrors(form) {
    const errors = {};
    if (form.rate !== undefined) {
      const rate = rateDigits(form.rate);
      if (rate.error) errors.rate = rate.error;
    }
    if (!collapse(form.name)) errors.name = 'Впишите фамилию и имя.';
    if (!collapse(form.role)) errors.role = 'Выберите должность.';
    if (form.type === 'temporary' && form.from && form.to && form.to < form.from) {
      errors.period = 'Последний день раньше первого.';
    }
    return errors;
  }

  function summary(cards) {
    const list = cards || [];
    return {total: list.length, withPhoto: list.filter(card => card.photo).length,
      noPhoto: list.filter(card => !card.photo && !card.photo_unknown).length, problems: list.filter(hasProblem).length};
  }

  /* Шаги в карточке: фото → человек на устройстве → лицо на устройстве. */
  function photoStep(card, uploading) {
    if (uploading) return {step: 'active', title: 'Загружаем фото…'};
    if (card.photo_unknown) return {step: 'wait', title: 'Фото в Hikvision не проверено'};
    if (card.photo?.source === 'hikvision') return {step: 'ok', title: 'Фото из Hikvision'};
    return card.photo ? {step: 'ok', title: 'Фото сохранено'} : {step: 'wait', title: 'Фото ещё нет'};
  }

  function hikvisionStep(card, sending) {
    const h = card.hikvision;
    if (!h) return {step: 'wait', title: 'Hikvision', number: null, note: null};
    if (sending && h.state !== 'sent') return {step: 'active', title: 'Добавляем в Hikvision…', number: null, note: null};
    if (h.state === 'sent') return {step: 'ok', title: 'В Hikvision', number: h.employee_no, note: null};
    if (h.state === 'manual') return {step: 'wait', title: 'Отмечает бухгалтер вручную — на устройство не отправляем', number: null, note: null};
    if (h.state === 'error') return {step: 'error', title: 'Не добавлен в Hikvision', number: h.employee_no,
      note: h.message || 'Hikvision не подтвердил добавление. Отправьте ещё раз.'};
    if (h.state === 'pending') return {step: 'wait', title: 'Ждёт отправки в Hikvision', number: h.employee_no, note: h.message || null};
    return {step: 'wait', title: 'В Hikvision уйдёт вместе с фото', number: null, note: null};
  }

  /* Лицо на устройстве — отдельный шаг после человека: без фото его нет. */
  function faceStep(card, sending) {
    const face = card && card.hikvision && card.hikvision.face;
    if (!face || face.state === 'none') return null;
    if (sending && face.state !== 'sent') return {step: 'active', title: 'Отправляем фото на устройство…', note: null};
    if (face.state === 'sent') return {step: 'ok', title: 'Фото на устройстве', note: null};
    if (face.state === 'error') return {step: 'error', title: 'Фото не на устройстве', note: face.message || null};
    return {step: 'wait', title: 'Фото ждёт отправки на устройство', note: face.message || null};
  }

  return {collapse, initials, roleLine, isDone, hasProblem, rowTag, matches, inPlace, filterCards, groupsOf, rolesOf, summary,
    photoStep, hikvisionStep, faceStep, typeTag, needsPhoto, sections, nextToShoot, goal, roleKeyOf, formErrors,
    rateDigits, formatRate, rateLine};
});
