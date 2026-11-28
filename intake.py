"""Source-file intake for payments and PO documents."""
import csv
import hashlib
import io
import re
import subprocess
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import Form, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from openpyxl import load_workbook

from app import app, db


def run_text(arguments):
    try:
        result=subprocess.run(arguments,capture_output=True,timeout=18,check=True)
        return result.stdout.decode('utf-8','replace')
    except (OSError,subprocess.SubprocessError):
        raise HTTPException(422,'The file could not be read')


async def read_upload(file):
    content=await file.read(10_000_001)
    if not content or len(content)>10_000_000:
        raise HTTPException(400,'Choose a file up to 10 MB')
    return content


def extract_text(content, image_only=False):
    with tempfile.TemporaryDirectory() as temp:
        source=Path(temp)/'source'
        source.write_bytes(content)
        if content.startswith(b'%PDF-') and not image_only:
            text=run_text(['pdftotext','-layout',str(source),'-'])
            if re.search(r'\w{3}',text):
                return text
            rendered=Path(temp)/'page'
            run_text(['pdftoppm','-scale-to','2500','-f','1','-l','1','-singlefile','-png',str(source),str(rendered)])
            source=rendered.with_suffix('.png')
        elif not content.startswith((b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff')):
            raise HTTPException(400,'Choose a PDF, PNG, or JPEG')
        return run_text(['tesseract',str(source),'stdout','--psm','6'])


with db() as connection:
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS document_intake_review (
      id INTEGER PRIMARY KEY, filename TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE,
      reason TEXT NOT NULL, content BLOB NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS advice_batches (
      id INTEGER PRIMARY KEY, actor_id INTEGER, sha256 TEXT UNIQUE NOT NULL,
      report TEXT NOT NULL DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS advice_receipts (
      payment_doc TEXT NOT NULL, document_no TEXT NOT NULL, invoice_no TEXT NOT NULL,
      po_id INTEGER NOT NULL REFERENCES pos(id), cents INTEGER NOT NULL, transfer_date TEXT,
      batch_id INTEGER NOT NULL REFERENCES advice_batches(id), PRIMARY KEY(payment_doc,document_no,invoice_no)
    );
    """)


def accounting(user):
    if not user['is_admin'] and 'accounting_edit' not in user['permissions'].split(','):
        raise HTTPException(403,'Accounting permission required')


def historical_rows(content):
    try:
        with io.BytesIO(content) as buffer:
            book=load_workbook(buffer,read_only=True,data_only=True)
            try:
                values=iter(book.active.values)
                headers=[str(cell or '').strip().casefold() for cell in next(values)]
                indexes=[headers.index(key) for key in ('payment document','document no','invoice no','amount','currency','purchase doc.')]
                return [tuple(row[index] for index in indexes)+(None,) for row in values if any(cell is not None for cell in row)]
            finally:
                book.close()
    except Exception:
        raise HTTPException(422,'The workbook does not match the NUPCO payment file layout')


ROW=re.compile(r'^20\d{2}\s+(\d{10})\s+(\d{10})\s+(\d{10})\s+\d{2}[./]\d{2}[./]\d{4}\s+\d{2}[./]\d{2}[./]\d{4}\s+\S+\s+([\d,]+\.\d{2})\s+(SAR)\s+((?:300\d{7}|4[36]00\d{6}))(?:\s+\S+)?$',re.I)


def screenshot_rows(content):
    text=extract_text(content,image_only=True)
    if 'payment advice' not in text.lower():
        raise HTTPException(422,'Payment Advice heading not found')
    date=re.search(r'Transfer\s+Date\s*[:|]?\s*(\d{2}[./]\d{2}[./]\d{4})',text,re.I)
    if not date:
        raise HTTPException(422,'Transfer date could not be read')
    try:transfer=datetime.strptime(date.group(1).replace('/','.'),'%d.%m.%Y').date().isoformat()
    except ValueError:raise HTTPException(422,'Transfer date is invalid')
    results=[]
    for line in text.splitlines():
        line=line.strip()
        match=ROW.fullmatch(line)
        if match:
            results.append((*match.groups(),transfer))
        elif re.match(r'^20\d{2}\s+\d',line) or re.search(r'\bSAR\b\s+\d',line,re.I):
            results.append((None,None,None,None,None,None,transfer))
    if not results:raise HTTPException(422,'No payment rows could be read')
    return results


@app.post('/api/payment-advice')
async def payment_advice(request:Request,file:UploadFile=File(...)):
    user=request.state.user
    accounting(user)
    content=await read_upload(file)
    historical=content.startswith(b'PK')
    records=historical_rows(content) if historical else screenshot_rows(content)
    digest=hashlib.sha256(content).hexdigest()
    report=io.StringIO()
    writer=csv.writer(report)
    writer.writerow(['PO','Payment document','Document no','Invoice','Transfer date','This receipt SAR','Cumulative SAR','Result'])
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        prior=c.execute('SELECT id FROM advice_batches WHERE sha256=?',(digest,)).fetchone()
        if prior:return {'batch_id':prior['id'],'posted':0,'duplicate_upload':True}
        batch=c.execute('INSERT INTO advice_batches(actor_id,sha256) VALUES(?,?)',(user['id'],digest)).lastrowid
        posted=0
        batch_cents=0
        summaries={}
        for payment,document,invoice,amount,currency,number,transfer in records:
            payment,document,invoice,number=(str(value or '').strip() for value in (payment,document,invoice,number))
            result='Unreadable or unsupported row'
            cumulative=''
            value=''
            try:
                sar=Decimal(str(amount).replace(',',''))
                if not sar.is_finite() or sar<=0 or sar>Decimal('1000000000') or sar!=sar.quantize(Decimal('.01')):
                    raise InvalidOperation
                if str(currency).strip().upper()!='SAR' or not all(re.fullmatch(r'\d{10}',x) for x in (payment,document,invoice,number)):
                    raise InvalidOperation
                cents=int(sar*100)
                value=f'{sar:.2f}'
            except (InvalidOperation,ValueError):
                writer.writerow(['','','','',transfer or '','','',result])
                continue
            po=c.execute('SELECT id,value,paid_amount,invoice_status FROM pos WHERE number=?',(number,)).fetchone()
            previous=c.execute('SELECT po_id,cents FROM advice_receipts WHERE payment_doc=? AND document_no=? AND invoice_no=?',(payment,document,invoice)).fetchone()
            if not po:result='Unmatched PO'
            elif previous:result='Duplicate' if (previous['po_id'],previous['cents'])==(po['id'],cents) else 'Conflicting receipt'
            else:
                cancelled,ordered=c.execute('SELECT COALESCE(SUM(cancelled_qty),0),COALESCE(SUM(quantity),0) FROM po_lines WHERE po_id=?',(po['id'],)).fetchone()
                target=round(float(po['value'] or 0)*100*(max(0,ordered-cancelled)/ordered if ordered else 1))
                baseline=c.execute('SELECT COALESCE(SUM(cents),0) FROM advice_receipts WHERE po_id=?',(po['id'],)).fetchone()[0]
                if target<=0:
                    result='PO payable value unavailable'
                elif not historical and not baseline and (po['paid_amount'] or po['invoice_status'] in ('paid','partially_paid')):
                    result='Historical receipts required before posting'
                else:
                    c.execute('INSERT INTO advice_receipts VALUES(?,?,?,?,?,?,?)',(payment,document,invoice,po['id'],cents,transfer,batch))
                    total=baseline+cents
                    cumulative=f'{total/100:.2f}'
                    complete=total>=target
                    # Keep existing paid status and balances during the historical baseline import.
                    if historical:
                        c.execute("""UPDATE pos SET paid_amount=MAX(COALESCE(paid_amount,0),?),
                            invoice_status=CASE WHEN invoice_status='paid' OR ? THEN 'paid' ELSE 'partially_paid' END WHERE id=?""",(total/100,complete,po['id']))
                    else:
                        c.execute('UPDATE pos SET paid_amount=?,invoice_status=? WHERE id=?',(total/100,'paid' if complete else 'partially_paid',po['id']))
                    result='Fully paid' if complete else 'Partially paid'
                    posted+=1
                    batch_cents+=cents
                    summaries[number]=(total,result)
                    c.execute('INSERT INTO audit(user_id,action,target) VALUES(?,?,?)',(user['id'],'payment:posted',number+':'+document))
            writer.writerow([number,payment,document,invoice,transfer or '',value,cumulative,result])
        writer.writerow([])
        writer.writerow(['Batch total SAR',f'{batch_cents/100:.2f}'])
        writer.writerow(['PO','Cumulative SAR','Final status for this batch'])
        for number,(total,state) in sorted(summaries.items()):
            writer.writerow([number,f'{total/100:.2f}',state])
        c.execute('UPDATE advice_batches SET report=? WHERE id=?',(report.getvalue(),batch))
    return {'batch_id':batch,'posted':posted}


@app.get('/api/payment-advice/{batch_id}')
def batch_report(batch_id:int,request:Request):
    accounting(request.state.user)
    with db() as c:
        row=c.execute('SELECT report FROM advice_batches WHERE id=?',(batch_id,)).fetchone()
        if not row:raise HTTPException(404,'Payment batch not found')
    return Response(row['report'],media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="payment-batch-{batch_id}.csv"'})


def identify_document(text):
    lower=' '.join(text.lower().split())
    matches=[]
    for code,pattern in [('GR',r'goods receipt|(?:plant info.*supplier ref\..*storage\.loc\. info.*wms ref\.)'),
                         ('MRR',r'material receiving report|material receipt report|\bMRR\b'),
                         ('RCV',r'receiving record|\bRCV\b'),
                         ('Rejection Form',r'rejection form'),('ASN',r'advance shipping notice'),
                         ('Invoice',r'tax invoice'),('PO',r'nupco po.*catalog number')]:
        if re.search(pattern,lower,re.I):matches.append(code)
    return matches[0] if len(matches)==1 else None


def review_document(c, file, content, reason):
    filename=Path(file.filename or 'document').name[:180]
    c.execute('INSERT OR IGNORE INTO document_intake_review(filename,sha256,reason,content) VALUES(?,?,?,?)',
              (filename,hashlib.sha256(content).hexdigest(),reason,content))
    return {'status':'review','reason':reason}


@app.get('/api/document-intake')
def document_review_list():
    with db() as c:
        return [dict(row) for row in c.execute('SELECT id,filename,reason,created_at FROM document_intake_review ORDER BY id DESC LIMIT 100')]


@app.get('/api/document-intake/{file_id}')
def download_review_file(file_id:int):
    with db() as c:
        row=c.execute('SELECT content FROM document_intake_review WHERE id=?',(file_id,)).fetchone()
        if not row:raise HTTPException(404,'File not found')
        return Response(row['content'],media_type='application/octet-stream',headers={'Content-Disposition':'attachment'})


@app.post('/api/document-intake')
async def document_intake(request:Request,file:UploadFile=File(...),document_type:str=Form('')):
    from updates import DOCS
    from secrets import token_hex
    content=await read_upload(file)
    try:text=extract_text(content)
    except HTTPException as error:
        if error.status_code!=422:raise
        with db() as c:return review_document(c,file,content,'Document could not be read')
    selected=document_type if isinstance(document_type,str) else ''
    if selected and selected not in ('PO','GR','MRR','RCV','ASN','Invoice','Rejection Form','Delivery Note','Other'):
        raise HTTPException(422,'Unknown document type')
    detected=identify_document(text)
    kind=selected or detected
    numbers=set(re.findall(r'\b(?:300\d{7}|4[36]00\d{6})\b',text))
    refs=set(re.findall(r'\b(?:N\d{2,}|[MT]\d{3,})\b',text,re.I))
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if selected and detected and selected!=detected:return review_document(c,file,content,'Selected type differs from document text')
        if not kind:return review_document(c,file,content,'Document type is ambiguous')
        if len(numbers)>1:return review_document(c,file,content,'Multiple PO numbers found')
        candidates=set()
        for number in numbers:
            row=c.execute('SELECT id FROM pos WHERE number=?',(number,)).fetchone()
            if row:candidates.add(row['id'])
        ref_candidates=set()
        for ref in refs:
            for row in c.execute("""SELECT po_id FROM po_invoice_numbers WHERE number=?
                  UNION SELECT p.id FROM pos p JOIN source_status s ON s.po_number=p.number WHERE s.invoice_reference=?""",(ref.upper(),ref.upper())):
                ref_candidates.add(row[0])
        if candidates and ref_candidates and not candidates.issubset(ref_candidates):
            return review_document(c,file,content,'PO and invoice references disagree')
        if not numbers:candidates=ref_candidates
        if len(candidates)!=1:return review_document(c,file,content,'PO match is missing or ambiguous')
        po_id=candidates.pop()
        number=c.execute('SELECT number FROM pos WHERE id=?',(po_id,)).fetchone()[0]
        if kind=='MRR' and number.startswith(('4300','4600')):return review_document(c,file,content,'MRR does not apply to this PO family')
        digest=hashlib.sha256(content).hexdigest()
        if c.execute('SELECT 1 FROM po_documents WHERE po_id=? AND sha256=?',(po_id,digest)).fetchone():
            return {'status':'duplicate','po_number':number,'kind':kind}
        suffix='.pdf' if content.startswith(b'%PDF-') else '.png' if content.startswith(b'\x89PNG') else '.jpg'
        filename=kind+' '+number+suffix
        stored=token_hex(16)+suffix
        (DOCS/stored).write_bytes(content)
        try:
            c.execute('INSERT INTO po_documents(po_id,filename,stored_name,sha256,document_type) VALUES(?,?,?,?,?)',(po_id,filename,stored,digest,kind))
            c.execute('INSERT INTO audit(user_id,action,target) VALUES(?,?,?)',(request.state.user['id'],'document:auto_attach',number+':'+kind))
            c.commit()
        except Exception:
            (DOCS/stored).unlink(missing_ok=True)
            raise
    return {'status':'attached','po_number':number,'kind':kind}


@app.post('/api/due-region-archive')
async def archive_from_due_region(request:Request,file:UploadFile=File(...)):
    from updates import create_backup,decode_seed
    if not request.state.user['is_admin']:
        raise HTTPException(403,'Admin only')
    content=await read_upload(file)
    records,errors=decode_seed(content,'due_region')
    if errors or not records:
        raise HTTPException(422,'Use a valid Due and Region workbook')
    snapshot=archive_members(content,records)
    backup=create_backup('Before Due and Region archive reconciliation')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        return apply_archive(c,snapshot,request,content,backup)


def archive_members(content,records):
    workbook=load_workbook(io.BytesIO(content),read_only=True,data_only=True)
    active={row['po'] for row in records}
    mentioned=set(active)
    uploaded=set()
    waiting_sheets=[sheet for sheet in workbook if 'INVOICED UP' in sheet.title.upper()]
    if len(waiting_sheets)!=1:
        raise HTTPException(422,'The Invoiced up sheet is required')
    for sheet in workbook:
        if sheet in waiting_sheets or sheet.title.startswith('Done'):
            values=iter(sheet.values)
            for row in values:
                names=[str(value or '').strip().upper() for value in row]
                index=next((i for i,value in enumerate(names) if value in ('PO #','PO NUMBER')),None)
                if index is not None:break
            else:raise HTTPException(422,f'PO column not found in {sheet.title}')
            for row in values:
                number=str(row[index] or '').strip()
                if re.fullmatch(r'(?:300\d{7}|4[36]00\d{6})',number):
                    mentioned.add(number)
                    if sheet in waiting_sheets:uploaded.add(number)
    workbook.close()
    conflict=active & uploaded
    return active,uploaded,mentioned,conflict


def apply_archive(c,snapshot,request,content,backup):
    active,uploaded,mentioned,conflict=snapshot
    changed=0
    for order in c.execute('SELECT id,number,status,archived,invoice_status FROM pos').fetchall():
        number=order['number']
        if number in conflict and number!='4300006061':continue
        invoice_stage=order['invoice_status']
        if number in uploaded and invoice_stage not in ('paid','partially_paid'):
            invoice_stage='invoiced_up'
        if number=='4300006061' or number not in mentioned or invoice_stage in ('invoiced_up','paid','partially_paid') and number in uploaded:
            archived=1
        elif number in active:
            archived=int(invoice_stage in ('invoiced_up','paid'))
        else:
            archived=order['archived']
        if (archived,invoice_stage)!=(order['archived'],order['invoice_status']):
            c.execute('UPDATE pos SET archived=?,invoice_status=? WHERE id=?',(archived,invoice_stage,order['id']))
            changed+=1
    special=c.execute("SELECT id FROM pos WHERE number='4300006061'").fetchone()
    if special:
        c.execute("UPDATE pos SET region='North',archived=1,value=CASE WHEN COALESCE(value,0)=0 THEN 2083.87 ELSE value END WHERE id=?",(special['id'],))
        c.execute("UPDATE source_status SET invoice_reference='006' WHERE po_number='4300006061' AND invoice_reference='6'")
    c.execute('INSERT INTO audit(user_id,action,target) VALUES(?,?,?)',(request.state.user['id'],'archive:due_and_region',hashlib.sha256(content).hexdigest()))
    return {'changed':changed,'conflicts':sorted(conflict),'backup_id':backup}
