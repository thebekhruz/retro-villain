import test from 'node:test';
import assert from 'node:assert/strict';
// UMD-модуль: из node приходит CJS-экспортом, в браузере ложится в globalThis.
import logic from '../../retro/static/shokh-logic.js';

const draft = (extra = {}) => ({point: 'Базар', item: 'Помидоры', unit: 'кг',
  quantity: '12', price: '9000', hasPhoto: false, ...extra});

test('итог покупки — количество на цену, с копейками', () => {
  assert.equal(logic.total(draft()), 108000);
  assert.equal(logic.total(draft({quantity: '1,5', price: '3333'})), 4999.5);
  // Пока чего-то не хватает, итога нет — ноль показывать нельзя.
  assert.equal(logic.total(draft({price: ''})), null);
  assert.equal(logic.total(draft({quantity: '0'})), null);
});

test('шаг не пускает дальше, пока не заполнено нужное', () => {
  assert.equal(logic.stepReady('point', draft({point: '  '})), false);
  assert.equal(logic.stepReady('point', draft()), true);
  assert.equal(logic.stepReady('item', draft({item: ''})), false);
  assert.equal(logic.stepReady('amount', draft({price: ''})), false);
  assert.equal(logic.stepReady('amount', draft()), true);
  assert.equal(logic.stepReady('confirm', draft()), true);
});

test('шаги идут по кругу без выхода за края', () => {
  assert.equal(logic.nextStep('point'), 'item');
  assert.equal(logic.nextStep('confirm'), 'confirm');
  assert.equal(logic.previousStep('point'), 'point');
  assert.equal(logic.previousStep('amount'), 'item');
});

/* Дороже обычного — повод бухгалтеру проверить, а не запрет; первую покупку
   товара сравнивать не с чем. */
test('подсказка по цене сравнивает с обычной, когда она известна', () => {
  assert.equal(logic.priceHint(draft({price: '10000'}), '8000').kind, 'above');
  assert.equal(logic.priceHint(draft({price: '10000'}), '8000').delta, 25);
  assert.equal(logic.priceHint(draft({price: '7200'}), '8000').kind, 'below');
  assert.equal(logic.priceHint(draft({price: '8000'}), '8000').kind, 'same');
  assert.equal(logic.priceHint(draft(), null).kind, 'unknown');
  assert.equal(logic.priceHint(draft({price: ''}), '8000').kind, 'empty');
});

/* Награды обещаются до отправки, поэтому должны совпадать с серверными:
   30 за покупку, 10 за фото, 10 за цену не выше обычной. */
test('предпросмотр опыта повторяет серверные награды', () => {
  assert.equal(logic.xpPreview(draft(), null).total, 30);
  assert.equal(logic.xpPreview(draft({hasPhoto: true}), null).total, 40);
  assert.equal(logic.xpPreview(draft({price: '7000'}), '8000').total, 40);
  assert.equal(logic.xpPreview(draft({price: '9000'}), '8000').total, 30);
  assert.equal(logic.xpPreview(draft({hasPhoto: true, price: '7000'}), '8000').total, 50);
  assert.deepEqual(logic.xpPreview(draft({hasPhoto: true}), null).parts.map(p => p.label),
    ['Покупка', 'Фото']);
});

test('остаток после покупки может уйти в минус и это видно', () => {
  assert.equal(logic.pocketAfter('900000', draft()), 792000);
  // Записали больше, чем выдали — прятать нельзя.
  assert.equal(logic.pocketAfter('50000', draft()), -58000);
  // Подотчёта нет — остатка тоже нет.
  assert.equal(logic.pocketAfter(null, draft()), null);
});

test('таймер закупа считает минуты и держит цель в пятнадцать минут', () => {
  const started = '2026-09-16T09:00:00+05:00';
  assert.equal(logic.tripElapsedMinutes(started, '2026-09-16T09:12:00+05:00'), 12);
  assert.equal(logic.tripOnTime(started, '2026-09-16T09:12:00+05:00'), true);
  assert.equal(logic.tripOnTime(started, '2026-09-16T09:40:00+05:00'), false);
  assert.equal(logic.tripElapsedMinutes(null, '2026-09-16T09:12:00+05:00'), null);
  assert.equal(logic.clock(12.5), '12:30');
  assert.equal(logic.clock(null), '—');
});

test('доля отчитанных денег считается от всего выданного', () => {
  // На руках 792 000, ждёт проверки 108 000 → выдано 900 000, отчитались за 12%.
  assert.equal(logic.reportedShare('792000', '108000'), 12);
  assert.equal(logic.reportedShare('900000', '0'), 0);
  assert.equal(logic.reportedShare(null, '0'), null);
});
