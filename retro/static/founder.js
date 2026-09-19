const $=id=>document.getElementById(id);
const money=new Intl.NumberFormat('ru-RU',{maximumFractionDigits:0});
const shortDate=value=>new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'short',timeZone:'UTC'}).format(new Date(value+'T00:00:00Z'));
const directionMeta={retro:{label:'Retro',color:'#143e35'},school:{label:'Школа',color:'#52786f'},banquet:{label:'Банкет',color:'#a27445'}};
const paymentColors=['#143e35','#d8b977','#52786f','#a27445','#769a83','#8c6f98','#ba7b67','#87909a','#b3a676'];
const gate=FounderLogic.requestGate();let controller=null;

function svg(name,attrs={}){const node=document.createElementNS('http://www.w3.org/2000/svg',name);Object.entries(attrs).forEach(([key,value])=>node.setAttribute(key,value));return node}
function selectedDirections(){return [...document.querySelectorAll('input[name=direction]:checked')].map(input=>input.value)}
function setMessage(text,error=false){const node=$('message');node.hidden=!text;node.textContent=text||'';node.classList.toggle('is-error',error)}
function setLoading(value){document.querySelector('.founder-workspace').classList.toggle('is-loading',value);document.querySelector('.founder-metrics').setAttribute('aria-busy',String(value));$('refresh').disabled=value}

function revenuePeriod(group){
  const period=group.start===group.end?shortDate(group.start):`${shortDate(group.start)}–${shortDate(group.end)}`;
  return period+(group.incomplete?' · день не завершён':'');
}

function revenueTooltip(group,directions){
  const tooltip=document.createElement('div');tooltip.className='chart-tooltip';tooltip.hidden=true;
  const period=document.createElement('strong');period.textContent=revenuePeriod(group);
  const rows=document.createElement('div');rows.className='chart-tooltip-rows';
  let total=0;
  directions.forEach(direction=>{const value=Number(group.values[direction]||0);total+=value;const row=document.createElement('span');const label=document.createElement('i');label.style.background=directionMeta[direction].color;row.append(label,document.createTextNode(`${directionMeta[direction].label}: ${money.format(value)} сум`));rows.append(row)});
  const sum=document.createElement('b');sum.textContent=`Всего: ${money.format(total)} сум`;
  tooltip.append(period,rows,sum);return tooltip;
}

function paymentTooltip(group,payments){
  const tooltip=document.createElement('div');tooltip.className='chart-tooltip payment-tooltip';tooltip.hidden=true;
  const period=document.createElement('strong');period.textContent=revenuePeriod(group);
  const rows=document.createElement('div');rows.className='chart-tooltip-rows';
  let total=0;
  payments.forEach((payment,index)=>{const value=Number(group.values[payment]||0);total+=value;const row=document.createElement('span');const label=document.createElement('i');label.style.background=paymentColors[index%paymentColors.length];row.append(label,document.createTextNode(`${payment}: ${money.format(value)} сум`));rows.append(row)});
  const sum=document.createElement('b');sum.textContent=`Всего оплат: ${money.format(total)} сум`;
  tooltip.append(period,rows,sum);return tooltip;
}

