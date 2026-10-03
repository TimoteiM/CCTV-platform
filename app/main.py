import os
import logging
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from .config import CAMERAS, Settings
from .recordings import Store, camera, day
from .playback import PlaybackManager
from .timeline import coverage,locate,following,second
from .live_client import LiveClient
from .instant_playback import InstantPlayback
from .person_events import PersonEvents
from .media import media_response, SafeStreamingResponse

BASE = Path(__file__).parent
REQUEST_LOG = logging.getLogger('cctv.request')

def request_category(path):
    for prefix, category in (
        ('/api/live/', 'live-control'), ('/live-media/', 'live-media'), ('/api/timeline/', 'timeline'),
        ('/api/playback/status/', 'playback-status'), ('/api/playback/', 'playback-prepare'),
        ('/media/playback/', 'playback-media'), ('/download/', 'original-download'),
        ('/api/camera/', 'recording-list'), ('/camera/', 'camera-page'), ('/static/', 'static')):
        if path.startswith(prefix): return category
    return 'dashboard' if path == '/' else 'other'


def create_app(settings=None, playback=None):
    settings = settings or Settings.from_env()
    store = Store(settings)
    provider = playback or PlaybackManager(settings)
    instant = InstantPlayback(settings)
    person_events = PersonEvents(settings.person_events_root, settings.frigate_events_root)
    @asynccontextmanager
    async def lifespan(app):
        if hasattr(provider, "start"): provider.start()
        try: yield
        finally:
            instant.close()
            if hasattr(provider, "close"): provider.close()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, debug=False, lifespan=lifespan)
    templates = Jinja2Templates(directory=BASE / 'templates')
    templates.env.filters['filesize'] = lambda n: f'{n / 1024 / 1024:.1f} MB'
    app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')

    @app.middleware('http')
    async def security(request, call_next):
        category = request_category(request.url.path)
        method = request.method if request.method in ('GET', 'POST', 'HEAD') else 'other'
        # Authentication belongs to nginx. No client identity headers are trusted here.
        length = request.headers.get('content-length', '0')
        if not length.isdecimal() or int(length) > 4096 or request.headers.get('transfer-encoding'):
            REQUEST_LOG.info('Request method=%s category=%s status=413', method, category)
            return JSONResponse({'detail': 'Request body not allowed'}, status_code=413)
        response = await call_next(request)
        response.headers.update({'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer', 'Cache-Control': 'no-store', 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        REQUEST_LOG.info('Request method=%s category=%s status=%s', method, category, response.status_code)
        return response

    @app.exception_handler(Exception)
    async def safe_error(request, exc):
        return JSONResponse({'detail': 'Request could not be completed'}, status_code=500)

    def context(request):
        return dict(request=request, cameras=settings.camera_names)

    @app.get('/', response_class=HTMLResponse)
    def dashboard(request: Request):
        return templates.TemplateResponse(request=request, name='live.html', context=context(request))

    @app.get('/camera/{cam}', response_class=HTMLResponse)
    def camera_page(request: Request, cam: str, date: str | None = None):
        camera(cam)
        today = datetime.now(ZoneInfo('Europe/Bucharest')).date()
        selected = day(date) if date is not None else today
        records = store.list(cam, selected)
        previous = (selected - timedelta(days=1)).isoformat() if selected.year > 1 or selected.timetuple().tm_yday > 1 else None
        following = (selected + timedelta(days=1)).isoformat() if selected.isoformat() != '9999-12-31' else None
        return templates.TemplateResponse(request=request, name='camera.html', context={**context(request), 'cam': cam, 'selected': selected.isoformat(), 'previous': previous, 'following': following, 'records': records})

    @app.get('/api/camera/{cam}/recordings')
    def listing(cam: str, date: str):
        return {'camera': camera(cam), 'date': day(date).isoformat(), 'recordings': store.list(cam, day(date))}

    @app.api_route('/download/{cam}/{recording}', methods=['GET', 'HEAD'])
    def download(request: Request, cam: str, recording: str):
        handle, s = store.open(cam, recording)
        headers = {'Content-Disposition': f'attachment; filename="{cam}_{recording}"'}
        if settings.download_mode == 'nginx':
            os.close(handle)
            headers['X-Accel-Redirect'] = f'/_cctv_originals/{cam}/{recording}'
            return Response(headers=headers, media_type='video/x-matroska')
        if request.method == 'HEAD':
            os.close(handle)
            return Response(headers={**headers, 'Content-Length': str(s.st_size)}, media_type='video/x-matroska')
        # Own the validated FD; path is never reopened after validation.
        file = os.fdopen(handle, 'rb')
        def chunks():
            try:
                remaining = s.st_size
                while remaining:
                    block = file.read(min(256 * 1024, remaining))
                    if not block: break
                    remaining -= len(block)
                    yield block
            finally:
                file.close()
        headers['Content-Length'] = str(s.st_size)
        return SafeStreamingResponse(chunks(), file=file, headers=headers, media_type='video/x-matroska')

    @app.post('/api/playback/{cam}/{recording}')
    def prepare(cam: str, recording: str, format: str = "h264"):
        handle, _ = store.open(cam, recording)
        os.close(handle)
        result = provider.prepare(cam, recording, format)
        code = {"ready": 200, "busy": 429, "unavailable": 503}.get(result["state"], 202)
        headers = {"Retry-After": "8"} if code == 429 else {}
        return JSONResponse(result, status_code=code, headers=headers)

    @app.get('/api/playback/status/{job}')
    def playback_status(job: str):
        return provider.status(job)

    @app.api_route('/media/playback/{job}', methods=['GET', 'HEAD'])
    def playback_media(request: Request, job: str):
        handle, s, name = provider.open_media(job)
        if settings.download_mode == 'nginx':
            os.close(handle)
            return Response(headers={'X-Accel-Redirect': f'/_cctv_playback/{job}.mp4', 'Content-Disposition': f'inline; filename="{name}"'}, media_type='video/mp4')
        return media_response(request, handle, s.st_size, name)

    @app.api_route('/api/instant/{token}', methods=['POST', 'DELETE'])
    def instant_status(request: Request, token: str):
        return instant.release(token) if request.method == 'DELETE' else instant.state(token)

    @app.api_route('/instant-media/{token}/{name}', methods=['GET', 'HEAD'])
    def instant_media(request: Request, token: str, name: str):
        fd, size = instant.open_media(token, name)
        with os.fdopen(fd, 'rb') as file:
            content = file.read(size) if request.method == 'GET' else b''
        return Response(content, headers={'Content-Length': str(size)},
                        media_type='application/vnd.apple.mpegurl' if name == 'index.m3u8' else 'video/mp2t')

    live=LiveClient()
    @app.get('/live',response_class=HTMLResponse)
    def live_page(request:Request):
        return templates.TemplateResponse(request=request,name='live.html',context=context(request))

    @app.get('/playback',response_class=HTMLResponse)
    def playback_page(request:Request,cam:str='cam01',date:str|None=None,time:str|None=None,t:str|None=None,start:str|None=None,end:str|None=None):
        camera(cam);selected=day(date) if date else datetime.now(ZoneInfo('Europe/Bucharest')).date()
        chosen=t or time
        if chosen:second(chosen)
        if (start is None)!=(end is None):raise HTTPException(400,'Choose both start and end times')
        if start is not None and second(start)>=second(end):raise HTTPException(400,'End time must be after start time on the selected date')
        if start is not None and (not chosen or not second(start)<=second(chosen)<=second(end)):chosen=start
        return templates.TemplateResponse(request=request,name='nvr_playback.html',context={**context(request),'cam':cam,'selected':selected.isoformat(),'selected_time':chosen or '', 'range_start':start,'range_end':end})

    @app.get('/api/camera/{cam}/calendar')
    def calendar(cam:str):
        with store.directory(cam) as fd:
            entries=store.entries(fd);newest=entries[-1][0] if entries else None
            rows=[(name, stamp) for name,stamp,info in entries if store.eligible((name,stamp,info),newest)]
        latest=None
        if rows:
            duration=provider.recording_duration(cam,rows[-1][0]) if hasattr(provider,'recording_duration') else None
            point=rows[-1][1]+timedelta(seconds=max(0,(duration or 30)-30))
            latest={'date':point.date().isoformat(),'time':point.strftime('%H:%M:%S')}
        return {'camera':cam,'days':sorted({stamp.date().isoformat() for _,stamp in rows}), 'latest':latest}

    thumbnail_cache={}
    thumbnail_gate=__import__('threading').BoundedSemaphore(2)
    @app.get('/api/camera/{cam}/thumbnail')
    def camera_thumbnail(cam:str):
        import subprocess
        from .playback import identity
        with store.directory(cam) as directory:
            entries=store.entries(directory);newest=entries[-1][0] if entries else None
            eligible=[row for row in entries if store.eligible(row,newest)]
        if not eligible:raise HTTPException(404,'No recordings')
        recording=eligible[-1][0];fd,info=store.open(cam,recording)
        try:
            key=(recording,identity(info))
            if cam in thumbnail_cache and thumbnail_cache[cam][0]==key:
                return Response(thumbnail_cache[cam][1],media_type='image/jpeg')
            if not thumbnail_gate.acquire(timeout=15):raise HTTPException(503,'Preview loading')
            try:
                result=subprocess.run(['/usr/bin/nice','-n','15','/usr/bin/ffmpeg','-nostdin','-loglevel','error','-threads','1','-i',f'/proc/self/fd/{fd}','-frames:v','1','-vf','scale=240:-2','-threads','1','-f','image2pipe','-vcodec','mjpeg','pipe:1'],pass_fds=(fd,),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=15)
                if result.returncode or not result.stdout:raise HTTPException(503,'Preview unavailable')
                thumbnail_cache[cam]=(key,result.stdout)
                return Response(result.stdout,media_type='image/jpeg')
            except subprocess.TimeoutExpired:raise HTTPException(503,'Preview unavailable') from None
            finally:thumbnail_gate.release()
        finally:os.close(fd)

    def window_sources(cam, date, value, *, back=10, stop=None, max_window=1200):
        data=coverage(store,provider,cam,date)
        candidate=next((i for i,item in reversed(list(enumerate(data['segments']))) if item['start']<=value),None)
        if candidate is None:raise HTTPException(404,'No recording at this time')
        rows=data['segments'];first=rows[candidate]
        duration=provider.recording_duration(cam,first['recording'])
        if duration is None:raise HTTPException(503,'Unable to read recording duration')
        if value>=first['start']+duration:raise HTTPException(404,'No recording at this time')
        target_recording=first['recording']
        if candidate>0 and value-first['start']<back:
            previous=rows[candidate-1]
            previous_length=provider.recording_duration(cam,previous['recording'])
            if previous_length is not None and abs(previous['start']+previous_length-first['start'])<=2:
                candidate-=1;first=previous
        begin=max(first['start'],value-back);sources=[];total=0;cursor=first['start'];mapping=[]
        for row in rows[candidate:candidate+8]:
            length=provider.recording_duration(cam,row['recording'])
            if length is None:break
            length=min(length,86400-row['start'])
            if length<=0:break
            if sources and abs(row['start']-cursor)>2:break
            if sources and total+length>max_window and row['recording']!=target_recording:break
            sources.append((row['recording'],length));mapping.append({'start':row['start'],'duration':length,'recording':row['recording']})
            total+=length;cursor=row['start']+length
            if stop is not None and cursor>=stop:break
        if stop is not None and cursor<stop-.1:raise HTTPException(400,'The selected range contains a recording gap. Choose a continuous range.')
        return sources,begin-first['start'],begin,mapping

    @app.post('/api/timeline/{cam}/export/validate')
    def validate_export(cam:str,date:str,start:str,end:str):
        begin,finish=second(start),second(end)
        if not 0<finish-begin<=900:raise HTTPException(400,'Choose a clip between one second and 15 minutes')
        window_sources(cam,date,begin,back=0,stop=finish,max_window=2400)
        with instant.lock:
            if len(instant.sessions)>=2:
                return {'state':'busy','message':'Close another playback tab before exporting.'}
        return {'state':'ready'}

    @app.get('/api/timeline/{cam}/export')
    async def export_clip(cam:str,date:str,start:str,end:str):
        import subprocess, shutil
        import anyio
        begin,finish=second(start),second(end)
        if not 0<finish-begin<=900:raise HTTPException(400,'Choose a clip between one second and 15 minutes')
        sources,offset,_,_=await anyio.to_thread.run_sync(lambda:window_sources(cam,date,begin,back=0,stop=finish,max_window=2400))
        opened=[];folder=None;process=None
        # Shares the two-session budget with playback; no unbounded conversion jobs.
        try:
            instant.start()
            with instant.lock:
                if len(instant.sessions)>=2:raise HTTPException(429,'Close another playback tab before exporting')
                token=__import__('secrets').token_hex(16);folder=instant.root/token;folder.mkdir(mode=0o700)
                for recording,length in sources:
                    fd,info=store.open(cam,recording);opened.append((fd,length,recording,info))
                listing=folder/'sources.ffconcat'
                listing.write_text('ffconcat version 1.0\n'+''.join(f"file '/proc/self/fd/{fd}'\nduration {length:.6f}\n" for fd,length,_,_ in opened))
                args=['/usr/bin/nice','-n','10','/usr/bin/ffmpeg','-nostdin','-loglevel','error','-threads','2','-ss',str(offset),'-f','concat','-safe','0','-protocol_whitelist','file,pipe','-i',str(listing),'-t',str(finish-begin),'-map','0:v:0','-map','0:a:0?','-map_metadata','-1','-vf','scale=w=min(1280\\,iw):h=min(720\\,ih):force_original_aspect_ratio=decrease:force_divisible_by=2,fps=20','-c:v','libx264','-preset','veryfast','-crf','26','-threads','2','-filter_threads','1','-pix_fmt','yuv420p','-c:a','aac','-b:a','64k','-ar','48000','-ac','1','-movflags','frag_keyframe+empty_moov','-f','mp4','pipe:1']
                process=subprocess.Popen(args,pass_fds=tuple(item[0] for item in opened),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True)
                from .playback import identity
                instant.sessions[token]=dict(process=process,folder=folder,camera=cam,recording=opened[0][2],identity=identity(opened[0][3]),sources=[(r,identity(i)) for _,_,r,i in opened],seen=instant.clock(),started=instant.clock(),duration=finish-begin)
        except BaseException:
            if process:instant.terminate(process)
            if folder:shutil.rmtree(folder,ignore_errors=True)
            raise
        finally:
            for fd,_,_,_ in opened:os.close(fd)
        async def chunks():
            try:
                while True:
                    block=await anyio.to_thread.run_sync(lambda:process.stdout.read(64*1024))
                    if not block:break
                    with instant.lock:
                        if token in instant.sessions:instant.sessions[token]['seen']=instant.clock()
                    yield block
            finally:
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(lambda:instant.release(token))
                    process.stdout.close()
        return StreamingResponse(chunks(),media_type='video/mp4',headers={'Content-Disposition':f'attachment; filename="{cam}_{date}_{start.replace(":","-")}_{end.replace(":","-")}.mp4"'})

    @app.get('/api/timeline/{cam}')
    def timeline(cam:str,date:str,focus:str|None=None):
        data=coverage(store,provider,cam,date)
        person_events.request(cam,date,second(focus) if focus else None)
        return {**data, **person_events.for_day(cam,date)}

    @app.post('/api/timeline/{cam}/seek')
    def seek(cam:str,date:str,time:str,stream:bool=False,window:bool=False,window_seconds:int=1200,prefetch:bool=False):
        data=coverage(store,provider,cam,date);value=second(time)
        person_events.request(cam,date,value)
        if stream and window:
            if not 300<=window_seconds<=2400:raise HTTPException(400,'Invalid playback window length')
            if prefetch:
                with instant.lock:
                    if len(instant.sessions)>=2:return {'state':'deferred'}
            sources,offset,begin,mapping=window_sources(cam,date,value,max_window=window_seconds)
            try:result=instant.prepare_window(cam,sources,offset)
            except HTTPException as error:
                if prefetch and error.status_code==429:return {'state':'deferred'}
                raise
            return {**result,'offset':value-begin,'segment_start':begin,'recording':sources[0][0],
                    'window_end':begin+result['duration'],'segments':mapping,
                    'download_url':f"/download/{cam}/{sources[0][0]}"}
        candidate=next((item for item in reversed(data['segments']) if item['start']<=value),None)
        if candidate and hasattr(provider,'recording_duration'):
            provider.recording_duration(cam,candidate['recording']);data=coverage(store,provider,cam,date)
        segment,offset=locate(data,time)
        if stream:
            duration=provider.recording_duration(cam,segment['recording'])
            if duration is None:raise HTTPException(503,'Unable to read recording duration')
            result=instant.prepare(cam,segment['recording'],offset,duration)
            return {**result,'offset':0,'segment_start':segment['start']+offset,'recording':segment['recording'],'download_url':f"/download/{cam}/{segment['recording']}"}
        result=provider.prepare(cam,segment['recording'])
        return {**result,'offset':offset,'segment_start':segment['start'],'recording':segment['recording'],'download_url':f"/download/{cam}/{segment['recording']}"}

    @app.post('/api/timeline/{cam}/next')
    def next_segment(cam:str,date:str,recording:str,prefetch:bool=True,stream:bool=False):
        fd,_=store.open(cam,recording);os.close(fd)
        duration=provider.known_duration(cam,recording) if hasattr(provider,'known_duration') else None
        segment=following(coverage(store,provider,cam,date),recording,duration)
        next_date=date
        if segment is None and duration is not None:
            current=next((item for item in coverage(store,provider,cam,date)['segments'] if item['recording']==recording),None)
            if current and current['start']+duration>=86397 and day(date).isoformat()!='9999-12-31':
                next_date=(day(date)+timedelta(days=1)).isoformat()
                tomorrow=coverage(store,provider,cam,next_date)['segments']
                if tomorrow and abs(tomorrow[0]['start']+86400-current['start']-duration)<=3:segment=tomorrow[0]
        if segment is None:return {'state':'gap','message':'No continuous recording follows'}
        result={} if stream else (provider.prefetch(cam,segment['recording']) if prefetch and hasattr(provider,'prefetch') else provider.prepare(cam,segment['recording']))
        current=next((item for item in coverage(store,provider,cam,date)['segments'] if item['recording']==recording),None)
        offset=max(0,current['start']+duration-segment['start']-(86400 if next_date!=date else 0)) if current and duration is not None else 0
        if stream:
            duration=provider.recording_duration(cam,segment['recording'])
            if duration is None:raise HTTPException(503,'Unable to read recording duration')
            result=instant.prepare(cam,segment['recording'],offset,duration)
        return {**result,'date':next_date,'offset':0 if stream else offset,'segment_start':segment['start']+offset if stream else segment['start'],'recording':segment['recording'],'download_url':f"/download/{cam}/{segment['recording']}"}

    @app.post('/api/live/{cam}/sessions')
    def live_session(cam:str):
        camera(cam);return live.call('/sessions/'+cam,'POST')

    @app.api_route('/api/live/{cam}/sessions/{lease}',methods=['POST','DELETE'])
    def live_heartbeat(request:Request,cam:str,lease:str):
        camera(cam)
        if not __import__('re').fullmatch('[a-f0-9]{32}',lease):raise HTTPException(404,'Live session expired')
        return live.call('/sessions/'+cam+'/'+lease,request.method)

    @app.api_route('/live-media/{cam}/{generation}/{lease}/{name}',methods=['GET','HEAD'])
    def live_media(cam:str,generation:str,lease:str,name:str):
        camera(cam)
        if not __import__('re').fullmatch('[a-f0-9]{32}',generation) or not __import__('re').fullmatch('[a-f0-9]{32}',lease) or not __import__('re').fullmatch(r'index\.m3u8|seg[0-9]{5,}\.ts',name):raise HTTPException(404,'Live media unavailable')
        data=live.call('/media/'+cam+'/'+generation+'/'+lease+'/'+name)
        return Response(headers={'X-Accel-Redirect':data['internal']},media_type='application/vnd.apple.mpegurl' if name=='index.m3u8' else 'video/mp2t')

    return app

app = create_app()
