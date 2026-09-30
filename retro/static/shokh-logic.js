/* Расчёты закупа без DOM: шаги, итог покупки, сравнение с обычной ценой,
   остаток на руках и время закупа. */
(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.ShokhLogic=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){

  const STEPS=['point','item','amount','confirm'];

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

  /* Наличные за покупку: итог накладной, округлённый до целого сума половиной
     вверх — как shokh.store.cash_amount на сервере. Тийинов в наличных нет:
     3 × 33 333,33 уходит в накладную как 99 999,99, а из кармана — 100 000.
     «На руках после», «Итого» и все суммы денег считаются по нему. */
  function cashTotal(draft){
    const quantity=scaled(draft.quantity,3), price=scaled(draft.price,2);
    if(quantity===null||price===null)return null;
    return Number((serverTotal(quantity,price)+50n)/100n);
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

  /* Сравнение с обычной ценой («Функционал» §3.12, §4): дешевле — «дешевле на
     N%», до +10% — «в норме», выше — «дороже на N% — бухгалтер увидит».
     Порог тот же, что у сервера (store.ABOVE_USUAL): сравниваем в тийинах,
     чтобы округление процента не спорило с флагом «дороже обычного». */
  function priceHint(draft,usual){
    const price=number(String(draft.price==null?'':draft.price).replace(/[\s\u00a0\u202f]/g,''));
    if(price===null)return {kind:'empty',text:''};
    if(usual==null)return {kind:'unknown',text:'Раньше не покупали — цену не с чем сравнить'};
    const reference=Number(usual);
    if(!Number.isFinite(reference)||reference<=0)return {kind:'unknown',text:''};
    const paid=Math.round(price*100), normal=Math.round(reference*100);
    const delta=Math.round((price/reference-1)*100);
    if(paid*10>normal*11)return {kind:'above',delta,text:'Дороже обычного на '+delta+'% — бухгалтер увидит'};
    if(paid<normal)return {kind:'below',delta,text:'Дешевле обычного на '+Math.max(1,Math.abs(delta))+'%'};
    return {kind:'same',delta:Math.max(0,delta),text:'В норме'};
  }

  /* Сколько осталось на руках после покупки. Может уйти в минус — значит,
     записали больше, чем выдали, и это надо увидеть, а не спрятать. */
  function pocketAfter(pocket,draft){
    if(pocket==null)return null;
    const sum=cashTotal(draft);
    return sum===null?Number(pocket):Math.round((Number(pocket)-sum)*100)/100;
  }

  function tripElapsedMinutes(startedAt,now){
    if(!startedAt)return null;
    const started=new Date(startedAt).getTime(), current=new Date(now).getTime();
    if(!Number.isFinite(started)||!Number.isFinite(current))return null;
    return Math.max(0,(current-started)/60000);
  }
  function clock(minutes){
    if(minutes==null)return '—';
    const whole=Math.floor(minutes);
    return String(whole).padStart(2,'0')+':'+String(Math.floor((minutes-whole)*60)).padStart(2,'0');
  }

  const fold=value=>String(value==null?'':value).trim().toLowerCase().replace(/ё/g,'е');

  /* История покупок (/api/shokh/history) → товары справочника iiko: сколько
     раз брали всего и на каждой точке, обычная цена. Товар узнаём по id
     номенклатуры iiko, а записи без id (до связи с iiko, товар не из
     справочника) — по названию. Товары не из справочника, которых в iiko так
     и нет, добавляются отдельными строками с custom: выбор такой строки ведёт
     в «Новый товар» с тем же названием и единицей. */
  function withHistory(items,history){
    const byId=new Map(), byName=new Map();
    (history||[]).forEach(entry=>{
      if(entry.product_id)byId.set(entry.product_id,entry);
      else byName.set(fold(entry.item),entry);
    });
    const used=new Set();
    const rows=(items||[]).filter(row=>!row.custom).map(row=>{
      const found=[byId.get(row.id),byName.get(fold(row.item))].filter(Boolean);
      found.forEach(entry=>used.add(entry));
      const points={};
      let times=0;
      found.forEach(entry=>{
        times+=Number(entry.times)||0;
        Object.entries(entry.points||{}).forEach(([point,count])=>{points[point]=(points[point]||0)+Number(count);});
      });
      const usual=found.length?found[0].usual_price:null;
      return Object.assign({},row,{times,points,usual_price:usual==null?null:usual});
    });
    (history||[]).forEach(entry=>{
      if(used.has(entry)||entry.product_id||!entry.off_catalog)return;
      rows.push({id:null,custom:true,item:entry.item,unit:entry.unit,code:'',times:Number(entry.times)||0,
        points:Object.assign({},entry.points||{}),usual_price:entry.usual_price==null?null:entry.usual_price});
    });
    return rows;
  }

  /* Поиск и «Часто покупаете»: без запроса — сначала то, что Шох брал на этой
     точке, потом то, что брал вообще (чаще — выше), потом остальное по
     алфавиту; с запросом — каждое слово должно встретиться в названии или
     артикуле, в любом порядке. «ё» = «е». */
  function searchItems(items,query,limit,point){
    const words=fold(query).split(/\s+/).filter(Boolean);
    const rows=(items||[]).filter(row=>{
      const haystack=fold(row.item)+' '+fold(row.code);
      return words.every(word=>haystack.includes(word));
    });
    const here=row=>(point&&row.points&&Number(row.points[point]))||0;
    rows.sort((a,b)=>here(b)-here(a)||(Number(b.times)||0)-(Number(a.times)||0)
      ||String(a.item).localeCompare(String(b.item),'ru'));
    return rows.slice(0,limit||10);
  }

  /* Черновик закупа переживает F5 (localStorage, свой на каждый закуп).
     Фото не сохраняем — после восстановления его прикрепляют заново. */
  const DRAFT_FIELDS=['point','item','unit','quantity','price','priceMode','priceInput','hasPhoto',
    'productId','unitId','supplierId','storageId','operationId','custom'];
  const DRAFT_TTL_MS=12*60*60*1000;
  function draftKey(tripId){return 'shokh-draft:'+tripId;}
  function draftSnapshot({tripId,tripStartedAt,date,step,draft},now){
    const saved={};
    DRAFT_FIELDS.forEach(field=>{if(draft&&draft[field]!==undefined)saved[field]=draft[field];});
    return {tripId,tripStartedAt,date,step,draft:saved,savedAt:new Date(now).toISOString()};
  }
  /* Можно ли вернуть черновик: тот же день, закуп ещё открыт на сервере, не
     старше 12 часов и в нём уже что-то выбрано. */
  function restorableDraft(saved,home,now){
    if(!saved||typeof saved!=='object'||!saved.draft||!home)return null;
    if(saved.date!==home.date)return null;
    const open=(home.trips||[]).some(trip=>trip.id===saved.tripId&&!trip.finished_at);
    if(!open)return null;
    const age=new Date(now).getTime()-new Date(saved.savedAt).getTime();
    if(!Number.isFinite(age)||age<0||age>DRAFT_TTL_MS)return null;
    if(!STEPS.includes(saved.step))return null;
    if(!(saved.draft.point||'').trim()&&!(saved.draft.item||'').trim())return null;
    return saved;
  }

  return {STEPS,number,total,cashTotal,priceFromTotal,amountProblem,stepReady,
          nextStep,previousStep,priceHint,pocketAfter,tripElapsedMinutes,clock,searchItems,withHistory,
          draftKey,draftSnapshot,restorableDraft};
});
