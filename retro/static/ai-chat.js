/** Встроенный AI-чат: вкладка директора и правая колонка учредителя.
 *
 *  В отличие от выдвижной панели (founder-chat.js) чат здесь всегда на
 *  экране, а вопрос можно задать кнопкой из любого блока: «Спросить AI
 *  подробнее», «Разобрать с AI». История хранится на сервере по учётной
 *  записи, поэтому переживает перезагрузку и смену устройства.
 *
 *  Отклик (T-393, docs/feedback-principles.md): вопрос появляется в ленте
 *  сразу, ответ ждём «печатающим» пузырём, поле ввода остаётся на месте и
 *  показывает, что помощник занят; ошибка — пузырём в ленте с кнопкой
 *  «Повторить», а не строкой где-то под полем. */
(function (root) {
  const busy = () => root.RetroBusy || null;

  /** Три точки «печатает» — общий знак ожидания ответа. */
  function typing(doc) {
    const dots = doc.createElement('span');
    dots.className = 'rm-typing';
    dots.setAttribute('aria-hidden', 'true');
    dots.append(doc.createElement('i'), doc.createElement('i'), doc.createElement('i'));
    return dots;
  }

  function mount(options) {
    const {messages, form, input, status, endpoint} = options;
    const prompts = options.prompts || null;
    const send = form.querySelector('[type=submit]');
    let loaded = false, loading = null, pendingAsk = false, configured = true;
    // История встаёт сразу после приветствия, даже если вопрос задан раньше,
    // чем она пришла: иначе новый вопрос оказался бы над старой перепиской.
    let anchor = messages.lastElementChild;

    function setStatus(text, error) {
      if (!status) return;
      status.textContent = text || '';
      status.classList.toggle('is-error', Boolean(error));
    }

    function bubble(role, content, place) {
      const node = document.createElement('div');
      node.className = 'rm-msg ' + (role === 'assistant' ? 'is-ai' : 'is-user');
      if (role === 'assistant' && root.FounderMarkdown) {
        // Ответ модели приходит по-русски; переводчик по кускам делал из него смесь языков.
        node.dataset.i18n = 'off';
        const rendered = root.FounderMarkdown.render(document, content);
        node.append(rendered.node);
        node.classList.toggle('has-table', rendered.hasTable);
      } else {
        node.textContent = content;
      }
      if (place === 'history') {
        if (anchor && anchor.parentNode === messages) anchor.after(node); else messages.prepend(node);
        anchor = node;
      } else {
        messages.append(node);
        node.scrollIntoView({block: 'end', behavior: 'smooth'});
      }
      return node;
    }

    /** Ошибка — в ленте, рядом с вопросом, с повтором. */
    function errorBubble(message, question) {
      const node = document.createElement('div');
      node.className = 'rm-msg is-ai is-error';
      node.setAttribute('role', 'alert');
      const text = document.createElement('span');
      text.className = 'rm-msg-error-text';
      text.textContent = message;
      node.append(text);
      if (question && configured) {
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'rm-msg-retry';
        retry.textContent = 'Повторить';
        retry.addEventListener('click', () => { node.remove(); ask(question, {repeat: true}); });
        node.append(retry);
      }
      messages.append(node);
      node.scrollIntoView({block: 'end', behavior: 'smooth'});
      return node;
    }

    async function request(url, init) {
      const response = await fetch(url, {...init, headers: {'Content-Type': 'application/json', ...(init && init.headers)}});
      if (!response.ok) {
        let detail = 'Не удалось связаться с помощником.';
        try { detail = (await response.json()).detail || detail; } catch (error) { /* ответ без тела */ }
        const error = new Error(detail);
        error.status = response.status;
        throw error;
      }
      return response.status === 204 ? null : response.json();
    }

    function load() {
      if (loaded) return Promise.resolve();
      if (loading) return loading;
      // Скелет пузыря — только если история не пришла сразу (быстрое не мигает).
      let skeleton = null;
      const timer = setTimeout(() => {
        skeleton = document.createElement('div');
        skeleton.className = 'rm-msg is-ai rm-msg-skel';
        skeleton.setAttribute('aria-hidden', 'true');
        skeleton.innerHTML = '<span class="rm-skel"></span><span class="rm-skel"></span>';
        if (anchor && anchor.parentNode === messages) anchor.after(skeleton); else messages.prepend(skeleton);
      }, 250);
      loading = request(endpoint).then(data => {
        (data.messages || []).forEach(item => bubble(item.role, item.content, 'history'));
        loaded = true;
        configured = data.configured !== false;
        if (!configured) setStatus('Помощник ещё не настроен на сервере.', true);
      }).catch(error => setStatus(error.message, true)).finally(() => {
        clearTimeout(timer);
        if (skeleton) skeleton.remove();
        loading = null;
      });
      return loading;
    }

    function lockForm(on) {
      form.classList.toggle('is-busy', on);
      form.setAttribute('aria-busy', String(on));
      // Поле не выключаем: выключенное теряет фокус и выглядит сломанным.
      input.readOnly = on;
    }

    async function ask(text, opts = {}) {
      const question = String(text || '').trim();
      if (!question || pendingAsk) return;
      pendingAsk = true;
      if (!opts.repeat) bubble('user', question);
      const pending = document.createElement('div');
      pending.className = 'rm-msg is-ai is-pending';
      pending.setAttribute('role', 'status');
      const label = document.createElement('span');
      label.className = 'rm-typing-label';
      label.textContent = 'AI смотрит данные…';
      pending.append(typing(document), label);
      messages.append(pending);
      pending.scrollIntoView({block: 'end', behavior: 'smooth'});
      lockForm(true);
      setStatus('');
      const work = (async () => {
        await load();
        if (!configured) throw new Error('Помощник ещё не настроен на сервере: ответ не придёт, пока не заданы ключи AI.');
        return request(endpoint, {method: 'POST', body: JSON.stringify({message: question})});
      })();
      if (busy() && send) busy().button(send, work, {done: false});
      if (opts.trigger && busy()) busy().button(opts.trigger, work, {done: false});
      try {
        const data = await work;
        pending.remove();
        bubble('assistant', data.message.content);
      } catch (error) {
        pending.remove();
        if (error.status === 503) configured = false;
        errorBubble(error.message, question);
        setStatus('');
      } finally {
        pendingAsk = false;
        lockForm(false);
        if (!opts.fromButton) input.focus({preventScroll: true});
      }
    }

    form.addEventListener('submit', event => {
      event.preventDefault();
      if (pendingAsk) return;
      const text = input.value;
      if (!text.trim()) { input.focus(); return; }
      input.value = '';
      ask(text);
    });
    if (prompts) {
      prompts.addEventListener('click', event => {
        const button = event.target.closest('button');
        if (button && !pendingAsk) ask(button.textContent, {trigger: button, fromButton: true});
      });
    }
    return {ask, load};
  }

  root.RetroChat = {mount, typing};
})(globalThis);
