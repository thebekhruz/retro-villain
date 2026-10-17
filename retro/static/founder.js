const $=id=>document.getElementById(id);
const money=new Intl.NumberFormat('ru-RU',{maximumFractionDigits:0});
const exactMoney=new Intl.NumberFormat('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2});
const axisNumber=new Intl.NumberFormat('ru-RU',{maximumFractionDigits:1});
const shortDate=value=>new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'short',timeZone:'UTC'}).format(new Date(value+'T00:00:00Z'));
const shortMonth=value=>new Intl.DateTimeFormat('ru-RU',{month:'short',year:'numeric',timeZone:'UTC'}).format(new Date(value+'T00:00:00Z'));
// ТЗ 16.10, п. 5.2: одно название направления на всей странице — в фильтре,
// карточках, легенде, подсказке и таблице.
const directionMeta={retro:{label:'Retro',color:'#143e35'},school:{label:'Retro в школе',color:'#52786f'},banquet:{label:'Банкетный зал',color:'#a27445'}};
const bookingMeta={bookings:{label:'Активные заявки',color:'#143e35'},guests:{label:'Гости по заявкам',color:'#d8b977'},cancelled:{label:'Отменённые',color:'#ba7b67'}};
const gate=FounderLogic.requestGate(),guestGate=FounderLogic.requestGate();
let controller=null,guestController=null,lastAnalytics=null,lastBookings=null,lastGuests=null,lastQuery='',activePeriod='30';

function svg(name,attrs={}){const node=document.createElementNS('http://www.w3.org/2000/svg',name);Object.entries(attrs).forEach(([key,value])=>node.setAttribute(key,value));return node}
function selectedDirections(){return [...document.querySelectorAll('input[name=direction]:checked')].map(input=>input.value)}
// Пояснения — по одному предложению в своём узле: переводчик сопоставляет строку целиком,
// и склеенный абзац из нескольких пояснений на узбекском оставался русским.
function sentences(node,parts){node.replaceChildren(...parts.filter(Boolean).flatMap((part,index)=>{const span=document.createElement('span');span.textContent=part;return index?[' ',span]:[span]}))}
function setMessage(text,error=false){$('message').classList.remove('is-skel');const node=$('message'),parts=(Array.isArray(text)?text:[text]).filter(Boolean);node.hidden=!parts.length;sentences(node,parts);node.classList.toggle('is-error',error)}
function setLoading(value){document.querySelectorAll('.founder-metrics').forEach(node=>node.setAttribute('aria-busy',String(value)))}
function plural(count,one,few,many){const tens=count%100,units=count%10;return units===1&&tens!==11?one:units>=2&&units<=4&&(tens<12||tens>14)?few:many}

/* ── Отклик и ожидание (busy.js, T-393) ──────────────────────────────────
   Первая загрузка — скелеты в форме цифр, графиков и таблиц. Повторная
   (другой период, «Показать») — прежние данные на месте и гаснут, пока не
   придут новые: пустой экран на время iiko читался как поломка. */
const Busy=globalThis.RetroBusy;
const ANALYTICS_PANELS='.founder-workspace>.founder-metrics,.founder-workspace>.founder-panel:not(.booking-panel):not(.broadcast-panel)';
const BOOKING_CARDS=['booking-all','booking-total','booking-cancelled','booking-guests'];
const skel=(className='rm-skel is-val')=>{const node=document.createElement('span');node.className=className;return node};
function chartSkeleton(target,height){const block=document.createElement('div');block.className='fa-skel-chart';block.style.height=height+'px';block.setAttribute('aria-hidden','true');for(let index=0;index<4;index+=1)block.append(skel('rm-skel'));target.replaceChildren(block)}
function cardsSkeleton(target,count){target.replaceChildren(...Array.from({length:count},()=>{const card=document.createElement('article');card.className='fa-skel-card';card.setAttribute('aria-hidden','true');card.append(skel('rm-skel'),skel('rm-skel is-md'),skel('rm-skel'));return card}))}
function tableSkeleton(target,rows){target.replaceChildren(...Array.from({length:rows},()=>{const row=document.createElement('div');row.className='rm-skel-row fa-skel-row';row.setAttribute('aria-hidden','true');for(let index=0;index<6;index+=1)row.append(skel('rm-skel'));return row}))}
function paintSkeleton(){
  document.querySelectorAll('.founder-metrics:not(.booking-metrics) article strong').forEach(node=>node.replaceChildren(skel()));
  $('total-retro-yandex').replaceChildren(skel());
  // Пояснения к выборке приходят почти всегда — место под них держим сразу, иначе вся страница съезжает вниз.
  const message=$('message');message.hidden=false;message.classList.remove('is-error');message.classList.add('is-skel');message.replaceChildren(skel('rm-skel'),skel('rm-skel'),skel('rm-skel'));
  chartSkeleton($('revenue-chart'),230);tableSkeleton($('series-table'),5);
  bookingSkeleton();guestSkeleton();
}
function bookingSkeleton(){BOOKING_CARDS.forEach(id=>$(id).replaceChildren(skel()));chartSkeleton($('booking-chart'),255);cardsSkeleton($('booking-sources'),3);const status=$('booking-status');status.replaceChildren(skel('rm-skel is-val'));status.classList.remove('is-error')}
function guestSkeleton(){$('iiko-guests').replaceChildren(skel())}
/** Ответа не будет (ошибка или фильтры сменились до ответа) — прочерки вместо вечных полос. */
function settleSkeletons(){
  document.querySelectorAll('.founder-metrics article strong,.booking-metrics article strong,#total-retro-yandex').forEach(node=>{if(node.querySelector('.rm-skel'))node.textContent='—'});
  ['revenue-chart','booking-chart','booking-sources'].forEach(id=>{if($(id).querySelector('.rm-skel'))$(id).replaceChildren()});
  const message=$('message');if(message.classList.contains('is-skel')){message.classList.remove('is-skel');message.replaceChildren();message.hidden=true}
  const status=$('booking-status');if(status.querySelector('.rm-skel'))status.textContent='Ожидает загрузки';
  if($('series-table').querySelector('.rm-skel'))renderSeriesTable();
}
function beginLoading(reload,analytics,bookings){
  if(!reload){paintSkeleton();return}
  if(!Busy)return;
  document.querySelectorAll(ANALYTICS_PANELS).forEach(node=>Busy.section(node,analytics));
  Busy.section(document.querySelector('.booking-panel'),bookings);
}

