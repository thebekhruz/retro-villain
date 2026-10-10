/* Ручная ведомость сменных: столбец — день выплаты, смена — на день раньше.
   Клетка — галочка: выдано по ставке смены, другая сумма или не выдано. */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.SalaryDayLogic=api;})(typeof globalThis!=='undefined'?globalThis:this,function(){
  function parseAmount(value){
    const clean=String(value??'').replace(/[\s  ]/g,'').replace(',','.');
    if(!clean)return 0;
    if(!/^\d+(\.\d{1,2})?$/.test(clean))return null;
    const amount=Number(clean);
    return Number.isFinite(amount)&&Number.isSafeInteger(Math.round(amount*100))?amount:null;
  }
  function previousDay(day){const date=new Date(day+'T12:00:00Z');date.setUTCDate(date.getUTCDate()-1);return date.toISOString().slice(0,10);}
  function shiftMonth(month,step){const date=new Date(month+'-01T12:00:00Z');date.setUTCMonth(date.getUTCMonth()+step);return date.toISOString().slice(0,7);}
  function canEdit(data,person,day){
    return !data.closed&&!person.archived&&day>=data.entry_start&&day<=data.today
      &&data.days.includes(day)&&person.cells?.[day]?.editable!==false;
  }
  /* Ставка смены: сервер отдаёт её по версии реестра на день смены; если нет —
     нынешняя ставка сотрудника. Ноль и пусто — ставки нет, галочкой не выдать. */
  function rateOf(person,day){
    const cell=person.cells?.[day];
    const raw=cell&&cell.rate!==undefined?cell.rate:person.rate;
    const rate=raw==null?null:parseAmount(raw);
    return rate?rate:null;
  }
  /* off — не выдано, on — выдано ровно по ставке, odd — другая сумма. */
  function cellState(amount,rate){
    if(!amount)return 'off';
    return rate&&Math.round(amount*100)===Math.round(rate*100)?'on':'odd';
  }
  /* Что ставит клик по клетке: выдано → снять (0); не выдано → ставка;
     ставки нет → null (нужно ввести сумму). */
  function toggleTarget(amount,rate){return amount?0:rate||null;}
  /* Доп. выплаты (Б-05) по сотруднику и дню выплаты, в тийинах: {id|день: сумма}. */
  function extraCents(data){
    const sums=new Map();
    (data.extras||[]).forEach(item=>{
      const amount=parseAmount(item.amount);
      if(amount===null)throw new Error('Не удалось прочитать сумму выплаты. Обновите ведомость.');
      const key=item.employee_id+'|'+item.paid_day;sums.set(key,(sums.get(key)||0)+Math.round(amount*100));
    });
    return sums;
  }
  function extraOf(data,personId,day){return (extraCents(data).get(personId+'|'+day)||0)/100;}
  /* Итоги ведомости: клетка — обычная выплата; доп. выплата в клетку не входит,
     но входит в «Выдано» сотрудника и в итог дня выплаты. */
  function matrix(data){
    const perDay=Object.fromEntries(data.days.map(day=>[day,0]));
    const extras=extraCents(data);
    const people=(data.people||[]).map(person=>{
      let paidCents=0,extraTotal=0;
      const cells=data.days.map(day=>{
        const cell=person.cells?.[day];
        const amount=cell?(cell.amount==null?null:parseAmount(cell.amount)):0;
        if(amount===null)throw new Error('Не удалось прочитать сумму выплаты. Обновите ведомость.');
        const extra=extras.get(person.id+'|'+day)||0;
        const cents=Math.round(amount*100);paidCents+=cents+extra;perDay[day]+=cents+extra;extraTotal+=extra;
        const rate=rateOf(person,day);
        return {day,amount,extra:extra/100,rate,state:cellState(amount,rate),workDay:cell?.work_day||previousDay(day),editable:canEdit(data,person,day)};
      });
      return {...person,cells,paid:paidCents/100,extra:extraTotal/100};
    });
    return {people,perDay:Object.fromEntries(Object.entries(perDay).map(([day,value])=>[day,value/100])),
      total:people.reduce((sum,person)=>sum+Math.round(person.paid*100),0)/100};
  }
  return {parseAmount,previousDay,shiftMonth,canEdit,rateOf,cellState,toggleTarget,matrix,extraOf};
});
