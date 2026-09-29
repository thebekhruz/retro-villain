import test from 'node:test';
import assert from 'node:assert/strict';
// UMD-модуль: из node приходит CJS-экспортом, в браузере ложится в globalThis.
import logic from '../../retro/static/shokh-logic.js';

const draft = (extra = {}) => ({point: 'Базар', item: 'Помидоры', unit: 'кг',
  quantity: '12', price: '9000', hasPhoto: false, ...extra});

test('итог покупки — количество на цену, с копейками', () => {
  assert.equal(logic.total(draft()), 108000);
  assert.equal(logic.total(draft({quantity: '1,5', price: '3333'})), 4999.5);
  assert.equal(logic.total(draft({quantity: '1.005', price: '1'})), 1.01);
  assert.equal(logic.total(draft({quantity: '1.125', price: '1'})), 1.13);
  assert.equal(logic.total(draft({quantity: '1.125', price: '11200'})), 12600);
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

test('до +10% к обычной — «в норме», выше — «дороже на N% — бухгалтер увидит»', () => {
  assert.equal(logic.priceHint(draft({price: '8800'}), '8000').kind, 'same');
  assert.equal(logic.priceHint(draft({price: '8800'}), '8000').text, 'В норме');
  const above = logic.priceHint(draft({price: '8801'}), '8000');
  assert.equal(above.kind, 'above');
  assert.equal(above.text, 'Дороже обычного на 10% — бухгалтер увидит');
  assert.equal(logic.priceHint(draft({price: '6000'}), '8000').text, 'Дешевле обычного на 25%');
  // Проценты целые: «13.5%» не узнал бы узбекский перевод.
  assert.equal(logic.priceHint(draft({price: '10440'}), '9200').delta, 13);
});

test('поиск товара: слова в любом порядке, частые — первыми', () => {
  const items = [{item: 'Овощ Помидор черри', code: '00011', times: 0},
    {item: 'Овощ Помидор', code: '00010', times: 4}, {item: 'Агар', code: '01503', times: 0},
    {item: 'Зелень Лук зелёный', code: '00020', times: 9}];
  assert.deepEqual(logic.searchItems(items, '').map(r => r.item),
    ['Зелень Лук зелёный', 'Овощ Помидор', 'Агар', 'Овощ Помидор черри']);
  assert.deepEqual(logic.searchItems(items, 'черри помидор').map(r => r.item), ['Овощ Помидор черри']);
  assert.deepEqual(logic.searchItems(items, 'зеленый').map(r => r.item), ['Зелень Лук зелёный']);
  assert.deepEqual(logic.searchItems(items, '00010').map(r => r.item), ['Овощ Помидор']);
  assert.equal(logic.searchItems(items, '', 2).length, 2);
});

test('остаток после покупки может уйти в минус и это видно', () => {
  assert.equal(logic.pocketAfter('900000', draft()), 792000);
  // Записали больше, чем выдали — прятать нельзя.
  assert.equal(logic.pocketAfter('50000', draft()), -58000);
  // Подотчёта нет — остатка тоже нет.
  assert.equal(logic.pocketAfter(null, draft()), null);
});

test('таймер закупа считает минуты', () => {
  const started = '2026-09-16T09:00:00+05:00';
  assert.equal(logic.tripElapsedMinutes(started, '2026-09-16T09:12:00+05:00'), 12);
  assert.equal(logic.tripElapsedMinutes(null, '2026-09-16T09:12:00+05:00'), null);
  assert.equal(logic.clock(12.5), '12:30');
  assert.equal(logic.clock(null), '—');
});

test('долю «Отчитались» считает сервер, своей формулы у экрана нет', () => {
  // «Функционал» §1: формулы раздела 3 — в одном серверном модуле.
  assert.equal(logic.reportedShare, undefined);
});

/* «За всё»: сумму покупки переводим в цену за единицу так, как её примет
   сервер (цена до тийина, итог = количество × цена с округлением половиной
   вверх), чтобы экран и накладная iiko не расходились. */
test('цена «за всё» даёт ровно введённую сумму, когда это возможно', () => {
  assert.deepEqual(logic.priceFromTotal('10', '120000'),
    {price: '12000', total: 120000, entered: 120000, exact: true});
  // Из всех цен с тем же итогом берём честное частное, а не «3 999,98».
  assert.equal(logic.priceFromTotal('0,25', '1000').price, '4000');
  assert.equal(logic.priceFromTotal('2.5', '12 500').price, '5000');
  assert.equal(logic.priceFromTotal('1,5', '4999,5').price, '3333');
  // Меньше единицы: 0,007 кг за 1 сум — цена с тийинами, итог тот же.
  const tiny = logic.priceFromTotal('0.007', '1');
  assert.equal(tiny.exact, true);
  assert.equal(logic.total({quantity: '0.007', price: tiny.price}), 1);
});

test('если ровно не делится — показываем итог, который уйдёт в накладную', () => {
  const fit = logic.priceFromTotal('3', '100000');
  assert.deepEqual(fit, {price: '33333.33', total: 99999.99, entered: 100000, exact: false});
  // Итог совпадает с тем, что посчитает сервер по отправленной цене.
  assert.equal(logic.total({quantity: '3', price: fit.price}), fit.total);
  assert.equal(logic.priceFromTotal('7.5', '100000').total, 99999.98);
  // Ближайший итог, даже если он больше введённого.
  assert.equal(logic.priceFromTotal('1000', '999999999').total, 1000000000);
});

test('больше знаков, чем примет сервер, — не считаем и объясняем', () => {
  assert.equal(logic.priceFromTotal('1.2345', '100'), null);
  assert.equal(logic.priceFromTotal('3', '100.001'), null);
  assert.equal(logic.amountProblem(draft({quantity: '1.2345'})), 'Количество — не больше трёх знаков после запятой');
  assert.equal(logic.amountProblem(draft({price: '10.555'})), 'Цена — не больше двух знаков после запятой');
  assert.equal(logic.amountProblem(draft({quantity: '1000', price: '2000000'})), 'Слишком большая сумма покупки');
  assert.equal(logic.amountProblem(draft({price: '12 000'})), '');
  assert.equal(logic.stepReady('amount', draft({quantity: '1.2345'})), false);
  // Пробелы-разделители в цене не мешают: «12 000» — это 12000.
  assert.equal(logic.total(draft({price: '12 000'})), 144000);
});

test('в расчётах закупа нет опыта и бонуса за скорость', () => {
  assert.equal(logic.xpPreview, undefined);
  assert.equal(logic.tripOnTime, undefined);
});
