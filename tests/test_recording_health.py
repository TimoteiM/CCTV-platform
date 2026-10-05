import json
import time
from fastapi.testclient import TestClient
from app.config import Settings,CAMERAS
from app.main import create_app
from app.playback import PendingPlayback

def client(tmp_path, rows=None, age=0):
    root=tmp_path/'recordings'
    for cam in CAMERAS:(root/cam).mkdir(parents=True)
    status=tmp_path/'status.json'
    status.write_text(json.dumps({'checked_at':time.time()-age,'cameras':rows if rows is not None else [{'camera':cam,'status':'healthy','last_write':time.time()} for cam in CAMERAS]}))
    settings=Settings(recordings_root=root,cache_root=tmp_path/'cache',recorder_health_path=status)
    return TestClient(create_app(settings,PendingPlayback()))

def test_health_reports_all_cameras_without_exposing_private_details(tmp_path):
    data=client(tmp_path).get('/api/recording-health').json()
    assert data['configured'] and data['available']
    assert len(data['cameras'])==8
    assert all(set(row)=={'camera','name','status'} for row in data['cameras'])

def test_expired_monitor_status_is_unavailable(tmp_path):
    assert client(tmp_path,age=101).get('/api/recording-health').json()['available'] is False

def test_invalid_status_is_rejected(tmp_path):
    assert client(tmp_path,rows=[{'camera':'cam03','status':'healthy'}]*8).get('/api/recording-health').json()['available'] is False

def test_camera_recovery_is_visible(tmp_path):
    rows=[{'camera':cam,'status':'recovering' if cam=='cam03' else 'healthy'} for cam in CAMERAS]
    data=client(tmp_path,rows=rows).get('/api/recording-health').json()
    assert data['available']
    assert data['cameras'][2]['status']=='recovering'
