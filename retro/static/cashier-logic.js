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

  /* Первая загрузка дня (T-393): экран собирается из шести независимых частей.
     Цифра — скелетом, пока не пришла хотя бы одна часть, из которых она
     считается: «К передаче» ждёт и iiko, и расходы, и поступления, и Шоха. */
  const PARTS=['day','expenses','receipts','shokh','usd','rate'];
  const SKELETON={
    'total-inflow':['day','receipts'],composition:['day','receipts'],revenue:['day'],receipts:['day'],average:['day'],
    'card-prepay':['day'],'card-prepay-cash':['day'],'card-prepay-card':['day'],'payments-sub':['day'],'payment-total':['day'],'payments-inflow':['day','receipts'],
    handover:['day','expenses','receipts','shokh'],'demo-cash':['day'],'cash-prepay':['day'],
    'handover-receipts':['receipts'],'handover-expenses':['expenses','shokh'],'receipt-auto-value':['day'],
    'expense-total':['expenses','shokh'],'receipt-total':['receipts'],'shokh-pocket':['shokh'],
    'usd-today':['usd'],'usd-safe':['usd'],'usd-official':['rate'],'usd-restaurant':['rate'],
  };
  /* {id: скелет ли} по набору ещё не пришедших частей. */
  function skeletonMap(waiting){
    const pending=waiting instanceof Set?waiting:new Set(waiting||[]);
    const result={};
    for(const [id,parts] of Object.entries(SKELETON))result[id]=parts.some(part=>pending.has(part));
    return result;
  }

  return {CASH_PAYMENT,composition,shiftLabel,handoverView,parseAmount,PARTS,skeletonMap};
});
