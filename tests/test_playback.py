import concurrent.futures
import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.config import Settings,CAMERAS
from app.converter import ConversionError,FFmpegConverter,arguments
from app.main import create_app
from app.playback import PlaybackManager

OLD='2026-10-01_10-00-00.mkv'
NEXT='2026-10-01_11-00-00.mkv'
DATA=b'abcdefghij'*100

class FakeConverter:
    def __init__(self,blocked=False,fail=False):
        self.gate=threading.Event();self.entered=threading.Event()
        if not blocked:self.gate.set()
        self.calls=0;self.fail=fail
        self.active=0;self.max_active=0
        self.lock=threading.Lock()
    def convert(self,source_fd,output_fd,mode,limit):
        with self.lock:
            self.calls+=1;self.active+=1;self.max_active=max(self.max_active,self.active)
        self.entered.set()
        try:
            assert self.gate.wait(5)
            os.write(output_fd,DATA)
            if self.fail:raise ConversionError('private internal failure /secret')
            return {'duration':300,'video_codec':'h264' if mode=='h264' else 'hevc','audio_codec':'aac','width':1280,'height':720}
        finally:
            with self.lock:self.active-=1
    def cancel(self,force=False):self.gate.set()

@pytest.fixture
def settings(tmp_path):
    root=tmp_path/'recordings';root.mkdir()
    for cam in CAMERAS:
        folder=root/cam;folder.mkdir()
        for name in [OLD,NEXT,'2026-10-01_12-00-00.mkv','2026-10-01_13-00-00.mkv']:
            file=folder/name;file.write_bytes(b'source')
            os.utime(file,(time.time()-600,time.time()-600))
    return Settings(recordings_root=root,cache_root=tmp_path/'cache')

def wait(manager,key,state='ready'):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        result=manager.status(key)
        if result['state']==state:return result
        if result['state']=='failed' and state!='failed':pytest.fail(str(result))
        time.sleep(.01)
    pytest.fail('Job did not reach '+state)

@pytest.fixture
def manager(settings):
    manager=PlaybackManager(settings,FakeConverter())
    yield manager
    manager.close()

def test_uncached_success_cache_hit(manager):
    result=manager.prepare('cam01',OLD)
    assert result['state']=='queued'
    ready=wait(manager,result['job_id'])
    assert ready['media_url'].startswith('/media/playback/')
    assert manager.prepare('cam01',OLD)['cached'] is True
    assert manager.converter.calls==1
    assert not list(manager.settings.cache_root.glob('*.part'))
    fd,s,_=manager.open_media(result['job_id'])
    assert os.read(fd,s.st_size)==DATA;os.close(fd)

def test_simultaneous_duplicates(settings):
    fake=FakeConverter(blocked=True);manager=PlaybackManager(settings,fake)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results=list(executor.map(lambda _:manager.prepare('cam01',OLD),range(8)))
        assert len({r['job_id'] for r in results})==1
        assert fake.entered.wait(2)
        assert fake.calls==1
        fake.gate.set();wait(manager,results[0]['job_id'])
        assert fake.max_active==1
    finally:manager.close()

def test_bounded_queue_and_single_worker(settings):
    fake=FakeConverter(blocked=True)
    manager=PlaybackManager(replace(settings,playback_queue_size=1),fake)
    try:
        first=manager.prepare('cam01',OLD);assert fake.entered.wait(2)
        second=manager.prepare('cam02',OLD);assert second['state']=='queued'
        assert manager.prepare('cam03',OLD)['state']=='busy'
        fake.gate.set();wait(manager,first['job_id']);wait(manager,second['job_id'])
        assert fake.calls==2 and fake.max_active==1
    finally:manager.close()

def test_failure_removes_temporary_output(settings):
    fake=FakeConverter(fail=True);manager=PlaybackManager(settings,fake)
    try:
        result=manager.prepare('cam01',OLD)
        failure=wait(manager,result['job_id'],'failed')
        assert '/secret' not in json.dumps(failure)
        assert not list(settings.cache_root.glob('*.part'))
        assert not list(settings.cache_root.glob('*.mp4'))
        assert not list(settings.cache_root.glob('*.json'))
        with pytest.raises(HTTPException):manager.open_media(result['job_id'])
    finally:manager.close()

@pytest.mark.parametrize('cam,name',[('cam09',OLD),('cam01','../secret'),('cam01','..\\secret'),('cam01','invalid.mkv'),('cam01','2026-10-01_20-00-00.mkv')])
def test_invalid_source(manager,cam,name):
    with pytest.raises(HTTPException):manager.prepare(cam,name)
    assert manager.converter.calls==0

