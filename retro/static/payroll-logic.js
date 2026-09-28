/* Расчёты ведомости месяца без DOM. Сетка «сотрудник × день» — это только
   раскладка присланных начислений, поэтому её удобнее проверять тестом. */
(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.PayrollLogic=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){

  /* Состояние ячейки дня.
     Пустая ячейка — смены не было: начисления за этот день у человека нет.
     Иначе показываем, выдано ли начисленное, и не забываем статус прохода:
     «не пришёл» — это ноль по делу, а не невыданный долг. */
  function cellState(cell){
    if(!cell)return {kind:'empty'};
    const amount=Number(cell.amount), paid=Number(cell.paid), debt=Number(cell.debt);
    if(cell.status==='missing'&&amount===0)return {kind:'missing',amount,paid,debt};
    if(debt<=0)return {kind:'paid',amount,paid,debt};
    if(paid>0)return {kind:'partial',amount,paid,debt};
    return {kind:cell.status==='late'?'late':'owed',amount,paid,debt};
  }

  /* Сетка: на каждого человека — ячейка под каждый день месяца. */
  function sheet(data){
    const days=data.days||[];
    return (data.shift||[]).map(person=>({
      name:person.name, group:person.group, rate:person.rate,
      accrued:person.accrued, paid:person.paid, debt:person.debt,
      cells:days.map(day=>{
        const cell=person.cells?person.cells[day]:null;
        return Object.assign({day,accrualId:cell?cell.accrual_id:null},cellState(cell));
      }),
    }));
  }

  /* Итоги месяца. Оклады идут отдельной строкой: они не начисляются сменами и
     в данных не разложены по сотрудникам и дням. */
  function totals(data){
    const sum=key=>(data.shift||[]).reduce((total,person)=>total+Number(person[key]||0),0);
    const accrued=sum('accrued'), paid=sum('paid'), debt=sum('debt');
    const people=(data.shift||[]).length;
    const owing=(data.shift||[]).filter(person=>Number(person.debt)>0).length;
    return {accrued,paid,debt,people,owing,
      monthlyPaid:Number(data.monthly_paid||0),
      monthlyFund:Number(data.monthly_total||0),
      monthlyPeople:(data.monthly||[]).length};
  }

  /* Суммы, выданные из кассы в каждый день месяца. */
  function dayTotals(data){
    const perDay=data.paid_per_day||{};
    return (data.days||[]).map(day=>({day,amount:Number(perDay[day]||0)}));
  }

  function shiftMonth(month,step){
    const [year,index]=month.split('-').map(Number);
    const moved=new Date(Date.UTC(year,index-1+step,1));
    return moved.toISOString().slice(0,7);
  }

  return {cellState,sheet,totals,dayTotals,shiftMonth};
});
