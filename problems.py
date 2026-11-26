"""Independent PO exception records and evidence."""
import io
import json
import math

from fastapi import File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import Workbook, load_workbook
from pydantic import BaseModel, Field

from app import app, db, rows

CATEGORIES = ('delivery','payment','quantity','invoice','asn','inventory','overdue','integrity','manual')
STATUSES = ('open','investigating','awaiting_nupco','resolved')
SEVERITIES = ('low','medium','high','critical')

with db() as connection:
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS po_problems (
      id INTEGER PRIMARY KEY, po_id INTEGER NOT NULL REFERENCES pos(id),
      category TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
      item_codes TEXT NOT NULL DEFAULT '', expected TEXT NOT NULL DEFAULT '',
      actual TEXT NOT NULL DEFAULT '', impact REAL, assignee TEXT NOT NULL DEFAULT '',
      remarks TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
      detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, resolved_at TEXT,
      source_key TEXT UNIQUE
    );
    CREATE TABLE IF NOT EXISTS problem_events (
      id INTEGER PRIMARY KEY, problem_id INTEGER NOT NULL REFERENCES po_problems(id),
      actor TEXT NOT NULL, action TEXT NOT NULL, snapshot TEXT NOT NULL,
      event_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS payment_evidence (
      po_id INTEGER NOT NULL REFERENCES pos(id), reference TEXT NOT NULL, cents INTEGER NOT NULL,
      PRIMARY KEY(po_id,reference)
    );
    CREATE TABLE IF NOT EXISTS problem_files (
      id INTEGER PRIMARY KEY, problem_id INTEGER NOT NULL REFERENCES po_problems(id),
      filename TEXT NOT NULL, content BLOB NOT NULL
    );
    """)
    for number in ('4600137745','4600163415'):
        cursor=connection.execute("""INSERT OR IGNORE INTO po_problems(po_id,category,severity,remarks,source_key)
          SELECT id,'delivery','high','Dispatched but not reflected in NUPCO',? FROM pos WHERE number=?""",
          ('delivery:'+number,number))
        if cursor.rowcount:
            connection.execute("INSERT INTO problem_events(problem_id,actor,action,snapshot) VALUES(?,'System','created',?)",
                               (cursor.lastrowid,json.dumps({'po_number':number,'remarks':'Dispatched but not reflected in NUPCO'})))


def audit(connection, request, problem_id, action, snapshot):
    user=request.state.user
    connection.execute('INSERT INTO problem_events(problem_id,actor,action,snapshot) VALUES(?,?,?,?)',
                       (problem_id,user['username'],action,json.dumps(snapshot,ensure_ascii=False)))
    connection.execute('INSERT INTO audit(user_id,action,target) VALUES(?,?,?)',
                       (user['id'],'problem:'+action,str(problem_id)))


def problem_rows(connection):
    return rows(connection,'''SELECT e.*,p.number po_number FROM po_problems e
      JOIN pos p ON p.id=e.po_id ORDER BY e.id DESC''')


@app.get('/api/problems')
def list_problems():
    with db() as connection:
        return {'problems':problem_rows(connection),
                'employees':rows(connection,"SELECT username FROM users WHERE active=1 ORDER BY username")}


class ProblemInput(BaseModel):
    po_number: str
    category: str
    severity: str = 'medium'
    status: str = 'open'
    item_codes: str = ''
    expected: str = ''
    actual: str = ''
    impact: float | None = Field(default=None, allow_inf_nan=False)
    assignee: str = ''
    remarks: str = Field(min_length=1,max_length=4000)
    notes: str = Field(default='',max_length=8000)


@app.post('/api/problems')
@app.put('/api/problems/{problem_id}')
def save_problem(value: ProblemInput, request: Request, problem_id: int | None = None):
    if value.category not in CATEGORIES or value.severity not in SEVERITIES or value.status not in STATUSES:
        raise HTTPException(400,'Choose a valid category, severity and status')
    values=value.model_dump(exclude={'po_number'})
    with db() as connection:
        po=connection.execute('SELECT id FROM pos WHERE number=?',(value.po_number.strip(),)).fetchone()
        if not po:
            raise HTTPException(404,'Purchase order not found')
        previous=None
        if problem_id is not None:
            previous=connection.execute('SELECT * FROM po_problems WHERE id=?',(problem_id,)).fetchone()
            if not previous or po['id']!=previous['po_id']:
                raise HTTPException(404,'Problem not found for this PO')
        if value.assignee and (not previous or previous['assignee']!=value.assignee):
            if not connection.execute('SELECT 1 FROM users WHERE username=? AND active=1',(value.assignee,)).fetchone():
                raise HTTPException(400,'Choose an active employee')
        if problem_id is None:
            problem_id=connection.execute("""INSERT INTO po_problems
                (po_id,category,severity,status,item_codes,expected,actual,impact,assignee,remarks,notes)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(po['id'],*values.values())).lastrowid
        else:
            connection.execute("""UPDATE po_problems SET category=?,severity=?,status=?,item_codes=?,
                expected=?,actual=?,impact=?,assignee=?,remarks=?,notes=? WHERE id=?""",(*values.values(),problem_id))
        connection.execute("""UPDATE po_problems SET resolved_at=CASE WHEN status='resolved'
            THEN COALESCE(resolved_at,CURRENT_TIMESTAMP) ELSE NULL END WHERE id=?""",(problem_id,))
        audit(connection,request,problem_id,'updated' if previous else 'created',value.model_dump())
        return {'id':problem_id}


