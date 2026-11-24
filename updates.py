"""Operational additions. Uploaded files are data and remain outside the public source tree."""
import hashlib
import io
import json
import math
import os
import re
import secrets
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from openpyxl import load_workbook
from pydantic import BaseModel
from pypdf import PdfReader, PdfWriter

from app import DB, app, business_today, db, rows, pallet_size, outstanding_by_line
from imports import decode as decode_master, money, qty

DOCS = Path(os.getenv('LOGISTICS_DOCUMENTS', DB.parent / 'documents'))
DOCS.mkdir(parents=True, exist_ok=True)
MAX_FILE = 15_000_000
CORE_PO = ['open', 'asn_booked', 'dispatched', 'delivered', 'gr_issued', 'asn_cancelled', 'asn_rejected']
CORE_INVOICE = ['rcv_needed', 'rcv_uploaded', 'ready_to_upload', 'invoiced_up', 'paid', 'partially_paid']


def fail(message, status=400):
    raise HTTPException(status, message)


def stored_file(c, po_id, filename, content):
    if len(content) > MAX_FILE: fail('File exceeds 15 MB', 413)
    if not content.startswith(b'%PDF-'): fail('A PDF document is required')
    clean = Path(filename).name[:120] or 'document.pdf'
    name = secrets.token_hex(16) + '.pdf'
    (DOCS / name).write_bytes(content)
    c.execute('INSERT INTO po_documents(po_id,filename,stored_name) VALUES(?,?,?)', (po_id, clean, name))
    return name


@app.get('/api/pos/{po_id}/documents')
def list_documents(po_id: int):
    with db() as c:
        return rows(c, 'SELECT id,po_id,filename,uploaded_at FROM po_documents WHERE po_id=? ORDER BY id DESC', (po_id,))


@app.get('/api/documents/{document_id}')
def download_document(document_id: int):
    with db() as c:
        row = c.execute('SELECT filename,stored_name FROM po_documents WHERE id=?', (document_id,)).fetchone()
    if not row: fail('Document not found', 404)
    path = DOCS / row['stored_name']
    if not path.is_file(): fail('Stored document is missing', 404)
    return FileResponse(path, media_type='application/pdf', filename=row['filename'])


@app.post('/api/po-pdf/bulk-preview')
async def bulk_preview(files: list[UploadFile] = File(...)):
    if not files or len(files) > 100: fail('Choose 1 to 100 PDF files')
    from pdf_extract import extract
    output = []
    for f in files:
        if not f.filename.lower().endswith('.pdf'): fail(f'{f.filename}: PDF required')
        content = await f.read()
        try:
            result = extract(content)
            output.append({'filename': f.filename, 'fingerprint': hashlib.sha256(content).hexdigest(), **result})
        except HTTPException as e:
            output.append({'filename': f.filename, 'error': e.detail})
    return {'files': output, 'ready': all(x.get('po_number') and x.get('items') and not x.get('warnings') and not x.get('error') for x in output)}


