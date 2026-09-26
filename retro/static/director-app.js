/* Директор · телефон (ui_solid1 6a).
 *
 * Четыре вкладки: «Сегодня», «Меню», AI и «Команда». На экранах только
 * главное, всё остальное — вопросом в чат. Данные живые: касса — из iiko,
 * отчёт бухгалтера — его же день из «Финансов дня», команда — общий реестр
 * сотрудников, поэтому правка ставки сразу видна бухгалтеру. */
(() => {
  const $ = id => document.getElementById(id);
  const logic = globalThis.DirectorLogic;
  const checks = globalThis.AccountantLogic;
  const money = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 0});
  const sum = value => money.format(Math.round(Number(value) || 0));
  /** «42,5 млн» — на телефоне полные суммы не помещаются в плитку. */
  function short(value) {
    const number = Number(value) || 0;
    if (Math.abs(number) >= 1e6) return String(Math.round(number / 1e5) / 10).replace('.', ',') + ' млн';
    return money.format(Math.round(number));
  }
  const plural = logic.plural;
  const WEEKDAYS = ['воскресенье', 'понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота'];

  const view = {
    tab: 'home', today: null, menuDays: 7, venue: 'all', slice: 'top', query: '',
    reports: {}, loading: {}, team: null, teamDay: 'today', teamFilter: 'all',
    editing: null, editorType: 'shift', manual: false,
  };
  let chat = null;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  async function request(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const error = new Error(body.detail || 'Не удалось получить данные.');
      error.status = response.status;
      throw error;
    }
    return response.status === 204 ? null : response.json();
  }

  function say(text, error) {
    const state = $('state');
    state.hidden = !text;
    state.textContent = text || '';
    state.classList.toggle('is-error', Boolean(error));
  }

  function shiftDay(iso, days) {
    const value = new Date(iso + 'T12:00:00Z');
    value.setUTCDate(value.getUTCDate() + days);
    return value.toISOString().slice(0, 10);
  }

  // ── Вкладки ──────────────────────────────────────────────────────────
  const TITLES = {home: 'Салом, директор', menu: 'Меню', ai: 'AI-помощник', team: 'Команда'};

  function openTab(tab) {
    view.tab = tab;
    document.querySelectorAll('.dir-tabs .rm-tab').forEach(button => {
      const active = button.dataset.tab === tab;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-selected', String(active));
    });
    ['home', 'menu', 'ai', 'team'].forEach(name => { $('tab-' + name).hidden = name !== tab; });
    $('screen-title').textContent = TITLES[tab];
    if (tab === 'menu') loadMenu();
    if (tab === 'team') { loadTeam(); loadReport(7); }
    if (tab === 'ai') chat.load();
    window.scrollTo({top: 0});
  }

  function ask(question) {
    openTab('ai');
    chat.ask(question);
  }

  // ── Отчёты iiko ──────────────────────────────────────────────────────
  /** Отчёт директора за последние N закрытых дней. Держим в памяти: 3, 7 и 30
   *  дней нужны сразу нескольким блокам, и iiko не должен считать их дважды. */
  function loadReport(days) {
    if (view.reports[days]) return Promise.resolve(view.reports[days]);
    if (view.loading[days]) return view.loading[days];
    const range = globalThis.RetroPeriod.presetRange(view.today, String(days));
    view.loading[days] = request('/api/director/report?start=' + range.start + '&end=' + range.end)
      .then(snapshot => { view.reports[days] = snapshot; return snapshot; })
      .finally(() => { delete view.loading[days]; });
    return view.loading[days];
  }

  // ── Сегодня ──────────────────────────────────────────────────────────
  function renderTips(snapshot) {
    const list = $('tips');
    list.replaceChildren();
    const tips = logic.dayTips(snapshot);
    const weekday = WEEKDAYS[new Date(view.today + 'T12:00:00Z').getUTCDay()];
    const texts = tips.map(tip => {
      if (tip.kind === 'promo') return 'Сегодня ' + weekday + ': продвигайте «' + tip.dish + '» — маржа ' + Math.round(tip.margin) + '%, на нём Retro зарабатывает больше всего.';
      if (tip.kind === 'trap') return '«' + tip.dish + '» хорошо продаётся, но маржа ' + Math.round(tip.margin) + '% — не ставьте его в акции.';
      return '«' + tip.dish + '» почти не заказывают: ' + tip.quantity + ' шт за неделю. Подумайте, убрать ли из меню.';
    });
    if (!texts.length) texts.push('За неделю в iiko нет продаж — советовать не из чего.');
    texts.forEach(text => list.append(node('li', '', text)));
  }

  async function loadHome() {
    loadReport(7).then(renderTips).catch(error => {
      $('tips').replaceChildren(node('li', 'is-muted', 'Меню за неделю недоступно: ' + error.message));
    });
    request('/api/director/cash-today').then(data => {
      $('kassa').textContent = short(data.retro);
      const orders = data.retro_checks;
      $('kassa-sub').textContent = 'Retro · ' + orders + ' ' + plural(orders, 'чек', 'чека', 'чеков') +
        ' · Oxbridge ' + short(data.school);
    }).catch(error => {
      $('kassa').textContent = '—';
      $('kassa-sub').textContent = error.message;
    });
    loadAccounting();
    loadTeam();
  }

  function issueText(item, day) {
    const parts = [];
    if (item.sub.count) parts.push(item.sub.count + ' ' + plural(item.sub.count, 'строка', 'строки', 'строк'));
    if (item.sub.amount !== undefined) parts.push(sum(item.sub.amount) + ' сум');
    parts.push(day === view.today ? 'сегодня' : 'вчера');
    return parts.join(' · ');
  }

  async function loadAccounting() {
    const yesterday = shiftDay(view.today, -1);
    try {
      const [today, previous] = await Promise.all([
        request('/api/director/accounting/day?date=' + view.today),
        request('/api/director/accounting/day?date=' + yesterday),
      ]);
      // Пока кассир не передал сегодняшнюю кассу, остаток дня не посчитан —
      // показываем вчерашний конец дня и прямо об этом пишем.
      const known = today.ledger.cash_balance !== null;
      const balance = known ? today.ledger.cash_balance : previous.ledger.cash_balance;
      $('cash').textContent = balance === null ? '—' : short(balance);
      $('cash').classList.toggle('is-negative', balance !== null && Number(balance) < 0);
      $('cash-sub').textContent = balance === null ? 'нет начального остатка' : known ? 'на конец дня' : 'на конец вчера · касса ещё не передана';
      // «Касса не передана» за сегодня — не ошибка: смена ещё идёт.
      // Отставание по дивидендам считается на неделю — берём его только сегодняшним.
      const items = checks.dayChecks(previous).filter(item => item.text !== 'Отстаём от недельных дивидендов')
        .map(item => ({...item, day: yesterday}))
        .concat(checks.dayChecks(today).filter(item => item.text !== 'Касса не передана')
          .map(item => ({...item, day: view.today})));
      const bad = items.filter(item => item.level === 'bad').length;
      const badge = $('err-badge');
      badge.textContent = items.length ? items.length + ' ' + plural(items.length, 'замечание', 'замечания', 'замечаний') : 'Ошибок нет';
      badge.className = 'dir-badge ' + (bad ? 'is-bad' : items.length ? '' : 'is-ok');
      const list = $('err-list');
      list.replaceChildren();
      items.sort((a, b) => (a.level === 'bad' ? 0 : 1) - (b.level === 'bad' ? 0 : 1)).slice(0, 3).forEach(item => {
        const row = node('div');
        const text = node('div');
        text.append(node('strong', '', item.text), node('small', '', issueText(item, item.day)));
        row.append(node('i', item.level === 'bad' ? 'is-bad' : ''), text);
        list.append(row);
      });
      $('ask-err').hidden = !items.length;
      $('ask-err').textContent = (items.length > 3 ? 'и ещё ' + (items.length - 3) + ' · ' : '') + 'Что важнее всего? · AI →';
      view.accountingIssues = items;
    } catch (error) {
      $('cash').textContent = '—';
      $('err-badge').textContent = 'Нет данных';
      $('err-list').replaceChildren(node('p', 'dir-note', error.message));
    }
  }

  // ── Меню ─────────────────────────────────────────────────────────────
  const VIEW_TITLES = {top: 'Продаются лучше всего', weak: 'Продаются хуже всего',
    lowm: 'Популярные, но маржа ниже ' + logic.LOW_MARGIN + '%', notb: 'Хиты Retro и Oxbridge, которых нет в Бехрузе'};

  function marginClass(margin) {
    if (margin === null) return '';
    return Math.round(margin) < logic.LOW_MARGIN ? 'm-low' : margin < 60 ? 'm-mid' : 'm-ok';
  }

  function dishButton(row, index, max) {
    const button = node('button', 'dir-dish');
    button.type = 'button';
    const line = node('span', 'dir-dish-line');
    const name = node('span', 'dir-dish-name');
    const meta = node('small');
    meta.append(document.createTextNode(short(row.revenue) + ' сум · '));
    meta.append(node('span', marginClass(row.margin), row.margin === null ? 'маржа —' : 'маржа ' + Math.round(row.margin) + '%'));
    name.append(node('strong', '', row.name), meta);
    const qty = node('span', 'dir-dish-qty');
    qty.append(node('strong', 'rm-num', money.format(Math.round(row.quantity)) + ' шт'));
    const change = logic.trend(row.name, view.slice === 'notb' ? 'all' : view.venue, view.reports[3], view.reports[30]);
    if (change !== null && Number.isFinite(change) && Math.abs(change) >= 0.05) {
      qty.append(node('small', change > 0 ? 'is-up' : 'is-down', (change > 0 ? '↑ ' : '↓ ') + Math.abs(Math.round(change * 100)) + '%'));
    }
    line.append(node('span', 'dir-rank', String(index + 1)), name, qty);
    const bar = node('span', 'dir-dish-bar');
    const fill = node('span');
    fill.style.width = Math.max(4, row.quantity / max * 100) + '%';
    bar.append(fill);
    button.append(line, bar);
    button.addEventListener('click', () => ask('Расскажи про «' + row.name + '»: продажи по заведениям, маржа и что с ним делать?'));
    return button;
  }

  function renderMenu() {
    const snapshot = view.reports[view.menuDays];
    const list = $('menu-list');
    list.replaceChildren();
    $('view-title').textContent = VIEW_TITLES[view.slice];
    $('view-note').textContent = view.slice === 'notb' ? 'Сумма Retro + Oxbridge · фильтр заведения не действует' : '';
    if (!snapshot) return;
    $('menu-period').textContent = 'iiko · ' + logic.periodLabel(snapshot.period_start, snapshot.period_end);
    if (view.query.trim()) return renderSearch(snapshot);
    $('menu-found').hidden = true;
    $('menu-browse').hidden = false;
    const rows = logic.menuSlice(snapshot, view.venue, view.slice, 5);
    if (!rows.length) { list.append(node('p', 'dir-empty', 'За период в этом срезе нет блюд.')); return; }
    const max = Math.max(1, ...rows.map(row => row.quantity));
    rows.forEach((row, index) => list.append(dishButton(row, index, max)));
  }

  function renderSearch(snapshot) {
    $('menu-browse').hidden = true;
    const target = $('menu-found');
    target.hidden = false;
    target.replaceChildren();
    const found = logic.search(logic.rank(snapshot.item_metrics.all || {}, 'quantity'), view.query).slice(0, 8);
    if (!found.length) { target.append(node('p', 'dir-empty', 'Такого блюда в продажах за период нет.')); return; }
    found.forEach(row => {
      const card = node('article', 'rm-phone-card dir-found-card');
      const head = node('div', 'dir-found-head');
      const title = node('div');
      title.append(node('strong', '', row.name), node('small', '', 'маржа ' + (row.margin === null ? '—' : Math.round(row.margin) + '%') + ' · ' + short(row.revenue) + ' сум'));
      head.append(title, node('b', 'rm-num', money.format(Math.round(row.quantity)) + ' шт'));
      const split = node('div', 'dir-split');
      [['retro', 'Retro'], ['oxbridge', 'Oxbridge'], ['banquet', 'Бехруз']].forEach(([group, label]) => {
        const metric = (snapshot.item_metrics[group] || {})[row.name];
        const quantity = metric ? logic.amount(metric.quantity) : 0;
        const cell = node('div');
        cell.append(node('span', '', label), node('strong', quantity ? 'rm-num' : 'rm-num is-zero', money.format(Math.round(quantity))));
        split.append(cell);
      });
      const more = node('button', 'dir-link', '✦ Спросить AI про это блюдо');
      more.type = 'button';
      more.addEventListener('click', () => ask('Расскажи про «' + row.name + '»: продажи по заведениям, маржа и что с ним делать?'));
      card.append(head, split, more);
      target.append(card);
    });
  }

  async function loadMenu() {
    const days = view.menuDays;
    if (!view.reports[days]) {
      $('menu-list').replaceChildren(node('p', 'dir-empty', 'Собираем продажи из iiko…'));
      $('menu-period').textContent = '';
    }
    try {
      await loadReport(days);
      if (days === view.menuDays) renderMenu();
      // Тренд — это ещё два отчёта. Грузим их вслед, чтобы список не ждал.
      Promise.all([loadReport(3), loadReport(30)]).then(renderMenu).catch(() => {});
    } catch (error) {
      if (days === view.menuDays) $('menu-list').replaceChildren(node('p', 'dir-empty', error.message));
    }
  }

  // ── Команда ──────────────────────────────────────────────────────────
  const STATUS = {on_time: 'Вовремя', late: 'Опоздал', missing: 'Не пришёл', unlinked: 'Без Hikvision',
    unavailable: 'Нет данных'};

  function hm(iso) {
    if (!iso) return '';
    return new Date(iso).toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Tashkent'});
  }

  async function loadTeam() {
    const day = view.teamDay === 'today' ? view.today : shiftDay(view.today, -1);
    try {
      const team = await request('/api/director/team?date=' + day);
      if (view.teamDay === 'today') {
        $('late-n').textContent = team.counts.late;
        $('miss-n').textContent = team.counts.missing;
        $('nohik-n').textContent = team.counts.no_hikvision;
        const health = team.attendance && team.attendance.status;
        $('attendance-note').textContent = health === 'ok' ? '' :
          health === 'not_configured' ? 'Hikvision ресторана не подключён: входы не видны, опоздания не считаются.' :
          'Hikvision сейчас без свежих данных: отсутствие входа ещё не значит прогул.';
      }
      view.team = team;
      const roles = $('team-roles');
      roles.replaceChildren(...(team.roles || []).map(role => { const option = node('option'); option.value = role; return option; }));
      renderTeam();
    } catch (error) {
      $('team-list').replaceChildren(node('p', 'dir-empty', error.message));
    }
  }

  function renderTeam() {
    const rows = logic.teamRows(view.team);
    const filters = [['all', 'Все'], ['shift', 'Сменные'], ['monthly', 'Оклад'], ['late', 'Опоздали'],
      ['missing', 'Не пришли'], ['nohik', 'Без Hikvision']];
    const bar = $('team-filters');
    bar.replaceChildren(...filters.map(([key, label]) => {
      const button = node('button', key === view.teamFilter ? 'is-active' : '',
        label + ' · ' + rows.filter(row => logic.teamMatches(row, key)).length);
      button.type = 'button';
      button.addEventListener('click', () => { view.teamFilter = key; renderTeam(); });
      return button;
    }));
    const list = $('team-list');
    list.replaceChildren();
    const shown = rows.filter(row => logic.teamMatches(row, view.teamFilter));
    if (!shown.length) { list.append(node('p', 'dir-empty', 'Никого в этом списке.')); return; }
    shown.forEach(row => {
      const button = node('button', 'dir-person');
      button.type = 'button';
      const main = node('span', 'dir-person-main');
      const name = node('span', 'dir-person-name');
      name.append(node('strong', '', row.name));
      if (row.noHik) name.append(node('span', 'rm-flag-nohik', row.manual ? '⊘ вручную' : '⊘ Hik'));
      main.append(name, node('small', '', row.role + ' · ' + (row.hasRate ? sum(row.rate) + (row.type === 'monthly' ? ' в месяц' : ' за смену') : 'нет ставки')));
      if (row.type === 'monthly') {
        const progress = node('span', 'rm-bar');
        const fill = node('span');
        fill.style.width = Math.min(100, row.rate ? row.paid / row.rate * 100 : 0) + '%';
        progress.append(fill);
        const paid = node('span', 'dir-paid');
        paid.append(node('span', '', 'выдано ' + short(row.paid)), node('span', row.rest > 0 ? 'is-rest' : '', row.rest > 0 ? 'осталось ' + short(row.rest) : 'выдано полностью'));
        main.append(progress, paid);
      }
      let pill = STATUS[row.status] || 'Оклад';
      if (row.status === 'late' || row.status === 'on_time') pill += ' ' + hm(row.firstEntry);
      if (row.manual) pill = 'Вручную';
      const status = node('span', 'dir-pill ' + (row.manual ? 'manual' : row.status), pill);
      button.append(main, status, node('span', 'dir-chevron', '›'));
      button.addEventListener('click', () => openEditor(row));
      list.append(button);
    });
  }

  function renderWaiters(snapshot) {
    const rows = logic.waiters(snapshot.waiter_metrics || {}).filter(row => row.revenue > 0).slice(0, 6);
    const target = $('waiters');
    target.replaceChildren();
    if (!rows.length) { target.append(node('p', 'dir-empty', 'iiko не вернул продажи официантов за неделю.')); return; }
    const max = Math.max(1, ...rows.map(row => row.revenue));
    rows.forEach((row, index) => {
      const item = node('div');
      const line = node('div', 'dir-waiter-line');
      line.append(node('span', '', String(index + 1)), node('strong', '', row.name), node('b', 'rm-num', short(row.revenue)));
      const bar = node('div', 'rm-bar');
      const fill = node('span');
      fill.style.width = Math.max(4, row.revenue / max * 100) + '%';
      if (logic.lowMargin(row)) fill.style.background = '#c9a15a';
      bar.append(fill);
      item.append(line, bar, node('small', '', 'маржа ' + (row.margin === null ? '—' : Math.round(row.margin) + '%') + ' · ' + money.format(Math.round(row.quantity)) + ' позиций'));
      target.append(item);
    });
  }

  // ── Редактор сотрудника ──────────────────────────────────────────────
  function setEditorType(type) {
    view.editorType = type;
    $('editor-type').querySelectorAll('button').forEach(button => button.classList.toggle('is-active', button.dataset.type === type));
    $('editor-amount-label').textContent = type === 'monthly' ? 'Оклад в месяц, сум' : 'Ставка за смену, сум';
    $('editor-manual').hidden = type === 'monthly';
  }

  function setManual(on) {
    view.manual = on;
    $('editor-manual').setAttribute('aria-pressed', String(on));
  }

  function openEditor(row) {
    view.editing = row || null;
    $('editor-title').textContent = row ? row.name : 'Новый сотрудник';
    $('editor-type').hidden = Boolean(row);
    setEditorType(row ? row.type : 'shift');
    $('editor-name').value = row ? row.name : '';
    $('editor-role').value = row ? row.role : '';
    $('editor-amount').value = row && row.hasRate ? money.format(row.rate) : '';
    setManual(Boolean(row && row.manual));
    $('editor-error').textContent = '';
    $('editor-delete-zone').hidden = !row;
    $('editor-delete').hidden = false;
    $('editor-confirm').hidden = true;
    $('team-editor').hidden = false;
    $('team-editor').scrollIntoView({block: 'start', behavior: 'smooth'});
    $('editor-name').focus({preventScroll: true});
  }

  function closeEditor() {
    view.editing = null;
    $('team-editor').hidden = true;
  }

  function toast(text, error) {
    const target = $('team-toast');
    target.textContent = text;
    target.classList.toggle('is-error', Boolean(error));
    target.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { target.hidden = true; }, 6000);
  }

  async function saveEditor(event) {
    event.preventDefault();
    const name = $('editor-name').value.trim(), role = $('editor-role').value.trim();
    const amount = $('editor-amount').value.replace(/\D/g, '');
    if (!name || !role || !Number(amount)) { $('editor-error').textContent = 'Укажите имя, должность и сумму.'; return; }
    const row = view.editing;
    const body = {name, role, amount};
    if (view.editorType === 'shift') body.manual_attendance = view.manual;
    const url = row ? '/api/director/team/' + row.type + '/' + row.id : '/api/director/team';
    if (!row) body.type = view.editorType;
    $('editor-save').disabled = true;
    try {
      await request(url, {method: row ? 'PATCH' : 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      closeEditor();
      toast(row ? 'Сохранено: ' + name + ' · ' + sum(amount) + ' сум. Бухгалтер видит новую ставку.'
        : name + ' в реестре. Видно у бухгалтера в «Сотрудниках» и «Финансах дня».');
      await loadTeam();
    } catch (error) {
      $('editor-error').textContent = error.message;
    } finally {
      $('editor-save').disabled = false;
    }
  }

  async function deleteEditor() {
    const row = view.editing;
    if (!row) return;
    $('editor-delete-yes').disabled = true;
    try {
      await request('/api/director/team/' + row.type + '/' + row.id, {method: 'DELETE'});
      closeEditor();
      toast(row.name + ' удалён(а) из реестра.');
      await loadTeam();
    } catch (error) {
      $('editor-error').textContent = error.message;
    } finally {
      $('editor-delete-yes').disabled = false;
    }
  }

  // ── Привязка событий ─────────────────────────────────────────────────
  function bind() {
    document.querySelectorAll('.dir-tabs .rm-tab').forEach(button =>
      button.addEventListener('click', () => openTab(button.dataset.tab)));
    $('ask-tip').addEventListener('click', () => ask('Что лучше всего сделать сегодня, чтобы увеличить продажи? Учитывай данные по всем заведениям.'));
    $('ask-err').addEventListener('click', () => ask('Какие ошибки сегодня и вчера у бухгалтера и что из них самое важное?'));
    $('ask-waiters').addEventListener('click', () => ask('Почему у официантов такая разница в продажах за неделю?'));
    document.querySelectorAll('[data-team-filter]').forEach(button => button.addEventListener('click', () => {
      view.teamFilter = button.dataset.teamFilter;
      view.teamDay = 'today';
      openTab('team');
    }));
    $('menu-query').addEventListener('input', event => { view.query = event.target.value; renderMenu(); });
    document.querySelectorAll('#tab-menu [data-days]').forEach(button => button.addEventListener('click', () => {
      view.menuDays = Number(button.dataset.days);
      document.querySelectorAll('#tab-menu [data-days]').forEach(other => other.classList.toggle('is-active', other === button));
      loadMenu();
    }));
    document.querySelectorAll('[data-venue]').forEach(button => button.addEventListener('click', () => {
      view.venue = button.dataset.venue;
      document.querySelectorAll('[data-venue]').forEach(other => other.classList.toggle('is-active', other === button));
      renderMenu();
    }));
    document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => {
      view.slice = button.dataset.view;
      document.querySelectorAll('[data-view]').forEach(other => other.classList.toggle('is-active', other === button));
      renderMenu();
    }));
    $('team-days').querySelectorAll('button').forEach(button => button.addEventListener('click', () => {
      view.teamDay = button.dataset.day;
      $('team-days').querySelectorAll('button').forEach(other => other.classList.toggle('is-active', other === button));
      loadTeam();
    }));
    $('team-add').addEventListener('click', () => openEditor(null));
    $('editor-close').addEventListener('click', closeEditor);
    $('editor-type').querySelectorAll('button').forEach(button => button.addEventListener('click', () => setEditorType(button.dataset.type)));
    $('editor-manual').addEventListener('click', () => setManual(!view.manual));
    $('editor-amount').addEventListener('input', event => {
      const digits = event.target.value.replace(/\D/g, '');
      event.target.value = digits ? money.format(Number(digits)) : '';
    });
    $('team-editor').addEventListener('submit', saveEditor);
    $('editor-delete').addEventListener('click', () => { $('editor-delete').hidden = true; $('editor-confirm').hidden = false; });
    $('editor-keep').addEventListener('click', () => { $('editor-delete').hidden = false; $('editor-confirm').hidden = true; });
    $('editor-delete-yes').addEventListener('click', deleteEditor);
  }

  (async function start() {
    view.today = new Date().toISOString().slice(0, 10);
    try {
      const config = await globalThis.RetroConfig;
      if (config && config.today) view.today = config.today;
    } catch (error) {
      say(error.message, true);
    }
    const date = new Date(view.today + 'T12:00:00Z');
    $('today-label').textContent = date.toLocaleDateString('ru-RU', {weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC'})
      .replace(/^./, letter => letter.toUpperCase());
    chat = globalThis.RetroChat.mount({
      messages: $('chat-messages'), form: $('chat-form'), input: $('chat-input'),
      status: $('chat-status'), prompts: $('chat-prompts'), endpoint: '/api/director/chat',
    });
    bind();
    loadHome();
    loadReport(7).then(renderWaiters).catch(error => {
      $('waiters').replaceChildren(node('p', 'dir-empty', error.message));
    });
  })();
})();
