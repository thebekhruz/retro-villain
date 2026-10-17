import test from 'node:test';
import assert from 'node:assert/strict';
import logic from '../../retro/static/director-logic.js';

// T-500: плитка «Обед сегодня» и лист обеда на телефоне директора (ТЗ 16.10 §2).
// Ответ — как у /api/director/sales?window=lunch&direction=retro,school (T-499).
const TODAY = '2026-10-17';
const answer = (changes = {}) => ({
  window: {kind: 'lunch', label: '12:00–15:00', basis: 'close', time: 'по времени закрытия чека, Ташкент'},
  lunch_hours: '12:00–15:00', direction: 'retro,school', history_start: '2026-09-01', configured: true,
  period: {start: TODAY, end: TODAY}, missing_days: [], partial: true, partial_days: [TODAY],
  updated_at: '2026-10-17T15:20:00+05:00', refresh_error: null, warnings: [],
  totals: {revenue: '616000', receipts: 4, average_check: '154000', orders: 5},
  by_direction: {retro: {revenue: '316000', receipts: 3, average_check: '105333'},
    school: {revenue: '300000', receipts: 1, average_check: '300000'}},
  by_hour: [{hour: '12:00', revenue: '258000', receipts: 2, average_check: '129000'},
    {hour: '13:00', revenue: '300000', receipts: 1, average_check: '300000'},
    {hour: '14:00', revenue: '58000', receipts: 1, average_check: '58000'}],
  by_day: [{day: TODAY, revenue: '616000', receipts: 4, status: 'ok', partial: true}],
  top_items: ['Лагман', 'Плов', 'Сет Ретро', 'Чай', 'ШКОЛА ОБЕД', 'Шашлык'].map((name, index) => (
    {name, quantity: String(10 - index), revenue: String(100000 * (6 - index)), item_class: 'DISH'})),
  ...changes,
});
const view = (data, options = {}) => logic.lunchView(data, {day: TODAY, today: TODAY, now: '15:30', ...options});
const spaced = text => text.replace(/ | /g, ' ');

test('запрос обеда: окно сервера, Retro и Школа без банкета, топ-5', () => {
  assert.deepEqual(logic.LUNCH_DIRECTIONS, ['retro', 'school']);
  assert.equal(logic.lunchUrl(TODAY),
    '/api/director/sales?start=2026-10-17&end=2026-10-17&window=lunch&direction=retro,school&top=5');
  assert.equal(logic.lunchScope(), 'Retro и Школа · банкетный зал не входит');
  assert.equal(logic.lunchScope(['retro', 'school', 'banquet']), 'Retro, Школа и Банкет');
});

test('плитка: выручка, чеки и средний чек, «на ЧЧ:ММ» и «день идёт»', () => {
  const state = view(answer());
  assert.equal(state.state, 'ok');
  assert.equal(state.revenue, 616000);
  assert.equal(state.partial, true);
  assert.equal(state.updated, '15:20');
  const tile = logic.lunchTile(state);
  assert.equal(spaced(tile.value), '616 000 сум');
  assert.equal(spaced(tile.split), 'Retro 316 000 · Школа 300 000');
  assert.equal(spaced(tile.sub), '4 чека · средний чек 154 000');
  assert.equal(tile.note, 'на 15:20');
  assert.equal(tile.badge, 'день идёт');
  assert.equal(logic.lunchTile(view(answer({totals: {revenue: '12450000', receipts: 101, average_check: '123267'}}))).value,
    '12,5 млн сум');
});

test('настоящий ноль — «0 сум» и пустые часы, а не «нет данных»', () => {
  const zero = view(answer({totals: {revenue: '0', receipts: 0, average_check: null}, by_direction: {}, by_hour: [],
    top_items: [], partial: false, partial_days: []}), {day: '2026-10-16'});
  assert.equal(zero.state, 'ok');
  const tile = logic.lunchTile(zero);
  assert.equal(tile.value, '0 сум');
  assert.equal(tile.sub, '0 чеков');
  assert.equal(tile.empty, false);
  // Прошедший день прочитан на следующий день — время с датой.
  assert.equal(zero.updatedLabel, 'на 17.10 15:20');
  assert.deepEqual(zero.hours.map(row => [row.label, row.revenue, row.phase]),
    [['12:00–13:00', 0, 'done'], ['13:00–14:00', 0, 'done'], ['14:00–15:00', 0, 'done']]);
  assert.deepEqual(zero.directions.map(row => [row.name, row.revenue, row.share]), [['Retro', 0, 0], ['Школа', 0, 0]]);
  assert.deepEqual(zero.top, []);
});

