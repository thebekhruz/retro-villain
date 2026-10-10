'use strict';

const form = document.getElementById('login-form');
const submit = document.getElementById('submit');
const error = document.getElementById('error');

// Спиннер на самой кнопке; повторное нажатие busy.js не пропустит.
function busy(button, attempt) {
  if (globalThis.RetroBusy) RetroBusy.button(button, attempt);
  else { button.disabled = true; attempt.finally(() => { button.disabled = false; }); }
  return attempt;
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  error.textContent = '';
  busy(submit, signIn());
});

async function signIn() {
  try {
    const response = await fetch('/api/session', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        username: form.username.value,
        password: form.password.value,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Не удалось войти.');
    remember('password');
    window.location.assign(data.path);
    return true;
  } catch (failure) {
    error.textContent = failure.message;
    form.password.select();
    return false;
  }
}

/* ── Вход по номеру телефона и SMS-коду (ТЗ 09.10, М-05) ────────────────
   Номер → «Получить код» → код из SMS → кабинет. Сессия живёт на телефоне,
   поэтому код нужен только при первом входе, после «Выйти» и по истечении
   срока. Экран помнит, как человек входил в прошлый раз. */
const Logic = globalThis.LoginLogic;
const $ = id => document.getElementById(id);
const card = document.querySelector('.login-card');
const subtitle = $('subtitle');
const phoneForm = $('phone-form');
const phoneInput = $('phone');
const codeForm = $('code-form');
const codeInput = $('code');
const resend = $('resend');
const modeSwitch = $('mode-switch');
const MODE_KEY = 'retro:login-mode';
const SUBTITLES = {
  password: 'Введите логин и пароль своей панели.',
  phone: 'Введите номер телефона — пришлём код в SMS.',
};
const NETWORK = 'Нет связи с сервером. Проверьте интернет и повторите.';
const phoneLogin = card.dataset.phoneLogin === 'on';

let step = 'password';
let phone = null;
let resendAt = 0;
let timer = null;
let submittedCode = '';

function remember(mode) {
  try { localStorage.setItem(MODE_KEY, mode); } catch { /* приватный режим */ }
}

function remembered() {
  try { return localStorage.getItem(MODE_KEY); } catch { return null; }
}

function show(next) {
  step = next;
  form.hidden = next !== 'password';
  phoneForm.hidden = next !== 'phone';
  codeForm.hidden = next !== 'code';
  $('code-links').hidden = next !== 'code';
  // На шаге кода переключатель не нужен: назад ведёт «Другой номер».
  modeSwitch.hidden = !phoneLogin || next === 'code';
  modeSwitch.textContent = next === 'password' ? 'Войти по номеру телефона' : 'Войти по логину и паролю';
  if (next !== 'code') subtitle.textContent = SUBTITLES[next];
  error.textContent = '';
}

function caretToEnd(input) {
  const end = input.value.length;
  try { input.setSelectionRange(end, end); } catch { /* type=tel без выделения */ }
}

async function post(path, body) {
  let response;
  try {
    response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)});
  } catch {
    throw new Error(NETWORK);
  }
  const data = await response.json().catch(() => ({}));
  return {ok: response.ok, status: response.status, data};
}

function tick() {
  const left = (resendAt - Date.now()) / 1000;
  resend.textContent = Logic.resendLabel(left);
  resend.disabled = left > 0;
  if (left <= 0 && timer !== null) { clearInterval(timer); timer = null; }
}

function startCountdown(seconds) {
  resendAt = Date.now() + Math.max(0, seconds) * 1000;
  if (timer !== null) clearInterval(timer);
  timer = setInterval(tick, 1000);
  tick();
}

async function requestCode(number) {
  try {
    const {ok, data} = await post('/api/session/phone/code', {phone: number});
    if (!ok) {
      error.textContent = data.detail || 'Не удалось отправить код. Повторите.';
      if (step === 'code' && data.retry_after) startCountdown(data.retry_after);
      return false;
    }
    phone = number;
    show('code');
    // Номер не рвётся по строкам: неразрывные пробелы между группами.
    subtitle.textContent = (data.sent ? 'Код отправлен на ' : 'Код уже отправлен на ')
      + String(data.phone).replace(/ /g, '\u00a0') + '.';
    codeInput.value = '';
    submittedCode = '';
    startCountdown(data.resend_in);
    codeInput.focus();
    return true;
  } catch (failure) {
    error.textContent = failure.message;
    return false;
  }
}

async function verify(code) {
  try {
    const {ok, data} = await post('/api/session/phone/verify', {phone, code});
    if (!ok) {
      error.textContent = data.detail || 'Не удалось войти. Повторите.';
      // Неверный код с оставшимися попытками — исправить; иначе поле пустое:
      // этот код больше не примут, нужен новый.
      if (data.code === 'wrong' && data.attempts_left > 0) codeInput.select();
      else codeInput.value = '';
      submittedCode = '';
      return false;
    }
    remember('phone');
    window.location.assign(data.path);
    return true;
  } catch (failure) {
    error.textContent = failure.message;
    submittedCode = '';
    return false;
  }
}

modeSwitch.addEventListener('click', () => {
  if (step === 'password') {
    show('phone');
    remember('phone');
    phoneInput.focus();
    caretToEnd(phoneInput);
  } else {
    show('password');
    remember('password');
    form.username.focus();
  }
});

// Маска: «+998 » всегда на месте, цифры встают группами 2-3-2-2.
phoneInput.addEventListener('input', () => {
  const next = Logic.formatPhone(phoneInput.value);
  if (next !== phoneInput.value) { phoneInput.value = next; caretToEnd(phoneInput); }
});
// Пустое поле — курсор после «+998 », а не перед ним. Набранный номер не
// трогаем: человек мог выделить его, чтобы заменить.
phoneInput.addEventListener('focus', () => setTimeout(() => {
  if (phoneInput.value === Logic.PREFIX) caretToEnd(phoneInput);
}, 0));

phoneForm.addEventListener('submit', (event) => {
  event.preventDefault();
  error.textContent = '';
  const number = Logic.normalizePhone(phoneInput.value);
  if (!number) {
    error.textContent = 'Введите номер полностью: +998 и 9 цифр.';
    phoneInput.focus();
    return;
  }
  busy($('phone-submit'), requestCode(number));
});

// Шесть цифр — входим сразу: iOS подставляет код из SMS целиком.
codeInput.addEventListener('input', () => {
  const digits = Logic.codeDigits(codeInput.value);
  if (digits !== codeInput.value) codeInput.value = digits;
  if (digits.length === Logic.CODE_LENGTH && digits !== submittedCode) $('code-submit').click();
});

codeForm.addEventListener('submit', (event) => {
  event.preventDefault();
  error.textContent = '';
  const code = Logic.codeDigits(codeInput.value);
  if (code.length !== Logic.CODE_LENGTH) {
    error.textContent = 'Введите ' + Logic.CODE_LENGTH + ' цифр из SMS.';
    codeInput.focus();
    return;
  }
  submittedCode = code;
  busy($('code-submit'), verify(code));
});

resend.addEventListener('click', () => {
  if (!phone || resend.disabled) return;
  error.textContent = '';
  busy(resend, requestCode(phone));
});

$('other-phone').addEventListener('click', () => {
  if (timer !== null) { clearInterval(timer); timer = null; }
  show('phone');
  phoneInput.focus();
  caretToEnd(phoneInput);
});

if (phoneLogin && remembered() === 'phone') {
  show('phone');
  phoneInput.focus();
} else {
  show('password');
}