@app.post('/api/po-pdf/bulk-approve')
async def bulk_approve(request: Request, files: list[UploadFile] = File(...), fingerprints: str = Form(...)):
    try: approved = json.loads(fingerprints)
    except (ValueError, TypeError): fail('Invalid approval fingerprints')
    if len(files) != len(approved) or not files or len(files) > 100: fail('File count changed since preview')
    from pdf_extract import extract
    prepared = []
    for f, expected in zip(files, approved):
        content = await f.read()
        if hashlib.sha256(content).hexdigest() != expected: fail(f'{f.filename}: file changed since preview')
        result = extract(content)
        if not result['po_number'] or not result['items'] or result['warnings']: fail(f'{f.filename}: resolve extraction warnings first')
        prepared.append((f.filename, content, result))
    grouped = defaultdict(list)
    for filename, content, result in prepared: grouped[result['po_number']].append((filename, content, result))
    written = []
    try:
        with db() as c:
            for number, files_for_po in grouped.items():
                kind = 'marketplace' if number.startswith('300') else 'tender'
                profile=files_for_po[0][2]
                existing = c.execute('SELECT id FROM pos WHERE number=?', (number,)).fetchone()
                if existing: po_id = existing['id']
                else: po_id = c.execute('INSERT INTO pos(number,kind,customer,customer_po,value) VALUES(?,?,?,?,?)',
                                        (number,kind,profile['customer'],profile['customer_po'],profile['value'] or 0)).lastrowid
                extracted = []
                for _, _, result in files_for_po:
                    for item in result['items']:
                        for index,schedule in enumerate(item['schedules'],1):
                            raw = schedule['due_date']
                            try: due = datetime.strptime(raw, '%d/%m/%Y').date().isoformat()
                            except ValueError: fail(f'{number}: invalid due date {raw}')
                            line_no=item.get('line_no') or f"{item['page']*10:04d}"
                            if len(item['schedules'])>1:line_no=f'{line_no}-{index}'
                            extracted.append((line_no,item['item_code'], schedule['quantity'], due, item['address']))
                # The same PO can be attached twice; do not duplicate its item lines.
                extracted = list(dict.fromkeys(extracted))
                if not extracted: fail(f'{number}: no dated item lines')
                current = rows(c, 'SELECT item_code,quantity,due_date,address FROM po_lines WHERE po_id=? ORDER BY line_no', (po_id,))
                if current:
                    a = sorted((x['item_code'],x['quantity'],x['due_date']) for x in current)
                    b = sorted((x[1],x[2],x[3]) for x in extracted)
                    if a != b: fail(f'{number}: existing PO lines differ from this PDF. Review manually before replacing.',409)
                else:
                    for n,(line_no,code,quantity,due,address) in enumerate(extracted,1):
                        pack = 250 if code == 'CTND-200' else 100 if code.startswith('RIMZP-') else 200 if code.startswith(('RIM-','RIB-')) else 1
                        c.execute('INSERT INTO items(code,pcs_per_box,boxes_per_pallet) VALUES(?,?,?) ON CONFLICT(code) DO NOTHING', (code,pack,pallet_size(code)))
                        c.execute('INSERT INTO po_lines(po_id,line_no,item_code,quantity,due_date,address) VALUES(?,?,?,?,?,?)', (po_id,line_no,code,quantity,due,address))
                    c.execute("UPDATE pos SET address=COALESCE(NULLIF(address,''),?) WHERE id=?", (extracted[0][4],po_id))
                c.execute("UPDATE pos SET customer=CASE WHEN customer='' THEN ? ELSE customer END,customer_po=CASE WHEN customer_po='' THEN ? ELSE customer_po END,value=CASE WHEN value IS NULL OR value=0 THEN ? ELSE value END WHERE id=?",
                          (profile['customer'],profile['customer_po'],profile['value'] or 0,po_id))
                for filename,content,_ in files_for_po:
                    name = stored_file(c,po_id,filename,content)
                    written.append(name)
                c.execute('INSERT INTO audit(user_id,action,target) VALUES(?,?,?)', (request.state.user['id'],'approve_po_pdfs',number))
    except Exception:
        for name in written: (DOCS / name).unlink(missing_ok=True)
        raise
    return {'po_count':len(grouped),'documents':len(prepared),'po_numbers':list(grouped)}


@app.post('/api/po-pdf/merge')
async def merge_pdfs(files: list[UploadFile] = File(...)):
    if len(files) < 2 or len(files) > 50: fail('Choose 2 to 50 PDFs')
    writer = PdfWriter()
    for f in files:
        content = await f.read()
        if len(content)>MAX_FILE or not content.startswith(b'%PDF-'): fail(f'{f.filename}: invalid or oversized PDF')
        try: writer.append(PdfReader(io.BytesIO(content)))
        except Exception: fail(f'{f.filename}: cannot read PDF')
    output = io.BytesIO(); writer.write(output); output.seek(0)
    return StreamingResponse(output, media_type='application/pdf', headers={'Content-Disposition':'attachment; filename="merged-po-documents.pdf"'})


@app.get('/api/invoices/next-number')
def next_invoice_number(po_id: int):
    with db() as c:
        po = c.execute('SELECT number FROM pos WHERE id=?',(po_id,)).fetchone()
        if not po: fail('PO not found',404)
        prefix = 'M' if po['number'].startswith('300') else 'T'
        used = {r['number'].upper() for r in rows(c,'SELECT number FROM invoices WHERE UPPER(number) LIKE ?', (prefix+'%',))}
        n=1
        while f'{prefix}{n:04d}' in used:n+=1
        return {'next_number':f'{prefix}{n:04d}','prefix':prefix}


