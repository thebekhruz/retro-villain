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
    if (handover.status === 'ok') {
      return {mark: '✓', tone: 'ok', tip: handover.confirmed ? 'Бухгалтер подтвердил: получено ' + sum(num(handover.recorded)) +
        ' — совпало с расчётом' : 'Передача совпала с расчётом'};
    }
    if (handover.status === 'mismatch') {
      if (handover.confirmed) {
        return {mark: '⚠', tone: 'bad', tip: 'Получено ' + sum(num(handover.recorded)) + ' при расчёте ' +
          sum(num(handover.expected)) + ' · недостача ' + sum(num(handover.shortfall))};
      }
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
    // «Отстаём» решает сервер (§3.7: меньше 85 % плана к сегодня, включая
    // сегодня); подпись — насколько отложенное меньше этого плана.
    const status = target === null ? 'Цель на неделю не задана'
      : data.done ? 'Недельная сумма собрана'
      : data.behind ? 'Отстаём от плана на ' + sum(Math.round(Math.max(0, pace - collected)))
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
      history: (data.history || []).map(week => ({
        range: dm(week.start) + '–' + dm(week.end),
        text: week.target === null ? 'собрано ' + short(num(week.collected)) + ' · цели не было'
          : 'собрано ' + short(num(week.collected)) + ' из ' + short(num(week.target)),
        paid: num(week.paid_out) ? 'выдано ' + dm(week.payout_day) + ' · ' + short(num(week.paid_out)) : '',
        tone: week.target === null ? 'none' : week.done ? 'ok' : 'warn',
      })),
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

  // Проверки, которые описывают состояние на сегодня, а не событие дня:
  // «Без ставки» или долг по старым сменам в каждом дне недели повторялись бы
  // одним и тем же пунктом. Такие берём по последнему дню, где они есть.
  const STANDING_CHECKS = new Set(['Невыданные смены', 'Без ставки', 'Неоплаченные расходы']);

  /** Замечания к бухгалтеру за неделю (7b, Функционал §4): недостача от кассира —
   *  первой, затем остальные ошибки, затем предупреждения. Источники: сверка
   *  передачи, проверки дня из «Финансов дня», смены, выданные без входа, и
   *  покупки Шоха на проверку. */
  function accountantIssues(week, spending, dayChecks) {
    const items = [];
    const standing = new Map();
    const days = ((week && week.days) || []).filter(day => !day.future);
    days.forEach(day => {
      if (!day.handover) return;
      const handover = day.handover;
      if (handover.status === 'mismatch') {
        const diff = num(handover.difference);
        const text = handover.confirmed ? (diff < 0 ? 'От кассира получено меньше расчёта' : 'От кассира получено больше расчёта')
          : diff < 0 ? 'Кассир передал меньше расчёта' : 'Кассир передал больше расчёта';
        items.push({level: 'bad', rank: diff < 0 ? 0 : 1, text: text + ' · ' + dm(day.date),
          sub: (handover.confirmed ? 'Получено ' : 'Передано ') + sum(num(handover.recorded)) + ' при расчёте ' + sum(num(handover.expected))});
      } else if (handover.status === 'missing') {
        items.push({level: 'bad', rank: 1, text: 'Передача кассы не записана · ' + dm(day.date),
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
          const item = {level: check.level, rank: check.level === 'bad' ? 1 : 2, text: check.text + ' · ' + dm(day.date),
            sub: parts.join(' · ') || 'Финансы дня'};
          if (STANDING_CHECKS.has(check.text)) standing.set(check.text, item); else items.push(item);
        });
      }
    });
    items.push(...standing.values());
    // «Сменному выдано без входа» (ошибка): начисление 0 — входа не было, а
    // деньги выданы. Начисления за неделю берём из журнала последнего дня.
    const last = days.filter(day => day.accounting && day.accounting.ledger).at(-1);
    if (last && week.start) {
      const from = dayBefore(week.start);
      (last.accounting.ledger.accruals || []).forEach(row => {
        if (row.work_day < from || row.work_day > last.date) return;
        if (num(row.amount) === 0 && num(row.paid) > 0) {
          items.push({level: 'bad', rank: 1, text: 'Выдано без входа: ' + row.name + ' · ' + dm(row.work_day),
            sub: 'Смена ' + dm(row.work_day) + ' · ' + sum(num(row.paid)) + ' сум'});
        }
      });
    }
    ((spending && spending.shokh && spending.shokh.flagged) || []).forEach(row => {
      items.push({level: 'warn', rank: 2, text: 'Покупка Шоха: ' + row.item + ' · ' + dm(row.day),
        sub: sum(num(row.total)) + ' сум · ' + row.reason});
    });
    return items.sort((a, b) => a.rank - b.rank).map(({rank, ...item}) => item);
  }

  function dayBefore(iso) {
    const value = new Date(iso + 'T12:00:00Z');
    value.setUTCDate(value.getUTCDate() - 1);
    return value.toISOString().slice(0, 10);
  }

  /** Счета Шефа выше порога за сегодня и вчера — «уведомления» на телефоне.
   *  Доставки push нет, поэтому это баннер в интерфейсе; закрытый баннер
   *  запоминается по номеру заказа. */
  function chefAlerts(chef, today, yesterday, dismissed) {
    if (!chef || !chef.bills) return [];
    const hidden = new Set(dismissed || []);
    return chef.bills.filter(bill => bill.over && (bill.day === today || bill.day === yesterday) && !hidden.has(bill.order_id));
  }

  // dayWords наружу не отдаём: он нужен только подписи выдачи внутри модуля.
  return {WEEKDAYS, DIVIDEND_PRESETS, DIVIDEND_EDIT_STEP, CHEF_LIMIT, num, sum, short, plural, dm,
    forecastFor, handoverMark, weekRows, weekTotals, dividendView, stepTarget, parseAmount,
    accountantIssues, chefAlerts};
});
