"""Opaque, revocable sessions; legacy credentials remain outside the source tree."""
import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit, unquote
from passlib.hash import apr_md5_crypt

COOKIE = '__Host-cctv_session'
FORM_COOKIE = '__Host-cctv_login'
YEAR = 365 * 24 * 3600

def destination(value):
    value = value or '/live'
    decoded = unquote(unquote(value))
    if not decoded.startswith('/') or decoded.startswith('//') or '\\' in decoded or any(ord(c) < 32 for c in decoded) or urlsplit(decoded).netloc or decoded.startswith(('/login', '/logout', '/auth/')):
        return '/live'
    return value

class Sessions:
    def __init__(self, credentials, root):
        self.credentials = Path(credentials)
        self.users = dict(line.split(':', 1) for line in self.credentials.read_text().splitlines() if line)
        if not self.users or any(not value.startswith('$apr1$') for value in self.users.values()):
            raise ValueError('Unsupported authentication configuration')
        self.path = Path(root) / 'sessions.sqlite'
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, username TEXT, credential TEXT, expires REAL, remember INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS attempts (identity TEXT PRIMARY KEY, count INTEGER, until REAL)')
        self.dummy = apr_md5_crypt.hash(secrets.token_urlsafe(32))

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def authenticate(self, username, password, address):
        now = time.time()
        identities = [self.digest('ip:' + address), self.digest('user:' + username)]
        with self.connect() as db:
            db.execute('DELETE FROM attempts WHERE until < ?', (now,))
            for identity in identities:
                row = db.execute('SELECT count FROM attempts WHERE identity=?', (identity,)).fetchone()
                if row and row[0] >= 10:
                    return 'limited'
            expected = self.users.get(username, self.dummy)
            valid = len(password) <= 512 and apr_md5_crypt.verify(password, expected) and username in self.users
            if valid:
                db.execute('DELETE FROM attempts WHERE identity=?', (identities[1],))
                return 'valid'
            for identity in identities:
                db.execute('INSERT INTO attempts VALUES (?,1,?) ON CONFLICT(identity) DO UPDATE SET count=count+1', (identity, now + 900))
        return 'invalid'

    def issue(self, username, remember):
        token = secrets.token_urlsafe(48)
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?)', (self.digest(token), username, self.digest(self.users[username]), time.time() + (YEAR if remember else 86400), int(remember)))
        return token

    def lookup(self, token):
        if not token or len(token) > 128:
            return None
        with self.connect() as db:
            row = db.execute('SELECT username,credential,expires,remember FROM sessions WHERE token=?', (self.digest(token),)).fetchone()
        if not row or row[2] < time.time() or row[0] not in self.users or not hmac.compare_digest(row[1], self.digest(self.users[row[0]])):
            return None
        return {'username': row[0], 'remember': bool(row[3])}

    def renew(self, token):
        with self.connect() as db:
            db.execute('UPDATE sessions SET expires=? WHERE token=? AND remember=1', (time.time() + YEAR, self.digest(token)))

    def revoke(self, token):
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE token=?', (self.digest(token or ''),))

    @staticmethod
    def cookie(response, token, remember):
        response.set_cookie(COOKIE, token, max_age=YEAR if remember else None, secure=True, httponly=True, samesite='lax', path='/')
