import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {setImmediate as flush} from 'node:timers/promises';

const source = readFileSync(new URL('../../retro/static/manager.js', import.meta.url), 'utf8');
// Run the actual controller with a delayed image decoder and delayed network.
const code = source.slice(source.indexOf('async function sendToHikvision()'), source.indexOf('/* ── События'));
function harness() {
  const requests = [], messages = [];
  const first = {id: 12, can_photo: true}, second = {id: 34, can_photo: true};
  const state = {card: first, home: {employees: [first, second]}};
  let decode;
  const context = vm.createContext({state, JSON,
    $: id => ({id}), renderCard() {}, renderList() {},
    photoBusy: () => state.preparing || state.uploading || state.sending,
    message: text => messages.push(text),
    shrinkPhoto: () => new Promise(resolve => { decode = resolve; }),
    Busy: {button: (element, promise) => promise},
    api: (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
  });
  vm.runInContext(source.slice(source.indexOf('function replaceCard('), source.indexOf('/* Фото с камеры')), context);
  vm.runInContext(code, context);
  return {state, second, context, requests, messages,
    start: () => context.photoTaken({currentTarget: {id: 'gallery-input', files: [{}], value: 'selected'}}),
    decode: async () => { decode('data:image/jpeg;base64,test'); await flush(); },
  };
}

test('employee is captured before decoding; one request saves and sends to that employee', async () => {
  const h = harness(), work = h.start();
  assert.equal(h.state.preparing, true);
  // Even a forced card change outside normal disabled navigation cannot retarget the photo.
  h.state.card = h.second;
  await h.decode();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].url, '/employees/12/photo?send=true');
  assert.equal(h.requests[0].options.method, 'PUT');
  assert.equal(JSON.parse(h.requests[0].options.body).image, 'data:image/jpeg;base64,test');
  h.requests[0].resolve({data: {employee: {id: 12, photo: {url: '/saved'}, can_retry: true}}});
  await work;
  assert.equal(h.requests.length, 1, 'no follow-up write to the newly opened employee');
  assert.equal(h.state.card.id, 34);
  assert.equal(h.state.home.employees[0].photo.url, '/saved');
  assert.equal(h.state.uploading, false);
  assert.equal(h.state.preparing, false);
});

test('another photo or retry during preparation/upload cannot start a second write', async () => {
  const h = harness(), work = h.start();
  await h.start();
  assert.equal(await h.context.sendToHikvision(), false);
  await h.decode();
  await h.start();
  assert.equal(await h.context.sendToHikvision(), false);
  assert.equal(h.requests.length, 1);
  h.requests[0].resolve({data: {employee: {id: 12}}});
  await work;
});

test('lost response refreshes only the original employee and preserves the uncertain outcome', async () => {
  const h = harness(), work = h.start();
  await h.decode();
  h.requests[0].reject(Object.assign(new Error('offline'), {network: true}));
  await flush();
  h.state.card = h.second;
  assert.equal(h.requests[1].url, '/employees/12');
  assert.equal(h.requests[1].options, undefined, 'refresh is read-only');
  h.requests[1].resolve({data: {employee: {id: 12, photo: {url: '/saved'}, can_retry: true}}});
  await work;
  assert.match(h.messages[0], /результат отправки неизвестен/);
  assert.equal(h.state.home.employees[0].can_retry, true);
  assert.equal(h.state.card.id, 34);
});
