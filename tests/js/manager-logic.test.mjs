import test from 'node:test';
import assert from 'node:assert/strict';
// UMD-модуль: из node приходит CJS-экспортом, в браузере ложится в globalThis.
import logic from '../../retro/static/manager-logic.js';

/* Кабинет менеджера (T-432). Проверка на экране — та же, что на сервере
   (retro/modules/accountant/names.py): тексты ошибок совпадают слово в слово. */
test('кириллица: узбекские буквы, пробелы и дефис проходят', () => {
  assert.deepEqual(logic.checkText('Карамат'), {value: 'Карамат'});
  assert.deepEqual(logic.checkText('  Абдулганиева   Сельвина '), {value: 'Абдулганиева Сельвина'});
  assert.deepEqual(logic.checkText('Ўткир Қодиров'), {value: 'Ўткир Қодиров'});
  assert.deepEqual(logic.checkText('Ғайрат Ҳамидов'), {value: 'Ғайрат Ҳамидов'});
  assert.deepEqual(logic.checkText('Анна-Мария'), {value: 'Анна-Мария'});
  assert.deepEqual(logic.checkText('Повар 2-го цеха (тандыр)', 'role'), {value: 'Повар 2-го цеха (тандыр)'});
});

test('латиница и смесь похожих букв — отказ с названием буквы', () => {
  assert.equal(logic.checkText('Karamat').error, 'Имя набрано латиницей — наберите кириллицей.');
  assert.equal(logic.checkText('Карамat').error,
    'В имени «Карамat» латинская «a» вместо кириллической «а» — наберите кириллицей.');
  assert.equal(logic.checkText('Kарамат').error,
    'В имени «Kарамат» латинская «K» вместо кириллической «К» — наберите кириллицей.');
  assert.equal(logic.checkText('Каrамат').error, 'В имени «Каrамат» латинская буква «r» — наберите кириллицей.');
  assert.equal(logic.checkText('Сотрудник 1').error,
    'В имени «Сотрудник 1» недопустимый знак «1»: можно буквы кириллицы, пробел и дефис.');
  assert.equal(logic.checkText('Іван').error, 'В имени «Іван» буква «І» не из русской или узбекской кириллицы.');
  assert.equal(logic.checkText('   ').error, 'Укажите имя сотрудника.');
  assert.equal(logic.checkText('Hostess', 'role').error, 'Должность набрана латиницей — наберите кириллицей.');
});

test('ошибка сервера попадает под своё поле', () => {
  assert.equal(logic.fieldOfError('Имя набрано латиницей — наберите кириллицей.'), 'name');
  assert.equal(logic.fieldOfError('В имени «Карамat» латинская «a» вместо кириллической «а» — наберите кириллицей.'), 'name');
  assert.equal(logic.fieldOfError('Должность набрана латиницей — наберите кириллицей.'), 'role');
  assert.equal(logic.fieldOfError('Это направление ведёт другой менеджер.'), 'direction');
  assert.equal(logic.fieldOfError('Нет связи с панелью.'), null);
});

test('Hikvision: успех только после подтверждения, ошибка — с причиной и повтором', () => {
  const card = state => ({hikvision: {state, employee_no: '134', message: state === 'error' ? 'Hikvision недоступен — карточка сохранена и ждёт отправки.' : null}});
  assert.deepEqual(logic.hikvisionStep(card('sent')), {step: 'ok', title: 'Добавлен в Hikvision', number: '134', note: null});
  assert.equal(logic.hikvisionStep(card('pending'), true).step, 'active');
  assert.equal(logic.hikvisionStep(card('pending'), true).title, 'Отправляем в Hikvision…');
  const failed = logic.hikvisionStep(card('error'));
  assert.equal(failed.step, 'error');
  assert.equal(failed.note, 'Hikvision недоступен — карточка сохранена и ждёт отправки.');
  assert.equal(logic.hikvisionStep(card('pending')).title, 'Ожидает отправки в Hikvision');
  assert.deepEqual(logic.hikvisionTag(card('sent')), {text: 'В Hikvision', tone: 'ok'});
  assert.deepEqual(logic.hikvisionTag(card('error')), {text: 'Ошибка Hikvision', tone: 'error'});
  assert.equal(logic.hikvisionTag({kind: 'monthly', hikvision: null}), null);
});

test('инициалы и ключ запроса', () => {
  assert.equal(logic.initials('Абдулганиева Сельвина'), 'АС');
  assert.equal(logic.initials('Карамат'), 'К');
  assert.equal(logic.newKey({randomUUID: () => 'fixed'}), 'fixed');
  assert.match(logic.newKey(null), /^k[0-9a-z]+$/);
});
