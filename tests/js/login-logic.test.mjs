import test from 'node:test';
import assert from 'node:assert/strict';
// UMD-модуль: из node приходит CJS-экспортом, в браузере ложится в globalThis.
import logic from '../../retro/static/login-logic.js';

/* Вход по номеру телефона (T-433). Нормализация — та же, что на сервере
   (retro/phone_numbers.py, tests/test_phone_login.py): один номер, как его ни набери. */
test('номер, набранный по-разному, — один и тот же', () => {
  for (const raw of ['+998901234567', '+998 90 123 45 67', '998901234567', '901234567',
    '90 123-45-67', '(90) 123 45 67', '+998 (90) 123-45-67', '00998901234567']) {
    assert.equal(logic.normalizePhone(raw), '+998901234567', raw);
  }
});

test('неполный и чужой номер не отправляется', () => {
  for (const raw of ['', '+998 ', '+998 90 123 45', '+7 999 123 45 67', '89011234567',
    '9989012345678', 'abc', null, undefined]) {
    assert.equal(logic.normalizePhone(raw), null, String(raw));
  }
});

test('маска: «+998 » всегда на месте, цифры группами 2-3-2-2', () => {
  assert.equal(logic.formatPhone(''), '+998 ');
  assert.equal(logic.formatPhone('+998'), '+998 ');
  assert.equal(logic.formatPhone('+998 9'), '+998 9');
  assert.equal(logic.formatPhone('+998 901'), '+998 90 1');
  assert.equal(logic.formatPhone('+998 90 1234'), '+998 90 123 4');
  assert.equal(logic.formatPhone('+998 90 123 45 67'), '+998 90 123 45 67');
  // Лишняя цифра в конце отрезается, номер не растёт.
  assert.equal(logic.formatPhone('+998 90 123 45 678'), '+998 90 123 45 67');
  // Вставили номер целиком — с кодом страны или без.
  assert.equal(logic.formatPhone('998901234567'), '+998 90 123 45 67');
  assert.equal(logic.formatPhone('901234567'), '+998 90 123 45 67');
  // Стёрли префикс и начали заново — префикс возвращается.
  assert.equal(logic.formatPhone('9'), '+998 9');
});

test('код: только цифры и не длиннее шести', () => {
  assert.equal(logic.codeDigits('12 34 56'), '123456');
  assert.equal(logic.codeDigits('1234567'), '123456');
  assert.equal(logic.codeDigits('код: 042'), '042');
  assert.equal(logic.codeDigits(null), '');
  assert.equal(logic.CODE_LENGTH, 6);
});

test('обратный отсчёт до повторной отправки', () => {
  assert.equal(logic.countdown(60), '1:00');
  assert.equal(logic.countdown(42), '0:42');
  assert.equal(logic.countdown(4.2), '0:05');
  assert.equal(logic.resendLabel(42), 'Отправить ещё раз через 0:42');
  assert.equal(logic.resendLabel(0), 'Отправить код ещё раз');
  assert.equal(logic.resendLabel(-3), 'Отправить код ещё раз');
});

test('подписи входа по номеру переводятся на узбекский', async () => {
  globalThis.document = {readyState: 'loading', addEventListener() {}};
  await import('../../retro/static/i18n-uz.js');
  await import('../../retro/static/i18n.js');
  const {translate} = globalThis.RetroI18n;
  assert.equal(translate('Войти по номеру телефона'), 'Telefon raqami orqali kirish');
  assert.equal(translate(logic.resendLabel(42)), '0:42 dan keyin qayta yuborish');
  assert.equal(translate('Код отправлен на +998 90 123 45 67.'), 'Kod +998 90 123 45 67 raqamiga yuborildi.');
  // На экране номер — с неразрывными пробелами, чтобы не рвался по строкам.
  assert.equal(translate('Код уже отправлен на +998\u00a090\u00a0123\u00a045\u00a067.'),
    'Kod +998\u00a090\u00a0123\u00a045\u00a067 raqamiga allaqachon yuborilgan.');
  assert.equal(translate('Неверный код. Осталось попыток: 3.'), "Kod noto'g'ri. Qolgan urinishlar: 3.");
  assert.equal(translate('Новый код можно запросить через 30 с.'), "Yangi kodni 30 soniyadan keyin so'rash mumkin.");
  assert.equal(translate('Слишком много SMS на этот номер. Попробуйте через 55 мин.'),
    "Bu raqamga juda ko'p SMS yuborildi. 55 daqiqadan keyin urinib ko'ring.");
  assert.equal(translate('Этот номер не подключён к панели. Обратитесь к администратору.'),
    'Bu raqam panelga ulanmagan. Administratorga murojaat qiling.');
});
