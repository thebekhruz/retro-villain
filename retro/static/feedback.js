/** Обратная связь и удобный ввод — общие для всех модулей.
 *
 *  1. Тост. Сообщение «Сохранено» или ошибка раньше выводились в строку
 *     наверху страницы. Бухгалтер жал «Записать» внизу «Финансов дня» и не
 *     видел ни успеха, ни отказа — для него это была тишина. Тост висит внизу
 *     экрана при любой прокрутке: успех зелёный и уходит сам, ошибка красная
 *     и держится, пока её не закроют.
 *  2. Подсказка суммы. Поля сумм числовые, и в «1500000» легко потерять или
 *     добавить ноль. Под полем по мере ввода видно «= 1 500 000 сум».
 *  3. Следующая строка. После успешной записи форма очищается, и курсор
 *     сразу стоит в первом поле — расходы дня вводятся подряд, без мыши. */
(() => {
  const OK_MS = 4500;
  let box = null, timer = 0;

  function ensureBox() {
    if (box) return box;
    box = document.createElement('div');
    box.className = 'rm-toast';
    box.hidden = true;
    const icon = document.createElement('span');
    icon.className = 'rm-toast-icon';
    icon.setAttribute('aria-hidden', 'true');
    const text = document.createElement('span');
    text.className = 'rm-toast-text';
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'rm-toast-close';
    close.setAttribute('aria-label', 'Закрыть');
    close.textContent = '×';
    close.addEventListener('click', hide);
    box.append(icon, text, close);
    document.body.append(box);
    return box;
  }

  function hide() {
    clearTimeout(timer);
    if (box) box.hidden = true;
  }

  /** Показать итог действия. kind: 'ok' | 'error'. Служебные «Загружаем…» не показываем. */
  function show(message, kind = 'ok') {
    const text = String(message || '').trim();
    if (!text || text.endsWith('…')) return;
    const node = ensureBox();
    const error = kind === 'error';
    node.classList.toggle('is-error', error);
    node.setAttribute('role', error ? 'alert' : 'status');
    node.querySelector('.rm-toast-icon').textContent = error ? '!' : '✓';
    node.querySelector('.rm-toast-text').textContent = text;
    node.hidden = false;
    // Перезапуск анимации: одинаковое сообщение подряд тоже должно быть заметно.
    node.classList.remove('is-in');
    void node.offsetWidth;
    node.classList.add('is-in');
    clearTimeout(timer);
    if (!error) timer = setTimeout(hide, OK_MS);
  }

  // ── Подсказка суммы ────────────────────────────────────────────────────
  const money = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});

  function isMoneyField(input) {
    if (input.dataset.moneyHint === 'off') return false;
    if (input.type !== 'number' && input.inputMode !== 'numeric' && input.inputMode !== 'decimal') return false;
    const label = input.labels && input.labels[0] ? input.labels[0].textContent : '';
    return input.dataset.moneyHint === 'on' || /сум|summa|so'm/i.test(label + ' ' + (input.getAttribute('aria-label') || ''));
  }

  function hintFor(input) {
    let hint = input.nextElementSibling;
    if (!hint || !hint.classList.contains('rm-money-hint')) {
      hint = document.createElement('span');
      hint.className = 'rm-money-hint';
      hint.setAttribute('aria-live', 'polite');
      input.insertAdjacentElement('afterend', hint);
    }
    return hint;
  }

  function updateHint(input) {
    const raw = String(input.value || '').replace(/\s/g, '').replace(',', '.');
    const value = Number(raw);
    const hint = hintFor(input);
    // Подсказка нужна там, где легко ошибиться в разрядах: от тысячи.
    hint.textContent = raw && Number.isFinite(value) && Math.abs(value) >= 1000 ? '= ' + money.format(value) + ' сум' : '';
  }

  document.addEventListener('input', event => {
    const input = event.target;
    if (input instanceof HTMLInputElement && isMoneyField(input)) updateHint(input);
  });

  // ── Следующая строка после записи ──────────────────────────────────────
  // Форму запоминаем в момент отправки: пока идёт запись, кнопку отключают,
  // и фокус с неё слетает — по activeElement уже не понять, чья это форма.
  let lastSubmitted = null, lastSubmittedAt = 0;
  document.addEventListener('submit', event => {
    lastSubmitted = event.target; lastSubmittedAt = Date.now();
  }, true);

  document.addEventListener('reset', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    setTimeout(() => {
      form.querySelectorAll('.rm-money-hint').forEach(hint => { hint.textContent = ''; });
      const first = form.querySelector('input:not([type=hidden]):not([disabled]), select:not([disabled]), textarea:not([disabled])');
      // Фокус только если человек работал в этой форме: не уводим экран к ней сам.
      const ownSubmit = form === lastSubmitted && Date.now() - lastSubmittedAt < 30000;
      if (first && (ownSubmit || form.contains(document.activeElement))) first.focus({preventScroll: true});
    }, 0);
  });

  globalThis.RetroToast = {show, hide};
})();
