import os
import time
from datetime import date
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.config import CAMERAS, Settings
from app.main import create_app
from app.playback import PendingPlayback
from app.recordings import Store, day, filename

@pytest.fixture
def setup(tmp_path):
    root = tmp_path / 'recordings'
    root.mkdir()
    for cam in CAMERAS:
        folder = root / cam
        folder.mkdir()
        for name in ['2026-10-01_12-00-00.mkv','2026-10-01_10-00-00.mkv','2026-10-02_10-00-00.mkv']:
            path = folder / name
            path.write_bytes(b'original fixture')
            os.utime(path, (time.time()-300, time.time()-300))
    settings = Settings(recordings_root=root)
    return root, settings, TestClient(create_app(settings,PendingPlayback()))

@pytest.mark.parametrize('cam', CAMERAS)
def test_all_cameras(setup, cam):
    _, _, client = setup
    response = client.get(f'/api/camera/{cam}/recordings?date=2026-10-01')
    assert response.status_code == 200
    assert [r['time'] for r in response.json()['recordings']] == ['10:00:00','12:00:00']
    assert client.get(f'/camera/{cam}').status_code == 200

@pytest.mark.parametrize('cam', ['cam00','cam09','CAM01','test','etc'])
def test_invalid_camera(setup, cam):
    assert setup[2].get(f'/camera/{cam}').status_code == 404

@pytest.mark.parametrize('value', ['2026-10-01','2024-02-29','0001-01-01','9999-12-31'])
def test_valid_date(setup, value):
    assert day(value).isoformat() == value
    assert setup[2].get('/camera/cam01',params={'date':value}).status_code == 200

@pytest.mark.parametrize('value', ['2026-2-01','2026-02-29','2026-13-01','2026-10-01x','../','２０２６-１０-０１','2026-10-01\n','0000-01-01'])
def test_invalid_date(setup, value):
    assert setup[2].get('/api/camera/cam01/recordings',params={'date':value}).status_code == 400

@pytest.mark.parametrize('value', ['../secret','2026-10-01_10-00-00.mkv/','2026-10-01_10-00-00.mkv\\','2026-02-30_10-00-00.mkv','2026-10-01_25-00-00.mkv','2026-10-01_10-00-00.MKV','2026-10-01_10-00-00.mkv\n','/etc/passwd','..','2026-10-01_10-00-00∕mkv'])
def test_invalid_filename(value):
    with pytest.raises(HTTPException): filename(value)

def test_valid_filename():
    assert filename('2026-10-01_10-00-00.mkv').hour == 10

@pytest.mark.parametrize('value', ['..%2Fetc%2Fpasswd','..%5Cetc%5Cpasswd','%252e%252e%252fetc%252fpasswd','%2Fetc%2Fpasswd','%00','2026-10-01_10-00-00.mkv%2Fextra','..%E2%88%95etc'])
def test_encoded_traversal(setup, value):
    response = setup[2].get('/download/cam01/' + value)
    assert response.status_code == 404
    assert 'root:' not in response.text

def test_symlinks_and_unrelated(setup, tmp_path):
    root, settings, client = setup
    outside = tmp_path/'secret'; outside.write_text('SECRET')
    (root/'cam01'/'2026-10-01_09-00-00.mkv').symlink_to(outside)
    (root/'cam01'/'unrelated.txt').write_text('SECRET')
    assert len(Store(settings).list('cam01', date(2026,10,1))) == 2
    assert client.get('/download/cam01/2026-10-01_09-00-00.mkv').status_code == 404
    (root/'cam09').symlink_to(root/'cam01',target_is_directory=True)
    assert client.get('/camera/cam09').status_code == 404
    # Replace a fixture camera with a symlink; directory traversal must reject it.
    (root/'cam08').rename(root/'oldcam08')
    (root/'cam08').symlink_to(root/'oldcam08',target_is_directory=True)
    assert client.get('/camera/cam08').status_code == 404

def test_active_and_newest(setup):
    root, settings, client = setup
    os.utime(root/'cam01'/'2026-10-01_12-00-00.mkv', None)
    result = Store(settings).list('cam01', date(2026,10,1))
    assert [r['time'] for r in result] == ['10:00:00']
    for name in ['2026-10-01_12-00-00.mkv','2026-10-02_10-00-00.mkv']:
        assert client.get('/download/cam01/'+name).status_code == 404
        assert client.post('/api/playback/cam01/'+name).status_code == 404

def test_download_and_missing(setup):
    root, settings, client = setup
    before = (root/'cam01'/'2026-10-01_10-00-00.mkv').stat()
    response = client.get('/download/cam01/2026-10-01_10-00-00.mkv')
    assert response.status_code == 200
    assert response.content == b'original fixture'
    assert response.headers['content-disposition'] == 'attachment; filename="cam01_2026-10-01_10-00-00.mkv"'
    after = (root/'cam01'/'2026-10-01_10-00-00.mkv').stat()
    assert (before.st_size,before.st_mtime_ns,before.st_ctime_ns) == (after.st_size,after.st_mtime_ns,after.st_ctime_ns)
    assert client.get('/download/cam01/2026-10-01_08-00-00.mkv').status_code == 404
    assert client.get('/download/cam09/2026-10-01_10-00-00.mkv').status_code == 404

