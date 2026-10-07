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

  const clamp=day=>day<'2026-10-02'?'2026-10-02':day;
  function quickPeriod(kind,todayIso){
    const today=new Date(todayIso+'T00:00:00Z');
    if(kind==='month')return {start:clamp(todayIso.slice(0,8)+'01'),end:todayIso};
    const days=Number(kind);
    const end=new Date(today);end.setUTCDate(end.getUTCDate()-1);
    const start=new Date(end);start.setUTCDate(start.getUTCDate()-days+1);
    return {start:clamp(iso(start)),end:iso(end)};
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

  /** Ошибка периода до запроса — те же правила, что у сервера (422). */
  function periodError(start,end){
    if(!start||!end)return 'Укажите обе даты периода.';
    if(start<'2026-10-02'||end<'2026-10-02')return 'Учёт доступен со 2 октября 2026.';
    if(start>end)return 'Дата начала должна быть не позже даты конца.';
    const days=(Date.parse(end+'T00:00:00Z')-Date.parse(start+'T00:00:00Z'))/864e5;
    if(days>=366)return 'Период не может быть длиннее 366 дней.';
    return null;
  }

  return {nearestRevenueIndex,paymentLineSeries,periodError,quickPeriod,revenuePaths,requestGate,weekday};
});