@app.get('/api/problems/{problem_id}/history')
def history(problem_id: int):
    with db() as connection:
        return {'events':rows(connection,'SELECT * FROM problem_events WHERE problem_id=? ORDER BY id DESC',(problem_id,)),
                'files':rows(connection,'SELECT id,filename FROM problem_files WHERE problem_id=? ORDER BY id DESC',(problem_id,))}


@app.post('/api/problems/{problem_id}/files')
async def attach(problem_id: int, request: Request, file: UploadFile = File(...)):
    content=await file.read(10_000_001)
    if not content or len(content)>10_000_000:
        raise HTTPException(400,'Upload a file up to 10 MB')
    filename=(file.filename or 'attachment').replace('\\','/').rsplit('/',1)[-1][:180]
    with db() as connection:
        if not connection.execute('SELECT 1 FROM po_problems WHERE id=?',(problem_id,)).fetchone():
            raise HTTPException(404,'Problem not found')
        connection.execute('INSERT INTO problem_files(problem_id,filename,content) VALUES(?,?,?)',(problem_id,filename,content))
        audit(connection,request,problem_id,'attachment',{'filename':filename})
    return {'ok':True}


@app.get('/api/problems/files/{file_id}')
def download(file_id:int):
    from urllib.parse import quote
    with db() as connection:
        file=connection.execute('SELECT filename,content FROM problem_files WHERE id=?',(file_id,)).fetchone()
        if not file:
            raise HTTPException(404,'Attachment not found')
        return StreamingResponse(iter([file['content']]),media_type='application/octet-stream',
                                 headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(file['filename'])})


@app.get('/api/problems-export')
def export_problems():
    workbook=Workbook()
    sheet=workbook.active
    sheet.title='PO Problems'
    columns=['po_number','category','severity','status','item_codes','expected','actual','impact','assignee','remarks','notes','detected_at','resolved_at']
    sheet.append(columns)
    with db() as connection:
        for problem in problem_rows(connection):
            sheet.append([problem[key] for key in columns])
            for cell in sheet[sheet.max_row]:
                if isinstance(cell.value,str):
                    cell.data_type='s'
    buffer=io.BytesIO()
    workbook.save(buffer)
    return StreamingResponse(iter([buffer.getvalue()]),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                             headers={'Content-Disposition':'attachment; filename="po-problems.xlsx"'})


