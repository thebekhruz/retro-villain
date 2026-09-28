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