class Transfer(BaseModel):
    target_po_id: int
    confirmed: bool = False


@app.post('/api/invoices/{invoice_id}/transfer')
def transfer_invoice_number(invoice_id:int, v:Transfer, request:Request):
    if not v.confirmed: fail('Confirm transfer of the existing booking')
    with db() as c:
        inv=c.execute('SELECT * FROM invoices WHERE id=?',(invoice_id,)).fetchone()
        target=c.execute('SELECT * FROM pos WHERE id=?',(v.target_po_id,)).fetchone()
        if not inv or not target: fail('Invoice or target PO not found',404)
        if inv['status']!='booked': fail('Only a booked, undispatched invoice can be transferred')
        if inv['po_id']==v.target_po_id: fail('Invoice already belongs to this PO')
        if not inv['number'].upper().startswith('M' if target['number'].startswith('300') else 'T'): fail('Invoice prefix does not match target PO')
        source_lines=rows(c,'SELECT il.id,il.quantity,l.item_code FROM invoice_lines il JOIN po_lines l ON l.id=il.po_line_id WHERE il.invoice_id=?',(invoice_id,))
        target_lines=rows(c,'''SELECT l.id,l.po_id,l.item_code,l.quantity,l.due_date,
          COALESCE((SELECT SUM(il.quantity) FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id
            WHERE il.po_line_id=l.id AND i.status IN ('delivered','gr_issued','uploaded','paid')),0) delivered_qty
          FROM po_lines l WHERE l.po_id=? ORDER BY l.id''',(v.target_po_id,))
        remaining=outstanding_by_line(c,target_lines)
        used={r['id']:c.execute("SELECT COALESCE(SUM(il.quantity),0) FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id WHERE il.po_line_id=? AND i.status IN ('booked','dispatched')",(r['id'],)).fetchone()[0] for r in target_lines}
        mapped=[]
        for source in source_lines:
            match=next((r for r in target_lines if r['item_code']==source['item_code'] and remaining[r['id']]-used[r['id']]>=source['quantity']),None)
            if not match: fail('Target PO has no matching item line with enough unbooked quantity')
            used[match['id']]+=source['quantity'];mapped.append((match['id'],source['id']))
        for line_id,invoice_line_id in mapped:c.execute('UPDATE invoice_lines SET po_line_id=? WHERE id=?',(line_id,invoice_line_id))
        c.execute('UPDATE invoices SET po_id=? WHERE id=?',(v.target_po_id,invoice_id))
        c.execute('INSERT INTO invoice_number_transfers(number,from_po_id,to_po_id,user_id) VALUES(?,?,?,?)',(inv['number'],inv['po_id'],v.target_po_id,request.state.user['id']))
    return {'ok':True}


class POStatus(BaseModel):
    status: str
    reason: str = ''
    confirmed: bool = False


def options(c, key, core):
    row=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    if not row:return core
    try:return json.loads(row['value'])
    except ValueError:return core


@app.get('/api/status-options')
def get_status_options():
    with db() as c:return {'po':options(c,'po_status_options',CORE_PO),'invoice':options(c,'invoice_status_options',CORE_INVOICE)}


class StatusOptions(BaseModel):
    group: str
    options: list[str]


@app.put('/api/status-options')
def save_status_options(v:StatusOptions, request:Request):
    if not request.state.user['is_admin']:fail('Admin only',403)
    if v.group not in ('po','invoice'): fail('Unknown status group')
    core=CORE_PO if v.group=='po' else CORE_INVOICE
    cleaned=list(dict.fromkeys(x.strip().lower().replace(' ','_') for x in v.options if x.strip()))
    if not set(core).issubset(cleaned):fail('Core statuses cannot be removed because workflows depend on them')
    if any(not re.fullmatch(r'[a-z][a-z0-9_]{1,39}',x) for x in cleaned):fail('Use letters, numbers and underscores in status codes')
    with db() as c:c.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(v.group+'_status_options',json.dumps(cleaned)))
    return {'ok':True}


