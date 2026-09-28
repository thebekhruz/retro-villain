/** Встроенный AI-чат: вкладка директора и правая колонка учредителя.
 *
 *  В отличие от выдвижной панели (founder-chat.js) чат здесь всегда на
 *  экране, а вопрос можно задать кнопкой из любого блока: «Спросить AI
 *  подробнее», «Разобрать с AI». История хранится на сервере по учётной
 *  записи, поэтому переживает перезагрузку и смену устройства. */
(function (root) {
  function mount(options) {
    const {messages, form, input, status, endpoint} = options;
    const prompts = options.prompts || null;
    let loaded = false, loading = null, busy = false;

    function setStatus(text, error) {
      if (!status) return;
      status.textContent = text || '';
      status.classList.toggle('is-error', Boolean(error));
    }

    function bubble(role, content) {
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
      messages.append(node);
      node.scrollIntoView({block: 'end', behavior: 'smooth'});
      return node;
    }

    async function request(url, init) {
      const response = await fetch(url, {...init, headers: {'Content-Type': 'application/json', ...(init && init.headers)}});
      if (!response.ok) {
        let detail = 'Не удалось связаться с помощником.';
        try { detail = (await response.json()).detail || detail; } catch (error) { /* ответ без тела */ }
        throw new Error(detail);
      }
      return response.status === 204 ? null : response.json();
    }

    function load() {
      if (loaded) return Promise.resolve();
      if (loading) return loading;
      loading = request(endpoint).then(data => {
        (data.messages || []).forEach(item => bubble(item.role, item.content));
        loaded = true;
        if (!data.configured) setStatus('Помощник ещё не настроен на сервере.', true);
      }).catch(error => setStatus(error.message, true)).finally(() => { loading = null; });
      return loading;
    }

    async function ask(text) {
      const question = String(text || '').trim();
      if (!question || busy) return;
      await load();
      busy = true;
      form.querySelectorAll('button,input,textarea').forEach(node => { node.disabled = true; });
      bubble('user', question);
      const pending = bubble('assistant', 'AI смотрит данные…');
      // Заглушка — наша подпись, а не ответ модели: её переводим.
      delete pending.dataset.i18n;
      pending.classList.add('is-pending');
      setStatus('');
      try {
        const data = await request(endpoint, {method: 'POST', body: JSON.stringify({message: question})});
        pending.remove();
        bubble('assistant', data.message.content);
      } catch (error) {
        pending.remove();
        setStatus(error.message, true);
      } finally {
        busy = false;
        form.querySelectorAll('button,input,textarea').forEach(node => { node.disabled = false; });
        input.focus({preventScroll: true});
      }
    }

    form.addEventListener('submit', event => {
      event.preventDefault();
      const text = input.value;
      input.value = '';
      ask(text);
    });
    if (prompts) {
      prompts.addEventListener('click', event => {
        const button = event.target.closest('button');
        if (button) ask(button.textContent);
      });
    }
    return {ask, load};
  }

  root.RetroChat = {mount};
})(globalThis);