test('день без загруженных данных — «за этот день данных нет» с причиной', () => {
  const empty = {totals: null, by_direction: {}, by_hour: [], top_items: [], updated_at: null, partial: false,
    partial_days: []};
  const missing = view(answer({...empty, missing_days: ['2026-10-10']}), {day: '2026-10-10'});
  assert.equal(missing.state, 'missing');
  assert.equal(missing.reason, 'День ещё не загружен из iiko: он стоит в очереди загрузки.');
  assert.equal(logic.lunchTile(missing).value, 'Нет данных');
  const early = view(answer({...empty, missing_days: ['2026-08-20']}), {day: '2026-08-20'});
  assert.equal(early.reason, 'История продаж хранится с 01.09.2026.');
  const offline = view(answer({...empty, configured: false}), {day: '2026-10-10'});
  assert.equal(offline.state, 'missing');
  assert.equal(offline.reason, 'iiko не подключено: продажи загрузить неоткуда.');
});

test('iiko или синхронизация недоступны — «нет данных» с причиной, не ноль', () => {
  const empty = {totals: null, by_direction: {}, by_hour: [], top_items: [], updated_at: null, missing_days: [TODAY]};
  const failed = view(answer({...empty, refresh_error: 'Не удалось подключиться к iiko. Показаны данные последней загрузки.'}));
  assert.equal(failed.state, 'unavailable');
  assert.equal(failed.reason, 'Не удалось подключиться к iiko.');
  const tile = logic.lunchTile(failed);
  assert.equal(tile.value, 'Нет данных');
  assert.equal(tile.sub, 'Не удалось подключиться к iiko.');
  assert.equal(tile.empty, true);
  assert.equal(view(answer({...empty, refresh_error: 'iiko отвечает медленно: показаны данные последней загрузки, обновление идёт.'})).reason,
    'iiko отвечает медленно: день ещё загружается.');
  assert.equal(view(answer({...empty, refresh_error: 'iiko сейчас не отвечает: показаны данные последней загрузки.'})).reason,
    'iiko сейчас не отвечает.');
  assert.equal(view(answer({...empty, configured: false})).reason, 'iiko не подключено: продажи загрузить неоткуда.');
  // Запрос не дошёл: с кодом ответа — текст сервера, без него — связь.
  assert.deepEqual([view(null, {error: Object.assign(new Error('Будущие даты недоступны.'), {status: 422})}).reason,
    view(null, {error: new TypeError('Failed to fetch')}).reason],
  ['Будущие даты недоступны.', 'Нет связи с панелью. Проверьте интернет и откройте снова.']);
  assert.equal(view(null).state, 'loading');
  assert.equal(logic.lunchTile(view(null)).value, null);
});

test('старые данные при неудачном обновлении остаются, с пометкой', () => {
  const state = view(answer({updated_at: '2026-10-17T12:10:00+05:00',
    refresh_error: 'iiko сейчас не отвечает: показаны данные последней загрузки.',
    by_hour: [{hour: '12:00', revenue: '258000', receipts: 2, average_check: '129000'}],
    totals: {revenue: '258000', receipts: 2, average_check: '129000'}}), {now: '13:40'});
  assert.equal(state.state, 'ok');
  assert.equal(logic.lunchTile(state).note, 'на 12:10 · обновить не удалось');
  assert.ok(state.notes.includes('iiko сейчас не отвечает: показаны данные последней загрузки.'));
  // После 12:10 iiko не читали: 13:00 уже наступил, но не загружен; 14:00 ещё впереди.
  assert.deepEqual(state.hours.map(row => [row.phase, row.upcoming]),
    [['current', false], ['ahead', false], ['ahead', true]]);
});

test('часы окна: прошедшие, идущий и ещё не наступившие', () => {
  const state = view(answer({updated_at: '2026-10-17T13:04:00+05:00',
    by_hour: [{hour: '12:00', revenue: '258000', receipts: 2, average_check: '129000'},
      {hour: '13:00', revenue: '300000', receipts: 1, average_check: '300000'}]}), {now: '13:05'});
  assert.deepEqual(state.hours.map(row => [row.label, row.phase, row.upcoming, Math.round(row.share * 100)]), [
    ['12:00–13:00', 'done', false, 86], ['13:00–14:00', 'current', false, 100], ['14:00–15:00', 'ahead', true, 0]]);
  assert.ok(state.notes[0].startsWith('День идёт: учтены чеки, закрытые к 13:04.'));
  // Прошедший день — все часы прошли, «впереди» не бывает.
  const past = view(answer({partial: false, partial_days: []}), {day: '2026-10-16'});
  assert.ok(past.hours.every(row => row.phase === 'done'));
  assert.equal(past.partial, false);
  assert.deepEqual(past.notes, []);
});

