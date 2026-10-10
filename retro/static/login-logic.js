/* Вход по номеру телефона без DOM (ТЗ 09.10, М-05): маска +998, номер для
   сервера, цифры кода и обратный отсчёт до «Отправить ещё раз». Нормализация
   — та же, что на сервере (retro/phone_numbers.py): «90 123 45 67»,
   «998901234567» и «+998 (90) 123-45-67» — один номер. Тесты —
   tests/js/login-logic.test.mjs. */
(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LoginLogic = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {

  const LOCAL = 9;          // цифр после +998: код оператора и номер
  const CODE_LENGTH = 6;
  const PREFIX = '+998 ';

  /* Цифры после +998 или null, если номер не узбекский. Плюс или больше
     девяти цифр — код страны набран, и он обязан быть 998. */
  function localDigits(raw) {
    const text = String(raw == null ? '' : raw).trim();
    let digits = text.replace(/\D/g, '');
    if (digits.startsWith('00998')) digits = digits.slice(2);
    if (text.startsWith('+') || digits.length > LOCAL) return digits.startsWith('998') ? digits.slice(3) : null;
    return digits;
  }

  /* «+998 90 123 45 67» → «+998901234567»; неполный или чужой номер — null. */
  function normalizePhone(raw) {
    const digits = localDigits(raw);
    return digits !== null && digits.length === LOCAL ? '+998' + digits : null;
  }

  /* Маска поля: «+998 » всегда на месте, дальше 2-3-2-2. Лишнее отрезаем. */
  function formatPhone(raw) {
    const digits = (localDigits(raw) || '').slice(0, LOCAL);
    const groups = [digits.slice(0, 2), digits.slice(2, 5), digits.slice(5, 7), digits.slice(7, 9)];
    return PREFIX + groups.filter(Boolean).join(' ');
  }

  /* Код из SMS: только цифры (iOS подставляет его целиком), не длиннее шести. */
  function codeDigits(raw, length = CODE_LENGTH) {
    return String(raw == null ? '' : raw).replace(/\D/g, '').slice(0, length);
  }

  const countdown = seconds => {
    const left = Math.max(0, Math.ceil(seconds));
    return Math.floor(left / 60) + ':' + String(left % 60).padStart(2, '0');
  };

  /* Подпись кнопки повторной отправки: пока идёт отсчёт — «через 0:42». */
  function resendLabel(seconds) {
    return seconds > 0 ? 'Отправить ещё раз через ' + countdown(seconds) : 'Отправить код ещё раз';
  }

  return {localDigits, normalizePhone, formatPhone, codeDigits, countdown, resendLabel, CODE_LENGTH, PREFIX};
});
