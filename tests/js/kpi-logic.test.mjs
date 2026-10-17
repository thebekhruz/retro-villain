import test from 'node:test';
import assert from 'node:assert/strict';

import logic from '../../retro/static/kpi-logic.js';

const period = {start: '2026-10-19', end: '2026-10-25', days_total: 7, days_passed: 4, today: '2026-10-22'};

test('суммы: тысячи, миллионы, знак', () => {
  assert.equal(logic.thousands(2884000), '2 884 тыс');
  assert.equal(logic.thousands(12_400_000), '12,4 млн');
  assert.equal(logic.thousands(10_000_000), '10 млн');
  assert.equal(logic.thousands(0), '0');
  assert.equal(logic.signed(997000), '+997 тыс');
  assert.equal(logic.signed(-72000), '−72 тыс');
  assert.equal(logic.rate(4.16), '4,2');
  assert.equal(logic.percent(0.25), '25 %');
});

test('подписи периода и дня', () => {
  assert.equal(logic.periodLabel('2026-10-19', '2026-10-25'), '19–25 октября');
  assert.equal(logic.periodLabel('2026-10-26', '2026-11-01'), '26 октября – 1 ноября');
  assert.equal(logic.dayLabel(period), 'Четверг, 4-й день из 7');
  assert.equal(logic.dayLabel({...period, today: '2026-10-16', days_passed: 0}), 'Старт: понедельник, 19 октября');
  assert.equal(logic.dayLabel({...period, today: '2026-10-27', days_passed: 7}), 'Период завершён');
  assert.equal(logic.fetchedLabel('2026-10-22T14:32:10+05:00', '2026-10-22'), 'данные iiko на 14:32');
  assert.equal(logic.fetchedLabel('2026-10-21T23:50:00+05:00', '2026-10-22'), 'данные iiko на 21.10, 23:50');
  assert.equal(logic.fetchedLabel(null), 'данных iiko ещё нет');
});

test('прогресс зала и человека, строка под именем', () => {
  assert.equal(logic.goalProgress({goal: 3_200_000, team_uplift: 1_472_000}), 46);
  assert.equal(logic.goalProgress({goal: 0, team_uplift: 10}), 0);
  const rows = [
    {waiter: 'Валентина', eligible: true, premium: 255000, sold: 31, checks: 63, shifts: 6},
    {waiter: 'Баходир', eligible: true, premium: 113000, sold: 12, checks: 26, shifts: 6},
    {waiter: 'Нурик', eligible: false, premium: 0, sold: 3, checks: 15, shifts: 4},
    {waiter: 'Султан', eligible: false, premium: 0, sold: 0, checks: 3, shifts: 2},
  ];
  const scheme = {min_checks: 20, min_shifts: 3};
  assert.equal(logic.personProgress(rows[0], rows, scheme), 100);
  assert.equal(logic.personProgress(rows[1], rows, scheme), 44);
  assert.equal(logic.personProgress(rows[2], rows, scheme), 75, 'до порога: 15 из 20 чеков');
  assert.equal(logic.personProgress(rows[3], rows, scheme), 15);
  assert.equal(logic.personLine(rows[0], scheme), '63 чека · премия 255 тыс');
  assert.equal(logic.personLine(rows[2], scheme), '15 чеков · 4 смены · ещё 5 чеков');
  assert.equal(logic.personLine(rows[3], scheme), '3 чека · 2 смены · ещё 17 чеков и 1 смена');
  assert.deepEqual(logic.ranking(rows.slice().reverse()).map(r => r.waiter), ['Валентина', 'Баходир', 'Нурик', 'Султан']);
  assert.deepEqual(logic.ranking([{waiter: 'А', eligible: true, premium: 10, sold: 1}, {waiter: 'Б', eligible: true, premium: 10, sold: 4}]).map(r => r.waiter), ['Б', 'А'], 'при равной премии выше тот, кто продал больше');
  assert.equal(logic.initials('Малика млад'), 'ММ');
});

test('позиции: тон, прогресс к цели, спарклайн по дням', () => {
  const item = {base: 2.5, now: 4.4, target: 4, by_day: {'2026-10-19': 2, '2026-10-20': 1, '2026-10-22': 3}};
  const row = {checks_by_day: {'2026-10-19': 10, '2026-10-20': 12, '2026-10-21': 9, '2026-10-22': 10}};
  assert.equal(logic.itemTone(item), 'is-up');
  assert.equal(logic.itemTone({base: 3, now: 1}), 'is-down');
  assert.equal(logic.itemProgress(item), 100);
  assert.equal(logic.itemProgress({now: 1.5, target: 3}), 50);
  const path = logic.sparkPath(item, row, period);
  assert.match(path, /^M0\.0,/);
  assert.equal(path.split(' ').length, 4, 'четыре дня с чеками, три остальных пропущены');
  assert.equal(logic.sparkPath(item, {checks_by_day: {'2026-10-19': 10}}, period), '', 'одна точка — линии нет');
});

test('настройки из формы: проценты в доли, тысячи в сумы', () => {
  const body = logic.settingsFromForm({kind: 'week', start: '2026-10-19', end: '2026-10-25', share: '30',
    cap: '1 000', min_checks: '20', min_shifts: '3', netting: false,
    targets: [{title: 'Чай', group: 'напиток', target100: '5,0', margin: '36 000', dishes: [{id: 'a', name: 'ЧАЙ'}]}]});
  assert.equal(body.scheme.share, 0.3);
  assert.equal(body.scheme.personal, undefined, 'личной части больше нет');
  assert.equal(body.scheme.cap, 1_000_000);
  assert.equal(body.scheme.netting, false);
  assert.deepEqual(body.targets[0], {key: 't1', title: 'Чай', group: 'напиток', target100: null, manual: false, margin: 36000, dishes: [['a', 'ЧАЙ']]},
    'без ручной отметки цель считает правило');
  const manual = logic.settingsFromForm({share: '25', cap: '1000', min_checks: '20', min_shifts: '3',
    targets: [{title: 'Чай', target100: '7,5', manual: true, dishes: [{id: 'a', name: 'ЧАЙ'}]}]});
  assert.equal(manual.targets[0].target100, 7.5);
  assert.equal(manual.targets[0].manual, true);
  assert.equal(logic.targetSource({target_source: 'base'}), 'цель: база + 70 %');
  assert.equal(logic.targetSource({target_source: 'manual'}), 'цель вручную');
  assert.throws(() => logic.settingsFromForm({share: 'x', cap: '1'}), /доля прироста/);
});

