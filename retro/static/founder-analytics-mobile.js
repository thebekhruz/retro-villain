/* Аналитика учредителя на телефоне (макет «3б», ТЗ от 10.11.2026, задача 2).
 *
 * На телефоне — только «Общая картина» недели и компактная рассылка. Неделя
 * берётся теми же запросами, что в кабинете (/api/founder/week, дивиденды,
 * прогноз), и считается логикой кабинета (FounderCabinetLogic); здесь только
 * сравнение с теми же днями прошлой недели. На компьютере (≥ 901) ничего не делает. */
(() => {
  const $ = id => document.getElementById(id);
  const logic = globalThis.FounderCabinetLogic;
  const {num, sum, short, plural, dm} = logic;
  const phone = matchMedia('(max-width: 900px)');
  const state = {today: null, weekDate: null, dividends: null, forecast: null, started: false};

  function setText(id, value) { $(id).textContent = value; }
  function shiftDay(iso, days) {
    const value = new Date(iso + 'T12:00:00Z');
    value.setUTCDate(value.getUTCDate() + days);
    return value.toISOString().slice(0, 10);
  }
  async function request(url) {
    const response = await fetch(url);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || 'Не удалось получить данные.');
    return body;
  }

  // ── «Общая картина» недели ────────────────────────────────────────────
  function renderWeek(week) {
    const totals = logic.weekTotals(week);
    const current = week.start <= state.today && state.today <= week.end;
    setText('fa-week-range', dm(week.start) + '–' + dm(week.end));
    setText('fa-week-current', current ? '· текущая' : '');
    // Большая аналитика на телефоне не грузится (founder.js) — статус дня, как на главной, из этой недели.
    if (current) setText('updated', logic.todayStatus(week.days.find(day => day.date === state.today), state.today));
    $('fa-week-next').disabled = current || week.end >= state.today;
    setText('fa-week-total', short(totals.retro + totals.school));
    renderCompare(week, null);  // процент — когда придёт прошлая неделя
    const days = totals.days + ' ' + plural(totals.days, 'день', 'дня', 'дней');
    setText('fa-k-retro', short(totals.retro));
    setText('fa-k-retro-sub', days + ' недели');
    setText('fa-k-ox', short(totals.school));
    setText('fa-k-ox-sub', week.orders_error ? 'чеки: ' + week.orders_error : days + ' недели');
    setText('fa-k-demo', short(totals.demo));
    setText('fa-k-demo-sub', totals.demoShare === null ? '' : totals.demoShare + '% выручки Retro');
    renderDividends(week);
  }

  /** Выручка (Retro + Oxbridge) этой недели к тем же дням недели прошлой: Пн–Вт к Пн–Вт,
   *  по дню недели, а не по датам. Только закрытые дни: сегодняшний ещё идёт, и неполный
   *  день против целого всегда дал бы минус. Нет данных iiko хоть за один такой день — сравнения нет. */
  function weekCompare(week, previous) {
    const days = ((week && week.days) || []).filter(day => !day.future && day.cashier && day.date < state.today);
    const before = new Map(((previous && previous.days) || []).map(day => [day.weekday, day]));
    const pairs = days.map(day => before.get(day.weekday));
    if (!days.length || pairs.some(day => !day || !day.cashier)) return null;
    const revenue = list => list.reduce((acc, day) => acc + (num(day.cashier.retro) || 0) + (num(day.cashier.school) || 0), 0);
    const current = revenue(days), past = revenue(pairs);
    if (!past) return null;
    const name = day => logic.WEEKDAYS[day.weekday].toLowerCase();
    return {current, past, percent: Math.round((current - past) / past * 100),
      span: days.length === 1 ? name(days[0]) : name(days[0]) + '–' + name(days[days.length - 1])};
  }

  /** Процент к тем же дням недели прошлой недели — относительно прошлой недели. */
  function renderCompare(week, view) {
    const change = $('fa-week-change'), count = logic.weekTotals(week).days;
    change.hidden = !view;
    if (!view) { setText('fa-week-days', count + ' ' + plural(count, 'день', 'дня', 'дней') + ' недели'); return; }
    change.textContent = (view.percent >= 0 ? '▲ ' : '▼ ') + Math.abs(view.percent) + '%';
    change.className = view.percent >= 0 ? 'is-up' : 'is-down';
    // Итог выше — с сегодняшним днём, процент — по закрытым дням: обе суммы сравнения на виду.
    setText('fa-week-days', view.span + ': ' + short(view.current) + ' к ' + short(view.past) + ' прошлой недели');
  }

  function renderDividends(week) {
    const card = $('fa-k-div-card'), data = state.dividends;
    if (!data) { card.hidden = true; return; }
    const view = logic.dividendView(data);
    // Неделя дивидендов — текущая; для прошлой — запись из истории, если есть.
    const past = week && week.start !== data.start ? (data.history || []).find(item => item.start === week.start) : null;
    if (week && week.start !== data.start && !past) { card.hidden = true; return; }
    card.hidden = false;
    const target = past ? num(past.target) : view.target, collected = past ? num(past.collected) || 0 : view.collected;
    setText('fa-k-div', short(collected));
    setText('fa-k-div-sub', target === null ? 'цель не задана'
      : 'из ' + short(target) + (past ? '' : ' · ' + view.status.toLowerCase()));
    $('fa-k-div-bar').style.width = (target ? Math.min(100, collected / target * 100) : 0) + '%';
  }

  function renderForecast(data) {
    const month = data && data.month;
    if (!month) { setText('fa-k-month', '—'); setText('fa-k-month-sub', data && data.error ? data.error : ''); return; }
    const total = num(month.total), fact = num(month.fact);
    setText('fa-k-month', short(total));
    setText('fa-k-month-sub', 'факт ' + short(fact) + ' + прогноз ' + short(num(month.forecast)));
    $('fa-k-month-bar').style.width = (total ? Math.min(100, fact / total * 100) : 0) + '%';
  }

  function weekFailed(error) {
    ['fa-week-total', 'fa-k-retro', 'fa-k-ox', 'fa-k-demo'].forEach(id => setText(id, '—'));
    setText('fa-week-days', error.message);
    ['fa-k-retro-sub', 'fa-k-ox-sub', 'fa-k-demo-sub'].forEach(id => setText(id, ''));
    $('fa-week-change').hidden = true;
    renderDividends(null);
  }

  function loadWeek() {
    // Быстро листают недели — рисуем только ответ на последний выбор.
    const asked = state.weekDate;
    request('/api/founder/week?date=' + asked).then(week => {
      if (asked !== state.weekDate) return;
      state.weekStart = week.start;
      renderWeek(week);
      // Прошлая неделя — тот же запрос, нужна только для сравнения; не ответила — процента нет.
      return request('/api/founder/week?date=' + shiftDay(week.start, -7)).then(previous => {
        if (asked === state.weekDate) renderCompare(week, weekCompare(week, previous));
      }, () => {});
    }).catch(error => { if (asked === state.weekDate) weekFailed(error); });
  }

  async function startWeek() {
    if (state.started || !phone.matches) return;
    state.started = true;
    try {
      const config = await globalThis.RetroConfig;
      state.today = config.today;
    } catch (error) { state.today = new Date().toISOString().slice(0, 10); }
    state.weekDate = state.today;
    request('/api/founder/dividends/weekly').then(data => { state.dividends = data; renderDividends(null); }).catch(() => renderDividends(null));
    request('/api/founder/forecast?date=' + state.today).then(renderForecast).catch(error => renderForecast({error: error.message}));
    loadWeek();
  }

  // Переключатель недель: /api/founder/week принимает любую дату — прошлые недели доступны.
  $('fa-week-prev').addEventListener('click', () => {
    if (!state.weekStart) return;
    state.weekDate = shiftDay(state.weekStart, -7);
    loadWeek();
  });
  $('fa-week-next').addEventListener('click', () => {
    if (!state.weekStart) return;
    const next = shiftDay(state.weekStart, 7);
    state.weekDate = next > state.today ? state.today : next;
    loadWeek();
  });

  // ── Рассылка: форма и журнал — во весь экран ──────────────────────────
  const broadcast = document.querySelector('.broadcast-panel');
  function openBroadcast(mode) {
    broadcast.classList.remove('fa-writing', 'fa-journal');
    if (mode) broadcast.classList.add(mode);
    document.body.classList.toggle('fa-sheet-open', Boolean(mode));
    if (mode) broadcast.scrollTop = 0;
  }
  $('fa-broadcast-write').addEventListener('click', () => openBroadcast('fa-writing'));
  $('fa-broadcast-journal').addEventListener('click', () => openBroadcast('fa-journal'));
  $('fa-broadcast-close').addEventListener('click', () => openBroadcast(null));
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && document.body.classList.contains('fa-sheet-open')) openBroadcast(null); });

  startWeek();
  phone.addEventListener('change', startWeek);
})();
