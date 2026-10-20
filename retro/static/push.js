/* Уведомления о заказах за Счёт Шефа на этом устройстве (Web Push).
 *
 * Рисует переключатель в каждом [data-push] страницы: «Включить», а когда
 * включено — «Проверить» и «Выключить». Плашка с data-push="until-on" (в блоке
 * «Счёт Шефа») после включения сжимается в тонкую строку (founder-cabinet.css):
 * на компьютере — строка с кнопками, на телефоне её нет совсем — там они в профиле.
 * Устройство подписывается в браузере, адрес и ключи уходят на сервер
 * (/api/founder/push/*), обработчик — /sw.js.
 *
 * Safari:
 *  • разрешение спрашиваем первой строкой обработчика нажатия, до любого
 *    await — иначе Safari считает, что человек ничего не нажимал, и молча
 *    отказывает;
 *  • ключ сервера — массивом байт: строку base64 Safari не принимает;
 *  • на iPhone и iPad (iOS 16.4+) уведомления есть только у значка на экране
 *    «Домой». В обычной вкладке Safari объясняем, как его поставить. */
(() => {
  const boxes = [...document.querySelectorAll('[data-push]')];
  if (!boxes.length) return;

  const ua = navigator.userAgent;
  const ios = /iP(hone|od|ad)/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  const safariMac = !ios && /Safari\//.test(ua) && !/Chrome|Chromium|Edg\//.test(ua);
  const supported = window.isSecureContext && 'serviceWorker' in navigator && 'PushManager' in window
    && 'Notification' in window;

  const TEXT = {
    off: 'Каждый заказ за Счёт Шефа — уведомление на это устройство.',
    on: 'Включены на этом устройстве.',
    denied: safariMac
      ? 'Уведомления для сайта запрещены: Safari → Настройки → Веб-сайты → Уведомления.'
      : ios ? 'Уведомления запрещены: Настройки iPhone → Уведомления → Retro Milliy.'
        : 'Уведомления для этого сайта запрещены в настройках браузера.',
    iosTab: 'На iPhone уведомления приходят только из значка: «Поделиться» → «На экран „Домой“», '
      + 'откройте Retro Milliy со значка и включите здесь.',
    iosOld: 'Для уведомлений на iPhone нужна iOS 16.4 или новее.',
    unsupported: 'Этот браузер не умеет получать уведомления.',
    sent: 'Проверочное уведомление отправлено.',
  };

  let registration = null;
  let key = null;
  let subscription = null;

  function decode(text) {
    const base64 = (text + '='.repeat((4 - text.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/');
    return Uint8Array.from(atob(base64), char => char.charCodeAt(0));
  }

  function sameKey(current) {
    const used = current && current.options && current.options.applicationServerKey;
    if (!used || !key) return true;  // старый браузер не сообщает ключ — верим подписке
    const a = new Uint8Array(used), b = decode(key);
    return a.length === b.length && a.every((byte, index) => byte === b[index]);
  }

  async function send(path, body) {
    const response = await fetch(path, {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Сервер не ответил.');
    return data;
  }

  function button(text, handler, primary) {
    const node = document.createElement('button');
    node.type = 'button';
    node.className = 'push-button' + (primary ? ' is-primary' : '');
    node.textContent = text;
    node.addEventListener('click', handler);
    return node;
  }

  function render(state, note) {
    boxes.forEach(box => {
      box.hidden = false;
      box.dataset.state = state;
      const status = document.createElement('span');
      status.className = 'push-status';
      status.setAttribute('role', 'status');
      status.textContent = note || TEXT[state] || '';
      const actions = document.createElement('span');
      actions.className = 'push-actions';
      if (state === 'off') actions.append(button('Включить', enable, true));
      if (state === 'on') actions.append(button('Проверить', test), button('Выключить', disable));
      const title = document.createElement('strong');
      // Строка «включены» стоит в самом блоке Счёта Шефа — хватает «Уведомления».
      title.textContent = state === 'on' && box.dataset.push === 'until-on' ? 'Уведомления' : 'Уведомления о Счёте Шефа';
      const text = document.createElement('span');
      text.className = 'push-text';
      text.append(title, status);
      box.replaceChildren(text, actions);
    });
  }

  function busy(on) {
    boxes.forEach(box => box.querySelectorAll('button').forEach(node => { node.disabled = on; }));
  }

  function enable() {
    // Первой строкой, синхронно с нажатием: так требует Safari.
    const asked = Notification.permission === 'granted' ? Promise.resolve('granted') : Notification.requestPermission();
    busy(true);
    asked.then(async permission => {
      if (permission !== 'granted') { render(permission === 'denied' ? 'denied' : 'off'); return; }
      if (subscription && !sameKey(subscription)) { await subscription.unsubscribe(); subscription = null; }
      subscription = subscription
        || await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: decode(key)});
      await send('/api/founder/push/subscribe', subscription.toJSON());
      render('on');
    }).catch(error => render('off', 'Не удалось включить: ' + (error && error.message ? error.message : 'ошибка браузера')));
  }

  async function disable() {
    busy(true);
    try {
      if (subscription) {
        const endpoint = subscription.endpoint;
        await subscription.unsubscribe();
        await send('/api/founder/push/unsubscribe', {endpoint});
      }
      subscription = null;
      render('off');
    } catch (error) {
      render('on', 'Не удалось выключить: ' + error.message);
    }
  }

  async function test() {
    busy(true);
    try {
      await send('/api/founder/push/test', {endpoint: subscription.endpoint});
      render('on', TEXT.sent);
    } catch (error) {
      if (/устарела|не подписано/.test(error.message)) { subscription = null; render('off', error.message); return; }
      render('on', error.message);
    }
  }

  async function start() {
    if (!supported) {
      if (ios && !standalone) render('iosTab');
      else if (ios) render('iosOld');
      else render('unsupported');
      return;
    }
    const response = await fetch('/api/founder/push', {credentials: 'same-origin'}).catch(() => null);
    if (!response || !response.ok) return;  // нет доступа к кабинету — переключатель не показываем
    key = (await response.json()).key;
    registration = await navigator.serviceWorker.register('/sw.js', {scope: '/'});
    subscription = await registration.pushManager.getSubscription();
    if (Notification.permission === 'denied') { render('denied'); return; }
    if (subscription && Notification.permission === 'granted' && sameKey(subscription)) {
      // Напоминаем серверу о подписке: он мог её потерять, а браузер — сменить адрес.
      send('/api/founder/push/subscribe', subscription.toJSON()).catch(() => {});
      render('on');
      return;
    }
    render('off');
  }

  start().catch(() => render('unsupported'));
})();
