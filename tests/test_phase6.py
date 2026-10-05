import os,time,logging
from pathlib import Path
from dataclasses import replace
import pytest
from fastapi import HTTPException
from app.live_engine import LiveManager
from app.timeline import second,coverage,locate,following
from app.recordings import Store
from app.config import Settings,CAMERAS
from app.main import create_app
from app.playback import PendingPlayback
from fastapi.testclient import TestClient
class Process:
 def __init__(self):self.code=None;self.pid=12345
 def poll(self):return self.code
class Clock:
 def __init__(self):self.now=0
 def __call__(self):return self.now
@pytest.fixture
def live(tmp_path):
 clock=Clock();processes=[]
 def factory(cam,folder):
  p=Process();processes.append(p);(folder/'index.m3u8').write_text('#EXTM3U\nseg00000.ts\n');(folder/'seg00000.ts').write_bytes(b'media');return p
 manager=LiveManager(tmp_path/'live',factory,clock,sleep=lambda delay:setattr(clock,'now',clock.now+delay));manager.terminate=lambda p:setattr(p,'code',0)
 yield manager,clock,processes
 manager.close()
@pytest.mark.parametrize('camera',CAMERAS)
def test_live_all_cameras(live,camera):
 manager,_,_=live;assert manager.session(camera)['state']=='live'
@pytest.mark.parametrize('camera',['cam09','cam00','CAM01','../cam01','cam01/','cam01\\','etc'])
def test_live_camera_validation(live,camera):
 with pytest.raises(HTTPException):live[0].session(camera)
def test_duplicate_live_viewers_share_pipeline(live):
 manager,_,processes=live;a=manager.session('cam01');b=manager.session('cam01');assert a['lease']!=b['lease'] and a['generation']==b['generation'];assert len(processes)==1
 manager.release('cam01',a['lease']);assert manager.state('cam01',b['lease'])['state']=='live'
def test_live_idle_shutdown(live):
 manager,clock,processes=live;s=manager.session('cam01');manager.release('cam01',s['lease']);clock.now=31;manager.reap();assert not manager.pipelines and processes[0].code==0
def test_live_abandoned_viewer_expiry(live):
 manager,clock,_=live;manager.session('cam01');clock.now=21;manager.reap();assert not manager.leases;clock.now=52;manager.reap();assert not manager.pipelines
def test_live_heartbeat(live):
 manager,clock,_=live;s=manager.session('cam01');clock.now=15;manager.state('cam01',s['lease'],True);clock.now=25;assert manager.state('cam01',s['lease'])['state']=='live'
def test_live_failure_safe_state(live):
 manager,_,processes=live;s=manager.session('cam01');processes[0].code=1;assert manager.state('cam01',s['lease'])['state']=='error'
def test_camera_unavailable_sanitized(tmp_path,caplog):
 def fail(*args):raise RuntimeError('rtsp://private:secret@198.51.100.1')
 manager=LiveManager(tmp_path/'live',fail)
 with pytest.raises(HTTPException) as e:manager.session('cam01')
 assert e.value.detail=='Camera unavailable' and 'secret' not in caplog.text
 assert not manager.pipelines
def test_live_capacity_and_startup_rate(live):
 manager,clock,processes=live
 for camera in CAMERAS:assert manager.session(camera)['state']=='live'
 assert len(processes)==8
 assert clock.now>=1.75

 manager.drop('cam08');manager.max=7
 with pytest.raises(HTTPException) as error:manager.session('cam08')
 assert error.value.status_code==429

@pytest.mark.parametrize('filename',['../index.m3u8','seg00000.ts/','seg00000.ts\\','.worker.lock','password','index.m3u8.tmp'])
def test_live_media_traversal(live,filename):
 manager,_,_=live;s=manager.session('cam01')
 with pytest.raises(HTTPException):manager.media('cam01',s['generation'],s['lease'],filename)
def test_live_session_media_resolution(live):
 manager,_,_=live;s=manager.session('cam01');r=manager.media('cam01',s['generation'],s['lease'],'index.m3u8');assert r['internal'].startswith('/_cctv_live/cam01/')
 manager.release('cam01',s['lease'])
 with pytest.raises(HTTPException):manager.media('cam01',s['generation'],s['lease'],'index.m3u8')
def test_live_symlink_rejection(live,tmp_path):
 manager,_,_=live;s=manager.session('cam01');folder=manager.pipelines['cam01']['folder'];(folder/'seg00000.ts').unlink();(folder/'seg00000.ts').symlink_to(tmp_path/'secret')
 with pytest.raises(HTTPException):manager.media('cam01',s['generation'],s['lease'],'seg00000.ts')
