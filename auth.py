import hashlib
import os
import secrets
import sqlite3
import smtplib
import time
from email.message import EmailMessage
from datetime import datetime, timezone

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from app import app, db, rows

PERMISSIONS = ('pos_edit','inventory_edit','invoice_edit','issue_edit','task_edit','reports_view','imports_apply','users_manage','merge_pdfs','accounting_edit')

with db() as c:
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, permissions TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, user_id INTEGER, action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS reset_tokens (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at INTEGER NOT NULL);
    ''')
    if 'email' not in [r['name'] for r in c.execute('PRAGMA table_info(users)')]:
        c.execute("ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''")
    if 'role' not in [r['name'] for r in c.execute('PRAGMA table_info(users)')]:
        c.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'staff'")
        c.execute("UPDATE users SET role='warehouse',permissions='' WHERE LOWER(username) LIKE '%warehouse%'")


def hash_password(password, salt=None):
    if len(password)<12: raise ValueError('Password must have at least 12 characters')
    salt=salt or secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,600_000)
    return salt.hex()+':'+digest.hex()


def verify(password, stored):
    try:
        salt_hex,digest=stored.split(':')
        salt=bytes.fromhex(salt_hex)
        check=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,600_000).hex()
        return secrets.compare_digest(check,digest)
    except (ValueError,AttributeError): return False


def capability(path,method):
    if path=='/api/dashboard':return ''
    if path.startswith('/api/auth/users'): return 'users_manage'
    if path.startswith('/api/admin/'): return 'users_manage'
    if path.startswith(('/api/import/','/api/nupco/')):
        return 'imports_apply' if method=='POST' else 'reports_view'
    if path.startswith('/api/seed/'): return 'imports_apply'
    if path.startswith('/api/inventory/starting-'): return 'inventory_edit'
    if path.startswith('/api/status-options'): return 'users_manage' if method!='GET' else ''
    if path=='/api/po-pdf/merge':return 'merge_pdfs'
    if path.startswith('/api/po-pdf/'): return 'pos_edit'
    if path.startswith('/api/pos/') and path.endswith('/documents/upload'):return ''
    if path.startswith('/api/pos/') and path.endswith('/documents/merge'):return ''
    if path.endswith('/payment') and path.startswith('/api/pos/'):return 'accounting_edit'
    if path.startswith('/api/po-lines/') and path.endswith('/reschedule'):return 'pos_edit'
    if path.startswith('/api/po-documents/'): return 'imports_apply'
    # Assignment ownership, rather than a broad tool grant, controls ASN submissions.
    if path.startswith('/api/asn-assignments'): return ''
    if path.startswith('/api/warehouse-assignments'): return ''
    if path.startswith('/api/documents/'): return ''
    if path.startswith('/api/reports/operations'): return 'reports_view'
    if path.startswith('/api/items') or path.startswith('/api/batches'): return 'inventory_edit'
    if path.startswith('/api/pos/') and path.endswith('/invoice-stage'): return 'invoice_edit'
    if path.startswith('/api/pos'): return 'pos_edit' if method!='GET' else 'reports_view'
    if path=='/api/invoices/next-number': return 'invoice_edit'
    if path.startswith('/api/invoices'): return 'invoice_edit' if method!='GET' else 'reports_view'
    if path.startswith('/api/issues'): return 'issue_edit'
    if path.startswith('/api/tasks'): return 'task_edit'
    return 'reports_view'


@app.middleware('http')
async def protect(request:Request,call_next):
    path=request.url.path
    if getattr(app.state,'maintenance',False) and path.startswith('/api/'):
        return JSONResponse({'detail':'Database maintenance in progress; retry shortly'},status_code=503)
    if path=='/' or path in ('/api/auth/login','/api/auth/logout','/api/auth/reset-request','/api/auth/reset-confirm'):
        return await call_next(request)
    if not path.startswith('/api/') and path not in ('/docs','/openapi.json','/redoc'):
        return await call_next(request)
    token=request.cookies.get('session','')
    with db() as c:
        user=c.execute('''SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1''',(hashlib.sha256(token.encode()).hexdigest(),int(time.time()))).fetchone() if token else None
    if not user: return JSONResponse({'detail':'Sign in required'},status_code=401)
    request.state.user=dict(user)
    if user['role']=='warehouse':
        allowed=path in ('/api/dashboard','/api/auth/me','/api/auth/password') or path.startswith(('/api/warehouse-assignments','/api/documents/'))
        if not allowed:return JSONResponse({'detail':'Warehouse access is limited to assigned work'},status_code=403)
    if path not in ('/api/auth/me','/api/auth/password') and not user['is_admin']:
        required=capability(path,request.method)
        if required and required not in user['permissions'].split(','): return JSONResponse({'detail':f'Permission required: {required}'},status_code=403)
    if request.method in ('POST','PATCH','PUT','DELETE') and path!='/api/auth/login':
        # Custom header is required for browser writes so another site cannot submit a form with the session cookie.
        if request.headers.get('X-Logistics-Request')!='1': return JSONResponse({'detail':'Request header required'},status_code=403)
    return await call_next(request)


class Login(BaseModel):
    username:str
    password:str


@app.post('/api/auth/login')
def login(v:Login,response:Response):
    with db() as c:
        user=c.execute('SELECT * FROM users WHERE username=? AND active=1',(v.username.strip(),)).fetchone()
        if not user or not verify(v.password,user['password_hash']): raise HTTPException(401,'Invalid username or password')
        token=secrets.token_urlsafe(32)
        c.execute('INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user['id'],int(time.time())+12*3600))
        c.execute('INSERT INTO audit(user_id,action) VALUES(?,?)',(user['id'],'login'))
    response.set_cookie('session',token,max_age=12*3600,httponly=True,samesite='strict',secure=os.getenv('COOKIE_SECURE','0')=='1')
    return {'username':user['username'],'is_admin':bool(user['is_admin']),'role':user['role'],'permissions':list(PERMISSIONS) if user['is_admin'] else user['permissions'].split(',')}


@app.post('/api/auth/logout')
def logout(request:Request,response:Response):
    token=request.cookies.get('session','')
    if token:
        with db() as c:c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie('session')
    return {'ok':True}


@app.get('/api/auth/me')
def me(request:Request):
    u=request.state.user
    return {'id':u['id'],'username':u['username'],'is_admin':bool(u['is_admin']),'role':u['role'],'permissions':list(PERMISSIONS) if u['is_admin'] else u['permissions'].split(',')}


class UserCreate(BaseModel):
    username:str
    password:str
    permissions:list[str]=[]
    email:str=''
    is_admin:bool=False
    role:str='staff'


@app.get('/api/auth/users')
def users():
    with db() as c:return rows(c,'SELECT id,username,email,is_admin,active,permissions,role FROM users ORDER BY username')


@app.post('/api/auth/users')
def create_user(v:UserCreate,request:Request):
    if v.role not in ('staff','warehouse','accountant'):raise HTTPException(400,'Unknown role')
    if any(p not in PERMISSIONS for p in v.permissions):raise HTTPException(400,'Unknown permission')
    try: password_hash=hash_password(v.password)
    except ValueError as e:raise HTTPException(400,str(e))
    with db() as c:
        owner=c.execute("SELECT id FROM users WHERE username='admin' AND is_admin=1").fetchone()
        if v.is_admin and (not request.state.user['is_admin'] or not owner or request.state.user['id']!=owner['id']):raise HTTPException(403,'Only the primary admin can create assistant admins')
        try: uid=c.execute('INSERT INTO users(username,email,password_hash,is_admin,permissions,role) VALUES(?,?,?,?,?,?)',(v.username.strip(),v.email.strip().lower(),password_hash,int(v.is_admin),'' if v.role=='warehouse' else ','.join(sorted(set(v.permissions))),v.role)).lastrowid
        except sqlite3.IntegrityError:raise HTTPException(409,'Username already exists')
        c.execute('INSERT INTO audit(user_id,action,target) VALUES(?,?,?)',(request.state.user['id'],'create_user',str(uid)))
    return {'id':uid}


class UserUpdate(BaseModel):
    active:bool|None=None
    permissions:list[str]|None=None
    password:str|None=None
    email:str|None=None
    role:str|None=None


@app.patch('/api/auth/users/{user_id}')
def update_user(user_id:int,v:UserUpdate,request:Request):
    if any(p not in PERMISSIONS for p in (v.permissions or [])):raise HTTPException(400,'Unknown permission')
    if user_id==request.state.user['id'] and v.active is False:raise HTTPException(400,'Cannot deactivate yourself')
    with db() as c:
        target=c.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
        if not target:raise HTTPException(404,'User not found')
        owner=c.execute("SELECT id FROM users WHERE username='admin' AND is_admin=1").fetchone()
        if owner and user_id==owner['id'] and request.state.user['id']!=owner['id']:raise HTTPException(403,'Primary admin account is protected')
        if target['is_admin'] and not request.state.user['is_admin']:raise HTTPException(403,'Only an admin can change an admin account')
        if target['is_admin'] and user_id==owner['id'] and (v.active is False or v.permissions is not None):raise HTTPException(400,'Primary admin account cannot be deactivated or restricted')
        if v.role is not None:
            if v.role not in ('staff','warehouse','accountant'):raise HTTPException(400,'Unknown role')
            c.execute('UPDATE users SET role=? WHERE id=?',(v.role,user_id))
        if v.active is not None:
            c.execute('UPDATE users SET active=? WHERE id=?',(int(v.active),user_id))
            if not v.active:c.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
        if v.permissions is not None:c.execute('UPDATE users SET permissions=? WHERE id=?',(','.join(sorted(set(v.permissions))),user_id))
        if v.email is not None:c.execute('UPDATE users SET email=? WHERE id=?',(v.email.strip().lower(),user_id))
        if v.password is not None:
            if not owner or request.state.user['id']!=owner['id']:raise HTTPException(403,'Primary admin only')
            try:h=hash_password(v.password)
            except ValueError as e:raise HTTPException(400,str(e))
            c.execute('UPDATE users SET password_hash=? WHERE id=?',(h,user_id))
            c.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
        c.execute('INSERT INTO audit(user_id,action,target) VALUES(?,?,?)',(request.state.user['id'],'update_user',str(user_id)))
    return {'ok':True}


@app.delete('/api/auth/users/{user_id}')
def delete_user(user_id:int,request:Request):
    with db() as c:
        owner=c.execute("SELECT id FROM users WHERE username='admin' AND is_admin=1").fetchone()
        if not owner or request.state.user['id']!=owner['id']:raise HTTPException(403,'Primary admin only')
        if user_id==owner['id']:raise HTTPException(400,'Primary admin account is protected')
        target=c.execute('SELECT username FROM users WHERE id=?',(user_id,)).fetchone()
        if not target:raise HTTPException(404,'User not found')
        c.execute("UPDATE tasks SET assigned_to='' WHERE assigned_to=? AND status NOT IN ('done','completed')",(target['username'],))
        c.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
        c.execute('DELETE FROM reset_tokens WHERE user_id=?',(user_id,))
        c.execute('UPDATE audit SET user_id=NULL WHERE user_id=?',(user_id,))
        c.execute('UPDATE po_status_events SET user_id=NULL WHERE user_id=?',(user_id,))
        c.execute('UPDATE invoice_number_transfers SET user_id=NULL WHERE user_id=?',(user_id,))
        c.execute('UPDATE po_invoice_numbers SET assigned_by=NULL WHERE assigned_by=?',(user_id,))
        c.execute('UPDATE tasks SET created_by=NULL WHERE created_by=?',(user_id,))
        c.execute('DELETE FROM users WHERE id=?',(user_id,))
        c.execute('INSERT INTO audit(user_id,action,target) VALUES(?,?,?)',(request.state.user['id'],'delete_user',target['username']))
    return {'ok':True}


class PasswordChange(BaseModel):
    old_password:str
    new_password:str


@app.post('/api/auth/password')
def change_password(v:PasswordChange,request:Request):
    user=request.state.user
    if not verify(v.old_password,user['password_hash']):raise HTTPException(401,'Current password is incorrect')
    try:h=hash_password(v.new_password)
    except ValueError as e:raise HTTPException(400,str(e))
    with db() as c:
        c.execute('UPDATE users SET password_hash=? WHERE id=?',(h,user['id']))
        c.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
        c.execute('INSERT INTO audit(user_id,action) VALUES(?,?)',(user['id'],'change_password'))
    return {'ok':True}


class ResetRequest(BaseModel):
    email:str


@app.post('/api/auth/reset-request')
def request_reset(v:ResetRequest):
    config=('SMTP_HOST','SMTP_FROM','APP_BASE_URL')
    if not all(os.getenv(k) for k in config):raise HTTPException(503,'Email reset is not configured')
    with db() as c:
        user=c.execute('SELECT id FROM users WHERE email=? AND active=1',(v.email.strip().lower(),)).fetchone()
        if user:
            token=secrets.token_urlsafe(32)
            c.execute('INSERT INTO reset_tokens(token_hash,user_id,expires_at) VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user['id'],int(time.time())+3600))
            link=os.getenv('APP_BASE_URL').rstrip('/')+'/?reset='+token
            message=EmailMessage()
            message['Subject']='Logistics Control password reset'
            message['From']=os.getenv('SMTP_FROM')
            message['To']=v.email.strip().lower()
            message.set_content(f'Use this one-time link within one hour to reset your password:\n{link}\n\nIf you did not request it, ignore this message.')
            try:
                with smtplib.SMTP(os.getenv('SMTP_HOST'),int(os.getenv('SMTP_PORT','587')),timeout=10) as smtp:
                    smtp.starttls()
                    if os.getenv('SMTP_USER'):smtp.login(os.getenv('SMTP_USER'),os.getenv('SMTP_PASSWORD',''))
                    smtp.send_message(message)
            except (OSError,smtplib.SMTPException):
                c.execute('DELETE FROM reset_tokens WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
                raise HTTPException(503,'Email service unavailable')
    return {'ok':True}


class ResetConfirm(BaseModel):
    token:str
    new_password:str


@app.post('/api/auth/reset-confirm')
def confirm_reset(v:ResetConfirm):
    try:h=hash_password(v.new_password)
    except ValueError as e:raise HTTPException(400,str(e))
    token_hash=hashlib.sha256(v.token.encode()).hexdigest()
    with db() as c:
        row=c.execute('SELECT user_id FROM reset_tokens WHERE token_hash=? AND expires_at>?',(token_hash,int(time.time()))).fetchone()
        if not row:raise HTTPException(400,'Reset link is invalid or expired')
        c.execute('UPDATE users SET password_hash=? WHERE id=?',(h,row['user_id']))
        c.execute('DELETE FROM reset_tokens WHERE user_id=?',(row['user_id'],))
        c.execute('DELETE FROM sessions WHERE user_id=?',(row['user_id'],))
        c.execute('INSERT INTO audit(user_id,action) VALUES(?,?)',(row['user_id'],'reset_password'))
    return {'ok':True}
