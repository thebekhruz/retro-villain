/** RetroBusy — единый отклик панели на нажатия и ожидание.
 *
 *  Принципы (подробно и с примерами — docs/feedback-principles.md):
 *  1. Каждое нажатие отвечает сразу, быстрее 100 мс: кнопки, чипы, вкладки,
 *     ячейки «проседают» (:active в style.css), с клавиатуры виден фокус.
 *  2. Ожидание показываем там, где оно происходит, а не общим спиннером:
 *     крутится та кнопка / то поле / та строка, что начали действие.
 *  3. Быстрое (< 250 мс) не мигает: до порога видно только нажатие и итог ✓.
 *     Показанный спиннер держится не меньше 350 мс — без дёрганья.
 *  4. Итог виден на месте: ✓ на кнопке, зелёная галочка в поле и подсветка
 *     строки; ошибка — красная рамка и текст под полем, введённое не теряется.
 *  5. Набранное, но не отправленное число выглядит иначе, чем сохранённое.
 *  6. Перезагрузка раздела не опустошает его: данные слегка гаснут, сверху
 *     тонкая золотая полоса. Первая загрузка — скелет в форме содержимого.
 *  7. Размеры не прыгают: спиннер рисуется поверх подписи, ширина кнопки та же.
 *  8. Во время записи повторное нажатие не проходит — двойной выдачи нет.
 *
 *  Модули перерисовывают строки целиком (replaceChildren). Чтобы состояние
 *  «сохраняю / ✓ / ошибка» пережило перерисовку, у элемента ставят
 *  data-busy-key — новый узел с тем же ключом получает состояние сам.
 *
 *  API: RetroBusy.button, field, row, flash, section, skeleton, track,
 *  silent, clear. Запросы к /api/ сами ведут верхнюю полосу; фоновые
 *  опросы помечают fetch(url, {retroBusy: false}) или RetroBusy.silent(fn). */