@app.post('/api/pos/{po_id}/operational-status')
async def set_po_status(request:Request,po_id:int,status:str=Form(...),reason:str=Form(''),confirmed:str=Form('false'),asn_number:str=Form(''),asn_date:str=Form(''),asn_link:str=Form(''),file:UploadFile|None=File(None)):
    if confirmed!='true':fail('Confirmation required')
    content=await file.read() if file else None
    with db() as c:
        po=c.execute('SELECT * FROM pos WHERE id=?',(po_id,)).fetchone()
        if not po:fail('PO not found',404)
        if status not in options(c,'po_status_options',CORE_PO):fail('Unknown status')
        if status=='asn_booked' and (not (asn_number.strip() or asn_link.strip()) or not asn_date.strip()):fail('ASN number or link, and ASN date, are required')
        if status=='gr_issued' and not content:fail('Attach the GR PDF')
        if status=='asn_rejected' and not content and reason.strip().upper()!='N/A':fail('Attach rejection PDF or enter N/A as the reason')
        if status in ('delivered','gr_issued'):
            ordered=c.execute('SELECT COALESCE(SUM(quantity),0) FROM po_lines WHERE po_id=?',(po_id,)).fetchone()[0]
            delivered=c.execute('''SELECT COALESCE(SUM(il.quantity),0) FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id
              WHERE i.po_id=? AND i.status IN ('delivered','gr_issued','uploaded','paid')''',(po_id,)).fetchone()[0]
            if ordered and delivered<ordered:fail('Mark all linked invoice quantities delivered first so inventory is deducted')
        if po['status'] in ('delivered','gr_issued') and status=='open':
            if not request.state.user['is_admin']:fail('Only an admin can reopen a delivered PO',403)
            if not reason.strip():fail('A reason is required to reopen this PO')
        if status=='asn_cancelled' and not reason.strip():fail('Enter the ASN cancellation reason')
        if content: stored_file(c,po_id,file.filename,content)
        c.execute('UPDATE pos SET status=?,asn_number=CASE WHEN ?="" THEN asn_number ELSE ? END,asn_date=CASE WHEN ?="" THEN asn_date ELSE ? END,asn_link=CASE WHEN ?="" THEN asn_link ELSE ? END,notes=CASE WHEN ?="" THEN notes ELSE COALESCE(notes,"") || CHAR(10) || ? END WHERE id=?',(status,asn_number,asn_number,asn_date,asn_date,asn_link,asn_link,reason,reason,po_id))
        c.execute('INSERT INTO po_status_events(po_id,status,reason,user_id) VALUES(?,?,?,?)',(po_id,status,reason,request.state.user['id']))
    return {'ok':True}


@app.patch('/api/pos/{po_id}/invoice-stage')
def set_invoice_stage(po_id:int,v:POStatus,request:Request):
    if not v.confirmed:fail('Confirmation required')
    with db() as c:
        po=c.execute('SELECT * FROM pos WHERE id=?',(po_id,)).fetchone()
        if not po:fail('PO not found',404)
        if v.status not in options(c,'invoice_status_options',CORE_INVOICE):fail('Unknown invoice stage')
        if v.status=='partially_paid' and not v.reason.strip():fail('Enter the partial payment reason')
        if v.status=='paid' and po['status'] not in ('delivered','gr_issued'):fail('PO must be delivered or GR issued before archiving as paid')
        c.execute('UPDATE pos SET invoice_status=?,archived=?,notes=CASE WHEN ?="" THEN notes ELSE COALESCE(notes,"") || CHAR(10) || ? END WHERE id=?',(v.status,int(v.status=='paid'),v.reason,v.reason,po_id))
        c.execute('INSERT INTO po_status_events(po_id,status,reason,user_id) VALUES(?,?,?,?)',(po_id,'invoice:'+v.status,v.reason,request.state.user['id']))
    return {'ok':True}


@app.patch('/api/pos/{po_id}/marketplace-stage')
def set_marketplace_stage(po_id:int,v:POStatus):
    if not v.confirmed:fail('Confirmation required')
    if v.status not in ('delivered','rcv_returned'):fail('Unknown marketplace stage')
    if v.status=='rcv_returned' and not v.reason.strip():fail('Return reason required')
    with db() as c:
        po=c.execute('SELECT kind FROM pos WHERE id=?',(po_id,)).fetchone()
        if not po or po['kind']!='marketplace':fail('Marketplace PO required')
        c.execute('UPDATE pos SET marketplace_status=?,marketplace_reason=? WHERE id=?',(v.status,v.reason,po_id))
    return {'ok':True}


