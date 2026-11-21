"""Local bootstrap: python manage.py create-admin"""
import getpass
import sqlite3
import sys
from auth import hash_password
from app import db

if len(sys.argv)!=2 or sys.argv[1]!='create-admin':
    print('Usage: python manage.py create-admin');sys.exit(2)
username=input('Admin username: ').strip()
email=input('Admin email (for password recovery): ').strip().lower()
password=getpass.getpass('Admin password (12+ characters): ')
confirm=getpass.getpass('Confirm password: ')
if not username or '@' not in email or password!=confirm:
    print('Username, email or password confirmation invalid');sys.exit(1)
try:h=hash_password(password)
except ValueError as e:print(e);sys.exit(1)
with db() as c:
    try:c.execute('INSERT INTO users(username,email,password_hash,is_admin) VALUES(?,?,?,1)',(username,email,h))
    except sqlite3.IntegrityError:print('Username already exists');sys.exit(1)
print('Admin created')