@pytest.fixture
def archive(tmp_path):
 root=tmp_path/'source';root.mkdir()
 for cam in CAMERAS:
  folder=root/cam;folder.mkdir()
  for t in ['10-00-00','10-05-00','11-00-00','12-00-00']:
   p=folder/f'2026-10-01_{t}.mkv';p.write_bytes(b'fixture');os.utime(p,(time.time()-600,time.time()-600))
 return Store(Settings(recordings_root=root,cache_root=tmp_path/'cache'))
def test_timeline_intervals_and_gaps(archive):
 data=coverage(archive,PendingPlayback(),'cam01','2026-10-01');assert [(i['start'],i['end']) for i in data['intervals']]==[(36000,36600),(39600,39900)]
 with pytest.raises(HTTPException):locate(data,'10:30:00')
def test_timestamp_mapping_and_offset(archive):
 data=coverage(archive,PendingPlayback(),'cam01','2026-10-01');segment,offset=locate(data,'10:07:20');assert segment['recording'].endswith('10-05-00.mkv') and offset==140
 assert following(data,data['segments'][0]['recording'],300)['start']==36300
 assert following(data,data['segments'][1]['recording'],300) is None
@pytest.mark.parametrize('value',['24:00:00','10:61:00','1:00:00','../','12:00','12:00:00\n'])
def test_invalid_timeline_time(value):
 with pytest.raises(HTTPException):second(value)
def test_timeline_duration_refinement(archive):
 class Provider:
  def known_duration(self,*args):return 320
 data=coverage(archive,Provider(),'cam01','2026-10-01');assert data['segments'][0]['end']==36320 and not data['segments'][0]['estimated']
def test_timeline_api_and_names(archive):
 settings=archive.settings if hasattr(archive,'settings') else None
 # Store retains the configured root through its settings.
 client=TestClient(create_app(settings,PendingPlayback())) if settings else None
 if client:
  assert client.get('/api/timeline/cam09?date=2026-10-01').status_code==404
  assert client.get('/api/timeline/cam01?date=bad').status_code==400
  assert client.post('/api/timeline/cam01/seek?date=2026-10-01&time=25:00:00').status_code==400
  assert client.get('/playback?cam=cam01&date=2026-10-01&time=10:07:20').status_code==200
  assert 'rtsp://' not in client.get('/live').text


def test_live_resource_pressure_drops_optional_work(live):
    manager,_,processes=live;manager.session('cam01');manager.pressure=lambda:True;manager.reap();assert not manager.pipelines and processes[0].code==0
    with pytest.raises(HTTPException) as e:manager.session('cam02')
    assert e.value.status_code==429


def test_credentials_only_in_stdin(monkeypatch,tmp_path,caplog):
    import io
    import app.live_engine as module
    captured={}
    class Input(io.BytesIO):
        def close(self):captured['input']=self.getvalue();super().close()
    class Fake:
        stdin=Input();pid=12345
        def poll(self):return None
    def spawn(args,**kwargs):captured['args']=args;captured['environment']=kwargs['env'];return Fake()
    monkeypatch.setenv('HOME_PUBLIC_IP','198.51.100.1');monkeypatch.setenv('CAM_USER','fixture-user');monkeypatch.setenv('CAM_PASS','fixture-private-password')
    monkeypatch.setattr(module.subprocess,'Popen',spawn)
    LiveManager(tmp_path).spawn('cam01',tmp_path)
    assert 'fixture-private-password' not in str(captured['args'])
    assert 'fixture-private-password' not in str(captured['environment'])
    assert b'fixture-private-password' in captured['input']
    assert 'fixture-private-password' not in caplog.text


@pytest.mark.parametrize('path',[
 '/api/live/cam09/sessions',
 '/api/live/cam01/sessions/malformed',
 '/live-media/cam01/../bad/index.m3u8',
 '/live-media/cam01/'+('a'*32)+'/'+('b'*32)+'/secret',
 '/live-media/cam09/'+('a'*32)+'/'+('b'*32)+'/index.m3u8'])
def test_web_live_validation_before_private_service(archive,path):
 client=TestClient(create_app(archive.settings,PendingPlayback()))
 result=client.post(path) if path.startswith('/api/') else client.get(path)
 assert result.status_code==404
 assert 'rtsp://' not in result.text


