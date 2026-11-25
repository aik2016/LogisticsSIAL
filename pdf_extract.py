"""Review-only extraction for bilingual Nupco PO PDFs."""
import re
import subprocess

from fastapi import File, HTTPException, UploadFile
from app import app

CODE=re.compile(r'\b(?:CTND|RIMZP|RIM|RIB)-[A-Z0-9]+\b')
PO=re.compile(r'\b(?:300\d{7}|4300\d{6}|4600\d{6})\b')
SCHEDULE=re.compile(r'Qty:\s*([\d,]+(?:\.\d+)?)\s+Date:\s*(\d{2}/\d{2}/\d{4})')


def near(lines,key,pattern,window=4):
    for n,line in enumerate(lines):
        if key in line:
            for candidate in lines[n+1:n+window]:
                m=pattern.search(candidate)
                if m:return m.group(0)
    return None


def extract(content):
    if len(content)>10_000_000:raise HTTPException(413,'PDF exceeds 10 MB')
    try:
        result=subprocess.run(['pdftotext','-layout','-','-'],input=content,capture_output=True,timeout=15)
    except (OSError,subprocess.TimeoutExpired):raise HTTPException(400,'PDF text extraction failed')
    if result.returncode or not result.stdout.strip():raise HTTPException(400,'This PDF has no readable text; scan/OCR review is needed')
    text=result.stdout.decode('utf-8','replace')
    po_match=PO.search(text)
    first_page=text.split('\f')[0]
    header=first_page.splitlines()
    created_at=''
    for n,line in enumerate(header):
        if 'Date (Gregorian)' in line:
            match=re.search(r'\b(\d{2}/\d{2}/\d{4})\b',' '.join(header[n:n+4]))
            if match:
                from datetime import datetime
                created_at=datetime.strptime(match.group(1),'%d/%m/%Y').date().isoformat()
            break
    value=None
    for n,line in enumerate(header):
        if 'PO Value' in line:
            value_match=re.search(r'([\d,]+\.\d{2})\s*(?:SAR)?\b', ' '.join(header[n:n+3]))
            if value_match:
                value=float(value_match.group(1).replace(',',''))
            break
    customer_po=''
    for n,line in enumerate(header):
        if 'Customer PO' in line or 'Cust. PO' in line:
            nearby=' '.join(header[n:n+3])
            candidates=re.findall(r'\b\d{10,22}\b',nearby)
            customer_po=next((x for x in candidates if not po_match or x!=po_match.group(0)),'')
            if customer_po:break
    customer=''
    for n,line in enumerate(header):
        if 'Customer Name' in line:
            nearby=' '.join(header[n:n+3]).split('Customer Ref',1)[0]
            customer_match=re.search(r'(MOH|MOD|MNGHA|NGH|MOE)\s*-\s*[A-Za-z][A-Za-z ]+',nearby)
            if customer_match:customer=customer_match.group(0).strip()
            break
    if not customer:
        for n,line in enumerate(header):
            if 'Warehouse' in line:
                customer=next((x.strip() for x in header[n+1:n+5] if re.search(r'\b(MOH|MOD|MNGHA|NGH|MOE)\b',x)), '')
                break
    items=[]
    for page_no,page in enumerate(text.split('\f'),1):
        if 'Catalog Number' not in page:continue
        lines=page.splitlines()
        code=near(lines,'Catalog Number',CODE)
        qty_raw=near(lines,'Required Quantity',re.compile(r'\b[\d,]+\.\d{3}\b'))
        address_line=next((x.strip() for x in lines if x.strip().startswith('Plant:')),None)
        address=address_line[6:].strip() if address_line else ''
        address=re.split(r'[\u0600-\u06ff]',address,1)[0].strip()
        if not address and customer:address=customer
        item_number=''
        for n,line in enumerate(lines):
            if 'Item Number' in line:
                number_match=re.search(r'\b(\d{1,4})\b',line.split('Item Number',1)[-1])
                if number_match:item_number=number_match.group(1).zfill(4)
                break
        schedule=[]
        for q,d in SCHEDULE.findall(page):
            schedule.append({'quantity':int(float(q.replace(',',''))),'due_date':d})
        if not schedule and qty_raw:
            delivery_line=next((x for x in lines if 'Delivery Date /' in x),'')
            plain_date=re.search(r'\b\d{2}/\d{2}/\d{4}\b',delivery_line)
            if plain_date:schedule.append({'quantity':int(float(qty_raw.replace(',',''))),'due_date':plain_date.group(0)})
        if code:
            items.append({'page':page_no,'line_no':item_number,'item_code':code,'quantity':int(float(qty_raw.replace(',',''))) if qty_raw else None,
                          'schedules':schedule,'address':address})
    arabic_address=''
    for line in text.splitlines():
        if line.strip().startswith('Plant:'):
            match=re.search(r'[\u0600-\u06ff].*',line)
            if match:arabic_address=match.group(0).strip();break
    return {'po_number':po_match.group(0) if po_match else None,'customer_po':customer_po if po_match and po_match.group(0).startswith('300') else '', 'customer':customer,'value':value,'created_at':created_at,'address_ar':arabic_address,'items':items,
            'warnings':(["PO number not found"] if not po_match else [])+(["No item lines found"] if not items else [])+
                       [f"Item on page {i['page']}: due date not found" for i in items if not i['schedules']]+
                       [f"Item on page {i['page']}: schedule total differs from required quantity" for i in items if i['quantity'] is not None and i['schedules'] and sum(x['quantity'] for x in i['schedules'])!=i['quantity']]}


@app.post('/api/po-pdf/preview')
async def preview_pdf(file:UploadFile=File(...)):
    if not file.filename.lower().endswith('.pdf'):raise HTTPException(400,'Upload a PDF')
    return extract(await file.read())
