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

  /** Средняя фактическая цена позиции (ТЗ 3.2): выручка продаж на проданное
   *  количество, после скидок. Счёт Шефа и дегустация выручки не дают и в цену
   *  не входят. В ответе без разбивки (старый формат) отделить их нечем —
   *  там выручка на всё количество. Нет платных продаж — цены нет (null). */
  function unitPrice(quantity, revenue) {
    return quantity > 0 && revenue > 0 ? revenue / quantity : null;
  }

  function entries(group) {
    if (!group) return [];
    return Object.keys(group).map(function (name) {
      const metric = group[name];
      const revenue = amount(metric.revenue);
      const cost = amount(metric.cost);
      const profit = amount(metric.gross_profit);
      const quantity = amount(metric.quantity);
      const breakdown = metric.breakdown ? Object.fromEntries(
        ['sales', 'chef', 'tasting', 'other_zero'].map(function (key) {
          const part = metric.breakdown[key] || {};
          return [key, { quantity: amount(part.quantity), revenue: amount(part.revenue),
            cost: amount(part.cost), profit: amount(part.gross_profit) }];
        })) : null;
      return {
        name: name,
        quantity: quantity,
        revenue: revenue,
        cost: cost,
        profit: profit,
        breakdown: breakdown,
        price: breakdown ? unitPrice(breakdown.sales.quantity, breakdown.sales.revenue) : unitPrice(quantity, revenue),
        // Маржу считаем сами, а не берём строкой: позиция с нулевой выручкой
        // не имеет процента, и рисовать у неё ноль было бы враньём.
        margin: revenue > 0 ? (profit / revenue) * 100 : null,
      };
    });
  }

  /** Ценовые диапазоны (ТЗ 3.2). Пороги в сум приходят вместе с отчётом
   *  (DIRECTOR_PRICE_TIERS на сервере, по умолчанию 30 000 и 45 000): два
   *  порога — три диапазона «до 30 тыс», «30–45 тыс», «от 45 тыс». Порог
   *  входит в верхний диапазон: блюдо за 45 000 — «от 45 тыс». */
  const PRICE_TIERS = [30000, 45000];

  function thousands(value) { return String(Math.round(value / 100) / 10).replace('.', ','); }

  function priceTiers(snapshot) {
    const raw = snapshot && Array.isArray(snapshot.price_tiers) ? snapshot.price_tiers : PRICE_TIERS;
    const limits = raw.map(Number).filter(function (value) { return Number.isFinite(value) && value > 0; })
      .sort(function (a, b) { return a - b; })
      .filter(function (value, index, list) { return index === 0 || value !== list[index - 1]; });
    if (!limits.length) return [];
    return limits.map(function (limit, index) {
      const low = index ? limits[index - 1] : null;
      return {key: (low === null ? '' : low) + '-' + limit, min: low, max: limit,
        label: low === null ? 'до ' + thousands(limit) + ' тыс' : thousands(low) + '–' + thousands(limit) + ' тыс'};
    }).concat([{key: limits[limits.length - 1] + '-', min: limits[limits.length - 1], max: null,
      label: 'от ' + thousands(limits[limits.length - 1]) + ' тыс'}]);
  }

  /** Попадает ли позиция в диапазон цены. Пустой ключ — «Все»; позиция без
   *  цены (не продавалась за деньги) в диапазоны не попадает. */
  function inPriceTier(row, key, tiers) {
    if (!key) return true;
    const tier = (tiers || []).find(function (item) { return item.key === key; });
    if (!tier) return true;
    if (row.price === null || row.price === undefined) return false;
    return (tier.min === null || row.price >= tier.min) && (tier.max === null || row.price < tier.max);
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
    // Один день («Вчера») — «28 сентября 2026», а не «28 — 28 сентября».
    if (String(startIso) === String(endIso)) return dayLabel(startIso);
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
  const NOT_DISH = /контейнер|пакет|упаковк|одноразов|аренд|депозит|обслуживан|сервисн|доставк/i;
  const NOT_DISH_GROUP = /контейнер/i;
  function isDish(row, group) { return !NOT_DISH.test(row.name) && !NOT_DISH_GROUP.test(group || ''); }
  /** Справочник меню (сервер, item_classes): ингредиент, компонент сета или
   *  услуга — не блюдо, даже если название проходит isDish (ТЗ 2.3: «Мед»). */
  function menuDish(snapshot) {
    const groups = (snapshot && snapshot.item_groups) || {};
    const classes = (snapshot && snapshot.item_classes) || {};
    return function (row) { return !classes[row.name] && isDish(row, groups[row.name]); };
  }

  /** «Слабые» — без напитков и выпечки (3.11): чай и хлеб всегда продаются
   *  понемногу, и убирать их из меню никто не станет. Группа — DishGroup iiko;
   *  у позиций без группы (и в группе «Доставка Яндекс») решает название. */
  const DRINK_OR_BAKERY_GROUP = /напит|лимонад|кофе|чай|вино|водк|коньяк|коктейл|алкогол|пиво|хлеб|выпеч|соус/i;
  // \b в JS не видит границ кириллических слов, поэтому начало слова — явно:
  // иначе «кола» находилась бы в каждой «ШКОЛА …», а «чай» — в «случайный».
  const DRINK_OR_BAKERY_NAME = /напиток|(^|[^а-яё])(сок|чай|кола|лед)([^а-яё]|$)|вода|кофе|капучино|латте|американо|эспрессо|лимонад|cola|fanta|sprite|pepsi|пиво|вино|водка|коньяк|коктейл|молоко|компот|айран|морс|хлеб|лепешк|самса|булочк|круассан|выпечк|соус/i;
  function isDrinkOrBakery(name, group) {
    return DRINK_OR_BAKERY_GROUP.test(group || '') || DRINK_OR_BAKERY_NAME.test(name || '');
  }

  /** Низкая маржа — по тому же округлению, что видит директор: иначе
   *  блюдо с «45%» на экране попадало в список «ниже 45%». */
  function lowMargin(row) { return row.margin !== null && Math.round(row.margin) < LOW_MARGIN; }

  /** Сортировки срезов меню: сверху — лучшие. Маржа без выручки — в конце. */
  const SLICE_SORTS = {
    revenue: function (a, b) { return b.revenue - a.revenue || b.quantity - a.quantity; },
    quantity: function (a, b) { return b.quantity - a.quantity || b.revenue - a.revenue; },
    margin: function (a, b) {
      if (a.margin === null && b.margin === null) return b.revenue - a.revenue;
      if (a.margin === null) return 1;
      if (b.margin === null) return -1;
      return b.margin - a.margin || b.revenue - a.revenue;
    },
  };

  /** Категории (группы iiko) проданных блюд заведения — для фильтра меню. */
  function menuCategories(snapshot, venue) {
    const metrics = (snapshot && snapshot.item_metrics) || {};
    const groups = (snapshot && snapshot.item_groups) || {};
    const dish = menuDish(snapshot);
    const counts = {};
    entries(metrics[venue === 'all' || !venue ? 'all' : venue]).forEach(function (row) {
      const group = groups[row.name];
      if (row.revenue > 0 && group && dish(row)) counts[group] = (counts[group] || 0) + 1;
    });
    return Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a] || a.localeCompare(b, 'ru'); })
      .map(function (name) { return {name: name, count: counts[name]}; });
  }

  /** Срезы меню: лидеры, слабые, низкая маржа, нет в Бехрузе и все блюда
   *  (`all`, ТЗ 3.3). `snapshot` — отчёт директора; `venue` — all | retro |
   *  oxbridge | banquet. `options.sort` — revenue (по умолчанию) | quantity |
   *  margin: популярность не по числу дешёвых позиций (ТЗ 2.7); «Слабые» —
   *  снизу по тому же показателю. `options.category` — группа iiko (DishGroup),
   *  `options.price` — ключ ценового диапазона (priceTiers). `limit` — сколько
   *  строк вернуть (5 по умолчанию, Infinity — все). У строки есть цена и
   *  доля в выручке заведения (`share`) — основание среза «Низкая маржа». */
  function menuSlice(snapshot, venue, view, limit, options) {
    const opts = options || {};
    const sort = SLICE_SORTS[opts.sort] ? opts.sort : 'revenue';
    const metrics = (snapshot && snapshot.item_metrics) || {};
    const groups = (snapshot && snapshot.item_groups) || {};
    const dish = menuDish(snapshot);
    const count = limit || 5;
    // Выручка заведения целиком — основание «доли в выручке» у среза.
    let rows, base = 0;
    if (view === 'notb') {
      // Хиты зала, которых нет в банкетном меню. Фильтр заведения здесь не
      // действует: сравниваем весь зал (Retro + Oxbridge) с Бехрузом.
      const banquet = new Set(entries(metrics.banquet).map(function (row) { return dishKey(row.name); }));
      const hall = {};
      ['retro', 'oxbridge'].forEach(function (group) {
        entries(metrics[group]).forEach(function (row) {
          const current = hall[row.name] || {name: row.name, quantity: 0, revenue: 0, cost: 0, profit: 0,
            salesQuantity: 0, salesRevenue: 0};
          const sales = row.breakdown ? row.breakdown.sales : row;
          current.quantity += row.quantity; current.revenue += row.revenue;
          current.cost += row.cost; current.profit += row.profit;
          current.salesQuantity += sales.quantity; current.salesRevenue += sales.revenue;
          hall[row.name] = current;
        });
        base += totals(metrics[group]).revenue;
      });
      rows = Object.keys(hall).map(function (name) {
        const row = hall[name];
        row.margin = row.revenue > 0 ? row.profit / row.revenue * 100 : null;
        row.price = unitPrice(row.salesQuantity, row.salesRevenue);
        return row;
      }).filter(function (row) { return row.revenue > 0 && dish(row) && !banquet.has(dishKey(row.name)); });
      rows.sort(SLICE_SORTS[sort]);
    } else {
      base = totals(metrics[venue === 'all' ? 'all' : venue]).revenue;
      const sold = entries(metrics[venue === 'all' ? 'all' : venue])
        .filter(function (row) { return row.revenue > 0 && dish(row); });
      if (view === 'weak') {
        rows = sold.filter(function (row) { return !isDrinkOrBakery(row.name, groups[row.name]); })
          .sort(function (a, b) { return SLICE_SORTS[sort](b, a); });
      } else if (view === 'lowm') {
        rows = sold.filter(lowMargin)
          .sort(SLICE_SORTS[sort]);
      } else {
        rows = sold.slice().sort(SLICE_SORTS[sort]);
      }
    }
    if (opts.category) rows = rows.filter(function (row) { return (groups[row.name] || '') === opts.category; });
    if (opts.price) {
      const tiers = priceTiers(snapshot);
      rows = rows.filter(function (row) { return inPriceTier(row, opts.price, tiers); });
    }
    return rows.slice(0, count).map(function (row) {
      return Object.assign({}, row, {category: groups[row.name] || '', share: base > 0 ? row.revenue / base : null});
    });
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

  /** Советы дня по данным меню, без обращения к AI (ТЗ 16.10, 3.1).
   *  Модель зовём, только когда директор спрашивает подробнее: совет на
   *  главном экране должен открываться мгновенно и не стоить запроса.
   *
   *  `week` — отчёт за 7 дней; `options.short`/`options.long` — за 3 и 30 дней
   *  (тренд и дни недели), `options.today` — дата ресторана. Каждый совет
   *  несёт основание и период (ТЗ 2.4). Неделя сдвигается на день, поэтому
   *  «не акционировать» и «убрать» берутся из трёх первых по очереди дней —
   *  иначе один и тот же совет висел неделями (ТЗ 2.2). «Просело» и «день
   *  недели» меняются вместе с данными. */
  const PROMO_MARGIN = 60;
  const DROP = -0.35;
  const WEEKDAY_SHIFT = 0.1;
  const WEEKDAYS_DATIVE = ['воскресеньям', 'понедельникам', 'вторникам', 'средам', 'четвергам', 'пятницам', 'субботам'];

  function dayNumber(iso) {
    const time = Date.parse(String(iso || '') + 'T12:00:00Z');
    return Number.isFinite(time) ? Math.floor(time / 86400000) : 0;
  }

  function boosted(row) { return row.profit * (1 + Math.max(-0.5, Math.min(1, row.trend || 0))); }

  /** Кандидат «по очереди дней»: из первых трёх, но всегда один и тот же за день. */
  function rotate(rows, today) {
    if (!rows.length) return null;
    return rows[dayNumber(today) % Math.min(3, rows.length)];
  }

  /** Выручка Retro в этот день недели против среднего дня — по отчёту за 30 дней. */
  function weekdayShift(long, today) {
    const days = (long && long.daily_totals) || [];
    if (!days.length || !today) return null;
    const weekday = new Date(today + 'T12:00:00Z').getUTCDay();
    const revenue = function (row) { return amount((row.directions || {}).retro); };
    const all = days.map(revenue);
    const same = days.filter(function (row) { return new Date(row.day + 'T12:00:00Z').getUTCDay() === weekday; }).map(revenue);
    if (same.length < 2) return null;
    const average = all.reduce(function (sum, value) { return sum + value; }, 0) / all.length;
    if (!average) return null;
    const mean = same.reduce(function (sum, value) { return sum + value; }, 0) / same.length;
    return {weekday: weekday, shift: mean / average - 1, days: same.length};
  }

  function dayTips(week, options) {
    const opts = options || {};
    const metrics = (week && week.item_metrics) || {};
    const groups = (week && week.item_groups) || {};
    const short = opts.short || null, long = opts.long || null, today = opts.today || '';
    const period = week && week.period_start ? {start: week.period_start, end: week.period_end} : null;
    const withTrend = function (row) {
      return Object.assign({}, row, {trend: trend(row.name, 'retro', short, long)});
    };
    const dish = menuDish(week);
    const sold = entries(metrics.retro).filter(function (row) { return row.revenue > 0 && dish(row); })
      .map(withTrend);
    const tips = [];
    const used = {};
    const add = function (tip) { if (tip && !used[tip.dish]) { used[tip.dish] = true; tips.push(tip); } };

    const day = weekdayShift(long, today);
    if (day && Math.abs(day.shift) >= WEEKDAY_SHIFT) {
      tips.push({kind: 'weekday', weekday: day.weekday, shift: day.shift, days: day.days,
        weekdayDative: WEEKDAYS_DATIVE[day.weekday],
        period: long ? {start: long.period_start, end: long.period_end} : null});
    }
    // Продвигать: три самых прибыльных с высокой маржой, растущие — выше;
    // по очереди дней, а не один и тот же лидер неделями.
    const promo = rotate(sold.filter(function (row) { return row.margin !== null && row.margin >= PROMO_MARGIN; })
      .sort(function (a, b) { return boosted(b) - boosted(a); }), today);
    if (promo) add({dish: promo.name, kind: 'promo', margin: promo.margin, quantity: promo.quantity,
      revenue: promo.revenue, trend: promo.trend, period: period});
    // Просело: заметное блюдо (в первой двадцатке по количеству) упало к среднему за 30.
    const popular = sold.slice().sort(SORTS.quantity).slice(0, 20);
    const drop = popular.filter(function (row) { return row.trend !== null && row.trend <= DROP; })
      .sort(function (a, b) { return a.trend - b.trend; })[0];
    if (drop) add({dish: drop.name, kind: 'drop', trend: drop.trend, quantity: drop.quantity, period: period});
    const trap = rotate(sold.filter(lowMargin).sort(SORTS.revenue)
      .filter(function (row) { return !used[row.name]; }), today);
    if (trap) add({dish: trap.name, kind: 'trap', margin: trap.margin, revenue: trap.revenue, period: period});
    // «Убрать из меню» — как срез «Слабые»: без напитков и выпечки.
    const weak = rotate(sold.filter(function (row) { return row.quantity > 0 && !isDrinkOrBakery(row.name, groups[row.name]) && !used[row.name]; })
      .sort(function (a, b) { return a.quantity - b.quantity || a.revenue - b.revenue; }), today);
    if (weak) add({dish: weak.name, kind: 'weak', quantity: weak.quantity, period: period});
    return tips.slice(0, 4);
  }

  /** Чего не хватает советам дня: директор должен видеть, что они неполные (ТЗ 2.4). */
  function tipNotes(week, options) {
    const opts = options || {};
    const notes = [];
    if (!opts.short || !opts.long) notes.push('Тренд к 30 дням недоступен — советы только по неделе.');
    if (week && week.missing_days && week.missing_days.length) {
      notes.push('В неделе нет данных iiko за ' + week.missing_days.length + ' дн. — цифры неполные.');
    }
    return notes;
  }

  /** Строки «Команды»: сменные из Hikvision и оклады из реестра бухгалтера.
   *  Месяц считает сервер (3.5): у сменного — смены и выдано с 1-го числа, у
   *  оклада — выдано частями и остаток; остаток меньше нуля — переплата. */
  function teamRows(team) {
    if (!team) return [];
    const shift = (team.shift || []).map(function (row) {
      return {id: row.employee_id, type: 'shift', name: row.name, role: row.role, rate: amount(row.rate),
        hasRate: row.rate !== null && row.rate !== undefined, status: row.status,
        firstEntry: row.first_entry, manual: Boolean(row.manual_attendance),
        noHik: !row.hikvision_registered,
        monthShifts: row.month_shifts === undefined ? null : Number(row.month_shifts) || 0,
        monthPaid: amount(row.month_paid)};
    });
    const monthly = (team.monthly || []).map(function (row) {
      const salary = amount(row.salary);
      const known = row.month_paid !== undefined && row.month_paid !== null;
      // Старый ответ без месяца — ручной реестр: полей выплат хватает только на «выдано».
      const paid = known ? amount(row.month_paid) : amount(row.card) + amount(row.cash) + amount(row.advances);
      const rest = known ? amount(row.month_left) : amount(row.remaining);
      return {id: row.id, type: 'monthly', name: row.name, role: row.role, rate: salary, hasRate: true,
        status: 'monthly', paid: paid, rest: rest, monthKnown: known,
        state: salaryState(salary, paid), noHik: false, manual: false};
    });
    return shift.concat(monthly);
  }

  /** Оклад за месяц: «осталось», «закрыт» (выдано ровно оклад) или «переплата». */
  function salaryState(salary, paid) {
    if (paid > salary) return 'over';
    if (salary > 0 && paid === salary) return 'closed';
    return 'left';
  }

  /** Официанты Retro за период (3.11): по выручке, с чеками, сменами и
   *  средним чеком. Ответ без разбивки Retro — общий список без чеков. */
  function retroWaiters(snapshot) {
    const stats = snapshot && snapshot.waiter_retro;
    if (!stats) return waiters((snapshot && snapshot.waiter_metrics) || {}).map(function (row) {
      return {name: row.name, revenue: row.revenue, checks: null, shifts: null, averageCheck: null};
    });
    return Object.keys(stats).map(function (name) {
      const row = stats[name];
      const checks = Number(row.checks) || 0;
      return {name: name, revenue: amount(row.revenue), checks: checks, shifts: Number(row.shifts) || 0,
        averageCheck: checks ? amount(row.revenue) / checks : null};
    }).filter(function (row) { return row.revenue > 0; })
      .sort(function (a, b) { return b.revenue - a.revenue; });
  }

  /** Поиск человека по имени и должности (ТЗ 3.2): без регистра, «ё» = «е»,
   *  кириллица находится латиницей (Alijon → Алижон, Shoxrux / Shokhrukh →
   *  Шохрух). Та же нормализация, что у бухгалтера в «Сотрудниках»
   *  (employees-logic.js): тот модуль директору не открыт, поэтому — копия. */
  const CYR = {а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ж: 'j', з: 'z', и: 'i', й: 'y', к: 'k', л: 'l',
    м: 'm', н: 'n', о: 'o', п: 'p', р: 'r', с: 's', т: 't', у: 'u', ф: 'f', х: 'x', ц: 's', ч: 'ch', ш: 'sh',
    щ: 'sh', ъ: '', ы: 'i', ь: '', э: 'e', ю: 'yu', я: 'ya', ў: 'o', қ: 'q', ғ: 'g', ҳ: 'x'};
  function plainText(value) { return String(value || '').toLowerCase().replace(/ё/g, 'е').replace(/\s+/g, ' ').trim(); }
  function latinKey(value) {
    // Для латиницы «ё» — это «yo» (Ихтиёр → Ixtiyor), а не «е».
    const cyr = String(value || '').toLowerCase().replace(/\s+/g, ' ').trim().replace(/ё/g, 'yo')
      .replace(/[а-яўқғҳ]/g, function (ch) { return CYR[ch] !== undefined ? CYR[ch] : ch; });
    return cyr.replace(/[ʻʼ’‘'`]/g, '').replace(/kh/g, 'x').replace(/zh/g, 'j').replace(/ts/g, 's')
      .replace(/(^|[^sc])h/g, '$1x').replace(/ye/g, 'e').replace(/w/g, 'v');
  }
  function personMatches(query) {
    const fields = Array.prototype.slice.call(arguments, 1);
    const needle = plainText(query);
    if (!needle) return true;
    const latin = latinKey(needle);
    return fields.some(function (field) {
      return plainText(field).indexOf(needle) !== -1 || Boolean(latin && latinKey(field).indexOf(latin) !== -1);
    });
  }

  /** Строки «Команды» под поиском: имя или должность. */
  function teamSearch(rows, query) {
    return (rows || []).filter(function (row) { return personMatches(query, row.name, row.role); });
  }

  /** Чипы фильтра «Команды» со счётчиками. Пустой чип не показываем: в нём
   *  некого смотреть. «Все» и выбранный остаются всегда — выбранный приходит
   *  и с «Сегодня» (карточка «опоздали»), и пропавший чип запутал бы. */
  const TEAM_FILTERS = [['all', 'Все'], ['shift', 'Сменные'], ['monthly', 'Оклад'], ['late', 'Опоздали'],
    ['missing', 'Не пришли'], ['nohik', 'Без Hikvision']];
  function teamChips(rows, active) {
    return TEAM_FILTERS.map(function (pair) {
      return {key: pair[0], label: pair[1], count: (rows || []).filter(function (row) { return teamMatches(row, pair[0]); }).length};
    }).filter(function (chip) { return chip.key === 'all' || chip.key === active || chip.count > 0; });
  }

  function teamMatches(row, filter) {
    if (filter === 'shift') return row.type === 'shift';
    if (filter === 'monthly') return row.type === 'monthly';
    if (filter === 'late') return row.status === 'late';
    // «Не пришли» — как счётчик сервера: и по турникету, и отмеченные вручную.
    if (filter === 'missing') return row.status === 'missing' || row.status === 'manual_absent';
    if (filter === 'nohik') return row.noHik;
    return true;
  }

  // ── Обед на телефоне директора (T-500, ТЗ 16.10 §2) ───────────────────
  // Данные — история чеков iiko в базе панели (/api/director/sales, T-499),
  // окно обеда — LUNCH_HOURS сервера (по умолчанию 12:00–15:00 по закрытию чека).

  /** Направления плитки «Обед»: Retro и Школа. Банкетный зал (свадьбы в зале
   *  Бехруза) по умолчанию в обед не входит: это не обеденный зал, и одна
   *  свадьба перекрыла бы весь обед. Сервер считает только эти направления. */
  const LUNCH_DIRECTIONS = ['retro', 'school'];
  /** Имена и цвета направлений — как у кассира и учредителя. */
  const SALES_DIRECTIONS = {retro: {name: 'Retro', color: '#143e35'}, school: {name: 'Школа', color: '#52786f'},
    banquet: {name: 'Банкет', color: '#a27445'}};
  const LUNCH_TOP = 5;

  function lunchUrl(day, directions) {
    return '/api/director/sales?start=' + day + '&end=' + day + '&window=lunch&direction='
      + (directions || LUNCH_DIRECTIONS).join(',') + '&top=' + LUNCH_TOP;
  }

  /** Подпись направлений обеда: кто входит и что нет. */
  function lunchScope(directions) {
    const keys = directions || LUNCH_DIRECTIONS;
    const names = keys.map(function (key) { return (SALES_DIRECTIONS[key] || {name: key}).name; });
    const text = names.length > 1 ? names.slice(0, -1).join(', ') + ' и ' + names[names.length - 1] : names[0] || '';
    return keys.indexOf('banquet') === -1 ? text + ' · банкетный зал не входит' : text;
  }

  const SUMS = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 0});
  /** «2,4 млн» и «850 000» — как суммы в плитках телефона. */
  function compactSum(value) {
    const number = Math.round(amount(value));
    if (Math.abs(number) >= 1e6) return String(Math.round(number / 1e5) / 10).replace('.', ',') + ' млн';
    return SUMS.format(number);
  }

  /** «58 чеков · средний чек 55 000»; без чеков — «0 чеков»: это ноль, а не пропуск. */
  function receiptsText(receipts, average) {
    const count = Number(receipts) || 0;
    const text = count + ' ' + plural(count, 'чек', 'чека', 'чеков');
    return count && average !== null && average !== undefined ? text + ' · средний чек ' + compactSum(average) : text;
  }

  /** «13:42» из времени сервера: оно уже в поясе ресторана (+05:00). */
  function clockOf(iso) {
    const match = /T(\d{2}):(\d{2})/.exec(String(iso || ''));
    return match ? match[1] + ':' + match[2] : '';
  }

  /** Сейчас по Ташкенту, «ЧЧ:ММ»: окно обеда — часы ресторана, а не телефона. */
  function tashkentClock(moment) {
    const parts = new Intl.DateTimeFormat('en-GB', {hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
      timeZone: 'Asia/Tashkent'}).formatToParts(moment || new Date());
    const pick = function (type) { return (parts.find(function (part) { return part.type === type; }) || {}).value || '00'; };
    return pick('hour') + ':' + pick('minute');
  }

  function minutesOf(clock) {
    const match = /^(\d{1,2}):(\d{2})$/.exec(String(clock || ''));
    return match ? Number(match[1]) * 60 + Number(match[2]) : null;
  }

  function clockText(minutes) {
    return String(Math.floor(minutes / 60)).padStart(2, '0') + ':' + String(minutes % 60).padStart(2, '0');
  }

  /** Границы окна в минутах из подписи сервера «12:00–15:00». */
  function windowBounds(label) {
    const match = /^(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})$/.exec(String(label || '').trim());
    if (!match) return null;
    const start = minutesOf(match[1]), end = minutesOf(match[2]);
    return start !== null && end !== null && start < end ? {start: start, end: end} : null;
  }

  /** Часы окна по порядку, пустые тоже: в загруженном дне пустой час — честный
   *  ноль. Сегодня час, который начался после последней загрузки, — «впереди»
   *  (`ahead`), а не ноль; час, в котором была загрузка, — «идёт» (`current`).
   *  `upcoming` — час ещё не наступил по часам ресторана; иначе он просто ещё
   *  не загружен. Полоса — доля от самого сильного часа. */
  function lunchHours(data, bounds, options) {
    const opts = options || {};
    const byHour = {};
    ((data && data.by_hour) || []).forEach(function (row) { byHour[Number(String(row.hour).slice(0, 2))] = row; });
    const rows = [];
    if (bounds) {
      const loaded = opts.isToday ? minutesOf(opts.updated) : null;
      const now = opts.isToday ? minutesOf(opts.now) : null;
      for (let hour = Math.floor(bounds.start / 60); hour * 60 < bounds.end; hour += 1) {
        const from = Math.max(bounds.start, hour * 60), to = Math.min(bounds.end, hour * 60 + 60);
        const row = byHour[hour];
        const phase = !opts.isToday || loaded === null ? 'done'
          : loaded >= to ? 'done' : (loaded >= from || row) ? 'current' : 'ahead';
        rows.push({label: clockText(from) + '–' + clockText(to), phase: phase,
          upcoming: phase === 'ahead' && (now === null || now < from),
          revenue: row ? amount(row.revenue) : 0, receipts: row ? Number(row.receipts) || 0 : 0,
          average: row && row.average_check !== null && row.average_check !== undefined ? amount(row.average_check) : null});
      }
    } else {
      // Подписи окна нет (старый ответ) — только часы с продажами, как пришли.
      Object.keys(byHour).sort().forEach(function (hour) {
        const row = byHour[hour];
        rows.push({label: row.hour, phase: 'done', upcoming: false, revenue: amount(row.revenue),
          receipts: Number(row.receipts) || 0,
          average: row.average_check !== null && row.average_check !== undefined ? amount(row.average_check) : null});
      });
    }
    const max = Math.max.apply(null, [0].concat(rows.map(function (row) { return row.revenue; })));
    rows.forEach(function (row) { row.share = max > 0 ? row.revenue / max : 0; });
    return rows;
  }

  /** Направления обеда в порядке плитки; направление без продаж — ноль. */
  function lunchDirections(data, directions) {
    const table = (data && data.by_direction) || {};
    const rows = (directions || LUNCH_DIRECTIONS).map(function (key) {
      const item = table[key];
      const info = SALES_DIRECTIONS[key] || {name: key, color: '#24594b'};
      return {key: key, name: info.name, color: info.color, revenue: item ? amount(item.revenue) : 0,
        receipts: item ? Number(item.receipts) || 0 : 0,
        average: item && item.average_check !== null && item.average_check !== undefined ? amount(item.average_check) : null};
    });
    const max = Math.max.apply(null, [0].concat(rows.map(function (row) { return row.revenue; })));
    rows.forEach(function (row) { row.share = max > 0 ? row.revenue / max : 0; });
    return rows;
  }

  /** Топ блюд окна: сервер уже отобрал готовые блюда и напитки по справочнику. */
  function lunchTop(data, limit) {
    return ((data && data.top_items) || []).slice(0, limit || LUNCH_TOP).map(function (item) {
      return {name: item.name, quantity: amount(item.quantity), revenue: amount(item.revenue)};
    });
  }

  /** Почему данных нет. Сервер к ошибке обновления дописывает «показаны данные
   *  последней загрузки» — когда показать нечего, эту часть не повторяем. */
  function refreshReason(text) {
    const value = String(text || '').trim();
    if (/^iiko отвечает медленно/.test(value)) return 'iiko отвечает медленно: день ещё загружается.';
    if (/^iiko сейчас не отвечает/.test(value)) return 'iiko сейчас не отвечает.';
    return value.replace(/\s*Показаны данные последней загрузки[^.]*\.?$/, '') || 'iiko не вернул продажи.';
  }

  function dottedDay(iso) {
    const parts = String(iso || '').split('-');
    return parts.length === 3 ? parts[2] + '.' + parts[1] + '.' + parts[0] : '';
  }

  /** Обед за день — состояние для плитки и листа. `data` — ответ
   *  /api/director/sales (null — ещё грузится). `options`: day и today — даты
   *  ресторана, now — «ЧЧ:ММ» по Ташкенту, error — запрос не удался.
   *  Состояния: `loading`; `unavailable` — iiko или синхронизация недоступны,
   *  это «нет данных» с причиной, а не ноль; `missing` — прошедший день не
   *  загружен («за этот день данных нет»); `ok` — цифры, и ноль здесь
   *  настоящий («0 сум»). Сегодня всегда `partial`: учтены только чеки,
   *  закрытые к `updated` (открытые столы iiko не отдаёт). */
  function lunchView(data, options) {
    const opts = options || {};
    const day = opts.day || opts.today || '';
    const isToday = Boolean(day) && day === opts.today;
    const base = {day: day, isToday: isToday, window: '', basis: '', updated: '', notes: []};
    if (opts.error) {
      const error = opts.error;
      // Без кода ответа — запрос не дошёл до панели (сеть, сервер перезапускается).
      return Object.assign(base, {state: 'unavailable',
        reason: error.status ? String(error.message || 'Не удалось получить данные.') : 'Нет связи с панелью. Проверьте интернет и откройте снова.'});
    }
    if (!data) return Object.assign(base, {state: 'loading'});
    base.window = (data.window && data.window.label) || data.lunch_hours || '';
    base.basis = (data.window && data.window.time) || '';
    base.updated = clockOf(data.updated_at);
    // Прошедший день читается следующим утром: «на 07:00» без даты сбивало бы с толку.
    const read = String(data.updated_at || '').slice(0, 10);
    base.updatedLabel = !base.updated ? '' : read && read !== day
      ? 'на ' + read.slice(8, 10) + '.' + read.slice(5, 7) + ' ' + base.updated : 'на ' + base.updated;
    base.historyStart = data.history_start || '';
    if (!data.totals) {
      let reason;
      if (data.refresh_error) reason = refreshReason(data.refresh_error);
      else if (data.configured === false) reason = 'iiko не подключено: продажи загрузить неоткуда.';
      else if (data.history_start && day < data.history_start) reason = 'История продаж хранится с ' + dottedDay(data.history_start) + '.';
      else if (isToday) reason = 'Сегодняшние чеки ещё не загружены из iiko.';
      else reason = 'День ещё не загружен из iiko: он стоит в очереди загрузки.';
      return Object.assign(base, {state: isToday ? 'unavailable' : 'missing', reason: reason});
    }
    const totals = data.totals;
    const partial = (data.partial_days || []).indexOf(day) !== -1 || (isToday && Boolean(data.partial));
    Object.assign(base, {
      state: 'ok', revenue: amount(totals.revenue), receipts: Number(totals.receipts) || 0,
      average: totals.average_check === null || totals.average_check === undefined ? null : amount(totals.average_check),
      // Сегодня — «день идёт»; прошедший день, не перечитанный после 06:00, ещё может уточниться.
      partial: isToday && partial, stale: !isToday && partial, refreshError: data.refresh_error || '',
      hours: lunchHours(data, windowBounds(base.window), {isToday: isToday, updated: base.updated, now: opts.now}),
      directions: lunchDirections(data, opts.directions), top: lunchTop(data),
    });
    if (base.partial) base.notes.push('День идёт: учтены чеки, закрытые к ' + base.updated + '. Открытые столы iiko не отдаёт.');
    if (base.stale) base.notes.push('День ещё не перечитан после закрытия смены: цифры могут уточниться.');
    if (base.refreshError) base.notes.push(base.refreshError);
    if ((data.by_day || []).some(function (row) { return row.status && row.status !== 'ok'; })) {
      base.notes.push('Сверка с итогом iiko за этот день не сошлась.');
    }
    return base;
  }

  /** Тексты плитки «Обед сегодня». `value: null` — скелет, ещё грузится. */
  function lunchTile(state) {
    if (state.state === 'loading') return {value: null, split: '', sub: '', note: '', badge: '', empty: false};
    if (state.state !== 'ok') return {value: 'Нет данных', split: '', sub: state.reason, note: '', badge: '', empty: true};
    return {
      value: compactSum(state.revenue) + ' сум',
      split: state.directions.map(function (row) { return row.name + ' ' + compactSum(row.revenue); }).join(' · '),
      sub: receiptsText(state.receipts, state.average),
      note: state.updatedLabel ? state.updatedLabel + (state.refreshError ? ' · обновить не удалось' : '') : '',
      badge: state.partial ? 'день идёт' : '', empty: false,
    };
  }

  /** Вопрос AI про обед выбранного дня: инструмент продаж по времени (T-499). */
  function lunchQuestion(state) {
    const when = state.isToday ? 'сегодня' : dayLabel(state.day);
    return 'Как прошёл обед ' + when + ': выручка Retro и Школы без банкетного зала, чеки и что продавалось '
      + 'лучше всего? Сравни с тем же днём прошлой недели.';
  }

  return {
    LUNCH_DIRECTIONS: LUNCH_DIRECTIONS,
    SALES_DIRECTIONS: SALES_DIRECTIONS,
    lunchUrl: lunchUrl,
    lunchScope: lunchScope,
    compactSum: compactSum,
    receiptsText: receiptsText,
    clockOf: clockOf,
    tashkentClock: tashkentClock,
    windowBounds: windowBounds,
    lunchHours: lunchHours,
    lunchDirections: lunchDirections,
    lunchTop: lunchTop,
    lunchView: lunchView,
    lunchTile: lunchTile,
    lunchQuestion: lunchQuestion,
    dishKey: dishKey,
    isDish: isDish,
    isDrinkOrBakery: isDrinkOrBakery,
    salaryState: salaryState,
    retroWaiters: retroWaiters,
    lowMargin: lowMargin,
    menuSlice: menuSlice,
    menuCategories: menuCategories,
    trend: trend,
    dayTips: dayTips,
    menuDish: menuDish,
    tipNotes: tipNotes,
    teamRows: teamRows,
    teamMatches: teamMatches,
    teamSearch: teamSearch,
    teamChips: teamChips,
    personMatches: personMatches,
    priceTiers: priceTiers,
    inPriceTier: inPriceTier,
    PRICE_TIERS: PRICE_TIERS,
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
