(()=>{
  const toggle=document.getElementById('ai-chat-toggle');
  const drawer=document.getElementById('ai-chat-drawer');
  if(!toggle||!drawer)return;
  const closeButton=document.getElementById('ai-chat-close');
  const backdrop=document.getElementById('ai-chat-backdrop');
  const messages=document.getElementById('ai-chat-messages');
  const empty=document.getElementById('ai-chat-empty');
  const form=document.getElementById('ai-chat-form');
  const input=document.getElementById('ai-chat-input');
  const send=document.getElementById('ai-chat-send');
  const clear=document.getElementById('ai-chat-clear');
  const status=document.getElementById('ai-chat-status');
  let loaded=false,busy=false,lastFocus=null;

  function setStatus(text,error=false){status.textContent=text||'';status.classList.toggle('is-error',error)}
  function scrollBottom(){messages.scrollTop=messages.scrollHeight}
  function resizeInput(){input.style.height='auto';input.style.height=Math.min(input.scrollHeight,120)+'px'}
  function bubble(item){
    const article=document.createElement('article');article.className=`ai-chat-message is-${item.role}`;
    const label=document.createElement('span');label.textContent=item.role==='assistant'?'Claude':'Вы';
    const content=document.createElement('p');content.textContent=item.content;
    article.append(label,content);messages.append(article);empty.hidden=true;scrollBottom();return article;
  }
  async function request(url,options={}){
    const response=await fetch(url,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});
    if(!response.ok){let text='Не удалось выполнить запрос.';try{text=(await response.json()).detail||text}catch{}throw new Error(text)}
    return response.status===204?null:response.json();
  }
  async function loadHistory(){
    if(loaded)return;setStatus('Загружаю историю…');
    try{
      const data=await request('/api/founder/chat');
      data.messages.forEach(bubble);loaded=true;
      setStatus(data.configured?'':'Claude ещё не настроен на сервере.',!data.configured);
    }catch(error){setStatus(error.message,true)}
  }
  function open(){
    lastFocus=document.activeElement;drawer.classList.add('is-open');drawer.setAttribute('aria-hidden','false');
    toggle.setAttribute('aria-expanded','true');backdrop.hidden=false;document.body.classList.add('ai-chat-open');
    loadHistory().finally(()=>input.focus());
  }
  function close(){
    drawer.classList.remove('is-open');drawer.setAttribute('aria-hidden','true');toggle.setAttribute('aria-expanded','false');
    backdrop.hidden=true;document.body.classList.remove('ai-chat-open');if(lastFocus)lastFocus.focus();
  }
  async function submit(){
    const question=input.value.trim();if(!question||busy)return;
    busy=true;send.disabled=true;input.disabled=true;setStatus('Claude формирует ответ…');bubble({role:'user',content:question});
    input.value='';resizeInput();
    const pending=bubble({role:'assistant',content:'Думаю…'});pending.classList.add('is-pending');
    try{
      const data=await request('/api/founder/chat',{method:'POST',body:JSON.stringify({message:question})});
      pending.remove();bubble(data.message);setStatus('');
    }catch(error){pending.remove();setStatus(error.message,true)}
    finally{busy=false;send.disabled=false;input.disabled=false;input.focus()}
  }

  toggle.addEventListener('click',open);closeButton.addEventListener('click',close);backdrop.addEventListener('click',close);
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&drawer.classList.contains('is-open'))close()});
  input.addEventListener('input',resizeInput);
  input.addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();form.requestSubmit()}});
  form.addEventListener('submit',event=>{event.preventDefault();submit()});
  empty.querySelectorAll('button').forEach(button=>button.addEventListener('click',()=>{input.value=button.textContent;resizeInput();input.focus()}));
  clear.addEventListener('click',async()=>{
    if(busy||!window.confirm('Удалить всю историю этого чата?'))return;
    try{await request('/api/founder/chat',{method:'DELETE'});messages.querySelectorAll('.ai-chat-message').forEach(node=>node.remove());empty.hidden=false;setStatus('История очищена.')}catch(error){setStatus(error.message,true)}
  });
})();