@app.post('/api/problems/payment-file')
async def reconcile_payments(request: Request, file: UploadFile = File(...)):
    user=request.state.user
    if not user['is_admin'] and 'accounting_edit' not in user['permissions'].split(','):
        raise HTTPException(403,'Accounting permission required')
    from decimal import Decimal, InvalidOperation
    content=await file.read(10_000_001)
    if not content or len(content)>10_000_000:
        raise HTTPException(400,'Upload an Excel file up to 10 MB')
    try:
        workbook=load_workbook(io.BytesIO(content),read_only=True,data_only=True)
        entries=iter(workbook.active.values)
        headers=[str(cell or '').strip().casefold() for cell in next(entries)]
        indexes=[headers.index(column) for column in ('po number','payment reference','amount received (sar)')]
    except Exception:
        raise HTTPException(400,'Expected PO Number, Payment Reference and Amount Received (SAR) columns')
    with db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        totals={}
        for row_number,row in enumerate(entries,2):
            if not any(cell is not None for cell in row):
                continue
            number,reference,amount=(row[index] for index in indexes)
            number,reference=str(number or '').strip(),str(reference or '').strip()
            try:
                received=Decimal(str(amount))
                if not received.is_finite() or received<0 or received!=received.quantize(Decimal('.01')):
                    raise InvalidOperation
                cents=int(received*100)
                if cents>10**13 or not reference:
                    raise InvalidOperation
            except (InvalidOperation,ValueError,TypeError):
                raise HTTPException(400,f'Invalid payment reference or SAR amount on row {row_number}')
            po=connection.execute('SELECT id,value FROM pos WHERE number=?',(number,)).fetchone()
            if not po:
                raise HTTPException(409,f'Unknown PO on row {row_number}: {number}')
            existing=connection.execute('SELECT cents FROM payment_evidence WHERE po_id=? AND reference=?',(po['id'],reference)).fetchone()
            if existing and existing['cents']!=cents:
                raise HTTPException(409,f'Conflicting amount for PO {number}, reference {reference}')
            if not existing:
                connection.execute('INSERT INTO payment_evidence(po_id,reference,cents) VALUES(?,?,?)',(po['id'],reference,cents))
                connection.execute('INSERT INTO audit(user_id,action,target) VALUES(?,?,?)',(user['id'],'payment_evidence:add',number+':'+reference))
            totals[po['id']]=(number,po['value'])
        created=0
        for po_id,(number,value) in totals.items():
            if not value:
                continue
            paid=connection.execute('SELECT SUM(cents) FROM payment_evidence WHERE po_id=?',(po_id,)).fetchone()[0]
            expected=int(round(value*100))
            if expected==paid:
                continue
            cursor=connection.execute("""INSERT OR IGNORE INTO po_problems
                (po_id,category,severity,expected,actual,impact,remarks,source_key)
                VALUES(?,'payment','high',?,?,?,'NUPCO payment evidence differs from master PO value',?)""",
                (po_id,f'SAR {expected/100:.2f}',f'SAR {paid/100:.2f}',abs(expected-paid)/100,f'payment:{po_id}:{expected}:{paid}'))
            if cursor.rowcount:
                created+=1
                audit(connection,request,cursor.lastrowid,'reconciled',{'po':number,'master_cents':expected,'received_cents':paid})
    workbook.close()
    return {'matched_pos':len(totals),'problems_created':created}


def sync_legacy_problems(connection, issue_id=None):
    """Keep linked legacy reports visible while retaining the original issue records."""
    for issue in rows(connection,'''SELECT i.*,COALESCE(i.po_id,v.po_id) linked_po FROM issues i
        LEFT JOIN invoices v ON v.id=i.invoice_id
        WHERE COALESCE(i.po_id,v.po_id) IS NOT NULL AND (? IS NULL OR i.id=?)''',(issue_id,issue_id)):
        status='resolved' if issue['status']=='resolved' else 'open'
        cursor=connection.execute("""INSERT OR IGNORE INTO po_problems
          (po_id,category,severity,status,remarks,notes,detected_at,source_key)
          VALUES(?,'manual','medium',?,?,?,?,?)""",
          (issue['linked_po'],status,issue['description'],
           'Sada: '+issue['ticket_no'] if issue['ticket_no'] else '',issue['created_at'],f'legacy-issue:{issue["id"]}'))
        if cursor.rowcount:
            connection.execute("INSERT INTO problem_events(problem_id,actor,action,snapshot) VALUES(?,'System','linked legacy issue',?)",
                               (cursor.lastrowid,json.dumps({'original_issue_id':issue['id'],'type':issue['type'],'status':issue['status']})))


with db() as connection:
    sync_legacy_problems(connection)