test('окно не по целым часам подписывается своими границами', () => {
  assert.deepEqual(logic.windowBounds('11:30–15:00'), {start: 690, end: 900});
  assert.equal(logic.windowBounds('обед'), null);
  const rows = logic.lunchHours({by_hour: [{hour: '11:00', revenue: '1000', receipts: 1, average_check: '1000'}]},
    logic.windowBounds('11:30–13:15'), {isToday: false});
  assert.deepEqual(rows.map(row => row.label), ['11:30–12:00', '12:00–13:00', '13:00–13:15']);
});

test('направления по порядку плитки, топ — пять блюд окна', () => {
  const state = view(answer());
  assert.deepEqual(state.directions.map(row => [row.key, row.name, row.color, row.receipts]),
    [['retro', 'Retro', '#143e35', 3], ['school', 'Школа', '#52786f', 1]]);
  assert.equal(state.directions[0].share, 1);
  assert.equal(state.top.length, 5);
  assert.deepEqual(state.top[0], {name: 'Лагман', quantity: 10, revenue: 600000});
});

test('прошедший день, не перечитанный после смены, и расхождение сверки — заметки', () => {
  const state = view(answer({partial_days: ['2026-10-16'], partial: true,
    by_day: [{day: '2026-10-16', status: 'mismatch'}]}), {day: '2026-10-16'});
  assert.equal(state.partial, false);
  assert.equal(state.stale, true);
  assert.deepEqual(state.notes, ['День ещё не перечитан после закрытия смены: цифры могут уточниться.',
    'Сверка с итогом iiko за этот день не сошлась.']);
  assert.equal(logic.lunchTile(state).badge, '');
});

test('время ресторана и время данных — по Ташкенту', () => {
  assert.equal(logic.tashkentClock(new Date('2026-10-17T08:05:00Z')), '13:05');
  assert.equal(logic.tashkentClock(new Date('2026-10-16T19:30:00Z')), '00:30');
  assert.equal(logic.clockOf('2026-10-17T13:04:00+05:00'), '13:04');
  assert.equal(logic.clockOf(null), '');
});

test('вопрос AI называет день обеда', () => {
  assert.match(logic.lunchQuestion(view(answer())), /^Как прошёл обед сегодня: выручка Retro и Школы без банкетного зала/);
  assert.match(logic.lunchQuestion(view(answer(), {day: '2026-10-16'})), /^Как прошёл обед 16 октября 2026:/);
});

test('строки обеда переводятся на узбекский', async () => {
  globalThis.document = {readyState: 'loading', addEventListener() {}};
  await import('../../retro/static/i18n-uz.js');
  await import('../../retro/static/i18n.js');
  const {translate} = globalThis.RetroI18n;
  const state = view(answer());
  const tile = logic.lunchTile(state);
  for (const text of ['Обед сегодня', 'Обед', 'день идёт', 'По часам', 'По направлениям', 'Топ-5 блюд окна',
    'по количеству · под названием выручка', '✦ Спросить AI про этот обед →', 'Сколько продали за обед сегодня?',
    'За этот день данных нет', 'идёт', 'ещё не наступил', 'ещё не загружен', 'В окне обеда блюд не продавали.',
    logic.lunchScope(), tile.sub, tile.note, 'на 12:10 · обновить не удалось', 'окно 12:00–15:00',
    '12:00–15:00 · по времени закрытия чека, Ташкент', 'Сегодня · 17 октября 2026', 'выручка 150 000 сум',
    'День ещё не загружен из iiko: он стоит в очереди загрузки.', 'История продаж хранится с 01.09.2026.',
    'iiko не подключено: продажи загрузить неоткуда.', 'iiko отвечает медленно: день ещё загружается.',
    'iiko сейчас не отвечает.', 'Сегодняшние чеки ещё не загружены из iiko.',
    'Нет связи с панелью. Проверьте интернет и откройте снова.', state.notes[0],
    'День ещё не перечитан после закрытия смены: цифры могут уточниться.',
    'Сверка с итогом iiko за этот день не сошлась.', 'iiko сейчас не отвечает: показаны данные последней загрузки.',
    'iiko отвечает медленно: показаны данные последней загрузки, обновление идёт.']) {
    const uz = translate(text);
    assert.ok(uz && !/[А-Яа-яЁё]/.test(uz), text + ' → ' + uz);
  }
  assert.equal(translate('Обед сегодня'), 'Bugungi tushlik');
  assert.equal(spaced(translate(tile.sub)), "4 ta chek · o'rtacha chek 154 000");
  assert.equal(translate('на 15:20'), '15:20 holatiga');
  assert.equal(translate('на 17.10 07:00'), '17.10 07:00 holatiga');
  assert.equal(spaced(translate(tile.split)), 'Retro 316 000 · Maktab 300 000');
});
