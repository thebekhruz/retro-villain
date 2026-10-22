/* Модуль «Аналитика» (T-524): выручка за 7 дней против двух предыдущих
   семидневок — плитки, две карточки сравнения и линейный график из трёх линий.
   Данные — /api/analytics/revenue/weeks; расчёт и подписи — analytics-logic.js. */
(() => {
  const L = globalThis.AnalyticsLogic;
  const $ = id => document.getElementById(id);
  const SERIES = [
    {key: 'current', label: 'Последние 7 дней', color: '#143e35'},
    {key: 'previous', label: 'Неделей раньше', color: '#c5a359'},
    {key: 'before', label: 'Двумя неделями раньше', color: '#91a39a', dashed: true},
  ];
  const endInput = $('an-end'), form = $('an-filters'), message = $('an-message'), status = $('updated');
  const chartBox = $('an-chart'), metrics = document.querySelector('.an-metrics');
  let today = null, last = null, request = 0;

  function svg(name, attrs = {}) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', name);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  }

  function say(text, error = false) {
    message.hidden = !text;
    message.textContent = text || '';
    message.classList.toggle('is-error', Boolean(error));
  }

  function setLoading(on) {
    metrics.setAttribute('aria-busy', on ? 'true' : 'false');
    chartBox.classList.toggle('is-loading', on);
    $('an-show').disabled = on;
    if (on) status.textContent = '';
  }

  function renderMetrics(data) {
    data.periods.forEach(period => {
      const card = document.querySelector(`.an-metric[data-period="${period.key}"]`);
      if (!card) return;
      card.querySelector('[data-total]').textContent = period.total === null ? '—' : L.formatMoney(period.total);
      card.querySelector('[data-range]').textContent = L.rangeLabel(period.start, period.end);
      card.querySelector('[data-coverage]').textContent = L.coverageLabel(period);
      card.classList.toggle('is-incomplete', !period.complete);
    });
  }

  function renderComparison(data) {
    data.comparison.forEach(item => {
      const card = document.querySelector(`.an-trend[data-against="${item.against}"]`);
      if (!card) return;
      const trend = L.trend(item);
      card.classList.remove('is-up', 'is-down', 'is-flat', 'is-unknown');
      card.classList.add('is-' + trend.tone);
      card.querySelector('[data-percent]').textContent = trend.percent === '—' && trend.tone !== 'unknown' ? trend.amount : trend.percent;
      card.querySelector('[data-amount]').textContent = trend.percent === '—' ? '' : trend.amount;
      const other = data.periods.find(period => period.key === item.against);
      const notes = [trend.sentence];
      if (trend.tone !== 'unknown') notes.push(`Сравнение с ${L.rangeLabel(item.start, item.end)}.`);
      // Неполное окно честно названо: сумма по загруженным дням, а не за всю неделю.
      if (trend.tone !== 'unknown' && other && !other.complete) notes.push(`Неделя для сравнения неполная: ${L.coverageLabel(other)}.`);
      if (trend.tone !== 'unknown' && !data.periods[0].complete) notes.push(`Последняя неделя неполная: ${L.coverageLabel(data.periods[0])}.`);
      card.querySelector('[data-sentence]').textContent = notes.join(' ');
    });
  }

  function renderLegend() {
    const legend = $('an-legend');
    legend.replaceChildren(...SERIES.map(series => {
      const item = document.createElement('span'), line = document.createElement('i');
      line.style.setProperty('--an-color', series.color);
      if (series.dashed) line.classList.add('is-dashed');
      item.append(line, document.createTextNode(series.label));
      return item;
    }));
  }

  function tooltipFor(data, index) {
    const box = document.createElement('div');
    box.className = 'an-tooltip';
    const title = document.createElement('strong');
    const currentDay = data.periods[0].days[index];
    title.textContent = L.dayLabel(currentDay);
    const rows = document.createElement('div');
    rows.className = 'an-tooltip-rows';
    SERIES.forEach((series, position) => {
      const day = data.periods[position].days[index];
      const row = document.createElement('span'), name = document.createElement('span'), dot = document.createElement('i'),
        value = document.createElement('b');
      dot.style.background = series.color;
      name.append(dot, document.createTextNode(L.shortDate(day.day)));
      value.textContent = day.missing ? 'нет данных' : L.formatMoney(day.revenue);
      row.append(name, value);
      rows.append(row);
    });
    box.append(title, rows);
    if (currentDay.partial) {
      const note = document.createElement('em');
      note.textContent = 'День ещё идёт или не перечитан после закрытия.';
      box.append(note);
    }
    return box;
  }

  function renderChart(data) {
    chartBox.replaceChildren();
    const periods = data.periods;
    if (!periods.some(period => period.days_with_data > 0)) {
      const empty = document.createElement('div');
      empty.className = 'an-empty';
      empty.textContent = 'За эти три недели в базе панели нет загруженных продаж.';
      chartBox.append(empty);
      return;
    }
    const width = Math.max(320, Math.floor(chartBox.clientWidth || 720)), height = Math.max(200, chartBox.clientHeight || 280);
    const pad = {left: 58, right: 18, top: 14, bottom: 34};
    const plotWidth = width - pad.left - pad.right, plotHeight = height - pad.top - pad.bottom;
    const values = periods.map(period => period.days.map(day => day.revenue));
    const max = L.niceCeil(Math.max(0, ...values.flat().map(Number).filter(Number.isFinite)));
    const chart = svg('svg', {viewBox: `0 0 ${width} ${height}`, 'aria-hidden': 'true'});
    for (let step = 0; step <= 4; step += 1) {
      const value = max - max * step / 4, y = pad.top + plotHeight * step / 4;
      chart.append(svg('line', {x1: pad.left, y1: y, x2: width - pad.right, y2: y, class: value === 0 ? 'an-grid is-zero' : 'an-grid'}));
      const label = svg('text', {x: pad.left - 8, y: y + 3.5, 'text-anchor': 'end', class: 'an-axis'});
      label.textContent = L.compactMoney(value);
      chart.append(label);
    }
    const pointSets = values.map(list => L.linePoints(list, max, plotWidth, plotHeight));
    const currentDays = periods[0].days;
    currentDays.forEach((day, index) => {
      const point = pointSets[0][index];
      const label = svg('text', {x: pad.left + point.x, y: height - 10, 'text-anchor': index === 0 ? 'start' : index === currentDays.length - 1 ? 'end' : 'middle', class: 'an-axis'});
      label.textContent = L.dayLabel(day);
      chart.append(label);
    });
    // Рисуем от дальней недели к текущей, чтобы текущая линия была сверху.
    [...SERIES.keys()].reverse().forEach(position => {
      const series = SERIES[position], points = pointSets[position];
      L.lineSegments(points.map(point => ({...point, x: point.x + pad.left, y: point.y === null ? null : point.y + pad.top})))
        .forEach(segment => chart.append(svg('polyline', {points: segment, class: `an-line is-${series.key}`, stroke: series.color})));
      points.forEach((point, index) => {
        if (point.y === null) return;
        const day = periods[position].days[index];
        chart.append(svg('circle', {cx: pad.left + point.x, cy: pad.top + point.y, r: 3.6, fill: series.color,
          stroke: day.partial ? series.color : '#fff', class: day.partial ? 'an-point is-partial' : 'an-point'}));
      });
    });
    const hoverLine = svg('line', {y1: pad.top, y2: pad.top + plotHeight, class: 'an-hover-line', visibility: 'hidden'});
    const hoverLayer = svg('rect', {x: pad.left, y: pad.top, width: plotWidth, height: plotHeight, class: 'an-hover-layer',
      tabindex: '0', 'aria-label': 'Наведите или коснитесь точки, чтобы увидеть суммы трёх недель'});
    chart.append(hoverLine, hoverLayer);
    let tooltip = null;
    const show = event => {
      const bounds = chart.getBoundingClientRect();
      if (!bounds.width) return;
      const chartX = (event.clientX - bounds.left) / bounds.width * width - pad.left;
      const index = L.nearestIndex(chartX, plotWidth, currentDays.length);
      const x = pad.left + pointSets[0][index].x;
      const next = tooltipFor(data, index);
      if (tooltip) tooltip.replaceWith(next); else chartBox.append(next);
      tooltip = next;
      tooltip.style.left = x / width * 100 + '%';
      tooltip.classList.toggle('is-left', x > width * 0.62);
      hoverLine.setAttribute('visibility', 'visible');
      hoverLine.setAttribute('x1', x);
      hoverLine.setAttribute('x2', x);
    };
    const hide = () => {
      if (tooltip) tooltip.remove();
      tooltip = null;
      hoverLine.setAttribute('visibility', 'hidden');
    };
    hoverLayer.addEventListener('pointermove', show);
    hoverLayer.addEventListener('pointerdown', show);
    hoverLayer.addEventListener('pointerleave', hide);
    hoverLayer.addEventListener('blur', hide);
    chartBox.append(chart);
  }

  function renderTable(data) {
    const body = $('an-table').tBodies[0];
    body.replaceChildren();
    const current = data.periods[0];
    current.days.forEach((_, index) => {
      const row = document.createElement('tr');
      const head = document.createElement('td');
      head.textContent = current.days[index].weekday;
      row.append(head);
      data.periods.forEach(period => {
        const day = period.days[index], cell = document.createElement('td'), date = document.createElement('small');
        cell.textContent = day.missing ? 'нет данных' : L.formatMoney(day.revenue) + (day.partial ? ' *' : '');
        if (day.missing) cell.classList.add('is-missing');
        date.textContent = L.shortDate(day.day);
        cell.append(date);
        row.append(cell);
      });
      body.append(row);
    });
    const total = document.createElement('tr');
    total.className = 'is-total';
    const label = document.createElement('td');
    label.textContent = 'Итого';
    total.append(label);
    data.periods.forEach(period => {
      const cell = document.createElement('td');
      cell.textContent = period.total === null ? '—' : L.formatMoney(period.total);
      total.append(cell);
    });
    body.append(total);
  }

  function render(data) {
    last = data;
    renderMetrics(data);
    renderComparison(data);
    renderLegend();
    renderChart(data);
    renderTable(data);
    status.textContent = L.updatedLabel(data);
    say(data.warnings && data.warnings.length ? data.warnings.join(' ') : '');
  }

  async function load() {
    const end = endInput.value;
    if (!end) { say('Укажите, по какой день считать.', true); return; }
    if (today && end > today) { say('Будущие даты недоступны.', true); return; }
    const id = ++request;
    setLoading(true);
    say('');
    try {
      const response = await fetch('/api/analytics/revenue/weeks?end=' + encodeURIComponent(end), {headers: {accept: 'application/json'}});
      const payload = await response.json().catch(() => null);
      if (id !== request) return;
      if (!response.ok) {
        say((payload && payload.detail) || 'Не удалось загрузить выручку.', true);
        return;
      }
      render(payload);
    } catch (error) {
      if (id === request) say('Нет связи с сервером. Повторите позже.', true);
    } finally {
      if (id === request) setLoading(false);
    }
  }

  function setQuick(kind) {
    document.querySelectorAll('.an-quick .chip').forEach(chip => chip.classList.toggle('active', chip.dataset.end === kind));
    if (!today) return;
    endInput.value = kind === 'today' ? today : L.defaultEnd(today);
  }

  form.addEventListener('submit', event => { event.preventDefault(); load(); });
  document.querySelectorAll('.an-quick .chip').forEach(chip => chip.addEventListener('click', () => { setQuick(chip.dataset.end); load(); }));
  endInput.addEventListener('change', () => {
    document.querySelectorAll('.an-quick .chip').forEach(chip => chip.classList.toggle('active',
      today && endInput.value === (chip.dataset.end === 'today' ? today : L.defaultEnd(today))));
  });
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (last) renderChart(last); }, 150);
  });

  const config = globalThis.RetroConfig || fetch('/api/config', {headers: {accept: 'application/json'}}).then(r => (r.ok ? r.json() : null));
  Promise.resolve(config).then(data => {
    today = (data && data.today) || new Date().toISOString().slice(0, 10);
    endInput.max = today;
    setQuick('yesterday');
    load();
  }).catch(() => {
    today = new Date().toISOString().slice(0, 10);
    setQuick('yesterday');
    load();
  });
})();
