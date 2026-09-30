import test from 'node:test';
import assert from 'node:assert/strict';
import logic from '../../retro/static/director-logic.js';

const metric = (quantity, revenue, cost) => ({quantity: String(quantity), revenue: String(revenue),
  cost: String(cost), gross_profit: String(revenue - cost)});

const snapshot = {
  period_days: 7,
  item_metrics: {
    all: {'Плов': metric(100, 6500000, 2500000), 'Лагман': metric(60, 2880000, 2000000),
      'Контейнер 1-х (Миллий)': metric(250, 500000, 100000), 'Халим': metric(1, 42000, 20000)},
    retro: {'Плов': metric(70, 4550000, 1750000), 'Лагман': metric(60, 2880000, 2000000),
      'Контейнер 1-х (Миллий)': metric(250, 500000, 100000), 'Халим': metric(1, 42000, 20000)},
    oxbridge: {'Плов': metric(30, 1950000, 750000), 'Салат Цезарь': metric(40, 2000000, 800000)},
    banquet: {'СВАДЬБА Салат Цезарь (Бехруз)': metric(80, 0, 900000)},
  },
};

test('банкетная обёртка не мешает узнать блюдо зала', () => {
  assert.equal(logic.dishKey('СВАДЬБА Салат Цезарь (Бехруз)'), logic.dishKey('Салат Цезарь'));
  assert.equal(logic.dishKey('СВАДЬБА БРУСКЕТЫ (8 шт) Бехруз'), 'брускеты 8 шт');
});

test('упаковка и аренда не попадают в рейтинг блюд', () => {
  const top = logic.menuSlice(snapshot, 'all', 'top').map(row => row.name);
  assert.deepEqual(top, ['Плов', 'Лагман', 'Халим']);
});

test('четыре среза меню считают по своим правилам', () => {
  assert.deepEqual(logic.menuSlice(snapshot, 'retro', 'weak').map(row => row.name)[0], 'Халим');
  // Лагман: маржа 30,6 % — ниже 45 %; Плов — 61,5 %.
  assert.deepEqual(logic.menuSlice(snapshot, 'all', 'lowm').map(row => row.name), ['Лагман']);
  // Цезарь уже есть в банкетном меню, поэтому «нет в Бехрузе» его не показывает.
  const notBanquet = logic.menuSlice(snapshot, 'banquet', 'notb').map(row => row.name);
  assert.deepEqual(notBanquet, ['Плов', 'Лагман', 'Халим']);
});

test('низкая маржа сравнивается по тому числу, что видно на экране', () => {
  assert.equal(logic.lowMargin({margin: 44.6}), false);  // на экране «45%»
  assert.equal(logic.lowMargin({margin: 44.4}), true);
  assert.equal(logic.lowMargin({margin: null}), false);
});

test('тренд — последние три дня к среднему за тридцать', () => {
  const short = {period_days: 3, item_metrics: {all: {'Плов': metric(60, 1, 0)}}};
  const long = {period_days: 30, item_metrics: {all: {'Плов': metric(300, 1, 0)}}};
  assert.equal(logic.trend('Плов', 'all', short, long), 1);  // 20 в день против 10
  assert.equal(logic.trend('Манты', 'all', short, long), null);
  assert.equal(logic.trend('Плов', 'all', null, long), null);
});

test('советы дня опираются на меню Retro: продвигать, не акционировать, убрать', () => {
  const tips = logic.dayTips(snapshot);
  assert.deepEqual(tips.map(tip => [tip.kind, tip.dish]), [
    ['promo', 'Плов'], ['trap', 'Лагман'], ['weak', 'Халим']]);
  assert.deepEqual(logic.dayTips({item_metrics: {}}), []);
});

test('команда: смены с посещаемостью и оклады с выплатами из реестра', () => {
  const rows = logic.teamRows({
    shift: [{employee_id: 1, name: 'Жасур', role: 'официант', rate: '180000', status: 'late',
      first_entry: '2026-09-24T10:14:00+05:00', manual_attendance: false, hikvision_registered: true},
    {employee_id: 2, name: 'Гульшан', role: 'техперсонал', rate: null, status: 'unlinked',
      first_entry: null, manual_attendance: true, hikvision_registered: false}],
    monthly: [{id: 7, name: 'Азиз', role: 'менеджер', salary: '8000000', card: '1000000',
      cash: '2000000', advances: '0', remaining: '5000000'}],
  });
  assert.equal(rows.length, 3);
  assert.equal(rows[1].hasRate, false);
  assert.equal(rows[2].paid, 3000000);
  assert.equal(rows[2].rest, 5000000);
  assert.deepEqual(rows.filter(row => logic.teamMatches(row, 'late')).map(row => row.name), ['Жасур']);
  assert.deepEqual(rows.filter(row => logic.teamMatches(row, 'nohik')).map(row => row.name), ['Гульшан']);
  assert.deepEqual(rows.filter(row => logic.teamMatches(row, 'monthly')).map(row => row.id), [7]);
});

