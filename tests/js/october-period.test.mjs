import test from 'node:test';
import assert from 'node:assert/strict';
import period from '../../retro/static/period.js';

test('October presets never start in September before October 6', () => {
  for(const preset of ['7','10','30','month']){
    assert.deepEqual(period.presetRange('2026-10-07',preset),{start:'2026-10-06',end:'2026-10-06'});
  }
  assert.ok(period.check('2026-09-30','2026-10-06','2026-10-07'));
  assert.ok(period.check('2026-10-04','2026-10-06','2026-10-07'));
  assert.equal(period.check('2026-10-06','2026-10-06','2026-10-07'),'');
  assert.ok(period.presetRange('2026-10-07','prev-month').end<period.START);
  assert.deepEqual(period.presetRange('2026-11-07','prev-month'),{start:'2026-10-06',end:'2026-10-31'});
});