function renderRevenue(data){
  const target=$('revenue-chart');target.replaceChildren();const directions=data.directions;
  if(!data.revenue_series.length){const empty=document.createElement('div');empty.className='empty-chart';empty.textContent='За период нет временных групп.';target.append(empty);return}
  const width=Math.max(720,data.revenue_series.length*58),height=230,pad={left:66,right:18,top:12,bottom:40};const plotWidth=width-pad.left-pad.right,plotHeight=height-pad.top-pad.bottom;
  const chart=svg('svg',{viewBox:`0 0 ${width} ${height}`,'aria-hidden':'true'});chart.style.width=width+'px';const all=data.revenue_series.flatMap(group=>directions.map(direction=>Number(group.values[direction]||0)));const max=Math.max(1,...all),min=Math.min(0,...all),span=max-min;
  for(let step=0;step<=4;step+=1){const y=pad.top+plotHeight*step/4;chart.append(svg('line',{x1:pad.left,y1:y,x2:width-pad.right,y2:y,class:'chart-grid'}));const label=svg('text',{x:pad.left-8,y:y+3,'text-anchor':'end',class:'chart-axis'});label.textContent=money.format(max-span*step/4);chart.append(label)}
  const paths=FounderLogic.revenuePaths(data.revenue_series,directions,plotWidth,plotHeight);
  directions.forEach(direction=>{const points=paths[direction].split(' ').map(pair=>{const [x,y]=pair.split(',').map(Number);return `${x+pad.left},${y+pad.top}`}).join(' ');chart.append(svg('polyline',{points,class:'chart-line',stroke:directionMeta[direction].color}));data.revenue_series.forEach((group,index)=>{const x=data.revenue_series.length===1?pad.left+plotWidth/2:pad.left+index*plotWidth/(data.revenue_series.length-1);const value=Number(group.values[direction]||0),y=pad.top+plotHeight-(value-min)*plotHeight/span;const point=svg('circle',{cx:x,cy:y,r:4,fill:directionMeta[direction].color,class:'chart-point'});const title=svg('title');title.textContent=`${directionMeta[direction].label}: ${money.format(value)} сум · ${revenuePeriod(group)}`;point.append(title);chart.append(point)})});
  const labelEvery=Math.max(1,Math.ceil(data.revenue_series.length/6));data.revenue_series.forEach((group,index)=>{if(index%labelEvery!==0&&index!==data.revenue_series.length-1)return;const x=data.revenue_series.length===1?pad.left+plotWidth/2:pad.left+index*plotWidth/(data.revenue_series.length-1);const label=svg('text',{x,y:height-10,'text-anchor':'middle',class:'chart-axis'});label.textContent=shortDate(group.start)+(group.incomplete?' *':'');chart.append(label)});
  const hoverLine=svg('line',{y1:pad.top,y2:pad.top+plotHeight,class:'chart-hover-line',visibility:'hidden'});chart.append(hoverLine);
  const hoverLayer=svg('rect',{x:pad.left,y:pad.top,width:plotWidth,height:plotHeight,class:'chart-hover-layer',tabindex:'0','aria-label':'Наведите или коснитесь графика, чтобы увидеть суммы'});chart.append(hoverLayer);
  const tooltip=revenueTooltip(data.revenue_series[0],directions);
  const showTooltip=event=>{const bounds=chart.getBoundingClientRect();const chartX=(event.clientX-bounds.left)/bounds.width*width;const index=FounderLogic.nearestRevenueIndex(chartX-pad.left,plotWidth,data.revenue_series.length);const group=data.revenue_series[index];const x=data.revenue_series.length===1?pad.left+plotWidth/2:pad.left+index*plotWidth/(data.revenue_series.length-1);const next=revenueTooltip(group,directions);tooltip.replaceChildren(...next.childNodes);tooltip.hidden=false;tooltip.style.left=x+'px';tooltip.classList.toggle('is-left',x-target.scrollLeft>target.clientWidth*.62);hoverLine.setAttribute('visibility','visible');hoverLine.setAttribute('x1',x);hoverLine.setAttribute('x2',x)};
  hoverLayer.addEventListener('pointermove',showTooltip);hoverLayer.addEventListener('pointerdown',showTooltip);hoverLayer.addEventListener('click',showTooltip);hoverLayer.addEventListener('pointerleave',()=>{tooltip.hidden=true;hoverLine.setAttribute('visibility','hidden')});
  target.append(chart,tooltip);
  const legend=$('revenue-legend');legend.replaceChildren();directions.forEach(direction=>{const item=document.createElement('span');const dot=document.createElement('i');dot.className='legend-dot';dot.style.background=directionMeta[direction].color;item.append(dot,document.createTextNode(directionMeta[direction].label));legend.append(item)})
}

