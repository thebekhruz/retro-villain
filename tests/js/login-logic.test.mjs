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

/* Панель на экране телефона (T-451): подсказка только на телефоне, только в
   обычном браузере и не в окне, открытом с иконки. */
const UA = {
  iphoneSafari: 'Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1',
  iphoneChrome: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/141.0.7390.41 Mobile/15E148 Safari/604.1',
  iphoneInApp: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 400.0.0.0',
  androidChrome: 'Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36',
  androidWebView: 'Mozilla/5.0 (Linux; Android 15; Pixel 9; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/141.0.0.0 Mobile Safari/537.36',
  androidTablet: 'Mozilla/5.0 (Linux; Android 15; SM-X710) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
  desktop: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
};

test('подсказка «на экран телефона»: какой телефон и какой браузер', () => {
  assert.equal(logic.installHint(UA.iphoneSafari, false), 'ios');
  assert.equal(logic.installHint(UA.iphoneChrome, false), 'ios');
  assert.equal(logic.installHint(UA.androidChrome, false), 'android');
  // Во встроенном браузере мессенджера пункта «На экран Домой» нет.
  assert.equal(logic.installHint(UA.iphoneInApp, false), null);
  assert.equal(logic.installHint(UA.androidWebView, false), null);
  // Только телефон.
  assert.equal(logic.installHint(UA.androidTablet, false), null);
  assert.equal(logic.installHint(UA.desktop, false), null);
  assert.equal(logic.installHint('', false), null);
  assert.equal(logic.installHint(undefined, false), null);
});

test('с иконки панель открыта как приложение — подсказки нет', () => {
  assert.equal(logic.installHint(UA.iphoneSafari, true), null);
  assert.equal(logic.installHint(UA.androidChrome, true), null);
});

test('текст подсказки: iPhone — через «Поделиться», Android — меню или кнопка', () => {
  assert.equal(logic.installText('ios', false), 'Добавьте панель на экран телефона: Поделиться → На экран «Домой».');
  assert.equal(logic.installText('android', false),
    'Добавьте панель на экран телефона: меню ⋮ → «Добавить на главный экран».');
  assert.equal(logic.installText('android', true), 'Добавьте панель на экран телефона.');
  assert.equal(logic.installText(null, false), '');
  for (const text of [logic.installText('ios'), logic.installText('android'), logic.installText('android', true)]) {
    assert.ok(!text.includes('!'), text);
    assert.ok(!text.includes('—'), text);
  }
});

test('подсказка «на экран телефона» переводится на узбекский', async () => {
  globalThis.document = {readyState: 'loading', addEventListener() {}};
  await import('../../retro/static/i18n-uz.js');
  await import('../../retro/static/i18n.js');
  const {translate} = globalThis.RetroI18n;
  for (const text of [logic.installText('ios'), logic.installText('android'), logic.installText('android', true),
    'Установить']) {
    const uz = translate(text);
    assert.ok(uz && !/[А-Яа-яЁё]/.test(uz), text + ' → ' + uz);
  }
  assert.equal(translate('Установить'), "O'rnatish");
});
