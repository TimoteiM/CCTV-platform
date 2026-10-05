import re
import sqlite3
import time
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from passlib.hash import apr_md5_crypt
from app.auth import COOKIE, FORM_COOKIE, Sessions, destination
from app.config import CAMERAS, Settings
from app.main import create_app
from app.playback import PendingPlayback

@pytest.fixture
def client(tmp_path):
    root = tmp_path / 'recordings'
    for cam in CAMERAS:
        (root / cam).mkdir(parents=True)
    credentials = tmp_path / 'users'
    credentials.write_text('operator:' + apr_md5_crypt.hash('fixture-good-passphrase') + '\n')
    settings = Settings(recordings_root=root, cache_root=tmp_path / 'cache', auth_credentials=credentials, auth_state=tmp_path / 'auth')
    return TestClient(create_app(settings, PendingPlayback()), base_url='https://testserver'), settings

def sign_in(client, remember=False, **overrides):
    response = client.get('/login?next=%2Fevents', follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([^"]+)"', response.text)[1]
    data = dict(csrf=csrf, username='operator', password='fixture-good-passphrase', next='/events')
    if remember: data['remember'] = 'on'
    data.update(overrides)
    return client.post('/login', data=data, follow_redirects=False)

@pytest.mark.parametrize('path', ['/api/recording-health', '/api/events?date=2026-10-05', '/event-media/' + 'a'*64 + '.jpg', '/live-media/cam01/invalid/index.m3u8', '/download/cam01/foo', '/auth/check'])
def test_private_endpoints_reject_anonymous(client, path):
    response = client[0].get(path)
    assert response.status_code == 401
    assert 'www-authenticate' not in response.headers

def test_login_public_and_deep_link(client):
    c, _ = client
    response = c.get('/playback?cam=cam03&date=2026-10-05', follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'].startswith('/login?next=')
    login = c.get(response.headers['location'])
    assert login.status_code == 200
    assert 'Keep me signed in' in login.text
    assert c.get('/static/logo.svg').status_code == 200
    assert 'no-store' in login.headers['cache-control']
    assert login.headers['referrer-policy'] == 'same-origin'

def test_remembered_session_secure_and_persistent(client):
    c, settings = client
    result = sign_in(c, True)
    assert result.status_code == 303
    assert result.headers['location'] == '/events'
    cookie = result.headers.get_list('set-cookie')[0]
    for flag in ['HttpOnly', 'Secure', 'SameSite=lax', 'Max-Age=31536000', 'Path=/']:
        assert flag in cookie
    assert c.get('/auth/check').status_code == 204
    token = c.cookies.get(COOKIE)
    assert Sessions(settings.auth_credentials, settings.auth_state).lookup(token)['remember']
    with sqlite3.connect(settings.auth_state / 'sessions.sqlite') as db:
        saved = db.execute('SELECT token FROM sessions').fetchone()[0]
    assert saved != token
    assert 'fixture-good-passphrase' not in (settings.auth_state / 'sessions.sqlite').read_bytes().decode('latin1')
    page = c.get('/events')
    assert page.status_code == 200
    assert 'Sign out' in page.text
    assert 'Max-Age=31536000' in page.headers['set-cookie']

def test_unchecked_cookie_is_browser_session(client):
    response = sign_in(client[0])
    assert response.status_code == 303
    assert 'Max-Age' not in response.headers.get_list('set-cookie')[0]

def test_bad_password_and_csrf(client):
    c, _ = client
    assert sign_in(c, password='incorrect').status_code == 401
    assert c.get('/auth/check').status_code == 401
    assert sign_in(c, csrf='invalid').status_code == 403
    assert sign_in(c, csrf='é').status_code == 403
    assert c.post('/login', data={'username':'operator','password':'fixture-good-passphrase'}).status_code == 403

def test_rate_limit_survives_restart(client):
    c, settings = client
    for _ in range(10): assert sign_in(c, password='incorrect').status_code == 401
    assert sign_in(c).status_code == 429
    fresh = Sessions(settings.auth_credentials, settings.auth_state)
    assert fresh.authenticate('operator', 'fixture-good-passphrase', 'testclient') == 'limited'

def test_logout_revokes_session_and_csrf(client):
    c, settings = client
    sign_in(c, True)
    token = c.cookies.get(COOKIE)
    assert c.post('/logout', data={'csrf':'invalid'}).status_code == 403
    assert c.post('/logout', data={'csrf':Sessions.digest(token)}, headers={'Origin':'https://evil.example'}).status_code == 403
    result = c.post('/logout', data={'csrf':Sessions.digest(token)}, follow_redirects=False)
    assert result.status_code == 303
    assert Sessions(settings.auth_credentials, settings.auth_state).lookup(token) is None
    assert c.get('/auth/check').status_code == 401

@pytest.mark.parametrize('value', ['https://evil.example', '//evil.example', '/%2f%2fevil.example', '/%255cevil.example', '/\\evil.example', '/login', '/live\r\nLocation: evil'])
def test_redirects_stay_local(value):
    assert destination(value) == '/live'

def test_tampered_expired_and_changed_credentials(client):
    c, settings = client
    sign_in(c, True)
    token = c.cookies.get(COOKIE)
    sessions = Sessions(settings.auth_credentials, settings.auth_state)
    assert sessions.lookup(token + 'x') is None
    with sessions.connect() as db:
        db.execute('UPDATE sessions SET expires=?', (time.time()-1,))
    assert sessions.lookup(token) is None
    token = sessions.issue('operator', True)
    settings.auth_credentials.write_text('operator:' + apr_md5_crypt.hash('changed-fixture-password') + '\n')
    assert Sessions(settings.auth_credentials, settings.auth_state).lookup(token) is None

@pytest.mark.parametrize('address', ['https://testserver', 'https://192.0.2.50', 'https://camera.example:8443'])
def test_login_and_logout_from_current_browser_origin(client, address):
    original, _ = client
    c = TestClient(original.app, base_url=address)
    response = c.get('/login')
    csrf = re.search(r'name="csrf" value="([^"]+)"', response.text)[1]
    result = c.post('/login', data={'csrf':csrf, 'username':'operator', 'password':'fixture-good-passphrase'}, headers={'Origin':address, 'Sec-Fetch-Site':'same-origin'}, follow_redirects=False)
    assert result.status_code == 303
    token = c.cookies.get(COOKIE)
    assert c.post('/logout', data={'csrf':Sessions.digest(token)}, headers={'Origin':address}, follow_redirects=False).status_code == 303

def test_login_rejects_foreign_origin(client):
    c, _ = client
    response = c.get('/login')
    csrf = re.search(r'name="csrf" value="([^"]+)"', response.text)[1]
    result = c.post('/login', data={'csrf':csrf, 'username':'operator', 'password':'fixture-good-passphrase'}, headers={'Origin':'https://foreign.example'}, follow_redirects=False)
    assert result.status_code == 403
    assert c.get('/auth/check').status_code == 401
