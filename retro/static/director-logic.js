(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.DirectorLogic = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  // iiko отдаёт числа строками, и пустое значение приходит как null.
  // Number('') равен нулю, поэтому пустоту отсекаем до приведения типа.
  function amount(value) {
    if (value === null || value === undefined || value === '') return 0;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function entries(group) {
    if (!group) return [];
    return Object.keys(group).map(function (name) {
      const metric = group[name];
      const revenue = amount(metric.revenue);
      const cost = amount(metric.cost);
      const profit = amount(metric.gross_profit);
      return {
        name: name,
        quantity: amount(metric.quantity),
        revenue: revenue,
        cost: cost,
        profit: profit,
        // Маржу считаем сами, а не берём строкой: позиция с нулевой выручкой
        // не имеет процента, и рисовать у неё ноль было бы враньём.
        margin: revenue > 0 ? (profit / revenue) * 100 : null,
      };
    });
  }

  function totals(group) {
    const rows = entries(group);
    const sum = rows.reduce(
      function (acc, row) {
        acc.quantity += row.quantity;
        acc.revenue += row.revenue;
        acc.cost += row.cost;
        acc.profit += row.profit;
        return acc;
      },
      { quantity: 0, revenue: 0, cost: 0, profit: 0 }
    );
    sum.positions = rows.length;
    sum.margin = sum.revenue > 0 ? (sum.profit / sum.revenue) * 100 : null;
    return sum;
  }

  const SORTS = {
    revenue: function (a, b) { return b.revenue - a.revenue; },
    profit: function (a, b) { return b.profit - a.profit; },
    quantity: function (a, b) { return b.quantity - a.quantity; },
    // Позиции без выручки не участвуют в сравнении марж: у них её просто нет,
    // и в конце списка они держатся кучей, а не выдают себя за худшие.
    margin: function (a, b) {
      if (a.margin === null && b.margin === null) return b.revenue - a.revenue;
      if (a.margin === null) return 1;
      if (b.margin === null) return -1;
      return a.margin - b.margin;
    },
  };

  function rank(group, sort, limit) {
    const rows = entries(group).slice();
    rows.sort(SORTS[sort] || SORTS.revenue);
    if (typeof limit === 'number' && limit > 0) return rows.slice(0, limit);
    return rows;
  }

  function search(rows, query) {
    const needle = String(query || '').trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter(function (row) { return row.name.toLowerCase().indexOf(needle) !== -1; });
  }

  /** Локомотивы: где заработано больше всего денег, а не где выше процент.
   *  Процент высок у мелочей вроде чая, и на них ресторан не живёт. */
  function locomotives(group, count) {
    return rank(group, 'profit', count || 3).filter(function (row) { return row.profit > 0; });
  }

  /** Позиции, которые съедают маржу: заметная доля выручки при низкой марже.
   *  Порог доли отсекает единичные продажи, иначе список забивает случайный шум. */
  function drains(group, count, shareThreshold) {
    const sum = totals(group);
    if (!sum.revenue) return [];
    const share = typeof shareThreshold === 'number' ? shareThreshold : 0.005;
    const rows = entries(group).filter(function (row) {
      return row.margin !== null && row.revenue / sum.revenue >= share;
    });
    rows.sort(SORTS.margin);
    return rows.slice(0, count || 3);
  }


  const MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];

  /** «2026-09-09 — 2026-09-18» читается хуже, чем «9 — 18 сентября 2026».
   *  Месяц и год не повторяем, если они у границ периода общие. */
  function periodLabel(startIso, endIso) {
    const start = String(startIso || '').split('-');
    const end = String(endIso || '').split('-');
    if (start.length !== 3 || end.length !== 3) return '—';
    const startDay = Number(start[2]);
    const endDay = Number(end[2]);
    const startMonth = MONTHS[Number(start[1]) - 1];
    const endMonth = MONTHS[Number(end[1]) - 1];
    if (!startMonth || !endMonth) return '—';
    if (start[0] !== end[0]) {
      return startDay + ' ' + startMonth + ' ' + start[0] + ' — ' + endDay + ' ' + endMonth + ' ' + end[0];
    }
    if (start[1] !== end[1]) {
      return startDay + ' ' + startMonth + ' — ' + endDay + ' ' + endMonth + ' ' + end[0];
    }
    return startDay + ' — ' + endDay + ' ' + endMonth + ' ' + end[0];
  }

  /** Русское склонение после числа: 1 позиция, 2 позиции, 5 позиций.
   *  Одиннадцать — двадцать всегда берут последнюю форму. */
  function plural(count, one, few, many) {
    const abs = Math.abs(Math.trunc(count));
    const tail = abs % 100;
    if (tail >= 11 && tail <= 14) return many;
    const last = abs % 10;
    if (last === 1) return one;
    if (last >= 2 && last <= 4) return few;
    return many;
  }

  /** Одна дата словами: «19 сентября 2026». */
  function dayLabel(iso) {
    const parts = String(iso || '').split('-');
    if (parts.length !== 3) return '—';
    const month = MONTHS[Number(parts[1]) - 1];
    if (!month) return '—';
    return Number(parts[2]) + ' ' + month + ' ' + parts[0];
  }

  function waiters(metrics) {
    return entries(metrics).sort(SORTS.revenue);
  }

  /** Доля позиции в выручке группы — ширина полосы в таблице. */
  function shareOf(row, groupTotals) {
    if (!groupTotals || !groupTotals.revenue) return 0;
    return Math.max(0, Math.min(1, row.revenue / groupTotals.revenue));
  }

  return {
    amount: amount,
    entries: entries,
    totals: totals,
    rank: rank,
    search: search,
    locomotives: locomotives,
    drains: drains,
    waiters: waiters,
    periodLabel: periodLabel,
    plural: plural,
    dayLabel: dayLabel,
    shareOf: shareOf,
  };
});
