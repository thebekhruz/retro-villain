/* Расчёты экрана «Финансы дня» без DOM: их легче проверить тестом, чем
   выковыривать из отрисовки. Сервер отдаёт факты, здесь — только их сборка. */
(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.AccountantLogic=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){

  /* Подотчёт Шоха за день.
     `entries` приходит только за выбранный день, а `balance` — уже на его
     конец, поэтому остаток на начало восстанавливаем из конца минус движения
     дня, а не суммированием прошлых записей (их в ответе нет). */
  function shohPosition(shoh){
    const entries=shoh&&shoh.entries?shoh.entries:[];
    const sum=kind=>entries.filter(e=>e.kind===kind)
      .reduce((total,e)=>total+Number(e.amount),0);
    const given=sum('deposit'), accepted=sum('withdrawal');
    const known=shoh&&shoh.balance!==null&&shoh.balance!==undefined;
    const balance=known?Number(shoh.balance):null;
    return {known,given,accepted,balance,start:known?balance-given+accepted:null};
  }

  /* Строки секции «Смена».
     Смену за вчера выдают сегодня, поэтому непогашенные начисления прошлых
     дней показываем всегда — даже когда начисления выбранного дня ещё не
     подтверждены. Итоги «за смену» считаем только по выбранному дню, иначе
     день выглядел бы дороже, чем в нём начислили. */
  function shiftRows(data){
    const ledger=data.ledger, day=data.date, confirmed=!!ledger.payroll_confirmed;
    const employees=data.employees||[], accruals=ledger.accruals||[];
    const byName=new Map(employees.map(row=>[row.name,row]));
    const fromAccrual=row=>{
      const employee=byName.get(row.name)||{};
      const own=row.work_day===day;
      return {
        id:row.id, name:row.name, role:employee.role||row.group||'',
        status:row.status||employee.status,
        // Время входа известно только для выбранного дня: реестр приходит за него.
        first_entry:own?(employee.first_entry===undefined?null:employee.first_entry):null,
        showEntry:own, rate:row.rate, accrued:row.amount, paid:row.paid, debt:row.debt,
        day:row.work_day, noHik:employee.hikvision_registered===false,
      };
    };
    const stale=accruals.filter(row=>row.work_day<day&&Number(row.debt)>0)
      .sort((a,b)=>a.work_day.localeCompare(b.work_day)||a.name.localeCompare(b.name,'ru'))
      .map(fromAccrual);
    const own=confirmed
      ?accruals.filter(row=>row.work_day===day)
        .sort((a,b)=>a.name.localeCompare(b.name,'ru')).map(fromAccrual)
      :employees.map(row=>({
        id:null, name:row.name, role:row.role, status:row.status, first_entry:row.first_entry,
        showEntry:true, rate:row.rate, accrued:row.payable, paid:null, debt:null,
        day:day, noHik:row.hikvision_registered===false,
      }));
    const rows=stale.concat(own);
    const total=(list,key)=>list.reduce((sum,row)=>sum+Number(row[key]||0),0);
    return {confirmed, stale, own, rows, totals:{
      accrued:total(own,'accrued'), paid:total(own,'paid'),
      owed:total(rows,'debt'), staleOwed:total(stale,'debt'),
      settled:own.filter(row=>Number(row.debt)===0).length, count:own.length,
    }};
  }

  /* Срезы-фильтры над строками смены. */
  function shiftTabs(rows){
    return [
      ['all','Все',rows.length],
      ['owed','К выдаче',rows.filter(r=>Number(r.debt)>0).length],
      ['late','Опоздали',rows.filter(r=>r.status==='late').length],
      ['missing','Не пришли',rows.filter(r=>r.status==='missing').length],
      ['norate','Без ставки',rows.filter(r=>r.rate===null).length],
    ].filter(([key,,count])=>key==='all'||count>0);
  }

  function matchesTab(row,tab){
    if(tab==='all')return true;
    if(tab==='owed')return Number(row.debt)>0;
    if(tab==='norate')return row.rate===null;
    return row.status===tab;
  }

  /* Проверки дня.
     Сознательно НЕ проверяем переплату, выдачу поверх нулевого начисления и
     сумму мимо ставки: сервер их не пропускает (выплата больше долга — 422, у
     «не пришёл» начисление 0), а ставка и сумма пишутся в начисление одним
     куском. «Зарплата без привязки» тоже не ошибка — так проходят оклады. */
  function dayChecks(data){
    const ledger=data.ledger, items=[];
    const add=(level,text,sub,accrualId)=>items.push({level,text,sub,
      accrualId:accrualId===undefined?null:accrualId});
    if(ledger.cash_balance!==null&&Number(ledger.cash_balance)<0)
      add('bad','Остаток ушёл в минус',{amount:ledger.cash_balance});
    const stale=(ledger.accruals||[]).filter(row=>row.work_day<data.date&&Number(row.debt)>0);
    if(stale.length)
      add('warn','Невыданные смены',{count:stale.length,
        amount:stale.reduce((sum,row)=>sum+Number(row.debt),0)},stale[0].id);
    if(data.missing_rates)
      add('warn','Без ставки',{count:data.missing_rates});
    if(!ledger.payroll_confirmed&&data.payroll.unknown_count===0&&(data.employees||[]).length)
      add('warn','Смена не подтверждена',{});
    if(Number(ledger.manual_debt_total)>0)
      add('warn','Неоплаченные расходы',{amount:ledger.manual_debt_total});
    if(data.expected_cashier===null)
      add('warn','Касса не передана',{});
    return items;
  }

  /* Выплату нельзя записать без данных кассира за день — сервер откажет,
     поэтому строки запираем заранее, вместе с «Выдать всем». */
  function payLock(ledger,row){
    if(ledger.cash_balance===null)return 'no-cash';
    if(row&&!(Number(row.debt)>0))return 'settled';
    return null;
  }

  return {shohPosition,shiftRows,shiftTabs,matchesTab,dayChecks,payLock};
});
