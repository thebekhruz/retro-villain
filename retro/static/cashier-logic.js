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

  function cashPayment(snapshot){
    if(!snapshot||!snapshot.payments)return null;
    const row=snapshot.payments.find(item=>item.name===CASH_PAYMENT);
    return row?amount(row.amount):0;
  }

  /* К передаче: наличные из iiko + предоплаты наличными + прочие поступления
     − расходы наличными. Пока какой-то части нет, суммы не показываем: неполная
     цифра тут хуже прочерка. */
  function handover(snapshot,expenseTotal,receiptTotal){
    const cash=cashPayment(snapshot);
    if(cash===null||expenseTotal===null||receiptTotal===null)return null;
    return cash+amount(snapshot.cash_prepayment)+amount(receiptTotal)-amount(expenseTotal);
  }

  /* Весь приход смены: касса по регистру, если он есть, иначе продажи плюс
     новые предоплаты — плюс внесённые руками поступления. */
  function totalInflow(snapshot,receiptTotal){
    if(!snapshot||receiptTotal===null)return null;
    const register=snapshot.register_received_total;
    const base=register!==null&&register!==undefined
      ?amount(register)
      :amount(snapshot.revenue)+amount(snapshot.new_prepayment);
    return base+amount(receiptTotal);
  }

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
    if(!record)return {state:'none',difference:null};
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

  return {CASH_PAYMENT,cashPayment,handover,totalInflow,composition,shiftLabel,handoverView,parseAmount};
});