/* ── Выручка: столбцы с накоплением (ТЗ 16.10, п. 5.1) ─────────────────────
   Ширина — от контейнера, без прокрутки вбок; длинный ряд по дням
   собирается по неделям. Подсказка встаёт над серединой столбца. */
const UNIT_TEXT={day:'По дням.',week:'По неделям.',month:'По месяцам.'};
function compactMoney(value){const abs=Math.abs(value);if(abs>=1e6)return axisNumber.format(value/1e6)+' млн';if(abs>=1e3)return axisNumber.format(value/1e3)+' тыс';return money.format(value)}
function dated(value){return `${shortDate(value)}, ${FounderLogic.weekday(value)}`}
function revenuePeriod(group){
  const period=group.start===group.end?dated(group.start):`${dated(group.start)} – ${dated(group.end)}`;
  return period+(group.incomplete?' · день не завершён':'');
}
function partialText(bar,unit){
  if(!bar.partial)return '';
  const days=`${bar.days} ${plural(bar.days,'день','дня','дней')} из ${bar.full}`;
  return unit==='month'?`Неполный месяц: ${days}`:`Неполная неделя: ${days}`;
}
function barLabel(bar,unit){return unit==='month'?shortMonth(bar.start):shortDate(bar.start)}

function revenueTooltip(bar,directions,unit){
  const tooltip=document.createElement('div');tooltip.className='chart-tooltip';tooltip.hidden=true;
  const period=document.createElement('strong');period.textContent=revenuePeriod(bar);
  const rows=document.createElement('div');rows.className='chart-tooltip-rows';
  // Сверху вниз — как лежат отрезки в столбце.
  [...directions].reverse().forEach(direction=>{const row=document.createElement('span');const label=document.createElement('i');label.style.background=directionMeta[direction].color;row.append(label,document.createTextNode(`${directionMeta[direction].label}: ${money.format(bar.values[direction])} сум`));rows.append(row)});
  const sum=document.createElement('b');sum.textContent=`Всего: ${money.format(bar.total)} сум`;
  tooltip.append(period,rows,sum);
  const partial=partialText(bar,unit);if(partial){const note=document.createElement('em');note.textContent=partial;tooltip.append(note)}
  return tooltip;
}

