"""Create the first admin from environment variables when the database has no users."""
import hashlib
import os
import secrets

from app import db


def hash_pw(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600_000)
    return salt.hex() + ':' + digest.hex()


def main() -> None:
    username = os.getenv('ADMIN_USERNAME', '').strip()
    password = os.getenv('ADMIN_PASSWORD', '')
    email = os.getenv('ADMIN_EMAIL', '').strip().lower() or 'admin@local'
    if not username or not password:
        print('bootstrap_admin: ADMIN_USERNAME/ADMIN_PASSWORD not set; skipping')
        return
    with db() as c:
        if c.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            print('bootstrap_admin: users already exist; skipping')
            return
        c.execute(
            'INSERT INTO users(username,email,password_hash,is_admin,active) VALUES(?,?,?,1,1)',
            (username, email, hash_pw(password)),
        )
        print(f'bootstrap_admin: created admin user {username!r}')


if __name__ == '__main__':
    main()