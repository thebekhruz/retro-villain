/* Расчёты закупа без DOM: шаги, итог покупки, сравнение с обычной ценой и
   предпросмотр опыта. Награды повторяют серверные (modules/shokh/gamification),
   потому что экран обещает их до отправки. */
(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.ShokhLogic=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){

  const STEPS=['point','item','amount','confirm'];
  const XP_PURCHASE=30, XP_PHOTO=10, XP_FAIR_PRICE=10, XP_FAST_TRIP=50;
  const FAST_TRIP_MINUTES=15;

  function number(value){
    const text=String(value==null?'':value).replace(',','.').trim();
    if(!text)return null;
    const parsed=Number(text);
    return Number.isFinite(parsed)&&parsed>0?parsed:null;
  }

  function total(draft){
    const quantity=number(draft.quantity), price=number(draft.price);
    return quantity===null||price===null?null:Math.round(quantity*price*100)/100;
  }

  /* Шаг готов — можно идти дальше. Сумму на последнем шаге не проверяем
     повторно: она посчитана из тех же полей. */
  function stepReady(step,draft){
    if(step==='point')return !!(draft.point||'').trim();
    if(step==='item')return !!(draft.item||'').trim();
    if(step==='amount')return total(draft)!==null;
    return total(draft)!==null;
  }

  function nextStep(step){
    const index=STEPS.indexOf(step);
    return index<0||index===STEPS.length-1?step:STEPS[index+1];
  }
  function previousStep(step){
    const index=STEPS.indexOf(step);
    return index<=0?step:STEPS[index-1];
  }

  /* Сравнение с обычной ценой. Дороже — не запрет, а повод бухгалтеру
     проверить; дешевле и «как обычно» одинаково хороши. */
  function priceHint(draft,usual){
    const price=number(draft.price);
    if(price===null)return {kind:'empty',text:''};
    if(usual==null)return {kind:'unknown',text:'Раньше не покупали — цену не с чем сравнить'};
    const reference=Number(usual);
    if(!Number.isFinite(reference)||reference<=0)return {kind:'unknown',text:''};
    const delta=Math.round((price/reference-1)*1000)/10;
    if(delta>0)return {kind:'above',delta,text:'Дороже обычного на '+delta+'% — бухгалтер проверит'};
    if(delta<0)return {kind:'below',delta,text:'Дешевле обычного на '+Math.abs(delta)+'%'};
    return {kind:'same',delta:0,text:'Как обычно'};
  }

  /* Сколько опыта даст покупка. Премию за цену считаем только когда есть с чем
     сравнивать — как на сервере. */
  function xpPreview(draft,usual){
    const parts=[{label:'Покупка',xp:XP_PURCHASE}];
    if(draft.hasPhoto)parts.push({label:'Фото',xp:XP_PHOTO});
    const hint=priceHint(draft,usual);
    if(usual!=null&&(hint.kind==='below'||hint.kind==='same'))
      parts.push({label:'Цена в норме',xp:XP_FAIR_PRICE});
    return {parts,total:parts.reduce((sum,part)=>sum+part.xp,0)};
  }

  /* Сколько осталось на руках после покупки. Может уйти в минус — значит,
     записали больше, чем выдали, и это надо увидеть, а не спрятать. */
  function pocketAfter(pocket,draft){
    if(pocket==null)return null;
    const sum=total(draft);
    return sum===null?Number(pocket):Math.round((Number(pocket)-sum)*100)/100;
  }

  function tripElapsedMinutes(startedAt,now){
    if(!startedAt)return null;
    const started=new Date(startedAt).getTime(), current=new Date(now).getTime();
    if(!Number.isFinite(started)||!Number.isFinite(current))return null;
    return Math.max(0,(current-started)/60000);
  }
  function tripOnTime(startedAt,now){
    const minutes=tripElapsedMinutes(startedAt,now);
    return minutes!==null&&minutes<=FAST_TRIP_MINUTES;
  }
  function clock(minutes){
    if(minutes==null)return '—';
    const whole=Math.floor(minutes);
    return String(whole).padStart(2,'0')+':'+String(Math.floor((minutes-whole)*60)).padStart(2,'0');
  }

  /* Доля отчитанных денег: сколько из выданного уже объяснено покупками.
     Без подотчёта доли нет — Number(null) даёт ноль, и «отчитались за 0%»
     выглядело бы как факт вместо «подотчёт не заведён». */
  function reportedShare(pocket,pending){
    if(pocket==null||pending==null)return null;
    const left=Number(pocket), waiting=Number(pending);
    if(!Number.isFinite(left)||!Number.isFinite(waiting))return null;
    const advanced=left+waiting;
    return advanced>0?Math.round(waiting/advanced*100):0;
  }

  return {STEPS,FAST_TRIP_MINUTES,XP_FAST_TRIP,number,total,stepReady,nextStep,previousStep,
          priceHint,xpPreview,pocketAfter,tripElapsedMinutes,tripOnTime,clock,reportedShare};
});
