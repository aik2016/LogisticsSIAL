const problemVocabulary = {
  category: {
    delivery:['Delivery problems','مشكلات التسليم'], payment:['Payment discrepancies','فروقات الدفع'],
    quantity:['Quantity discrepancies','فروقات الكميات'], invoice:['Invoice discrepancies','فروقات الفواتير'],
    asn:['ASN problems','مشكلات إشعار الشحن'], inventory:['Inventory shortages','نقص المخزون'],
    overdue:['Overdue deliveries','التسليم المتأخر'], integrity:['Data integrity issues','مشكلات سلامة البيانات'],
    manual:['Manually reported problems','مشكلات مبلّغ عنها']
  },
  status: {
    open:['Open','مفتوحة'], investigating:['Under Investigation','قيد التحقيق'],
    awaiting_nupco:['Awaiting NUPCO','بانتظار نوبكو'], resolved:['Resolved','محلولة']
  },
  severity: {
    low:['Low','منخفضة'], medium:['Medium','متوسطة'], high:['High','مرتفعة'], critical:['Critical','حرجة']
  }
};
const bilingual=(english,arabic)=>lang==='ar'?arabic:english;
const problemLabel=(group,key)=>bilingual(...problemVocabulary[group][key]);
const problemChoices=(group,current)=>choices(Object.keys(problemVocabulary[group]).map(key=>[key,problemLabel(group,key)]),current);
let problemData={problems:[],employees:[]};
let problemFilter='', problemQuery='', problemPage=0, problemDescending=true;