def test_nginx_handoff(setup):
    root, _, _ = setup
    client = TestClient(create_app(Settings(recordings_root=root,download_mode='nginx')))
    response = client.get('/download/cam01/2026-10-01_10-00-00.mkv')
    assert response.status_code == 200
    assert not response.content
    assert response.headers['x-accel-redirect'] == '/_cctv_originals/cam01/2026-10-01_10-00-00.mkv'
    assert client.get('/_cctv_originals/cam01/2026-10-01_10-00-00.mkv').status_code == 404

def test_ui_and_playback(setup):
    _, settings, client = setup
    html = client.get('/').text
    assert all(settings.camera_names[c] in html for c in CAMERAS)
    page = client.get('/camera/cam01?date=2026-10-01')
    assert '2 recordings' in page.text
    assert 'Previous day' in page.text and 'Next day' in page.text
    assert 'No recordings available' in client.get('/camera/cam01?date=2025-01-01').text
    response = client.post('/api/playback/cam01/2026-10-01_10-00-00.mkv')
    assert response.status_code == 503 and response.json()['state'] == 'unavailable'
    assert client.get('/api/playback/status/arbitrary').status_code == 404
    assert 'frame-ancestors' in page.headers['content-security-policy']
    assert client.get('/docs').status_code == 404
    assert client.post('/api/playback/cam01/2026-10-01_10-00-00.mkv',content=b'x'*5000).status_code == 413

def test_camera_names_escaped(setup):
    root, _, _ = setup
    names = {c:c.upper() for c in CAMERAS}; names['cam01']='<script>alert(1)</script>'
    client=TestClient(create_app(Settings(recordings_root=root,camera_names=names)))
    assert '<script>alert(1)</script>' not in client.get('/').text
    assert '&lt;script&gt;' in client.get('/').text

@pytest.mark.parametrize('route',['/etc/passwd','/srv/cctv/cam01/','/static/..%2F..%2Fetc%2Fpasswd','/static/%2e%2e/app/main.py','/download/test/2026-10-01_10-00-00.mkv'])
def test_no_arbitrary_access(setup, route):
    assert setup[2].get(route).status_code == 404

def test_empty_and_special_files(setup):
    root, settings, client=setup
    (root/'cam01'/'2026-10-01_08-00-00.mkv').touch()
    os.mkfifo(root/'cam01'/'2026-10-01_07-00-00.mkv')
    assert len(Store(settings).list('cam01', date(2026,10,1)))==2
    for name in ['2026-10-01_08-00-00.mkv','2026-10-01_07-00-00.mkv']:
        assert client.get('/download/cam01/'+name).status_code == 404

def test_download_swap_rejected(setup, tmp_path, monkeypatch):
    root, settings, _=setup
    store=Store(settings)
    original=store.entries
    outside=tmp_path/'secret'; outside.write_text('SECRET')
    target=root/'cam01'/'2026-10-01_10-00-00.mkv'
    def swap(fd):
        rows=original(fd)
        target.unlink()
        target.symlink_to(outside)
        return rows
    monkeypatch.setattr(store,'entries',swap)
    with pytest.raises(HTTPException): store.open('cam01',target.name)

def test_configuration_and_age(setup):
    root, _, _=setup
    with pytest.raises(ValueError): Settings(minimum_age=-1)
    with pytest.raises(ValueError): Settings(download_mode='unsafe')
    with pytest.raises(ValueError): Settings(camera_names={'cam09':'Invalid'})
    settings=Settings(recordings_root=root,minimum_age=600)
    assert Store(settings).list('cam01',date(2026,10,1))==[]

@pytest.mark.parametrize('route,expected',[('/camera/cam01?date=2026-10-01','camera-page'),('/arbitrary-private-name?token=synthetic','other')])
def test_request_logs_do_not_include_urls_or_credentials(setup,caplog,route,expected):
    import logging
    caplog.set_level(logging.INFO,logger='cctv.request')
    setup[2].get(route,headers={'Authorization':'Basic synthetic-private-token'})
    records=[r.getMessage() for r in caplog.records if r.name=='cctv.request']
    assert any('category='+expected in record for record in records)
    assert all('synthetic' not in record and '/camera/' not in record and '2026-10-01' not in record and 'arbitrary-private' not in record for record in records)


def test_original_head(setup):
    _, _, client = setup
    response = client.head('/download/cam01/2026-10-01_10-00-00.mkv')
    assert response.status_code == 200
    assert response.content == b''
    assert int(response.headers['content-length']) == len(b'original fixture')
    assert response.headers['content-disposition'].startswith('attachment;')