@app.get('/api/reports/operations')
def operations_report(region:str='',include_archived:bool=False):
    with db() as c:
        sql='''SELECT p.id po_id,p.number,p.region,p.address,p.status,p.invoice_status,p.value,l.id,l.item_code,l.quantity,l.due_date,
          i.pcs_per_box,i.boxes_per_pallet,
          COALESCE((SELECT SUM(il.quantity) FROM invoice_lines il JOIN invoices v ON v.id=il.invoice_id WHERE il.po_line_id=l.id AND v.status IN ('delivered','gr_issued','uploaded','paid')),0) delivered_qty,
          COALESCE((SELECT SUM(il.quantity) FROM invoice_lines il JOIN invoices v ON v.id=il.invoice_id WHERE il.po_line_id=l.id AND v.status IN ('booked','dispatched')),0) booked_qty
          FROM po_lines l JOIN pos p ON p.id=l.po_id JOIN items i ON i.code=l.item_code WHERE (?='' OR p.region=?) AND (? OR p.archived=0)'''
        lines=rows(c,sql,(region,region,int(include_archived)))
        outstanding=outstanding_by_line(c,lines)
        by_item=defaultdict(int);by_region_item=defaultdict(int);pick=[]
        for line in lines:
            quantity=outstanding[line['id']]
            line['outstanding_qty']=quantity
            line['boxes']=math.ceil(quantity/line['pcs_per_box']) if line['pcs_per_box'] else 0
            line['pallets']=math.ceil(line['boxes']/line['boxes_per_pallet']) if line['boxes_per_pallet'] else None
            if quantity:
                by_item[line['item_code']]+=quantity
                by_region_item[(line['region'] or 'Unassigned',line['item_code'])]+=quantity
            if line['status']=='asn_booked' and quantity:
                line['batches']=rows(c,'SELECT batch_no,quantity FROM batches WHERE item_code=? AND quantity>0 ORDER BY expiry_date',(line['item_code'],))
                pick.append(line)
        return {'by_item':[{'item_code':k,'quantity':v} for k,v in sorted(by_item.items())],
                'by_region_item':[{'region':r,'item_code':i,'quantity':v} for (r,i),v in sorted(by_region_item.items())],
                'warehouse_pick_list':pick,'regions':sorted({x['region'] for x in lines if x['region']})}


# Starting inventory workbook: item code, batch number, quantity, manufacture date, expiry date.
def decode_stock(content):
    if len(content)>12_000_000:fail('Workbook exceeds 12 MB',413)
    try:book=load_workbook(io.BytesIO(content),read_only=True,data_only=True)
    except Exception:fail('Could not read XLSX workbook')
    sheet=book.active
    records=[];errors=[]
    header=[str(x or '').strip().lower().replace(' ','_') for x in next(sheet.iter_rows(values_only=True))]
    aliases={'item_code':['item_code','item','code'],'batch_no':['batch_no','batch_number','batch'],'quantity':['quantity','qty','qty_(pcs)','pieces'],'mfg_date':['mfg_date','manufacturing_date','manufacture_date'],'expiry_date':['expiry_date','expiration_date','expiry']}
    indexes={k:next((header.index(a) for a in names if a in header),None) for k,names in aliases.items()}
    if any(indexes[k] is None for k in ('item_code','batch_no','quantity')):fail('Required columns: Item Code, Batch No, Quantity')
    for n,row in enumerate(sheet.iter_rows(min_row=2,values_only=True),2):
        if not any(v is not None for v in row):continue
        try:
            val=lambda k:row[indexes[k]] if indexes[k] is not None and indexes[k]<len(row) else None
            code=str(val('item_code') or '').strip().upper();batch=str(val('batch_no') or '').strip()
            if not re.fullmatch(r'[A-Z0-9]+(?:-[A-Z0-9]+)+',code) or not batch:raise ValueError('Invalid item or batch')
            count=money(val('quantity'))
            if count<0 or int(count)!=count:raise ValueError('Quantity must be a non-negative whole number of pieces')
            count=int(count)
            def date_value(k):
                x=val(k)
                if isinstance(x,datetime):return x.date().isoformat()
                if isinstance(x,date):return x.isoformat()
                if not x:return ''
                for fmt in ('%Y-%m-%d','%d/%m/%Y','%d.%m.%Y'):
                    try:return datetime.strptime(str(x),fmt).date().isoformat()
                    except ValueError:pass
                raise ValueError(f'Invalid {k}')
            records.append({'row':n,'item_code':code,'batch_no':batch,'quantity':count,'mfg_date':date_value('mfg_date'),'expiry_date':date_value('expiry_date')})
        except ValueError as e:errors.append({'row':n,'error':str(e)})
    if len({(r['item_code'],r['batch_no']) for r in records})!=len(records):errors.append({'error':'Duplicate item/batch in workbook'})
    return records,errors