def test_invalid_format(manager):
    with pytest.raises(HTTPException):manager.prepare('cam01',OLD,'../other')

@pytest.mark.parametrize('operation',['remove','change'])
def test_source_disappears_or_changes_before_conversion(settings,operation):
    fake=FakeConverter(blocked=True);manager=PlaybackManager(settings,fake)
    try:
        first=manager.prepare('cam01',OLD);assert fake.entered.wait(2)
        second=manager.prepare('cam02',OLD)
        file=settings.recordings_root/'cam02'/OLD
        if operation=='remove':file.unlink()
        else:
            file.write_bytes(b'changed');os.utime(file,(time.time()-600,time.time()-600))
        fake.gate.set();wait(manager,first['job_id']);wait(manager,second['job_id'],'failed')
        assert fake.calls==1
    finally:manager.close()

def test_source_changes_during_conversion(settings):
    fake=FakeConverter(blocked=True);manager=PlaybackManager(settings,fake)
    try:
        result=manager.prepare('cam01',OLD);assert fake.entered.wait(2)
        file=settings.recordings_root/'cam01'/OLD;file.write_bytes(b'changed')
        fake.gate.set();wait(manager,result['job_id'],'failed')
        assert not list(settings.cache_root.glob('*.mp4'))
    finally:manager.close()

def test_cache_identity_invalidated(manager):
    first=manager.prepare('cam01',OLD);wait(manager,first['job_id'])
    file=manager.settings.recordings_root/'cam01'/OLD
    file.write_bytes(b'new source');os.utime(file,(time.time()-600,time.time()-600))
    assert manager.status(first['job_id'])['state']=='failed'
    second=manager.prepare('cam01',OLD)
    assert first['job_id']!=second['job_id']
    wait(manager,second['job_id']);assert manager.converter.calls==2

def test_original_retention_removes_cached_access(manager):
    result=manager.prepare('cam01',OLD);wait(manager,result['job_id'])
    (manager.settings.recordings_root/'cam01'/OLD).unlink()
    with pytest.raises(HTTPException):manager.open_media(result['job_id'])
    assert not list(manager.settings.cache_root.glob('*.mp4'))

def test_persistent_cache_and_exclusive_worker(settings):
    first=PlaybackManager(settings,FakeConverter());second=PlaybackManager(settings,FakeConverter())
    try:
        result=first.prepare('cam01',OLD);wait(first,result['job_id'])
        with pytest.raises(BlockingIOError):second.start()
        first.close();second.start()
        hit=second.prepare('cam01',OLD)
        assert hit['cached'] is True and second.converter.calls==0
    finally:
        if first.fd is not None:first.close()
        second.close()

def test_cleanup_age_size_orphans_symlinks(settings):
    manager=PlaybackManager(settings,FakeConverter())
    try:
        result=manager.prepare('cam01',OLD);wait(manager,result['job_id'])
        source=settings.recordings_root/'cam01'/OLD
        before=source.stat();content=source.read_bytes()
        orphan='a'*64
        (settings.cache_root/(orphan+'.part')).write_text('interrupted')
        (settings.cache_root/('b'*64+'.mp4')).symlink_to(source)
        (settings.cache_root/'unrelated').symlink_to(settings.recordings_root,target_is_directory=True)
        manager.cleanup()
        assert not (settings.cache_root/(orphan+'.part')).exists()
        assert not (settings.cache_root/('b'*64+'.mp4')).is_symlink()
        assert (settings.cache_root/'unrelated').is_symlink()
        meta_path=settings.cache_root/(result['job_id']+'.json')
        meta=json.loads(meta_path.read_text());meta['created']=time.time()-settings.cache_max_age-1
        meta_path.write_text(json.dumps(meta));manager.cleanup()
        assert not list(settings.cache_root.glob('*.mp4'))
        after=source.stat()
        assert source.read_bytes()==content
        assert (before.st_size,before.st_mtime_ns,before.st_ctime_ns)==(after.st_size,after.st_mtime_ns,after.st_ctime_ns)
    finally:manager.close()

def test_cache_size_budget(settings):
    manager=PlaybackManager(settings,FakeConverter())
    try:
        first=manager.prepare('cam01',OLD);wait(manager,first['job_id'])
        second=manager.prepare('cam02',OLD);wait(manager,second['job_id'])
        manager.settings=replace(settings,cache_max_bytes=1500)
        manager.cleanup()
        assert sum(p.stat().st_size for p in settings.cache_root.iterdir() if p.name.endswith(('.mp4','.json')))<=1500
    finally:manager.close()

