import test from 'node:test';
import assert from 'node:assert/strict';

import logic from '../../retro/static/manager-logic.js';

const card = (over = {}) => ({id: 1, name: 'Абдулганиева Сельвина', role: 'Хостес', group: 'Встреча гостей',
  photo: null, hikvision: {state: 'none', employee_no: null, face: {state: 'none'}}, ...over});
const photo = {url: '/api/manager/employees/1/photo?v=1', updated_at: '2026-10-10T12:40:00+05:00'};

test('метка строки: что осталось сделать с человеком', () => {
  assert.deepEqual(logic.rowTag(card()), {text: 'Нет фото', tone: 'warn'});
  assert.deepEqual(logic.rowTag(card({photo, hikvision: {state: 'sent', face: {state: 'sent'}}})), {text: 'Готово', tone: 'ok'});
  assert.deepEqual(logic.rowTag(card({photo, hikvision: {state: 'sent', face: {state: 'error'}}})), {text: 'Ошибка', tone: 'error'});
  assert.deepEqual(logic.rowTag(card({photo, hikvision: {state: 'pending', face: {state: 'pending'}}})), {text: 'Ждёт отправки', tone: 'warn'});
  // Ручная отметка: на устройство не отправляем — достаточно фото.
  assert.deepEqual(logic.rowTag(card({photo, hikvision: {state: 'manual', face: {state: 'none'}}})), {text: 'Фото есть', tone: 'idle'});
});

test('поиск и фильтры списка: по имени, должности, без фото, с ошибками', () => {
  const cards = [
    card({id: 1}),
    card({id: 2, name: 'Баходиров Ихтиёр', role: 'Менеджер', group: 'Управление', photo,
      hikvision: {state: 'sent', face: {state: 'sent'}}}),
    card({id: 3, name: 'Рахимов Бобур', role: 'Повар миллий', group: 'Кухня', photo,
      hikvision: {state: 'sent', face: {state: 'error'}}}),
  ];
  assert.deepEqual(logic.filterCards(cards, {}, 'ихтиер').map(c => c.id), [2], 'ё и е — одно и то же');
  assert.deepEqual(logic.filterCards(cards, {}, 'повар').map(c => c.id), [3]);
  assert.deepEqual(logic.filterCards(cards, {}, 'кухня милл').map(c => c.id), [3]);
  assert.deepEqual(logic.filterCards(cards, {status: 'nophoto'}).map(c => c.id), [1]);
  assert.deepEqual(logic.filterCards(cards, {status: 'problem'}).map(c => c.id), [3]);
  assert.deepEqual(logic.summary(cards), {total: 3, withPhoto: 2, noPhoto: 1, problems: 1});
});

test('фильтры по разделу и должности: чипы только из списка, со счётом', () => {
  const cards = [
    card({id: 1, name: 'Рахимов Бобур', role: 'Повар миллий', group: 'Кухня'}),
    card({id: 2, name: 'Юсупов Фаррух', role: 'повар миллий', group: 'Кухня', photo}),
    card({id: 3, name: 'Кая Эмре', role: 'Повар турк', group: 'Кухня'}),
    card({id: 4, name: 'Абдуллаев Тимур', role: 'бармен', group: 'Бар'}),
  ];
  assert.deepEqual(logic.groupsOf(cards), [{key: 'Бар', label: 'Бар', count: 1}, {key: 'Кухня', label: 'Кухня', count: 3}]);
  assert.deepEqual(logic.rolesOf(cards, 'Кухня').map(r => [r.label, r.count]), [['Повар миллий', 2], ['Повар турк', 1]]);
  assert.deepEqual(logic.filterCards(cards, {group: 'Кухня'}).map(c => c.id), [1, 2, 3]);
  assert.deepEqual(logic.filterCards(cards, {group: 'Кухня', role: 'повар миллий'}).map(c => c.id), [1, 2]);
  assert.deepEqual(logic.filterCards(cards, {group: 'Кухня', role: 'повар миллий', status: 'nophoto'}).map(c => c.id), [1]);
  assert.deepEqual(logic.filterCards(cards, {group: 'Бар'}, 'тимур').map(c => c.id), [4]);
});

test('шаги карточки: фото, человек и лицо на устройстве', () => {
  assert.equal(logic.photoStep(card(), false).step, 'wait');
  assert.equal(logic.photoStep(card({photo}), false).title, 'Фото сохранено');
  assert.equal(logic.photoStep(card(), true).step, 'active');
  assert.equal(logic.hikvisionStep(card(), false).title, 'В Hikvision уйдёт вместе с фото');
  const sent = card({photo, hikvision: {state: 'sent', employee_no: '110', face: {state: 'pending'}}});
  assert.deepEqual([logic.hikvisionStep(sent, false).step, logic.hikvisionStep(sent, false).number], ['ok', '110']);
  assert.equal(logic.faceStep(sent, true).step, 'active');
  assert.equal(logic.faceStep(card(), false), null);
  const failed = card({photo, hikvision: {state: 'error', message: 'Hikvision недоступен', face: {state: 'none'}}});
  assert.equal(logic.hikvisionStep(failed, false).note, 'Hikvision недоступен');
  assert.match(logic.hikvisionStep(card({hikvision: {state: 'manual'}}), false).title, /вручную/);
});

test('должность с заглавной и группа — подпись строки', () => {
  assert.equal(logic.roleLine(card({role: 'бармен', group: 'Бар'})), 'Бармен · Бар');
  assert.equal(logic.roleLine(card({role: '', group: 'Кухня'})), 'Кухня');
});

test('инициалы для строки без фото', () => {
  assert.equal(logic.initials('Абдулганиева Сельвина'), 'АС');
  assert.equal(logic.initials('Карамат'), 'К');
  assert.equal(logic.initials(''), '·');
});

test('device photos count as ready; an unreachable terminal is not proof of no photo', () => {
  const remote = card({id: 1, photo: {...photo, source: 'hikvision'},
    hikvision: {state: 'sent', face: {state: 'sent'}}});
  const unknown = card({id: 2, photo_unknown: true});
  const missing = card({id: 3});
  assert.equal(logic.rowTag(remote).text, 'Готово');
  assert.equal(logic.photoStep(remote).title, 'Фото из Hikvision');
  assert.equal(logic.rowTag(unknown).text, 'Фото не проверено');
  assert.equal(logic.photoStep(unknown).title, 'Фото в Hikvision не проверено');
  assert.deepEqual(logic.filterCards([remote, unknown, missing], {status: 'nophoto'}).map(c=>c.id), [3]);
  assert.deepEqual(logic.summary([remote, unknown, missing]), {total:3, withPhoto:1, noPhoto:1, problems:1});
});
