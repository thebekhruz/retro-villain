/* «Доп. выплаты» под ведомостью «Зарплата · день» (ТЗ 09.10, Б-05).

   Список месяца и строка ввода в конце списка — как «Оклады, выданные сегодня»
   в «Зарплате · месяц»: кому, за какую смену, в какой день выдано, сколько и
   за что. Выплата пишется расходом в день выплаты: входит в итог сотрудника и
   дня в ведомости, в «Операции за день» и в кассу. В клетку она не входит —
   клетка остаётся обычной выплатой за смену.

   Сумма в списке — правка (форма ниже переходит в режим правки), × — удалить.
   Если у человека за этот день уже есть выплата в клетке или такая же доп.
   выплата — подсказка красным и вторая кнопка «Всё равно записать». */
(() => {
  const $ = id => document.getElementById(id);
  const X = globalThis.SalaryExtraLogic, B = globalThis.RetroBusy;
  const number = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
  const fmt = value => number.format(value), money = value => fmt(value) + ' сум';
  const dm = day => day.slice(8, 10) + '.' + day.slice(5, 7);
  const node = (tag, cls, text) => { const el = document.createElement(tag); if (cls) el.className = cls; if (text != null) el.textContent = text; return el; };
  const MONTHS = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];
  const DEFAULT_HINT = 'Разовая выплата сверх клетки: уйдёт расходом в день выплаты и войдёт в итоги ведомости.';
  let page = null, editing = null, sure = false, saving = false, touched = false, people = [], serverWarning = '';

  const data = () => page?.data();
  const form = () => $('extra-form');
  function values() {
    return {person: editing ? editing.person : X.findPerson(data()?.people, $('extra-employee').value),
      work: $('extra-work').value, paid: $('extra-paid').value,
      amount: X.parseAmount($('extra-amount').value), note: $('extra-note').value.trim()};
  }

  /* ── Список месяца ─────────────────────────────────────────────────────── */
  function render() {
    const current = data(); if (!current || !$('extra-section')) return;
    const rows = X.rows(current, page.visible), filtered = page.filtered();
    const month = current.month || current.days[0].slice(0, 7);
    $('extra-title').textContent = (current.basis === 'shift' ? 'Доп. выплаты за смены · ' : 'Доп. выплаты за ') + MONTHS[Number(month.slice(5, 7)) - 1];
    $('extra-sum').textContent = rows.length ? (filtered ? 'по фильтру · ' : '') + rows.length + ' · ' + money(X.total(rows)) : '';
    const box = $('extra-list'); box.replaceChildren();
    rows.forEach(item => {
      const line = node('div', 'fd-mo-row sd-extra-row' + (editing?.id === item.id ? ' is-focus' : ''));
      const who = node('div', 'fd-mo-who');
      who.append(node('div', 'fd-who-name', item.name), node('div', 'fd-who-role', X.roleLine(item)));
      const open = item.editable && !current.closed;
      const amount = node(open ? 'button' : 'strong', 'num' + (open ? ' sd-extra-amount' : ''), fmt(item.value));
      if (open) {
        amount.type = 'button'; amount.title = 'Изменить'; amount.setAttribute('aria-label', 'Изменить доп. выплату ' + item.name + ' · ' + money(item.value));
        amount.addEventListener('click', () => startEdit(item));
      }
      const x = node('button', 'fd-x', '×');
      x.type = 'button'; x.title = 'Удалить выплату'; x.setAttribute('aria-label', 'Удалить доп. выплату');
      x.disabled = !open;
      x.addEventListener('click', () => remove(item, x));
      line.append(who, amount, node('span', 'fd-mo-left', X.describe(item)), x);
      box.append(line);
    });
    if (!rows.length) box.append(node('div', 'fd-mo-empty', filtered ? 'По фильтру доп. выплат нет.' : 'В этом месяце доп. выплат нет.'));
    fillPeople(current);
    const closed = !!current.closed;
    form().querySelectorAll('input,button').forEach(el => { el.disabled = closed || (!!editing && ['extra-employee', 'extra-paid'].includes(el.id)); });
    if (!touched && !editing) day(page.selectedDay());
    hint();
  }
  // В списке «Кому» — только те, чья смена (дата «Смена») в их периоде (T-434).
  function fillPeople(current) {
    const list = X.choices(current.people, $('extra-work').value);
    if (list.map(item => item.label).join('\n') === people.join('\n')) return;
    people = list.map(item => item.label);
    $('extra-people').replaceChildren(...list.map(item => { const option = node('option'); option.value = item.label; return option; }));
  }
  /* Форма, которую ещё не трогали, берёт выбранную смену и выплату на следующий день. */
  function day(selected) {
    if (touched || editing || !selected) return;
    const current = data();
    const next = X.defaults(selected, current.basis);
    $('extra-paid').value = next.paid; $('extra-work').value = next.work;
    $('extra-paid').max = $('extra-work').max = current.today;
    $('extra-paid').min = current.entry_start; $('extra-work').min = X.shiftDay(current.entry_start, -1);
    fillPeople(current);
    hint();
  }

  /* ── Подсказка под строкой ввода: кто выбран и не та же ли это выдача ─── */
  function hint(error) {
    const box = $('extra-hint'), button = $('extra-submit'), current = data();
    if (!box || !current) return;
    box.className = 'fd-mo-hint'; button.classList.remove('is-danger');
    button.textContent = editing ? 'Сохранить изменения' : 'Записать выплату';
    box.replaceChildren();
    if (error) { box.textContent = error; box.classList.add('is-bad'); return; }
    const v = values();
    const texts = v.person && v.work && v.paid ? X.warnings(current, v.person, v.work, v.paid, v.amount, editing?.id) : [];
    if (serverWarning && !texts.length) texts.push(serverWarning);
    if (editing) {
      box.classList.add('is-info');
      box.append('Правка: ' + editing.name + ' · выплата ' + dm(editing.paid_day) + '. Сотрудника и день выплаты не изменить — удалите запись и запишите заново. ');
      const cancel = node('button', 'fd-link-btn', 'Отменить правку'); cancel.type = 'button';
      cancel.addEventListener('click', () => stopEdit());
      box.append(cancel);
      return;
    }
    if (texts.length) {
      // Каждая причина — своим узлом: переводчик панели узнаёт их по отдельности.
      box.append(...texts.map((text, index) => node('span', '', (index ? ' ' : '') + text))); box.classList.add('is-bad');
      if (sure) { button.textContent = 'Всё равно записать'; button.classList.add('is-danger'); }
      return;
    }
    if (v.person && v.work && !X.inPeriod(v.person, v.work)) {
      // Смена вне периода временного (T-434): сказать сразу, а не после «Записать».
      box.textContent = X.outsideText(v.person, v.work); box.classList.add('is-bad');
      return;
    }
    if (v.person) {
      const rate = v.person.rate && Number(v.person.rate) ? ' · ставка ' + fmt(Number(v.person.rate)) : '';
      box.textContent = [v.person.name, v.person.role, X.typeTag(v.person)].filter(Boolean).join(' · ') + rate
        + (v.paid ? '. В клетке ' + dm(current.basis === 'shift' ? v.work : v.paid) + ' выплаты нет.' : '.');
      box.classList.add('is-info');
      return;
    }
    box.textContent = DEFAULT_HINT;
  }

  /* ── Записать, изменить, удалить ───────────────────────────────────────── */
  async function request(url, method, body) {
    const options = {method, headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined};
    // Новую выплату — с ключом операции: повтор после потерянного ответа не запишет её второй раз.
    const response = method === 'POST' && globalThis.RetroFinancialWrite ? await RetroFinancialWrite(url, options) : await fetch(url, options);
    if (response.status === 204) return null;
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = result.detail;
      const error = new Error(typeof detail === 'string' ? detail : detail?.message || 'Не удалось записать выплату.');
      error.confirm = !!detail?.confirm;
      throw error;
    }
    return result;
  }
  async function submit() {
    const current = data(); if (!current || saving) return false;
    const v = values(), error = X.check(v, current);
    if (error) { hint(error); return false; }
    const texts = editing ? [] : X.warnings(current, v.person, v.work, v.paid, v.amount);
    if (texts.length && !sure) { sure = true; hint(); $('extra-submit').focus({preventScroll: true}); return false; }
    saving = true;
    const body = editing
      ? {work_day: v.work, amount: String(v.amount), note: v.note, expected_amount: editing.amount}
      : {employee_id: v.person.id, work_day: v.work, paid_day: v.paid, amount: String(v.amount), note: v.note, confirm: sure};
    const work = editing ? request('/api/accountant/salary-day/extra/' + encodeURIComponent(editing.id), 'PUT', body)
      : request('/api/accountant/salary-day/extra', 'POST', body);
    globalThis.RetroSave?.track(work);
    try {
      await (B ? B.button($('extra-submit'), work, {done: false}) : work);
      const was = editing;
      // Новую запись — следующую за тот же день: даты остаются. После правки — снова выбранный день.
      clear(!was);
      saving = false;
      await page.reload();
      page.message(was ? 'Изменено: ' + was.name + ', ' + money(v.amount) + '.'
        : 'Записано: ' + v.person.name + ', ' + money(v.amount) + '. Выплата попала в «Операции за день» за ' + dm(v.paid) + ' и уменьшила кассу.');
      return true;
    } catch (error) {
      // Сервер нашёл то, чего не видно на экране (выплата за смену в другом месяце): тот же вопрос.
      if (error.confirm) { sure = true; serverWarning = error.message; }
      else page.message(error.message, true);
      return false;
    } finally { saving = false; hint(); }
  }
  function clear(keepDates = false) {
    editing = null; sure = false; serverWarning = '';
    if (!keepDates) touched = false;
    ['extra-employee', 'extra-amount', 'extra-note'].forEach(id => { $(id).value = ''; });
    form().classList.remove('is-focus');
  }
  function startEdit(item) {
    editing = {...item}; sure = false;
    $('extra-employee').value = [item.name, item.role].filter(Boolean).join(' · ');
    $('extra-work').value = item.work_day; $('extra-paid').value = item.paid_day;
    $('extra-amount').value = fmt(item.value); $('extra-note').value = item.note;
    form().classList.add('is-focus');
    render();
    $('extra-amount').focus(); $('extra-amount').select();
  }
  function stopEdit() { clear(); render(); }
  async function remove(item, button) {
    if (!await confirmAt(button, 'Удалить доп. выплату ' + item.name + ' · ' + money(item.value) + ' за ' + dm(item.paid_day) + '?')) return;
    const work = request('/api/accountant/salary-day/extra/' + encodeURIComponent(item.id), 'DELETE');
    globalThis.RetroSave?.track(work);
    try {
      await (B ? B.button(button, work, {done: false}) : work);
      if (editing?.id === item.id) clear();
      await page.reload();
      page.message('Доп. выплата удалена. Деньги вернулись в кассу ' + dm(item.paid_day) + '.');
    } catch (error) { page.message(error.message, true); }
  }

  /* Подтверждение удаления — строкой у кнопки, как в «Зарплате · месяц». */
  let popover = null;
  function confirmAt(anchor, question) {
    if (popover) popover.done(false);
    return new Promise(resolve => {
      const box = node('div', 'pr-confirm'); box.setAttribute('role', 'alertdialog');
      const actions = node('div', 'pr-confirm-actions');
      const keep = node('button', 'pr-confirm-keep', 'Оставить'), yes = node('button', 'pr-confirm-yes', 'Удалить');
      keep.type = yes.type = 'button';
      actions.append(keep, yes); box.append(node('span', 'pr-confirm-q', question), actions);
      document.body.append(box);
      const place = () => {
        const r = anchor.getBoundingClientRect(), w = box.offsetWidth, h = box.offsetHeight;
        const top = r.bottom + h + 8 > innerHeight ? r.top - h - 6 : r.bottom + 6;
        box.style.top = Math.max(8, top) + 'px';
        box.style.left = Math.min(Math.max(8, r.right - w), innerWidth - w - 8) + 'px';
      };
      const onKey = event => { if (event.key === 'Escape') done(false); };
      function done(answer) {
        box.remove(); popover = null;
        window.removeEventListener('scroll', place, true); window.removeEventListener('resize', place);
        document.removeEventListener('keydown', onKey);
        resolve(answer);
      }
      popover = {done};
      place();
      window.addEventListener('scroll', place, true); window.addEventListener('resize', place);
      document.addEventListener('keydown', onKey);
      keep.addEventListener('click', () => done(false));
      yes.addEventListener('click', () => done(true));
      setTimeout(() => { if (box.isConnected) keep.focus({preventScroll: true}); }, 0);
    });
  }

  function wire() {
    form().addEventListener('submit', event => { event.preventDefault(); submit(); });
    ['extra-employee', 'extra-amount', 'extra-note'].forEach(id => $(id).addEventListener('input', () => { sure = false; serverWarning = ''; hint(); }));
    ['extra-work', 'extra-paid'].forEach(id => $(id).addEventListener('change', () => {
      touched = true; sure = false; serverWarning = '';
      if (id === 'extra-work' && data()) fillPeople(data());
      hint();
    }));
    // Сумма с разрядами, как в остальных полях денег: 150000 → «150 000».
    $('extra-amount').addEventListener('change', () => { const value = X.parseAmount($('extra-amount').value); if (value) $('extra-amount').value = fmt(value); });
    form().addEventListener('keydown', event => { if (event.key === 'Escape' && editing) { event.preventDefault(); stopEdit(); } });
    globalThis.RetroSave?.register(form(), () => submit(), {dirty: () => !form().closest('[hidden]') && (editing
      ? X.parseAmount($('extra-amount').value) !== editing.value || $('extra-work').value !== editing.work_day
        || $('extra-note').value.trim() !== editing.note
      : ['extra-employee', 'extra-amount', 'extra-note'].some(id => $(id).value.trim()))});
  }

  /* page: data() — ответ ведомости, visible(person) и filtered() — фильтр
     ведомости, selectedDay(), reload() — перечитать месяц, message(text, error). */
  function mount(api) { page = api; wire(); return {render, day, busy: () => saving || !!editing || !!popover}; }
  globalThis.SalaryExtra = {mount};
})();
