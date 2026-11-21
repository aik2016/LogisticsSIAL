"""Conservative, repeatable importers for the supplied Sial workbook layouts.

Uploads are data, not instructions. Only known sheets and columns are read.
"""
import hashlib
import io
import json
import re
from collections import Counter
from datetime import datetime, date
from decimal import Decimal, InvalidOperation

from fastapi import File, HTTPException, UploadFile
from openpyxl import load_workbook
from app import app, db, rows, pallet_size


def money(v):
    if v is None or str(v).strip() == '': return 0.0
    s = str(v).strip().replace(' ', '')
    if ',' in s and '.' in s: s = s.replace(',', '')
    elif ',' in s: s = s.replace(',', '.') if len(s.rsplit(',',1)[-1]) <= 2 else s.replace(',', '')
    try: return float(Decimal(s))
    except InvalidOperation: raise ValueError(f'Invalid value: {v}')


def qty(v):
    if v is None: raise ValueError('Missing quantity')
    n = money(v)
    if n <= 0 or int(n) != n: raise ValueError(f'Invalid piece quantity: {v}')
    return int(n)


def due(v):
    if isinstance(v, datetime): return v.date().isoformat()
    if isinstance(v, date): return v.isoformat()
    s = str(v or '').strip()
    for fmt in ('%d.%m.%Y','%d/%m/%Y','%Y-%m-%d'):
        try: return datetime.strptime(s,fmt).date().isoformat()
        except ValueError: pass
    raise ValueError(f'Invalid due date: {v}')


def decode(content, kind):
    if len(content) > 12_000_000: raise HTTPException(413, 'Workbook exceeds 12 MB')
    try: book = load_workbook(io.BytesIO(content),read_only=True,data_only=True)
    except Exception: raise HTTPException(400,'Could not read XLSX workbook')
    if kind == 'masterlist':
        if not {'Tender','Marketplace'}.issubset(set(book.sheetnames)): raise HTTPException(400,'Expected Tender and Marketplace sheets')
        result=[]; errors=[]
        for sheet in ('Tender','Marketplace'):
            prior=None
            for n,r in enumerate(book[sheet].iter_rows(min_row=2,values_only=True),2):
                if not any(x is not None for x in r): continue
                raw=str(r[0]).strip() if r[0] is not None else ''
                if raw: prior=raw
                po=prior
                try:
                    if not po or not re.fullmatch(r'\d{10}',po): raise ValueError('Missing or invalid PO number')
                    if sheet=='Marketplace' and not po.startswith('300'): raise ValueError('Marketplace PO must start with 300')
                    if sheet=='Tender' and not po.startswith(('4300','4600')): raise ValueError('Tender PO must start with 4300 or 4600')
                    code=str(r[1]).strip().upper()
                    if not re.fullmatch(r'[A-Z0-9]+(?:-[A-Z0-9]+)+',code): raise ValueError('Invalid item code')
                    result.append(dict(sheet=sheet,row=n,po=po,item=code,quantity=qty(r[2]),due_date=due(r[3]),address=str(r[4] or '').strip(),value=money(r[5]),continued=not bool(raw)))
                except (ValueError,IndexError) as e: errors.append(dict(sheet=sheet,row=n,error=str(e)))
        return result,errors
    if kind == 'sial':
        if not {'Tender','Marketplace','Outstanding'}.issubset(set(book.sheetnames)): raise HTTPException(400,'Expected Tender, Marketplace and Outstanding sheets')
        result=[];errors=[]
        for sheet in ('Tender','Marketplace','Outstanding'):
            for n,r in enumerate(book[sheet].iter_rows(min_row=9,values_only=True),9):
                r=tuple(r)+(None,)*(11-len(r))
                po=str(r[0] or '').strip()
                if not re.fullmatch(r'\d{10}',po): continue
                result.append(dict(sheet=sheet,row=n,po=po,value=money(r[1]),status=str(r[6] or '').strip(),invoice_reference=str(r[7] or '').strip(),invoice_status=str(r[8] or '').strip(),remaining_detail=str(r[9] or '').strip(),remarks=str(r[10] or '').strip(),address=str(r[5] or '').strip()))
        return result,errors
    raise HTTPException(400,'Supported kinds: masterlist, sial')


