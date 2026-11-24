"""Operational additions. Uploaded files are data and remain outside the public source tree."""
import hashlib
import io
import json
import math
import os
import re
import secrets
import sqlite3
import threading
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
BACKUPS = DB.parent / 'backups'
BACKUPS.mkdir(parents=True, exist_ok=True)
BACKUP_LOCK = threading.Lock()
MAX_FILE = 15_000_000
CORE_PO = ['open', 'asn_booked', 'dispatched', 'dispatch_cancelled', 'delivered', 'gr_issued', 'asn_cancelled', 'asn_rejected']
CORE_INVOICE = ['rcv_needed', 'rcv_uploaded', 'ready_to_upload', 'invoiced_up', 'paid', 'partially_paid']


def fail(message, status=400):
    raise HTTPException(status, message)


def create_backup(label):
    """Snapshot the entire live SQLite database before an import or a reset."""
    backup_id=datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(4)
    target=BACKUPS / (backup_id+'.db')
    try:
        with sqlite3.connect(DB) as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        (BACKUPS / (backup_id+'.json')).write_text(json.dumps({'id':backup_id,'label':label,'created_at':datetime.utcnow().isoformat()+'Z'}))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return backup_id


def primary_admin(request):
    if not request.state.user['is_admin']:fail('Primary admin only',403)
    with db() as c:
        owner=c.execute("SELECT id FROM users WHERE username='admin' AND is_admin=1").fetchone()
        if not owner:owner=c.execute('SELECT id FROM users WHERE is_admin=1 ORDER BY id LIMIT 1').fetchone()
    if not owner or owner['id']!=request.state.user['id']:fail('Primary admin only',403)


@app.get('/api/admin/backups')
def list_backups(request:Request):
    primary_admin(request)
    return [json.loads(p.read_text()) for p in sorted(BACKUPS.glob('*.json'),reverse=True)[:30]]


@app.get('/api/admin/backups/{backup_id}')
def download_backup(backup_id:str,request:Request):
    primary_admin(request)
    if not re.fullmatch(r'\d{8}T\d{6}Z-[0-9a-f]{8}',backup_id):fail('Invalid backup ID')
    path=BACKUPS / (backup_id+'.db')
    if not path.is_file():fail('Backup not found',404)
    return FileResponse(path,media_type='application/octet-stream',filename=f'sial-backup-{backup_id}.db')


@app.get('/api/admin/reset-preview')
def reset_preview(request:Request):
    primary_admin(request)
    with db() as c:
        counts={name:c.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0] for name in ('pos','po_lines','invoices','invoice_lines','po_documents','po_status_events','source_rows','source_status','regional_po_rows','po_remaining_snapshot','po_invoice_numbers')}
        counts['linked_tasks']=c.execute('SELECT COUNT(*) FROM tasks WHERE po_id IS NOT NULL').fetchone()[0]
        counts['linked_issues']=c.execute('SELECT COUNT(*) FROM issues WHERE po_id IS NOT NULL OR invoice_id IS NOT NULL').fetchone()[0]
        counts['invoice_movements']=c.execute('SELECT COUNT(*) FROM movements WHERE invoice_id IS NOT NULL').fetchone()[0]
    return counts


class ResetConfirmation(BaseModel):
    phrase:str


@app.post('/api/admin/reset-pos')
def reset_all_pos(v:ResetConfirmation,request:Request):
    primary_admin(request)
    if v.phrase!='RESET ALL PURCHASE ORDERS':fail('Type RESET ALL PURCHASE ORDERS exactly')
    if not BACKUP_LOCK.acquire(blocking=False):fail('Another database operation is in progress',409)
    app.state.maintenance=True
    try:
        backup_id=create_backup('Before resetting all POs and work history')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            for r in rows(c,'SELECT batch_id,SUM(quantity) net FROM movements WHERE invoice_id IS NOT NULL GROUP BY batch_id'):
                c.execute('UPDATE batches SET quantity=quantity-? WHERE id=?',(r['net'],r['batch_id']))
            c.execute('DELETE FROM movements WHERE invoice_id IS NOT NULL')
            c.execute('DELETE FROM invoice_lines')
            c.execute('DELETE FROM invoices')
            c.execute('DELETE FROM issues WHERE po_id IS NOT NULL OR invoice_id IS NOT NULL')
            c.execute('DELETE FROM tasks WHERE po_id IS NOT NULL')
            for name in ('po_status_events','invoice_number_transfers','po_documents','po_remaining_snapshot','po_invoice_numbers','regional_po_rows','source_status','nupco_records','po_lines','pos'):
                c.execute(f'DELETE FROM {name}')
            c.execute("DELETE FROM source_rows WHERE source_name NOT LIKE 'starting-stock:%'")
            c.execute('INSERT INTO audit(user_id,action,target) VALUES(?,?,?)',(request.state.user['id'],'reset_all_pos',backup_id))
        return {'ok':True,'backup_id':backup_id}
    finally:
        app.state.maintenance=False
        BACKUP_LOCK.release()


