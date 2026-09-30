import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

// Без document busy.js отдаёт только чистые помощники — их и проверяем.
await import('../../retro/static/busy.js');
const {tracksRequest, outcome, normalize, isDirty, holdFor, createTracker, skeletonWidths} = globalThis.RetroBusy.pure;

test('полосу ведут только запросы к своему /api/, тихие — нет', () => {
  const origin = 'https://panel.example';
  assert.equal(tracksRequest('/api/accountant/day?date=2026-09-29', undefined, origin), true);
  assert.equal(tracksRequest(origin + '/api/accountant/day', {}, origin), true);
  assert.equal(tracksRequest('/api/shokh/home', {retroBusy: false}, origin), false);
  assert.equal(tracksRequest('/static/style.css', undefined, origin), false);
  assert.equal(tracksRequest('https://other.example/api/x', undefined, origin), false);
  assert.equal(tracksRequest(undefined, undefined, origin), false);
});

test('false от run() модуля — это неудача, остальное — успех', () => {
  assert.equal(outcome(false), 'error');
  assert.equal(outcome(true), 'ok');
  assert.equal(outcome(undefined), 'ok');
  assert.equal(outcome(0), 'ok');
});

test('«не сохранено» не путается с разрядными пробелами', () => {
  assert.equal(normalize('1 500 000'), '1500000');
  assert.equal(normalize('1 500 000'), '1500000');
  assert.equal(isDirty('1500000', '1 500 000'), false);
  assert.equal(isDirty('1500001', '1 500 000'), true);
  assert.equal(isDirty('', null), false);
});

test('показанный спиннер держится не меньше минимума, непоказанный не ждёт', () => {
  assert.equal(holdFor(null, 1000), 0);
  assert.equal(holdFor(1000, 1100), 250);
  assert.equal(holdFor(1000, 2000), 0);
});

function fakeTimers() {
  let now = 0, id = 0;
  const queue = new Map();
  return {
    setTimeout(fn, ms) { id += 1; queue.set(id, {fn, at: now + ms}); return id; },
    clearTimeout(handle) { queue.delete(handle); },
    advance(ms) {
      now += ms;
      for (const [handle, item] of [...queue]) if (item.at <= now) { queue.delete(handle); item.fn(); }
    },
  };
}

test('быстрый запрос не зажигает полосу', () => {
  const timers = fakeTimers(), log = [];
  const t = createTracker({onShow: () => log.push('show'), onHide: () => log.push('hide'), timers});
  t.start(); timers.advance(200); t.end(); timers.advance(500);
  assert.deepEqual(log, []);
});

test('долгий запрос: полоса после порога и гаснет, когда закончились все', () => {
  const timers = fakeTimers(), log = [];
  const t = createTracker({onShow: () => log.push('show'), onHide: () => log.push('hide'), timers});
  t.start(); t.start();
  timers.advance(260);
  assert.deepEqual(log, ['show']);
  t.end();
  assert.deepEqual(log, ['show'], 'второй запрос ещё идёт');
  t.end();
  assert.deepEqual(log, ['show', 'hide']);
  assert.equal(t.count, 0);
  t.end();
  assert.equal(t.count, 0, 'лишний end не уводит счётчик в минус');
});

test('скелет: ширины разные, но стабильные между загрузками', () => {
  const a = skeletonWidths(6), b = skeletonWidths(6);
  assert.deepEqual(a, b);
  assert.ok(new Set(a).size > 3);
  assert.ok(a.every(w => w >= 48 && w < 93));
});

test('busy.js подключён на каждой странице панели и раньше модулей', () => {
  const pages = ['index.html', 'accountant.html', 'payroll.html', 'employees.html', 'shokh.html',
    'director-app.html', 'director.html', 'founder-cabinet.html', 'founder.html', 'login.html'];
  for (const page of pages) {
    const html = readFileSync(new URL('../../retro/static/' + page, import.meta.url), 'utf8');
    const at = html.indexOf('/static/busy.js');
    assert.ok(at > 0, page);
    const firstScript = html.indexOf('<script src="/static/');
    assert.equal(html.indexOf('/static/busy.js'), firstScript + '<script src="'.length, page + ': busy.js — первый скрипт');
  }
});
