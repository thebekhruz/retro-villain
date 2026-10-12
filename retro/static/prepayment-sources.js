/* Independent source view: hints never alter registry rows or revenue groups. */
(() => {
  'use strict';
  const shiftDay = (day, offset) => {
    const value = new Date(day + 'T12:00:00Z');
    value.setUTCDate(value.getUTCDate() + offset);
    return value.toISOString().slice(0, 10);
  };
  const validRange = (start, end) => {
    const days = (Date.parse(end) - Date.parse(start)) / 86400000;
    return Number.isFinite(days) && days >= 0 && days <= 30;
  };
  const linkText = payment => payment.link_status === 'unconfirmed' && payment.candidate_order
    ? 'Совпали сумма и время · связь с заказом не подтверждена'
    : payment.link_status === 'ambiguous' ? 'Несколько совпадений · заказ не определён' : 'Заказ не определён';
  globalThis.RetroPrepaymentSources = {shiftDay, validRange, linkText};
  if (typeof document === 'undefined') return;

  const el = id => document.getElementById(id);
  const root = el('prepay-sources');
  if (!root) return;
  const form = el('prepay-sources-period'), button = el('prepay-sources-load');
  const money = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
  const demo = new URLSearchParams(location.search).get('demo') === '1';
  let generation = 0, controller = null, loaded = false;
  const displayDay = day => day.slice(8, 10) + '.' + day.slice(5, 7) + '.' + day.slice(0, 4);
  const displayTime = value => displayDay(value.slice(0, 10)) + ' · ' + value.slice(11, 16);

  function node(tag, className, value, raw = false) {
    const item = document.createElement(tag);
    item.className = className;
    item.textContent = value;
    if (raw) item.dataset.i18n = 'off';
    return item;
  }
  function partStatus(id, part, empty) {
    const target = el(id);
    target.classList.toggle('is-warn', part.status !== 'ok');
    target.replaceChildren();
    if (part.status !== 'ok') target.append(part.issue || 'Источник временно недоступен.');
    else if (!part.items.length) target.append(empty);
    else if (part.checked_at) target.append(node('span', '', 'Обновлено'), ' ',
      node('span', '', displayTime(part.checked_at), true));
  }
  function reservationRow(row) {
    const card = node('article', 'prepay-source-row', '');
    const main = node('div', 'prepay-source-main', '');
    main.append(node('strong', '', displayTime(row.estimated_start_time), true));
    for (const table of row.tables) {
      const location = node('span', '', table.section + ' · ', true);
      // Keep dictionary content separate from translatable interface labels.
      const line = node('div', 'prepay-source-meta', '');
      line.append(location, node('span', '', 'Стол'), ' ', node('span', '', String(table.number), true));
      main.append(line);
    }
    const extra = node('div', 'prepay-source-meta', '');
    extra.append(node('span', '', 'Гостей'), ' ', node('span', '', String(row.guests_count), true),
      ' · ', node('span', '', String(row.duration_minutes), true), ' ', node('span', '', 'мин'));
    main.append(extra);
    card.append(main, node('span', 'rm-pill prepay-source-label', 'Бронь iiko'));
    return card;
  }
  function paymentRow(row) {
    const card = node('article', 'prepay-source-row', '');
    const main = node('div', 'prepay-source-main', '');
    main.append(node('strong', '', row.payment_method || 'Способ не указан', Boolean(row.payment_method)),
      node('small', 'prepay-source-meta', displayTime(row.received_at), true));
    if (row.link_status === 'unconfirmed' && row.candidate_order) {
      const candidate = node('div', 'prepay-source-candidate', '');
      candidate.append(node('span', '', 'Возможно, заказ'), ' ',
        node('span', '', '№' + row.candidate_order.order_number, true));
      main.append(candidate);
    }
    main.append(node('small', 'prepay-source-meta', linkText(row)));
    card.append(main, node('strong', 'rm-num prepay-source-amount', money.format(Number(row.amount)), true));
    return card;
  }
  function clearLists() {
    el('prepay-reserves-list').replaceChildren();
    el('prepay-source-payments').replaceChildren();
    el('prepay-reserves-status').replaceChildren();
    el('prepay-payments-status').replaceChildren();
    el('prepay-matches-status').hidden = true;
  }
  async function load(refresh = false) {
    if (!form.reportValidity()) return;
    const start = el('prepay-sources-from').value, end = el('prepay-sources-to').value;
    if (!validRange(start, end)) {
      el('prepay-sources-status').textContent = 'Выберите период не больше 31 дня.';
      return;
    }
    const current = ++generation;
    controller?.abort(); controller = new AbortController();
    button.disabled = true; root.setAttribute('aria-busy', 'true'); clearLists();
    el('prepay-sources-status').textContent = 'Загружаем брони и платежи iiko…';
    try {
      const query = new URLSearchParams({start, end, demo: String(demo), refresh: String(refresh)});
      const response = await fetch('/api/cashier/prepayment-sources?' + query,
        {signal: controller.signal, cache: 'no-store'});
      const data = await RetroState.responseJson(response);
      if (current !== generation) return;
      partStatus('prepay-reserves-status', data.reservations, 'Броней на этот период нет.');
      partStatus('prepay-payments-status', data.payments, 'Безналичных предоплат за этот период не найдено.');
      el('prepay-reserves-list').replaceChildren(...(data.reservations.items || []).map(reservationRow));
      el('prepay-source-payments').replaceChildren(...(data.payments.items || []).map(paymentRow));
      el('prepay-matches-status').hidden = !data.matching_issue;
      el('prepay-matches-status').textContent = data.matching_issue || '';
      const status = el('prepay-sources-status'); status.replaceChildren();
      status.append(node('span', '', 'Показан период'), ' ',
        node('span', '', displayDay(data.start) + ' — ' + displayDay(data.end), true));
      loaded = true;
    } catch (error) {
      if (current !== generation || error.name === 'AbortError') return;
      clearLists();
      el('prepay-sources-status').textContent = 'Не удалось загрузить источники iiko. Повторите запрос.';
    } finally {
      if (current === generation) { button.disabled = false; root.setAttribute('aria-busy', 'false'); }
    }
  }
  const ready = Promise.resolve(globalThis.RetroConfig).then(config => {
    if (!config?.today) return;
    // A recent receipt and a future visit can fall in the same selected period.
    const earliest = config.accounting_start || '2026-10-06';
    el('prepay-sources-from').min = earliest;
    el('prepay-sources-to').min = earliest;
    el('prepay-sources-from').value = [shiftDay(config.today, -7), earliest].sort().at(-1);
    el('prepay-sources-to').value = shiftDay(config.today, 23);
  }).catch(() => {
    el('prepay-sources-status').textContent = 'Не удалось загрузить источники iiko. Повторите запрос.';
  });
  root.addEventListener('toggle', () => {
    if (root.open && !loaded) ready.then(() => load()).catch(() => {
      el('prepay-sources-status').textContent = 'Не удалось загрузить источники iiko. Повторите запрос.';
    });
  });
  form.addEventListener('submit', event => { event.preventDefault(); load(true); });
  form.addEventListener('input', () => {
    generation++; controller?.abort(); button.disabled = false; loaded = false;
    root.setAttribute('aria-busy', 'false'); clearLists();
    el('prepay-sources-status').textContent = 'Выберите период и нажмите «Показать».';
  });
})();
