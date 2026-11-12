/* Демо-режим страниц учредителя: тестовые данные только для проверки вёрстки.
 *
 * Включается адресом `?demo=1` (данные) или `?demo=empty` (нет связи с iiko)
 * и только на компьютере разработчика — localhost, 127.0.0.1, *.localhost.
 * На рабочем сайте параметр ничего не делает.
 *
 * Как работает: подменяет `fetch` до того, как страница запросит данные, и
 * отвечает теми же структурами, что отдаёт сервер. Шаблоны и логика страниц
 * не знают, что данные тестовые. Запись (POST и др.) в /api/founder/ в демо
 * не уходит на сервер — отвечаем отказом. Все значения вымышленные. */
(() => {
  const params = new URLSearchParams(location.search);
  const mode = params.get('demo');
  const host = location.hostname;
  const local = host === 'localhost' || host === '127.0.0.1' || host === '[::1]' || host.endsWith('.localhost');
  if (!local || (mode !== '1' && mode !== 'empty')) return;

  const empty = mode === 'empty';
  const TODAY = '2026-11-10', YESTERDAY = '2026-11-09', WEEK_START = '2026-11-09';
  const IIKO_OFF = 'Подключение iiko ещё не настроено. Нужен файл build/.env.';
  const realFetch = globalThis.fetch.bind(globalThis);

  // ── Даты ──────────────────────────────────────────────────────────────
  function shift(iso, days) {
    const value = new Date(iso + 'T12:00:00Z');
    value.setUTCDate(value.getUTCDate() + days);
    return value.toISOString().slice(0, 10);
  }
  function between(start, end) {
    const days = [];
    for (let day = start; day <= end; day = shift(day, 1)) days.push(day);
    return days;
  }
  const weekdayOf = iso => (new Date(iso + 'T12:00:00Z').getUTCDay() + 6) % 7;
  /** Повторяемое «случайное» число 0..1 для даты: перезагрузка даёт те же цифры. */
  function noise(iso, salt) {
    const n = Number(iso.replace(/-/g, '')) * 9301 + (salt || 0) * 49297;
    return (n % 233280) / 233280;
  }
  const money = value => String(Math.round(value));

  // ── Дни кассы и бухгалтера ────────────────────────────────────────────
  function movements() {
    // 109 операций за сегодня: передача кассира, выплаты зарплаты, расходы.
    const list = [{type: 'cashier_transfer', description: 'Передача кассира за 09.11', amount: '30122000'}];
    for (let i = 1; i <= 60; i += 1) list.push({type: 'salary_payment', description: 'Зарплата · сотрудник ' + i, amount: '480000'});
    for (let i = 1; i <= 48; i += 1) list.push({type: 'other_expense', description: 'Расход ' + i, amount: i === 48 ? '440000' : '450000'});
    return list;
  }

  function day(date) {
    const today = date === TODAY;
    const cashier = empty ? null : today ? {
      retro: '2700000', school: '1300000', demo: '1100000', fetched_at: TODAY + 'T14:11:00+05:00',
      shift: {open: true}, expected_handover: null,
      report: {revenue: '4000000', receipts_count: 59, average: '67797',
        payments: [{name: 'Наличные', amount: '2300000'}, {name: 'Uzcard', amount: '900000'}, {name: 'Click', amount: '800000'}],
        demo_cash: '2300000', cash_prepayment: '0', receipts: '0', expenses: '0', shokh: '0', usd_total: '0', handover: '2300000',
        prepayments: {count: 0, total: '0', methods: []}, issues: []},
    } : {
      retro: '28790500', school: '1701000', demo: '16400000', fetched_at: TODAY + 'T06:00:00+05:00',
      shift: {open: false, closed_at: YESTERDAY + 'T23:40:00+05:00'}, expected_handover: '30122000',
      report: {revenue: '30491500', receipts_count: 142, average: '214729',
        payments: [{name: 'Наличные', amount: '18200000'}, {name: 'Uzcard', amount: '6100000'}, {name: 'Humo', amount: '3400000'},
          {name: 'Click', amount: '1291500'}, {name: 'Payme', amount: '1500000'}],
        demo_cash: '18200000', cash_prepayment: '1500000', receipts: '11300000', expenses: '478000', shokh: '400000',
        usd_total: '0', handover: '30122000',
        prepayments: {count: 2, total: '1500000', methods: [{name: 'Uzcard', amount: '1500000'}]}, issues: []},
    };
    // Дни раньше вчерашнего (тот же день недели неделю назад) — касса поменьше, чтобы было видно сравнение.
    if (cashier && date < YESTERDAY) cashier.report = {...cashier.report, revenue: '27224553'};
    const accounting = today ? {
      date: TODAY,
      ledger: {cash_flow: {opening_balance: '22937000', received_from_cashier: '30122000', received_counted: '30122000',
        other_receipts: '0', salary_paid: '28800000', other_outflows: '21590000', closing_balance: '2669000'}, movements: movements()},
      day_report: null,
    } : {
      date: YESTERDAY,
      ledger: {cash_flow: {opening_balance: '23715000', received_from_cashier: '30122000', received_counted: '30122000',
        other_receipts: '0', salary_paid: '26000000', other_outflows: '4900000', closing_balance: '22937000'}, movements: []},
      day_report: {submitted_at: TODAY + 'T12:52:00+05:00', changed: false},
    };
    return {
      date, cashier, cashier_error: empty ? IIKO_OFF : null,
      handover: today ? {status: 'pending', recorded: null}
        : {status: 'ok', recorded: '30122000', expected: '30122000', difference: '0', confirmed: true},
      opening_balance: today ? '22937000' : '23715000',
      closing_balance: today ? '2669000' : '22937000',
      outlook: today ? {salary_due: '4500000', procurement: '6200000', other: '1100000', evening: '1869000'} : null,
      accounting, usd: {safe_balance: '400', deposits: []},
    };
  }

  /** Неделя (пн–вс) с датой `date`. Текущая — из дней выше; прошлые — сгенерированы. */
  function week(date) {
    const start = shift(date, -weekdayOf(date));
    if (start !== WEEK_START) {
      const days = between(start, shift(start, 6)).map(iso => {
        if (iso > TODAY) return {date: iso, weekday: weekdayOf(iso), future: true};
        const total = (30 + Math.round(noise(iso, 3) * 30)) * 1e6;
        const school = Math.round(total * .055);
        return {date: iso, weekday: weekdayOf(iso), future: false,
          cashier: {retro: money(total - school), school: money(school), demo: money(total * .52)},
          orders: {retro: 120 + Math.round(noise(iso, 4) * 60), school: 40}, closing_balance: '20000000',
          handover: {status: 'ok', recorded: money(total * .9), expected: money(total * .9), difference: '0', confirmed: true},
          flows: {salary: '4000000', procurement: '2000000', other: '1500000', dividends: '0'}};
      });
      return {start, end: shift(start, 6), days};
    }
    const days = between(WEEK_START, shift(WEEK_START, 6)).map(date => {
      const future = date > TODAY;
      if (future) return {date, weekday: weekdayOf(date), future: true};
      const value = day(date);
      return {date, weekday: weekdayOf(date), future: false, cashier: value.cashier, cashier_error: value.cashier_error,
        orders: date === TODAY ? {retro: 38, school: 21} : {retro: 131, school: 42}, handover: value.handover,
        flows: date === TODAY ? {salary: '28800000', procurement: '0', other: '21590000', dividends: '0'}
          : {salary: '26000000', procurement: '2400000', other: '2500000', dividends: '0'},
        closing_balance: value.closing_balance};
    });
    return {start: WEEK_START, end: shift(WEEK_START, 6), days};
  }

  const FORECAST_DAYS = [['Пн', 38e6, 3.5e6, 240], ['Вт', 40e6, 3.7e6, 280], ['Ср', 41e6, 3.8e6, 285], ['Чт', 43e6, 3.9e6, 300],
    ['Пт', 52e6, 4.1e6, 360], ['Сб', 61e6, 2.2e6, 410], ['Вс', 58e6, 1.9e6, 390]];
  function forecast() {
    const weekdays = FORECAST_DAYS.map(([label, retro, school, orders], weekday) =>
      ({weekday, label, retro: money(retro), school: money(school), revenue: money(retro + school), orders}));
    return {weekdays, sample_weeks: {min: 8, max: 8},
      today: {orders: {retro: 38, school: 21}, forecast: {retro: '40000000', school: '3700000', orders: 280}},
      month: {total: '1688900000', fact: '484900000', forecast: '1204000000'}};
  }

  const DIVIDENDS = {week: WEEK_START, start: WEEK_START, end: shift(WEEK_START, 6), target: '10000000', collected: '0',
    pace: '2857143', done: false, behind: true, payout_day: '2026-11-16', free_cash_week: '12400000',
    target_source: {inherited: true},
    history: [
      {start: '2026-11-02', end: '2026-11-08', target: '10000000', collected: '10000000', done: true, paid_out: '10000000', payout_day: '2026-11-09'},
      {start: '2026-10-26', end: '2026-11-01', target: '10000000', collected: '7500000', done: false, paid_out: '7500000', payout_day: '2026-11-02'},
    ]};

  // Состав счёта — только в демо, с пометкой «пример». В ответе /chef-account состава пока нет;
  // поле `dishes` — как у заказов Счёта Шефа в push-уведомлении сервера (iiko.chef_orders_from_olap).
  const BILLS = [
    ['1024', ['Баходиров Иктиер'], 620000, [['Шашлык из баранины', 4, 180000], ['Плов', 2, 170000], ['Лимонад 1 л', 2, 120000],
      ['Салат «Ачичук»', 3, 90000], ['Лепёшка', 4, 60000]]],
    ['8', ['Баходиров Иктиер'], 206000], ['32', ['Баходир'], 103000], ['1028', ['Малика касса'], 66000],
    ['2', ['Баходир', 'Музаффар'], 59000], ['1', ['Баходир'], 36000], ['1025', ['Аббос', 'Алина', 'Азиз'], 30000],
  ].map(([table, waiters, amount, items], index) => ({
    order_id: 'demo-' + (index + 1), day: YESTERDAY, table, waiters, amount: money(amount), over: amount > 400000,
    ...(items ? {dishes: items.map(([name, quantity, sum]) => ({name, quantity: String(quantity), amount: money(sum)})), dishes_sample: true} : {}),
  }));
  const CHEF = {limit: '400000', week_total: '1120000', week_over: 1, month_total: '8500000', week: BILLS, bills: BILLS,
    week_alert: {total: '1120000', limit: '30000000', percent: 3.7, reached: false, recipient: true, last: null}};

  const REPORTS = {
    closures: [{month: '2026-10', name: 'Октябрь 2026', closing_balance: '23715000', closed_at: '2026-11-01T18:20:00+05:00',
      closed_by: 'Малика', debts: {salary: '0', expenses: '0'}, remarks: []}],
    reports: [{day: YESTERDAY, submitted_at: TODAY + 'T12:52:00+05:00', submitted_by: 'Малика', changed: false,
      flow: {opening: '23715000', handover_counted: '30122000', outflows: '30900000', closing: '22937000'}}],
  };

  const SPENDING = {month: '2026-11',
    expenses: {total: '50390000', categories: [
      {label: 'Зарплаты', amount: '28800000', share_percent: '57.2'}, {label: 'Закуп · Шох', amount: '8400000', share_percent: '16.7'},
      {label: 'Коммунальные', amount: '6200000', share_percent: '12.3'}, {label: 'Хозтовары', amount: '4390000', share_percent: '8.7'},
      {label: 'Дивиденды', amount: '2600000', share_percent: '5.2'}]},
    shokh: {given: '8400000', spent: '7950000', transfers: '3100000', direct: '0', pocket: '450000', source: 'invoices', flagged: [],
      top_items: [{item: 'Баранина', amount: '3200000'}, {item: 'Овощи', amount: '1850000'}, {item: 'Рис девзира', amount: '1200000'}]}};

  const dish = (revenue, cost, quantity) => ({revenue: money(revenue), cost: money(cost), gross_profit: money(revenue - cost), quantity});
  const DISHES = {period_start: '2026-11-03', period_end: YESTERDAY, item_classes_checked: true, item_classes: {},
    item_metrics: {all: {'Плов': dish(42e6, 15e6, 490), 'Шашлык из баранины': dish(38e6, 29.6e6, 845),
      'Лимонад 1 л': dish(14e6, 3e6, 233), 'Салат «Ачичук»': dish(9e6, 2.4e6, 300), 'Лепёшка': dish(6e6, 1.5e6, 400),
      'Манты': dish(11e6, 4.2e6, 260)}}};

  // ── Аналитика: выручка по дням, сверка оплат, P&L, брони ─────────────
  const PAYMENTS = [['Наличные', .6], ['Uzcard', .2], ['Humo', .12], ['Click', .08]];
  function revenueDay(date) {
    const total = (35 + Math.round(noise(date, 1) * 60)) * 1e6;           // 35–95 млн
    const banquet = weekdayOf(date) === 6 ? Math.round(total * .06) : 0;  // банкеты — по воскресеньям
    const school = Math.round((total - banquet) * .035);
    return {retro: total - banquet - school, school, banquet};
  }
  /** Группы периода как у сервера: дни, недели с понедельника или месяцы, края по периоду. */
  function groups(start, end, unit) {
    const keyOf = date => unit === 'week' ? shift(date, -weekdayOf(date)) : unit === 'month' ? date.slice(0, 7) : date;
    const map = new Map();
    between(start, end).forEach(date => {
      const key = keyOf(date);
      if (!map.has(key)) map.set(key, {start: date, end: date, days: []});
      const group = map.get(key);
      group.end = date;
      group.days.push(date);
    });
    return [...map.values()];
  }

  function analytics(query) {
    const start = query.get('start'), end = query.get('end'), unit = query.get('granularity') || 'day';
    const directions = (query.get('directions') || 'retro,school,banquet').split(',').filter(Boolean);
    const all = ['retro', 'school', 'banquet'];
    const totals = {retro: 0, school: 0, banquet: 0};
    const revenueSeries = [], paymentSeries = [];
    groups(start, end, unit).forEach(group => {
      const values = {retro: 0, school: 0, banquet: 0};
      group.days.forEach(date => { const value = revenueDay(date); all.forEach(key => { values[key] += value[key]; }); });
      all.forEach(key => { totals[key] += values[key]; });
      const incomplete = group.end >= TODAY;
      revenueSeries.push({start: group.start, end: group.end, incomplete,
        values: Object.fromEntries(all.map(key => [key, money(values[key])])),
        total: money(directions.reduce((acc, key) => acc + values[key], 0))});
      paymentSeries.push({start: group.start, end: group.end, incomplete,
        directions: Object.fromEntries(all.map(key => [key, Object.fromEntries(PAYMENTS.map(([name, share]) => [name, money(values[key] * share)]))]))});
    });
    const selected = directions.reduce((acc, key) => acc + totals[key], 0);
    const restaurant = totals.retro + totals.school + totals.banquet;
    const prepaid = Math.round(selected * .0612);
    const cost = Math.round(restaurant * .32), gross = restaurant - cost;
    const operating = Math.round(restaurant * .37), otherExpenses = Math.round(restaurant * .0566), otherIncome = Math.round(restaurant * .0069);
    const net = gross - operating - otherExpenses + otherIncome;
    const dashboard = {cashier: 18400000, accountant: 50390000, accountant_salary: 28800000, accountant_other: 21590000, total: 68790000};
    return {
      period: {start, end}, granularity: unit, directions,
      totals: {...Object.fromEntries(all.map(key => [key, money(totals[key])])), selected: money(selected)},
      yandex_totals: {retro: money(totals.retro * .039)},
      revenue_series: revenueSeries, payment_series: paymentSeries, payment_summary: PAYMENTS.map(([name]) => ({name})),
      sales_bridge: {reconciled: true, unknown_operations: [],
        totals: {paid: money(selected - prepaid), prepaid: money(prepaid), other: '0', sales: money(selected)},
        payments: PAYMENTS.map(([name, share]) => ({name, paid: money((selected - prepaid) * share), prepaid: money(prepaid * share),
          other: '0', sales: money(selected * share)}))},
      pnl: {sales: money(restaurant), cost: money(cost), gross_profit: money(gross), net_profit: money(net),
        operating_expenses: money(operating), other_expenses: money(otherExpenses), other_income: money(otherIncome)},
      dashboard_expenses: Object.fromEntries(Object.entries(dashboard).map(([key, value]) => [key, money(value)])),
      net_profit_after_dashboard_expenses: money(net - dashboard.total),
      internal_costs: {tasting: '3200000', chef_account: '8500000'},
      reconciled: true, warnings: [], scope_note: '', includes_current_day: end >= TODAY, updated_at: TODAY + 'T14:11:00+05:00',
    };
  }

  /** Разложить целое по группам с весами — сумма групп ровно равна итогу. */
  function spread(total, weights) {
    const sum = weights.reduce((a, b) => a + b, 0) || 1;
    const parts = weights.map(weight => Math.floor(total * weight / sum));
    let rest = total - parts.reduce((a, b) => a + b, 0);
    for (let i = 0; rest > 0; i = (i + 1) % parts.length, rest -= 1) parts[i] += 1;
    return parts;
  }
  function bookings(query) {
    const list = groups(query.get('start'), query.get('end'), query.get('granularity') || 'day');
    const weights = list.map(group => group.days.reduce((acc, date) => acc + 1 + noise(date, 2), 0));
    const active = spread(318, weights), cancelled = spread(47, weights), guests = spread(1260, weights);
    return {
      totals: {all: 365, bookings: 318, cancelled: 47, guests: 1260, unknown_guest_bookings: 0},
      series: list.map((group, i) => ({start: group.start, end: group.end, incomplete: group.end >= TODAY,
        values: {bookings: active[i], cancelled: cancelled[i], guests: guests[i]}})),
      sources: [{name: 'Telegram-бот', all: 241, bookings: 212, cancelled: 29, guests: 840, share_percent: 66},
        {name: 'Instagram', all: 84, bookings: 70, cancelled: 14, guests: 290, share_percent: 23},
        {name: 'Сайт', all: 40, bookings: 36, cancelled: 4, guests: 130, share_percent: 11}],
      coverage: {sources: [{name: 'Telegram-бот', history_started_at: '2026-09-01T00:00:00+05:00'}],
        historical_data_complete: true, excluded_missing_date: 0},
    };
  }
  function guests(query) {
    const list = groups(query.get('start'), query.get('end'), query.get('granularity') || 'day');
    const counts = spread(1480, list.map(group => group.days.length));
    return {totals: {retro: {guests: 1480, checks: 3920}},
      series: list.map((group, i) => ({start: group.start, end: group.end, values: {guests: counts[i]}}))};
  }
  const AUDIENCE = {subscribers: 860, profiles: 1120, recipients: Array.from({length: 860}, (_, i) =>
    ({id: 'demo-' + (i + 1), name: 'Тестовый гость ' + (i + 1), phone: '+998 00 000 ' + String(i + 1).padStart(4, '0')}))};

  // ── Маршруты ──────────────────────────────────────────────────────────
  const fail = detail => ({status: 503, body: {detail}});
  const ok = body => ({status: 200, body});
  /** Ответ демо на GET; `null` — запрос уходит на сервер как обычно. */
  function answer(path, query) {
    switch (path) {
      case '/api/founder/dividends/weekly': return ok(DIVIDENDS);
      case '/api/founder/accountant-reports': return ok(REPORTS);
      case '/api/founder/spending': return ok(SPENDING);
      case '/api/founder/day': return ok(day(query.get('date') || TODAY));
      case '/api/founder/week': return empty ? fail(IIKO_OFF) : ok(week(query.get('date') || TODAY));
      case '/api/founder/chef-account': return empty ? fail(IIKO_OFF) : ok(CHEF);
      case '/api/founder/forecast': return empty ? fail(IIKO_OFF) : ok(forecast());
      case '/api/founder/dishes': return empty ? fail(IIKO_OFF) : ok(DISHES);
      case '/api/founder/analytics': return empty ? fail(IIKO_OFF) : ok(analytics(query));
      case '/api/founder/guests': return empty ? fail(IIKO_OFF) : ok(guests(query));
      case '/api/founder/bookings': return empty ? fail('API бронирований ещё не подключён') : ok(bookings(query));
      case '/api/founder/broadcast/audience': return empty ? fail('Бот брони ещё не подключён') : ok(AUDIENCE);
      case '/api/founder/broadcast/jobs': return ok({supported: true, jobs: [], next_cursor: null});
      case '/api/founder/broadcast/journal': return ok({entries: []});
      default: return null;
    }
  }

  function respond({status, body}) {
    // Небольшая задержка: видно скелеты загрузки, как с настоящим сервером.
    return new Promise(resolve => setTimeout(() => resolve(new Response(JSON.stringify(body),
      {status, headers: {'Content-Type': 'application/json'}})), 350));
  }

  globalThis.fetch = async (input, init) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    const method = ((init && init.method) || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
    if (url.origin !== location.origin) return realFetch(input, init);
    if (url.pathname.startsWith('/api/founder/') && method !== 'GET') {
      return respond({status: 403, body: {detail: 'Демо-режим: сохранение отключено, тестовые данные никуда не отправляются.'}});
    }
    if (url.pathname === '/api/config') {
      // Настоящая сессия и меню, но «сегодня» — дата тестовых данных.
      const response = await realFetch(input, init);
      if (!response.ok) return response;
      const config = await response.json();
      return respond({status: 200, body: {...config, today: TODAY, configured: !empty}});
    }
    const demo = method === 'GET' && answer(url.pathname, url.searchParams);
    return demo ? respond(demo) : realFetch(input, init);
  };
  globalThis.RetroDemo = {mode};

  // Аналитика пишет фильтры в адрес (history.replaceState) — `demo` в нём сохраняем,
  // иначе после обновления страницы демо выключалось бы.
  const replaceState = history.replaceState.bind(history);
  history.replaceState = (data, unused, url) => {
    if (url !== undefined && url !== null) {
      const next = new URL(url, location.href);
      next.searchParams.set('demo', mode);
      url = next.pathname + next.search + next.hash;
    }
    return replaceState(data, unused, url);
  };

  // Плашка «Тестовые данные» — чтобы скриншоты не перепутали с настоящими.
  document.addEventListener('DOMContentLoaded', () => {
    const banner = document.createElement('div');
    banner.className = 'demo-banner';
    banner.setAttribute('role', 'status');
    banner.textContent = empty ? 'Тестовые данные · нет связи с iiko' : 'Тестовые данные';
    banner.style.cssText = 'position:relative;z-index:1000;padding:6px 16px;background:#6A3E0A;color:#fff;' +
      'font:600 12px/1.4 system-ui,sans-serif;text-align:center;letter-spacing:.02em';
    // В <main>, а не в <body>: у body на компьютере колонки «меню | страница».
    (document.querySelector('main') || document.body).prepend(banner);
  });
})();
