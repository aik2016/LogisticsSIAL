async function openShipment(id){
  try{
    const result=await api('shipment-booking/'+id);
    const t=result.task;
    const asnFields=result.requires_asn
      ? field('asn_number','ASN #','text',`required value="${esc(t.asn_number||t.po_asn||'')}"`)+
        field('asn_date','ASN date','date',`required value="${esc(t.asn_date||t.po_asn_date||'')}"`)+
        field('asn_link','ASN URL','url',`value="${esc(t.asn_link||t.po_asn_link||'')}"`)
      : field('shipment_date','Shipment date','date',`required value="${esc(t.planned_delivery_date||'')}"`);
    const lines=result.lines.map(line=>`<section class="shipment-line" data-remaining="${line.available_to_book}"><h3>${esc(line.item_code)} · ${esc(line.line_no)}</h3>
      <p>${fmt(line.quantity)} ordered · ${fmt(line.outstanding_qty)} remaining · ${fmt(line.available_to_book)} unreserved</p>
      ${table([tr('batch'),tr('mfg'),tr('expiry'),tr('free'),'Qty to book'],result.batches.filter(b=>b.item_code===line.item_code).map(b=>
        `<tr><td>${esc(b.batch_no)}</td><td>${esc(b.mfg_date||'—')}</td><td>${esc(b.expiry_date||'—')}</td><td>${fmt(b.free_quantity)}</td><td><input aria-label="Quantity for ${esc(b.batch_no)}" type="number" min="0" step="1" name="line:${line.id}:${b.id}" value="${result.selected[line.id+':'+b.id]||0}"></td></tr>`))}</section>`).join('');
    const canEdit=['open','in_progress','returned','submitted','booked'].includes(t.status);
    shell(`<p><button class="secondary" onclick="go('tasks')">Back</button> <button class="po-link" onclick="openPO(${t.po_id})">Full PO record</button></p>
      <p>${esc(t.address_ar||t.address||'—')}</p>
      ${t.status==='returned'?`<p class="alert">${esc(t.asn_remarks)}</p>`:''}
      ${canEdit?`<form oninput="describeBooking(this)" onsubmit="saveShipment(event,${id})">${lines}<output class="booking-summary"></output><div class="form">${asnFields}${field('remarks','Remarks')}<button class="primary">Save booking</button></div></form>`:''}
      ${t.status==='booked'?`<form class="form" onsubmit="sendShipment(event,${id})">${select('warehouse_user','Warehouse',choices([['','Choose user'],...result.warehouse.map(w=>[w.username,w.username])]),'required')}<button class="primary">Send to Warehouse</button></form>`:''}`,
      'Shipment · '+t.number,st(t.status));
    const form=document.querySelector('form[oninput="describeBooking(this)"]');
    if(form)describeBooking(form);
  }catch(error){toast(error.message)}
}
function describeBooking(form){
  const groups=[...form.querySelectorAll('.shipment-line')];
  const entered=groups.map(group=>[...group.querySelectorAll('input')].reduce((sum,input)=>sum+Number(input.value||0),0));
  const label=entered.some(Boolean)?groups.every((group,index)=>entered[index]===Number(group.dataset.remaining))?'Full booking':'Partial booking':'Enter quantities to book';
  form.querySelector('.booking-summary').textContent=label;
}
function shipmentHistory(po){
  const events=(data.shipment_history||[]).filter(event=>event.po_id===po.id);
  if(!events.length)return '';
  return `<details class="card"><summary>Shipment activity</summary>${table([tr('status'),tr('user'),tr('date'),tr('reason')],events.map(event=>`<tr><td>${st(event.status)}</td><td>${esc(event.actor)}</td><td>${esc(event.created_at)} UTC</td><td>${esc(event.reason)}</td></tr>`))}</details>`;
}
async function saveShipment(event,id){
  event.preventDefault();
  const payload=Object.fromEntries(new FormData(event.target));
  payload.lines=[];
  for(const key of Object.keys(payload)){
    if(!key.startsWith('line:'))continue;
    const [,line,batch]=key.split(':');
    if(Number(payload[key])>0)payload.lines.push({po_line_id:Number(line),batch_id:Number(batch),quantity:Number(payload[key])});
    delete payload[key];
  }
  try{await api('shipment-booking/'+id,'POST',payload);await refresh();await openShipment(id)}
  catch(error){toast(error.message)}
}
async function sendShipment(event,id){
  event.preventDefault();
  try{await api('shipment-booking/'+id+'/send','POST',Object.fromEntries(new FormData(event.target)));await refresh();toast('Sent to warehouse')}
  catch(error){toast(error.message)}
}
function shipmentTracking(){
  if(!user.is_admin)return '';
  const entries=[...asnAssignments,...warehouseAssignments];
  if(!entries.length)return '';
  return `<details class="card"><summary>Shipment assignments · ${fmt(entries.length)}</summary>
    ${table([tr('po'),tr('assignedTo'),tr('status'),''],entries.map(t=>`<tr><td><button class="po-link" onclick="openPO(${t.po_id})">${esc(t.po_number)}</button></td><td>${esc(t.assigned_to)}</td><td>${st(t.status)}</td><td>${reassignControl(t)}</td></tr>`))}</details>`;
}
let lastAdviceBatch=0;
function paymentIntake(){
  if(!can('accounting_edit'))return '';
  return `<section class="actions"><input type="file" id="payment-file" hidden accept=".png,.jpg,.jpeg,.xlsx" onchange="uploadIntake(this,'payment-advice')"><button class="secondary" onclick="document.getElementById('payment-file').click()">Payment</button>
    ${lastAdviceBatch?`<a class="button secondary" href="/api/payment-advice/${lastAdviceBatch}">Download payment report</a>`:''}</section>`;
}
function documentIntake(){
  const types=['GR','MRR','RCV','ASN','Invoice','PO','Rejection Form','Delivery Note','Other'];
  return `<section class="card"><h3>${bilingual('Upload Document','رفع مستند')}</h3><label>Document type<select id="intake-kind" onchange="document.getElementById('intake-file').disabled=!this.value">${choices([['','Choose document type'],...types.map(t=>[t,t])])}</select></label><input id="intake-file" disabled type="file" accept=".pdf,.png,.jpg,.jpeg" onchange="uploadIntake(this,'document-intake')"><div id="document-upload-result" role="status"></div><details ontoggle="if(this.open)loadDocumentReview()"><summary>Files needing review</summary><div id="document-review-list"></div></details></section>`;
}
async function uploadIntake(input,path){
  const file=input.files[0];
  if(!file)return;
  if(path==='due-region-archive'&&!confirm('Apply archive rules using this Due and Region workbook?')){input.value='';return}
  input.disabled=true;
  const payload=new FormData();payload.set('file',file);if(path==='document-intake')payload.set('document_type',document.getElementById('intake-kind').value);
  try{
    const response=await api(path,'POST',payload);
    if(path==='payment-advice'){lastAdviceBatch=response.batch_id;await refresh();toast(`${response.posted} receipts posted`)}
    else if(path==='due-region-archive'){await refresh();const result=document.getElementById('document-upload-result');if(result)result.textContent=`${response.changed} changed. Conflicting POs left for review: ${response.conflicts.join(', ')||'None'}`;toast('Archive rules applied')}
    else{
      const result=document.getElementById('document-upload-result');
      if(result)result.textContent=response.status==='review'?`Review required: ${response.reason}`:`${response.status}: ${response.kind} · ${response.po_number}`;
    }
  }catch(error){toast(error.message)}
  finally{input.disabled=false;input.value=''}
}

async function loadDocumentReview(){
  const target=document.getElementById('document-review-list');
  try{
    const records=await api('document-intake');
    target.innerHTML=table(['File','Reason'],records.map(row=>`<tr><td><a href="/api/document-intake/${row.id}">${esc(row.filename)}</a></td><td>${esc(row.reason)}</td></tr>`));
  }catch(error){toast(error.message)}
}