@app.post('/api/inventory/starting-preview')
async def stock_preview(file:UploadFile=File(...)):
    content=await file.read();records,errors=decode_stock(content)
    return {'rows':len(records),'errors':errors[:30],'error_count':len(errors),'sample':records[:8],
            'fingerprint':hashlib.sha256(content).hexdigest(),'ready':bool(records) and not errors}


@app.post('/api/inventory/starting-apply')
async def stock_apply(file:UploadFile=File(...),fingerprint:str=Form(...)):
    content=await file.read()
    if hashlib.sha256(content).hexdigest()!=fingerprint:fail('Workbook changed since preview')
    records,errors=decode_stock(content)
    if errors or not records:fail('Correct workbook errors before applying')
    source='starting-stock:'+fingerprint[:16]
    with db() as c:
        if c.execute('SELECT 1 FROM source_rows WHERE source_name=?',(source,)).fetchone():return {'already_imported':True,'rows':0}
        for r in records:
            existing=c.execute('SELECT id FROM batches WHERE item_code=? AND batch_no=?',(r['item_code'],r['batch_no'])).fetchone()
            if existing and c.execute('SELECT 1 FROM movements WHERE batch_id=? LIMIT 1',(existing['id'],)).fetchone():fail(f"{r['item_code']} / {r['batch_no']}: batch has movement history; cannot reset its starting quantity",409)
        for r in records:
            code=r['item_code'];pack=250 if code=='CTND-200' else 100 if code.startswith('RIMZP-') else 200 if code.startswith(('RIM-','RIB-')) else 1
            c.execute('INSERT INTO items(code,pcs_per_box,boxes_per_pallet) VALUES(?,?,?) ON CONFLICT(code) DO NOTHING',(code,pack,pallet_size(code)))
            c.execute('''INSERT INTO batches(item_code,batch_no,mfg_date,expiry_date,quantity) VALUES(?,?,?,?,?)
              ON CONFLICT(item_code,batch_no) DO UPDATE SET mfg_date=excluded.mfg_date,expiry_date=excluded.expiry_date,quantity=excluded.quantity''',(code,r['batch_no'],r['mfg_date'],r['expiry_date'],r['quantity']))
            c.execute('INSERT INTO source_rows(source_name,sheet,row_number,row_json) VALUES(?,?,?,?)',(source,'Stock',r['row'],json.dumps(r)))
    return {'already_imported':False,'rows':len(records)}

REGION_SHEETS={'RUH':'Riyadh','North':'North','East':'East','West':'West','Qassim':'Qassim','Med':'Madinah','South':'South'}
SIAL_SHEETS={'Paid':'paid','Uploaded not paid':'uploaded_not_paid','Delivered':'delivered','Outstanding':'outstanding'}