/* «Не пришли» у директора считается как на сервере (counts.missing):
   и по турникету, и отмеченные бухгалтером вручную «не был». */
test('фильтр «Не пришли» включает отметку «не был · вручную»', () => {
  const absent = {type: 'shift', status: 'manual_absent', noHik: true};
  assert.equal(logic.teamMatches(absent, 'missing'), true);
  assert.equal(logic.teamMatches(absent, 'late'), false);
  assert.equal(logic.teamMatches({type: 'shift', status: 'missing'}, 'missing'), true);
  assert.equal(logic.teamMatches({type: 'shift', status: 'manual_present'}, 'missing'), false);
});

test('«Слабые» — без напитков и выпечки: по группе iiko и по названию', () => {
  const data = {item_metrics: {all: {
    'Халим': metric(1, 42000, 20000), 'Чай зелёный': metric(2, 10000, 2000),
    'Хлеб Лепешка': metric(3, 15000, 5000), 'ШКОЛА Эклер': metric(4, 60000, 30000),
    'Олот Самса': metric(5, 40000, 20000), 'Стакан Миллий': metric(1, 5000, 1000),
    'Лимонад Тархун': metric(2, 30000, 9000),
  }}, item_groups: {'Лимонад Тархун': 'Лимонад', 'Стакан Миллий': 'Контейнеры', 'ШКОЛА Эклер': 'Десерты (ШКОЛА)'}};
  // «кола» внутри «ШКОЛА» — не напиток: кириллица без \b.
  assert.deepEqual(logic.menuSlice(data, 'all', 'weak', 5).map(row => row.name), ['Халим', 'ШКОЛА Эклер']);
  assert.equal(logic.isDrinkOrBakery('ШКОЛА Эклер', ''), false);
  assert.equal(logic.isDrinkOrBakery('Напиток COCA COLA 1 L', 'ДОСТАВКА ЯНДЕКС'), true);
  // Лидеры упаковку из группы «Контейнеры» тоже не показывают.
  assert.ok(!logic.menuSlice(data, 'all', 'top', 10).some(row => row.name === 'Стакан Миллий'));
});

test('строка команды: месяц сменного и оклад — осталось, закрыт, переплата', () => {
  const rows = logic.teamRows({shift: [{employee_id: 1, name: 'А', role: 'официант', rate: '180000', status: 'on_time',
    hikvision_registered: true, month_shifts: 12, month_paid: '1980000'}],
  monthly: [
    {id: 7, name: 'Б', role: 'менеджер', salary: '8000000', month_paid: '3000000', month_left: '5000000'},
    {id: 8, name: 'В', role: 'бухгалтер', salary: '6000000', month_paid: '6000000', month_left: '0'},
    {id: 9, name: 'Г', role: 'повар', salary: '5000000', month_paid: '5500000', month_left: '-500000'},
  ]});
  assert.equal(rows[0].monthShifts, 12);
  assert.equal(rows[0].monthPaid, 1980000);
  assert.deepEqual(rows.slice(1).map(row => [row.paid, row.rest, row.state]),
    [[3000000, 5000000, 'left'], [6000000, 0, 'closed'], [5500000, -500000, 'over']]);
});

test('официанты Retro: по выручке, чеки, смены, средний чек', () => {
  const rows = logic.retroWaiters({waiter_retro: {
    'Алина': {revenue: '4000000', checks: 10, shifts: 3},
    'Музаффар': {revenue: '6000000', checks: 12, shifts: 4},
    'Пустой': {revenue: '0', checks: 0, shifts: 0},
  }});
  assert.deepEqual(rows.map(row => row.name), ['Музаффар', 'Алина']);
  assert.equal(rows[0].averageCheck, 500000);
  assert.equal(rows[1].checks, 10);
  // Старый ответ без разбивки Retro — общий список без чеков.
  assert.equal(logic.retroWaiters({waiter_metrics: {'Олег': metric(1, 100, 50)}})[0].checks, null);
});

test('период из одного дня подписан одной датой', () => {
  assert.equal(logic.periodLabel('2026-09-28', '2026-09-28'), '28 сентября 2026');
  assert.equal(logic.periodLabel('2026-09-22', '2026-09-28'), '22 — 28 сентября 2026');
});
