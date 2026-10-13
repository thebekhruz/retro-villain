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
  const visitForm = el('prepay-reserves-day'), visitDate = el('prepay-reserves-date');
  const visitButton = el('prepay-reserves-load'), visitPanel = el('prepay-reserves-panel');
  const money = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
  const demo = new URLSearchParams(location.search).get('demo') === '1';
  let generation = 0, controller = null;
  let visitGeneration = 0, visitController = null, visitLoaded = false, today = '';
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
    card.dataset.reserveId = row.id;
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
  function clearPayments() {
    el('prepay-source-payments').replaceChildren();
    el('prepay-payments-status').replaceChildren();
    el('prepay-matches-status').hidden = true;
  }
  function resetVisit() {
    visitGeneration++; visitController?.abort(); visitLoaded = false;
    visitButton.disabled = false; visitPanel.setAttribute('aria-busy', 'false');
    el('prepay-reserves-list').replaceChildren();
    el('prepay-reserves-summary').replaceChildren();
    el('prepay-reserves-status').replaceChildren();
    el('prepay-reserves-status').classList.toggle('is-warn', false);
  }
  async function loadVisit(refresh = false) {
    resetVisit();
    const day = visitDate.value;
    if (!day || !visitForm.reportValidity()) {
      el('prepay-reserves-status').textContent = 'Выберите дату визита.';
      return;
    }
    const current = visitGeneration;
    visitController = new AbortController();
    visitButton.disabled = true; visitPanel.setAttribute('aria-busy', 'true');
    el('prepay-reserves-status').textContent = 'Загружаем брони на выбранную дату…';
    try {
      const query = new URLSearchParams({date: day, demo: String(demo), refresh: String(refresh)});
      const response = await fetch('/api/cashier/reservations?' + query,
        {signal: visitController.signal, cache: 'no-store'});
      const data = await RetroState.responseJson(response);
      if (current !== visitGeneration) return;
      if (data.date !== day) throw new Error('Unexpected reservation date');
      partStatus('prepay-reserves-status', data.reservations, 'Броней на выбранную дату нет.');
      const rows = data.reservations.items || [];
      el('prepay-reserves-list').replaceChildren(...rows.map(reservationRow));
      const summary = el('prepay-reserves-summary');
      summary.replaceChildren(node('span', '', 'Дата визита'), ' ', node('strong', '', displayDay(day), true));
      if (data.reservations.status === 'ok') summary.append(' · ', node('span', '', 'Броней'), ' ',
        node('strong', '', String(rows.length), true), ' · ', node('span', '', 'Гостей'), ' ',
        node('strong', '', String(rows.reduce((sum, row) => sum + row.guests_count, 0)), true));
      visitLoaded = data.reservations.status === 'ok';
    } catch (error) {
      if (current !== visitGeneration || error.name === 'AbortError') return;
      el('prepay-reserves-list').replaceChildren();
      el('prepay-reserves-summary').replaceChildren();
      el('prepay-reserves-status').classList.toggle('is-warn', true);
      el('prepay-reserves-status').textContent = 'Не удалось загрузить брони. Нажмите «Обновить».';
    } finally {
      if (current === visitGeneration) {
        visitButton.disabled = false; visitPanel.setAttribute('aria-busy', 'false');
      }
    }
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
    button.disabled = true; el('prepay-payments-panel').setAttribute('aria-busy', 'true'); clearPayments();
    el('prepay-sources-status').textContent = 'Загружаем платежи iiko…';
    try {
      const query = new URLSearchParams({start, end, demo: String(demo), refresh: String(refresh)});
      const response = await fetch('/api/cashier/prepayment-sources/payments?' + query,
        {signal: controller.signal, cache: 'no-store'});
      const data = await RetroState.responseJson(response);
      if (current !== generation) return;
      partStatus('prepay-payments-status', data.payments, 'Безналичных предоплат за этот период не найдено.');
      el('prepay-source-payments').replaceChildren(...(data.payments.items || []).map(paymentRow));
      el('prepay-matches-status').hidden = !data.matching_issue;
      el('prepay-matches-status').textContent = data.matching_issue || '';
      const status = el('prepay-sources-status'); status.replaceChildren();
      status.append(node('span', '', 'Показан период'), ' ',
        node('span', '', displayDay(data.start) + ' — ' + displayDay(data.end), true));
    } catch (error) {
      if (current !== generation || error.name === 'AbortError') return;
      clearPayments();
      el('prepay-sources-status').textContent = 'Не удалось загрузить источники iiko. Повторите запрос.';
    } finally {
      if (current === generation) { button.disabled = false; el('prepay-payments-panel').setAttribute('aria-busy', 'false'); }
    }
  }
  const ready = Promise.resolve(globalThis.RetroConfig).then(config => {
    if (!config?.today) return;
    today = config.today;
    visitDate.value = today;
    // Receipt dates are independent from the visit day, and loaded on demand.
    const earliest = config.accounting_start || '2026-10-06';
    el('prepay-sources-from').min = earliest;
    el('prepay-sources-to').min = earliest;
    el('prepay-sources-from').value = [shiftDay(config.today, -7), earliest].sort().at(-1);
    el('prepay-sources-to').value = config.today;
  }).catch(() => {
    el('prepay-reserves-status').textContent = 'Выберите дату визита.';
  });
  root.addEventListener('toggle', () => {
    if (root.open && !visitLoaded) ready.then(() => loadVisit()).catch(() => {
      el('prepay-reserves-status').textContent = 'Не удалось загрузить брони. Нажмите «Обновить».';
    });
  });
  visitForm.addEventListener('submit', event => { event.preventDefault(); loadVisit(true); });
  visitDate.addEventListener('input', resetVisit);
  visitDate.addEventListener('change', () => loadVisit(true));
  for (const [id, offset] of [['prepay-reserves-prev', -1], ['prepay-reserves-next', 1]]) {
    el(id).addEventListener('click', () => {
      if (!visitDate.value || !visitForm.reportValidity()) return;
      visitDate.value = shiftDay(visitDate.value, offset); loadVisit(true);
    });
  }
  el('prepay-reserves-today').addEventListener('click', () => {
    if (today) { visitDate.value = today; loadVisit(true); }
  });
  form.addEventListener('submit', event => { event.preventDefault(); load(true); });
  form.addEventListener('input', () => {
    generation++; controller?.abort(); button.disabled = false;
    el('prepay-payments-panel').setAttribute('aria-busy', 'false'); clearPayments();
    el('prepay-sources-status').textContent = 'Выберите период и нажмите «Показать».';
  });
})();
