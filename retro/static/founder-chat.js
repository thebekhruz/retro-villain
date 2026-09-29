(()=>{
  const toggle=document.getElementById('ai-chat-toggle');
  const drawer=document.getElementById('ai-chat-drawer');
  if(!toggle||!drawer)return;
  const endpoint=drawer.dataset.endpoint||'/api/founder/chat';
  const closeButton=document.getElementById('ai-chat-close');
  const backdrop=document.getElementById('ai-chat-backdrop');
  const messages=document.getElementById('ai-chat-messages');
  const empty=document.getElementById('ai-chat-empty');
  const form=document.getElementById('ai-chat-form');
  const input=document.getElementById('ai-chat-input');
  const send=document.getElementById('ai-chat-send');
  const clear=document.getElementById('ai-chat-clear');
  const status=document.getElementById('ai-chat-status');
  let loaded=false,busy=false,lastFocus=null,configured=true;
  const Busy=window.RetroBusy;

  function setStatus(text,error=false){status.textContent=text||'';status.classList.toggle('is-error',error)}
  function scrollBottom(){messages.scrollTop=messages.scrollHeight}
  function resizeInput(){input.style.height='auto';input.style.height=Math.min(input.scrollHeight,120)+'px'}
  function bubble(item){
    const article=document.createElement('article');article.className=`ai-chat-message is-${item.role}`;
    const label=document.createElement('span');label.textContent=item.role==='assistant'?'Помощник':'Вы';
    let content;
    if(item.role==='assistant'&&window.FounderMarkdown){const rendered=window.FounderMarkdown.render(document,item.content);content=rendered.node;article.classList.toggle('has-table',rendered.hasTable)}
    else{content=document.createElement('p');content.textContent=item.content}
    // Ответ модели приходит по-русски: переводчик по кускам делал из него смесь языков.
    if(item.role==='assistant'&&!item.pending)content.dataset.i18n='off';
    article.append(label,content);messages.append(article);empty.hidden=true;scrollBottom();return article;
  }
  async function request(url,options={}){
    const response=await fetch(url,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});
    if(!response.ok){let text='Не удалось выполнить запрос.';try{text=(await response.json()).detail||text}catch{}const error=new Error(text);error.status=response.status;throw error}
    return response.status===204?null:response.json();
  }
  /* Отклик (busy.js, T-393): история грузится скелетом пузырей, ответ ждём
     «печатающим» пузырём, ошибка — пузырём с «Повторить». */
  function typing(){const dots=document.createElement('span');dots.className='rm-typing';dots.setAttribute('aria-hidden','true');dots.append(document.createElement('i'),document.createElement('i'),document.createElement('i'));return dots}
  let history=null;
  function loadHistory(){
    if(loaded)return Promise.resolve();
    if(history)return history;
    const skeleton=document.createElement('div');skeleton.className='ai-chat-skel';skeleton.setAttribute('aria-hidden','true');
    skeleton.innerHTML='<span class="rm-skel"></span><span class="rm-skel"></span><span class="rm-skel"></span>';
    const timer=setTimeout(()=>{empty.hidden=true;messages.append(skeleton)},250);
    history=request(endpoint).then(data=>{
      clearTimeout(timer);skeleton.remove();
      data.messages.forEach(bubble);loaded=true;configured=data.configured!==false;
      if(!data.messages.length)empty.hidden=false;
      setStatus(configured?'':'Помощник ещё не настроен на сервере.',!configured);
    }).catch(error=>{clearTimeout(timer);skeleton.remove();empty.hidden=false;setStatus(error.message,true)})
      .finally(()=>{history=null});
    return history;
  }
  function errorBubble(message,question){
    const article=document.createElement('article');article.className='ai-chat-message is-assistant is-error';article.setAttribute('role','alert');
    const label=document.createElement('span');label.textContent='Помощник';
    const body=document.createElement('p');const text=document.createElement('span');text.className='ai-chat-error-text';text.textContent=message;body.append(text);
    if(question&&configured){const retry=document.createElement('button');retry.type='button';retry.className='ai-chat-retry';retry.textContent='Повторить';
      retry.addEventListener('click',()=>{article.remove();submit(question)});body.append(retry)}
    article.append(label,body);messages.append(article);empty.hidden=true;scrollBottom();
  }
  function lock(on){form.classList.toggle('is-busy',on);form.setAttribute('aria-busy',String(on));input.readOnly=on}
  function open(){
    lastFocus=document.activeElement;drawer.classList.add('is-open');drawer.setAttribute('aria-hidden','false');
    toggle.setAttribute('aria-expanded','true');backdrop.hidden=false;document.body.classList.add('ai-chat-open');
    loadHistory().finally(()=>input.focus());
  }
  function close(){
    drawer.classList.remove('is-open');drawer.setAttribute('aria-hidden','true');toggle.setAttribute('aria-expanded','false');
    backdrop.hidden=true;document.body.classList.remove('ai-chat-open');if(lastFocus)lastFocus.focus();
  }
  async function submit(repeat){
    const question=repeat||input.value.trim();if(!question||busy)return;
    busy=true;lock(true);setStatus('');
    if(!repeat){bubble({role:'user',content:question});input.value='';resizeInput()}
    const pending=bubble({role:'assistant',content:'Помощник смотрит данные…',pending:true});pending.classList.add('is-pending');
    pending.querySelector('p')?.prepend(typing());
    const work=(async()=>{
      await loadHistory();
      if(!configured)throw new Error('Помощник ещё не настроен на сервере: ответ не придёт, пока не заданы ключи AI.');
      return request(endpoint,{method:'POST',body:JSON.stringify({message:question})});
    })();
    if(Busy)Busy.button(send,work,{done:false});
    try{
      const data=await work;
      pending.remove();bubble(data.message);
    }catch(error){pending.remove();if(error.status===503)configured=false;errorBubble(error.message,question)}
    finally{busy=false;lock(false);input.focus()}
  }

  toggle.addEventListener('click',open);closeButton.addEventListener('click',close);backdrop.addEventListener('click',close);
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&drawer.classList.contains('is-open'))close()});
  input.addEventListener('input',resizeInput);
  input.addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();form.requestSubmit()}});
  form.addEventListener('submit',event=>{event.preventDefault();submit()});
  empty.querySelectorAll('button').forEach(button=>button.addEventListener('click',()=>{input.value=button.textContent;resizeInput();input.focus()}));
  clear.addEventListener('click',()=>{
    if(busy)return;
    if(!messages.querySelector('.ai-chat-message')){setStatus('История чата уже пуста.');input.focus();return}
    if(!window.confirm('Удалить всю историю этого чата?'))return;
    const work=request(endpoint,{method:'DELETE'});
    if(Busy)Busy.button(clear,work);
    work.then(()=>{messages.querySelectorAll('.ai-chat-message').forEach(node=>node.remove());empty.hidden=false;setStatus('История очищена.')},
      error=>setStatus(error.message,true));
  });
})();
