/* KPI официантов — логика без DOM: суммы, прогресс, подписи, спарклайн.
   Тот же файл читает кабинет менеджера (вкладка «Неделя») и телефон директора
   (карточка и настройка периода); проверяется tests/js/kpi-logic.test.mjs. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.KpiLogic = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября',
    'октября', 'ноября', 'декабря'];
  const DAYS = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье'];

  /* 2 884 000 → «2 884 тыс», 12 400 000 → «12,4 млн», 0 → «0». */
  function thousands(value) {
    const n = Math.round(Number(value) || 0);
    const abs = Math.abs(n);
    if (abs >= 10_000_000) return (n / 1_000_000).toFixed(1).replace('.', ',').replace(',0', '') + ' млн';
    if (abs >= 1000) return Math.round(n / 1000).toLocaleString('ru-RU').replace(/ /g, ' ') + ' тыс';
    return String(n);
  }
  function signed(value) {
    const n = Math.round(Number(value) || 0);
    if (n === 0) return '0';
    return (n > 0 ? '+' : '−') + thousands(Math.abs(n));
  }
  function rate(value) {
    return (Math.round((Number(value) || 0) * 10) / 10).toFixed(1).replace('.', ',');
  }
  function percent(value) {
    return Math.round((Number(value) || 0) * 100) + ' %';
  }

  /* «19–25 октября», «26 октября – 1 ноября», «1–30 ноября». */
  function periodLabel(start, end) {
    const a = new Date(start + 'T00:00:00'), b = new Date(end + 'T00:00:00');
    if (a.getMonth() === b.getMonth()) return `${a.getDate()}–${b.getDate()} ${MONTHS[a.getMonth()]}`;
    return `${a.getDate()} ${MONTHS[a.getMonth()]} – ${b.getDate()} ${MONTHS[b.getMonth()]}`;
  }
  /* «Четверг, 4-й день из 7», до старта — «Старт в понедельник 19 октября», после — «Период завершён». */
  function dayLabel(period) {
    const today = new Date(period.today + 'T00:00:00');
    const start = new Date(period.start + 'T00:00:00');
    if (today < start) {
      const name = DAYS[(start.getDay() + 6) % 7];
      return `Старт: ${name}, ${start.getDate()} ${MONTHS[start.getMonth()]}`;
    }
    if (period.days_passed >= period.days_total && period.today > period.end) return 'Период завершён';
    const name = DAYS[(today.getDay() + 6) % 7];
    return `${name[0].toUpperCase()}${name.slice(1)}, ${period.days_passed}-й день из ${period.days_total}`;
  }
  /* «данные iiko на 14:32» или «на 14.10, 23:50», если снимок не сегодняшний. */
  function fetchedLabel(iso, today) {
    if (!iso) return 'данных iiko ещё нет';
    const time = iso.slice(11, 16);
    const day = iso.slice(0, 10);
    if (today && day !== today) return `данные iiko на ${day.slice(8, 10)}.${day.slice(5, 7)}, ${time}`;
    return `данные iiko на ${time}`;
  }

  /* Прогресс зала к цели периода, 0–100. */
  function goalProgress(totals) {
    if (!totals || !totals.goal) return 0;
    return Math.max(0, Math.min(100, Math.round(totals.team_uplift / totals.goal * 100)));
  }
  /* Прогресс человека: участнику — его прирост против лучшего в списке, остальным — путь до порога участия. */
  function personProgress(row, rows, scheme) {
    if (!row.eligible) {
      const checks = scheme?.min_checks ? row.checks / scheme.min_checks : 1;
      const shifts = scheme?.min_shifts ? row.shifts / scheme.min_shifts : 1;
      return Math.max(0, Math.min(100, Math.round(Math.min(checks, shifts) * 100)));
    }
    const top = Math.max(0, ...rows.filter(r => r.eligible).map(r => r.premium || 0));
    if (!top || !(row.premium > 0)) return 0;
    return Math.max(4, Math.min(100, Math.round(row.premium / top * 100)));
  }
  /* Подпись под именем: участнику — прирост и чеки, остальным — сколько не хватает до участия. */
  function personLine(row, scheme) {
    const checks = `${row.checks} ${plural(row.checks, 'чек', 'чека', 'чеков')}`;
    if (row.eligible) return `${checks} · премия ${thousands(row.premium)}`;
    const needChecks = Math.max(0, (scheme?.min_checks || 0) - row.checks);
    const needShifts = Math.max(0, (scheme?.min_shifts || 0) - row.shifts);
    const parts = [];
    if (needChecks) parts.push(`${needChecks} ${plural(needChecks, 'чек', 'чека', 'чеков')}`);
    if (needShifts) parts.push(`${needShifts} ${plural(needShifts, 'смена', 'смены', 'смен')}`);
    return `${checks} · ${row.shifts} ${plural(row.shifts, 'смена', 'смены', 'смен')}` + (parts.length ? ' · ещё ' + parts.join(' и ') : '');
  }
  function plural(n, one, few, many) {
    const m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return few;
    return many;
  }
  function baseLabel(row) {
    return row.own_base ? 'база своя' : 'база зала';
  }
  function itemTone(item) {
    if (item.now > item.base) return 'is-up';
    if (item.now < item.base) return 'is-down';
    return '';
  }
  function itemProgress(item) {
    if (!item.target) return 0;
    return Math.max(0, Math.min(100, Math.round(item.now / item.target * 100)));
  }
  /* Путь спарклайна по дням периода: порций позиции на 100 чеков человека в день. */
  function sparkPath(item, row, period, width = 60, height = 24) {
    const days = [];
    const [year, month, date] = period.start.split('-').map(Number);
    const startUtc = Date.UTC(year, month - 1, date);
    for (let i = 0; i < period.days_total; i += 1) {
      const day = new Date(startUtc + i * 86400000).toISOString().slice(0, 10);
      const checks = row.checks_by_day?.[day] || 0;
      const qty = item.by_day?.[day] || 0;
      days.push(checks ? qty * 100 / checks : null);
    }
    const known = days.filter(v => v !== null);
    if (known.length < 2) return '';
    const max = Math.max(...known, item.target || 0, 0.1);
    const step = width / Math.max(days.length - 1, 1);
    let path = '', pen = false;
    days.forEach((value, index) => {
      if (value === null) { pen = false; return; }
      const x = (index * step).toFixed(1);
      const y = (height - 2 - value / max * (height - 4)).toFixed(1);
      path += `${pen ? 'L' : 'M'}${x},${y} `;
      pen = true;
    });
    return path.trim();
  }
  /* Сортировка рейтинга: участники по приросту, потом остальные по чекам. Без «красных» отметок. */
  /* Сортировка списка: участники по премии, потом по проданным порциям; остальные по чекам. Без «красных». */
  function ranking(rows) {
    return rows.slice().sort((a, b) => {
      if (a.eligible !== b.eligible) return a.eligible ? -1 : 1;
      if (a.eligible) return (b.premium - a.premium) || ((b.sold || 0) - (a.sold || 0)) || a.waiter.localeCompare(b.waiter, 'ru');
      return b.checks - a.checks || a.waiter.localeCompare(b.waiter, 'ru');
    });
  }
  function initials(name) {
    return (name || '').split(/[\s|]+/).filter(Boolean).slice(0, 2).map(p => p[0]).join('').toUpperCase();
  }
  /* Настройки: из полей формы в тело PUT /settings; ошибки — словами. */
  function settingsFromForm(form) {
    const number = (value, name) => {
      const n = Number(String(value ?? '').replace(/\s/g, '').replace(',', '.'));
      if (!Number.isFinite(n)) throw new Error(`Поле «${name}»: нужно число.`);
      return n;
    };
    const share = number(form.share, 'доля прироста') / 100;
    return {
      kind: form.kind === 'month' ? 'month' : 'week',
      start: form.start, end: form.end,
      scheme: {share, cap: number(form.cap, 'потолок') * 1000,
        min_checks: number(form.min_checks, 'минимум чеков'), min_shifts: number(form.min_shifts, 'минимум смен'),
        baseline_weeks: number(form.baseline_weeks ?? 4, 'база'), netting: Boolean(form.netting)},
      targets: (form.targets || []).map((t, index) => ({
        key: t.key || `t${index + 1}`, title: t.title, group: t.group || 'блюдо',
        target100: number(t.target100, 'цель'), margin: t.margin ? number(t.margin, 'маржа') : null,
        dishes: (t.dishes || []).map(d => [d.id ?? d[0], d.name ?? d[1]]),
      })),
    };
  }
  /* ── Кольцо прогресса (главная менеджера): продано целевых позиций против цели периода в штуках. ── */
  function ringPercent(sold, goal) {
    if (!goal || goal <= 0) return 0;
    return Math.max(0, Math.min(100, Math.round((Number(sold) || 0) / goal * 100)));
  }
  /* Длина дуги для stroke-dasharray/-dashoffset: окружность и смещение под процент. */
  function ringDash(percent, radius) {
    const circumference = 2 * Math.PI * radius;
    const share = Math.max(0, Math.min(100, Number(percent) || 0)) / 100;
    return {circumference: Math.round(circumference * 100) / 100, offset: Math.round(circumference * (1 - share) * 100) / 100};
  }
  function remaining(sold, goal) {
    return Math.max(0, Math.ceil((Number(goal) || 0) - (Number(sold) || 0)));
  }
  /* Подпись темпа: цель взята / идём в темпе / отстаём на N к сегодняшнему дню. Темп — по прошедшим дням. */
  function paceState(totals, period) {
    const goal = Number(totals?.goal_qty) || 0, sold = Number(totals?.sold) || 0;
    if (!period || !period.days_passed) return {state: 'start', text: 'Цель появится после первой смены', lag: 0};
    const monthly = period.days_total > 10;
    if (goal && sold >= goal) return {state: 'done', text: monthly ? 'Цель месяца взята' : 'Цель недели взята', lag: 0};
    const expected = goal * Math.min(period.days_passed, period.days_total) / period.days_total;
    if (sold >= expected) return {state: 'on_pace', text: 'Идём в темпе', lag: 0};
    const lag = Math.ceil(expected - sold);
    return {state: 'behind', text: `Отстаём на ${lag} ${plural(lag, 'порцию', 'порции', 'порций')} к сегодняшнему дню`, lag};
  }
  /* «Лучший день: четверг, 31 порция» по продажам целевых позиций по дням зала. */
  function bestDay(soldByDay) {
    const entries = Object.entries(soldByDay || {}).filter(([, qty]) => qty > 0);
    if (!entries.length) return null;
    entries.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    const [day, qty] = entries[0];
    const [year, month, date] = day.split('-').map(Number);
    const name = DAYS[(new Date(Date.UTC(year, month - 1, date)).getUTCDay() + 6) % 7];
    return {day, qty, text: `${name}, ${qty} ${plural(qty, 'порция', 'порции', 'порций')}`};
  }
  return {thousands, signed, rate, percent, periodLabel, dayLabel, fetchedLabel, goalProgress, personProgress,
    personLine, plural, baseLabel, itemTone, itemProgress, sparkPath, ranking, initials, settingsFromForm,
    ringPercent, ringDash, remaining, paceState, bestDay};
});
