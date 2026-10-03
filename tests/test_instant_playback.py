import os
import time
from dataclasses import replace
import pytest
from fastapi import HTTPException
from app.config import Settings
from app.instant_playback import InstantPlayback

class Process:
    code = None
    def poll(self): return self.code

@pytest.fixture
def streaming(tmp_path):
    source = tmp_path/'recordings'/'cam01'
    source.mkdir(parents=True)
    for name in ['2026-10-01_10-00-00.mkv', '2026-10-01_10-05-00.mkv']:
        file = source/name
        file.write_bytes(b'original')
        os.utime(file, (time.time()-600, time.time()-600))
    calls=[]
    def factory(fd, folder, offset, duration):
        assert os.read(fd, 8)==b'original'
        calls.append((offset,duration))
        (folder/'index.m3u8').write_text('#EXTM3U\nseg00000.ts\n')
        (folder/'seg00000.ts').write_bytes(b'video')
        return Process()
    manager = InstantPlayback(Settings(recordings_root=source.parent, cache_root=tmp_path/'cache'), factory)
    manager.terminate=lambda process:setattr(process,'code',0)
    yield manager,source,calls
    manager.close()

def test_seek_starts_at_requested_offset_and_releases(streaming):
    manager,source,calls=streaming
    result=manager.prepare('cam01','2026-10-01_10-00-00.mkv',123,300)
    assert result['state']=='ready' and result['duration']==177
    assert calls==[(123,177)]
    fd,size=manager.open_media(result['stream_id'],'seg00000.ts')
    assert os.read(fd,size)==b'video'
    os.close(fd)
    manager.release(result['stream_id'])
    assert not manager.sessions and not list(manager.root.iterdir())
    assert (source/'2026-10-01_10-00-00.mkv').read_bytes()==b'original'

def test_deleted_source_revokes_session(streaming):
    manager,source,_=streaming
    result=manager.prepare('cam01','2026-10-01_10-00-00.mkv',0,300)
    (source/'2026-10-01_10-00-00.mkv').unlink()
    with pytest.raises(HTTPException):manager.open_media(result['stream_id'],'seg00000.ts')
    assert not manager.sessions

def test_capacity_frees_on_release(streaming):
    manager,_,_=streaming
    one=manager.prepare('cam01','2026-10-01_10-00-00.mkv',0,300)
    two=manager.prepare('cam01','2026-10-01_10-00-00.mkv',0,300)
    with pytest.raises(HTTPException) as error:manager.prepare('cam01','2026-10-01_10-00-00.mkv',0,300)
    assert error.value.status_code==429
    manager.release(one['stream_id'])
    assert manager.prepare('cam01','2026-10-01_10-00-00.mkv',0,300)['state']=='ready'

@pytest.mark.parametrize('name',['../index.m3u8','index.m3u8.tmp','seg00000.ts/','secret'])
def test_media_names_restricted(streaming,name):
    manager,_,_=streaming
    result=manager.prepare('cam01','2026-10-01_10-00-00.mkv',0,300)
    with pytest.raises(HTTPException):manager.open_media(result['stream_id'],name)

def test_media_symlink_rejected(streaming):
    manager,source,_=streaming
    result=manager.prepare('cam01','2026-10-01_10-00-00.mkv',0,300)
    file=manager.root/result['stream_id']/'seg00000.ts'
    file.unlink();file.symlink_to(source/'2026-10-01_10-00-00.mkv')
    with pytest.raises(HTTPException):manager.open_media(result['stream_id'],file.name)

@pytest.mark.parametrize('offset,duration',[(300,300),(-1,300),(float('nan'),300),(0,1000)])
def test_offsets_bounded(streaming,offset,duration):
    with pytest.raises(HTTPException):streaming[0].prepare('cam01','2026-10-01_10-00-00.mkv',offset,duration)


def test_abandoned_session_releases_capacity(streaming):
    manager,_,_=streaming
    now=[0]
    manager.clock=lambda:now[0]
    result=manager.prepare('cam01','2026-10-01_10-00-00.mkv',0,300)
    now[0]=61
    manager.reap()
    assert not manager.sessions and not list(manager.root.iterdir())


def test_streaming_seek_bypasses_whole_file_conversion(streaming,monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    manager,_,calls=streaming
    monkeypatch.setattr('app.main.InstantPlayback',lambda settings:manager)
    class Provider:
        def known_duration(self,*args):return 300
        def recording_duration(self,*args):return 300
        def prepare(self,*args):raise AssertionError('Whole-file conversion must not run')
    with TestClient(create_app(manager.settings,Provider())) as client:
        result=client.post('/api/timeline/cam01/seek?date=2026-10-01&time=10:02:03&stream=true')
        assert result.status_code==200
        data=result.json()
        assert data['segment_start']==36123 and data['offset']==0 and data['duration']==177
        assert calls==[(123,177)]
        assert client.get(data['media_url']).text.startswith('#EXTM3U')
        assert client.head(data['media_url']).headers['content-length']==str(len('#EXTM3U\nseg00000.ts\n'))
        assert client.delete('/api/instant/'+data['stream_id']).status_code==200
        assert client.get(data['media_url']).status_code==404