def decode_seed(content,kind):
    if kind=='masterlist':
        records,errors=decode_master(content,'masterlist')
        return records,errors
    if len(content)>12_000_000:fail('Workbook exceeds 12 MB',413)
    try:book=load_workbook(io.BytesIO(content),read_only=True,data_only=True)
    except Exception:fail('Could not read XLSX workbook')
    records=[];errors=[]
    if kind=='due_region':
        if not set(REGION_SHEETS).issubset(book.sheetnames):fail('Expected RUH, North, East, West, Qassim, Med and South sheets')
        for sheet,region in REGION_SHEETS.items():
            values=book[sheet].iter_rows(values_only=True)
            header=[str(x or '').strip().upper() for x in next(values)]
            po_index=next((i for i,h in enumerate(header) if h in ('PO #','PO NUMBER')),None)
            if po_index is None:errors.append({'sheet':sheet,'error':'PO # column missing'});continue
            for n,row in enumerate(values,2):
                po=str(row[po_index] or '').strip() if po_index<len(row) else ''
                if re.fullmatch(r'(?:300|4300|4600)\d+',po) and len(po)==10:
                    records.append({'sheet':sheet,'row':n,'po':po,'region':region})
    elif kind=='sial_formatted':
        if not set(SIAL_SHEETS).issubset(book.sheetnames):fail('Expected Paid, Uploaded not paid, Delivered and Outstanding sheets')
        for sheet,stage in SIAL_SHEETS.items():
            for n,row in enumerate(book[sheet].iter_rows(min_row=9,values_only=True),9):
                po=str(row[0] or '').strip()
                if not re.fullmatch(r'\d{10}',po):continue
                try:
                    records.append({'sheet':sheet,'row':n,'po':po,'stage':stage,'value':money(row[1]),
                                    'address':str(row[5] or '').strip(),'status':str(row[6] or '').strip(),
                                    'invoice_reference':str(row[7] or '').strip(),'invoice_status':str(row[8] or '').strip(),
                                    'remaining_detail':str(row[9] or '').strip(),'remarks':str(row[10] or '').strip()})
                except (ValueError,IndexError) as e:errors.append({'sheet':sheet,'row':n,'error':str(e)})
    else:fail('Supported types: masterlist, due_region, sial_formatted')
    return records,errors


@app.post('/api/seed/{kind}/preview')
async def seed_preview(kind:str,file:UploadFile=File(...)):
    content=await file.read();records,errors=decode_seed(content,kind)
    with db() as c:
        known={r['number'] for r in rows(c,'SELECT number FROM pos')}
    po_set={r['po'] for r in records}
    return {'kind':kind,'rows':len(records),'distinct_pos':len(po_set),'matched_pos':len(po_set&known),
            'new_pos':len(po_set-known),'errors':errors[:30],'error_count':len(errors),
            'sample':records[:5],'fingerprint':hashlib.sha256(content).hexdigest(),
            'ready':bool(records) and not errors}


