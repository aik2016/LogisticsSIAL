import io
import os
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook
from fastapi.testclient import TestClient

os.environ['LOGISTICS_DB'] = tempfile.mktemp(suffix='.db')
from app import app, db
from auth import hash_password


def workbook(sheets):
    w=Workbook();w.remove(w.active)
    for name,lines in sheets.items():
        s=w.create_sheet(name)
        for line in lines:s.append(line)
    out=io.BytesIO();w.save(out);return out.getvalue()


class WorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client=TestClient(app)
        with db() as c:c.execute('INSERT INTO users(username,password_hash,is_admin) VALUES(?,?,1)',('admin',hash_password('secure password 123')))
        assert cls.client.post('/api/auth/login',json={'username':'admin','password':'secure password 123'}).status_code==200
        cls.headers={'X-Logistics-Request':'1'}

    def call(self,path,body):
        r=self.client.post('/api/'+path,json=body,headers=self.headers)
        self.assertEqual(r.status_code,200,r.text)
        return r.json()

    def test_auth_and_inventory_transition(self):
        self.assertEqual(TestClient(app).get('/api/dashboard').status_code,401)
        self.call('items',{'code':'CTND-200','pcs_per_box':250,'boxes_per_pallet':12})
        b=self.call('batches',{'item_code':'CTND-200','batch_no':'B1','quantity':1000})['id']
        p=self.call('pos',{'number':'4300007810','kind':'tender'})['id']
        line=self.call(f'pos/{p}/lines',{'line_no':'0010','item_code':'CTND-200','quantity':800,'due_date':'2027-01-01'})['id']
        inv=self.call('invoices',{'number':'N001','po_id':p,'lines':[{'po_line_id':line,'batch_id':b,'quantity':500}]})['id']
        stock=self.client.get('/api/dashboard').json()['stock'][0]
        self.assertEqual((stock['physical'],stock['booked'],stock['free']),(1000,500,500))
        for status in ('dispatched','delivered','gr_issued'):
            r=self.client.patch(f'/api/invoices/{inv}/status',json={'status':status,'confirmed':True},headers=self.headers)
            self.assertEqual(r.status_code,200,r.text)
        stock=self.client.get('/api/dashboard').json()['stock'][0]
        self.assertEqual((stock['physical'],stock['booked'],stock['free']),(500,0,500))
        self.call(f'invoices/{inv}/returns',{'batch_id':b,'quantity':200,'confirmed':True})
        self.assertEqual(self.client.get('/api/dashboard').json()['stock'][0]['physical'],700)

    def test_masterlist_continuation_and_sial_value(self):
        master=workbook({'Tender':[['PO Number','Item Code','Qty','Due Date','Delivery Address','Total Value'],['4600999999','RIM-70',200,'01.12.2026','Riyadh',100]],'Marketplace':[['PO Number','Item Code','Qty','Due Date','Delivery Address','Total Value'],['3001999999','RIMZP-45',100,'01.12.2026','Dammam',300],[None,'RIMZP-50',100,'15.12.2026','Dammam',300]]})
        r=self.client.post('/api/import/masterlist/preview',files={'file':('master.xlsx',master)},headers=self.headers)
        self.assertEqual((r.json()['rows'],r.json()['distinct_pos'],r.json()['error_count']),(3,2,0))
        r=self.client.post('/api/import/masterlist/apply',files={'file':('master.xlsx',master)},headers=self.headers)
        self.assertEqual(r.status_code,200,r.text)
        r=self.client.post('/api/import/masterlist/apply',files={'file':('master.xlsx',master)},headers=self.headers)
        self.assertTrue(r.json()['already_imported'])
        sial=workbook({'Tender':[['heading']]*8+[['4600999999',120,'','','','','Outstanding','','','']], 'Marketplace':[['heading']]*8+[['3001999999',400,'','','','','Outstanding','','','']], 'Outstanding':[['heading']]*8})
        r=self.client.post('/api/import/sial/apply',files={'file':('sial.xlsx',sial)},headers=self.headers)
        self.assertEqual(r.status_code,200,r.text)
        with db() as c:
            po=c.execute("SELECT value FROM pos WHERE number='3001999999'").fetchone()
            lines=c.execute("SELECT COUNT(*) n FROM po_lines WHERE po_id=(SELECT id FROM pos WHERE number='3001999999')").fetchone()
            self.assertEqual(po['value'],400)
            self.assertEqual(lines['n'],2)
            packs={r['code']:r['boxes_per_pallet'] for r in c.execute("SELECT code,boxes_per_pallet FROM items WHERE code IN ('RIM-70','RIMZP-45','RIMZP-50')")}
            self.assertEqual(packs,{'RIM-70':9,'RIMZP-45':14,'RIMZP-50':14})


if __name__=='__main__':unittest.main()
