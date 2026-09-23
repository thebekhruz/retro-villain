// Run the actual founder load() function with synthetic independent API delays.
// No browser, production data, or external requests are used.
import fs from 'node:fs';
import vm from 'node:vm';
import { performance } from 'node:perf_hooks';

const source = fs.readFileSync(new URL('../retro/static/founder.js', import.meta.url), 'utf8');
const load = source.slice(source.indexOf('async function load('), source.indexOf('async function start(){'));
const events = [];
const start = performance.now();
const mark = name => events.push({ name, ms: Math.round(performance.now() - start) });
const context = {
  AbortController, URLSearchParams,
  controller: null, lastAnalytics: null, lastQuery: "",
  gate: { next: () => 1, isCurrent: () => true },
  selectedDirections: () => ['retro'],
  $: id => ({ value: ({ start: '2026-09-01', end: '2026-09-22', granularity: 'day' })[id] }),
  setLoading: () => {}, clearResults: () => {}, setMessage: () => {},
  RetroState: { responseJson: async value => value },
  fetch: async url => {
    const kind = url.includes('/bookings') ? 'bookings' : 'analytics';
    await new Promise(resolve => setTimeout(resolve, kind === 'bookings' ? 20 : 200));
    mark(`${kind}_response`);
    return {};
  },
  render: () => mark('analytics_render'),
  renderBookings: () => mark('bookings_render'),
};
await vm.runInNewContext(`${load}\nload()`, context);
console.log(JSON.stringify(events, null, 2));