async function loadProblemSummary(){
  if(!can('issue_edit')||user.role==='warehouse')return;
  problemData=await api('problems');
}
const problemIndicators={
  backlog:['Unresolved problems','مشكلات غير محلولة',p=>p.status!=='resolved'],
  critical:['Critical problems','مشكلات حرجة',p=>p.status!=='resolved'&&p.severity==='critical'],
  financial:['Financial discrepancies','فروقات مالية',p=>p.status!=='resolved'&&['payment','invoice'].includes(p.category)],
  resolved:['Resolved in 30 days','المحلولة خلال ٣٠ يومًا',p=>p.status==='resolved'&&Date.parse(p.resolved_at?.replace(' ','T')+'Z')>Date.now()-30*86400000]
};
function problemSummary(){
  if(!can('issue_edit')||user.role==='warehouse')return '';
  return '<div class="stats">'+Object.entries(problemIndicators).map(([key,[label,ar,match]])=>
    `<button class="stat kpi" onclick="showProblems('${key}')"><span>${bilingual(label,ar)}</span><strong>${fmt(problemData.problems.filter(match).length)}</strong></button>`).join('')+'</div>';
}
function showProblems(filter=''){
  problemFilter=filter;
  problemPage=0;
  go('problems');
}
function problemForm(problem={}){
  const values=(key)=>`value="${esc(problem[key]??'')}"`;
  const options=problemData.employees.map(x=>[x.username,x.username]);
  if(problem.assignee&&!options.some(x=>x[0]===problem.assignee))options.push([problem.assignee,problem.assignee]);
  const title=(en,ar)=>bilingual(en,ar);
  return `<form class="form" onsubmit="saveProblem(event,${problem.id||0})">
    ${field('po_number',tr('po'),'text',values('po_number')+' required '+(problem.id?'readonly':''))}
    ${field('item_codes',title('Item codes','رموز الأصناف'),'text',values('item_codes'))}
    ${select('category',title('Category','الفئة'),problemChoices('category',problem.category||'manual'))}
    ${select('severity',title('Severity','الخطورة'),problemChoices('severity',problem.severity||'medium'))}
    ${select('status',tr('status'),problemChoices('status',problem.status||'open'))}
    ${select('assignee',tr('assignedTo'),choices([['',title('Unassigned','غير معيّن')],...options],problem.assignee||''))}
    ${field('expected',title('Expected','المتوقع'),'text',values('expected'))}
    ${field('actual',title('Actual','الفعلي'),'text',values('actual'))}
    ${field('impact',title('Impact (SAR)','الأثر بالريال'),'number',values('impact')+' step="0.01"')}
    <label>${title('Remarks','الملاحظات')}<textarea name="remarks" required>${esc(problem.remarks||'')}</textarea></label>
    <label>${title('Internal notes','ملاحظات داخلية')}<textarea name="notes">${esc(problem.notes||'')}</textarea></label>
    <div class="actions"><button class="primary">${tr('save')}</button></div>
  </form>`;
}
function problemList(){
  const query=problemQuery.trim().toLowerCase();
  const results=problemData.problems.filter(p=>(!problemFilter||(problemIndicators[problemFilter]?.[2](p)??p.category===problemFilter))
    &&[p.po_number,p.item_codes,p.remarks,p.assignee].some(value=>value.toLowerCase().includes(query)));
  if(!problemDescending)results.reverse();
  return results.slice(problemPage*20,problemPage*20+20).map(p=>`<details class="card problem-record" ontoggle="if(this.open)loadProblemHistory(${p.id})">
    <summary><strong>${esc(p.po_number)}</strong> · ${problemLabel('category',p.category)} · ${problemLabel('status',p.status)}
    <span class="pill ${p.severity==='critical'?'danger':''}">${problemLabel('severity',p.severity)}</span></summary>
    <p>${esc(p.remarks)}</p><small>${esc(p.detected_at)} UTC</small>
    ${problemForm(p)}
    <form class="actions" onsubmit="attachProblemFile(event,${p.id})"><input type="file" name="file" required><button class="secondary">${bilingual('Attach evidence','إرفاق مستند')}</button></form>
    <div id="problem-history-${p.id}"></div>
    </details>`).join('')+
    `<div class="actions"><button class="secondary" ${problemPage===0?'disabled':''} onclick="problemPage--;refreshProblemList()">←</button>
      <span>${fmt(results.length)} · ${fmt(problemPage+1)} / ${fmt(Math.max(1,Math.ceil(results.length/20)))}</span>
      <button class="secondary" ${(problemPage+1)*20>=results.length?'disabled':''} onclick="problemPage++;refreshProblemList()">→</button></div>`;
}
function refreshProblemList(reset=false){
  if(reset)problemPage=0;
  document.getElementById('problem-list').innerHTML=problemList();
}
function problemsView(){
  const filters=[['',bilingual('All problems','كل المشكلات')],...Object.entries(problemIndicators).map(([key,[en,ar]])=>[key,bilingual(en,ar)]),
    ...Object.keys(problemVocabulary.category).map(key=>[key,problemLabel('category',key)])];
  shell(problemSummary()+`<div class="card actions">
    <input type="search" value="${esc(problemQuery)}" aria-label="${tr('search')}" placeholder="${tr('search')}" oninput="problemQuery=this.value;refreshProblemList(true)">
    ${select('problem_filter',tr('status'),choices(filters,problemFilter),'onchange="problemFilter=this.value;refreshProblemList(true)"')}
    <button class="secondary" onclick="problemDescending=!problemDescending;refreshProblemList(true)">${bilingual('Reverse detection order','عكس ترتيب الاكتشاف')}</button>
    <a class="button secondary" href="/api/problems-export">${bilingual('Export Excel','تصدير إكسل')}</a></div>
    <details class="card"><summary>${bilingual('Report a problem','الإبلاغ عن مشكلة')}</summary>${problemForm()}</details>
    ${can('accounting_edit')?`<details class="card"><summary>${bilingual('Reconcile payment file','مطابقة ملف المدفوعات')}</summary>
      <p>${bilingual('Required columns: PO Number, Payment Reference, Amount Received (SAR).','الأعمدة المطلوبة: PO Number و Payment Reference و Amount Received (SAR).')}</p>
      <form class="actions" onsubmit="importPaymentEvidence(event)"><input type="file" name="file" accept=".xlsx" required><button class="primary">${bilingual('Reconcile','مطابقة')}</button></form></details>`:''}
    <section id="problem-list">${problemList()}</section>`,tr('problems'));
}
async function saveProblem(event,id){
  event.preventDefault();
  const payload=Object.fromEntries(new FormData(event.target));
  payload.impact=payload.impact===''?null:Number(payload.impact);
  try{await api(id?'problems/'+id:'problems',id?'PUT':'POST',payload);await loadProblemSummary();render();toast(tr('save'))}
  catch(error){toast(error.message)}
}
async function loadProblemHistory(id){
  try{
    const result=await api('problems/'+id+'/history');
    const target=document.getElementById('problem-history-'+id);
    if(!target)return;
    target.innerHTML=`<h4>${bilingual('Activity and evidence','النشاط والمستندات')}</h4>`
      +result.files.map(file=>`<p><a href="/api/problems/files/${file.id}">${esc(file.filename)}</a></p>`).join('')
      +result.events.map(event=>`<details><summary>${esc(event.event_at)} UTC · ${esc(event.actor)} · ${esc(event.action)}</summary><pre>${esc(JSON.stringify(JSON.parse(event.snapshot),null,2))}</pre></details>`).join('');
  }catch(error){toast(error.message)}
}
async function attachProblemFile(event,id){
  event.preventDefault();
  try{await api('problems/'+id+'/files','POST',new FormData(event.target));event.target.reset();await loadProblemHistory(id)}
  catch(error){toast(error.message)}
}
async function importPaymentEvidence(event){
  event.preventDefault();
  try{
    const result=await api('problems/payment-file','POST',new FormData(event.target));
    await loadProblemSummary();problemsView();
    toast(bilingual('New discrepancies','فروقات جديدة')+' · '+fmt(result.problems_created));
  }catch(error){toast(error.message)}
}

function poProblemHistory(po){
  if(!can('issue_edit')||user.role==='warehouse')return '';
  const records=problemData.problems.filter(problem=>problem.po_id===po.id);
  if(!records.length)return '';
  return `<section class="card"><h3>${bilingual('PO problems','مشكلات أمر الشراء')}</h3>${records.map(problem=>`
    <details class="inline-problem" ontoggle="if(this.open)loadProblemHistory(${problem.id})">
      <summary>${problemLabel('category',problem.category)} · ${problemLabel('status',problem.status)}</summary>
      <p>${esc(problem.remarks)}</p>${problemForm(problem)}<div id="problem-history-${problem.id}"></div>
    </details>`).join('')}</section>`;
}