async def get_file(upload:UploadFile):
    if not upload.filename.lower().endswith('.xlsx'): raise HTTPException(400,'Upload an XLSX file')
    return await upload.read()


@app.post('/api/import/{kind}/preview')
async def preview(kind:str,file:UploadFile=File(...)):
    content=await get_file(file); records,errors=decode(content,kind)
    return {'kind':kind,'filename':file.filename,'fingerprint':hashlib.sha256(content).hexdigest()[:16],
            'rows':len(records),'distinct_pos':len({x['po'] for x in records}),
            'by_sheet':dict(Counter(x['sheet'] for x in records)), 'errors':errors[:30], 'error_count':len(errors),
            'sample':records[:5], 'ready':bool(records) and not errors}


@app.post('/api/import/{kind}/apply')
async def apply(kind:str,file:UploadFile=File(...)):
    content=await get_file(file); records,errors=decode(content,kind)
    if errors: raise HTTPException(400,f'{len(errors)} rows need correction; preview errors first')
    if not records: raise HTTPException(400,'No PO rows found')
    fingerprint=hashlib.sha256(content).hexdigest()[:16]
    source=f'{kind}:{fingerprint}'
    with db() as c:
        already=c.execute('SELECT 1 FROM source_rows WHERE source_name=? LIMIT 1',(source,)).fetchone()
        if already: return {'source':source,'already_imported':True,'imported_rows':0}
        if kind=='masterlist':
            grouped={}
            for r in records: grouped.setdefault(r['po'],[]).append(r)
            for po,group in grouped.items():
                existing=c.execute('SELECT id FROM pos WHERE number=?',(po,)).fetchone()
                if existing:
                    existing_lines=c.execute('SELECT COUNT(*) n FROM po_lines WHERE po_id=?',(existing['id'],)).fetchone()['n']
                    if existing_lines: raise HTTPException(409,f'PO {po} already has lines; import to an empty database or reconcile manually')
            for po,group in grouped.items():
                kind_value='marketplace' if po.startswith('300') else 'tender'
                c.execute('INSERT INTO pos(number,kind,address,value) VALUES(?,?,?,NULL) ON CONFLICT(number) DO NOTHING',(po,kind_value,group[0]['address']))
                pid=c.execute('SELECT id FROM pos WHERE number=?',(po,)).fetchone()['id']
                for n,r in enumerate(group,1):
                    code=r['item']; pack=250 if code=='CTND-200' else 100 if code.startswith('RIMZP') else 200 if code.startswith(('RIM','RIB')) else 1
                    pallet=pallet_size(code)
                    c.execute('INSERT INTO items(code,pcs_per_box,boxes_per_pallet) VALUES(?,?,?) ON CONFLICT(code) DO NOTHING',(code,pack,pallet))
                    c.execute('INSERT INTO po_lines(po_id,line_no,item_code,quantity,due_date,address) VALUES(?,?,?,?,?,?)',(pid,f'{n*10:04d}',code,r['quantity'],r['due_date'],r['address']))
        else:
            for r in records:
                c.execute('INSERT INTO source_status(source_name,po_number,value,status,invoice_reference,invoice_status,remaining_detail,remarks) VALUES(?,?,?,?,?,?,?,?)',(source,r['po'],r['value'],r['status'],r['invoice_reference'],r['invoice_status'],r['remaining_detail'],r['remarks']))
                c.execute('UPDATE pos SET value=? WHERE number=? AND value IS NULL',(r['value'],r['po']))
        for r in records:
            c.execute('INSERT INTO source_rows(source_name,sheet,row_number,row_json) VALUES(?,?,?,?)',(source,r['sheet'],r['row'],json.dumps(r,ensure_ascii=False)))
    return {'source':source,'already_imported':False,'imported_rows':len(records),'distinct_pos':len({x['po'] for x in records})}


