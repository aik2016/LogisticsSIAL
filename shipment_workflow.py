"""Shipment reservations and accountant-to-warehouse handoff."""
from datetime import date

from fastapi import HTTPException, Request
from pydantic import BaseModel

from app import Invoice, InvoiceLine, app, db, reserve_invoice, rows


def assignment(c, task_id, user):
    task=c.execute("""SELECT t.*,p.number,p.address,p.address_ar,p.asn_number po_asn,p.asn_date po_asn_date,p.asn_link po_asn_link
        FROM tasks t JOIN pos p ON p.id=t.po_id WHERE t.id=? AND t.task_type='asn_booking'""",(task_id,)).fetchone()
    if not task or (task['assigned_to']!=user['username'] and not user['is_admin']):
        raise HTTPException(404,'Assignment not found')
    return task


def needs_asn(task):
    address=((task['address'] or '')+' '+(task['address_ar'] or '')).lower()
    return bool(task['po_asn'] or task['asn_number'] or not task['number'].startswith('300') or 'nupco' in address or 'نوبكو' in address)


def log(c, user, po_id, action, reason=''):
    c.execute('INSERT INTO po_status_events(po_id,status,reason,user_id) VALUES(?,?,?,?)',(po_id,action,reason,user['id']))


@app.get('/api/shipment-booking/{task_id}')
def booking_details(task_id:int,request:Request):
    from app import dashboard_data
    user=request.state.user
    with db() as c:
        task=assignment(c,task_id,user)
        warehouse=rows(c,"SELECT username FROM users WHERE active=1 AND role='warehouse' ORDER BY username")
        booked=rows(c,'SELECT po_line_id,batch_id,quantity FROM invoice_lines WHERE invoice_id=?',(task['shipment_invoice_id'],))
        on_order=rows(c,"""SELECT il.po_line_id,SUM(il.quantity) quantity FROM invoice_lines il
          JOIN invoices i ON i.id=il.invoice_id WHERE i.po_id=? AND i.status IN ('booked','dispatched') GROUP BY il.po_line_id""",(task['po_id'],))
    reserved={line['po_line_id']:line['quantity'] for line in on_order}
    own={(row['po_line_id'],row['batch_id']):row['quantity'] for row in booked}
    for row in booked:reserved[row['po_line_id']]=reserved.get(row['po_line_id'],0)-row['quantity']
    data=dashboard_data()
    lines=[dict(line,available_to_book=max(0,(line['outstanding_qty'] or 0)-reserved.get(line['id'],0)))
           for line in data['lines'] if line['po_id']==task['po_id']]
    codes={line['item_code'] for line in lines}
    return {'task':dict(task),'selected':{f'{line}:{batch}':qty for (line,batch),qty in own.items()},'requires_asn':needs_asn(task),'lines':lines,
            'batches':[b for b in data['batches'] if b['item_code'] in codes],'warehouse':warehouse}


class Booking(BaseModel):
    lines:list[InvoiceLine]
    asn_number:str=''
    asn_date:date|None=None
    asn_link:str=''
    shipment_date:date|None=None
    remarks:str=''


@app.post('/api/shipment-booking/{task_id}')
def save_booking(task_id:int,value:Booking,request:Request):
    user=request.state.user
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        task=assignment(c,task_id,user)
        if task['status'] not in ('open','in_progress','returned','submitted','booked'):
            raise HTTPException(409,'This assignment cannot be booked')
        has_asn=needs_asn(task)
        if has_asn and (not value.asn_number.strip() or not value.asn_date):
            raise HTTPException(400,'Enter the ASN number and date')
        if not has_asn and not value.shipment_date:
            raise HTTPException(400,'Enter the shipment date')
        previous=task['shipment_invoice_id']
        if previous:
            changed=c.execute("UPDATE invoices SET status='cancelled' WHERE id=? AND status='booked'",(previous,)).rowcount
            if not changed:raise HTTPException(409,'The previous shipment is no longer editable')
        shipment=reserve_invoice(Invoice(number=task['invoice_number'],po_id=task['po_id'],lines=value.lines,
            asn_number=value.asn_number if has_asn else '',asn_date=value.asn_date.isoformat() if has_asn else '',
            asn_link=value.asn_link if has_asn else '',asn_remarks=value.remarks),c)
        c.execute("""UPDATE tasks SET status='booked',shipment_invoice_id=?,asn_number=?,asn_date=?,asn_link=?,
            planned_delivery_date=?,asn_remarks=? WHERE id=?""",(shipment['id'],value.asn_number if has_asn else '',
            value.asn_date.isoformat() if has_asn else '',value.asn_link if has_asn else '',
            value.shipment_date.isoformat() if not has_asn else None,value.remarks,task_id))
        c.execute("UPDATE pos SET status='booked' WHERE id=?",(task['po_id'],))
        log(c,user,task['po_id'],'booked',value.remarks)
    return shipment


