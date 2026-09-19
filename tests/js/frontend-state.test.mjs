import test from 'node:test';
import assert from 'node:assert/strict';
import '../../retro/static/frontend-state.js';

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
