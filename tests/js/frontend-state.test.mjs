import test from 'node:test';
import assert from 'node:assert/strict';
import '../../retro/static/frontend-state.js';

await import('../../retro/static/logout.js').catch(error => {
  if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error;
});

test('successful JSON response returns its amount', async () => {
  const response = new Response(JSON.stringify({amount:'125'}), {status:200});
  assert.equal((await globalThis.RetroState.responseJson(response)).amount, '125');
});

test('failed JSON response exposes the server detail', async () => {
  const response = new Response(JSON.stringify({detail:'Ошибка данных'}), {status:422});
  await assert.rejects(globalThis.RetroState.responseJson(response), /Ошибка данных/);
});

test('failed founder request clears previous analytics', () => {
  assert.equal(globalThis.RetroState.analyticsAfterFailure({totals:{retro:'1'}}), null);
});

test('busy state clears only for current request', () => {
  assert.equal(globalThis.RetroState.shouldReleaseBusy(4, 4), true);
  assert.equal(globalThis.RetroState.shouldReleaseBusy(3, 4), false);
});

test('logout posts to the session endpoint before opening login', async () => {
  assert.equal(typeof globalThis.RetroLogout?.logout, 'function');
  const calls = [];
  let destination = '';

  await globalThis.RetroLogout.logout(async (url, options) => {
    calls.push([url, options]);
    return {ok: true};
  }, url => { destination = url; });

  assert.deepEqual(calls, [['/api/session/logout', {method: 'POST'}]]);
  assert.equal(destination, '/login');
});