@app.get('/api/import/snapshots')
def snapshots():
    with db() as c:
        return {'imports':rows(c,'SELECT source_name,COUNT(*) row_count,MIN(imported_at) imported_at FROM source_rows GROUP BY source_name ORDER BY imported_at DESC'),
                'statuses':rows(c,'SELECT * FROM source_status ORDER BY id DESC LIMIT 200')}

# Nupco extracts have different grains; their records are kept as evidence first.
NUPCO_LAYOUTS = {
    'sap': ('Purchasing_Document','Po_Item','Ordered_Quantity','Gr_Qty','Remaining_Qty'),
    'gr': ('Gr_number','Po_number','Po_line_item','Quantity','Pstng_date'),
    'invoicing': ('Number','SAP Number','Invoice Status','PO Number','Combined IR number'),
    'invoices': ('Invoice_number','Status','Supplier_invoice_number','Invoice_Amount'),
    'statement': ('Document_number','Document_type','Clearing_status','Amount_in_local_currency'),
}

with db() as c:
    if 'value' not in [r['name'] for r in c.execute('PRAGMA table_info(source_status)')]:
        c.execute('ALTER TABLE source_status ADD COLUMN value REAL')
    c.execute('''CREATE TABLE IF NOT EXISTS nupco_records (id INTEGER PRIMARY KEY, source_name TEXT NOT NULL,
      kind TEXT NOT NULL, source_row INTEGER NOT NULL, po_number TEXT DEFAULT '', po_line TEXT DEFAULT '',
      document_number TEXT DEFAULT '', supplier_reference TEXT DEFAULT '', status TEXT DEFAULT '',
      quantity REAL, gr_quantity REAL, remaining_quantity REAL, amount REAL, raw_json TEXT NOT NULL,
      UNIQUE(source_name,source_row))''')


def decode_nupco(content,kind):
    if len(content)>12_000_000:raise HTTPException(413,'Workbook exceeds 12 MB')
    try:book=load_workbook(io.BytesIO(content),read_only=True,data_only=True)
    except Exception:raise HTTPException(400,'Could not read XLSX workbook')
    sheet=book.active
    iterator=sheet.iter_rows(values_only=True)
    headers=[str(x or '').strip() for x in next(iterator)]
    expected=NUPCO_LAYOUTS[kind]
    if not all(x in headers for x in expected):raise HTTPException(400,f'Expected columns: {", ".join(expected)}')
    output=[];errors=[]
    for rowno,values in enumerate(iterator,2):
        if not any(x is not None for x in values):continue
        if str(values[0] or '').strip().startswith('©'):continue
        r={h:values[n] for n,h in enumerate(headers) if h}
        try:
            text=lambda key:str(r.get(key) or '').strip()
            numeric=lambda key:money(r.get(key)) if r.get(key) not in (None,'') else None
            record=dict(row=rowno,po='',line='',document='',supplier_ref='',status='',quantity=None,gr_quantity=None,remaining_quantity=None,amount=None,raw={k:str(v) if v is not None else '' for k,v in r.items()})
            if kind=='sap':record.update(po=text('Purchasing_Document'),line=text('Po_Item'),status=text('Procstate_name'),quantity=numeric('Ordered_Quantity'),gr_quantity=numeric('Gr_Qty'),remaining_quantity=numeric('Remaining_Qty'))
            elif kind=='gr':record.update(po=text('Po_number'),line=text('Po_line_item'),document=text('Gr_number'),quantity=numeric('Quantity'),amount=numeric('Lc_amount'))
            elif kind=='invoicing':record.update(po=text('PO Number'),document=text('Number'),supplier_ref=text('Combined IR number'),status=text('Invoice Status'),amount=numeric('Total Amount'))
            elif kind=='invoices':record.update(document=text('Invoice_number'),supplier_ref=text('Supplier_invoice_number'),status=text('Status'),amount=numeric('Invoice_Amount'))
            elif kind=='statement':record.update(document=text('Document_number'),supplier_ref=text('Reference'),status=text('Clearing_status'),amount=numeric('Amount_in_local_currency'))
            if kind in ('sap','gr','invoicing') and not re.fullmatch(r'\d{10}',record['po']):raise ValueError('Invalid PO number')
            output.append(record)
        except (ValueError,IndexError) as e:errors.append(dict(row=rowno,error=str(e)))
    return output,errors


