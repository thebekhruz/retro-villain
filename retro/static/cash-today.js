/* «Баланс на сегодня» на «Зарплате · день» и «Зарплате · месяц» (T-470).

   Остаток кассы на конец сегодняшнего дня — тот же, что в «Финансах дня»:
   страницы его не считают, а берут /api/accountant/cash-today. Карточка
   [data-cash-today] стоит первой в ряду итогов. Когда при прокрутке длинной
   ведомости она уходит вверх, внизу справа остаётся плашка с той же суммой.
   Выплата из ведомости уменьшает кассу — страница зовёт refresh() после записи.
   Плашку можно перетащить мышью или пальцем (T-475): место запоминается в этом
   браузере, двойной клик возвращает её в угол. */
(() => {
  const number = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
  const NO_DATA = 'Нет данных из Финансов дня';
  const NOTE = 'из «Финансов дня»';
  let value, sequence = 0, timer = null, pill = null, away = false;

  // «12 345 678 сум», «0 сум»; нет данных — «—».
  const text = amount => amount === null ? '—' : (amount < 0 ? '−' : '') + number.format(Math.abs(amount)) + ' сум';

  function paint() {
    if (value === undefined) return;
    document.querySelectorAll('[data-cash-today]').forEach(card => {
      card.querySelector('[data-cash-value]').textContent = text(value);
      card.querySelector('[data-cash-note]').textContent = value === null ? NO_DATA : NOTE;
      card.classList.toggle('is-empty', value === null);
      card.classList.toggle('is-negative', value !== null && value < 0);
    });
    if (pill) {
      pill.querySelector('strong').textContent = text(value);
      pill.classList.toggle('is-negative', value !== null && value < 0);
      pill.hidden = !away;
      place();
    }
  }

  async function load() {
    const token = ++sequence;
    let next = null;
    try {
      const response = await fetch('/api/accountant/cash-today', {cache: 'no-store'});
      const data = await response.json();
      if (response.ok && data.cash_balance !== null && data.cash_balance !== undefined) {
        const amount = Number(data.cash_balance);
        if (Number.isFinite(amount)) next = amount;
      }
    } catch { /* сеть или сервер — показываем «—» с подсказкой */ }
    if (token !== sequence) return;
    value = next;
    paint();
  }
  // Подряд несколько выплат — один запрос после последней.
  function refresh() { clearTimeout(timer); timer = setTimeout(load, 300); }

  /* Плашка при прокрутке: карточка ушла выше экрана — показываем, вернулась — прячем. */
  function watch() {
    const card = document.querySelector('[data-cash-today]');
    if (!card || !('IntersectionObserver' in window)) return;
    pill = document.createElement('div');
    pill.className = 'cash-pill'; pill.hidden = true;
    pill.setAttribute('aria-hidden', 'true');
    pill.append(document.createElement('span'), document.createElement('strong'));
    pill.firstChild.textContent = 'Баланс на сегодня';
    pill.title = 'Перетащите, чтобы подвинуть. Двойной клик — обратно в угол.';
    document.body.append(pill);
    drag(pill);
    new IntersectionObserver(([entry]) => {
      // Скрытая карточка (ведомость ещё грузится) — не «ушла вверх».
      away = !entry.isIntersecting && entry.boundingClientRect.bottom < 0 && entry.boundingClientRect.height > 0;
      paint();
      if (pill.hidden !== !away) pill.hidden = !away;
      place();
    }).observe(card);
  }

  /* ── Перетаскивание (T-475) ──────────────────────────────────────────────
     Место — доля ширины и высоты окна: на другом размере окна плашка остаётся
     примерно там же и не выходит за край. */
  const SPOT = 'retro.cashPill.spot';
  let spot = null;
  try { const saved = JSON.parse(localStorage.getItem(SPOT) || 'null'); if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) spot = saved; } catch { /* без памяти — угол */ }
  const clamp = (value, max) => Math.min(Math.max(8, value), Math.max(8, max - 8));
  function move(left, top) {
    pill.style.left = clamp(left, innerWidth - pill.offsetWidth) + 'px';
    pill.style.top = clamp(top, innerHeight - pill.offsetHeight) + 'px';
    pill.style.right = pill.style.bottom = 'auto';
  }
  function place() {
    if (!pill || pill.hidden) return;
    if (!spot) { pill.style.left = pill.style.top = pill.style.right = pill.style.bottom = ''; return; }
    move(spot.x * innerWidth, spot.y * innerHeight);
  }
  function drag(el) {
    let from = null;
    el.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      const box = el.getBoundingClientRect();
      from = {dx: event.clientX - box.left, dy: event.clientY - box.top};
      el.setPointerCapture(event.pointerId);
      el.classList.add('is-dragging');
      event.preventDefault();
    });
    el.addEventListener('pointermove', event => { if (from) move(event.clientX - from.dx, event.clientY - from.dy); });
    const drop = () => {
      if (!from) return;
      from = null;
      el.classList.remove('is-dragging');
      const box = el.getBoundingClientRect();
      spot = {x: box.left / innerWidth, y: box.top / innerHeight};
      try { localStorage.setItem(SPOT, JSON.stringify(spot)); } catch { /* не запомнили — не страшно */ }
    };
    el.addEventListener('pointerup', drop);
    el.addEventListener('pointercancel', drop);
    el.addEventListener('dblclick', () => {
      spot = null;
      try { localStorage.removeItem(SPOT); } catch { /* ничего */ }
      place();
    });
    window.addEventListener('resize', place);
  }

  function start() {
    watch();
    load();
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
    // «Финансы дня» могли поменять в другой вкладке: раз в минуту, пока окно на экране.
    setInterval(() => { if (!document.hidden) load(); }, 60000);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
  globalThis.RetroCashToday = {refresh, text};
})();