@pytest.mark.parametrize('root', ['/srv/cctv','/srv','/','/srv/cctv/cache'])
def test_cache_cannot_overlap_originals(root):
    with pytest.raises(ValueError):Settings(cache_root=Path(root))

def test_api_media_ranges_and_head(manager):
    with TestClient(create_app(manager.settings,manager)) as client:
        response=client.post('/api/playback/cam01/'+OLD)
        assert response.status_code==202
        ready=wait(manager,response.json()['job_id'])
        response=client.post('/api/playback/cam01/'+OLD)
        assert response.status_code==200 and response.json()['cached']
        url=ready['media_url']
        assert client.get(url).content==DATA
        for value,expected in [('bytes=2-5',DATA[2:6]),('bytes=-4',DATA[-4:]),('bytes=990-',DATA[990:])]:
            r=client.get(url,headers={'Range':value})
            assert r.status_code==206 and r.content==expected
        assert client.head(url).headers['content-length']==str(len(DATA))
        assert client.head(url).content==b''
        for value in ['bytes=1001-','bytes=5-2','bytes=-0','bytes=1-2,4-5','garbage']:
            assert client.get(url,headers={'Range':value}).status_code==416
        assert client.get('/media/playback/..%2Fetc%2Fpasswd').status_code==404
        assert client.get('/media/playback/'+'0'*64).status_code==404

def test_api_busy_response(settings):
    fake=FakeConverter(blocked=True);manager=PlaybackManager(replace(settings,playback_queue_size=1),fake)
    with TestClient(create_app(manager.settings,manager)) as client:
        client.post('/api/playback/cam01/'+OLD);assert fake.entered.wait(2)
        client.post('/api/playback/cam02/'+OLD)
        response=client.post('/api/playback/cam03/'+OLD)
        assert response.status_code==429 and response.headers['retry-after']=='8'

@pytest.mark.parametrize('mode',['h264','hevc'])
def test_structured_ffmpeg_arguments(mode):
    args=arguments(11,12,mode,1000000)
    assert isinstance(args,list)
    assert '/proc/self/fd/11' in args and args[-1]=='/proc/self/fd/12'
    assert '-f' in args and 'matroska' in args
    assert '-movflags' in args and '+faststart' in args
    assert ('hvc1' in args)==(mode=='hevc')

def test_converter_validation(settings,monkeypatch):
    converter=FFmpegConverter()
    source_probe={'format':{'duration':'300'},'streams':[{'codec_type':'video','codec_name':'hevc','width':2304,'height':1296},{'codec_type':'audio','codec_name':'pcm_mulaw'}]}
    output_probe={'format':{'duration':'300'},'streams':[{'codec_type':'video','codec_name':'h264','width':1280,'height':720,'pix_fmt':'yuv420p'},{'codec_type':'audio','codec_name':'aac'}]}
    source=os.open(settings.recordings_root/'cam01'/OLD,os.O_RDONLY)
    output=os.open(settings.cache_root.parent/'output',os.O_RDWR|os.O_CREAT,0o600)
    try:
        monkeypatch.setattr(converter,'probe',lambda fd,source=False:source_probe if source else output_probe)
        monkeypatch.setattr(converter,'execute',lambda *args,**kwargs:os.write(output,b'mp4'))
        assert converter.convert(source,output,'h264',1000)['video_codec']=='h264'
        output_probe['streams'][0]['codec_name']='wrong'
        with pytest.raises(ConversionError):converter.convert(source,output,'h264',1000)
        output_probe['streams'][0]['codec_name']='h264';output_probe['format']['duration']='100'
        with pytest.raises(ConversionError):converter.convert(source,output,'h264',1000)
    finally:os.close(source);os.close(output)

def test_cleanup_startup_interrupted_outputs(settings):
    settings.cache_root.mkdir()
    for name in ['a'*64+'.part','b'*64+'.json.part','c'*64+'.mp4']:
        (settings.cache_root/name).write_bytes(b'partial')
    manager=PlaybackManager(settings,FakeConverter())
    try:
        manager.start()
        assert not list(settings.cache_root.glob('*.part'))
        assert not list(settings.cache_root.glob('*.mp4'))
    finally:manager.close()

def test_cache_root_cannot_redirect_into_sources(settings):
    settings.cache_root.symlink_to(settings.recordings_root,target_is_directory=True)
    manager=PlaybackManager(settings,FakeConverter())
    with pytest.raises(ValueError):manager.start()
    assert not (settings.recordings_root/'.worker.lock').exists()

