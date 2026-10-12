/* Несколько смен оплачиваются одной датой. Выбор не записывает деньги. */
globalThis.SalaryBatch={mount({data,reload}){
  const $=id=>document.getElementById(id), L=globalThis.SalaryDayLogic;
  const section=$('salary-batch');if(!section)return null;
  const fmt=n=>new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(n);
  const dm=d=>d.slice(8,10)+'.'+d.slice(5,7);
  const full=d=>d?new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(d+'T12:00:00Z')):'';
  const node=(tag,cls,text)=>{const el=document.createElement(tag);if(cls)el.className=cls;if(text!=null)el.textContent=text;return el;};
  const button=(text,run,primary=false)=>{const b=node('button','sb-button'+(primary?' sb-primary':''),text);b.type='button';b.addEventListener('click',run);return b;};
  const key=r=>r.employee_id+':'+r.work_day;
  let preview=null,loading=false,saving=false,sequence=0,initialized=false,history=null;
  const selected=new Map();
  function message(text,error=false,id='batch-message'){
    const el=$(id);el.textContent=text;el.hidden=!text;el.classList.toggle('is-error',error);el.setAttribute('role',error?'alert':'status');
  }
  function dates(){return {paid_day:$('batch-paid').value,first:$('batch-first').value,last:$('batch-last').value};}
  function dateLabels(){const d=dates().paid_day;$('batch-paid-label').textContent=full(d);$('batch-expense').textContent=d?'Расход в «Финансах дня» — '+full(d):'';}
  function dirty(){return selected.size>0;}
  function controls(){
    $('batch-dates').querySelectorAll('input,button').forEach(el=>el.disabled=saving);
    $('batch-load').disabled=saving||loading;
    $('batch-load').textContent=loading?'Загружаем смены…':'Показать смены';
    $('batch-select').disabled=saving||loading;$('batch-clear').disabled=saving||loading;
    $('batch-rows').querySelectorAll('input,button').forEach(el=>el.disabled=saving||loading);
    section.setAttribute('aria-busy',String(saving||loading));
    summary();
  }
  function summary(){
    let total=0,valid=true;const people=new Set();
    selected.forEach(({row,amount})=>{const value=L.parseAmount(amount);if(value===null||value<=0||value>Number(row.remaining))valid=false;else total+=Math.round(value*100);people.add(row.employee_id);});
    $('batch-count').textContent=selected.size?'Сотрудников: '+people.size+' · смен: '+selected.size:'Ничего не выбрано';
    $('batch-total').textContent=fmt(total/100)+' сум';
    $('batch-save').disabled=saving||loading||!selected.size||!valid||!preview;
    $('batch-save').textContent=saving?'Записываем…':'Записать выдачу';
    $('batch-rows').querySelectorAll('[data-person-total]').forEach(el=>{
      let sum=0;selected.forEach(v=>{if(v.row.employee_id===Number(el.dataset.personTotal))sum+=Math.round((L.parseAmount(v.amount)||0)*100);});el.textContent=fmt(sum/100);
    });
    dateLabels();
  }
  function visible(row){const query=$('batch-search').value;return globalThis.EmployeesLogic?.matchesQuery(query,row.name,row.role)??row.name.toLowerCase().includes(query.toLowerCase());}
  function render(){
    if(!preview)return;
    const head=node('tr');['Сотрудник',...preview.days.map(dm),'Выдаём, сум'].forEach(text=>{const th=node('th','',text);th.scope='col';head.append(th);});
    $('batch-head').replaceChildren(head);$('batch-rows').replaceChildren();
    const people=new Map();preview.rows.forEach(row=>{if(!people.has(row.employee_id))people.set(row.employee_id,[]);people.get(row.employee_id).push(row);});
    for(const [id,rows] of people){
      if(!visible(rows[0]))continue;
      const tr=node('tr'),who=node('th','sb-who');who.scope='row';who.append(node('strong','',rows[0].name),node('small','',rows[0].role));tr.append(who);
      for(const day of preview.days){
        const row=rows.find(r=>r.work_day===day),td=node('td');
        if(!row){tr.append(td);continue;}
        const chosen=selected.get(key(row));td.classList.toggle('is-selected',!!chosen);
        if(!row.editable){
          td.append(node('span','sb-state',row.reason));
          if(Number(row.paid))td.append(node('small','',fmt(Number(row.paid))+' сум · '+[...new Set(row.payments.map(p=>dm(p.paid_day)))].join(', ')));
          if(row.attendance.status==='unknown'&&!Number(row.paid)){const link=node('a','sb-link','Открыть сотрудников');link.href='/accountant/employees?date='+day;td.append(link);}
        } else {
          const label=node('label','sb-check'),check=node('input');check.type='checkbox';check.checked=!!chosen;check.setAttribute('aria-label',row.name+' · смена '+dm(day));
          label.append(check,node('strong','',fmt(Number(row.remaining))));td.append(label);
          if(Number(row.paid))td.append(node('small','', 'Ранее выдано '+fmt(Number(row.paid))));
          const status=L.shiftText(row.attendance);if(status)td.append(node('small','',status));
          check.addEventListener('change',()=>{if(check.checked)selected.set(key(row),{row,amount:row.remaining,custom:false});else selected.delete(key(row));render();});
          if(chosen){
            if(chosen.custom){
              const amount=node('input','sb-amount');amount.inputMode='decimal';amount.value=chosen.amount;amount.dataset.moneyHint='off';amount.dataset.saveIgnore='';amount.setAttribute('aria-label','Выдаём '+row.name+' за '+dm(day)+', сум');
              const hint=node('small','sb-limit','Не больше '+fmt(Number(row.remaining))+' сум');
              const validate=()=>{const value=L.parseAmount(amount.value),bad=value===null||value<=0||value>Number(row.remaining);amount.setAttribute('aria-invalid',String(bad));hint.textContent=bad?'Введите сумму от 0,01 до '+fmt(Number(row.remaining)):value<Number(row.remaining)?'Останется '+fmt(Number(row.remaining)-value)+' сум':'Вся сумма за смену';};
              amount.addEventListener('input',()=>{chosen.amount=amount.value;validate();summary();});td.append(amount,hint);validate();
            } else {const change=button('Другая сумма',()=>{chosen.custom=true;render();const input=$('batch-rows').querySelector('[aria-label="Выдаём '+CSS.escape(row.name)+' за '+dm(day)+', сум"]');input?.focus();input?.select();});change.classList.add('sb-change');td.append(change);}
          }
        }
        tr.append(td);
      }
      const total=node('td','sb-person-total');total.dataset.personTotal=String(id);tr.append(total);$('batch-rows').append(tr);
    }
    if(!$('batch-rows').children.length){const tr=node('tr'),td=node('td','','Сотрудники не найдены');td.colSpan=preview.days.length+2;tr.append(td);$('batch-rows').append(tr);}
    controls();
  }
  async function request(url,method,body){
    const options={method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};
    const response=await (method==='POST'?globalThis.RetroFinancialWrite(url,options):fetch(url,options));
    const result=await response.json();
    if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'Не удалось записать выплату. Проверьте поля.');
    return result;
  }
  async function load(){
    if(saving||!initialized)return false;
    if(!$('batch-dates').reportValidity())return false;
    const d=dates(),current=data();
    if(d.first>d.last||d.last>d.paid_day||d.last>=current.today||d.paid_day>current.today||d.first<current.shift_start||d.paid_day<current.entry_start){message('Проверьте даты: только завершённые смены в пределах учёта, не позже дня выдачи.',true);return false;}
    const token=++sequence;loading=true;preview=null;selected.clear();$('batch-list').hidden=true;message('');controls();
    try{
      const response=await fetch('/api/accountant/salary-day/batch?'+new URLSearchParams(d),{cache:'no-store'}),result=await response.json();
      if(token!==sequence)return false;
      if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'Не удалось загрузить смены.');
      preview=result;$('batch-list').hidden=false;render();return true;
    }catch(error){if(token===sequence)message(error.message,true);return false;}
    finally{if(token===sequence){loading=false;controls();}}
  }
  async function save(){
    if($('batch-save').disabled||!preview)return false;
    const body={paid_day:preview.paid_day,items:[...selected.values()].map(({row,amount})=>({employee_id:row.employee_id,work_day:row.work_day,amount:String(L.parseAmount(amount)),version:row.version}))};
    saving=true;controls();message('');
    try{
      const result=await request('/api/accountant/salary-day/batch','POST',body);
      selected.clear();preview=null;$('batch-list').hidden=true;
      const receipt=$('batch-receipt');receipt.replaceChildren(node('strong','', 'Выдача записана · '+fmt(Number(result.total))+' сум'),node('p','', 'Расход '+full(result.paid_day)+' · оплачено смен: '+result.payments.length));receipt.hidden=false;
      const refreshed=await reload(result.paid_day);
      if(!refreshed)message('Выдача записана. Не удалось обновить ведомость — нажмите «Обновить».',true);
      globalThis.RetroToast?.show('Выдача за несколько смен записана.','ok');return true;
    }catch(error){message(error.message,true);return false;}
    finally{saving=false;controls();}
  }
  $('batch-dates').addEventListener('submit',event=>{event.preventDefault();load();});
  $('batch-dates').querySelectorAll('input').forEach(input=>input.addEventListener('change',()=>{sequence++;loading=false;preview=null;selected.clear();$('batch-list').hidden=true;$('batch-receipt').hidden=true;message('Нажмите «Показать смены», чтобы загрузить выбранные даты.');controls();}));
  $('batch-search').addEventListener('input',render);
  $('batch-select').addEventListener('click',()=>{for(const row of preview?.rows||[])if(row.editable&&visible(row)&&!selected.has(key(row)))selected.set(key(row),{row,amount:row.remaining,custom:false});render();});
  $('batch-clear').addEventListener('click',()=>{selected.clear();render();});
  $('batch-save').addEventListener('click',()=>globalThis.RetroSave?.track(save()));
  // «Сохранить» сверху не подтверждает выдачу за человека: приводит к явной кнопке с итогом.
  globalThis.RetroSave?.register(section,()=>{section.open=true;message('Проверьте итог и нажмите «Записать выдачу».',true);$('batch-save').focus();return false;},{dirty});
  section.addEventListener('toggle',()=>{if(section.open&&initialized&&!preview&&!saving&&!loading&&!$('batch-receipt').hidden)return;if(section.open&&initialized&&!preview&&!saving&&!loading)load();});

  function showHistory(personId,day){
    const person=data()?.people.find(p=>p.id===personId);if(!person)return;
    const rows=person.cells?.[day]?.payment_details||[];history={personId,day};
    $('batch-history-title').textContent=person.name;
    $('batch-history-info').textContent='Выдано '+full(day)+' · '+fmt(rows.reduce((sum,r)=>sum+Number(r.amount),0))+' сум';
    $('batch-history-rows').replaceChildren();message('',false,'batch-history-message');
    for(const row of rows){
      const item=node('div','sb-payment'),label=node('label','','За смену '+dm(row.work_day));
      const input=node('input','sb-amount');input.value=row.amount;input.inputMode='decimal';input.dataset.saveIgnore='';input.readOnly=!row.editable;label.append(input);item.append(label);
      if(row.editable){
        item.append(button('Сохранить сумму',()=>edit(row,input.value),true));
        const cancel=button('Отменить выплату',()=>{if(confirm('Отменить выплату '+fmt(Number(row.amount))+' сум за смену '+dm(row.work_day)+'?'+(row.restores_balance?' Остаток за смену восстановится.':'')))edit(row,'0');});cancel.classList.add('sb-danger');item.append(cancel);
      }else item.append(node('span','sb-state','Только просмотр'));
      $('batch-history-rows').append(item);
    }
    if(!rows.length)$('batch-history-rows').append(node('p','','Все выплаты этой выдачи отменены.'));
    if(!$('batch-history').open)$('batch-history').showModal();
  }
  async function edit(row,amount){
    if(saving)return;
    const value=L.parseAmount(amount);if(value===null||value<0){message('Введите корректную сумму.',true,'batch-history-message');return;}
    saving=true;$('batch-history').querySelectorAll('button,input').forEach(el=>el.disabled=true);controls();
    const target={...history};
    const work=(async()=>{try{
      await request('/api/accountant/salary-day/batch-payments/'+row.id,'PUT',{amount:String(value),expected_amount:row.amount});
      preview=null;selected.clear();$('batch-list').hidden=true;
      if(await reload())showHistory(target.personId,target.day);
      else { $('batch-history').close();message('Изменение записано. Обновите ведомость перед следующей правкой.',true); }
    }catch(error){message(error.message,true,'batch-history-message');}
    finally{saving=false;$('batch-history').querySelectorAll('button,input').forEach(el=>el.disabled=false);controls();}})();
    globalThis.RetroSave?.track(work);await work;
  }
  $('batch-history-close').addEventListener('click',()=>{if(!saving)$('batch-history').close();});
  $('batch-history').addEventListener('cancel',event=>{if(saving)event.preventDefault();});
  return {busy:()=>loading||saving||dirty(),showHistory,render(){
    const current=data();if(!current)return;
    $('batch-paid').min=current.entry_start;$('batch-paid').max=current.today;
    for(const id of ['batch-first','batch-last']){$(id).min=current.shift_start;$(id).max=L.previousDay(current.today);}
    if(!initialized){initialized=true;$('batch-paid').value=current.today;$('batch-last').value=L.previousDay(current.today);$('batch-first').value=[current.shift_start,L.previousDay($('batch-last').value)].sort().at(-1);dateLabels();if(section.open)load();}
  }};
}};
