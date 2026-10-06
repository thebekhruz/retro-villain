/* Ручная ведомость сменных: столбец — день выплаты, смена — на день раньше. */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.SalaryDayLogic=api;})(typeof globalThis!=='undefined'?globalThis:this,function(){
  function parseAmount(value){
    const clean=String(value??'').replace(/[\s\u00a0\u202f]/g,'').replace(',','.');
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
  function matrix(data){
    const perDay=Object.fromEntries(data.days.map(day=>[day,0]));
    const people=(data.people||[]).map(person=>{
      let paidCents=0;
      const cells=data.days.map(day=>{
        const cell=person.cells?.[day];
        const amount=cell?(cell.amount==null?null:parseAmount(cell.amount)):0;
        if(amount===null)throw new Error('Не удалось прочитать сумму выплаты. Обновите ведомость.');
        const cents=Math.round(amount*100);paidCents+=cents;perDay[day]+=cents;
        return {day,amount,workDay:cell?.work_day||previousDay(day),editable:canEdit(data,person,day)};
      });
      return {...person,cells,paid:paidCents/100};
    });
    return {people,perDay:Object.fromEntries(Object.entries(perDay).map(([day,value])=>[day,value/100])),
      total:people.reduce((sum,person)=>sum+Math.round(person.paid*100),0)/100};
  }
  return {parseAmount,previousDay,shiftMonth,canEdit,matrix};
});
