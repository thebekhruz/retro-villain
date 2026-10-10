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
  // Часовой пояс устройства не меняет рабочий день ресторана.
  const calendar=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Tashkent',year:'numeric',month:'2-digit',day:'2-digit'});
  function tashkentDay(now){
    const parts=Object.fromEntries(calendar.formatToParts(now).map(part=>[part.type,part.value]));
    return parts.year+'-'+parts.month+'-'+parts.day;
  }
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
  /* Посещаемость смены (ТЗ 09.10, Б-04): сервер кладёт её в клетку выплаты —
     status on_time / late / absent / unknown / manual, time — первый вход,
     source — статус «Сотрудников». Её не трогает ни выдача, ни снятие выдачи:
     снял галочку — в клетке снова то, что прислал сервер. */
  function attendanceOf(person,day){const value=person.cells?.[day]?.attendance;return value&&value.status?value:null;}
  /* Невыданная клетка: время входа, «нет» — не пришёл, «?» — нет данных или
     привязки, «был» — отмечен вручную. Пусто — о смене сказать нечего. */
  const MARKS={absent:'нет',unknown:'?',manual:'был'};
  function shiftMark(attendance){
    if(!attendance)return '';
    if(attendance.status==='on_time'||attendance.status==='late')return attendance.time||'';
    return MARKS[attendance.status]||'';
  }
  /* Смена словами — подсказка и подпись для чтения с экрана: статус не только цветом. */
  function shiftText(attendance){
    if(!attendance)return '';
    const time=attendance.time;
    if(attendance.status==='on_time')return time?'пришёл '+time+', вовремя':'пришёл вовремя';
    if(attendance.status==='late')return time?'пришёл '+time+', опоздал':'опоздал';
    if(attendance.status==='absent')return attendance.source==='manual_absent'?'не был, отмечено вручную':'не пришёл';
    if(attendance.status==='manual')return 'был, отмечено вручную';
    if(attendance.status==='unknown')return attendance.source==='unlinked'?'нет привязки к Hikvision':'нет данных Hikvision';
    return '';
  }
  const money=new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2});
  const dm=day=>day.slice(8,10)+'.'+day.slice(5,7);
  /* «Смена 08.10: пришёл 09:31, вовремя» — и после выдачи: «… · выдано 360 000 сум». */
  function shiftTitle(day,attendance,amount){
    const text=shiftText(attendance);
    if(!text)return '';
    return 'Смена '+dm(previousDay(day))+': '+text+(amount?' · выдано '+money.format(amount)+' сум':'');
  }
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
  /* Временный (T-434): пометка «временный · 08.10–10.10» — период подписывает сервер. */
  function typeTag(person){return person.temporary?(person.work_period?'временный · '+person.work_period:'временный'):'';}
  /* Смена клетки вне периода временного: клетка заперта и пуста, как будущая;
     подсказка — «Работает 08.10–10.10» («с 08.10», «по 10.10»). */
  function isOutside(person,day){return !!person.cells?.[day]?.outside;}
  function outsideTitle(person){return person.work_period?'Работает '+person.work_period:'';}
  return {parseAmount,previousDay,tashkentDay,shiftMonth,canEdit,rateOf,cellState,toggleTarget,attendanceOf,shiftMark,shiftText,shiftTitle,matrix,extraOf,
    typeTag,isOutside,outsideTitle};
});
