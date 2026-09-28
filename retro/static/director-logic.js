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
        breakdown: metric.breakdown ? Object.fromEntries(
          ['sales', 'chef', 'tasting', 'other_zero'].map(function (key) {
            const part = metric.breakdown[key] || {};
            return [key, { quantity: amount(part.quantity), revenue: amount(part.revenue),
              cost: amount(part.cost), profit: amount(part.gross_profit) }];
          })) : null,
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
    sum.breakdown = rows.length && rows.every(row => row.breakdown)
      ? Object.fromEntries(['sales', 'chef', 'tasting', 'other_zero'].map(key => [key,
        rows.reduce((total, row) => {
          for (const field of ['quantity', 'revenue', 'cost', 'profit']) total[field] += row.breakdown[key][field];
          return total;
        }, {quantity: 0, revenue: 0, cost: 0, profit: 0})])) : null;
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


  // ── Телефон директора (6a) ────────────────────────────────────────────

  /** Имя блюда без банкетной обёртки: «СВАДЬБА Салат Цезарь (Бехруз)» и
   *  «Салат Цезарь» — одно блюдо. Так сравниваем меню зала и Бехруза. */
  function dishKey(name) {
    return String(name || '').toLowerCase().replace(/ё/g, 'е')
      .replace(/свадьба|бехруз/g, ' ').replace(/[()«»"]/g, ' ')
      .replace(/\s+/g, ' ').trim();
  }

  /** Маржа ниже этой — «съедает» деньги, даже если блюдо хорошо продаётся. */
  const LOW_MARGIN = 45;

  /** Позиции iiko, которые не блюда: упаковка, аренда, сервис. В рейтинге
   *  меню они только мешают — «Контейнер» не продвигают и не убирают. */
  const NOT_DISH = /контейнер|пакет|упаковк|аренд|депозит|обслуживан|сервисн|доставк/i;
  function isDish(row) { return !NOT_DISH.test(row.name); }

  /** Низкая маржа — по тому же округлению, что видит директор: иначе
   *  блюдо с «45%» на экране попадало в список «ниже 45%». */
  function lowMargin(row) { return row.margin !== null && Math.round(row.margin) < LOW_MARGIN; }

  /** Четыре среза меню: лидеры, слабые, низкая маржа, нет в Бехрузе.
   *  `snapshot` — отчёт директора; `venue` — all | retro | oxbridge | banquet. */
  function menuSlice(snapshot, venue, view, limit) {
    const metrics = (snapshot && snapshot.item_metrics) || {};
    const count = limit || 5;
    let rows;
    if (view === 'notb') {
      // Хиты зала, которых нет в банкетном меню. Фильтр заведения здесь не
      // действует: сравниваем весь зал (Retro + Oxbridge) с Бехрузом.
      const banquet = new Set(entries(metrics.banquet).map(function (row) { return dishKey(row.name); }));
      const hall = {};
      ['retro', 'oxbridge'].forEach(function (group) {
        entries(metrics[group]).forEach(function (row) {
          const current = hall[row.name] || {name: row.name, quantity: 0, revenue: 0, cost: 0, profit: 0};
          current.quantity += row.quantity; current.revenue += row.revenue;
          current.cost += row.cost; current.profit += row.profit;
          hall[row.name] = current;
        });
      });
      rows = Object.keys(hall).map(function (name) {
        const row = hall[name];
        row.margin = row.revenue > 0 ? row.profit / row.revenue * 100 : null;
        return row;
      }).filter(function (row) { return row.revenue > 0 && isDish(row) && !banquet.has(dishKey(row.name)); });
      rows.sort(SORTS.quantity);
    } else {
      const sold = entries(metrics[venue === 'all' ? 'all' : venue])
        .filter(function (row) { return row.revenue > 0 && isDish(row); });
      if (view === 'weak') {
        rows = sold.slice().sort(function (a, b) { return a.revenue - b.revenue; });
      } else if (view === 'lowm') {
        rows = sold.filter(lowMargin)
          .sort(SORTS.quantity);
      } else {
        rows = sold.slice().sort(SORTS.quantity);
      }
    }
    return rows.slice(0, count);
  }

  /** Тренд блюда: средние продажи последних трёх дней к среднему за тридцать.
   *  null — если хотя бы одного из отчётов нет или за месяц блюдо не продавалось. */
  function trend(name, venue, short, long) {
    if (!short || !long) return null;
    const group = venue === 'all' || !venue ? 'all' : venue;
    const pick = function (snapshot) {
      const metric = ((snapshot.item_metrics || {})[group] || {})[name];
      return metric ? amount(metric.quantity) : 0;
    };
    const month = pick(long), recent = pick(short);
    const shortDays = short.period_days || 3, longDays = long.period_days || 30;
    if (!month) return null;
    return (recent / shortDays) / (month / longDays) - 1;
  }

  /** Три совета на день по данным меню, без обращения к AI.
   *  Модель зовём, только когда директор спрашивает подробнее: совет на
   *  главном экране должен открываться мгновенно и не стоить запроса. */
  function dayTips(snapshot) {
    const metrics = (snapshot && snapshot.item_metrics) || {};
    const sold = entries(metrics.retro).filter(function (row) { return row.revenue > 0 && isDish(row); });
    const tips = [];
    const promo = sold.filter(function (row) { return row.margin !== null && row.margin >= 60; })
      .sort(function (a, b) { return b.profit - a.profit; })[0];
    if (promo) tips.push({dish: promo.name, kind: 'promo', margin: promo.margin});
    const trap = sold.filter(lowMargin)
      .sort(SORTS.revenue)[0];
    if (trap) tips.push({dish: trap.name, kind: 'trap', margin: trap.margin});
    const weak = sold.filter(function (row) { return row.quantity > 0; })
      .sort(function (a, b) { return a.quantity - b.quantity || a.revenue - b.revenue; })[0];
    if (weak && (!promo || weak.name !== promo.name)) tips.push({dish: weak.name, kind: 'weak', quantity: weak.quantity});
    return tips;
  }

  /** Строки «Команды»: сменные из Hikvision и оклады из реестра бухгалтера. */
  function teamRows(team) {
    if (!team) return [];
    const shift = (team.shift || []).map(function (row) {
      return {id: row.employee_id, type: 'shift', name: row.name, role: row.role, rate: amount(row.rate),
        hasRate: row.rate !== null && row.rate !== undefined, status: row.status,
        firstEntry: row.first_entry, manual: Boolean(row.manual_attendance),
        noHik: !row.hikvision_registered};
    });
    const monthly = (team.monthly || []).map(function (row) {
      const salary = amount(row.salary);
      const paid = amount(row.card) + amount(row.cash) + amount(row.advances);
      return {id: row.id, type: 'monthly', name: row.name, role: row.role, rate: salary, hasRate: true,
        status: 'monthly', paid: paid, rest: amount(row.remaining), noHik: false, manual: false};
    });
    return shift.concat(monthly);
  }

  function teamMatches(row, filter) {
    if (filter === 'shift') return row.type === 'shift';
    if (filter === 'monthly') return row.type === 'monthly';
    if (filter === 'late') return row.status === 'late';
    if (filter === 'missing') return row.status === 'missing';
    if (filter === 'nohik') return row.noHik;
    return true;
  }

  return {
    dishKey: dishKey,
    isDish: isDish,
    lowMargin: lowMargin,
    menuSlice: menuSlice,
    trend: trend,
    dayTips: dayTips,
    teamRows: teamRows,
    teamMatches: teamMatches,
    LOW_MARGIN: LOW_MARGIN,
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
