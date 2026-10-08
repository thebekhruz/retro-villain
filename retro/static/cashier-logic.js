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

  /* Выручка по кассам iiko (ТЗ «Выручка, оплаты и предоплаты», 08.10.2026, 3.1).
     Группы и типы — как прислал iiko, порядок задаёт сервер (касса с «Демо»
     первой). Сумма типа — полный счёт: оплаты плюс зачтённые в этот день
     предоплаты; проценты — от выручки по чекам. Типы без денег прячутся до
     «показать все». Нет групп от iiko — показываем оплаты продаж одним списком
     и прямо говорим, что предоплат в нём нет. */
  function revenueGroups(snapshot){
    if(!snapshot)return null;
    const type=(item,total)=>{
      const paid=toNumber(item.paid!==undefined?item.paid:item.amount);
      const redeemed=toNumber(item.redeemed)||0;
      const sum=toNumber(item.total!==undefined?item.total:item.amount);
      return {name:item.name,label:item.name===CASH_PAYMENT?'Наличные':item.name,isCash:item.name===CASH_PAYMENT,
        goesToSafe:String(item.name).includes(COLLECTION_HINT),paid,redeemed,total:sum,
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
    if(!Array.isArray(snapshot.payment_groups)){
      const total=toNumber(snapshot.revenue);
      return {fallback:true,total,receipts,average:toNumber(snapshot.average_receipt),
        groups:[group('Оплаты продаж',snapshot.payments||[],total)],
        match:null,difference:null,redeemed:null,realCash:null};
    }
    const total=toNumber(snapshot.full_total);
    const groups=snapshot.payment_groups.map(item=>group(item.name,item.types||[],total));
    const groupsTotal=toNumber(snapshot.groups_total);
    const difference=groupsTotal===null||total===null?null:Math.round((groupsTotal-total)*100)/100;
    return {fallback:false,total,receipts,average:toNumber(snapshot.full_average_receipt),groups,
      match:snapshot.groups_match===true,difference,
      redeemed:toNumber(snapshot.redeemed_total),realCash:toNumber(snapshot.real_cash)};
  }

  /* Реестр предоплат (ТЗ 3.2). Статус: ждёт → зачтена / возврат; ждущая
     предоплата с прошедшей датой события — отдельный сигнал кассиру. */
  function prepaymentStatus(row,today){
    if(!row)return null;
    if(row.status==='refund')return {key:'refund',text:'возврат'};
    if(row.status==='credited')return {key:'credited',text:'зачтена',day:row.credited_day||null};
    if(row.event_day&&today&&row.event_day<today)return {key:'overdue',text:'ждёт · дата прошла'};
    return {key:'pending',text:'ждёт'};
  }
  /* Сумма строки: во вкладке «Зачтено» — сколько зачли, в остальных — сколько внесли. */
  function prepaymentAmount(row,tab){
    const received=toNumber(row?.amount),credited=toNumber(row?.credited_amount);
    return tab==='credited'?(credited!==null?credited:received):(received!==null?received:credited);
  }
  /* Группировка периода: по людям (без имени — в конце) или по датам события
     (по возрастанию, без даты — в конце). Итог группы — сумма её предоплат. */
  function prepaymentGroups(rows,by){
    const groups=new Map();
    for(const row of rows||[]){
      const guest=String(row.guest||'').trim();
      const key=by==='dates'?(row.event_day||''):guest.toLocaleLowerCase('ru');
      if(!groups.has(key))groups.set(key,{key,title:by==='dates'?(row.event_day||''):guest,rows:[],total:0});
      const entry=groups.get(key);
      entry.rows.push(row);
      entry.total=Math.round((entry.total+(prepaymentAmount(row,'period')||0))*100)/100;
    }
    return [...groups.values()].sort((a,b)=>!a.key?1:!b.key?-1:a.key.localeCompare(b.key,'ru'));
  }

  /* Первая загрузка дня (T-393): экран собирается из независимых частей.
     Цифра — скелетом, пока не пришла хотя бы одна часть, из которых она
     считается: «К передаче» ждёт и iiko, и расходы, и поступления, и Шоха. */
  const PARTS=['day','expenses','receipts','shokh','usd','rate','prepay'];
  const SKELETON={
    revenue:['day'],receipts:['day'],average:['day'],'real-cash':['day'],'payment-total':['day'],
    'prepay-received':['prepay'],'prepay-credited':['day'],'prepay-total':['prepay'],
    handover:['day','expenses','receipts','shokh'],'demo-cash':['day'],'cash-prepay':['day'],
    'handover-receipts':['receipts'],'handover-expenses':['expenses','shokh'],
    'expense-total':['expenses','shokh'],'receipt-total':['receipts'],
    'usd-today':['usd'],'usd-safe':['usd'],'usd-official':['rate'],'usd-restaurant':['rate'],
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