class Send(BaseModel):
    warehouse_user:str


@app.post('/api/shipment-booking/{task_id}/send')
def send_booking(task_id:int,value:Send,request:Request):
    user=request.state.user
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        task=assignment(c,task_id,user)
        if task['status']!='booked':raise HTTPException(409,'Save booking first')
        if not c.execute("SELECT 1 FROM users WHERE username=? AND active=1 AND role='warehouse'",(value.warehouse_user,)).fetchone():
            raise HTTPException(400,'Choose an active warehouse user')
        if not c.execute("SELECT 1 FROM po_documents WHERE po_id=? AND document_type='PO'",(task['po_id'],)).fetchone():
            raise HTTPException(400,'Attach the PO file before sending')
        new=c.execute("""INSERT INTO tasks(title,assigned_to,po_id,status,created_by,task_type,invoice_number,
            shipment_invoice_id,asn_number,asn_date,asn_link,planned_delivery_date,details)
            VALUES('Dispatch shipment',?,?,'open',?,'warehouse_dispatch',?,?,?,?,?,?,?)""",(value.warehouse_user,task['po_id'],
            user['id'],task['invoice_number'],task['shipment_invoice_id'],task['asn_number'],task['asn_date'],
            task['asn_link'],task['planned_delivery_date'],str(task_id))).lastrowid
        c.execute("UPDATE tasks SET status='with_warehouse' WHERE id=?",(task_id,))
        c.execute("INSERT INTO audit(user_id,action,target) VALUES(?,'shipment:sent',?)",(user['id'],str(new)))
        log(c,user,task['po_id'],'with_warehouse',value.warehouse_user)
    return {'id':new}


def process_warehouse_status(c,task,value,user):
    source_id=int(task['details'].splitlines()[0])
    if value.status in ('returned','dispatch_cancelled'):
        c.execute("UPDATE tasks SET status='returned',asn_remarks=? WHERE id=?",(value.reason,source_id))
        c.execute("UPDATE tasks SET status='returned' WHERE id=?",(task['id'],))
        log(c,user,task['po_id'],'returned_to_accountant',value.reason)
        return {'status':'returned'}
    updated=c.execute("UPDATE invoices SET status='dispatched',dispatched_at=CURRENT_TIMESTAMP WHERE id=? AND status='booked'",
                      (task['shipment_invoice_id'],)).rowcount
    if not updated:raise HTTPException(409,'Reservation has changed')
    c.execute("UPDATE tasks SET status='done' WHERE id IN (?,?)",(task['id'],source_id))
    remaining=c.execute("SELECT 1 FROM invoice_lines WHERE invoice_id=?",(task['shipment_invoice_id'],)).fetchone()
    if remaining:
        from app import outstanding_by_line
        lines=rows(c,"""SELECT l.*,COALESCE((SELECT SUM(il.quantity) FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id
            WHERE il.po_line_id=l.id AND i.status IN ('delivered','gr_issued','uploaded','paid')),0) delivered_qty
            FROM po_lines l WHERE l.po_id=?""",(task['po_id'],))
        covered=c.execute("""SELECT COALESCE(SUM(il.quantity),0) FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id
            WHERE i.po_id=? AND i.status='dispatched'""",(task['po_id'],)).fetchone()[0]
        if covered>=sum(outstanding_by_line(c,lines).values()):
            c.execute("UPDATE pos SET status='dispatched' WHERE id=?",(task['po_id'],))
    log(c,user,task['po_id'],'dispatched')
    return {'status':'dispatched'}