function renderRevenue(data){
  const target=$('revenue-chart');target.replaceChildren();const directions=data.directions;
  const legend=$('revenue-legend');legend.replaceChildren();
  if(!data.revenue_series.length){const empty=document.createElement('div');empty.className='empty-chart';empty.textContent='За период нет временных групп.';target.append(empty);return}
  const {unit,regrouped,bars}=FounderLogic.revenueBars(data.revenue_series,directions,{granularity:data.granularity});
  // Высота — та же, что у .chart-shell в founder.css: пустой блок (:empty) ниже, мерить его нельзя.
  const media=query=>typeof matchMedia==='function'&&matchMedia(query).matches;
  const width=Math.max(260,Math.floor(target.clientWidth||720)),height=media('(max-width:650px)')?240:media('(max-width:1180px)')?260:280;
  const hasPartial=bars.some(bar=>bar.partial);
  const pad={left:54,right:6,top:12,bottom:hasPartial?44:30};const plotWidth=width-pad.left-pad.right,plotHeight=height-pad.top-pad.bottom;
  const chart=svg('svg',{viewBox:`0 0 ${width} ${height}`,'aria-hidden':'true'});
  const {max,min}=FounderLogic.barScale(bars,directions),span=max-min||1,yOf=value=>pad.top+(max-value)*plotHeight/span;
  for(let step=0;step<=4;step+=1){const value=max-span*step/4,y=yOf(value);chart.append(svg('line',{x1:pad.left,y1:y,x2:width-pad.right,y2:y,class:value===0?'chart-grid is-zero':'chart-grid'}));const label=svg('text',{x:pad.left-8,y:y+3,'text-anchor':'end',class:'chart-axis'});label.textContent=compactMoney(value);chart.append(label)}
  const slot=plotWidth/bars.length,barWidth=Math.max(2,Math.min(56,slot*.68)),center=index=>pad.left+(index+.5)*slot;
  const band=svg('rect',{x:pad.left,y:pad.top,width:slot,height:plotHeight,class:'chart-hover-band',visibility:'hidden'});chart.append(band);
  bars.forEach((bar,index)=>{
    const group=svg('g',{class:'chart-bar'});const x=center(index)-barWidth/2;
    FounderLogic.barStack(bar,directions).forEach(part=>{if(part.to===part.from)return;group.append(svg('rect',{x,y:yOf(part.to),width:barWidth,height:Math.max(.5,yOf(part.from)-yOf(part.to)),fill:directionMeta[part.direction].color}))});
    // Неполная неделя или месяц — пунктирная рамка на всю высоту полосы.
    if(bar.partial)group.append(svg('rect',{x:x-2,y:pad.top,width:barWidth+4,height:plotHeight,class:'chart-bar-partial'}));
    chart.append(group);
  });
  const every=Math.max(1,Math.ceil(bars.length/Math.max(2,Math.floor(plotWidth/64))));let lastLabel=-Infinity;
  bars.forEach((bar,index)=>{
    const edge=index===bars.length-1&&(index-lastLabel)*slot>=60;
    if(index%every!==0&&!edge)return;lastLabel=index;
    // У правого края подпись прижимается к краю, а не обрезается им.
    const atEdge=center(index)>width-30,x=atEdge?width-pad.right:center(index),anchor=atEdge?'end':'middle';
    const label=svg('text',{x,y:pad.top+plotHeight+16,'text-anchor':anchor,class:'chart-axis'});label.textContent=barLabel(bar,unit)+(bar.incomplete?' *':'');chart.append(label);
    if(bar.partial){const days=svg('text',{x,y:pad.top+plotHeight+29,'text-anchor':anchor,class:'chart-axis is-partial'});days.textContent=`${bar.days} из ${bar.full} дн.`;chart.append(days)}
  });
  const hoverLayer=svg('rect',{x:pad.left,y:pad.top,width:plotWidth,height:plotHeight,class:'chart-hover-layer',tabindex:'0','aria-label':'Наведите или коснитесь столбца, чтобы увидеть суммы'});chart.append(hoverLayer);
  const tooltip=revenueTooltip(bars[0],directions,unit);tooltip.classList.add('is-placed');let current=-1;
  // Подсказка встаёт сбоку от середины столбца и не выходит за края графика:
  // на телефоне её иначе обрезало.
  const showBar=index=>{current=index;const bar=bars[index],x=center(index),next=revenueTooltip(bar,directions,unit);tooltip.replaceChildren(...next.childNodes);tooltip.hidden=false;
    tooltip.style.left='0px';const box=tooltip.offsetWidth||190,right=x+10,left=x-10-box;tooltip.style.left=Math.max(0,Math.min(width-box,right+box<=width?right:left))+'px';
    band.setAttribute('x',pad.left+index*slot);band.setAttribute('visibility','visible')};
  const hide=()=>{tooltip.hidden=true;band.setAttribute('visibility','hidden')};
  const pointer=event=>{const bounds=chart.getBoundingClientRect();showBar(FounderLogic.barIndexAt((event.clientX-bounds.left)/bounds.width*width-pad.left,plotWidth,bars.length))};
  hoverLayer.addEventListener('pointermove',pointer);hoverLayer.addEventListener('pointerdown',pointer);hoverLayer.addEventListener('pointerleave',hide);hoverLayer.addEventListener('blur',hide);
  hoverLayer.addEventListener('keydown',event=>{if(event.key!=='ArrowLeft'&&event.key!=='ArrowRight')return;event.preventDefault();showBar(Math.max(0,Math.min(bars.length-1,(current<0?0:current)+(event.key==='ArrowRight'?1:-1))))});
  target.append(chart,tooltip);
  directions.forEach(direction=>{const item=document.createElement('span');const dot=document.createElement('i');dot.className='legend-dot';dot.style.background=directionMeta[direction].color;item.append(dot,document.createTextNode(directionMeta[direction].label));legend.append(item)});
  sentences($('revenue-scope'),[regrouped?`За ${data.revenue_series.length} ${plural(data.revenue_series.length,'день','дня','дней')} столбцы собраны по неделям.`:UNIT_TEXT[unit],'Столбец — сумма выбранных направлений.']);
  const note=$('revenue-partial');note.hidden=!hasPartial;
  note.textContent=hasPartial?(unit==='month'?'Пунктирная рамка — неполный месяц: в период попали не все его дни.':'Пунктирная рамка — неполная неделя: в период попали не все её дни.'):'';
}
// Ширина графика — от контейнера: при повороте телефона или сужении окна рисуем заново.
if(typeof ResizeObserver==='function'){
  const watch=(id,redraw)=>{let frame=0,seen=0;new ResizeObserver(entries=>{const width=Math.round(entries[0].contentRect.width);if(!width||width===seen)return;seen=width;cancelAnimationFrame(frame);frame=requestAnimationFrame(()=>{if(!$(id).querySelector('.rm-skel,.fa-unavailable'))redraw()})}).observe($(id))};
  watch('revenue-chart',()=>{if(lastAnalytics?.revenue_series)renderRevenue(lastAnalytics)});
  watch('booking-chart',()=>{if(lastBookings?.series)renderBookingChart(lastBookings)});
}

