import test from 'node:test';
import assert from 'node:assert/strict';
// UMD-модуль: из node приходит CJS-экспортом, в браузере ложится в globalThis.
import logic from '../../retro/static/employees-logic.js';

/* Ставку набирают как видят: «180 000». Поле number на iPhone отдавало
   такое значение пустым, и сотрудник молча сохранялся «без ставки». */
test('сумма из поля: разряды, запятая, пусто и ошибки', () => {
  assert.deepEqual(logic.parseAmount('180 000'), {value: '180000'});
  assert.deepEqual(logic.parseAmount('180 000'), {value: '180000'});
  assert.deepEqual(logic.parseAmount(' 180000 '), {value: '180000'});
  assert.deepEqual(logic.parseAmount('180 000,50'), {value: '180000.50'});
  assert.deepEqual(logic.parseAmount('180000.5'), {value: '180000.5'});
  assert.deepEqual(logic.parseAmount(''), {value: null});
  assert.deepEqual(logic.parseAmount(null), {value: null});
  assert.deepEqual(logic.parseAmount('0'), {error: 'zero'});
  assert.deepEqual(logic.parseAmount('180к'), {error: 'bad'});
  assert.deepEqual(logic.parseAmount('-5'), {error: 'bad'});
  assert.deepEqual(logic.parseAmount('1,005'), {error: 'bad'});
  assert.equal(logic.formatAmount('180000'), '180 000');
  assert.equal(logic.formatAmount(null), '');
});

test('поиск: регистр, ё/е, латиница находит кириллицу', () => {
  const m = (q, name) => logic.matchesQuery(q, name);
  assert.ok(m('алиев', 'Жасур Алиев'));
  assert.ok(m('АЛИЕВ', 'Жасур Алиев'));
  assert.ok(m('Aliev', 'Жасур Алиев'));
  assert.ok(m('Aliyev', 'Жасур Алиев'));
  assert.ok(m('jasur', 'Жасур Алиев'));
  assert.ok(m('Shoxrux', 'Нурмухаммедов Шохруххон'));
  assert.ok(m('Shokhrukh', 'Нурмухаммедов Шохруххон'));
  assert.ok(m('Ixtiyor', 'Баходиров Ихтиёр'));
  assert.ok(m('Федор', 'Фёдор Иванов'));
  assert.ok(logic.matchesQuery('официант', 'Кто-то', 'официант'));
  assert.ok(m('', 'Любой'));
  assert.ok(!m('zzz', 'Жасур Алиев'));
  assert.ok(!m('Karimov', 'Жасур Алиев'));
});

test('группа по должности — как на сервере; незнакомая — выбирают руками', () => {
  assert.equal(logic.groupForRole('официант'), 'Обслуживание зала');
  assert.equal(logic.groupForRole(' Повар горячего цеха '), 'Кухня');
  assert.equal(logic.groupForRole('кондитер'), 'Кухня');
  assert.equal(logic.groupForRole('охрана'), 'Охрана');
  assert.equal(logic.groupForRole('сомелье'), null);
  assert.equal(logic.groupForRole(''), null);
});

test('«Не начисляется»: все причины сразу', () => {
  assert.deepEqual(logic.attentionReasons({rate: null, status: 'unlinked'}), ['Нет ставки', 'нет привязки Hikvision']);
  assert.deepEqual(logic.attentionReasons({rate: '1', status: 'unavailable'}), ['Нет данных Hikvision']);
  assert.deepEqual(logic.attentionReasons({rate: null, status: 'manual_present'}), ['Нет ставки']);
  assert.deepEqual(logic.attentionReasons({rate: '1', status: 'on_time'}), []);
});
