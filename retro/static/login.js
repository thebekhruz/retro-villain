'use strict';

const form = document.getElementById('login-form');
const submit = document.getElementById('submit');
const error = document.getElementById('error');

form.addEventListener('submit', (event) => {
  event.preventDefault();
  error.textContent = '';
  const attempt = signIn();
  // Спиннер на самой кнопке; повторное нажатие busy.js не пропустит.
  if (globalThis.RetroBusy) RetroBusy.button(submit, attempt);
  else { submit.disabled = true; attempt.finally(() => { submit.disabled = false; }); }
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
    window.location.assign(data.path);
    return true;
  } catch (failure) {
    error.textContent = failure.message;
    form.password.select();
    return false;
  }
}
