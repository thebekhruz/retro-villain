'use strict';

const form = document.getElementById('login-form');
const submit = document.getElementById('submit');
const error = document.getElementById('error');

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  submit.disabled = true;
  error.textContent = '';
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
  } catch (failure) {
    error.textContent = failure.message;
    form.password.select();
  } finally {
    submit.disabled = false;
  }
});
