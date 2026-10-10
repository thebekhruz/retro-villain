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
  assert.deepEqual(logic.filterCards(cards, 'all', 'ихтиер').map(c => c.id), [2], 'ё и е — одно и то же');
  assert.deepEqual(logic.filterCards(cards, 'all', 'повар').map(c => c.id), [3]);
  assert.deepEqual(logic.filterCards(cards, 'all', 'кухня милл').map(c => c.id), [3]);
  assert.deepEqual(logic.filterCards(cards, 'nophoto', '').map(c => c.id), [1]);
  assert.deepEqual(logic.filterCards(cards, 'problem', '').map(c => c.id), [3]);
  assert.deepEqual(logic.summary(cards), {total: 3, withPhoto: 2, noPhoto: 1, problems: 1});
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

test('T-434: временный — с периодом рядом с пометкой, сменный — без пометки', () => {
  assert.equal(logic.typeTag(card({employment_type: 'temporary', work_period: '08.10–10.10'})), 'временный · 08.10–10.10');
  assert.equal(logic.typeTag(card({employment_type: 'temporary', work_period: 'по 10.10'})), 'временный · по 10.10');
  assert.equal(logic.typeTag(card({employment_type: 'temporary', work_period: null})), 'временный');
  assert.equal(logic.typeTag(card({employment_type: 'shift'})), '');
});