class RestoreConfirmation(BaseModel):
    backup_id:str
    phrase:str


@app.post('/api/admin/restore-backup')
def restore_backup(v:RestoreConfirmation,request:Request):
    primary_admin(request)
    if v.phrase!='RESTORE DATABASE':fail('Type RESTORE DATABASE exactly')
    if not re.fullmatch(r'\d{8}T\d{6}Z-[0-9a-f]{8}',v.backup_id):fail('Invalid backup ID')
    source_path=BACKUPS / (v.backup_id+'.db')
    if not source_path.is_file():fail('Backup not found',404)
    if not BACKUP_LOCK.acquire(blocking=False):fail('Another database operation is in progress',409)
    app.state.maintenance=True
    try:
        safety=create_backup('Before restoring '+v.backup_id)
        with sqlite3.connect(source_path) as source,sqlite3.connect(DB) as destination:
            source.backup(destination)
        return {'ok':True,'safety_backup_id':safety}
    finally:
        app.state.maintenance=False
        BACKUP_LOCK.release()


def stored_file(c, po_id, filename, content):
    if len(content) > MAX_FILE: fail('File exceeds 15 MB', 413)
    if not content.startswith(b'%PDF-'): fail('A PDF document is required')
    digest=hashlib.sha256(content).hexdigest()
    existing=rows(c,'SELECT stored_name,sha256 FROM po_documents WHERE po_id=?',(po_id,))
    for old in existing:
        if old['sha256']==digest:return None
        if not old['sha256'] and (DOCS / old['stored_name']).is_file() and hashlib.sha256((DOCS / old['stored_name']).read_bytes()).hexdigest()==digest:
            c.execute('UPDATE po_documents SET sha256=? WHERE stored_name=?',(digest,old['stored_name']))
            return None
    clean = Path(filename).name[:120] or 'document.pdf'
    name = secrets.token_hex(16) + '.pdf'
    (DOCS / name).write_bytes(content)
    c.execute('INSERT INTO po_documents(po_id,filename,stored_name,sha256) VALUES(?,?,?,?)', (po_id, clean, name,digest))
    return name


@app.get('/api/pos/{po_id}/documents')
def list_documents(po_id: int):
    with db() as c:
        return rows(c, 'SELECT id,po_id,filename,document_type,uploaded_at FROM po_documents WHERE po_id=? ORDER BY id DESC', (po_id,))


@app.get('/api/documents/{document_id}')
def document_access(c,document_id,request):
    row=c.execute('SELECT d.filename,d.stored_name,d.po_id FROM po_documents d WHERE d.id=?',(document_id,)).fetchone()
    if not row:fail('Document not found',404)
    u=request.state.user
    if not u['is_admin'] and 'pos_edit' not in u['permissions'].split(','):
        linked=c.execute('SELECT 1 FROM tasks WHERE po_id=? AND assigned_to=? LIMIT 1',(row['po_id'],u['username'])).fetchone()
        if not linked:fail('Document access denied',403)
    return row


def download_document(document_id: int,request:Request):
    with db() as c:
        row = document_access(c,document_id,request)
    path = DOCS / row['stored_name']
    if not path.is_file(): fail('Stored document is missing', 404)
    return FileResponse(path, filename=row['filename'])