@app.post('/api/seed/{kind}/apply')
async def seed_apply(kind:str,file:UploadFile=File(...),fingerprint:str=Form(...)):
    content=await file.read()
    if hashlib.sha256(content).hexdigest()!=fingerprint:fail('Workbook changed since preview')
    records,errors=decode_seed(content,kind)
    if errors or not records:fail('Correct preview errors first')
    source=kind+':'+fingerprint[:16]
    with db() as c:
        if c.execute('SELECT 1 FROM source_rows WHERE source_name=? LIMIT 1',(source,)).fetchone():
            return {'already_imported':True,'rows':0}
        if kind=='masterlist':
            grouped=defaultdict(list)
            for r in records:grouped[r['po']].append(r)
            for number,group in grouped.items():
                existing=c.execute('SELECT id FROM pos WHERE number=?',(number,)).fetchone()
                if existing:
                    prior=rows(c,'SELECT item_code,quantity,due_date FROM po_lines WHERE po_id=?',(existing['id'],))
                    expected=sorted((r['item'],r['quantity'],r['due_date']) for r in group)
                    actual=sorted((r['item_code'],r['quantity'],r['due_date']) for r in prior)
                    if actual and actual!=expected:fail(f'{number}: existing PO lines differ from masterlist; no changes applied',409)
            for number,group in grouped.items():
                kind_value='marketplace' if number.startswith('300') else 'tender'
                value=round(sum(v for _,v in {(r['item'],r['value']) for r in group}),2)
                c.execute('INSERT INTO pos(number,kind,address,value) VALUES(?,?,?,?) ON CONFLICT(number) DO NOTHING',(number,kind_value,group[0]['address'],value))
                pid=c.execute('SELECT id FROM pos WHERE number=?',(number,)).fetchone()['id']
                c.execute("UPDATE pos SET value=CASE WHEN value IS NULL OR value=0 THEN ? ELSE value END,address=CASE WHEN address='' THEN ? ELSE address END WHERE id=?",(value,group[0]['address'],pid))
                if not c.execute('SELECT 1 FROM po_lines WHERE po_id=? LIMIT 1',(pid,)).fetchone():
                    for n,r in enumerate(group,1):
                        code=r['item'];pack=250 if code=='CTND-200' else 100 if code.startswith('RIMZP-') else 200 if code.startswith(('RIM-','RIB-')) else 1
                        c.execute('INSERT INTO items(code,pcs_per_box,boxes_per_pallet) VALUES(?,?,?) ON CONFLICT(code) DO NOTHING',(code,pack,pallet_size(code)))
                        c.execute('INSERT INTO po_lines(po_id,line_no,item_code,quantity,due_date,address) VALUES(?,?,?,?,?,?)',(pid,f'{n*10:04d}',code,r['quantity'],r['due_date'],r['address']))
        elif kind=='due_region':
            mapping=defaultdict(set)
            for r in records:mapping[r['po']].add(r['region'])
            conflict={po:sorted(regions) for po,regions in mapping.items() if len(regions)>1}
            if conflict:fail(f'POs appear in multiple regions: {str(conflict)[:300]}',409)
            for number,regions in mapping.items():
                region=next(iter(regions))
                c.execute('INSERT INTO pos(number,kind,region) VALUES(?,?,?) ON CONFLICT(number) DO NOTHING',(number,'marketplace' if number.startswith('300') else 'tender',region))
                c.execute('UPDATE pos SET region=? WHERE number=?',(region,number))
        else:
            unique={}
            for r in records:
                if r['po'] in unique and unique[r['po']]['stage']!=r['stage']:
                    fail(f"{r['po']}: appears in multiple Sial report stages; review before applying",409)
                unique[r['po']]=r
            for number,r in unique.items():
                c.execute('INSERT INTO pos(number,kind,address,value) VALUES(?,?,?,?) ON CONFLICT(number) DO NOTHING',(number,'marketplace' if number.startswith('300') else 'tender',r['address'],r['value']))
                c.execute("UPDATE pos SET value=?,address=CASE WHEN address='' THEN ? ELSE address END WHERE number=?",(r['value'],r['address'],number))
                pid=c.execute('SELECT id FROM pos WHERE number=?',(number,)).fetchone()['id']
                manual=c.execute('SELECT 1 FROM po_status_events e JOIN pos p ON p.id=e.po_id WHERE p.number=? LIMIT 1',(number,)).fetchone()
                if not manual:
                    status='delivered' if r['stage'] in ('paid','uploaded_not_paid','delivered') else 'open'
                    inv_stage='paid' if r['stage']=='paid' else 'invoiced_up' if r['stage']=='uploaded_not_paid' else 'rcv_needed'
                    c.execute('UPDATE pos SET status=?,invoice_status=?,archived=? WHERE number=?',(status,inv_stage,int(r['stage']=='paid'),number))
                if not c.execute('SELECT 1 FROM invoices WHERE po_id=? LIMIT 1',(pid,)).fetchone():
                    remaining={}
                    if r['stage']=='outstanding':
                        for code,amount in re.findall(r'([A-Z]+(?:-[A-Z0-9]+)+)\s*:\s*([\d,]+)',r['remaining_detail']):
                            remaining[code]=remaining.get(code,0)+int(amount.replace(',',''))
                        if not remaining:fail(f'{number}: cannot read outstanding item quantities',409)
                    for line in rows(c,'SELECT DISTINCT item_code FROM po_lines WHERE po_id=?',(pid,)):
                        code=line['item_code']
                        c.execute('''INSERT INTO po_remaining_snapshot(po_id,item_code,remaining_qty,source_name) VALUES(?,?,?,?)
                          ON CONFLICT(po_id,item_code) DO UPDATE SET remaining_qty=excluded.remaining_qty,source_name=excluded.source_name''',
                          (pid,code,remaining.get(code,0),source))
                c.execute('INSERT INTO source_status(source_name,po_number,value,status,invoice_reference,invoice_status,remaining_detail,remarks) VALUES(?,?,?,?,?,?,?,?)',
                          (source,number,r['value'],r['status'],r['invoice_reference'],r['invoice_status'],r['remaining_detail'],r['remarks']))
        for r in records:
            c.execute('INSERT INTO source_rows(source_name,sheet,row_number,row_json) VALUES(?,?,?,?)',(source,r['sheet'],r['row'],json.dumps(r,ensure_ascii=False)))
    return {'already_imported':False,'rows':len(records),'distinct_pos':len({r['po'] for r in records})}