def test_timeline_lists_sources_once(archive,monkeypatch):
    calls=[]
    original=archive.entries
    def entries(fd):calls.append(1);return original(fd)
    monkeypatch.setattr(archive,'entries',entries)
    class Provider:
        def known_duration(self,cam,recording,source_identity):
            assert len(source_identity)==5
            return None
    coverage(archive,Provider(),'cam01','2026-10-01')
    assert len(calls)==1


def test_midnight_continuity_and_offset(archive):
    folder=archive.settings.recordings_root/'cam01'
    p=folder/'2026-09-30_23-58-00.mkv';p.write_bytes(b'fixture');os.utime(p,(time.time()-600,time.time()-600))
    p=folder/'2026-10-01_00-03-00.mkv';p.write_bytes(b'fixture');os.utime(p,(time.time()-600,time.time()-600))
    data=coverage(archive,PendingPlayback(),'cam01','2026-10-01')
    segment,offset=locate(data,'00:01:00')
    assert segment['recording']=='2026-09-30_23-58-00.mkv' and offset==180
    assert data['intervals'][0]['start']==0
    assert following(data,segment['recording'],300)['start']==180


def test_live_stale_playlist_reports_offline(live):
    manager,_,processes=live;session=manager.session('cam01')
    file=manager.pipelines['cam01']['folder']/'index.m3u8';os.utime(file,(time.time()-30,time.time()-30))
    assert manager.state('cam01',session['lease'])['state']=='error'
    assert processes[0].code==0


def test_live_csp_limits_media_and_connections_to_same_origin(archive):
    response=TestClient(create_app(archive.settings,PendingPlayback())).get('/live')
    policy=response.headers['Content-Security-Policy']
    assert "media-src 'self' blob:;" in policy
    assert "connect-src 'self';" in policy
    assert "script-src 'self';" in policy


def test_default_camera_names_are_configurable_home_names(tmp_path,monkeypatch):
    assert Settings().camera_names=={cam:f'Acasă {i}' for i,cam in enumerate(CAMERAS,1)}
    monkeypatch.setenv('CCTV_CAMERA_NAMES',str(tmp_path/'absent.json'))
    assert Settings.from_env().camera_names==Settings().camera_names

@pytest.mark.parametrize('start,end', [('20:00:00','20:11:00'),('00:00:00','23:59:59')])
def test_playback_timeframe(archive,start,end):
 response=TestClient(create_app(archive.settings,PendingPlayback())).get('/playback',params={'cam':'cam03','date':'2026-10-01','start':start,'end':end})
 assert response.status_code==200
 assert f'data-range-start="{start}"' in response.text and f'data-range-end="{end}"' in response.text
 assert 'Play range' in response.text and 'Export clip' in response.text

@pytest.mark.parametrize('params', [{'start':'20:00:00'}, {'end':'20:11:00'}, {'start':'20:11:00','end':'20:00:00'}, {'start':'20:00:00','end':'20:00:00'}, {'start':'bad','end':'20:11:00'}, {'start':'20:00:00','end':'24:00:00'}])
def test_playback_invalid_timeframe(archive,params):
 assert TestClient(create_app(archive.settings,PendingPlayback())).get('/playback',params=params).status_code==400


def test_eight_simultaneous_live_pipelines(live):
    manager,clock,processes=live
    for i in range(1,9):
        clock.now += .3
        assert manager.session(f'cam{i:02}')['state']=='live'
    assert len(processes)==8


def test_failed_pipeline_replacement_reclaims_all_old_viewers(live):
 manager,_,processes=live
 old=[manager.session('cam01') for _ in range(64)]
 processes[0].code=1
 new=manager.session('cam01')
 assert new['generation']!=old[0]['generation']
 assert len(manager.leases)==1
 assert len(processes)==2
 assert manager.state('cam01',new['lease'])['state']=='live'
 with pytest.raises(HTTPException):manager.state('cam01',old[0]['lease'])


def test_real_viewer_limit_remains_enforced(live):
 manager,_,processes=live
 for _ in range(64):manager.session('cam01')
 with pytest.raises(HTTPException) as error:manager.session('cam01')
 assert error.value.status_code==429 and len(processes)==1


def test_existing_camera_does_not_wait_for_startup_spacing(live):
 manager,clock,_=live
 manager.session('cam01');manager.session('cam01')
 assert clock.now==0


def test_expired_viewers_free_capacity_without_restarting_camera(live):
 manager,clock,processes=live
 for _ in range(64):manager.session('cam01')
 clock.now=21
 new=manager.session('cam01')
 assert len(manager.leases)==1 and len(processes)==1
 assert new['state']=='live'
