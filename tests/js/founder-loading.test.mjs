import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { setImmediate } from 'node:timers/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../retro/static/founder.js', import.meta.url), 'utf8');
const code = source.slice(source.indexOf('async function load('), source.indexOf('async function start(){'));
function harness() {
  const pending = [], rendered = [], messages = [], loading = [];
  let generation = 0;
  const context = vm.createContext({
    AbortController, URLSearchParams, controller: null, lastAnalytics: null, lastQuery: '',
    gate: { next: () => ++generation, isCurrent: id => id === generation },
    selectedDirections: () => ['retro'],
    $: id => ({ value: {start:'2026-09-01', end:'2026-09-22', granularity:'day'}[id] }),
    setLoading: value => loading.push(value), clearResults() {}, renderSeriesTable() {},
    setMessage: value => messages.push(value), renderBookingError: value => messages.push(value),
    RetroState: { responseJson: async value => value, analyticsAfterFailure: () => null },
    fetch: (url, options) => new Promise((resolve, reject) => pending.push({url, options, resolve, reject})),
    render: value => rendered.push(['analytics', value]),
    renderBookings: value => rendered.push(['bookings', value]),
  });
  vm.runInContext(code, context);
  return {pending, rendered, messages, loading, run: () => vm.runInContext('load()', context)};
}

test('bookings appear while analytics is still pending', async () => {
  const h = harness(), done = h.run();
  h.pending[1].resolve('bookings-ready');
  await setImmediate();
  assert.deepEqual(h.rendered, [['bookings', 'bookings-ready']]);
  h.pending[0].resolve('analytics-ready');
  await done;
  assert.equal(h.rendered.length, 2);
});

test('analytics becomes usable without waiting for bookings', async () => {
  const h = harness(), done = h.run();
  h.pending[0].resolve('analytics-ready');
  await setImmediate();
  assert.deepEqual(h.rendered, [['analytics', 'analytics-ready']]);
  assert.equal(h.loading.at(-1), false);
  h.pending[1].resolve('bookings-ready');
  await done;
});

test('an older response cannot overwrite a newer filter selection', async () => {
  const h = harness(), old = h.run(), current = h.run();
  assert.equal(h.pending[0].options.signal.aborted, true);
  h.pending[2].resolve('current-analytics');
  h.pending[3].resolve('current-bookings');
  await current;
  h.pending[0].resolve('old-analytics');
  h.pending[1].resolve('old-bookings');
  await old;
  assert.deepEqual(h.rendered, [['analytics', 'current-analytics'], ['bookings', 'current-bookings']]);
});

test('failure of one source preserves the other source', async () => {
  const h = harness(), done = h.run();
  h.pending[0].resolve('analytics-ready');
  h.pending[1].reject(new Error('synthetic booking outage'));
  await done;
  assert.deepEqual(h.rendered, [['analytics', 'analytics-ready']]);
  assert.ok(h.messages.includes('synthetic booking outage'));
});
