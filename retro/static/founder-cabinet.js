/* Кабинет учредителя (ui_solid1 7a / 7b).
 *
 * Каждый блок грузится своим запросом и падает отдельно: если iiko думает
 * долго, деньги бухгалтера всё равно на экране, а блок iiko пишет, почему
 * пуст. Нулей вместо «нет данных» здесь нет нигде. */
(() => {
  const $ = id => document.getElementById(id);
  const logic = globalThis.FounderCabinetLogic;
  const director = globalThis.DirectorLogic;
  const {num, sum, short, plural, dm} = logic;
  const desk = matchMedia('(min-width: 901px)');
  const DISMISSED_KEY = 'retro:founder-chef-dismissed';
  const MONTHS = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];

  const state = {today: null, yesterday: null, data: {}, pending: {}, editor: {open: false, draft: 0, message: '', error: false, saving: false}};
  let chat = null;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = text;
    return element;
  }

  async function request(url) {
    const response = await fetch(url);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || 'Не удалось получить данные.');
    }
    return response.json();
  }

  /** Один запрос на ключ: блоки телефона и компьютера делят данные. */
  function load(key, url) {
    if (state.data[key]) return Promise.resolve(state.data[key]);
    if (!state.pending[key]) {
      state.pending[key] = request(url).then(value => { state.data[key] = value; return value; })
        .finally(() => { delete state.pending[key]; });
    }
    return state.pending[key];
  }

  function shiftDay(iso, days) {
    const value = new Date(iso + 'T12:00:00Z');
    value.setUTCDate(value.getUTCDate() + days);
    return value.toISOString().slice(0, 10);
  }

  function setText(id, value) { $(id).textContent = value; }

  function dismissed() {
    try { return JSON.parse(localStorage.getItem(DISMISSED_KEY) || '[]'); } catch (error) { return []; }
  }

  function dismiss(orderId) {
    try {
      const list = dismissed().concat(orderId).slice(-50);
      localStorage.setItem(DISMISSED_KEY, JSON.stringify(list));
    } catch (error) { /* без хранилища баннер просто вернётся при обновлении */ }
  }

  // ── Дивиденды ────────────────────────────────────────────────────────
  function renderDividends(data) {
    const view = logic.dividendView(data);
    if (!view) return;
    setText('fo-div-range', view.range);
    setText('fo-div-collected', sum(view.collected));
    setText('fo-div-of', view.target === null ? 'цель не задана' : 'из ' + sum(view.target));
    $('fo-div-fill').style.width = view.pct + '%';
    $('fo-div-pace').style.left = view.pacePct + '%';
    $('fo-div-pace').hidden = view.target === null;
    setText('fo-div-status', view.status);
    $('fo-div-status').className = view.tone === 'warn' ? 'is-warn' : '';
    setText('fo-div-payout', view.payout);
    setText('fo-div-target', view.target === null ? 'не задана' : sum(view.target) + ' сум');
    setText('k-div', short(view.collected));
    setText('k-div-sub', view.target === null ? 'цель не задана' : 'из ' + short(view.target) + ' · ' + view.status.toLowerCase());
    $('k-div-bar').style.width = view.pct + '%';
    if (!state.editor.open) state.editor.draft = view.target || 0;
    renderEditor();
  }

  function editorHost() { return desk.matches ? $('fo-div-editor-desk') : $('fo-div-editor-phone'); }

  function renderEditor() {
    const data = state.data.dividends;
    const editor = state.editor;
    ['fo-div-editor-desk', 'fo-div-editor-phone'].forEach(id => { $(id).hidden = true; $(id).replaceChildren(); });
    $('fo-div-toggle').setAttribute('aria-expanded', String(editor.open));
    $('k-div-edit').setAttribute('aria-expanded', String(editor.open));
    setText('fo-div-toggle-label', editor.open ? 'Свернуть' : 'Изменить');
    setText('k-div-edit', editor.open ? 'Свернуть' : 'Изменить сумму');
    if (!editor.open || !data) return;
    const host = editorHost();
    host.hidden = false;
    const view = logic.dividendView(data);
    const stepper = node('div', 'fo-stepper');
    const minus = node('button', '', '−'), plus = node('button', '', '+');
    minus.type = plus.type = 'button';
    minus.setAttribute('aria-label', 'Меньше на 500 000');
    plus.setAttribute('aria-label', 'Больше на 500 000');
    const input = node('input', 'rm-num');
    input.inputMode = 'numeric';
    input.setAttribute('aria-label', 'Сумма дивидендов в неделю, сум');
    input.value = editor.draft ? sum(editor.draft) : '';
    minus.addEventListener('click', () => { editor.draft = logic.stepTarget(editor.draft, -1); editor.message = ''; renderEditor(); });
    plus.addEventListener('click', () => { editor.draft = logic.stepTarget(editor.draft, 1); editor.message = ''; renderEditor(); });
    input.addEventListener('change', () => { editor.draft = logic.parseAmount(input.value); editor.message = ''; renderEditor(); });
    stepper.append(minus, input, plus);
    const presets = node('div', 'fo-presets');
    logic.DIVIDEND_PRESETS.forEach(value => {
      const button = node('button', value === editor.draft ? 'is-active' : '', short(value));
      button.type = 'button';
      button.addEventListener('click', () => { editor.draft = value; editor.message = ''; renderEditor(); });
      presets.append(button);
    });
    const free = view.free;
    const feasible = node('p', 'fo-feasible' + (free !== null && editor.draft > free ? ' is-warn' : ''),
      free === null ? 'Средний свободный остаток кассы пока не посчитан: нет передач кассира за неделю.'
        : editor.draft > free ? 'Касса в среднем свободно даёт ≈ ' + short(free) + ' в неделю — больше может не хватить на закуп и зарплаты.'
          : 'Касса в среднем свободно даёт ≈ ' + short(free) + ' в неделю: после закупа, зарплат и расходов за 7 дней.');
    host.append(stepper, presets, feasible);
    if (editor.draft > 0 && editor.draft !== view.target) {
      const save = node('button', 'fo-save', editor.saving ? 'Сохраняем…' : 'Сохранить ' + sum(editor.draft) + ' сум');
      save.type = 'button';
      save.disabled = editor.saving;
      save.addEventListener('click', saveTarget);
      host.append(save);
    }
    const source = view.source;
    const note = editor.message || (source ? (source.inherited ? 'Сумма перешла с прошлой недели.' : 'Изменено ' +
      new Date(source.changed_at).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Tashkent'})) : '');
    host.append(node('p', 'fo-msg' + (editor.error ? ' is-error' : ''), note));
  }

  async function saveTarget() {
    const editor = state.editor, data = state.data.dividends;
    editor.saving = true;
    renderEditor();
    try {
      const response = await globalThis.RetroFinancialWrite('/api/founder/dividends/weekly', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({week: data.week, amount: String(editor.draft)}),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || 'Не удалось сохранить сумму.');
      state.data.dividends = body;
      editor.message = 'Сохранено. Бухгалтер видит новую сумму в «Финансах дня».';
      globalThis.RetroToast?.show(editor.message);
      editor.error = false;
      editor.saving = false;
      renderDividends(body);
    } catch (error) {
      editor.message = error.message;
      globalThis.RetroToast?.show(error.message, 'error');
      editor.error = true;
      editor.saving = false;
      renderEditor();
    }
  }

  function toggleEditor() {
    state.editor.open = !state.editor.open;
    state.editor.message = '';
    const data = state.data.dividends;
    if (state.editor.open && data) state.editor.draft = num(data.target) || 0;
    renderEditor();
  }

  // ── Телефон (7a) ─────────────────────────────────────────────────────
  function renderAlerts(chef) {
    const target = $('fo-alerts');
    target.replaceChildren();
    logic.chefAlerts(chef, state.today, state.yesterday, dismissed()).forEach(bill => {
      const alert = node('div', 'fo-alert');
      const body = node('div');
      const meta = node('small');
      meta.append(node('span', '', 'RETRO MILLIY'), node('span', '', bill.day === state.today ? 'сегодня' : 'вчера'));
      body.append(meta, node('strong', '', 'Счёт Шефа · ' + sum(num(bill.amount)) + ' сум'),
        node('p', '', 'Стол ' + bill.table + ' · ' + (bill.waiters.join(', ') || 'без официанта') + ' · больше ' + short(logic.CHEF_LIMIT) + ' за один счёт'));
      const close = node('button', '', '×');
      close.type = 'button';
      close.setAttribute('aria-label', 'Скрыть уведомление');
      close.addEventListener('click', () => { dismiss(bill.order_id); alert.remove(); });
      alert.append(node('span', 'fo-alert-mark', 'R'), body, close);
      target.append(alert);
    });
  }

  function renderPhoneDays(today, yesterday) {
    setText('fo-y-date', dm(state.yesterday));
    if (yesterday) {
      setText('fo-y-demo', yesterday.cashier ? sum(num(yesterday.cashier.demo)) : '—');
      const handover = yesterday.handover;
      setText('fo-y-recv', handover.recorded === null ? 'не записано' : sum(num(handover.recorded)));
      const mark = logic.handoverMark(yesterday);
      setText('fo-y-mark', mark.mark);
      $('fo-y-mark').className = 'fo-mark is-' + mark.tone;
      $('fo-y-mark').title = mark.tip;
    }
    if (!today) return;
    setText('fo-morning', today.opening_balance === null ? '—' : sum(num(today.opening_balance)));
    const cashier = today.cashier;
    const forecast = state.data.forecast && state.data.forecast.today;
    const guess = forecast && forecast.forecast;
    const retro = cashier ? num(cashier.retro) : null, school = cashier ? num(cashier.school) : null;
    setText('fo-t-retro', short(retro));
    setText('fo-t-ox', short(school));
    setText('fo-t-retro-fc', guess && guess.retro !== null ? '/ ≈' + short(num(guess.retro)) : '');
    setText('fo-t-ox-fc', guess && guess.school !== null ? '/ ≈' + short(num(guess.school)) : '');
    $('fo-t-retro-bar').style.width = guess && num(guess.retro) ? Math.min(100, (retro || 0) / num(guess.retro) * 100) + '%' : '0%';
    $('fo-t-ox-bar').style.width = guess && num(guess.school) ? Math.min(100, (school || 0) / num(guess.school) * 100) + '%' : '0%';
    const demo = cashier ? num(cashier.demo) : null;
    setText('fo-t-demo', cashier ? 'из них Демо ' + short(demo) + (retro ? ' · ' + Math.round(demo / retro * 100) + '% Retro' : '') : (today.cashier_error || ''));
    if (forecast) {
      const orders = forecast.orders ? Object.values(forecast.orders).reduce((a, b) => a + b, 0) : 0;
      setText('fo-t-checks', orders + (guess && guess.orders ? ' из ≈' + guess.orders : '') + ' ' + plural(guess && guess.orders ? guess.orders : orders, 'чек', 'чека', 'чеков'));
    }
    const outlook = today.outlook;
    // Оценки по средним — до десяти тысяч: точность до сума тут мнимая.
    const rough = value => '≈ ' + short(Math.round((num(value) || 0) / 1e4) * 1e4);
    if (outlook) {
      setText('fo-out-sal', short(num(outlook.salary_due)));
      setText('fo-out-zak', rough(outlook.procurement));
      setText('fo-out-oth', rough(outlook.other));
      const evening = logic.eveningCash(today);
      setText('fo-evening', evening === null ? '—' : rough(evening));
    }
    const issues = logic.accountantIssues({days: [yesterday, today].filter(Boolean)}, null, globalThis.AccountantLogic.dayChecks);
    const line = $('fo-acc-line');
    line.textContent = issues.length ? issues.length + ' ' + plural(issues.length, 'замечание', 'замечания', 'замечаний') + ' к бухгалтеру' : 'Бухгалтер без замечаний';
    line.className = 'fo-acc-line ' + (issues.length ? 'is-bad' : 'is-ok');
  }

  function renderChefWeek(chef) {
    const target = $('fo-chef-week');
    target.replaceChildren();
    if (chef.error) { target.append(node('p', 'fo-hint', 'Счёт Шефа: ' + chef.error)); return; }
    chef.week.filter(bill => bill.over).forEach(bill => {
      const row = node('div', 'fo-line');
      row.append(node('span', '', 'Счёт Шефа · ' + dm(bill.day) + ' · стол ' + bill.table), node('b', 'rm-num', sum(num(bill.amount)) + ' сум'));
      target.append(row);
    });
  }

  async function loadPhone() {
    const [today, yesterday] = await Promise.allSettled([
      load('today', '/api/founder/day?date=' + state.today),
      load('yesterday', '/api/founder/day?date=' + state.yesterday),
    ]);
    renderPhoneDays(today.value, yesterday.value);
    setText('updated', today.status === 'rejected' ? today.reason.message
      : 'Данные на ' + new Date().toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Tashkent'}));
  }

  // ── Компьютер (7b) ───────────────────────────────────────────────────
  function renderWeek(week) {
    const forecast = state.data.forecast;
    const table = $('fo-week-table');
    table.replaceChildren();
    const head = node('div', 'fo-row is-head');
    head.setAttribute('role', 'row');
    head.append(node('span', '', 'Показатель'));
    week.days.forEach(day => {
      const cell = node('span', day.date === state.today ? 'is-today' : day.future ? 'is-future' : '');
      cell.append(node('strong', '', logic.WEEKDAYS[day.weekday]), node('span', '', dm(day.date)));
      head.append(cell);
    });
    head.append(node('span', '', 'Итого'));
    table.append(head);
    logic.weekRows(week, forecast).forEach(row => {
      const line = node('div', 'fo-row' + (row.balance ? ' is-balance' : ''));
      line.setAttribute('role', 'row');
      line.append(node('span', '', row.label));
      row.cells.forEach(cell => line.append(node('span', cell.tone ? 'is-' + cell.tone : '', cell.text)));
      line.append(node('span', '', row.total));
      table.append(line);
    });
    const check = node('div', 'fo-row is-check');
    check.append(node('span', '', 'Проверка передачи'));
    week.days.forEach(day => {
      const mark = logic.handoverMark(day);
      const cell = node('span', 'is-' + mark.tone, mark.mark);
      if (mark.tip) cell.title = mark.tip;
      check.append(cell);
    });
    check.append(node('span'));
    table.append(check);
    const totals = logic.weekTotals(week);
    setText('k-retro', short(totals.retro));
    setText('k-ox', short(totals.school));
    setText('k-demo', short(totals.demo));
    setText('k-demo-sub', totals.demoShare === null ? '' : totals.demoShare + '% выручки Retro');
    setText('k-retro-sub', totals.days + ' ' + plural(totals.days, 'день', 'дня', 'дней') + ' недели');
    setText('k-ox-sub', week.orders_error ? 'чеки: ' + week.orders_error : '');
    setText('fo-week-label', dm(week.start) + '–' + dm(week.end));
  }

  function renderChecks() {
    const week = state.data.week;
    if (!week) return;
    const issues = logic.accountantIssues(week, state.data.spending, globalThis.AccountantLogic.dayChecks);
    state.issues = issues;
    setText('fo-checks-title', issues.length ? issues.length + ' ' + plural(issues.length, 'замечание', 'замечания', 'замечаний') : 'Замечаний нет');
    const list = $('fo-checks');
    list.replaceChildren();
    issues.slice(0, 7).forEach(item => {
      const row = node('div');
      const text = node('div');
      text.append(node('strong', '', item.text), node('small', '', item.sub));
      row.append(node('i', item.level === 'bad' ? 'is-bad' : ''), text);
      list.append(row);
    });
    setText('fo-checks-more', issues.length > 7 ? 'Ещё ' + (issues.length - 7) + ' — спросите AI' : '');
    $('fo-ask-checks').hidden = !issues.length;
  }

  function renderChef(chef) {
    renderAlerts(chef);
    renderChefWeek(chef);
    const list = $('fo-chef');
    list.replaceChildren();
    if (chef.error) { list.append(node('p', 'fo-empty', chef.error)); setText('fo-chef-title', 'Нет данных iiko'); return; }
    setText('fo-chef-title', short(num(chef.week_total)) + ' за неделю');
    setText('fo-chef-over', chef.week_over + ' выше 400 000');
    setText('fo-chef-month', 'За месяц: ' + short(num(chef.month_total)));
    if (!chef.week.length) list.append(node('p', 'fo-empty', 'На этой неделе счетов «Счёт Шефа» нет.'));
    // Счета выше порога — наверх: ради них учредитель и открывает блок.
    const bills = chef.week.filter(bill => bill.over).concat(chef.week.filter(bill => !bill.over));
    bills.slice(0, 12).forEach(bill => {
      const row = node('div', 'fo-chef-row' + (bill.over ? ' is-over' : ''));
      const who = node('span');
      who.append(document.createTextNode('Стол ' + bill.table + ' · '), node('small', '', bill.waiters.join(', ') || '—'));
      row.append(node('span', '', dm(bill.day)), who, node('b', '', sum(num(bill.amount))));
      list.append(row);
    });
  }

  function renderSpending(data) {
    const month = MONTHS[Number(data.month.slice(5, 7)) - 1].toUpperCase();
    setText('fo-exp-month', month);
    setText('fo-shokh-month', month);
    setText('fo-exp-total', sum(num(data.expenses.total)) + ' сум');
    const list = $('fo-exp-list');
    list.replaceChildren();
    const max = Math.max(1, ...data.expenses.categories.map(item => num(item.amount)));
    if (!data.expenses.categories.length) list.append(node('p', 'fo-empty', 'Бухгалтер ещё не внёс расходы за месяц.'));
    data.expenses.categories.forEach(item => {
      const row = node('div');
      const line = node('div', 'fo-bars-line');
      const value = node('span');
      value.append(node('b', 'rm-num', short(num(item.amount))), node('i', '', ' · ' + (item.share_percent === null ? '—' : Math.round(num(item.share_percent)) + '%')));
      line.append(node('span', '', item.label), value);
      const bar = node('div', 'rm-bar');
      const fill = node('span');
      fill.style.width = Math.max(3, num(item.amount) / max * 100) + '%';
      fill.style.background = item.label === 'Дивиденды' ? 'var(--gold)' : item.label.startsWith('Закуп') ? '#91a786' : item.label.startsWith('Зарплаты') ? '#24594b' : '#b2c3aa';
      bar.append(fill);
      row.append(line, bar);
      list.append(row);
    });
    const shokh = data.shokh;
    setText('z-given', short(num(shokh.given)));
    setText('z-spent', short(num(shokh.spent)));
    setText('z-direct', short(num(shokh.direct)));
    setText('z-pocket', shokh.pocket === null ? '—' : short(num(shokh.pocket)));
    setText('z-flag-title', shokh.flagged.length ? shokh.flagged.length + ' ' + plural(shokh.flagged.length, 'покупка требует', 'покупки требуют', 'покупок требуют') + ' проверки' : 'Все покупки Шоха в норме');
    const flags = $('z-flags');
    flags.replaceChildren(...shokh.flagged.slice(0, 3).map(row => {
      const item = node('div');
      item.append(node('span', '', row.item + ' · ' + sum(num(row.total))), node('small', '', dm(row.day) + ' · ' + row.reason));
      return item;
    }));
    const top = $('z-top');
    top.replaceChildren(...shokh.top_items.map(row => {
      const chip = node('span', '', row.item + ' · ');
      chip.append(node('b', '', short(num(row.amount))));
      return chip;
    }));
    // Без покупок подпись «Больше всего потрачено на» повисала без продолжения.
    $('z-top-title').hidden = !shokh.top_items.length;
    if (!shokh.top_items.length) top.append(node('span', '', 'Шох ещё не записывал покупки'));
  }

  function renderForecast(data) {
    const target = $('fo-forecast');
    target.replaceChildren();
    if (data.error || !data.weekdays) { target.append(node('p', 'fo-empty', data.error || 'Нет истории продаж.')); return; }
    const max = Math.max(1, ...data.weekdays.map(day => num(day.revenue) || 0));
    const todayWeekday = (new Date(state.today + 'T12:00:00Z').getUTCDay() + 6) % 7;
    data.weekdays.forEach(day => {
      const column = node('div', day.weekday === todayWeekday ? 'is-today' : '');
      const bar = node('i');
      bar.style.height = Math.max(6, (num(day.revenue) || 0) / max * 100) + '%';
      column.append(node('span', 'rm-num', short(num(day.revenue))), bar, node('strong', '', day.label),
        node('small', '', day.orders === null ? '—' : day.orders + ' чеков'));
      target.append(column);
    });
    setText('fo-forecast-note', 'Среднее за ' + data.weeks + ' недель по кассам Retro и Oxbridge, без банкетного зала. Сегодня выделен.');
    const month = data.month;
    setText('k-month', short(num(month.total)));
    setText('k-month-sub', 'факт ' + short(num(month.fact)) + ' + прогноз ' + short(num(month.forecast)));
    if (state.data.week) renderWeek(state.data.week);
    if (state.data.today) renderPhoneDays(state.data.today, state.data.yesterday);
  }

  function renderDishes(snapshot) {
    const rows = director.rank(snapshot.item_metrics.all || {}, 'revenue').filter(director.isDish).filter(row => row.revenue > 0);
    const target = $('fo-dishes');
    target.replaceChildren();
    rows.slice(0, 5).forEach((row, index) => {
      const line = node('div', 'fo-dish');
      const margin = row.margin === null ? '—' : Math.round(row.margin) + '%';
      line.append(node('span', '', String(index + 1)), node('strong', '', row.name),
        node('span', '', Math.round(row.quantity) + ' шт'), node('b', '', short(row.revenue)),
        node('small', director.lowMargin(row) ? 'm-low' : 'm-ok', margin));
      target.append(line);
    });
    const low = rows.filter(director.lowMargin).slice(0, 3);
    const chips = $('fo-dishes-low');
    chips.replaceChildren(...low.map(row => node('span', '', row.name + ' · маржа ' + Math.round(row.margin) + '% · ' + short(row.revenue) + ' за 7 дней')));
    if (!low.length) chips.append(node('span', '', 'Популярных блюд с маржой ниже ' + director.LOW_MARGIN + '% нет'));
  }

  function failBlock(id, error) {
    $(id).replaceChildren(node('p', 'fo-empty', error.message));
  }

  function loadDesk() {
    if (!chat) {
      chat = globalThis.RetroChat.mount({
        messages: $('fo-chat-messages'), form: $('fo-chat-form'), input: $('fo-chat-input'),
        status: $('fo-chat-status'), prompts: $('fo-chat-prompts'), endpoint: '/api/founder/chat',
      });
      chat.load();
    }
    load('week', '/api/founder/week?date=' + state.today).then(week => {
      renderWeek(week);
      renderChecks();
      setText('updated', 'Данные на ' + new Date().toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Tashkent'}));
    }).catch(error => failBlock('fo-week-table', error));
    load('spending', '/api/founder/spending?date=' + state.today).then(data => { renderSpending(data); renderChecks(); })
      .catch(error => failBlock('fo-exp-list', error));
    load('dishes', '/api/founder/dishes?days=7').then(renderDishes).catch(error => failBlock('fo-dishes', error));
  }

  function loadForLayout() {
    if (desk.matches) loadDesk(); else loadPhone().catch(() => {});
    renderEditor();
  }

  function bind() {
    $('fo-div-toggle').addEventListener('click', toggleEditor);
    $('k-div-edit').addEventListener('click', toggleEditor);
    $('fo-ask-checks').addEventListener('click', () => {
      const lines = (state.issues || []).slice(0, 15).map(item => '— ' + item.text + ' (' + item.sub + ')').join('\n');
      chat.ask('Разбери замечания к бухгалтеру за неделю: что критично, что можно игнорировать?\n' + lines);
    });
    document.querySelectorAll('.fo-subnav a[href^="#"]').forEach(link => link.addEventListener('click', () => {
      document.querySelectorAll('.fo-subnav a').forEach(other => other.classList.toggle('is-current', other === link));
    }));
    desk.addEventListener('change', loadForLayout);
  }

  (async function start() {
    state.today = new Date().toISOString().slice(0, 10);
    try {
      const config = await globalThis.RetroConfig;
      if (config && config.today) state.today = config.today;
    } catch (error) {
      setText('updated', error.message);
    }
    state.yesterday = shiftDay(state.today, -1);
    const date = new Date(state.today + 'T12:00:00Z');
    setText('fo-phone-date', date.toLocaleDateString('ru-RU', {weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC'}).replace(/^./, c => c.toUpperCase()));
    const month = state.today.slice(0, 7);
    $('fo-excel').href = '/api/founder/export/month?month=' + month;
    setText('fo-excel-label', 'Отчёт бухгалтера · ' + MONTHS[Number(month.slice(5, 7)) - 1]);
    bind();
    load('dividends', '/api/founder/dividends/weekly').then(renderDividends).catch(error => setText('fo-div-status', error.message));
    load('chef', '/api/founder/chef-account?date=' + state.today).then(renderChef).catch(error => renderChef({error: error.message}));
    load('forecast', '/api/founder/forecast?date=' + state.today).then(renderForecast).catch(error => renderForecast({error: error.message}));
    loadForLayout();
  })();
})();