function bookingTooltip(group,keys){
  const tooltip=document.createElement('div');tooltip.className='chart-tooltip';tooltip.hidden=true;const period=document.createElement('strong');period.textContent=revenuePeriod(group);const rows=document.createElement('div');rows.className='chart-tooltip-rows';keys.forEach(key=>{const row=document.createElement('span'),dot=document.createElement('i');dot.style.background=bookingMeta[key].color;row.append(dot,document.createTextNode(`${bookingMeta[key].label}: ${money.format(Number(group.values[key]||0))}`));rows.append(row)});tooltip.append(period,rows);return tooltip;
}

function renderBookingChart(data){
  // Ширина — от блока, как у выручки; узкий экран и короткий ряд — не уже 40 px на точку.
  const target=$('booking-chart'),fit=Math.floor(target.clientWidth||720);target.replaceChildren();const keys=Object.keys(bookingMeta),series=data.series;if(!series.length){const empty=document.createElement('div');empty.className='empty-chart';empty.textContent='За период нет временных групп.';target.append(empty);return}const width=Math.max(fit,Math.min(720,series.length*40)),height=255,pad={left:54,right:18,top:12,bottom:40},plotWidth=width-pad.left-pad.right,plotHeight=height-pad.top-pad.bottom;const chart=svg('svg',{viewBox:`0 0 ${width} ${height}`,'aria-hidden':'true'});chart.style.width=width+'px';const all=series.flatMap(group=>keys.map(key=>Number(group.values[key]||0))),max=Math.max(1,...all);
  for(let step=0;step<=4;step+=1){const y=pad.top+plotHeight*step/4;chart.append(svg('line',{x1:pad.left,y1:y,x2:width-pad.right,y2:y,class:'chart-grid'}));const label=svg('text',{x:pad.left-8,y:y+3,'text-anchor':'end',class:'chart-axis'});label.textContent=money.format(max*(1-step/4));chart.append(label)}
  const paths=FounderLogic.revenuePaths(series,keys,plotWidth,plotHeight);keys.forEach(key=>{const points=paths[key].split(' ').map(pair=>{const [x,y]=pair.split(',').map(Number);return `${x+pad.left},${y+pad.top}`}).join(' ');chart.append(svg('polyline',{points,class:'booking-line',stroke:bookingMeta[key].color}));series.forEach((group,index)=>{const x=series.length===1?pad.left+plotWidth/2:pad.left+index*plotWidth/(series.length-1),value=Number(group.values[key]||0),y=pad.top+plotHeight-value*plotHeight/max,point=svg('circle',{cx:x,cy:y,r:3.5,fill:bookingMeta[key].color,class:'booking-point'}),title=svg('title');title.textContent=`${bookingMeta[key].label}: ${money.format(value)} · ${revenuePeriod(group)}`;point.append(title);chart.append(point)})});const labelEvery=Math.max(1,Math.ceil(series.length/6));series.forEach((group,index)=>{if(index%labelEvery!==0&&index!==series.length-1)return;const x=series.length===1?pad.left+plotWidth/2:pad.left+index*plotWidth/(series.length-1),label=svg('text',{x,y:height-10,'text-anchor':'middle',class:'chart-axis'});label.textContent=shortDate(group.start)+(group.incomplete?' *':'');chart.append(label)});
  const hoverLine=svg('line',{y1:pad.top,y2:pad.top+plotHeight,class:'chart-hover-line',visibility:'hidden'}),hoverLayer=svg('rect',{x:pad.left,y:pad.top,width:plotWidth,height:plotHeight,class:'chart-hover-layer'});chart.append(hoverLine,hoverLayer);const tooltip=bookingTooltip(series[0],keys);const showTooltip=event=>{const bounds=chart.getBoundingClientRect(),chartX=(event.clientX-bounds.left)/bounds.width*width,index=FounderLogic.nearestRevenueIndex(chartX-pad.left,plotWidth,series.length),group=series[index],x=series.length===1?pad.left+plotWidth/2:pad.left+index*plotWidth/(series.length-1),next=bookingTooltip(group,keys);tooltip.replaceChildren(...next.childNodes);tooltip.hidden=false;tooltip.style.left=x+'px';tooltip.classList.toggle('is-left',x-target.scrollLeft>target.clientWidth*.62);hoverLine.setAttribute('visibility','visible');hoverLine.setAttribute('x1',x);hoverLine.setAttribute('x2',x)};hoverLayer.addEventListener('pointermove',showTooltip);hoverLayer.addEventListener('pointerdown',showTooltip);hoverLayer.addEventListener('click',showTooltip);hoverLayer.addEventListener('pointerleave',()=>{tooltip.hidden=true;hoverLine.setAttribute('visibility','hidden')});target.append(chart,tooltip);const legend=$('booking-legend');legend.replaceChildren();keys.forEach(key=>{const item=document.createElement('span'),dot=document.createElement('i');dot.className='legend-dot';dot.style.background=bookingMeta[key].color;item.append(dot,document.createTextNode(bookingMeta[key].label));legend.append(item)})
}