@app.get('/api/documents/{document_id}/view')
def view_document(document_id:int,request:Request):
    with db() as c:row=document_access(c,document_id,request)
    path=DOCS / row['stored_name']
    if not path.is_file():fail('Stored document is missing',404)
    import mimetypes
    media=mimetypes.guess_type(row['filename'])[0] or 'application/octet-stream'
    if media not in ('application/pdf','image/png','image/jpeg','image/webp','text/plain'):
        return FileResponse(path,filename=row['filename'])
    return FileResponse(path,media_type=media,headers={'Content-Disposition':'inline'})


@app.post('/api/pos/{po_id}/documents/upload')
async def upload_related_document(po_id:int,document_type:str=Form(...),file:UploadFile=File(...)):
    allowed={'PO','MRR','RCV','Rejection Form','GR','ASN','Invoice','Other'}
    if document_type not in allowed:fail('Choose a document type')
    content=await file.read()
    if not content or len(content)>MAX_FILE:fail('File must be 1 byte to 15 MB')
    suffix=Path(file.filename or '').suffix.lower()
    if suffix not in ('.pdf','.png','.jpg','.jpeg','.webp','.txt','.docx','.xlsx'):
        fail('Unsupported document format')
    if suffix=='.pdf' and not content.startswith(b'%PDF-'):fail('Invalid PDF')
    if suffix=='.png' and not content.startswith(b'\x89PNG\r\n\x1a\n'):fail('Invalid PNG')
    if suffix in ('.jpg','.jpeg') and not content.startswith(b'\xff\xd8'):fail('Invalid JPEG')
    if suffix in ('.docx','.xlsx') and not content.startswith(b'PK'):fail('Invalid Office file')
    digest=hashlib.sha256(content).hexdigest()
    with db() as c:
        if not c.execute('SELECT 1 FROM pos WHERE id=?',(po_id,)).fetchone():fail('PO not found',404)
        existing=c.execute('SELECT id FROM po_documents WHERE po_id=? AND sha256=?',(po_id,digest)).fetchone()
        if existing:return {'id':existing['id'],'duplicate':True}
        name=secrets.token_hex(16)+suffix
        (DOCS / name).write_bytes(content)
        try:
            doc_id=c.execute('INSERT INTO po_documents(po_id,filename,stored_name,sha256,document_type) VALUES(?,?,?,?,?)',
                 (po_id,Path(file.filename).name[:120],name,digest,document_type)).lastrowid
        except Exception:
            (DOCS / name).unlink(missing_ok=True)
            raise
    return {'id':doc_id,'duplicate':False}


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
    backup_id=create_backup('Before bulk PO PDF approval')
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
                    if name:written.append(name)
                c.execute('INSERT INTO audit(user_id,action,target) VALUES(?,?,?)', (request.state.user['id'],'approve_po_pdfs',number))
    except Exception:
        for name in written: (DOCS / name).unlink(missing_ok=True)
        raise
    return {'po_count':len(grouped),'documents_added':len(written),'duplicates_skipped':len(prepared)-len(written),'po_numbers':list(grouped),'backup_id':backup_id}


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
        assigned=c.execute('SELECT number FROM po_invoice_numbers WHERE po_id=?',(po_id,)).fetchone()
        if assigned:return {'next_number':assigned['number'],'prefix':assigned['number'][0],'reused':True}
        old=c.execute('SELECT invoice_reference FROM regional_po_rows WHERE po_number=? AND TRIM(invoice_reference)<>"" ORDER BY id DESC LIMIT 1',(po['number'],)).fetchone()
        old_number=(old['invoice_reference'] or '').strip().upper() if old else ''
        if re.fullmatch(r'(?:0\d{2,}|N\d{2,})',old_number):
            return {'next_number':old_number,'prefix':old_number[0],'reused':True}
        prefix = 'M' if po['number'].startswith('300') else 'T'
        used={r['number'].upper() for r in rows(c,'SELECT number FROM po_invoice_numbers WHERE UPPER(number) LIKE ?', (prefix+'%',))}
        used.update(r['reference_number'].upper() for r in rows(c,'SELECT COALESCE(NULLIF(reference_number,\'\'),number) reference_number FROM invoices WHERE UPPER(COALESCE(NULLIF(reference_number,\'\'),number)) LIKE ?', (prefix+'%',)))
        for source in rows(c,'SELECT invoice_reference FROM source_status UNION ALL SELECT invoice_reference FROM regional_po_rows'):
            used.update(x.upper() for x in re.findall(r'\b[MT]\d{4,}\b',source['invoice_reference'] or '',re.I))
        n=1
        while f'{prefix}{n:04d}' in used:n+=1
        return {'next_number':f'{prefix}{n:04d}','prefix':prefix,'reused':False}


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
        if status in ('asn_cancelled','dispatch_cancelled') and not reason.strip():fail('Enter the cancellation reason')
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
                assigned=rows(c,'''SELECT b.batch_no,SUM(il.quantity) quantity FROM invoice_lines il
                  JOIN invoices v ON v.id=il.invoice_id JOIN batches b ON b.id=il.batch_id
                  WHERE v.po_id=? AND b.item_code=? AND v.status IN ('booked','dispatched') GROUP BY b.id''',(line['po_id'],line['item_code']))
                line['batches']=[{**b,'source':'booked'} for b in assigned] if assigned else [
                    {**b,'source':'available'} for b in rows(c,'SELECT batch_no,quantity FROM batches WHERE item_code=? AND quantity>0 ORDER BY expiry_date',(line['item_code'],))]
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
    backup_id=create_backup('Before starting inventory import')
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
    return {'already_imported':False,'rows':len(records),'backup_id':backup_id}

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
            lookup=lambda row,*names:next((row[header.index(name)] for name in names if name in header and header.index(name)<len(row) and row[header.index(name)] is not None),None)
            as_text=lambda value:'' if value is None else str(value).strip()
            prior_po='';prior_address=''
            for n,row in enumerate(values,2):
                po=str(row[po_index] or '').strip() if po_index<len(row) else ''
                if re.fullmatch(r'(?:300|4300|4600)\d+',po) and len(po)==10:
                    prior_po=po;prior_address=''
                elif not po and prior_po and lookup(row,'ITEM'):
                    po=prior_po
                else:continue
                address=str(lookup(row,'DELIVERY ADDRESS') or prior_address).strip()
                if address:prior_address=address
                raw_date=lookup(row,'DUE DATE')
                if isinstance(raw_date,datetime):raw_date=raw_date.date().isoformat()
                elif isinstance(raw_date,date):raw_date=raw_date.isoformat()
                asn_date=lookup(row,'ASN DATE','ASN IDATE')
                if isinstance(asn_date,datetime):asn_date=asn_date.date().isoformat()
                elif isinstance(asn_date,date):asn_date=asn_date.isoformat()
                records.append({'sheet':sheet,'row':n,'po':po,'region':region,
                    'status':as_text(lookup(row,'STATUS')),
                    'invoice_reference':as_text(lookup(row,'INV#','INV #')),
                    'asn_number':as_text(lookup(row,'ASN ID')),
                    'asn_date':as_text(asn_date),
                    'item_text':as_text(lookup(row,'ITEM')),
                    'planned_qty':as_text(lookup(row,'P. QTY')),
                    'left_qty':as_text(lookup(row,'LEFT QTY','LEFT')),
                    'due_date':as_text(raw_date),'address':address,
                    'customer_po':as_text(lookup(row,'CUST PO'))})
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
        if c.execute('SELECT 1 FROM source_rows WHERE source_name=? LIMIT 1',(source,)).fetchone():return {'already_imported':True,'rows':0}
    backup_id=create_backup('Before '+kind+' workbook import')
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
                if not c.execute('SELECT 1 FROM po_status_events e JOIN pos p ON p.id=e.po_id WHERE p.number=? LIMIT 1',(number,)).fetchone():
                    raw=next((str(r['status']).lower() for r in records if r['po']==number and r['status']), '')
                    status='asn_booked' if 'asn' in raw and 'book' in raw else 'dispatched' if 'dispatch' in raw else 'open'
                    c.execute('UPDATE pos SET status=?,archived=0 WHERE number=?',(status,number))
            for r in records:
                c.execute('''INSERT INTO regional_po_rows(source_name,sheet,row_number,po_number,region,status,invoice_reference,asn_number,asn_date,item_text,planned_qty,left_qty,due_date,address,customer_po)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(source,r['sheet'],r['row'],r['po'],r['region'],r['status'],r['invoice_reference'],r['asn_number'],r['asn_date'],r['item_text'],r['planned_qty'],r['left_qty'],r['due_date'],r['address'],r['customer_po']))
                c.execute("UPDATE pos SET address=CASE WHEN address='' THEN ? ELSE address END,customer_po=CASE WHEN customer_po='' THEN ? ELSE customer_po END,asn_number=CASE WHEN asn_number='' THEN ? ELSE asn_number END,asn_date=CASE WHEN asn_date='' THEN ? ELSE asn_date END WHERE number=?",
                          (r['address'],r['customer_po'],r['asn_number'],r['asn_date'],r['po']))
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
                regional_outstanding=bool(c.execute('SELECT 1 FROM regional_po_rows WHERE po_number=? LIMIT 1',(number,)).fetchone())
                if not manual and not regional_outstanding:
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
                c.execute('INSERT INTO source_status(source_name,po_number,value,status,invoice_reference,invoice_status,remaining_detail,remarks,stage) VALUES(?,?,?,?,?,?,?,?,?)',
                          (source,number,r['value'],r['status'],r['invoice_reference'],r['invoice_status'],r['remaining_detail'],r['remarks'],r['stage']))
        for r in records:
            c.execute('INSERT INTO source_rows(source_name,sheet,row_number,row_json) VALUES(?,?,?,?)',(source,r['sheet'],r['row'],json.dumps(r,ensure_ascii=False)))
    return {'already_imported':False,'rows':len(records),'distinct_pos':len({r['po'] for r in records}),'backup_id':backup_id}

class ASNAssignmentBatch(BaseModel):
    po_ids:list[int]
    assigned_to:str
    due_date:date


@app.post('/api/asn-assignments')
def assign_asn_batch(v:ASNAssignmentBatch,request:Request):
    if not request.state.user['is_admin']:fail('Admin only',403)
    ids=list(dict.fromkeys(v.po_ids))
    if not ids or len(ids)>100:fail('Select 1 to 100 outstanding POs')
    with db() as c:
        target=c.execute('SELECT id,username FROM users WHERE username=? AND active=1',(v.assigned_to.strip(),)).fetchone()
        if not target:fail('Select an active user')
        used={r['number'].upper() for r in rows(c,'SELECT number FROM po_invoice_numbers')}
        used.update(r['reference_number'].upper() for r in rows(c,"SELECT COALESCE(NULLIF(reference_number,''),number) reference_number FROM invoices"))
        for source in rows(c,'SELECT invoice_reference FROM source_status UNION ALL SELECT invoice_reference FROM regional_po_rows'):
            used.update(x.upper() for x in re.findall(r'\b[MT]\d{4,}\b',source['invoice_reference'] or '',re.I))
        selected=[]
        for po_id in ids:
            po=c.execute('SELECT id,number,region,address,archived FROM pos WHERE id=?',(po_id,)).fetchone()
            if not po:fail(f'PO {po_id} not found',404)
            if po['archived']:fail(f"{po['number']} is archived")
            pending=c.execute("SELECT 1 FROM tasks WHERE po_id=? AND task_type='asn_booking' AND status IN ('open','in_progress','submitted','returned') LIMIT 1",(po_id,)).fetchone()
            if pending:fail(f"{po['number']} already has an active ASN assignment",409)
            lines=rows(c,'''SELECT l.id,l.po_id,l.item_code,l.quantity,l.due_date,
              COALESCE((SELECT SUM(il.quantity) FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id
              WHERE il.po_line_id=l.id AND i.status IN ('delivered','gr_issued','uploaded','paid')),0) delivered_qty
              FROM po_lines l WHERE l.po_id=?''',(po_id,))
            if not lines or not sum(outstanding_by_line(c,lines).values()):fail(f"{po['number']} has no outstanding item quantity")
            selected.append((po,lines))
        selected.sort(key=lambda pair:(min(x['due_date'] for x in pair[1]),pair[0]['number']))
        output=[]
        for po,lines in selected:
            assigned=c.execute('SELECT number FROM po_invoice_numbers WHERE po_id=?',(po['id'],)).fetchone()
            if assigned:number=assigned['number']
            else:
                old=c.execute('SELECT invoice_reference FROM regional_po_rows WHERE po_number=? AND TRIM(invoice_reference)<>"" ORDER BY id DESC LIMIT 1',(po['number'],)).fetchone()
                old_number=(old['invoice_reference'] or '').strip().upper() if old else ''
                if re.fullmatch(r'(?:0\d{2,}|N\d{2,})',old_number):
                    number=old_number
                    conflict=c.execute('SELECT po_id FROM po_invoice_numbers WHERE number=?',(number,)).fetchone()
                    if conflict and conflict['po_id']!=po['id']:fail(f'{number} already belongs to another PO; review before assigning',409)
                else:
                    prefix='M' if po['number'].startswith('300') else 'T'
                    n=1
                    while f'{prefix}{n:04d}' in used:n+=1
                    number=f'{prefix}{n:04d}'
                c.execute('INSERT INTO po_invoice_numbers(po_id,number,assigned_by) VALUES(?,?,?)',(po['id'],number,request.state.user['id']))
                used.add(number)
            details=f"PO {po['number']} · {po['region'] or 'Unassigned'} · Invoice {number} · {po['address'] or 'No address'}"
            task_id=c.execute('''INSERT INTO tasks(title,assigned_to,po_id,due_date,status,details,created_by,task_type,invoice_number)
              VALUES(?,?,?,?,'open',?,?, 'asn_booking',?)''',('Book ASN',target['username'],po['id'],v.due_date.isoformat(),details,request.state.user['id'],number)).lastrowid
            output.append({'po_id':po['id'],'po_number':po['number'],'invoice_number':number,'task_id':task_id})
        c.execute('INSERT INTO audit(user_id,action,target) VALUES(?,?,?)',(request.state.user['id'],'assign_asn_batch',','.join(x['po_number'] for x in output)))
    return {'assignments':output}


class ASNSubmission(BaseModel):
    asn_number:str=''
    asn_date:date
    asn_link:str=''
    remarks:str=''


@app.post('/api/asn-assignments/{task_id}/submit')
def submit_asn(task_id:int,v:ASNSubmission,request:Request):
    if not v.asn_number.strip() and not v.asn_link.strip():fail('ASN number or link required')
    with db() as c:
        task=c.execute("SELECT * FROM tasks WHERE id=? AND task_type='asn_booking'",(task_id,)).fetchone()
        if not task:fail('ASN task not found',404)
        if task['assigned_to']!=request.state.user['username'] and not request.state.user['is_admin']:fail('This task belongs to another user',403)
        if task['status'] not in ('open','in_progress','returned'):fail('Task cannot be submitted from this state')
        c.execute("UPDATE tasks SET status='submitted',asn_number=?,asn_date=?,asn_link=?,asn_remarks=?,submitted_at=CURRENT_TIMESTAMP WHERE id=?",
                  (v.asn_number.strip(),v.asn_date.isoformat(),v.asn_link.strip(),v.remarks.strip(),task_id))
        c.execute('INSERT INTO audit(user_id,action,target) VALUES(?,?,?)',(request.state.user['id'],'submit_asn',str(task_id)))
    return {'ok':True,'status':'submitted'}


class ASNReview(BaseModel):
    action:str
    reason:str=''


@app.post('/api/asn-assignments/{task_id}/review')
def review_asn(task_id:int,v:ASNReview,request:Request):
    if not request.state.user['is_admin']:fail('Admin only',403)
    if v.action not in ('accept','return'):fail('Choose accept or return')
    if v.action=='return' and not v.reason.strip():fail('Enter a reason for returning the task')
    with db() as c:
        task=c.execute("SELECT * FROM tasks WHERE id=? AND task_type='asn_booking'",(task_id,)).fetchone()
        if not task:fail('ASN task not found',404)
        if task['status']!='submitted':fail('Only submitted ASN tasks can be reviewed')
        if v.action=='accept':
            c.execute("UPDATE tasks SET status='done',reviewed_at=CURRENT_TIMESTAMP WHERE id=?",(task_id,))
            c.execute('UPDATE pos SET status=?,asn_number=?,asn_date=?,asn_link=? WHERE id=?',
                      ('asn_booked',task['asn_number'],task['asn_date'],task['asn_link'],task['po_id']))
            c.execute('INSERT INTO po_status_events(po_id,status,reason,user_id) VALUES(?,?,?,?)',
                      (task['po_id'],'asn_booked',task['asn_remarks'],request.state.user['id']))
        else:
            c.execute("UPDATE tasks SET status='returned',details=details || CHAR(10) || ? ,reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
                      ('Returned: '+v.reason.strip(),task_id))
        c.execute('INSERT INTO audit(user_id,action,target) VALUES(?,?,?)',(request.state.user['id'],'review_asn:'+v.action,str(task_id)))
    return {'ok':True,'status':'done' if v.action=='accept' else 'returned'}


@app.get('/api/asn-assignments')
def list_asn_assignments(request:Request):
    with db() as c:
        if request.state.user['is_admin']:
            return rows(c,"SELECT t.*,p.number po_number,p.region,p.address FROM tasks t JOIN pos p ON p.id=t.po_id WHERE t.task_type='asn_booking' ORDER BY CASE t.status WHEN 'submitted' THEN 0 WHEN 'returned' THEN 1 ELSE 2 END,t.id DESC")
        return rows(c,"SELECT t.*,p.number po_number,p.region,p.address FROM tasks t JOIN pos p ON p.id=t.po_id WHERE t.task_type='asn_booking' AND t.assigned_to=? ORDER BY t.id DESC",(request.state.user['username'],))


class WarehouseAssignment(BaseModel):
    po_ids:list[int]
    assigned_to:str
    due_date:date


@app.post('/api/warehouse-assignments')
def assign_warehouse(v:WarehouseAssignment,request:Request):
    if not request.state.user['is_admin']:fail('Admin only',403)
    ids=list(dict.fromkeys(v.po_ids))
    if not ids or len(ids)>100:fail('Select 1 to 100 POs')
    with db() as c:
        target=c.execute('SELECT id,username FROM users WHERE username=? AND active=1',(v.assigned_to.strip(),)).fetchone()
        if not target:fail('Select an active user')
        output=[]
        for po_id in ids:
            po=c.execute('SELECT * FROM pos WHERE id=?',(po_id,)).fetchone()
            if not po:fail(f'PO {po_id} not found',404)
            if po['archived'] or po['status'] in ('delivered','gr_issued'):fail(f"{po['number']} cannot be dispatched")
            if po['kind']!='marketplace' and po['status']!='asn_booked':fail(f"{po['number']}: ASN must be booked first")
            if c.execute("SELECT 1 FROM tasks WHERE po_id=? AND task_type='warehouse_dispatch' AND status IN ('open','in_progress')",(po_id,)).fetchone():
                fail(f"{po['number']} is already assigned to warehouse",409)
            assigned=c.execute('SELECT number FROM po_invoice_numbers WHERE po_id=?',(po_id,)).fetchone()
            source=c.execute('SELECT invoice_reference FROM regional_po_rows WHERE po_number=? ORDER BY id DESC LIMIT 1',(po['number'],)).fetchone()
            invoice=assigned['number'] if assigned else (source['invoice_reference'] if source else '')
            details=f"PO {po['number']} · Invoice {invoice or 'Not entered'} · ASN {po['asn_number'] or 'Not required'} · {po['address'] or 'Address not entered'}"
            task_id=c.execute('''INSERT INTO tasks(title,assigned_to,po_id,due_date,status,details,created_by,task_type,invoice_number,asn_number,asn_date,asn_link)
              VALUES(?,?,?,?,'open',?,?,'warehouse_dispatch',?,?,?,?)''',
              ('Prepare and dispatch',target['username'],po_id,v.due_date.isoformat(),details,request.state.user['id'],invoice,po['asn_number'],po['asn_date'],po['asn_link'])).lastrowid
            output.append({'task_id':task_id,'po_number':po['number']})
    return {'assignments':output}


@app.get('/api/warehouse-assignments')
def list_warehouse_assignments(request:Request):
    with db() as c:
        query="SELECT t.*,p.number po_number,p.kind,p.status po_status,p.address,p.region FROM tasks t JOIN pos p ON p.id=t.po_id WHERE t.task_type='warehouse_dispatch'"
        if request.state.user['is_admin']:return rows(c,query+' ORDER BY t.id DESC')
        return rows(c,query+' AND t.assigned_to=? ORDER BY t.id DESC',(request.state.user['username'],))


class WarehouseUpdate(BaseModel):
    status:str
    reason:str=''


@app.post('/api/warehouse-assignments/{task_id}/status')
def warehouse_status(task_id:int,v:WarehouseUpdate,request:Request):
    if v.status not in ('dispatched','dispatch_cancelled'):fail('Choose Dispatched or Dispatch Cancelled')
    if v.status=='dispatch_cancelled' and not v.reason.strip():fail('Enter a cancellation reason')
    with db() as c:
        task=c.execute("SELECT t.*,p.kind,p.status po_status FROM tasks t JOIN pos p ON p.id=t.po_id WHERE t.id=? AND t.task_type='warehouse_dispatch'",(task_id,)).fetchone()
        if not task:fail('Warehouse assignment not found',404)
        if task['assigned_to']!=request.state.user['username'] and not request.state.user['is_admin']:fail('Assignment belongs to another user',403)
        if task['status'] not in ('open','in_progress'):fail('Assignment is already closed')
        if task['kind']!='marketplace' and task['po_status']!='asn_booked':fail('Tender PO requires booked ASN')
        if task['po_status'] not in ('open','asn_booked'):fail('PO cannot be dispatched from its current status')
        c.execute('UPDATE pos SET status=? WHERE id=?',(v.status,task['po_id']))
        c.execute('UPDATE tasks SET status=?,details=details || CHAR(10) || ? WHERE id=?',('done' if v.status=='dispatched' else 'cancelled',v.reason,task_id))
        c.execute('INSERT INTO po_status_events(po_id,status,reason,user_id) VALUES(?,?,?,?)',(task['po_id'],v.status,v.reason,request.state.user['id']))
    return {'ok':True,'status':v.status}

@app.post('/api/po-documents/bulk-preview')
async def soft_copy_preview(files:list[UploadFile]=File(...)):
    from pdf_extract import extract
    if not files or len(files)>100:fail('Choose 1 to 100 PDF files')
    with db() as c:known={r['number']:r['id'] for r in rows(c,'SELECT id,number FROM pos')}
    output=[]
    for file in files:
        content=await file.read()
        try:
            result=extract(content)
            number=result['po_number']
            if not number:raise ValueError('PO number not found in PDF')
            with db() as c:
                duplicate=bool(c.execute('SELECT 1 FROM po_documents WHERE po_id=? AND sha256=?',(known.get(number,-1),hashlib.sha256(content).hexdigest())).fetchone())
            output.append({'filename':file.filename,'po_number':number,'known':number in known,'duplicate':duplicate,'fingerprint':hashlib.sha256(content).hexdigest(),'warnings':result['warnings']})
        except (HTTPException,ValueError) as e:
            output.append({'filename':file.filename,'error':e.detail if isinstance(e,HTTPException) else str(e)})
    return {'files':output,'ready':all(x.get('known') and not x.get('error') for x in output),
            'known_count':sum(bool(x.get('known')) for x in output),'duplicates':sum(bool(x.get('duplicate')) for x in output)}


@app.post('/api/po-documents/bulk-attach')
async def soft_copy_attach(files:list[UploadFile]=File(...),fingerprints:str=Form(...)):
    from pdf_extract import extract
    try:approved=json.loads(fingerprints)
    except ValueError:fail('Invalid approval fingerprints')
    if not files or len(files)>100 or len(files)!=len(approved):fail('File count changed since preview')
    prepared=[]
    for file,expected in zip(files,approved):
        content=await file.read()
        if hashlib.sha256(content).hexdigest()!=expected:fail(f'{file.filename}: file changed since preview')
        result=extract(content)
        if not result['po_number']:fail(f'{file.filename}: PO number not found')
        prepared.append((file.filename,content,result['po_number']))
    with db() as c:
        known={r['number']:r['id'] for r in rows(c,'SELECT id,number FROM pos')}
    missing=sorted({number for _,_,number in prepared if number not in known})
    if missing:fail('Unknown POs: '+', '.join(missing[:20]),409)
    backup_id=create_backup('Before bulk soft-copy attachment')
    written=[]
    try:
        with db() as c:
            for filename,content,number in prepared:
                name=stored_file(c,known[number],filename,content)
                if name:written.append(name)
    except Exception:
        for name in written:(DOCS / name).unlink(missing_ok=True)
        raise
    return {'documents_added':len(written),'duplicates_skipped':len(prepared)-len(written),'backup_id':backup_id}
