/* Расчёты смены кассира без DOM. Формула передачи повторяет серверную
   (cash_to_finance в modules/cashier/expenses.py), поэтому держим её в одном
   месте и проверяем тестом, а не правим в двух. */
(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.CashierLogic=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){

  const CASH_PAYMENT='Демо';
  /* «Наличные (Инкасса QR)» приходят на счёт, а не в ящик кассира, поэтому в
     передачу не входят — и подписывать их как наличные нельзя. */
  const COLLECTION_HINT='Инкасса';

  function amount(value){return Number(value||0)}

  /* «К передаче» и «Касса за день» экран не считает: готовые числа приходят
     с сервера (till_summary в modules/cashier/till.py, Функционал §1). */

  /* Полоса состава оплат: доли считаем от суммы всех способов, нулевые не
     рисуем, чтобы полоса не превращалась в пунктир из невидимых кусков. */
  function composition(payments,palette){
    const rows=(payments||[]).map(item=>({name:item.name,value:amount(item.amount)}));
    const total=rows.reduce((sum,row)=>sum+row.value,0);
    return rows.map((row,index)=>({
      name:row.name,
      value:row.value,
      isCash:row.name===CASH_PAYMENT,
      goesToSafe:row.name.includes(COLLECTION_HINT),
      share:total?row.value/total:0,
      percent:total?Math.round(row.value/total*1000)/10:0,
      color:palette[index%palette.length],
    })).filter(row=>row.value>0);
  }

  /* Смена кассы из iiko (снимок дня): «Смена открыта» или «Смена закрыта 22:56».
     Закрыли уже после полуночи — к времени дописываем дату. Нет данных — null. */
  function shiftLabel(shift,day){
    if(!shift)return null;
    if(shift.open)return {open:true,text:'Смена открыта'};
    const closed=shift.closed_at;
    if(!closed)return null;
    const time=String(closed).slice(11,16);
    const sameDay=!day||String(closed).slice(0,10)===day;
    const date=String(closed).slice(8,10)+'.'+String(closed).slice(5,7);
    return {open:false,text:'Смена закрыта '+(sameDay?time:date+' '+time)};
  }

  /* Состояние кнопки «Передать бухгалтеру».
     none — передачи за день нет; done — записана и совпадает с расчётом;
     diff — записана, но расчёт с тех пор изменился (новые продажи, расходы,
     выдачи Шоху): показываем разницу и даём передать ещё раз;
     accountant — приход записал бухгалтер, кнопка кассира его не трогает. */
  function handoverView(record,current){
    if(!record||record.amount===null||record.amount===undefined)return {state:'none',difference:null};
    /* Бухгалтер подтвердил получение: передачу уже не отменить и не изменить,
       разницу (если сумма потом изменилась) кассир говорит бухгалтеру сам. */
    if(record.confirmed_at){
      const base=record.expected_amount!==null&&record.expected_amount!==undefined?record.expected_amount:record.amount;
      const difference=current===null||current===undefined?null:Math.round((current-amount(base))*100)/100;
      return {state:'confirmed',difference:difference!==null&&Math.abs(difference)>=0.01?difference:null,
        shortfall:amount(record.shortfall)};
    }
    if(record.source&&record.source!=='cashier'&&record.source!=='auto')
      return {state:'accountant',difference:null};
    if(current===null||current===undefined)return {state:'done',difference:null};
    const difference=Math.round((current-amount(record.amount))*100)/100;
    return {state:Math.abs(difference)>=0.01?'diff':'done',difference};
  }

  /* Сумма из поля ввода: пробелы и запятая допустимы («1 500 000», «120,5»). */
  function parseAmount(text){
    const clean=String(text===null||text===undefined?'':text).replace(/[\s ]/g,'').replace(',','.');
    if(!/^\d+(\.\d{1,2})?$/.test(clean))return null;
    const value=Number(clean);
    return value>0?value:null;
  }

  const toNumber=value=>value===null||value===undefined||value===''||!Number.isFinite(Number(value))?null:Number(value);
  const percentOf=(value,total)=>total>0&&value!==null?Math.round(value/total*1000)/10:null;

  /* Receipt amounts always come from this day's transaction snapshot. The
     registry supplies labels only, never additional money or a receipt date.
     An order with mixed/partial redemption cannot establish a receipt method. */
  function receivedAdvances(snapshot,registry){
    const entries=Array.isArray(snapshot.prepayments)?snapshot.prepayments:[];
    const received=toNumber(snapshot.received_prepayments_total);
    if(received===null||entries.some(entry=>toNumber(entry.amount)===null||Number(entry.amount)<=0)||
      entries.reduce((sum,entry)=>sum+Math.round(Number(entry.amount)*100),0)!==Math.round(received*100))return [];
    const orderTotals=new Map();
    for(const entry of entries){
      const key=entry.order_number;
      orderTotals.set(key,(orderTotals.get(key)||0)+Math.round((toNumber(entry.amount)||0)*100));
    }
    const notes=new Map(registry&&registry.date===snapshot.date&&!registry.failed
      ?(registry.received||[]).map(row=>[row.order_number,row]):[]);
    return entries.map(entry=>{
      const note=notes.get(entry.order_number);
      const sameReceipt=note&&note.received_day===snapshot.date&&
        Math.round((toNumber(note.amount)||0)*100)===orderTotals.get(entry.order_number);
      let method=entry.payment_method||null;
      if(!method&&sameReceipt){
        if(note.method)method=note.method;
        else if(note.credited_methods?.length===1&&
          Math.round((toNumber(note.credited_amount)||0)*100)===orderTotals.get(entry.order_number))
          method=note.credited_methods[0];
      }
      return {...entry,amount:toNumber(entry.amount),payment_method:method,guest:sameReceipt?note.guest:''};
    });
  }

  /* Receipt-day revenue: bill payments + incoming advances, counted once.
     Redeemed advances remain available to the registry but never enter totals. */
  function revenueGroups(snapshot,registry=null){
    if(!snapshot)return null;
    const total=toNumber(snapshot.daily_revenue), received=toNumber(snapshot.received_prepayments_total);
    const paid=toNumber(snapshot.revenue), fallback=!Array.isArray(snapshot.payment_groups);
    const sourceGroups=fallback?[{name:'Оплаты счетов',types:[...(snapshot.payments||[])]}]
      :snapshot.payment_groups.map(item=>({...item,types:[...(item.types||[])]}));
    const normalize=name=>String(name||'').trim().toLocaleLowerCase('ru');
    const methods=new Map();
    for(const item of [...(snapshot.payments||[]),...sourceGroups.flatMap(group=>group.types)])
      methods.set(normalize(item.name),item.name);
    if(methods.has(normalize(CASH_PAYMENT)))methods.set(normalize('Наличные'),CASH_PAYMENT);
    const allocated=new Map(),unassigned=[];
    for(const entry of receivedAdvances(snapshot,registry)){
      const method=methods.get(normalize(entry.payment_method));
      if(!method||entry.amount===null||entry.amount<=0){unassigned.push(entry);continue;}
      if(!allocated.has(method))allocated.set(method,[]);
      allocated.get(method).push(entry);
      if(!sourceGroups.some(group=>group.types.some(item=>item.name===method))){
        let other=sourceGroups.find(group=>group.name==='Другие способы оплаты');
        if(!other){other={name:'Другие способы оплаты',types:[]};sourceGroups.push(other);}
        const payment=(snapshot.payments||[]).find(item=>item.name===method);
        other.types.push({name:method,paid:payment?.amount||0});
      }
    }
    // A type can occur in both a historical dictionary group and today's
    // group. Attach receipts once, preferring the group with actual payments.
    const owners=new Map();
    for(const item of sourceGroups.flatMap(group=>group.types)){
      const previous=owners.get(item.name);
      if(!previous||(!(toNumber(previous.paid??previous.amount)||0)&&
        (toNumber(item.paid??item.amount)||0)))owners.set(item.name,item);
    }
    const type=(item,total)=>{
      const paid=toNumber(item.paid!==undefined?item.paid:item.amount);
      const redeemed=toNumber(item.redeemed)||0;
      const advances=owners.get(item.name)===item?allocated.get(item.name)||[]:[];
      const received=advances.reduce((sum,entry)=>sum+Math.round(entry.amount*100),0)/100;
      const sum=paid===null?null:Math.round((paid+received)*100)/100;
      return {name:item.name,label:item.name===CASH_PAYMENT?'Наличные':item.name,isCash:item.name===CASH_PAYMENT,
        goesToSafe:String(item.name).includes(COLLECTION_HINT),paid,redeemed,received,advances,total:sum,
        percent:percentOf(sum,total),zero:!sum};
    };
    const group=(name,items,total)=>{
      const types=items.map(item=>type(item,total));
      const sum=types.reduce((acc,item)=>acc+Math.round((item.total||0)*100),0)/100;
      return {name,total:sum,percent:percentOf(sum,total),types,shown:types.filter(item=>!item.zero),
        hidden:types.filter(item=>item.zero).length,hasCash:types.some(item=>item.isCash&&!item.zero)};
    };
    const receipts=toNumber(snapshot.full_receipt_count!==undefined&&snapshot.full_receipt_count!==null
      ?snapshot.full_receipt_count:snapshot.receipt_count);
    const groups=sourceGroups.map(item=>group(item.name,item.types,total));
    const assignedTotal=[...allocated.values()].flat().reduce((sum,entry)=>sum+Math.round(entry.amount*100),0)/100;
    const unassignedTotal=received===null?null:Math.round((received-assignedTotal)*100)/100;
    const groupsTotal=groups.reduce((sum,item)=>sum+Math.round(item.total*100),0)/100;
    const difference=total===null||unassignedTotal===null?null:Math.round((groupsTotal+unassignedTotal-total)*100)/100;
    return {fallback,total,received,receipts,groups,unassigned,unassignedTotal,
      average:toNumber(fallback?snapshot.average_receipt:snapshot.full_average_receipt),
      match:difference===null?null:Math.abs(difference)<=1,difference,
      redeemed:fallback?null:toNumber(snapshot.redeemed_total),realCash:paid};
  }

  /* Реестр предоплат (ТЗ 3.2). Статус: ждёт → зачтена / возврат. «На дату»
     скрыта до точного источника даты визита (13.10.2026) — и сигнал по ней тоже. */
  function prepaymentStatus(row,today){
    if(!row)return null;
    if(row.status==='refund')return {key:'refund',text:'возврат'};
    if(row.status==='credited')return {key:'credited',text:'зачтена',day:row.credited_day||null};
    return {key:'pending',text:'ждёт'};
  }
  /* Сумма строки: во вкладке «Зачтено» — сколько зачли, в остальных — сколько внесли. */
  function prepaymentAmount(row,tab){
    const received=toNumber(row?.amount),credited=toNumber(row?.credited_amount);
    return tab==='credited'?(credited!==null?credited:received):(received!==null?received:credited);
  }
  /* Группировка периода по людям (без имени — в конце). Итог группы — сумма её предоплат. */
  function prepaymentGroups(rows){
    const groups=new Map();
    for(const row of rows||[]){
      const guest=String(row.guest||'').trim();
      const key=guest.toLocaleLowerCase('ru');
      if(!groups.has(key))groups.set(key,{key,title:guest,rows:[],total:0});
      const entry=groups.get(key);
      entry.rows.push(row);
      entry.total=Math.round((entry.total+(prepaymentAmount(row,'period')||0))*100)/100;
    }
    return [...groups.values()].sort((a,b)=>!a.key?1:!b.key?-1:a.key.localeCompare(b.key,'ru'));
  }

  /* Первая загрузка дня (T-393): экран собирается из независимых частей.
     Цифра — скелетом, пока не пришла хотя бы одна часть, из которых она
     считается: «К передаче» ждёт и iiko, и расходы, и поступления, и Шоха. */
  const PARTS=['day','expenses','receipts','shokh','usd','rate'];
  /* Редизайн T-442: каждое число на экране один раз — итоги расходов,
     поступлений и долларов за день живут только в формуле «К передаче». */
  const SKELETON={
    revenue:['day'],receipts:['day'],average:['day'],'prepay-total':['prepay'],
    handover:['day','expenses','receipts','shokh','usd','rate'],'demo-cash':['day'],'cash-prepay':['day'],
    'handover-usd':['day','usd','rate'],
    'handover-receipts':['receipts'],'handover-expenses':['expenses'],'handover-shokh':['shokh'],
    'shokh-pocket':['shokh'],'usd-safe':['usd'],'usd-restaurant':['rate'],
  };
  /* {id: скелет ли} по набору ещё не пришедших частей. */
  function skeletonMap(waiting){
    const pending=waiting instanceof Set?waiting:new Set(waiting||[]);
    const result={};
    for(const [id,parts] of Object.entries(SKELETON))result[id]=parts.some(part=>pending.has(part));
    return result;
  }

  return {CASH_PAYMENT,composition,shiftLabel,handoverView,parseAmount,revenueGroups,prepaymentStatus,
    prepaymentAmount,prepaymentGroups,PARTS,skeletonMap};
});