/* ── Брони (ТЗ 16.10, п. 5.3): всего = активные + отменённые ─────────────── */
function bookingCount(count){return `${money.format(count)} ${plural(count,'заявка','заявки','заявок')}`}
function renderBookings(data){
  lastBookings=data;document.querySelector('.booking-panel').classList.remove('is-unavailable');
  const totals=data.totals,all=Number(totals.all??(Number(totals.bookings)+Number(totals.cancelled))),unknown=Number(totals.unknown_guest_bookings);
  $('booking-all').textContent=money.format(all);$('booking-total').textContent=money.format(Number(totals.bookings));$('booking-cancelled').textContent=money.format(Number(totals.cancelled));$('booking-guests').textContent=money.format(Number(totals.guests));
  $('booking-guests-detail').textContent=unknown?`заявлено в активных заявках · без числа гостей: ${money.format(unknown)}`:'заявлено в активных заявках';
  renderBookingChart(data);const sources=$('booking-sources');sources.replaceChildren();
  data.sources.forEach(source=>{const card=document.createElement('article'),label=document.createElement('span'),total=document.createElement('strong'),detail=document.createElement('small'),count=Number(source.all??source.bookings);label.textContent=source.name;total.textContent=bookingCount(count);detail.textContent=`активных ${money.format(Number(source.bookings))} · отменено ${money.format(Number(source.cancelled||0))} · гостей ${money.format(Number(source.guests))} · ${source.share_percent===null?'доля не вычисляется':source.share_percent+'%'}`;card.append(label,total,detail);sources.append(card)});
  if(!data.sources.length){const empty=document.createElement('article');empty.textContent='Источников за период нет.';sources.append(empty)}
  const coverage=data.coverage,format=value=>new Intl.DateTimeFormat('ru-RU',{dateStyle:'medium',timeStyle:'short',timeZone:'Asia/Tashkent'}).format(new Date(value)),parts=coverage.sources?coverage.sources.map(source=>`${source.name}: журнал с ${format(source.history_started_at)}.`):[`Журнал ведётся с ${format(coverage.history_started_at)}.`];
  if(!coverage.historical_data_complete)parts.push('История до запуска журнала неполная.');if(coverage.excluded_missing_date)parts.push(`Без даты визита исключено: ${money.format(Number(coverage.excluded_missing_date))}.`);
  const note=$('booking-coverage');sentences(note,parts);note.classList.toggle('is-warning',!coverage.historical_data_complete||Boolean(coverage.excluded_missing_date));
  const status=$('booking-status');status.textContent=coverage.sources?coverage.sources.map(source=>source.name).join(' + ')+' · подключено':'Telegram API подключён';status.classList.remove('is-error');renderSeriesTable()
}

function renderBookingError(message){
  BOOKING_CARDS.forEach(id=>$(id).textContent='—');$('booking-guests-detail').textContent='заявлено в активных заявках';$('booking-legend').replaceChildren();$('booking-sources').replaceChildren();$('booking-coverage').textContent='Основная аналитика iiko продолжает работать независимо.';const status=$('booking-status');status.textContent='Брони недоступны';status.classList.add('is-error');document.querySelector('.booking-panel').classList.add('is-unavailable');
  const notice=document.createElement('div');notice.className='fa-unavailable';notice.setAttribute('role','status');const icon=document.createElement('span');icon.textContent='!';icon.setAttribute('aria-hidden','true');const text=document.createElement('p');text.textContent=message;notice.append(icon,text);$('booking-chart').replaceChildren(notice)
}

/** Гости по чекам iiko — то, что поставил официант; рядом с заявленными в бронях. */
function renderGuests(data){
  lastGuests=data;const retro=data.totals.retro;
  $('iiko-guests').textContent=money.format(Number(retro.guests));
  $('iiko-guests-detail').textContent=`касса Retro без банкетного зала · чеков: ${money.format(Number(retro.checks))}`;
  renderSeriesTable();
}
function renderGuestsError(message){lastGuests=null;$('iiko-guests').textContent='—';$('iiko-guests-detail').textContent=message;renderSeriesTable()}

