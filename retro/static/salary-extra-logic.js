/* Доп. выплаты «Зарплата · день» (ТЗ 09.10, Б-05) без DOM: подпись сотрудника
   в поиске, разбор того, что набрано в поле «Кому», предупреждение о второй
   выдаче и строки списка под ведомостью. Проверяется тестом
   (tests/js/salary-extra.test.mjs). */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.SalaryExtraLogic=api;})(typeof globalThis!=='undefined'?globalThis:this,function(){
  const number=new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2});
  const fmt=value=>number.format(value);
  const dm=day=>day.slice(8,10)+'.'+day.slice(5,7);
  function shiftDay(day,step){const date=new Date(day+'T12:00:00Z');date.setUTCDate(date.getUTCDate()+step);return date.toISOString().slice(0,10);}
  function parseAmount(value){
    const clean=String(value??'').replace(/[\s  ]/g,'').replace(',','.');
    if(!clean)return 0;
    if(!/^\d+(\.\d{1,2})?$/.test(clean))return null;
    const amount=Number(clean);
    return Number.isFinite(amount)&&Number.isSafeInteger(Math.round(amount*100))?amount:null;
  }
  const key=text=>String(text??'').toLowerCase().replace(/ё/g,'е').replace(/\s+/g,' ').trim();

  /* В виде по сменам выбран день смены, выплата — следующим днём. */
  function defaults(day,basis='payment'){return basis==='shift'?{paid:shiftDay(day,1),work:day}:{paid:day,work:shiftDay(day,-1)};}

  /* Временный (T-434): «временный · 08.10–10.10»; период подписывает сервер. */
  function typeTag(person){return person?.temporary?(person.work_period?'временный · '+person.work_period:'временный'):'';}
  /* Смена в периоде временного: границы необязательны, даты — ISO-строки. */
  function inPeriod(person,day){
    return !day||((!person?.work_from||day>=person.work_from)&&(!person?.work_to||day<=person.work_to));
  }
  /* Отказ словами — тот же, что у сервера: «Карамат работает с 08.10 по 10.10 —
     доп. выплату за смену 07.10 записать нельзя.» */
  function outsideText(person,work){
    const from=person.work_from, to=person.work_to;
    const span=from&&to?(from===to?'только '+dm(from):'с '+dm(from)+' по '+dm(to)):from?'с '+dm(from):'по '+dm(to);
    return person.name+' работает '+span+' — доп. выплату за смену '+dm(work)+' записать нельзя.';
  }

  /* Кому можно записать: реестр, включая временных; ушедшим в архив — нельзя.
     Временный — только если выбранная смена в его периоде (work — день смены;
     без него — все). Подпись — имя · должность · «временный · период»; у полных
     тёзок — ещё номер. */
  function choices(people,work){
    const list=(people||[]).filter(person=>!person.archived&&inPeriod(person,work)).map(person=>({person,
      label:[person.name,person.role,typeTag(person)].filter(Boolean).join(' · ')}));
    const seen=new Map();list.forEach(item=>seen.set(key(item.label),(seen.get(key(item.label))||0)+1));
    list.forEach(item=>{if(seen.get(key(item.label))>1)item.label+=' · №'+item.person.id;});
    return list.sort((a,b)=>a.label.localeCompare(b.label,'ru'));
  }
  /* Что набрано в поле «Кому»: подпись из списка или имя, если оно одно такое. */
  function findPerson(people,text){
    const wanted=key(text);if(!wanted)return null;
    const list=choices(people);
    const exact=list.find(item=>key(item.label)===wanted);if(exact)return exact.person;
    const named=list.filter(item=>key(item.person.name)===wanted);
    return named.length===1?named[0].person:null;
  }

  /* Почему это может быть та же выдача второй раз (сервер проверит так же):
     выплата в клетке за этот день выплаты или за эту смену, такая же доп. выплата. */
  function warnings(data,person,work,paid,amount,except){
    const texts=[];
    const cells=(data.basis==='shift'
      ? Object.entries(person?.cells||{}).filter(([day,cell])=>day===work||cell.paid_days?.includes(paid))
          .map(([day,cell])=>[day,parseAmount(cell.amount)||0])
      : [...new Set([paid,shiftDay(work,1)])].map(day=>[day,parseAmount(person?.cells?.[day]?.amount)||0]))
      .filter(([,value])=>value>0);
    if(person&&cells.length)texts.push('У сотрудника «'+person.name+'» уже отмечена выплата в клетке: '
      +cells.map(([day,value])=>dm(day)+' — '+fmt(value)+' сум (смена '+dm(data.basis==='shift'?day:shiftDay(day,-1))+')').join(', ')
      +'. Доп. выплата — отдельные деньги сверх клетки; если это та же выдача, второй раз её не записывайте.');
    const same=(data.extras||[]).some(item=>person&&item.employee_id===person.id&&item.id!==except
      &&item.work_day===work&&item.paid_day===paid&&parseAmount(item.amount)===amount);
    if(same)texts.push('Такая доп. выплата уже записана: '+fmt(amount)+' сум, выплата '+dm(paid)+' за смену '+dm(work)+'.');
    return texts;
  }

  /* Ошибка ввода или null. Даты — как у сервера: выплата с начала учёта и не
     в будущем, смена не позже выплаты и не раньше 05.10.2026. */
  function check({person,work,paid,amount,note},data){
    if(!person)return 'Выберите сотрудника из списка.';
    if(!/^\d{4}-\d{2}-\d{2}$/.test(paid||''))return 'Укажите дату выплаты.';
    if(!/^\d{4}-\d{2}-\d{2}$/.test(work||''))return 'Укажите дату смены.';
    if(paid<data.entry_start)return 'Доп. выплаты вводятся с 05.10.2026 — с начала рабочего учёта.';
    if(paid>data.today)return 'Нельзя записать выплату будущим днём.';
    if(work>paid)return 'Смена не может быть позже дня выплаты.';
    if(work<(data.shift_start||data.entry_start))return 'Смена — не раньше 05.10.2026: с неё начинается ручная ведомость.';
    if(!inPeriod(person,work))return outsideText(person,work);
    if(amount===null||!(amount>0))return 'Введите сумму цифрами, больше нуля.';
    if(!String(note||'').trim())return 'Укажите назначение: за что выплата.';
    return null;
  }

  /* Строки списка: новые сверху; фильтр ведомости (группа, поиск) — тот же. */
  function rows(data,visible){
    const people=new Map((data.people||[]).map(person=>[person.id,person]));
    const list=(data.extras||[]).map(item=>({...item,person:people.get(item.employee_id)||null,
      value:parseAmount(item.amount)}))
      .filter(item=>!visible||!item.person||visible(item.person));
    list.forEach(item=>{if(item.value===null)throw new Error('Не удалось прочитать сумму выплаты. Обновите ведомость.');});
    return list.sort((a,b)=>b.paid_day.localeCompare(a.paid_day)||b.id-a.id);
  }
  function total(list){return list.reduce((sum,item)=>sum+Math.round(item.value*100),0)/100;}
  /* «выплата 09.10 · смена 08.10 · назначение» — строка списка. */
  function describe(item){return 'выплата '+dm(item.paid_day)+' · смена '+dm(item.work_day)+(item.note?' · '+item.note:'');}
  function roleLine(item){
    const period=item.work_period||item.person?.work_period;
    const tag=item.temporary||item.person?.temporary?'временный'+(period?' · '+period:''):'';
    return [item.role,tag,item.person?.archived?'архив':''].filter(Boolean).join(' · ');
  }
  return {parseAmount,shiftDay,defaults,choices,findPerson,warnings,check,rows,total,describe,roleLine,
    typeTag,inPeriod,outsideText};
});
