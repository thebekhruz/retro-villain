(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.FounderLogic=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  function requestGate(){
    let current=0;
    return {next(){current+=1;return current},isCurrent(id){return id===current}};
  }

  function iso(date){return date.toISOString().slice(0,10)}

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

  return {quickPeriod,revenuePaths,requestGate};
});