function table(columns,rows){
  const node=document.createElement('table');node.className='series-table';
  const head=document.createElement('thead'),header=document.createElement('tr');
  columns.forEach(label=>{const th=document.createElement('th');th.scope='col';th.textContent=label;header.append(th)});head.append(header);node.append(head);
  const body=document.createElement('tbody');rows.forEach(values=>{const tr=document.createElement('tr');values.forEach((value,index)=>{const cell=document.createElement(index===0?'th':'td');if(index===0)cell.scope='row';cell.textContent=value===undefined||value===null?'—':index===0?value:money.format(Number(value));tr.append(cell)});body.append(tr)});node.append(body);return node;
}
function renderSeriesTable(){
  const target=$('series-table');target.replaceChildren();
  if(!lastAnalytics&&!lastBookings&&!lastGuests){target.textContent='Данные по периодам ещё не загружены.';renderPaymentTable();return}
  const analyticsRows=new Map((lastAnalytics?.revenue_series||[]).map(row=>[row.start,row]));
  const bookingRows=new Map((lastBookings?.series||[]).map(row=>[row.start,row]));
  const guestRows=new Map((lastGuests?.series||[]).map(row=>[row.start,row]));
  const starts=[...new Set([...analyticsRows.keys(),...bookingRows.keys(),...guestRows.keys()])].sort();
  const rows=starts.map(start=>{const revenue=analyticsRows.get(start),booking=bookingRows.get(start),guests=guestRows.get(start),values=booking?.values;
    return [revenuePeriod(revenue||booking||guests),revenue?.values.retro,revenue?.values.school,revenue?.values.banquet,revenue?.total,
      values?Number(values.bookings)+Number(values.cancelled):undefined,values?.bookings,values?.cancelled,values?.guests,guests?.values.guests]});
  target.append(table(['Период','Retro','Retro в школе','Банкетный зал','Всего','Заявок всего','Активные','Отменённые','Гости по заявкам','Гости по чекам iiko'],rows));
  renderPaymentTable();
}
/** Способы оплаты по периодам — свёрнуты: графика оплат больше нет, API оплат остаётся. */
function renderPaymentTable(){
  const target=$('payment-table');target.replaceChildren();const fold=$('payment-table-fold');
  const payments=lastAnalytics?.payment_summary?.map(item=>item.name)||[];fold.hidden=!payments.length;
  if(!payments.length)return;
  const rows=FounderLogic.paymentLineSeries(lastAnalytics.payment_series,lastAnalytics.directions,payments).map(row=>[revenuePeriod(row),...payments.map(name=>row.values[name])]);
  target.append(table(['Период',...payments],rows));
}

function renderSalesBridge(data){
  const bridge=data.sales_bridge;
  if(!bridge){$('sales-bridge-equation').textContent='Расшифровка оплат пока недоступна. Обновите отчёт.';return}
  const labels={paid:'Оплаты продаж',prepaid:'Зачтённые авансы',other:'Другие операции',sales:'Продажи'};
  ['paid','prepaid','other'].forEach(key=>$('sales-'+key).textContent=exactMoney.format(Number(bridge.totals[key])));
  $('sales-other-card').hidden=!Number(bridge.totals.other)&&!bridge.unknown_operations.length;
  $('sales-bridge-scope').textContent=data.directions.map(key=>directionMeta[key].label).join(' · ')+' · '+data.period.start+' — '+data.period.end;
  const terms=['paid','prepaid',...((Number(bridge.totals.other)||bridge.unknown_operations.length)?['other']:[])];
  const equation=terms.map(key=>labels[key]+': '+exactMoney.format(Number(bridge.totals[key]))).join(' + ');
  const difference=Number(data.totals.selected)-Number(bridge.totals.sales);
  $('sales-bridge-equation').textContent=equation+' = '+exactMoney.format(Number(bridge.totals.sales))+' сум.'+
    (difference?' Разница с отдельным отчётом продаж: '+exactMoney.format(difference)+' сум.':'')+
    (bridge.reconciled&&data.reconciled?' Разбивка сверена с отчётом продаж.':' Разбивка НЕ сверена — требуется проверка.');
  const target=$('sales-bridge-table');target.replaceChildren();
  const table=document.createElement('table');table.className='series-table';
  const head=document.createElement('thead'),header=document.createElement('tr');
  ['Способ оплаты',...terms.map(key=>labels[key]),'Всего продаж'].forEach(label=>{const th=document.createElement('th');th.scope='col';th.textContent=label;header.append(th)});
  head.append(header);table.append(head);const body=document.createElement('tbody');
  bridge.payments.forEach(row=>{const tr=document.createElement('tr');[row.name,...terms.map(key=>row[key]),row.sales].forEach((value,index)=>{const cell=document.createElement(index?'td':'th');if(!index)cell.scope='row';cell.textContent=index?exactMoney.format(Number(value)):value;tr.append(cell)});body.append(tr)});
  table.append(body);target.append(table);
}

