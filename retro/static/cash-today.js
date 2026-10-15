/* «Баланс на сегодня» на «Зарплате · день» и «Зарплате · месяц» (T-470).

   Остаток кассы на конец сегодняшнего дня — тот же, что в «Финансах дня»:
   страницы его не считают, а берут /api/accountant/cash-today. Карточка
   [data-cash-today] стоит первой в ряду итогов. Когда при прокрутке длинной
   ведомости она уходит вверх, внизу справа остаётся плашка с той же суммой.
   Выплата из ведомости уменьшает кассу — страница зовёт refresh() после записи. */
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
    document.body.append(pill);
    new IntersectionObserver(([entry]) => {
      // Скрытая карточка (ведомость ещё грузится) — не «ушла вверх».
      away = !entry.isIntersecting && entry.boundingClientRect.bottom < 0 && entry.boundingClientRect.height > 0;
      paint();
      if (pill.hidden !== !away) pill.hidden = !away;
    }).observe(card);
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
