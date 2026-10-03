/* «Сохранить» в каждой вкладке и каждом окне (ТЗ 02.10, п. 6).

   Почти всё в панели записывается сразу — кнопкой строки или уходом из поля
   суммы. Кнопка «Сохранить» дописывает то, что набрано, но ещё не отправлено:
   каждая форма ввода регистрирует здесь, как себя сохранить. После нажатия —
   «Сохранено». Есть несохранённое и бухгалтер уходит со страницы — браузер
   переспросит, а переход внутри страницы (другой день, другой месяц)
   спрашивает confirmLeave().

   RetroSave.register(form, save, {dirty})
     save()  — отправить форму; вернуть false, если не получилось.
     dirty() — есть ли набранное; по умолчанию — непустое текстовое поле.
   RetroSave.track(promise) — запись в работе: «Сохранить» её дождётся. */
(() => {
  const entries = [];
  const inflight = new Set();
  const say = (text, kind) => globalThis.RetroToast?.show(text, kind);
  const tr = text => (document.documentElement.lang === 'uz' && globalThis.RetroI18n
    ? globalThis.RetroI18n.translate(text) || text : text);

  const TEXT = 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=date])'
    + ':not([type=month]):not([type=search]):not([data-save-ignore]), textarea';
  function typed(form) {
    return [...form.querySelectorAll(TEXT)].some(input => !input.disabled && !input.readOnly
      && input.value.trim() !== (input.dataset.clean ?? input.defaultValue ?? '').trim());
  }
  const dirty = entry => entry.form.isConnected && !entry.form.closest('[hidden]')
    && (entry.dirty ? entry.dirty() : typed(entry.form));

  function register(form, save, options = {}) {
    if (!form) return;
    entries.push({form, save, dirty: options.dirty || null});
  }
  function pending() { return entries.filter(dirty); }

  function track(work) {
    const promise = Promise.resolve(work);
    inflight.add(promise);
    promise.finally(() => inflight.delete(promise)).catch(() => {});
    return work;
  }
  async function settle() {
    while (inflight.size) await Promise.allSettled([...inflight]);
  }

  /* Сохранить всё набранное. quiet — без «Сохранено» (его скажет вызвавший). */
  async function saveAll({quiet = false} = {}) {
    // Сумма в строке фиксирует себя на уходе из поля — даём ей это сделать.
    const active = document.activeElement;
    if (active && active !== document.body && typeof active.blur === 'function') active.blur();
    await new Promise(resolve => setTimeout(resolve, 0));
    await settle();
    for (const entry of pending()) {
      let ok = false;
      try { ok = await entry.save(); } catch (error) { say(error.message || 'Не удалось сохранить.', 'error'); ok = false; }
      if (ok === false) return false;
    }
    await settle();
    if (pending().length) {
      say('Не всё сохранено: проверьте выделенные поля.', 'error');
      return false;
    }
    if (!quiet) say('Сохранено');
    return true;
  }

  function confirmLeave() {
    if (!pending().length && !inflight.size) return true;
    return confirm(tr('Есть несохранённые данные. Уйти без сохранения?'));
  }

  window.addEventListener('beforeunload', event => {
    if (!pending().length && !inflight.size) return;
    event.preventDefault();
    event.returnValue = '';
  });

  // Кнопки «Сохранить» с атрибутом data-save — на любой странице.
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-save]');
    if (!button || button.disabled) return;
    event.preventDefault();
    const work = saveAll();
    // false — «не сохранено»: busy.js не ставит ✓ на кнопку.
    if (globalThis.RetroBusy) globalThis.RetroBusy.button(button, work);
  });

  globalThis.RetroSave = {register, pending, track, saveAll, confirmLeave};
})();