/** Как считаются цифры — свёрнутая подсказка вместо стены текста над карточками. */
function renderScopeNotes(data){
  const notes=[];if(data.scope_note)notes.push(...String(data.scope_note).split(/(?<=\.)\s+/).filter(Boolean));
  if(data.sales_totals)notes.push(`Все продажи выбранных направлений: ${money.format(data.directions.reduce((sum,key)=>sum+Number(data.sales_totals[key]),0))} сум.`,`Вне банкетной выборки: ${money.format(Number(data.scope_excluded_revenue))} сум.`);
  const target=$('scope-notes');target.replaceChildren(...notes.map(text=>{const line=document.createElement('p');line.textContent=text;return line}));
  $('scope-hint').hidden=!notes.length;
}

function render(data){
  renderSalesBridge(data);
  lastAnalytics=data;
  $('pnl-sales').textContent=exactMoney.format(Number(data.pnl.sales));
  $('pnl-cost').textContent=exactMoney.format(Number(data.pnl.cost));
  $('pnl-profit').textContent=exactMoney.format(Number(data.pnl.gross_profit));
  $('pnl-net-profit').textContent=exactMoney.format(Number(data.pnl.net_profit));
  // Период до начала учёта: расходов кассы и бухгалтерии за него нет — «—», не ноль.
  const expenses=data.dashboard_expenses,exact=value=>value===null||value===undefined?'—':exactMoney.format(Number(value));
  $('net-profit-final').textContent=exact(data.net_profit_after_dashboard_expenses);
  $('pnl-operating-expenses').textContent=exactMoney.format(Number(data.pnl.operating_expenses));
  $('pnl-other-expenses').textContent=exactMoney.format(Number(data.pnl.other_expenses));
  $('pnl-other-income').textContent=exactMoney.format(Number(data.pnl.other_income));
  $('expense-cashier').textContent=exact(expenses?.cashier);
  $('expense-accountant').textContent=exact(expenses?.accountant);
  $('expense-accountant-detail').textContent='зарплата: '+exact(expenses?.accountant_salary)+' · прочие: '+exact(expenses?.accountant_other);
  $('expense-dashboard-total').textContent=exact(expenses?.total);
  $('internal-tasting').textContent=exactMoney.format(Number(data.internal_costs.tasting));
  $('internal-chef').textContent=exactMoney.format(Number(data.internal_costs.chef_account));
  ['retro','school','banquet'].forEach(direction=>{$('total-'+direction).textContent=money.format(Number(data.totals[direction]));document.querySelector(`article[data-direction=${direction}]`).hidden=!data.directions.includes(direction)});$('total-selected').textContent=money.format(Number(data.totals.selected));$('updated').textContent='Обновлено '+new Intl.DateTimeFormat('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(data.updated_at));
  // ТЗ 5.2: Яндекс Еда — тип оплаты внутри Retro, из iiko; к «Всего» не прибавляется.
  const yandex=data.yandex_totals?.retro;$('total-retro-yandex').textContent=yandex===undefined||yandex===null?'—':money.format(Number(yandex));
  // «Чем оплачены продажи» убран (ТЗ 4.3): несверка оплат остаётся в предупреждениях ниже.
  renderRevenue(data);renderSeriesTable();renderScopeNotes(data);const notices=[...data.warnings];if(data.includes_current_day)notices.push('Период включает текущий незавершённый день — он отмечен звёздочкой.');setMessage(notices,!data.reconciled)
}

async function load(options = {}) {
  const directions = selectedDirections();
  if (!directions.length) { setMessage('Выберите хотя бы одно направление.', true); return; }
  // Неверный период ловим до запроса: ответ 422 иначе выглядел бы как
  // «Источник iiko недоступен» в строке статуса.
  const periodError = globalThis.FounderLogic?.periodError ? globalThis.FounderLogic.periodError($('start').value, $('end').value) : null;
  if (periodError) { setMessage(periodError, true); $('updated').textContent = 'Проверьте даты'; return false; }
  controller?.abort();
  controller = new AbortController();
  const signal = controller.signal, requestId = gate.next();
  const params = new URLSearchParams({start:$('start').value, end:$('end').value,
    granularity:$('granularity').value, directions:directions.join(',')});
  const query = params.toString();
  if (options.refresh === true && lastQuery === query) params.set('refresh', 'true');
  const reload = Boolean(lastAnalytics || lastBookings);
  setLoading(true); setMessage('');
  const bookingParams = new URLSearchParams({start:$('start').value, end:$('end').value,
    granularity:$('granularity').value});
  const fetchJson = async (url, fallback) => RetroState.responseJson(await fetch(url, {signal}), fallback);
  const analytics = fetchJson('/api/founder/analytics?' + params, 'Не удалось получить аналитику.')
    .then(data => { if (!gate.isCurrent(requestId)) return false; lastQuery=query; render(data); return true; })
    .catch(error => {
      if (!gate.isCurrent(requestId) || error.name === 'AbortError') return false;
      lastAnalytics=RetroState.analyticsAfterFailure(lastAnalytics); settleSkeletons(); renderSeriesTable();
      setMessage(error.message,true); $('updated').textContent='Источник iiko недоступен';
      return false;
    })
    .finally(() => { if (gate.isCurrent(requestId)) setLoading(false); });
  const bookings = fetchJson('/api/founder/bookings?' + bookingParams, 'Не удалось получить бронирования.')
    .then(data => { if (gate.isCurrent(requestId)) renderBookings(data); })
    .catch(error => {
      if (gate.isCurrent(requestId) && error.name !== 'AbortError') renderBookingError(error.message);
    });
  beginLoading(reload, analytics, bookings);
  const [result] = await Promise.allSettled([analytics, bookings]);
  // Для кнопки: ✓ — только если аналитика действительно на экране.
  return result.status === 'fulfilled' && result.value === true;
}

/** Гости по чекам iiko — отдельным лёгким отчётом: сбой броней или аналитики их не прячет. */
function loadGuests(filters,options={}){
  guestController?.abort();guestController=new AbortController();const signal=guestController.signal,requestId=guestGate.next();
  const params=new URLSearchParams({start:filters.start,end:filters.end,granularity:filters.granularity});
  if(options.refresh===true)params.set('refresh','true');
  if(!lastGuests)guestSkeleton();
  return fetch('/api/founder/guests?'+params,{signal}).then(response=>RetroState.responseJson(response,'Не удалось получить гостей из iiko.'))
    .then(data=>{if(guestGate.isCurrent(requestId))renderGuests(data)})
    .catch(error=>{if(guestGate.isCurrent(requestId)&&error.name!=='AbortError')renderGuestsError(error.message)});
}

/* ── Фильтры в адресе и «устарело» вместо пустого экрана (ТЗ 16.10, п. 5.4) ── */
function currentFilters(){return {start:$('start').value,end:$('end').value,granularity:$('granularity').value,directions:selectedDirections(),period:activePeriod}}
function applyFilters(filters){
  $('start').value=filters.start;$('end').value=filters.end;$('granularity').value=filters.granularity;
  document.querySelectorAll('input[name=direction]').forEach(input=>{input.checked=filters.directions.includes(input.value)});
  activePeriod=filters.period;document.querySelectorAll('[data-period]').forEach(button=>button.classList.toggle('is-active',button.dataset.period===activePeriod));
}
function setStale(on){
  const hasData=Boolean(lastAnalytics||lastBookings||lastGuests);
  document.querySelector('.founder-workspace').classList.toggle('is-stale',on&&hasData);
  const note=$('stale');note.hidden=!on;
  note.textContent=hasData?'Фильтры изменены. Ниже прежняя выборка, нажмите «Показать», чтобы обновить.':'Фильтры изменены. Нажмите «Показать», чтобы загрузить новую выборку.';
}
/** Загрузить выбранное: адрес страницы запоминает фильтры, данные — новые. */
function show(options={}){
  const filters=currentFilters();
  if(filters.directions.length&&!FounderLogic.periodError(filters.start,filters.end)){
    setStale(false);
    try{history.replaceState(history.state,'',location.pathname+'?'+FounderLogic.filtersToQuery(filters))}catch{}
    loadGuests(filters,options);
  }
  return load(options);
}

async function start(){paintSkeleton();try{const config=await globalThis.RetroConfig;$('start').max=config.today;$('end').max=config.today;$('fa-phone-excel').href='/api/founder/export/month?month='+config.today.slice(0,7);applyFilters(FounderLogic.filtersFromQuery(location.search,config.today));await show()}catch(error){setLoading(false);settleSkeletons();setMessage('Не удалось определить текущую дату сервера.',true)}}
/** Фильтр сменили — прежние цифры остаются на экране с пометкой «устарело»;
 *  стирать всё до «Показать» значило оставить учредителя с пустой страницей. */
function invalidatePending(event){
  controller?.abort();controller=null;gate.invalidate();guestController?.abort();guestController=null;guestGate.invalidate();setLoading(false);settleSkeletons();
  if(event?.target?.type==='date'){activePeriod=null;document.querySelectorAll('[data-period]').forEach(button=>button.classList.remove('is-active'))}
  $('updated').textContent='Фильтры изменены · нажмите «Показать»';setStale(true);
}
['start','end','granularity'].forEach(id=>$(id).addEventListener('change',invalidatePending));document.querySelectorAll('input[name=direction]').forEach(input=>input.addEventListener('change',invalidatePending));
// «Показать» крутится, пока iiko считает, и ставит ✓, когда цифры на экране;
// быстрый период — крутится сам чип.
$('filters').addEventListener('submit',event=>{event.preventDefault();const work=show({refresh:true});if(Busy)Busy.button($('refresh'),work)});document.querySelectorAll('[data-period]').forEach(button=>button.addEventListener('click',()=>{activePeriod=button.dataset.period;document.querySelectorAll('[data-period]').forEach(item=>item.classList.toggle('is-active',item===button));const period=FounderLogic.quickPeriod(button.dataset.period,$('end').max);$('start').value=period.start;$('end').value=period.end;const work=show();if(Busy)Busy.button(button,work,{done:false})}));
// Вкладка AI на телефоне открывает тот же чат, что круглая кнопка на компьютере.
$('fa-phone-ai').addEventListener('click',()=>$('ai-chat-toggle').click());
start();
