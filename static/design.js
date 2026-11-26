const NAV_ICONS={control:'layout-dashboard',reports:'chart-no-axes-column',search:'search',ngh:'building-2',recent:'clock',open:'package',delivered:'truck',inventory:'warehouse',imports:'upload',tasks:'list-checks',admin:'settings',problems:'circle-alert'};
function applyAppearance(){
  document.documentElement.dataset.theme=localStorage.getItem('sial-theme')||'light';
  document.documentElement.classList.toggle('sidebar-collapsed',localStorage.getItem('sial-sidebar')==='collapsed');
}
function toggleAppearance(){
  localStorage.setItem('sial-theme',document.documentElement.dataset.theme==='dark'?'light':'dark');
  applyAppearance();
}
function toggleSidebar(){
  localStorage.setItem('sial-sidebar',document.documentElement.classList.contains('sidebar-collapsed')?'expanded':'collapsed');
  applyAppearance();
}
function openGlobalSearch(){
  if(!user||user.role==='warehouse'||!document.querySelector('.side [data-nav="search"]'))return;
  searchAll='';
  go('search');
  document.querySelector('input[type=search]')?.focus();
}
document.addEventListener('keydown',event=>{
  if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='k'&&document.querySelector('.side [data-nav="search"]')){
    event.preventDefault();openGlobalSearch();
  }
});
applyAppearance();
function openOutstandingRegion(region){
  regionFilter=region;poFilter='outstanding';poSearch='';go('open');
}
function regionalChart(orders){
  const counts=new Map();
  for(const order of orders)if(order.region)counts.set(order.region,(counts.get(order.region)||0)+1);
  const sorted=[...counts].sort((a,b)=>b[1]-a[1]);
  const max=sorted[0]?.[1]||1;
  return sorted.map(([region,count],index)=>`<button class="chart-row" onclick="openOutstandingRegion(this.dataset.region)" data-region="${esc(region)}" title="${esc(region)} · ${fmt(count)}">
    <span>${esc(region)}</span><span class="chart-track"><span style="--bar:${count/max*100}%;--delay:${index*30}ms"></span></span><strong>${fmt(count)}</strong>
    </button>`).join('');
}
let overviewFilter='';
function showOverviewRecords(key){
  if(key==='outstanding')return openOutstandingRegion('');
  overviewFilter=key;
  render();
  document.getElementById('overview-records')?.scrollIntoView({block:'nearest'});
}
function overviewRecords(records){
  if(!overviewFilter)return '';
  return `<section id="overview-records" class="card"><button class="secondary" onclick="overviewFilter='';render()">${tr('close')}</button>
    ${table([tr('po'),tr('status'),tr('region')],records.map(p=>`<tr><td><button class="po-link" onclick="openPO(${p.id})">${esc(p.number)}</button></td><td>${st(p.status)}</td><td>${esc(p.region||'')}</td></tr>`))}</section>`;
}
function enhanceTables(){
  document.querySelectorAll('.main .table-wrap').forEach(wrapper=>{
    const table=wrapper.querySelector('table');
    const body=table?.tBodies[0];
    if(!body||body.querySelector('form, tr[hidden], input, select, textarea'))return;
    const records=[...body.rows];
    if(records.length<2)return;
    let page=0, query='', column=-1, descending=false;
    const size=25;
    const controls=document.createElement('div');
    controls.className='table-tools';
    controls.innerHTML=`<input type="search" placeholder="${bilingual('Filter rows','تصفية الصفوف')}" aria-label="${bilingual('Filter rows','تصفية الصفوف')}"><button class="secondary" aria-label="${bilingual('Previous page','الصفحة السابقة')}">←</button><output></output><button class="secondary" aria-label="${bilingual('Next page','الصفحة التالية')}">→</button>`;
    wrapper.before(controls);
    const [previous,next]=controls.querySelectorAll('button');
    const headers=[...table.tHead.rows[0].cells];
    const collator=new Intl.Collator(lang,{numeric:true,sensitivity:'base'});
    function draw(){
      const filtered=records.filter(row=>row.textContent.toLowerCase().includes(query));
      if(column>=0)filtered.sort((a,b)=>(descending?-1:1)*collator.compare(a.cells[column]?.textContent||'',b.cells[column]?.textContent||''));
      const pages=Math.max(1,Math.ceil(filtered.length/size));
      page=Math.min(page,pages-1);
      body.replaceChildren(...filtered.slice(page*size,(page+1)*size));
      controls.querySelector('output').textContent=`${fmt(page+1)} / ${fmt(pages)} · ${fmt(filtered.length)}`;
      previous.disabled=page===0;
      next.disabled=page+1===pages;
    }
    controls.querySelector('input').addEventListener('input',event=>{query=event.target.value.trim().toLowerCase();page=0;draw()});
    previous.onclick=()=>{page--;draw()};
    next.onclick=()=>{page++;draw()};
    headers.forEach((header,index)=>{
      const button=document.createElement('button');
      button.className='table-sort';
      button.textContent=header.textContent;
      if(!button.textContent.trim())return;
      header.replaceChildren(button);
      button.onclick=()=>{
        descending=column===index&&!descending;
        column=index;
        for(const cell of headers)cell.removeAttribute('aria-sort');
        header.setAttribute('aria-sort',descending?'descending':'ascending');
        page=0;draw();
      };
    });
    draw();
  });
}
