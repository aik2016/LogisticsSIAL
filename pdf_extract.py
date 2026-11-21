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
    items=[]
    for page_no,page in enumerate(text.split('\f'),1):
        if 'Catalog Number' not in page:continue
        lines=page.splitlines()
        code=near(lines,'Catalog Number',CODE)
        qty_raw=near(lines,'Required Quantity',re.compile(r'\b[\d,]+\.\d{3}\b'))
        address_line=next((x.strip() for x in lines if x.strip().startswith('Plant:')),None)
        address=address_line[6:].strip() if address_line else ''
        schedule=[]
        for q,d in SCHEDULE.findall(page):
            schedule.append({'quantity':int(float(q.replace(',',''))),'due_date':d})
        if not schedule and qty_raw:
            delivery_line=next((x for x in lines if 'Delivery Date /' in x),'')
            plain_date=re.search(r'\b\d{2}/\d{2}/\d{4}\b',delivery_line)
            if plain_date:schedule.append({'quantity':int(float(qty_raw.replace(',',''))),'due_date':plain_date.group(0)})
        if code:
            items.append({'page':page_no,'item_code':code,'quantity':int(float(qty_raw.replace(',',''))) if qty_raw else None,
                          'schedules':schedule,'address':address})
    return {'po_number':po_match.group(0) if po_match else None,'items':items,
            'warnings':(["PO number not found"] if not po_match else [])+(["No item lines found"] if not items else [])+
                       [f"Item on page {i['page']}: due date not found" for i in items if not i['schedules']]+
                       [f"Item on page {i['page']}: schedule total differs from required quantity" for i in items if i['quantity'] is not None and i['schedules'] and sum(x['quantity'] for x in i['schedules'])!=i['quantity']]}


@app.post('/api/po-pdf/preview')
async def preview_pdf(file:UploadFile=File(...)):
    if not file.filename.lower().endswith('.pdf'):raise HTTPException(400,'Upload a PDF')
    return extract(await file.read())
