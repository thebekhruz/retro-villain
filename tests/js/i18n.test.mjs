import test from 'node:test';
import assert from 'node:assert/strict';

// Переводчик ждёт документ только для запуска; для чистой функции хватает заглушки.
globalThis.document = {readyState: 'loading', addEventListener() {}};
await import('../../retro/static/i18n-uz.js');
await import('../../retro/static/i18n.js');
const {translate} = globalThis.RetroI18n;

test('экран из редизайна переводится целиком, без смеси языков', () => {
  assert.equal(translate('Финансы дня'), 'Kunlik moliya');
  assert.equal(translate('Записать три покупки'), 'Uchta xaridni yozish');
  assert.equal(translate('День выплат'), "To'lov kuni");
});

test('дни недели и месяцы без числа', () => {
  assert.equal(translate('Сб'), 'Sh');
  assert.equal(translate('Суббота, 26 сентября'), 'Shanba, 26 sentabr');
  assert.equal(translate('КУДА УШЛИ ДЕНЬГИ · СЕНТЯБРЬ'), 'PUL QAYERGA KETDI · SENTABR');
  // «Все» начинается с «Вс», но это не воскресенье.
  assert.equal(translate('Всего'), 'Jami');
});

test('единицы после чисел', () => {
  assert.equal(translate('5,9 млн'), '5,9 mln');
  assert.equal(translate('181 шт'), '181 dona');
  assert.equal(translate('31 из ≈147 чеков'), '31 / ≈147 chek');
});

test('шаблоны со своим порядком слов и неразрывными пробелами в суммах', () => {
  assert.equal(translate('39% выручки Retro'), 'Retro tushumining 39%');
  assert.equal(translate('Передано 0 при расчёте 14 723 000'), "Topshirildi 0, hisob bo'yicha 14 723 000");
  assert.equal(translate('3 замечания'), '3 ta izoh');
});

test('советы директора переводятся, название блюда остаётся как в iiko', () => {
  assert.equal(
    translate('«Сет Ретро на 5-6 чел.» хорошо продаётся, но маржа 35% — не ставьте его в акции.'),
    "«Сет Ретро на 5-6 чел.» yaxshi sotilmoqda, lekin marjasi 35% — uni aksiyaga qo'ymang.");
});
