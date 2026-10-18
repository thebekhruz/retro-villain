(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.FounderLogic=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  function requestGate(){
    let current=0;
    return {next(){current+=1;return current},invalidate(){current+=1},isCurrent(id){return id===current}};
  }

  function iso(date){return date.toISOString().slice(0,10)}

  function weekday(dateIso){
    const names=['воскресенье','понедельник','вторник','среда','четверг','пятница','суббота'];
    return names[new Date(dateIso+'T00:00:00Z').getUTCDay()];
  }

  function quickPeriod(kind,todayIso){
    const today=new Date(todayIso+'T00:00:00Z');
    if(kind==='month')return {start:todayIso.slice(0,8)+'01',end:todayIso};
    const days=Number(kind);
    const end=new Date(today);end.setUTCDate(end.getUTCDate()-1);
    const start=new Date(end);start.setUTCDate(start.getUTCDate()-days+1);
    return {start:iso(start),end:iso(end)};
  }

  function revenuePaths(series,directions,width,height){
    const values=series.flatMap(group=>directions.map(direction=>Number(group.values[direction]||0)));
    const minimum=Math.min(0,...values);
    const maximum=Math.max(1,...values);
    const span=maximum-minimum;
    const x=index=>series.length===1?width/2:index*width/(series.length-1);
    const y=value=>height-(value-minimum)*height/span;
    return Object.fromEntries(directions.map(direction=>[
      direction,
      series.map((group,index)=>`${x(index)},${y(Number(group.values[direction]||0))}`).join(' '),
    ]));
  }

  function nearestRevenueIndex(pointerX,width,count){
    if(count<=1)return 0;
    const position=Math.max(0,Math.min(width,Number(pointerX)));
    return Math.round(position/width*(count-1));
  }

  function paymentLineSeries(series,directions,payments){
    return series.map(group=>({
      start:group.start,
      end:group.end,
      incomplete:group.incomplete,
      values:Object.fromEntries(payments.map(payment=>[
        payment,
        directions.reduce((sum,direction)=>sum+Number(group.directions[direction]?.[payment]||0),0),
      ])),
    }));
  }

  const DAY_MS=864e5;
  const DIRECTIONS=['retro','school','banquet'];
  const GRANULARITIES=['day','week','month'];
  const QUICK_PERIODS=['7','30','month'];
  // Больше стольких дней в одном ряду столбцы становятся спичками: ряд
  // собирается по неделям сам, детализация в фильтре не меняется.
  const MAX_DAY_BARS=60;

  function addDays(dateIso,days){const date=new Date(dateIso+'T00:00:00Z');date.setUTCDate(date.getUTCDate()+days);return iso(date)}
  function spanDays(start,end){return Math.round((Date.parse(end+'T00:00:00Z')-Date.parse(start+'T00:00:00Z'))/DAY_MS)+1}
  function weekStart(dateIso){const shift=(new Date(dateIso+'T00:00:00Z').getUTCDay()+6)%7;return addDays(dateIso,-shift)}
  function monthDays(dateIso){const date=new Date(dateIso.slice(0,8)+'01T00:00:00Z');date.setUTCMonth(date.getUTCMonth()+1);date.setUTCDate(0);return date.getUTCDate()}

  /** Сколько дней корзины попало в период и сколько в ней бывает: первая и
   *  последняя неделя или месяц периода обычно неполные. */
  function bucketCoverage(start,end,unit){
    const days=spanDays(start,end);
    const full=unit==='week'?7:unit==='month'?monthDays(start):1;
    return {days,full,partial:days<full};
  }

  /** Столбцы выручки с накоплением по направлениям.
   *  Направления не пересекаются, поэтому высота столбца — «Всего». Длинный
   *  ряд по дням собирается по неделям (понедельник — воскресенье, края
   *  обрезаны периодом). */
  function revenueBars(series,directions,{granularity='day',maxBars=MAX_DAY_BARS}={}){
    let unit=granularity,groups=series;
    if(granularity==='day'&&series.length>maxBars){
      unit='week';const byWeek=new Map();
      series.forEach(group=>{
        const key=weekStart(group.start);
        if(!byWeek.has(key))byWeek.set(key,{start:group.start,end:group.end,incomplete:false,values:{}});
        const week=byWeek.get(key);week.end=group.end;week.incomplete=week.incomplete||Boolean(group.incomplete);
        directions.forEach(direction=>{week.values[direction]=(week.values[direction]||0)+Number(group.values[direction]||0)});
      });
      groups=[...byWeek.values()];
    }
    const bars=groups.map(group=>{
      const values=Object.fromEntries(directions.map(direction=>[direction,Number(group.values[direction]||0)]));
      return {start:group.start,end:group.end,incomplete:Boolean(group.incomplete),...bucketCoverage(group.start,group.end,unit),
        values,total:directions.reduce((sum,direction)=>sum+values[direction],0)};
    });
    return {unit,regrouped:unit!==granularity,bars};
  }

  /** Отрезки столбца снизу вверх: положительные суммы над нулём, возвраты
   *  (отрицательные) — под ним, чтобы ничего не наезжало. */
  function barStack(bar,directions){
    let up=0,down=0;
    return directions.map(direction=>{
      const value=bar.values[direction]||0;
      if(value>=0){const part={direction,from:up,to:up+value};up+=value;return part}
      const part={direction,from:down+value,to:down};down+=value;return part;
    });
  }

  /** Круглая верхняя граница шкалы. Шаги частые, чтобы столбцы занимали
   *  высоту графика: при 58 млн шкала до 60, а не до 100. Четверть любого
   *  шага — тоже круглое число (подписи сетки). */
  function niceCeil(value){
    if(!(value>0))return 1;
    const power=10**Math.floor(Math.log10(value)),scaled=value/power;
    const step=[1,1.2,1.6,2,2.4,3,4,5,6,8,10].find(item=>scaled<=item+1e-9);
    return step*power;
  }

  function barScale(bars,directions){
    let max=0,min=0;
    bars.forEach(bar=>barStack(bar,directions).forEach(part=>{max=Math.max(max,part.to);min=Math.min(min,part.from)}));
    return {max:niceCeil(max),min:min<0?-niceCeil(-min):0};
  }

  /** Столбец под указателем: каждый столбец владеет своей полосой ширины. */
  function barIndexAt(pointerX,width,count){
    if(count<=1)return 0;
    const position=Math.max(0,Math.min(width-1e-6,Number(pointerX)));
    return Math.min(count-1,Math.floor(position/(width/count)));
  }

  function isoDay(value){
    if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value))return false;
    const parsed=new Date(value+'T00:00:00Z');
    return !Number.isNaN(parsed.getTime())&&iso(parsed)===value;
  }

  /** Фильтры из адреса страницы: ссылкой можно поделиться, а перезагрузка
   *  не сбрасывает выбор. Быстрый период пересчитывается от сегодняшней даты. */
  function filtersFromQuery(search,todayIso){
    const params=new URLSearchParams(search||'');
    const granularity=GRANULARITIES.includes(params.get('granularity'))?params.get('granularity'):'day';
    const raw=params.get('directions');
    const picked=raw===null?DIRECTIONS:DIRECTIONS.filter(direction=>raw.split(',').includes(direction));
    const directions=picked.length?[...picked]:[...DIRECTIONS];
    const period=params.get('period');
    if(QUICK_PERIODS.includes(period))return {...quickPeriod(period,todayIso),granularity,directions,period};
    const start=params.get('start'),end=params.get('end');
    if(isoDay(start)&&isoDay(end)&&end<=todayIso&&!periodError(start,end))return {start,end,granularity,directions,period:null};
    return {...quickPeriod('30',todayIso),granularity,directions,period:'30'};
  }

  function filtersToQuery({start,end,granularity,directions,period}){
    const params=new URLSearchParams();
    if(period)params.set('period',period);else{params.set('start',start);params.set('end',end)}
    if(granularity&&granularity!=='day')params.set('granularity',granularity);
    const picked=DIRECTIONS.filter(direction=>directions.includes(direction));
    if(picked.length&&picked.length<DIRECTIONS.length)params.set('directions',picked.join(','));
    // Запятая в списке направлений читается в адресе, %2C — нет.
    return params.toString().replace(/%2C/gi,',');
  }

  /** Ошибка периода до запроса — те же правила, что у сервера (422). */
  function periodError(start,end){
    if(!start||!end)return 'Укажите обе даты периода.';
    if(start>end)return 'Дата начала должна быть не позже даты конца.';
    const days=(Date.parse(end+'T00:00:00Z')-Date.parse(start+'T00:00:00Z'))/864e5;
    if(days>=366)return 'Период не может быть длиннее 366 дней.';
    return null;
  }

  /** Подпись времени аналитики. `updated_at` сервера уже по Ташкенту (+05:00) —
     берём из строки, а не через часовой пояс браузера. Период с сегодняшним днём —
     «Сегодня ещё идёт · данные на ЧЧ:ММ» (ТЗ 16.10, п. 2): итог дня ещё растёт. */
  function updatedLabel(data){
    const at=data&&data.updated_at;
    if(!at)return data&&data.includes_current_day?'Сегодня ещё идёт · нет данных':'Нет данных';
    const time=at.slice(11,16),day=at.slice(8,10)+'.'+at.slice(5,7);
    return data.includes_current_day?'Сегодня ещё идёт · данные на '+time:'Обновлено '+day+', '+time;
  }

  return {barIndexAt,barScale,barStack,bucketCoverage,filtersFromQuery,filtersToQuery,nearestRevenueIndex,niceCeil,
    paymentLineSeries,periodError,quickPeriod,revenueBars,revenuePaths,requestGate,updatedLabel,weekday};
});