(() => {
  const DELAY = 250;        // раньше этого ничего не показываем
  const MIN_SHOWN = 350;    // показанный спиннер не исчезает раньше
  const DONE_MS = 900;      // ✓ на кнопке
  const FIELD_OK_MS = 1600; // галочка в поле и подсветка строки
  const COLLAPSE_MS = 220;  // удалённая строка сворачивается

  /* ── Чистые помощники (tests/js/busy.test.mjs) ─────────────────────── */

  /** Ведёт ли запрос верхнюю полосу: только наш /api/, не помеченный тихим. */
  function tracksRequest(url, init, origin) {
    if (init && init.retroBusy === false) return false;
    let parsed;
    try { parsed = new URL(String(url), origin || 'http://localhost'); } catch { return false; }
    if (origin && parsed.origin !== new URL(origin).origin) return false;
    return parsed.pathname.startsWith('/api/');
  }

  /** Итог действия по значению обещания: false — «не удалось» (так отвечает run() модулей). */
  const outcome = value => (value === false ? 'error' : 'ok');

  /** Число в поле без разрядных пробелов: «1 500 000» и «1500000» — одно и то же. */
  const normalize = value => String(value ?? '').replace(/[\s\u00a0\u202f]/g, '').trim();
  const isDirty = (value, saved) => normalize(value) !== normalize(saved);

  /** Сколько ещё держать показанный спиннер, чтобы он не мигнул. */
  const holdFor = (shownAt, now, min = MIN_SHOWN) => (shownAt === null ? 0 : Math.max(0, min - (now - shownAt)));

  /** Счётчик запросов для полосы: показывает после порога, прячет, когда всё закончилось.
   *  Таймеры внедряются — так он проверяется без браузера. */
  function createTracker({onShow, onHide, delay = DELAY, timers = globalThis}) {
    let count = 0, timer = null, shown = false;
    return {
      start() {
        count += 1;
        if (count === 1 && !shown && timer === null) {
          timer = timers.setTimeout(() => { timer = null; if (count > 0) { shown = true; onShow(); } }, delay);
        }
      },
      end() {
        count = Math.max(0, count - 1);
        if (count) return;
        if (timer !== null) { timers.clearTimeout(timer); timer = null; }
        if (shown) { shown = false; onHide(); }
      },
      get count() { return count; },
      get shown() { return shown; },
    };
  }

  /** Ширины полос скелета: разные, но одинаковые от загрузки к загрузке. */
  const skeletonWidths = (rows, seed = 0) =>
    Array.from({length: rows}, (_, i) => 48 + ((i + seed) * 37) % 45);

  const pure = {tracksRequest, outcome, normalize, isDirty, holdFor, createTracker, skeletonWidths};
  if (typeof document === 'undefined') { globalThis.RetroBusy = {pure}; return; }

  /* ── Реестр состояний по data-busy-key ─────────────────────────────── */
  const states = new Map();
  const keyOf = el => (el && el.dataset ? el.dataset.busyKey || null : null);
  const find = key => document.querySelector('[data-busy-key="' + (globalThis.CSS?.escape ? CSS.escape(key) : key) + '"]');
  /** Живой узел: перерисованный с тем же ключом или исходный. */
  const live = (el, key) => (el && el.isConnected ? el : key ? find(key) : null) || null;
  const now = () => performance.now();
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const tr = text => (document.documentElement.lang === 'uz' && globalThis.RetroI18n?.translate(text)) || text;

  function asPromise(work) {
    try { return Promise.resolve(typeof work === 'function' ? work() : work); }
    catch (error) { return Promise.reject(error); }
  }

  /* ── Кнопка ────────────────────────────────────────────────────────── */
  // Только классы и aria — без инлайн-стилей: спиннер рисуется цветом подписи
  // (currentColor), а подпись прячет -webkit-text-fill-color.
  function paintButton(el, entry) {
    if (!el) return;
    const locked = entry.state === 'pending';
    el.classList.toggle('rm-lock', locked);
    el.classList.toggle('rm-busy', locked && entry.shown);
    el.classList.toggle('rm-done', entry.state === 'done');
    if (locked || entry.state === 'done') {
      if (!el.classList.contains('rm-rel') && getComputedStyle(el).position === 'static') el.classList.add('rm-rel');
    } else el.classList.remove('rm-rel');
    if (locked) el.setAttribute('aria-busy', 'true'); else el.removeAttribute('aria-busy');
  }

  /** Кнопка, начавшая действие: блок от двойного нажатия сразу, спиннер после
   *  порога, ✓ на ~0,9 с при успехе. Обещание возвращается как есть.
   *  opts.done=false — без ✓ (итог покажет строка или поле). */
  function button(el, work, opts = {}) {
    const promise = asPromise(work);
    if (!el) return promise;
    const key = keyOf(el);
    const entry = {kind: 'button', state: 'pending', shown: false, shownAt: null};
    if (key) states.set(key, entry);
    paintButton(el, entry);
    const timer = setTimeout(() => {
      if (entry.state !== 'pending') return;
      entry.shown = true; entry.shownAt = now();
      paintButton(live(el, key), entry);
    }, DELAY);
    const finish = async result => {
      clearTimeout(timer);
      await wait(holdFor(entry.shownAt, now()));
      if (key && states.has(key) && states.get(key) !== entry) return;
      entry.state = result === 'ok' && opts.done !== false ? 'done' : 'idle';
      entry.shown = false;
      paintButton(live(el, key), entry);
      if (entry.state === 'done') {
        await wait(DONE_MS);
        if (entry.state !== 'done') return;
        entry.state = 'idle';
        paintButton(live(el, key), entry);
      }
      if (key && states.get(key) === entry) states.delete(key);
    };
    promise.then(value => finish(outcome(value)), () => finish('error'));
    return promise;
  }

  /* ── Поле ──────────────────────────────────────────────────────────── */
  const FIELD_CLASSES = ['rm-field-dirty', 'rm-field-saving', 'rm-field-ok', 'rm-field-error'];

  function messageNode(input) {
    const next = input.nextElementSibling;
    return next && next.classList.contains('rm-field-msg') ? next : null;
  }

  function paintField(input, entry) {
    if (!input) return;
    FIELD_CLASSES.forEach(cls => input.classList.remove(cls));
    const align = getComputedStyle(input).textAlign;
    input.classList.toggle('rm-field-start', align === 'right' || align === 'end');
    if (entry.state === 'saving' || entry.state === 'error' || entry.state === 'dirty') {
      if (entry.value !== undefined && input.value !== entry.value) input.value = entry.value;
    }
    if (entry.state === 'dirty') input.classList.add('rm-field-dirty');
    if (entry.state === 'saving') input.classList.add(entry.shown ? 'rm-field-saving' : 'rm-field-dirty');
    if (entry.state === 'ok') input.classList.add('rm-field-ok');
    if (entry.state === 'error') input.classList.add('rm-field-error');
    const saving = entry.state === 'saving';
    if (saving && !input.readOnly) { input.readOnly = true; input.dataset.busyRo = '1'; }
    if (!saving && input.dataset.busyRo) { input.readOnly = false; delete input.dataset.busyRo; }
    if (saving) input.setAttribute('aria-busy', 'true'); else input.removeAttribute('aria-busy');
    if (entry.state === 'error') input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid');
    let note = messageNode(input);
    if (entry.state === 'error' && entry.message) {
      if (!note) {
        note = document.createElement('span');
        note.className = 'rm-field-msg';
        note.setAttribute('role', 'alert');
        input.insertAdjacentElement('afterend', note);
      }
      note.textContent = entry.message;
    } else if (note) note.remove();
  }

  /** Поле, которое сохраняется само (по Enter / уходу из поля): «сохраняю…»
   *  внутри поля, затем зелёная галочка и подсветка строки; ошибка — красная
   *  рамка и текст под полем, введённое остаётся для повтора.
   *  opts.row — строка для подсветки; opts.message=false — без текста под полем;
   *  opts.restore — при ошибке поле не возвращает набранное (показывает сохранённое). */
  function field(input, work, opts = {}) {
    const promise = asPromise(work);
    if (!input) return promise;
    const key = keyOf(input);
    const entry = {kind: 'field', state: 'saving', shown: false, shownAt: null, value: input.value, message: ''};
    if (key) states.set(key, entry);
    paintField(input, entry);
    const timer = setTimeout(() => {
      if (entry.state !== 'saving') return;
      entry.shown = true; entry.shownAt = now();
      paintField(live(input, key), entry);
    }, DELAY);
    const finish = async (result, error) => {
      clearTimeout(timer);
      await wait(holdFor(entry.shownAt, now()));
      if (key && states.has(key) && states.get(key) !== entry) return;
      entry.shown = false;
      if (result === 'ok') {
        entry.state = 'ok';
        const target = live(input, key);
        paintField(target, entry);
        if (opts.row !== false) flash(opts.row || target?.closest('[data-busy-key].fd-row, [data-busy-key].pr-row, [data-busy-row]'));
        await wait(FIELD_OK_MS);
        if (entry.state !== 'ok') return;
        entry.state = 'idle';
        paintField(live(input, key), entry);
        if (key && states.get(key) === entry) states.delete(key);
      } else {
        entry.state = 'error';
        // opts.restore — отклонённое значение в поле не держим: после
        // перерисовки в нём сохранённое, а ошибка остаётся рамкой и текстом.
        if (opts.restore) entry.value = undefined;
        entry.message = opts.message === false ? '' : (error && error.message) || tr('Не сохранено. Проверьте и повторите.');
        paintField(live(input, key), entry);
      }
    };
    promise.then(value => finish(outcome(value)), error => finish('error', error));
    return promise;
  }

  // «Не отправлено»: поле с data-busy-key, в котором набрали, но ещё не сохранили.
  const baseline = new WeakMap();
  document.addEventListener('focusin', event => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement) || !keyOf(input)) return;
    const entry = states.get(keyOf(input));
    if (!entry || entry.state === 'idle' || entry.state === 'ok') baseline.set(input, input.value);
  }, true);
  document.addEventListener('input', event => {
    const input = event.target;
    const key = keyOf(input);
    if (!(input instanceof HTMLInputElement) || !key) return;
    const entry = states.get(key);
    if (entry && entry.kind === 'field' && entry.state === 'saving') return;
    const dirty = isDirty(input.value, baseline.has(input) ? baseline.get(input) : input.defaultValue);
    if (dirty) {
      const next = entry && entry.kind === 'field' && entry.state === 'dirty' ? entry : {kind: 'field', state: 'dirty'};
      next.value = input.value;
      states.set(key, next);
      paintField(input, next);
    } else if (entry && entry.kind === 'field') {
      states.delete(key);
      paintField(input, {state: 'idle'});
    }
  }, true);
  // Модуль вернул прежнее значение (Escape, «не менять») — снимаем пометку.
  document.addEventListener('focusout', event => {
    const input = event.target;
    const key = keyOf(input);
    if (!(input instanceof HTMLInputElement) || !key) return;
    setTimeout(() => {
      const entry = states.get(key);
      if (!entry || entry.kind !== 'field' || entry.state !== 'dirty') return;
      const target = live(input, key);
      if (target && !isDirty(target.value, baseline.get(input) ?? target.defaultValue)) {
        states.delete(key); paintField(target, {state: 'idle'});
      }
    }, 0);
  }, true);

  /* ── Строка ────────────────────────────────────────────────────────── */
  function paintRow(el, entry) {
    if (!el) return;
    el.classList.toggle('rm-row-pending', entry.state === 'pending' && entry.shown);
    if (entry.state === 'pending') el.setAttribute('aria-busy', 'true'); else el.removeAttribute('aria-busy');
    if (entry.state === 'ok') restartFlash(el);
  }
  function restartFlash(el) {
    el.classList.remove('rm-flash-ok');
    void el.offsetWidth;
    el.classList.add('rm-flash-ok');
    setTimeout(() => el.classList.remove('rm-flash-ok'), FIELD_OK_MS);
  }

  /** Короткая зелёная подсветка «записано» — переживает перерисовку по ключу. */
  function flash(el) {
    if (!el) return;
    const key = keyOf(el);
    const entry = {kind: 'row', state: 'ok'};
    if (key) {
      states.set(key, entry);
      setTimeout(() => { if (states.get(key) === entry) states.delete(key); }, FIELD_OK_MS);
    }
    restartFlash(el);
  }

  async function collapse(el) {
    if (!el || !el.isConnected) return;
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) { el.style.display = 'none'; return; }
    const height = el.offsetHeight;
    el.style.height = height + 'px';
    el.style.overflow = 'hidden';
    void el.offsetWidth;
    el.classList.add('rm-collapsing');
    el.style.height = '0px';
    await wait(COLLAPSE_MS);
  }

  /** Действие над строкой (выдать, принять, удалить): строка «в работе»,
   *  затем подсветка, а при opts.collapse — плавно сворачивается. */
  function row(el, work, opts = {}) {
    const promise = asPromise(work);
    if (!el) return promise;
    const key = keyOf(el);
    const entry = {kind: 'row', state: 'pending', shown: false, shownAt: null};
    if (key) states.set(key, entry);
    paintRow(el, entry);
    const timer = setTimeout(() => {
      if (entry.state !== 'pending') return;
      entry.shown = true; entry.shownAt = now();
      paintRow(live(el, key), entry);
    }, DELAY);
    // Сворачивание — до того, как модуль перерисует список: ждём его здесь.
    return promise.then(async value => {
      clearTimeout(timer);
      await wait(holdFor(entry.shownAt, now()));
      const ok = outcome(value) === 'ok';
      entry.state = ok ? 'ok' : 'idle'; entry.shown = false;
      const target = live(el, key);
      if (ok && opts.collapse) { paintRow(target, {state: 'idle'}); await collapse(target); if (key) states.delete(key); }
      else {
        paintRow(target, entry);
        if (key) setTimeout(() => { if (states.get(key) === entry) states.delete(key); }, ok ? FIELD_OK_MS : 0);
      }
      return value;
    }, async error => {
      clearTimeout(timer);
      entry.state = 'idle'; entry.shown = false;
      paintRow(live(el, key), entry);
      if (key && states.get(key) === entry) states.delete(key);
      throw error;
    });
  }

  /* ── Раздел и первая загрузка ──────────────────────────────────────── */
  const sectionCount = new WeakMap();
  /** Перезагрузка раздела: данные на месте, но гаснут (после порога). */
  function section(el, work) {
    const promise = asPromise(work);
    if (!el) return promise;
    sectionCount.set(el, (sectionCount.get(el) || 0) + 1);
    el.setAttribute('aria-busy', 'true');
    const timer = setTimeout(() => { if (sectionCount.get(el)) el.classList.add('rm-reloading'); }, DELAY);
    const done = () => {
      clearTimeout(timer);
      const left = Math.max(0, (sectionCount.get(el) || 1) - 1);
      sectionCount.set(el, left);
      if (!left) { el.classList.remove('rm-reloading'); el.setAttribute('aria-busy', 'false'); }
    };
    promise.then(done, done);
    return promise;
  }

  /** Скелет в форме содержимого: rows строк с полосами разной ширины.
   *  opts.cols — сколько полос в строке, opts.variant — 'row' | 'card' | 'cell'. */
  function skeleton(container, opts = {}) {
    if (!container) return;
    const rows = opts.rows || 4, cols = opts.cols || 3, variant = opts.variant || 'row';
    const widths = skeletonWidths(rows * cols, opts.seed || 0);
    const nodes = [];
    for (let r = 0; r < rows; r += 1) {
      const line = document.createElement('div');
      line.className = 'rm-skel-' + variant;
      for (let c = 0; c < cols; c += 1) {
        const bar = document.createElement('span');
        bar.className = 'rm-skel';
        bar.style.width = (c === cols - 1 && variant === 'row' ? 22 : widths[r * cols + c]) + '%';
        line.append(bar);
      }
      nodes.push(line);
    }
    container.replaceChildren(...nodes);
    container.setAttribute('aria-busy', 'true');
  }

  /* ── Верхняя полоса и учёт запросов ────────────────────────────────── */
  let bar = null, hideTimer = null;
  function ensureBar() {
    if (bar) return bar;
    bar = document.createElement('div');
    bar.className = 'rm-topbar';
    bar.setAttribute('aria-hidden', 'true');
    bar.append(document.createElement('i'));
    document.body.append(bar);
    return bar;
  }
  const tracker = createTracker({
    onShow() {
      clearTimeout(hideTimer);
      const node = ensureBar();
      node.classList.remove('is-done');
      node.classList.add('is-on');
    },
    onHide() {
      const node = ensureBar();
      node.classList.add('is-done');
      clearTimeout(hideTimer);
      hideTimer = setTimeout(() => node.classList.remove('is-on', 'is-done'), 420);
    },
  });

  /** Вести полосу вручную — для ожиданий не через fetch. */
  function track(work) {
    const promise = asPromise(work);
    tracker.start();
    promise.then(() => tracker.end(), () => tracker.end());
    return promise;
  }

  let silentDepth = 0;
  /** Фоновые запросы, начатые внутри fn СИНХРОННО, не зажигают полосу.
   *  Только синхронная часть: иначе на время долгой фоновой загрузки гасли бы
   *  и чужие запросы — нажатие человека осталось бы без полосы. Для запросов
   *  после await — fetch(url, {retroBusy: false}). */
  function silent(fn) {
    silentDepth += 1;
    try { return fn(); } finally { silentDepth -= 1; }
  }

  if (typeof globalThis.fetch === 'function' && !globalThis.fetch.retroBusy) {
    const original = globalThis.fetch;
    const wrapped = function(input, init) {
      const url = typeof input === 'string' || input instanceof URL ? input : input && input.url;
      const promise = original.call(this, input, init);
      if (!silentDepth && tracksRequest(url, init, location.origin)) track(promise);
      return promise;
    };
    wrapped.retroBusy = true;
    globalThis.fetch = wrapped;
  }

  /** Забыть состояния (смена дня / месяца): старые ключи не цепляются к новым строкам.
   *  only — только это состояние, например 'error': новое действие снимает старые ошибки. */
  function clear(prefix = '', only = null) {
    for (const [key, entry] of [...states]) {
      if (!key.startsWith(prefix) || (only && entry.state !== only)) continue;
      states.delete(key);
      const el = find(key);
      if (!el) continue;
      if (entry.kind === 'field' && el instanceof HTMLInputElement) paintField(el, {state: 'idle'});
      else if (entry.kind === 'button') paintButton(el, {state: 'idle'});
      else if (entry.kind === 'row') paintRow(el, {state: 'idle'});
    }
  }

  /* ── Перерисовка: новый узел с тем же ключом получает состояние ───── */
  function reapply(el) {
    const entry = states.get(keyOf(el));
    if (!entry) return;
    if (entry.kind === 'button') paintButton(el, entry);
    else if (entry.kind === 'field' && el instanceof HTMLInputElement) paintField(el, entry);
    else if (entry.kind === 'row') paintRow(el, entry);
  }
  new MutationObserver(records => {
    if (!states.size) return;
    for (const record of records) {
      record.addedNodes.forEach(node => {
        if (node.nodeType !== 1) return;
        if (keyOf(node)) reapply(node);
        node.querySelectorAll('[data-busy-key]').forEach(reapply);
      });
    }
  }).observe(document.documentElement, {childList: true, subtree: true});

  // Пока идёт запись, повторное нажатие и отправка формы не проходят.
  document.addEventListener('click', event => {
    const target = event.target instanceof Element ? event.target.closest('.rm-lock') : null;
    if (target) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  document.addEventListener('submit', event => {
    const form = event.target;
    if (form instanceof HTMLFormElement && form.querySelector('.rm-lock')) {
      event.preventDefault(); event.stopImmediatePropagation();
    }
  }, true);
  // Полоса в DOM с самого начала: узел, появившийся посреди работы, сбивал бы
  // сравнение состояний страницы (и проверки раскладки).
  if (document.body) ensureBar(); else document.addEventListener('DOMContentLoaded', ensureBar, {once: true});
  // iOS Safari включает :active только при слушателе касаний.
  document.addEventListener('touchstart', () => {}, {passive: true});

  globalThis.RetroBusy = {button, field, row, flash, section, skeleton, track, silent, clear, pure};
})();
