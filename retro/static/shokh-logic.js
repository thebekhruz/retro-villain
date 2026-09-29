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

  /* Десятичная строка → целое число в долях: «1,5» с places=3 → 1500n.
     Пробелы (в том числе неразрывные) — разделители разрядов. Больше знаков
     после запятой, чем places, — null: сервер такое не примет. */
  function scaled(value,places){
    const text=String(value==null?'':value).replace(/[\s\u00a0\u202f]/g,'').replace(',','.');
    const match=/^(\d+)(?:\.(\d*))?$/.exec(text);
    if(!match||(match[2]||'').length>places)return null;
    const units=BigInt(match[1]+(match[2]||'').padEnd(places,'0'));
    return units>0n?units:null;
  }
  /* Итог покупки так, как его сохранит сервер и запишет накладная iiko:
     количество в тысячных, цена в тийинах, произведение округляется до тийина
     половиной вверх (ROUND_HALF_UP в modules/shokh). Целые BigInt — чтобы
     крупные закупы не теряли тийины на плавающей точке. */
  function serverTotal(thousandths,tiyin){
    return (thousandths*tiyin+500n)/1000n;
  }
  function tiyinText(tiyin){
    const whole=tiyin/100n, part=tiyin%100n;
    return part===0n?String(whole):whole+'.'+String(part).padStart(2,'0');
  }

  function total(draft){
    const quantity=scaled(draft.quantity,3), price=scaled(draft.price,2);
    if(quantity===null||price===null)return null;
    return Number(serverTotal(quantity,price))/100;
  }

  /* Цена «за всё»: человек вводит сумму покупки, а сервер принимает цену за
     единицу (до двух знаков) и сам считает итог. Ищем цену, при которой итог
     сервера совпадёт с введённой суммой. Если такой нет (100 000 на 3 кг),
     берём ближайшую и возвращаем итог, который реально уйдёт в накладную, —
     экран и iiko не должны расходиться ни на тийин. */
  function priceFromTotal(quantity,amount){
    const count=scaled(quantity,3), wanted=scaled(amount,2);
    if(count===null||wanted===null)return null;
    // Цены (в тийинах), при которых итог сервера ровно равен введённому,
    // идут подряд: от low до high. Из них берём ближайшую к честному
    // частному — 1 000 за 0,25 кг даёт 4 000, а не «3 999,98».
    const low=(wanted*1000n-500n+count-1n)/count, high=(wanted*1000n+499n)/count;
    const quotient=(wanted*2000n+count)/(count*2n);
    let price;
    if(low<=high&&high>=1n){
      price=quotient<low?low:quotient>high?high:quotient;
      if(price<1n)price=1n;
    }else{
      // Ровно не выходит: соседние цены дают итог чуть меньше и чуть больше.
      // Берём тот, что ближе к введённой сумме; при равенстве — меньший.
      price=low;
      if(high>=1n&&wanted-serverTotal(count,high)<=serverTotal(count,low)-wanted)price=high;
      if(price<1n)price=1n;
    }
    const result=serverTotal(count,price);
    return {price:tiyinText(price),total:Number(result)/100,entered:Number(wanted)/100,exact:result===wanted};
  }

  /* Что мешает сохранить покупку, хотя цифры введены: сервер принимает
     количество до тысячных, цену до тийинов и покупку не больше миллиарда. */
  function amountProblem(draft){
    const quantity=String(draft.quantity==null?'':draft.quantity).trim();
    const price=String(draft.price==null?'':draft.price).trim();
    if(quantity&&number(quantity)!==null&&scaled(quantity,3)===null)
      return 'Количество — не больше трёх знаков после запятой';
    if(price&&number(price.replace(/[\s\u00a0\u202f]/g,''))!==null&&scaled(price,2)===null)
      return 'Цена — не больше двух знаков после запятой';
    const sum=total(draft);
    if(sum!==null&&sum>1000000000)return 'Слишком большая сумма покупки';
    return '';
  }

  /* Шаг готов — можно идти дальше. Сумму на последнем шаге не проверяем
     повторно: она посчитана из тех же полей. */
  function stepReady(step,draft){
    if(step==='point')return !!(draft.point||'').trim();
    if(step==='item')return !!(draft.item||'').trim();
    if(step==='amount')return total(draft)!==null&&!amountProblem(draft);
    return total(draft)!==null&&!amountProblem(draft);
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
    const price=number(String(draft.price==null?'':draft.price).replace(/[\s\u00a0\u202f]/g,''));
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

  return {STEPS,FAST_TRIP_MINUTES,XP_FAST_TRIP,number,total,priceFromTotal,amountProblem,stepReady,
          nextStep,previousStep,priceHint,xpPreview,pocketAfter,tripElapsedMinutes,tripOnTime,clock,reportedShare};
});
