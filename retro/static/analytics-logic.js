/* Чистые функции модуля «Аналитика» (T-524): даты окон, подписи, сравнение и
   геометрия линейного графика. Без DOM — проверяются node --test. */
(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.AnalyticsLogic=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  const money=new Intl.NumberFormat('ru-RU',{maximumFractionDigits:0});
  const axisNumber=new Intl.NumberFormat('ru-RU',{maximumFractionDigits:1});
  const percentNumber=new Intl.NumberFormat('ru-RU',{minimumFractionDigits:1,maximumFractionDigits:1});
  const MINUS='−';

  function number(value){
    if(value===null||value===undefined||value==='')return null;
    const parsed=Number(value);
    return Number.isFinite(parsed)?parsed:null;
  }

  function shiftIso(iso,days){
    const date=new Date(iso+'T00:00:00Z');
    date.setUTCDate(date.getUTCDate()+days);
    return date.toISOString().slice(0,10);
  }

  // Окно по умолчанию заканчивается вчера: сегодняшний день в iiko ещё идёт.
  function defaultEnd(todayIso){return shiftIso(todayIso,-1)}
  // Период из N дней по `endIso` включительно.
  function rangeOfDays(endIso,days){return {start:shiftIso(endIso,-(days-1)),end:endIso}}
  function daysBetween(startIso,endIso){return Math.round((Date.parse(endIso+'T00:00:00Z')-Date.parse(startIso+'T00:00:00Z'))/864e5)+1}
  const MAX_DAYS=92;
  function periodError(startIso,endIso,todayIso){
    if(!startIso||!endIso)return 'Укажите обе даты периода.';
    if(startIso>endIso)return 'Дата начала должна быть не позже даты конца.';
    if(todayIso&&endIso>todayIso)return 'Будущие даты недоступны.';
    if(daysBetween(startIso,endIso)>MAX_DAYS)return `Период — не длиннее ${MAX_DAYS} дней.`;
    return null;
  }
  // Какие дни подписывать на оси: не больше `limit` подписей, первая и последняя всегда.
  function axisIndexes(count,limit=8){
    if(count<=limit)return Array.from({length:count},(_,i)=>i);
    const step=Math.ceil((count-1)/(limit-1)),picked=[];
    for(let i=0;i<count-1;i+=step)picked.push(i);
    if(count-1-picked[picked.length-1]<step/2)picked.pop();
    picked.push(count-1);
    return picked;
  }

  function formatMoney(value){const parsed=number(value);return parsed===null?'—':money.format(parsed)}

  function signedMoney(value){
    const parsed=number(value);
    if(parsed===null)return '—';
    if(parsed===0)return '0';
    return (parsed>0?'+':MINUS)+money.format(Math.abs(parsed));
  }

  function compactMoney(value){
    const parsed=number(value)||0,abs=Math.abs(parsed);
    if(abs>=1e9)return axisNumber.format(parsed/1e9)+' млрд';
    if(abs>=1e6)return axisNumber.format(parsed/1e6)+' млн';
    if(abs>=1e3)return axisNumber.format(parsed/1e3)+' тыс';
    return money.format(parsed);
  }

  function formatPercent(value){
    const parsed=number(value);
    if(parsed===null)return '—';
    if(parsed===0)return '0 %';
    return (parsed>0?'+':MINUS)+percentNumber.format(Math.abs(parsed))+' %';
  }

  function shortDate(iso){return iso?iso.slice(8,10)+'.'+iso.slice(5,7):''}
  function fullDate(iso){return iso?shortDate(iso)+'.'+iso.slice(0,4):''}
  // Диапазон другого года подписываем с годом: «16.10 – 22.10» год назад вводит в заблуждение.
  function rangeLabel(start,end,todayIso){
    const otherYear=todayIso&&start&&start.slice(0,4)!==todayIso.slice(0,4);
    return otherYear?fullDate(start)+' – '+fullDate(end):shortDate(start)+' – '+shortDate(end);
  }

  /* Фраза сравнения: «выросла на 12,3 % (+1 234 567 сум)». Без процента (база
     нулевая) остаётся сумма; без данных — так и говорим, а не рисуем ноль. */
  function trend(comparison){
    if(!comparison||comparison.diff===null||comparison.diff===undefined){
      return {tone:'unknown',verb:'нет данных для сравнения',amount:'—',percent:'—',sentence:'Нет данных для сравнения.'};
    }
    const diff=number(comparison.diff)||0,percent=number(comparison.percent);
    const tone=diff>0?'up':diff<0?'down':'flat';
    const verb=tone==='up'?'выросла':tone==='down'?'упала':'не изменилась';
    const amount=signedMoney(diff)+' сум';
    const percentText=formatPercent(percent);
    let sentence;
    if(tone==='flat')sentence='Выручка не изменилась.';
    else if(percent===null)sentence=`Выручка ${verb} на ${money.format(Math.abs(diff))} сум; раньше продаж не было, процент не считается.`;
    else sentence=`Выручка ${verb} на ${percentNumber.format(Math.abs(percent))} % (${amount}).`;
    return {tone,verb,amount,percent:percentText,sentence};
  }

  // «Красивый» потолок оси: 1, 2, 2.5, 5 × 10^n, не ниже максимума.
  function niceCeil(value){
    if(!(value>0))return 1;
    const power=Math.pow(10,Math.floor(Math.log10(value)));
    for(const step of [1,2,2.5,5,10]){if(value<=step*power)return step*power}
    return 10*power;
  }

  /* Точки линии по индексам 0…n-1; пропуск (null) рвёт линию на отрезки,
     а не тянет её через ноль. */
  function linePoints(values,max,plotWidth,plotHeight){
    const count=values.length,step=count>1?plotWidth/(count-1):0;
    return values.map((value,index)=>{
      const parsed=number(value);
      return {index,x:count>1?index*step:plotWidth/2,
        y:parsed===null?null:plotHeight-(max>0?parsed/max:0)*plotHeight,value:parsed};
    });
  }

  function lineSegments(points){
    const segments=[];let current=[];
    points.forEach(point=>{
      if(point.y===null){if(current.length)segments.push(current);current=[];return}
      current.push(point);
    });
    if(current.length)segments.push(current);
    return segments.map(segment=>segment.map(point=>`${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' '));
  }

  function nearestIndex(x,plotWidth,count){
    if(count<=1)return 0;
    const step=plotWidth/(count-1);
    return Math.max(0,Math.min(count-1,Math.round(x/step)));
  }

  function updatedLabel(data){
    if(!data)return '';
    const stamp=data.updated_at?data.updated_at.slice(11,16):null;
    if(data.includes_today)return stamp?`Сегодня ещё идёт · данные на ${stamp}`:'Сегодня ещё идёт · нет данных';
    if(!data.updated_at)return 'Нет загруженных данных';
    return `Обновлено ${shortDate(data.updated_at)}, ${stamp}`;
  }

  // Подпись дня оси: «пн 15.10» по текущему окну.
  function dayLabel(day){return day?`${day.weekday} ${shortDate(day.day)}`:''}

  function coverageLabel(period){
    if(!period)return '';
    if(period.days_with_data===0)return 'нет данных';
    const partial=period.days.some(day=>day.partial);
    if(period.complete)return `${period.days_with_data} из ${period.days.length} дн.`;
    return `${period.days_with_data} из ${period.days.length} дн.`+(partial?' · есть незакрытые':'');
  }

  return {axisIndexes,coverageLabel,compactMoney,dayLabel,daysBetween,defaultEnd,formatMoney,formatPercent,fullDate,linePoints,lineSegments,
    nearestIndex,niceCeil,periodError,rangeLabel,rangeOfDays,shiftIso,shortDate,signedMoney,trend,updatedLabel};
});
