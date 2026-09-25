/** Выбор периода, общий для кассира, бухгалтера и директора.
 *
 *  Логика дат отделена от разметки: даты считаются строками «ГГГГ-ММ-ДД» без
 *  часовых поясов — new Date() в браузере кассира уводил границы на сутки.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.RetroPeriod = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
  const ISO = /^\d{4}-\d{2}-\d{2}$/;

  function valid(iso) { return typeof iso === 'string' && ISO.test(iso); }

  function shift(iso, days) {
    if (!valid(iso)) return iso;
    const value = new Date(iso + 'T12:00:00Z');
    value.setUTCDate(value.getUTCDate() + days);
    return value.toISOString().slice(0, 10);
  }

  function daysBetween(start, end) {
    if (!valid(start) || !valid(end)) return 0;
    const from = Date.parse(start + 'T12:00:00Z');
    const to = Date.parse(end + 'T12:00:00Z');
    return Math.round((to - from) / 86400000) + 1;
  }

  function monthStart(iso) { return valid(iso) ? iso.slice(0, 8) + '01' : iso; }

  /** Готовые периоды. Сегодняшний день не входит никуда: смена ещё идёт,
   *  и её цифры меняются, пока человек смотрит на таблицу. */
  function presetRange(today, preset) {
    const end = shift(today, -1);
    if (preset === 'yesterday') return { start: end, end: end };
    if (preset === 'month') {
      // Месяц берём по сегодняшней дате, а не по последнему закрытому дню:
      // первого числа иначе под подписью «этот месяц» открывался прошлый.
      const start = monthStart(today);
      return { start: start <= end ? start : end, end: end };
    }
    if (preset === 'prev-month') {
      const previousEnd = shift(monthStart(today), -1);
      return { start: monthStart(previousEnd), end: previousEnd < end ? previousEnd : end };
    }
    const length = Number(preset);
    if (!Number.isFinite(length) || length < 1) return { start: end, end: end };
    return { start: shift(end, -(Math.trunc(length) - 1)), end: end };
  }

  const SHORT = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн',
    'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

  /** «22 сен»: полные названия месяцев в ряду чипов не помещаются в строку. */
  function shortDay(iso) {
    if (!valid(iso)) return '—';
    const month = SHORT[Number(iso.slice(5, 7)) - 1];
    return month ? Number(iso.slice(8, 10)) + ' ' + month : '—';
  }

  function dayLabel(iso) {
    if (!valid(iso)) return '—';
    const month = MONTHS[Number(iso.slice(5, 7)) - 1];
    return month ? Number(iso.slice(8, 10)) + ' ' + month + ' ' + iso.slice(0, 4) : '—';
  }

  /** «13 — 22 сентября 2026»: месяц и год не повторяем, если они общие. */
  function label(start, end) {
    if (!valid(start) || !valid(end)) return '—';
    if (start === end) return dayLabel(start);
    const startMonth = MONTHS[Number(start.slice(5, 7)) - 1];
    const endMonth = MONTHS[Number(end.slice(5, 7)) - 1];
    if (!startMonth || !endMonth) return '—';
    const startDay = Number(start.slice(8, 10));
    const endDay = Number(end.slice(8, 10));
    if (start.slice(0, 4) !== end.slice(0, 4)) {
      return startDay + ' ' + startMonth + ' ' + start.slice(0, 4) + ' — ' +
        endDay + ' ' + endMonth + ' ' + end.slice(0, 4);
    }
    if (start.slice(5, 7) !== end.slice(5, 7)) {
      return startDay + ' ' + startMonth + ' — ' + endDay + ' ' + endMonth + ' ' + end.slice(0, 4);
    }
    return startDay + ' — ' + endDay + ' ' + endMonth + ' ' + end.slice(0, 4);
  }

  const MAX_DAYS = 62;

  /** Те же правила, что и на сервере: пустой ответ вместо понятного отказа
   *  человек читает как поломку панели. */
  function check(start, end, today) {
    if (!valid(start) || !valid(end)) return 'Выберите обе даты периода.';
    if (start > end) return 'Начало периода позже его конца.';
    if (end >= today) return 'Сегодняшний день ещё не закрыт: выберите период по вчерашний день.';
    if (daysBetween(start, end) > MAX_DAYS) return 'Период длиннее ' + MAX_DAYS + ' дней iiko не отдаёт.';
    return '';
  }

  const PRESETS = [
    { code: 'yesterday', name: 'Вчера' },
    { code: '7', name: '7 дней' },
    { code: '10', name: '10 дней' },
    { code: '30', name: '30 дней' },
    { code: 'month', name: 'Этот месяц' },
    { code: 'prev-month', name: 'Прошлый месяц' },
  ];

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  /** Разметка контрола. Возвращает объект со state() и set(): страница
   *  спрашивает выбор, а не хранит его у себя вторым экземпляром. */
  function mount(options) {
    const host = options.host;
    const today = options.today;
    const modes = options.modes || ['day', 'range'];
    const state = {
      mode: options.mode || modes[0],
      day: options.day || shift(today, -1),
      start: '', end: '', preset: options.preset || '10',
    };
    Object.assign(state, presetRange(today, state.preset));

    const root = node('div', 'period');
    const switcher = node('div', 'period-modes');
    switcher.setAttribute('role', 'group');
    switcher.setAttribute('aria-label', 'Что показывать');
    const dayPane = node('div', 'period-pane');
    const rangePane = node('div', 'period-pane');
    const status = node('p', 'period-state');
    status.setAttribute('role', 'status');

    const dayInput = document.createElement('input');
    dayInput.type = 'date';
    dayInput.id = options.dayInputId || 'period-day';
    dayInput.max = today;
    dayInput.value = state.day;
    const dayLabelNode = node('label', '', 'Дата');
    dayLabelNode.htmlFor = dayInput.id;
    const dayGroup = node('div', 'date-group');
    dayGroup.append(dayLabelNode, dayInput);
    const dayChips = node('div', 'quick-days');
    dayPane.append(dayGroup, dayChips);

    const startInput = document.createElement('input');
    const endInput = document.createElement('input');
    for (const input of [startInput, endInput]) {
      input.type = 'date';
      input.max = shift(today, -1);
    }
    startInput.id = 'period-start';
    endInput.id = 'period-end';
    const startLabel = node('label', '', 'Дата начала');
    startLabel.htmlFor = startInput.id;
    const endLabel = node('label', '', 'Дата конца');
    endLabel.htmlFor = endInput.id;
    const startGroup = node('div', 'date-group');
    startGroup.append(startLabel, startInput);
    const endGroup = node('div', 'date-group');
    endGroup.append(endLabel, endInput);
    const presetChips = node('div', 'quick-days');
    rangePane.append(startGroup, endGroup, presetChips);

    function emit() {
      if (typeof options.onChange === 'function') options.onChange(current());
    }

    function current() {
      return state.mode === 'day'
        ? { mode: 'day', day: state.day, start: state.day, end: state.day, label: dayLabel(state.day) }
        : { mode: 'range', day: null, start: state.start, end: state.end, preset: state.preset,
            label: label(state.start, state.end) };
    }

    function paint() {
      const range = state.mode === 'range';
      dayPane.hidden = range;
      rangePane.hidden = !range;
      dayInput.value = state.day;
      startInput.value = state.start;
      endInput.value = state.end;
      for (const button of switcher.querySelectorAll('button')) {
        const on = button.dataset.mode === state.mode;
        button.classList.toggle('active', on);
        button.setAttribute('aria-pressed', String(on));
      }
      for (const chip of dayChips.querySelectorAll('button')) {
        chip.classList.toggle('active', chip.dataset.date === state.day);
      }
      for (const chip of presetChips.querySelectorAll('button')) {
        chip.classList.toggle('active', chip.dataset.preset === state.preset);
      }
      const problem = range ? check(state.start, state.end, today) : '';
      status.textContent = problem || (range
        ? label(state.start, state.end) + ' · ' + daysBetween(state.start, state.end) + ' дн.'
        : dayLabel(state.day));
      status.classList.toggle('is-error', Boolean(problem));
      return !problem;
    }

    if (modes.length > 1) {
      for (const [mode, name] of [['day', 'День'], ['range', 'Период']]) {
        if (!modes.includes(mode)) continue;
        const button = node('button', 'period-mode', name);
        button.type = 'button';
        button.dataset.mode = mode;
        button.addEventListener('click', () => {
          if (state.mode === mode) return;
          state.mode = mode;
          if (paint()) emit();
        });
        switcher.append(button);
      }
      root.append(switcher);
    }

    if (modes.includes('day')) {
      for (let offset = 0; offset <= 6; offset++) {
        const day = shift(today, -offset);
        const chip = node('button', 'chip', offset === 0 ? 'Сегодня' : offset === 1 ? 'Вчера'
          : shortDay(day));
        chip.type = 'button';
        chip.dataset.date = day;
        chip.addEventListener('click', () => { state.day = day; if (paint()) emit(); });
        dayChips.append(chip);
      }
      root.append(dayPane);
    }
    if (modes.includes('range')) {
      for (const preset of PRESETS) {
        const chip = node('button', 'chip', preset.name);
        chip.type = 'button';
        chip.dataset.preset = preset.code;
        chip.addEventListener('click', () => {
          state.preset = preset.code;
          Object.assign(state, presetRange(today, preset.code));
          if (paint()) emit();
        });
        presetChips.append(chip);
      }
      root.append(rangePane);
    }
    root.append(status);

    dayInput.addEventListener('change', () => {
      if (!dayInput.value) return;
      state.day = dayInput.value;
      if (paint()) emit();
    });
    for (const [input, key] of [[startInput, 'start'], [endInput, 'end']]) {
      input.addEventListener('change', () => {
        if (!input.value) return;
        state[key] = input.value;
        state.preset = '';
        if (paint()) emit();
      });
    }

    host.replaceChildren(root);
    paint();
    return {
      element: root,
      state: current,
      valid: () => state.mode === 'day' || !check(state.start, state.end, today),
      note: text => { status.textContent = text; status.classList.remove('is-error'); },
      set: next => {
        Object.assign(state, next);
        if (paint()) emit();
      },
    };
  }

  return {
    shift: shift, daysBetween: daysBetween, monthStart: monthStart, presetRange: presetRange,
    label: label, dayLabel: dayLabel, shortDay: shortDay, check: check, mount: mount, MAX_DAYS: MAX_DAYS,
    PRESETS: PRESETS,
  };
});