function renderPayments(data){
  const summary=$('payment-summary');summary.replaceChildren();data.payment_summary.forEach((payment,index)=>{const card=document.createElement('article');const label=document.createElement('span');label.textContent=payment.name;const amount=document.createElement('strong');amount.textContent=money.format(Number(payment.amount))+' сум';const share=document.createElement('small');share.textContent=payment.share_percent===null?'Доля не вычисляется при нулевом итоге':payment.share_percent+'% выборки';card.style.borderTop=`3px solid ${paymentColors[index%paymentColors.length]}`;card.append(label,amount,share);summary.append(card)});
  if(!data.payment_summary.length){const empty=document.createElement('article');empty.textContent='Оплат за выбранный период нет.';summary.append(empty)}
  const target=$('payment-chart');target.replaceChildren();if(!data.payment_series.length||!data.payment_summary.length){const empty=document.createElement('div');empty.className='empty-chart';empty.textContent='Нет данных для линейного графика.';target.append(empty);return}
  const directions=data.directions,payments=data.payment_summary.map(item=>item.name),series=FounderLogic.paymentLineSeries(data.payment_series,directions,payments);const width=Math.max(720,series.length*58),height=255,pad={left:66,right:18,top:12,bottom:40},plotWidth=width-pad.left-pad.right,plotHeight=height-pad.top-pad.bottom;
  const chart=svg('svg',{viewBox:`0 0 ${width} ${height}`,'aria-hidden':'true'});chart.style.width=width+'px';const all=series.flatMap(group=>payments.map(payment=>Number(group.values[payment]||0)));const max=Math.max(1,...all),min=Math.min(0,...all),span=max-min;
  for(let step=0;step<=4;step+=1){const y=pad.top+plotHeight*step/4;chart.append(svg('line',{x1:pad.left,y1:y,x2:width-pad.right,y2:y,class:'chart-grid'}));const label=svg('text',{x:pad.left-8,y:y+3,'text-anchor':'end',class:'chart-axis'});label.textContent=money.format(max-span*step/4);chart.append(label)}
  const paths=FounderLogic.revenuePaths(series,payments,plotWidth,plotHeight);
  payments.forEach((payment,paymentIndex)=>{const color=paymentColors[paymentIndex%paymentColors.length];const points=paths[payment].split(' ').map(pair=>{const [x,y]=pair.split(',').map(Number);return `${x+pad.left},${y+pad.top}`}).join(' ');chart.append(svg('polyline',{points,class:'payment-line',stroke:color}));series.forEach((group,index)=>{const x=series.length===1?pad.left+plotWidth/2:pad.left+index*plotWidth/(series.length-1);const value=Number(group.values[payment]||0),y=pad.top+plotHeight-(value-min)*plotHeight/span;const point=svg('circle',{cx:x,cy:y,r:3,fill:color,class:'payment-point'});const title=svg('title');title.textContent=`${payment}: ${money.format(value)} сум · ${revenuePeriod(group)}`;point.append(title);chart.append(point)})});
  const labelEvery=Math.max(1,Math.ceil(series.length/6));series.forEach((group,index)=>{if(index%labelEvery!==0&&index!==series.length-1)return;const x=series.length===1?pad.left+plotWidth/2:pad.left+index*plotWidth/(series.length-1);const label=svg('text',{x,y:height-10,'text-anchor':'middle',class:'chart-axis'});label.textContent=shortDate(group.start)+(group.incomplete?' *':'');chart.append(label)});
  const hoverLine=svg('line',{y1:pad.top,y2:pad.top+plotHeight,class:'chart-hover-line',visibility:'hidden'});chart.append(hoverLine);const hoverLayer=svg('rect',{x:pad.left,y:pad.top,width:plotWidth,height:plotHeight,class:'chart-hover-layer'});chart.append(hoverLayer);const tooltip=paymentTooltip(series[0],payments);
  const showTooltip=event=>{const bounds=chart.getBoundingClientRect();const chartX=(event.clientX-bounds.left)/bounds.width*width;const index=FounderLogic.nearestRevenueIndex(chartX-pad.left,plotWidth,series.length);const group=series[index];const x=series.length===1?pad.left+plotWidth/2:pad.left+index*plotWidth/(series.length-1);const next=paymentTooltip(group,payments);tooltip.replaceChildren(...next.childNodes);tooltip.hidden=false;tooltip.style.left=x+'px';tooltip.classList.toggle('is-left',x-target.scrollLeft>target.clientWidth*.62);hoverLine.setAttribute('visibility','visible');hoverLine.setAttribute('x1',x);hoverLine.setAttribute('x2',x)};
  hoverLayer.addEventListener('pointermove',showTooltip);hoverLayer.addEventListener('pointerdown',showTooltip);hoverLayer.addEventListener('click',showTooltip);hoverLayer.addEventListener('pointerleave',()=>{tooltip.hidden=true;hoverLine.setAttribute('visibility','hidden')});target.append(chart,tooltip)
}