def test_cache_symlink_parent_rejected(settings,tmp_path):
    link=tmp_path/'linked-parent';link.symlink_to(tmp_path,target_is_directory=True)
    manager=PlaybackManager(replace(settings,cache_root=link/'another-cache'),FakeConverter())
    with pytest.raises(OSError):manager.start()
    assert not (tmp_path/'another-cache').exists()

def test_cache_broken_output_not_reused(manager):
    result=manager.prepare('cam01',OLD);wait(manager,result['job_id'])
    file=manager.settings.cache_root/(result['job_id']+'.mp4')
    file.write_bytes(b'corrupted')
    assert manager.status(result['job_id'])['state']=='failed'
    second=manager.prepare('cam01',OLD);wait(manager,second['job_id'])
    assert manager.converter.calls==2

def test_shutdown_discards_interrupted_job(settings):
    class Interrupted(FakeConverter):
        def convert(self,source_fd,output_fd,mode,limit):
            os.write(output_fd,b'partial');self.entered.set();self.gate.wait(5)
            raise ConversionError('interrupted')
    fake=Interrupted(blocked=True);manager=PlaybackManager(settings,fake)
    result=manager.prepare('cam01',OLD);assert fake.entered.wait(2)
    manager.close()
    assert manager.jobs[result['job_id']].state=='failed'
    assert not list(settings.cache_root.glob('*.part'))
    assert not list(settings.cache_root.glob('*.mp4'))

def test_format_keys_separate_and_share_worker(manager):
    h264=manager.prepare('cam01',OLD,'h264');hevc=manager.prepare('cam01',OLD,'hevc')
    assert h264['job_id']!=hevc['job_id']
    wait(manager,h264['job_id']);wait(manager,hevc['job_id'])
    assert manager.converter.max_active==1

def test_lifespan_without_playback_requests_does_not_convert(settings):
    fake=FakeConverter();manager=PlaybackManager(settings,fake)
    with TestClient(create_app(settings,manager)) as client:
        assert client.get('/').status_code==200
        assert client.get('/camera/cam01?date=2026-10-01').status_code==200
        assert fake.calls==0

@pytest.mark.parametrize('failure_at',['start','body'])
def test_stream_closes_file_on_disconnect(failure_at):
    import asyncio
    import io
    from starlette.requests import ClientDisconnect
    from app.media import SafeStreamingResponse
    file=io.BytesIO(b'footage')
    response=SafeStreamingResponse(iter([b'footage']),file=file)
    async def send(message):
        if message['type']=='http.response.'+failure_at:raise OSError('disconnected')
    async def receive():return {'type':'http.disconnect'}
    with pytest.raises(ClientDisconnect):
        asyncio.run(response({'type':'http','asgi':{'spec_version':'2.4'}},receive,send))
    assert file.closed


def test_nginx_published_media_read_only_group_access(settings):
    configured=replace(settings,download_mode='nginx')
    service=PlaybackManager(configured,FakeConverter())
    try:
        job=service.prepare('cam01',OLD)
        wait(service,job['job_id'])
        media=configured.cache_root/(job['job_id']+'.mp4')
        sidecar=configured.cache_root/(job['job_id']+'.json')
        assert media.stat().st_mode & 0o777 == 0o640
        assert sidecar.stat().st_mode & 0o777 == 0o600
        assert service.prepare('cam01',OLD)['cached']
        from fastapi.testclient import TestClient
        client=TestClient(create_app(configured,service))
        response=client.head('/media/playback/'+job['job_id'])
        assert response.status_code==200 and not response.content
        assert response.headers['x-accel-redirect']=='/_cctv_playback/'+job['job_id']+'.mp4'
    finally: service.close()


def test_prefetch_never_joins_active_queue(settings):
    converter=FakeConverter(blocked=True);service=PlaybackManager(settings,converter)
    try:
        requested=service.prepare('cam01',OLD)
        assert converter.entered.wait(2)
        assert service.prefetch('cam01',NEXT)['state']=='deferred'
        converter.gate.set();wait(service,requested['job_id'])
        next_job=service.prefetch('cam01',NEXT);wait(service,next_job['job_id'])
        assert service.prepare('cam01',NEXT)['cached']
    finally:service.close()


def test_requested_seek_cancels_obsolete_prefetch(settings):
    converter=FakeConverter(blocked=True);service=PlaybackManager(settings,converter)
    try:
        optional=service.prefetch('cam01',OLD);assert converter.entered.wait(2)
        requested=service.prepare('cam01',NEXT)
        wait(service,requested['job_id'])
        assert service.status(optional['job_id'])['state']=='failed'
        assert not (settings.cache_root/(optional['job_id']+'.mp4')).exists()
    finally:service.close()