@app.post('/api/nupco/{kind}/preview')
async def preview_nupco(kind:str,file:UploadFile=File(...)):
    if kind not in NUPCO_LAYOUTS:raise HTTPException(400,'Unknown Nupco report type')
    content=await get_file(file);records,errors=decode_nupco(content,kind)
    with db() as c:
        known={r['number'] for r in rows(c,'SELECT number FROM pos')}
    pos={r['po'] for r in records if r['po']}
    return {'kind':kind,'rows':len(records),'distinct_pos':len(pos),'matched_pos':len(pos&known),
            'unmatched_pos':sorted(pos-known)[:30],'error_count':len(errors),'errors':errors[:30],
            'sample':[{k:v for k,v in r.items() if k!='raw'} for r in records[:5]],'ready':bool(records) and not errors,
            'fingerprint':hashlib.sha256(content).hexdigest()[:16]}


@app.post('/api/nupco/{kind}/apply')
async def apply_nupco(kind:str,file:UploadFile=File(...)):
    if kind not in NUPCO_LAYOUTS:raise HTTPException(400,'Unknown Nupco report type')
    content=await get_file(file);records,errors=decode_nupco(content,kind)
    if errors:raise HTTPException(400,f'{len(errors)} rows need correction; preview first')
    if not records:raise HTTPException(400,'No rows found')
    source=f'{kind}:{hashlib.sha256(content).hexdigest()[:16]}'
    with db() as c:
        if c.execute('SELECT 1 FROM nupco_records WHERE source_name=? LIMIT 1',(source,)).fetchone():return {'source':source,'already_imported':True,'imported_rows':0}
        for r in records:
            c.execute('''INSERT INTO nupco_records(source_name,kind,source_row,po_number,po_line,document_number,supplier_reference,status,quantity,gr_quantity,remaining_quantity,amount,raw_json)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(source,kind,r['row'],r['po'],r['line'],r['document'],r['supplier_ref'],r['status'],r['quantity'],r['gr_quantity'],r['remaining_quantity'],r['amount'],json.dumps(r['raw'],ensure_ascii=False)))
    return {'source':source,'already_imported':False,'imported_rows':len(records)}


@app.get('/api/nupco/reconcile')
def reconcile_nupco():
    with db() as c:
        latest=c.execute("SELECT source_name FROM nupco_records WHERE kind='sap' ORDER BY id DESC LIMIT 1").fetchone()
        if not latest:return {'sap_source':None,'matched_pos':0,'differences':[]}
        source=latest['source_name']
        internal={r['number']:r['quantity'] for r in rows(c,'SELECT p.number,SUM(l.quantity) quantity FROM pos p JOIN po_lines l ON l.po_id=p.id GROUP BY p.id')}
        sap=rows(c,'SELECT po_number,SUM(quantity) quantity FROM nupco_records WHERE source_name=? GROUP BY po_number',(source,))
        differences=[]
        for r in sap:
            po=r['po_number'];external=r['quantity'] or 0;local=internal.get(po)
            if local is None or abs(external-local)>0.001:differences.append({'po':po,'local_quantity':local,'sap_quantity':external,'difference':None if local is None else round(external-local,3)})
        return {'sap_source':source,'matched_pos':sum(r['po_number'] in internal for r in sap),'differences':differences[:500]}