function render(data){
  ['retro','school','banquet'].forEach(direction=>{$('total-'+direction).textContent=money.format(Number(data.totals[direction]));document.querySelector(`[data-direction=${direction}]`).hidden=!data.directions.includes(direction)});$('total-selected').textContent=money.format(Number(data.totals.selected));$('updated').textContent='Обновлено '+new Intl.DateTimeFormat('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(data.updated_at));
  const reconcile=$('reconcile');reconcile.classList.toggle('is-warning',!data.reconciled);reconcile.textContent=data.reconciled?'✓ Оплаты сверены · '+money.format(Math.abs(Number(data.discrepancy)))+' сум':`⚠ Не сверено · ${money.format(Math.abs(Number(data.discrepancy)))} сум`;renderRevenue(data);renderPayments(data);const notices=[...data.warnings];if(data.includes_current_day)notices.push('Период включает текущий незавершённый день — он отмечен звёздочкой.');setMessage(notices.join(' '),!data.reconciled)
}

async function load(){
  const directions=selectedDirections();if(!directions.length){setMessage('Выберите хотя бы одно направление.',true);return}controller?.abort();controller=new AbortController();const requestId=gate.next();setLoading(true);setMessage('');const params=new URLSearchParams({start:$('start').value,end:$('end').value,granularity:$('granularity').value,directions:directions.join(',')});
  try{const response=await fetch('/api/founder/analytics?'+params,{signal:controller.signal});if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(body.detail||'Не удалось получить аналитику.')}const data=await response.json();if(gate.isCurrent(requestId))render(data)}catch(error){if(error.name!=='AbortError'&&gate.isCurrent(requestId)){setMessage(error.message,true);$('updated').textContent='Источник недоступен'}}finally{if(gate.isCurrent(requestId))setLoading(false)}
}

async function start(){try{const response=await fetch('/api/config');if(!response.ok)throw new Error();const config=await response.json();$('start').max=config.today;$('end').max=config.today;const period=FounderLogic.quickPeriod('30',config.today);$('start').value=period.start;$('end').value=period.end;await load()}catch(error){setLoading(false);setMessage('Не удалось определить текущую дату сервера.',true)}}
function clearResults(){['retro','school','banquet'].forEach(direction=>{$('total-'+direction).textContent='—';document.querySelector(`[data-direction=${direction}]`).hidden=false});$('total-selected').textContent='—';$('revenue-legend').replaceChildren();$('revenue-chart').replaceChildren();$('payment-summary').replaceChildren();$('payment-chart').replaceChildren();$('reconcile').replaceChildren()}
function invalidatePending(){controller?.abort();controller=null;gate.invalidate();setLoading(false);clearResults();$('updated').textContent='Фильтры изменены · нажмите «Показать»';setMessage('Фильтры изменены. Нажмите «Показать», чтобы загрузить новую выборку.')}
['start','end','granularity'].forEach(id=>$(id).addEventListener('change',invalidatePending));document.querySelectorAll('input[name=direction]').forEach(input=>input.addEventListener('change',invalidatePending));
$('filters').addEventListener('submit',event=>{event.preventDefault();document.querySelectorAll('[data-period]').forEach(button=>button.classList.remove('is-active'));load()});document.querySelectorAll('[data-period]').forEach(button=>button.addEventListener('click',()=>{document.querySelectorAll('[data-period]').forEach(item=>item.classList.toggle('is-active',item===button));const period=FounderLogic.quickPeriod(button.dataset.period,$('end').max);$('start').value=period.start;$('end').value=period.end;load()}));start();
