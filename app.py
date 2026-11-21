import csv
import io
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
DB = Path(os.getenv('LOGISTICS_DB', ROOT / 'logistics.db'))
app = FastAPI(title='Logistics Control')
SAUDI_TZ = ZoneInfo('Asia/Riyadh')


def business_today():
    return datetime.now(SAUDI_TZ).date()


@contextmanager
def db():
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS items (code TEXT PRIMARY KEY, description TEXT DEFAULT '', pcs_per_box INTEGER NOT NULL DEFAULT 1, boxes_per_pallet INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS batches (id INTEGER PRIMARY KEY, item_code TEXT NOT NULL REFERENCES items(code), batch_no TEXT NOT NULL, mfg_date TEXT, expiry_date TEXT, quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity>=0), UNIQUE(item_code,batch_no));
        CREATE TABLE IF NOT EXISTS pos (id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, customer TEXT DEFAULT '', customer_po TEXT DEFAULT '', address TEXT DEFAULT '', region TEXT DEFAULT '', value REAL DEFAULT 0, status TEXT DEFAULT 'open', overdue_reason TEXT DEFAULT '', notes TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS po_lines (id INTEGER PRIMARY KEY, po_id INTEGER NOT NULL REFERENCES pos(id), line_no TEXT NOT NULL, item_code TEXT NOT NULL REFERENCES items(code), quantity INTEGER NOT NULL CHECK(quantity>0), due_date TEXT NOT NULL, address TEXT DEFAULT '', UNIQUE(po_id,line_no));
        CREATE TABLE IF NOT EXISTS invoices (id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE, po_id INTEGER NOT NULL REFERENCES pos(id), status TEXT NOT NULL DEFAULT 'booked', asn_number TEXT DEFAULT '', asn_date TEXT DEFAULT '', asn_link TEXT DEFAULT '', asn_remarks TEXT DEFAULT '', dispatched_at TEXT, delivered_at TEXT, gr_at TEXT, uploaded_at TEXT, payment_status TEXT DEFAULT 'waiting', payment_reason TEXT DEFAULT '', value REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS invoice_lines (id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id), po_line_id INTEGER NOT NULL REFERENCES po_lines(id), batch_id INTEGER NOT NULL REFERENCES batches(id), quantity INTEGER NOT NULL CHECK(quantity>0));
        CREATE TABLE IF NOT EXISTS issues (id INTEGER PRIMARY KEY, po_id INTEGER REFERENCES pos(id), invoice_id INTEGER REFERENCES invoices(id), type TEXT NOT NULL, description TEXT NOT NULL, ticket_no TEXT DEFAULT '', status TEXT DEFAULT 'open', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, assigned_to TEXT NOT NULL, po_id INTEGER REFERENCES pos(id), due_date TEXT, status TEXT DEFAULT 'open', details TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS movements (id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id), invoice_id INTEGER REFERENCES invoices(id), quantity INTEGER NOT NULL, type TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_rows (id INTEGER PRIMARY KEY, source_name TEXT NOT NULL, sheet TEXT NOT NULL, row_number INTEGER NOT NULL, row_json TEXT NOT NULL, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(source_name,sheet,row_number));
        CREATE TABLE IF NOT EXISTS source_status (id INTEGER PRIMARY KEY, source_name TEXT NOT NULL, po_number TEXT NOT NULL, value REAL, status TEXT DEFAULT '', invoice_reference TEXT DEFAULT '', invoice_status TEXT DEFAULT '', remaining_detail TEXT DEFAULT '', remarks TEXT DEFAULT '', observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(source_name,po_number));
        ''')
        if 'address' not in [r['name'] for r in c.execute('PRAGMA table_info(po_lines)')]:
            c.execute("ALTER TABLE po_lines ADD COLUMN address TEXT DEFAULT ''")
        # Apply family defaults only to items whose pallet size is still unknown.
        c.execute("UPDATE items SET boxes_per_pallet=14 WHERE boxes_per_pallet=0 AND code LIKE 'RIMZP-%'")
        c.execute("UPDATE items SET boxes_per_pallet=9 WHERE boxes_per_pallet=0 AND (code LIKE 'RIM-%' OR code LIKE 'RIB-%')")
        c.execute("UPDATE items SET boxes_per_pallet=12 WHERE boxes_per_pallet=0 AND code LIKE 'CTND-%'")


init_db()


def rows(c, sql, args=()):
    return [dict(x) for x in c.execute(sql, args).fetchall()]


def missing(message):
    raise HTTPException(400, message)


class Item(BaseModel):
    code: str
    description: str = ''
    pcs_per_box: int = Field(gt=0)
    boxes_per_pallet: int = Field(ge=0)


def pallet_size(code: str) -> int:
    code = code.upper()
    if code.startswith('RIMZP-'): return 14
    if code.startswith(('RIM-', 'RIB-')): return 9
    if code.startswith('CTND-'): return 12
    return 0


class Batch(BaseModel):
    item_code: str
    batch_no: str
    mfg_date: str = ''
    expiry_date: str = ''
    quantity: int = Field(ge=0)


class PO(BaseModel):
    number: str
    kind: str = 'tender'
    customer: str = ''
    customer_po: str = ''
    address: str = ''
    region: str = ''
    value: float = 0
    notes: str = ''


class POLine(BaseModel):
    line_no: str
    item_code: str
    quantity: int = Field(gt=0)
    due_date: date
    address: str = ''


class InvoiceLine(BaseModel):
    po_line_id: int
    batch_id: int
    quantity: int = Field(gt=0)


class Invoice(BaseModel):
    number: str
    po_id: int
    value: float = 0
    asn_number: str = ''
    asn_date: str = ''
    asn_link: str = ''
    asn_remarks: str = ''
    lines: list[InvoiceLine]


class Status(BaseModel):
    status: str
    confirmed: bool = False
    when: str = ''


class Issue(BaseModel):
    po_id: int | None = None
    invoice_id: int | None = None
    type: str
    description: str
    ticket_no: str = ''


class Task(BaseModel):
    title: str
    assigned_to: str
    po_id: int | None = None
    due_date: str = ''
    details: str = ''


@app.get('/')
def home():
    return FileResponse(ROOT / 'static' / 'index.html')


def dashboard_data():
    today = business_today()
    with db() as c:
        pos = rows(c, '''SELECT p.*, MIN(l.due_date) due_date, SUM(l.quantity) ordered_qty,
          (SELECT ss.status FROM source_status ss WHERE ss.po_number=p.number ORDER BY ss.id DESC LIMIT 1) source_status,
          (SELECT ss.invoice_status FROM source_status ss WHERE ss.po_number=p.number ORDER BY ss.id DESC LIMIT 1) source_invoice_status,
          COALESCE((SELECT SUM(il.quantity) FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id WHERE i.po_id=p.id AND i.status IN ('delivered','gr_issued','uploaded','paid')),0) delivered_qty
          FROM pos p LEFT JOIN po_lines l ON l.po_id=p.id GROUP BY p.id ORDER BY due_date''')
        for p in pos:
            due = date.fromisoformat(p['due_date']) if p['due_date'] else None
            p['days_to_due'] = (due-today).days if due else None
            source_done = (p['source_status'] or '').lower().startswith('delivered')
            outstanding = not source_done and ((p['delivered_qty'] or 0) < (p['ordered_qty'] or 0))
            p['urgent'] = bool(due and due < today and outstanding)
            p['reminder'] = p['days_to_due'] in (10, 5, 2, 0) and outstanding
        stock = rows(c, '''SELECT i.code,i.description,i.pcs_per_box,i.boxes_per_pallet,COALESCE(SUM(b.quantity),0) physical,
          COALESCE((SELECT SUM(il.quantity) FROM invoice_lines il JOIN invoices v ON v.id=il.invoice_id JOIN batches bx ON bx.id=il.batch_id WHERE bx.item_code=i.code AND v.status IN ('booked','dispatched')),0) booked
          FROM items i LEFT JOIN batches b ON b.item_code=i.code GROUP BY i.code ORDER BY i.code''')
        for s in stock: s['free'] = s['physical'] - s['booked']
        invoices = rows(c, 'SELECT i.*,p.number po_number FROM invoices i JOIN pos p ON p.id=i.po_id ORDER BY i.id DESC')
        due_lines = rows(c, '''SELECT p.id po_id,p.number,p.customer,p.customer_po,p.region,p.value,p.status,p.overdue_reason,
           l.line_no,l.item_code,l.quantity ordered_qty,l.due_date,
           COALESCE((SELECT SUM(il.quantity) FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id WHERE il.po_line_id=l.id AND i.status IN ('delivered','gr_issued','uploaded','paid')),0) delivered_qty
           FROM po_lines l JOIN pos p ON p.id=l.po_id''')
        status_by_id = {p['id']:p for p in pos}
        reminders=[];urgent=[]
        for line in due_lines:
            parent=status_by_id[line['po_id']]
            if (parent['source_status'] or '').lower().startswith('delivered'): continue
            if line['delivered_qty'] >= line['ordered_qty']: continue
            line['days_to_due']=(date.fromisoformat(line['due_date'])-today).days
            line['source_status']=parent['source_status']
            line['urgent']=line['days_to_due']<0
            if line['urgent']:urgent.append(line)
            if line['days_to_due'] in (10,5,2,0):reminders.append(line)
        return {'pos':pos,'stock':stock,'batches':rows(c,'SELECT * FROM batches ORDER BY id DESC'), 'invoices':invoices,
                'issues':rows(c,'SELECT * FROM issues ORDER BY id DESC'), 'tasks':rows(c,'SELECT * FROM tasks ORDER BY id DESC'),
                'lines':rows(c,'SELECT l.*,p.number po_number,i.pcs_per_box,i.boxes_per_pallet FROM po_lines l JOIN pos p ON p.id=l.po_id JOIN items i ON i.code=l.item_code ORDER BY l.due_date'),
                'reminders':reminders, 'urgent':urgent}


@app.get('/api/dashboard')
def dashboard(request:Request):
    data=dashboard_data()
    user=request.state.user
    if user['is_admin'] or 'reports_view' in user['permissions'].split(','): return data
    perms=set(user['permissions'].split(','))
    if not perms & {'pos_edit','invoice_edit','issue_edit','task_edit'}:
        data['pos']=data['lines']=data['reminders']=data['urgent']=[]
    else:
        for p in data['pos']+data['lines']+data['reminders']+data['urgent']:p['value']=None
    if not perms & {'inventory_edit','invoice_edit'}:data['stock']=data['batches']=[]
    if 'invoice_edit' not in perms:data['invoices']=[]
    if 'issue_edit' not in perms:data['issues']=[]
    if 'task_edit' not in perms:data['tasks']=[]
    return data


@app.post('/api/items')
def add_item(v: Item):
    with db() as c:
        try: c.execute('INSERT INTO items(code,description,pcs_per_box,boxes_per_pallet) VALUES(?,?,?,?)',(v.code.upper(),v.description,v.pcs_per_box,v.boxes_per_pallet or pallet_size(v.code)))
        except sqlite3.IntegrityError as e: missing(str(e))
    return {'ok':True}


@app.post('/api/batches')
def add_batch(v: Batch):
    with db() as c:
        try:
            existing = c.execute('SELECT id FROM batches WHERE item_code=? AND batch_no=?',(v.item_code,v.batch_no)).fetchone()
            if existing:
                c.execute('UPDATE batches SET quantity=quantity+? WHERE id=?',(v.quantity,existing['id']))
                bid=existing['id']
            else:
                bid=c.execute('INSERT INTO batches(item_code,batch_no,mfg_date,expiry_date,quantity) VALUES(?,?,?,?,?)',tuple(v.model_dump().values())).lastrowid
            c.execute("INSERT INTO movements(batch_id,quantity,type) VALUES(?,?,'receipt')",(bid,v.quantity))
        except sqlite3.IntegrityError as e: missing(str(e))
    return {'id':bid}


@app.post('/api/pos')
def add_po(v: PO):
    if not (v.number.startswith('300') or v.number.startswith('4600') or v.number.startswith('4300')): missing('PO number must start with 300, 4300 or 4600')
    with db() as c:
        try: pid=c.execute('INSERT INTO pos(number,kind,customer,customer_po,address,region,value,notes) VALUES(?,?,?,?,?,?,?,?)',tuple(v.model_dump().values())).lastrowid
        except sqlite3.IntegrityError as e: missing(str(e))
    return {'id':pid}


@app.post('/api/pos/{po_id}/lines')
def add_line(po_id:int,v:POLine):
    with db() as c:
        try: lid=c.execute('INSERT INTO po_lines(po_id,line_no,item_code,quantity,due_date,address) VALUES(?,?,?,?,?,?)',(po_id,v.line_no,v.item_code,v.quantity,v.due_date.isoformat(),v.address)).lastrowid
        except sqlite3.IntegrityError as e: missing(str(e))
    return {'id':lid}


@app.patch('/api/pos/{po_id}')
async def update_po(po_id:int,request:Request):
    v=await request.json()
    allowed={'customer','customer_po','address','region','value','status','overdue_reason','notes'}
    updates={k:v[k] for k in v if k in allowed}
    if not updates: missing('No valid fields')
    with db() as c:
        c.execute('UPDATE pos SET '+','.join(f'{k}=?' for k in updates)+' WHERE id=?',(*updates.values(),po_id))
    return {'ok':True}


@app.post('/api/invoices')
def add_invoice(v:Invoice):
    if not v.lines: missing('Invoice needs at least one line')
    with db() as c:
        po=c.execute('SELECT number FROM pos WHERE id=?',(v.po_id,)).fetchone()
        if not po: missing('PO not found')
        if not v.number.strip(): missing('Invoice reference required')
        seen_lines = {}
        seen_batches = {}
        for line in v.lines:
            row=c.execute('''SELECT l.quantity,l.item_code,b.item_code batch_item,b.quantity stock,
              COALESCE((SELECT SUM(il.quantity) FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id WHERE il.po_line_id=l.id AND i.status!='cancelled'),0) allocated,
              COALESCE((SELECT SUM(il.quantity) FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id WHERE il.batch_id=b.id AND i.status IN ('booked','dispatched')),0) reserved
              FROM po_lines l JOIN batches b ON b.id=? WHERE l.id=? AND l.po_id=?''',(line.batch_id,line.po_line_id,v.po_id)).fetchone()
            if not row or row['item_code']!=row['batch_item']: missing('Line and batch must belong to the same item and PO')
            seen_lines[line.po_line_id] = seen_lines.get(line.po_line_id, 0) + line.quantity
            seen_batches[line.batch_id] = seen_batches.get(line.batch_id, 0) + line.quantity
            if row['allocated']+seen_lines[line.po_line_id]>row['quantity']: missing('Invoice quantity exceeds PO line quantity')
            if row['reserved']+seen_batches[line.batch_id]>row['stock']: missing('Insufficient free batch stock')
        try:
            iid=c.execute('INSERT INTO invoices(number,po_id,value,asn_number,asn_date,asn_link,asn_remarks) VALUES(?,?,?,?,?,?,?)',(v.number,v.po_id,v.value,v.asn_number,v.asn_date,v.asn_link,v.asn_remarks)).lastrowid
            c.executemany('INSERT INTO invoice_lines(invoice_id,po_line_id,batch_id,quantity) VALUES(?,?,?,?)',[(iid,l.po_line_id,l.batch_id,l.quantity) for l in v.lines])
        except sqlite3.IntegrityError as e: missing(str(e))
    return {'id':iid,'status':'booked'}


@app.patch('/api/invoices/{invoice_id}/status')
def invoice_status(invoice_id:int,v:Status):
    valid={'booked','dispatched','delivered','gr_issued','uploaded','paid','cancelled'}
    if v.status not in valid: missing('Unknown status')
    with db() as c:
        inv=c.execute('SELECT * FROM invoices WHERE id=?',(invoice_id,)).fetchone()
        if not inv: raise HTTPException(404,'Invoice not found')
        old=inv['status']
        allowed={'booked':{'dispatched','delivered','cancelled'},'dispatched':{'delivered','cancelled'},'delivered':{'gr_issued'},'gr_issued':{'uploaded'},'uploaded':{'paid'},'paid':set(),'cancelled':set()}
        if v.status not in allowed[old]: missing(f'Cannot move from {old} to {v.status}')
        if not v.confirmed: missing('Confirmation required for inventory or financial status changes')
        if v.status=='delivered':
            for line in rows(c,'SELECT batch_id,quantity FROM invoice_lines WHERE invoice_id=?',(invoice_id,)):
                stock=c.execute('SELECT quantity FROM batches WHERE id=?',(line['batch_id'],)).fetchone()['quantity']
                if stock<line['quantity']: missing('Insufficient batch stock to deliver')
                c.execute('UPDATE batches SET quantity=quantity-? WHERE id=?',(line['quantity'],line['batch_id']))
                c.execute("INSERT INTO movements(batch_id,invoice_id,quantity,type) VALUES(?,?,?,'delivery')",(line['batch_id'],invoice_id,-line['quantity']))
        column={'dispatched':'dispatched_at','delivered':'delivered_at','gr_issued':'gr_at','uploaded':'uploaded_at'}.get(v.status)
        if column: c.execute(f'UPDATE invoices SET status=?,{column}=? WHERE id=?',(v.status,v.when or business_today().isoformat(),invoice_id))
        else: c.execute('UPDATE invoices SET status=? WHERE id=?',(v.status,invoice_id))
    return {'ok':True}


@app.post('/api/invoices/{invoice_id}/returns')
async def return_stock(invoice_id:int,request:Request):
    v=await request.json(); batch_id=int(v.get('batch_id',0)); quantity=int(v.get('quantity',0))
    if not v.get('confirmed') or quantity<=0: missing('Positive quantity and confirmation required')
    with db() as c:
        delivered=c.execute("SELECT COALESCE(SUM(il.quantity),0) n FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id WHERE i.id=? AND il.batch_id=? AND i.status IN ('delivered','gr_issued','uploaded','paid')",(invoice_id,batch_id)).fetchone()['n']
        returned=c.execute("SELECT COALESCE(SUM(quantity),0) n FROM movements WHERE invoice_id=? AND batch_id=? AND type='return'",(invoice_id,batch_id)).fetchone()['n']
        if quantity>delivered-returned: missing('Return exceeds delivered quantity')
        c.execute('UPDATE batches SET quantity=quantity+? WHERE id=?',(quantity,batch_id))
        c.execute("INSERT INTO movements(batch_id,invoice_id,quantity,type) VALUES(?,?,?,'return')",(batch_id,invoice_id,quantity))
    return {'ok':True}


@app.post('/api/issues')
def add_issue(v:Issue):
    with db() as c: iid=c.execute('INSERT INTO issues(po_id,invoice_id,type,description,ticket_no) VALUES(?,?,?,?,?)',tuple(v.model_dump().values())).lastrowid
    return {'id':iid}


@app.post('/api/tasks')
def add_task(v:Task):
    with db() as c: tid=c.execute('INSERT INTO tasks(title,assigned_to,po_id,due_date,details) VALUES(?,?,?,?,?)',tuple(v.model_dump().values())).lastrowid
    return {'id':tid}


@app.patch('/api/tasks/{task_id}')
async def task_status(task_id:int,request:Request):
    v=await request.json()
    if v.get('status') not in ('open','in_progress','done'): missing('Invalid status')
    with db() as c: c.execute('UPDATE tasks SET status=? WHERE id=?',(v['status'],task_id))
    return {'ok':True}


@app.get('/api/reports')
def reports():
    with db() as c:
        return {'by_region':rows(c,'SELECT region,COUNT(*) po_count,SUM(value) value FROM pos GROUP BY region'),
                'by_item':rows(c,'SELECT item_code,SUM(quantity) quantity FROM po_lines GROUP BY item_code'),
                'invoice_status':rows(c,'SELECT status,COUNT(*) count,SUM(value) value FROM invoices GROUP BY status'),
                'tasks':rows(c,'SELECT assigned_to,status,COUNT(*) count FROM tasks GROUP BY assigned_to,status')}


@app.get('/api/export/pos.csv')
def export_pos():
    data=dashboard_data()['pos']; output=io.StringIO()
    if data:
        writer=csv.DictWriter(output,data[0].keys()); writer.writeheader(); writer.writerows(data)
    return StreamingResponse(iter([output.getvalue()]),media_type='text/csv',headers={'Content-Disposition':'attachment; filename=pos.csv'})

import imports  # register workbook import routes

import auth  # register account routes and access control

import pdf_extract  # register review-only PDF extraction
