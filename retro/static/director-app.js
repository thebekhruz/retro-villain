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
    // Создание сотрудника идёт через RetroFinancialWrite: у новой записи есть
    // ставка, и повтор после потерянного ответа иначе завёл бы второго
    // человека. Чтение, правка и удаление идут обычным fetch — сторож на
    // сервере смотрит только POST, а PATCH и DELETE идемпотентны по смыслу.
    const send = (options && options.method === 'POST' && globalThis.RetroFinancialWrite)
      ? globalThis.RetroFinancialWrite : fetch;
    const response = await send(url, options);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const error = new Error(body.detail || 'Не удалось получить данные.');
      error.status = response.status;
      throw error;
    }
    return response.status === 204 ? null : response.json();
  }

  // ── Отклик и ожидание (busy.js, T-393) ───────────────────────────────
  const Busy = globalThis.RetroBusy;
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  /** Кнопка / раздел «в работе» — если busy.js на странице; иначе просто ждём. */
  const busyButton = (button, work, opts) => (Busy ? Busy.button(button, work, opts) : Promise.resolve(work));
  const busySection = (element, work) => (Busy ? Busy.section(element, work) : Promise.resolve(work));

  /** Скелет списка: строки в форме будущих (точка/номер, две строки текста, число справа). */
  function skeletonList(target, rows, end) {
    const list = node('div', 'dir-skel-list');
    list.setAttribute('aria-hidden', 'true');
    for (let index = 0; index < rows; index += 1) {
      const line = node('div', 'dir-skel-line');
      const text = node('span');
      text.append(node('span', 'rm-skel'), node('span', 'rm-skel'));
      line.append(node('span', 'rm-skel is-dot'), text);
      if (end) line.append(node('span', 'rm-skel is-end'));
      list.append(line);
    }
    target.replaceChildren(list);
    target.setAttribute('aria-busy', 'true');
  }
  const settled = target => target.setAttribute('aria-busy', 'false');
  /** Число так и не пришло — прочерк вместо вечного скелета. */
  function dashIfSkeleton(...ids) {
    ids.forEach(id => { const target = $(id); if (target && target.querySelector('.rm-skel')) target.textContent = '—'; });
  }

  /** Ссылка на другую страницу: крутится, пока та открывается. Возврат
   *  «назад» из кеша браузера снимает спиннер. */
  function busyLink(link) {
    link.addEventListener('click', event => {
      if (!Busy || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      busyButton(link, new Promise(resolve => {
        addEventListener('pageshow', function back(page) { if (page.persisted) { removeEventListener('pageshow', back); resolve(false); } });
      }), {done: false});
    });
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
    // Как в макете: приветствие — только на «Сегодня»; на других вкладках
    // свой заголовок, а в шапке остаются язык и выход.
    document.body.dataset.dirTab = tab;
    if (tab === 'menu') loadMenu();
    if (tab === 'team') { loadTeam(); loadReport(7); }
    window.scrollTo({top: 0});
    // Переписка есть — открываем на последнем сообщении, как в мессенджере.
    if (tab === 'ai') chat.load().then(() => { if (view.tab === 'ai') chat.toEnd(); });
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
    settled(list);
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
      settled($('tips'));
    });
    loadToday();
  }

  /** Живые цифры «Сегодня»: касса, остаток и замечания бухгалтера, команда. */
  function loadToday() {
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

  /** «Сегодня» обновляется само раз в минуту, пока экран открыт и виден:
   *  касса и замечания бухгалтера меняются в течение дня. Это фон — верхняя
   *  полоса не зажигается, прежние цифры на месте до прихода новых. */
  const REFRESH_MS = 60000;
  function autoRefresh() {
    setInterval(() => {
      if (view.tab !== 'home' || document.visibilityState !== 'visible' || view.saving) return;
      if (view.teamDay !== 'today') return;
      if (Busy && Busy.silent) Busy.silent(loadToday); else loadToday();
    }, REFRESH_MS);
  }

  /** Проверки дня — тем же модулем, что «Финансы дня» (2a): директор видит
   *  сегодняшний день бухгалтера ровно с теми ошибками, что и бухгалтер
   *  (раздел 4: «выдано без входа», «переплата оклада», минус остатка…). */
  function accountingIssues(data, staff, purchases) {
    const board = checks.shiftBoard({payday: data.date, staff, accruals: data.ledger.accruals, movements: data.ledger.movements});
    const monthly = checks.monthlyBoard(data);
    const shoh = checks.shohBoard(data.reserves && data.reserves.shoh, purchases, data.ledger.movements,
      data.supplier_transfers, data.cashier_shokh_gives);
    return checks.financeIssues({data, board, blocker: checks.shiftBlocker(staff, board), monthly, shoh, cash: checks.cashCard(data)});
  }

  async function loadAccounting() {
    const yesterday = shiftDay(view.today, -1);
    try {
      const [today, previous, staff, buys] = await Promise.all([
        request('/api/director/accounting/day?date=' + view.today),
        request('/api/director/accounting/day?date=' + yesterday),
        request('/api/director/accounting/staff?date=' + yesterday).catch(() => null),
        request('/api/director/accounting/purchases?date=' + view.today).catch(() => ({purchases: []})),
      ]);
      // Пока кассир не передал сегодняшнюю кассу, остаток дня не посчитан —
      // показываем вчерашний конец дня и прямо об этом пишем.
      const known = today.ledger.cash_balance !== null;
      const balance = known ? today.ledger.cash_balance : previous.ledger.cash_balance;
      $('cash').textContent = balance === null ? '—' : short(balance);
      $('cash').classList.toggle('is-negative', balance !== null && Number(balance) < 0);
      $('cash-sub').textContent = balance === null ? 'нет начального остатка' : known ? 'на конец дня' : 'на конец вчера · касса ещё не передана';
      const items = accountingIssues(today, staff, buys.purchases || []);
      const errors = items.filter(item => item.lvl === 'err').length;
      const badge = $('err-badge');
      badge.textContent = errors ? errors + ' ' + plural(errors, 'ошибка', 'ошибки', 'ошибок')
        : items.length ? items.length + ' ' + plural(items.length, 'замечание', 'замечания', 'замечаний') : 'Ошибок нет';
      badge.className = 'dir-badge ' + (errors ? 'is-bad' : items.length ? '' : 'is-ok');
      const list = $('err-list');
      list.replaceChildren();
      settled(list);
      // Список уже отсортирован по уровню: ошибка, внимание, к выполнению.
      items.slice(0, 3).forEach(item => {
        const row = node('div');
        const text = node('div');
        text.append(node('strong', '', item.text), node('small', '', item.sub));
        row.append(node('i', item.lvl === 'err' ? 'is-bad' : item.lvl === 'todo' ? 'is-todo' : ''), text);
        list.append(row);
      });
      $('ask-err').hidden = !items.length;
      $('ask-err').textContent = (items.length > 3 ? 'ещё ' + (items.length - 3) + ' — спросить AI →' : 'Что важнее всего? · AI →');
      view.accountingIssues = items;
    } catch (error) {
      $('cash').textContent = '—';
      $('err-badge').textContent = 'Нет данных';
      $('err-list').replaceChildren(node('p', 'dir-note', error.message));
      settled($('err-list'));
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
    // Название блюда — данные iiko: переводчик не трогает («Хлеб» → «Non»).
    const dishName = node('strong', '', row.name);
    dishName.dataset.i18n = 'off';
    name.append(dishName, meta);
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
      const foundName = node('strong', '', row.name);
      foundName.dataset.i18n = 'off';
      title.append(foundName, node('small', '', 'маржа ' + (row.margin === null ? '—' : Math.round(row.margin) + '%') + ' · ' + short(row.revenue) + ' сум'));
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

  /** trigger — чип периода, который начал загрузку: крутится он. Прежний
   *  список на месте и гаснет; первый раз — скелет в форме строк блюд. */
  async function loadMenu(trigger) {
    const days = view.menuDays;
    const list = $('menu-list');
    const work = loadReport(days);
    if (!view.reports[days]) {
      const shown = list.querySelector('.dir-dish');
      if (shown) busySection($('tab-menu').querySelector('.dir-menu-card'), work.catch(() => {}));
      else { skeletonList(list, 5, true); $('menu-period').textContent = ''; }
      if (trigger) busyButton(trigger, work, {done: false});
    }
    try {
      await work;
      settled(list);
      if (days === view.menuDays) renderMenu();
      // Тренд — это ещё два отчёта. Грузим их вслед, чтобы список не ждал.
      // Это подгрузка в фоне: верхнюю полосу не зажигаем.
      // silent() получает синхронную функцию: запросы уходят внутри неё, а
      // ожидание — снаружи, чтобы не заглушить чужие запросы на это время.
      let trends;
      const start = () => { trends = Promise.all([loadReport(3), loadReport(30)]); };
      if (Busy) Busy.silent(start); else start();
      trends.then(renderMenu).catch(() => {});
    } catch (error) {
      if (days === view.menuDays) { list.replaceChildren(node('p', 'dir-empty', error.message)); settled(list); }
    }
  }

  // ── Команда ──────────────────────────────────────────────────────────
  const STATUS = {on_time: 'Вовремя', late: 'Опоздал', missing: 'Не пришёл', unlinked: 'Без Hikvision',
    unavailable: 'Нет данных', manual_present: 'Был · вручную', manual_absent: 'Не был · вручную'};

  function hm(iso) {
    if (!iso) return '';
    return new Date(iso).toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Tashkent'});
  }

  /** Первый раз — скелет списка; дальше прежний список на месте и гаснет. */
  function loadTeam() {
    const list = $('team-list');
    const work = fetchTeam();
    if (!view.team) skeletonList(list, 4, true);
    else busySection(list, work);
    return work;
  }

  async function fetchTeam() {
    const day = view.teamDay === 'today' ? view.today : shiftDay(view.today, -1);
    const asked = view.teamDay;
    try {
      const team = await request('/api/director/team?date=' + day);
      if (asked !== view.teamDay) return;
      if (view.teamDay === 'today') {
        $('late-n').textContent = team.counts.late;
        $('miss-n').textContent = team.counts.missing;
        $('nohik-n').textContent = team.counts.no_hikvision;
        // «Без Hikvision» — все, кого нет на устройстве; у бухгалтера в проверках
        // видны только непривязанные. Подписываем, сколько их, чтобы числа не спорили.
        const unlinked = team.counts.unlinked_hikvision || 0;
        $('nohik-sub').hidden = !unlinked;
        $('nohik-sub').textContent = unlinked ? 'из них ' + unlinked + ' без привязки' : '';
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
      dashIfSkeleton('late-n', 'miss-n', 'nohik-n');
      // Прежний список не стираем: ошибку — над ним.
      if (view.team) toast(error.message, true);
      else $('team-list').replaceChildren(node('p', 'dir-empty', error.message));
    } finally {
      settled($('team-list'));
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
      // Ключ переживает перерисовку: «сохранено» подсвечивает ту же строку.
      button.dataset.busyKey = 'dir-team:' + row.type + ':' + row.id;
      const main = node('span', 'dir-person-main');
      const name = node('span', 'dir-person-name');
      // Имя — данные реестра: «(оклад)» в имени не переводится в «(oklad)».
      const personName = node('strong', '', row.name);
      personName.dataset.i18n = 'off';
      name.append(personName);
      if (row.noHik) name.append(node('span', 'rm-flag-nohik', row.manual ? '⊘ вручную' : '⊘ Hik'));
      main.append(name, node('small', '', row.role + ' · ' + (row.hasRate ? sum(row.rate) + (row.type === 'monthly' ? ' в месяц' : ' за смену') : 'нет ставки')));
      if (row.type === 'monthly') {
        // Оклад за месяц (3.5): полоса «выдано X из Y» и «осталось», «закрыт»
        // или «переплата» — выдано частями с 1-го числа, считает сервер.
        const progress = node('span', 'rm-bar' + (row.state === 'over' ? ' is-over' : ''));
        const fill = node('span');
        fill.style.width = Math.min(100, row.rate ? row.paid / row.rate * 100 : 0) + '%';
        progress.append(fill);
        const paid = node('span', 'dir-paid');
        const restText = row.state === 'over' ? 'переплата ' + short(row.paid - row.rate)
          : row.state === 'closed' ? 'закрыт' : 'осталось ' + short(row.rest);
        paid.append(node('span', '', 'выдано ' + short(row.paid) + ' из ' + short(row.rate)),
          node('span', row.state === 'over' ? 'is-over' : row.state === 'closed' ? 'is-closed' : 'is-rest', restText));
        main.append(progress, paid);
      } else if (row.monthShifts !== null) {
        // Смена: сколько смен отработано в месяце и сколько за них выдано.
        main.append(node('small', 'dir-month', row.monthShifts + ' ' + plural(row.monthShifts, 'смена', 'смены', 'смен')
          + ' в месяце · выдано ' + short(row.monthPaid)));
      }
      let pill = STATUS[row.status] || 'Оклад';
      if (row.status === 'late' || row.status === 'on_time') pill += ' ' + hm(row.firstEntry);
      // Сервер уже говорит «был / не был · вручную» — это точнее, чем просто
      // «Вручную»; общая плашка остаётся для старых ответов без статуса.
      const manualKnown = row.status === 'manual_present' || row.status === 'manual_absent';
      if (row.manual && !manualKnown) pill = 'Вручную';
      const status = node('span', 'dir-pill ' + (row.manual && !manualKnown ? 'manual' : row.status), pill);
      button.append(main, status, node('span', 'dir-chevron', '›'));
      button.addEventListener('click', () => openEditor(row));
      list.append(button);
    });
  }

  /** Официанты Retro за 7 дней (3.11): по выручке, чеки, смены и средний
   *  чек. «Больше всех» и «меньше всех» — по всему списку; видно первые
   *  шесть, остальные — по кнопке. */
  const WAITERS_PREVIEW = 6;
  function renderWaiters(snapshot) {
    const rows = logic.retroWaiters(snapshot);
    const target = $('waiters');
    target.replaceChildren();
    settled(target);
    if (!rows.length) { target.append(node('p', 'dir-empty', 'iiko не вернул продажи официантов Retro за неделю.')); return; }
    const max = Math.max(1, ...rows.map(row => row.revenue));
    rows.forEach((row, index) => {
      const item = node('div', index >= WAITERS_PREVIEW ? 'is-folded' : '');
      const line = node('div', 'dir-waiter-line');
      // Первый и последний в рейтинге подписаны и окрашены, как в макете.
      const top = index === 0 && rows.length > 1, low = index === rows.length - 1 && rows.length > 1;
      const who = node('span', 'dir-waiter-name');
      const waiterName = node('strong', '', row.name);
      waiterName.dataset.i18n = 'off';
      who.append(waiterName);
      if (top) who.append(node('span', 'dir-waiter-tag is-top', 'больше всех'));
      if (low) who.append(node('span', 'dir-waiter-tag is-low', 'меньше всех'));
      line.append(node('span', '', String(index + 1)), who, node('b', 'rm-num', short(row.revenue)));
      const bar = node('div', 'rm-bar');
      const fill = node('span');
      fill.style.width = Math.max(4, row.revenue / max * 100) + '%';
      fill.style.background = top ? '#d8b977' : low ? '#e9a0af' : '#24594b';
      bar.append(fill);
      item.append(line, bar, node('small', '', row.checks === null ? 'чеки не посчитаны'
        : row.checks + ' ' + plural(row.checks, 'чек', 'чека', 'чеков') + ' · ' + row.shifts + ' '
          + plural(row.shifts, 'смена', 'смены', 'смен') + ' · средний чек ' + (row.averageCheck === null ? '—' : short(row.averageCheck))));
      target.append(item);
    });
    if (rows.length > WAITERS_PREVIEW) {
      const more = node('button', 'dir-link dir-more', 'Все официанты · ' + rows.length);
      more.type = 'button';
      more.setAttribute('aria-expanded', 'false');
      more.addEventListener('click', () => {
        const open = target.classList.toggle('is-unfolded');
        more.setAttribute('aria-expanded', String(open));
        more.textContent = open ? 'Свернуть' : 'Все официанты · ' + rows.length;
      });
      target.append(more);
    }
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
    // Второе касание «Сохранить», пока идёт первая запись, завело бы второго
    // человека: форма отправляется, даже когда кнопка уже крутится.
    if (view.saving) return;
    const name = $('editor-name').value.trim(), role = $('editor-role').value.trim();
    const amount = $('editor-amount').value.replace(/\D/g, '');
    if (!name || !role || !Number(amount)) {
      $('editor-error').textContent = 'Укажите имя, должность и сумму.';
      const empty = !name ? $('editor-name') : !role ? $('editor-role') : $('editor-amount');
      empty.focus();
      return;
    }
    const row = view.editing;
    const body = {name, role, amount};
    if (view.editorType === 'shift') body.manual_attendance = view.manual;
    const url = row ? '/api/director/team/' + row.type + '/' + row.id : '/api/director/team';
    if (!row) body.type = view.editorType;
    $('editor-error').textContent = '';
    const editor = $('team-editor');
    // Кнопка крутится, пока запись не легла и список не перечитан; потом ✓,
    // редактор закрывается, а строка человека в списке вспыхивает зелёным.
    const work = request(url, {method: row ? 'PATCH' : 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
      .then(() => loadTeam());
    view.saving = true;
    editor.setAttribute('aria-busy', 'true');
    editor.classList.add('is-saving');
    busyButton($('editor-save'), work);
    try {
      await work;
      await wait(650);
      closeEditor();
      toast(row ? 'Сохранено: ' + name + ' · ' + sum(amount) + ' сум. Бухгалтер видит новую ставку.'
        : name + ' в реестре. Видно у бухгалтера в «Сотрудниках» и «Финансах дня».');
      const line = row ? document.querySelector('[data-busy-key="dir-team:' + row.type + ':' + row.id + '"]') : null;
      if (line && Busy) { Busy.flash(line); line.scrollIntoView({block: 'nearest', behavior: 'smooth'}); }
    } catch (error) {
      $('editor-error').textContent = error.message;
    } finally {
      view.saving = false;
      editor.setAttribute('aria-busy', 'false');
      editor.classList.remove('is-saving');
    }
  }

  async function deleteEditor() {
    const row = view.editing;
    if (!row || view.saving) return;
    view.saving = true;
    $('editor-error').textContent = '';
    const line = document.querySelector('[data-busy-key="dir-team:' + row.type + ':' + row.id + '"]');
    const removal = request('/api/director/team/' + row.type + '/' + row.id, {method: 'DELETE'});
    // Строка в списке «в работе» и после удаления плавно сворачивается.
    const work = line && Busy ? Busy.row(line, removal, {collapse: true}) : removal;
    busyButton($('editor-delete-yes'), work, {done: false});
    try {
      await work;
      closeEditor();
      toast(row.name + ' удалён(а) из реестра.');
      await loadTeam();
    } catch (error) {
      $('editor-error').textContent = error.message;
    } finally {
      view.saving = false;
    }
  }

  // ── Привязка событий ─────────────────────────────────────────────────
  function bind() {
    document.querySelectorAll('.dir-tabs .rm-tab').forEach(button =>
      button.addEventListener('click', () => openTab(button.dataset.tab)));
    // Пока на телефоне набирают вопрос, нижние вкладки прячутся: иначе они
    // висели между полем и клавиатурой, а поле съезжало к середине экрана.
    if (globalThis.matchMedia && matchMedia('(pointer: coarse)').matches) {
      $('chat-input').addEventListener('focus', () => document.body.classList.add('dir-typing'));
      $('chat-input').addEventListener('blur', () => document.body.classList.remove('dir-typing'));
    }
    $('ask-tip').addEventListener('click', () => ask('Что лучше всего сделать сегодня, чтобы увеличить продажи? Учитывай данные по всем заведениям.'));
    $('ask-err').addEventListener('click', () => ask('Разбери замечания к сегодняшнему отчёту бухгалтера'
      + (view.accountingIssues && view.accountingIssues.length ? ' (' + view.accountingIssues.map(item => item.text).join('; ') + ')' : '')
      + ': что из них самое важное и что сделать?'));
    $('ask-waiters').addEventListener('click', () => ask('Почему у официантов такая разница в продажах за неделю?'));
    document.querySelectorAll('[data-team-filter]').forEach(button => button.addEventListener('click', () => {
      view.teamFilter = button.dataset.teamFilter;
      view.teamDay = 'today';
      openTab('team');
    }));
    $('menu-query').addEventListener('input', event => { view.query = event.target.value; renderMenu(); });
    document.querySelectorAll('#tab-menu [data-days]').forEach(button => button.addEventListener('click', () => {
      if (view.menuDays === Number(button.dataset.days)) return;
      view.menuDays = Number(button.dataset.days);
      document.querySelectorAll('#tab-menu [data-days]').forEach(other => other.classList.toggle('is-active', other === button));
      loadMenu(button);
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
      if (view.teamDay === button.dataset.day) return;
      view.teamDay = button.dataset.day;
      $('team-days').querySelectorAll('button').forEach(other => other.classList.toggle('is-active', other === button));
      busyButton(button, loadTeam(), {done: false});
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
    document.querySelectorAll('a[href="/director/report"]').forEach(busyLink);
  }

  (async function start() {
    view.today = new Date().toISOString().slice(0, 10);
    try {
      const config = await globalThis.RetroConfig;
      if (config && config.today) view.today = config.today;
      // «‹ Панель» — только если учётной записи открыт не один директор.
      $('dir-back').hidden = !(config && Array.isArray(config.modules)
        && config.modules.some(module => module.path !== '/director'));
    } catch (error) {
      say(error.message, true);
    }
    const date = new Date(view.today + 'T12:00:00Z');
    $('today-label').textContent = date.toLocaleDateString('ru-RU', {weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC'})
      .replace(/^./, letter => letter.toUpperCase());
    chat = globalThis.RetroChat.mount({
      messages: $('chat-messages'), form: $('chat-form'), input: $('chat-input'),
      status: $('chat-status'), prompts: $('chat-prompts'), clear: $('chat-clear'), endpoint: '/api/director/chat',
    });
    bind();
    // Поле вопроса прилегает к нижним вкладкам: их высота зависит от выреза
    // экрана (полоска «домой» на iPhone), поэтому меряем её, а не угадываем.
    const tabs = document.querySelector('.dir-tabs');
    if (tabs && 'ResizeObserver' in globalThis) {
      new ResizeObserver(() => document.documentElement.style.setProperty('--dir-tabs-h', tabs.offsetHeight + 'px')).observe(tabs);
    }
    skeletonList($('waiters'), 4, true);
    loadHome();
    autoRefresh();
    loadReport(7).then(renderWaiters).catch(error => {
      $('waiters').replaceChildren(node('p', 'dir-empty', error.message));
      settled($('waiters'));
    });
  })();
})();
