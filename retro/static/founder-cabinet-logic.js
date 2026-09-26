/* Расчёты кабинета учредителя без DOM: неделя по дням, сверка передачи,
   дивиденды, замечания к бухгалтеру. Сервер отдаёт факты, здесь — сборка
   для экрана, которую удобно проверить тестом. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.FounderCabinetLogic = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const WEEKDAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
  const MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
  const DIVIDEND_PRESETS = [5e6, 8e6, 10e6, 15e6, 20e6];
  // Шаг кнопок ± в редакторе недельной цели. Не путать с шагом округления
  // совета на сервере (DIVIDEND_SUGGEST_STEP в overview.py) — тот мельче.
  const DIVIDEND_EDIT_STEP = 500000;
  const CHEF_LIMIT = 400000;

  function num(value) {
    if (value === null || value === undefined || value === '') return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  const format = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 0});
  function sum(value) { return value === null || value === undefined ? '—' : format.format(Math.round(value)); }

  /** «42,5 млн» — короткая сумма для плиток и ячеек недели. */
  function short(value) {
    if (value === null || value === undefined) return '—';
    const n = Number(value);
    if (Math.abs(n) >= 1e9) return String(Math.round(n / 1e7) / 100).replace('.', ',') + ' млрд';
    if (Math.abs(n) >= 1e6) return String(Math.round(n / 1e5) / 10).replace('.', ',') + ' млн';
    return format.format(Math.round(n));
  }

  function plural(count, one, few, many) {
    const abs = Math.abs(Math.trunc(count)), tail = abs % 100, last = abs % 10;
    if (tail >= 11 && tail <= 14) return many;
    if (last === 1) return one;
    if (last >= 2 && last <= 4) return few;
    return many;
  }

  function dm(iso) { return iso ? iso.slice(8, 10) + '.' + iso.slice(5, 7) : '—'; }
  function dayWords(iso) { return Number(iso.slice(8, 10)) + ' ' + MONTHS[Number(iso.slice(5, 7)) - 1]; }

  /** Прогноз дня по среднему для его дня недели. Нет истории — нет прогноза. */
  function forecastFor(forecast, weekday) {
    if (!forecast || !forecast.weekdays) return null;
    return forecast.weekdays[weekday] || null;
  }

  /** Сверка передачи кассир → бухгалтер одной отметкой для строки недели. */
  function handoverMark(day) {
    if (!day || day.future) return {mark: '', tone: 'future', tip: ''};
    const handover = day.handover || {};
    if (handover.status === 'ok') return {mark: '✓', tone: 'ok', tip: 'Передача совпала с расчётом'};
    if (handover.status === 'mismatch') {
      return {mark: '⚠', tone: 'bad', tip: 'Передано ' + sum(num(handover.recorded)) + ' при расчёте ' +
        sum(num(handover.expected)) + ' · разница ' + sum(num(handover.difference))};
    }
    if (handover.status === 'pending') return {mark: '…', tone: 'wait', tip: 'День ещё идёт'};
    if (handover.status === 'missing') return {mark: '⚠', tone: 'bad', tip: 'Бухгалтер не записал передачу кассы'};
    return {mark: '?', tone: 'wait', tip: day.cashier_error || 'Нет данных iiko для сверки'};
  }

  /** Таблица «Неделя по дням»: показатель × семь дней + итог.
   *  Будущие дни выручки и чеков — прогноз со знаком «≈»; деньги бухгалтера
   *  в будущем не прогнозируем: пустая ячейка честнее выдуманной цифры. */
  function weekRows(week, forecast) {
    const days = (week && week.days) || [];
    const fromCashier = key => day => day.cashier ? num(day.cashier[key]) : null;
    const orders = day => day.orders ? (num(day.orders.retro) || 0) + (num(day.orders.school) || 0) : null;
    const flow = key => day => day.flows ? num(day.flows[key]) : null;
    const guess = key => day => {
      const value = forecastFor(forecast, day.weekday);
      return value ? num(value[key]) : null;
    };
    const specs = [
      {label: 'Retro · выручка', value: fromCashier('retro'), guess: guess('retro')},
      {label: 'Retro Oxbridge · выручка', value: fromCashier('school'), guess: guess('school')},
      {label: 'Чеки · оба заведения', value: orders, guess: guess('orders'), count: true},
      {label: 'Демо · только Retro', value: fromCashier('demo')},
      {label: 'Кассир передал', value: day => day.handover ? num(day.handover.recorded) : null},
      {label: '− Зарплаты', value: flow('salary')},
      {label: '− Закуп', value: flow('procurement')},
      {label: '− Прочие расходы', value: flow('other')},
      {label: '− Дивиденды', value: flow('dividends')},
      {label: '= Остаток у бухгалтера', value: day => num(day.closing_balance), balance: true},
    ];
    return specs.map(spec => {
      let total = 0, known = false, last = null;
      const cells = days.map(day => {
        if (day.future) {
          const value = spec.guess ? spec.guess(day) : null;
          return {text: value === null ? '' : '≈ ' + (spec.count ? sum(value) : short(value)), tone: 'future'};
        }
        const value = spec.value(day);
        if (value === null) return {text: '—', tone: 'none'};
        if (spec.balance) last = value; else { total += value; known = true; }
        return {text: spec.count ? sum(value) : short(value), tone: spec.balance && value < 0 ? 'bad' : ''};
      });
      const totalText = spec.balance ? short(last) : known ? (spec.count ? sum(total) : short(total)) : '—';
      return {label: spec.label, cells, total: totalText, balance: Boolean(spec.balance)};
    });
  }

  /** Итоги недели по дням, которые уже прошли или идут. */
  function weekTotals(week) {
    const days = ((week && week.days) || []).filter(day => !day.future && day.cashier);
    const total = key => days.reduce((acc, day) => acc + (num(day.cashier[key]) || 0), 0);
    const retro = total('retro'), school = total('school'), demo = total('demo');
    return {retro, school, demo, days: days.length,
      demoShare: retro ? Math.round(demo / retro * 100) : null};
  }

  /** Недельные дивиденды: подписи и доли для карточки и KPI. */
  function dividendView(data) {
    if (!data) return null;
    const target = num(data.target), collected = num(data.collected) || 0;
    const pace = num(data.pace), due = num(data.due);
    const status = target === null ? 'Цель на неделю не задана'
      : data.done ? 'Недельная сумма собрана'
      : data.behind ? 'Отстаём от плана на ' + sum(Math.round(due - collected))
      : 'Идём по плану';
    return {
      target, collected, status,
      tone: target === null ? 'none' : data.done ? 'ok' : data.behind ? 'warn' : 'ok',
      pct: target ? Math.min(100, collected / target * 100) : 0,
      pacePct: target && pace !== null ? Math.min(100, pace / target * 100) : 0,
      range: dm(data.start) + '–' + dm(data.end),
      payout: 'Выдача в понедельник, ' + dayWords(data.payout_day),
      free: num(data.free_cash_week),
      source: data.target_source,
    };
  }

  /** Шаг редактора цели: ±500 000, не ниже нуля. */
  function stepTarget(value, direction) {
    return Math.max(0, (Number(value) || 0) + direction * DIVIDEND_EDIT_STEP);
  }

  function parseAmount(text) {
    const digits = String(text || '').replace(/\D/g, '');
    return digits ? Number(digits) : 0;
  }

  /** Замечания к бухгалтеру за неделю: недостачи от кассира, проверки дня из
   *  «Финансов дня» и покупки Шоха, которые надо проверить. Одинаковые
   *  проверки разных дней не склеиваем — у каждой свой день. */
  function accountantIssues(week, spending, dayChecks) {
    const items = [];
    ((week && week.days) || []).forEach(day => {
      if (day.future || !day.handover) return;
      const handover = day.handover;
      if (handover.status === 'mismatch') {
        const diff = num(handover.difference);
        items.push({level: 'bad', text: (diff < 0 ? 'Кассир передал меньше расчёта' : 'Кассир передал больше расчёта') + ' · ' + dm(day.date),
          sub: 'Передано ' + sum(num(handover.recorded)) + ' при расчёте ' + sum(num(handover.expected))});
      } else if (handover.status === 'missing') {
        items.push({level: 'bad', text: 'Передача кассы не записана · ' + dm(day.date),
          sub: 'Расчёт кассира ' + sum(num(handover.expected))});
      }
      if (day.accounting && dayChecks) {
        dayChecks(day.accounting).forEach(check => {
          // Касса уже учтена сверкой выше, а дивиденды учредитель видит своей
          // карточкой — повторять их по каждому дню недели незачем.
          if (check.text === 'Касса не передана' || check.text === 'Отстаём от недельных дивидендов') return;
          const parts = [];
          if (check.sub && check.sub.count) parts.push(check.sub.count + ' шт');
          if (check.sub && check.sub.amount !== undefined) parts.push(sum(num(check.sub.amount)) + ' сум');
          items.push({level: check.level, text: check.text + ' · ' + dm(day.date), sub: parts.join(' · ') || 'Финансы дня'});
        });
      }
    });
    ((spending && spending.shokh && spending.shokh.flagged) || []).forEach(row => {
      items.push({level: 'warn', text: 'Покупка Шоха: ' + row.item + ' · ' + dm(row.day),
        sub: sum(num(row.total)) + ' сум · ' + row.reason});
    });
    return items.sort((a, b) => (a.level === 'bad' ? 0 : 1) - (b.level === 'bad' ? 0 : 1));
  }

  /** Счета Шефа выше порога за сегодня и вчера — «уведомления» на телефоне.
   *  Доставки push нет, поэтому это баннер в интерфейсе; закрытый баннер
   *  запоминается по номеру заказа. */
  function chefAlerts(chef, today, yesterday, dismissed) {
    if (!chef || !chef.bills) return [];
    const hidden = new Set(dismissed || []);
    return chef.bills.filter(bill => bill.over && (bill.day === today || bill.day === yesterday) && !hidden.has(bill.order_id));
  }

  /** «У бухгалтера к вечеру ≈»: утренний остаток плюс касса минус то, что уйдёт. */
  function eveningCash(today) {
    if (!today || !today.outlook) return null;
    const opening = num(today.opening_balance);
    if (opening === null) return null;
    const handover = today.cashier ? num(today.cashier.expected_handover) || 0 : 0;
    const out = (num(today.outlook.salary_due) || 0) + (num(today.outlook.procurement) || 0) + (num(today.outlook.other) || 0);
    return opening + handover - out;
  }

  // dayWords наружу не отдаём: он нужен только подписи выдачи внутри модуля.
  return {WEEKDAYS, DIVIDEND_PRESETS, DIVIDEND_EDIT_STEP, CHEF_LIMIT, num, sum, short, plural, dm,
    forecastFor, handoverMark, weekRows, weekTotals, dividendView, stepTarget, parseAmount,
    accountantIssues, chefAlerts, eveningCash};
});