test('кольцо: процент, дуга, остаток', () => {
  assert.equal(logic.ringPercent(31, 63), 49);
  assert.equal(logic.ringPercent(70, 63), 100, 'выше цели — кольцо полное');
  assert.equal(logic.ringPercent(5, 0), 0);
  const dash = logic.ringDash(50, 80);
  assert.equal(dash.circumference, 502.65);
  assert.equal(dash.offset, 251.33);
  assert.equal(logic.ringDash(0, 80).offset, 502.65);
  assert.equal(logic.remaining(31, 63.4), 33);
  assert.equal(logic.remaining(70, 63), 0);
});

test('темп: до старта, в темпе, отстаём, цель взята', () => {
  const week = {days_total: 7, days_passed: 4};
  assert.equal(logic.paceState({goal_qty: 63, sold: 0}, {...week, days_passed: 0}).state, 'start');
  assert.deepEqual(logic.paceState({goal_qty: 63, sold: 36}, week), {state: 'on_pace', text: 'Идём в темпе', lag: 0});
  assert.deepEqual(logic.paceState({goal_qty: 63, sold: 31}, week), {state: 'behind', text: 'Отстаём на 5 порций к сегодняшнему дню', lag: 5});
  assert.equal(logic.paceState({goal_qty: 63, sold: 35}, week).text, 'Отстаём на 1 порцию к сегодняшнему дню');
  assert.deepEqual(logic.paceState({goal_qty: 63, sold: 63}, week), {state: 'done', text: 'Цель недели взята', lag: 0});
  assert.equal(logic.paceState({goal_qty: 200, sold: 200}, {days_total: 31, days_passed: 10}).text, 'Цель месяца взята');
});

test('лучший день недели', () => {
  assert.equal(logic.bestDay({'2026-10-19': 12, '2026-10-22': 31, '2026-10-21': 31}).text, 'среда, 31 порция');
  assert.equal(logic.bestDay({}), null);
});

test('пустое состояние вкладки KPI: до старта, без участников, с данными', () => {
  const period = {start: '2026-10-19', end: '2026-10-25', days_passed: 0, days_total: 7, pilot: true};
  const none = 'Официантов зала нет ни в реестре, ни в отметках директора. Отметить их можно в настройке периода.';
  assert.deepEqual(logic.emptyState({period, logins: []}), {state: 'before', title: 'Пилот стартует 19 октября', text: none});
  assert.equal(logic.emptyState({period, logins: [{waiter: 'Алина', active: true, source: 'registry'}, {waiter: 'Любовь', active: false}]}).text,
    'Участвуют официанты: 1. Продажи засчитываются с понедельника, 19 октября; цели ниже посчитаны по базе зала за прошлые недели.');
  assert.equal(logic.emptyState({period: {...period, pilot: false, start: '2026-10-26'}, logins: []}).title, 'Период стартует 26 октября');
  const running = {...period, days_passed: 3};
  assert.deepEqual(logic.emptyState({period: running, logins: [{waiter: 'Алина', active: false}]}), {state: 'unmarked', title: 'Официанты не отмечены', text: none});
  assert.equal(logic.emptyState({period: running, logins: [{waiter: 'Алина', active: true}]}), null);
  assert.equal(logic.emptyState({}), null);
});

test('процент цели в кольце без потолка и подпись участия', () => {
  assert.equal(logic.goalPercent(0, 140), 0);
  assert.equal(logic.goalPercent(64, 140), 46);
  assert.equal(logic.goalPercent(157, 140), 112, 'перевыполнение показываем как есть');
  assert.equal(logic.goalPercent(5, 0), 0);
  assert.equal(logic.ringPercent(157, 140), 100, 'дуга кольца при этом полная');
  assert.equal(logic.sourceLabel({source: 'registry'}), 'из реестра');
  assert.equal(logic.sourceLabel({source: 'manual'}), 'отмечен вручную');
  assert.equal(logic.sourceLabel({source: 'manual_off'}), 'не считать, вручную');
  assert.equal(logic.sourceLabel({source: 'unmatched'}), 'не сопоставлен');
  assert.equal(logic.dayMonth('2026-11-01'), '1 ноября');
});

test('подпись порога шума под кольцом', () => {
  assert.deepEqual(logic.thresholdLine({threshold: 1500000, to_threshold: 1212000, above_threshold: false, fund: 0}),
    {above: false, text: 'До премии осталось 1 212 тыс маржи'});
  assert.deepEqual(logic.thresholdLine({threshold: 1500000, to_threshold: 0, above_threshold: true, fund: 420000}),
    {above: true, text: 'Премия начисляется: фонд 420 тыс'});
  assert.equal(logic.thresholdLine({threshold: 0}), null, 'без порога подписи нет');
  const body = logic.settingsFromForm({share: '40', cap: '1 000', noise_week: '1 500', min_checks: '20', min_shifts: '3', targets: []});
  assert.equal(body.scheme.noise_week, 1500000);
  assert.equal(body.scheme.share, 0.4);
});
