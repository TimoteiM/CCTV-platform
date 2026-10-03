"""Demand-driven live service. Secrets are passed only through stdin, never URLs returned to clients."""
import os,stat,re,time,secrets,threading,subprocess,signal,shutil,ipaddress,logging
from pathlib import Path
from urllib.parse import quote
from fastapi import FastAPI,HTTPException
from contextlib import asynccontextmanager
LOG=logging.getLogger('cctv.live')
CAMERA=re.compile(r'cam0[1-8]');TOKEN=re.compile(r'[a-f0-9]{32}');FILE=re.compile(r'index\.m3u8|seg[0-9]{5,}\.ts')
class LiveManager:
 def __init__(self,root=Path('/var/cache/cctv-live'),factory=None,clock=time.monotonic,max_pipelines=8,idle_timeout=30,lease_timeout=20):
  self.root=Path(root);self.factory=factory or self.spawn;self.clock=clock;self.max=max_pipelines;self.idle=idle_timeout;self.ttl=lease_timeout;self.lock=threading.RLock();self.pipelines={};self.leases={};self.last_start=-100;self.stop=threading.Event()
 def check(self,cam):
  if not CAMERA.fullmatch(cam):raise HTTPException(404,'Camera not found')
 def spawn(self,cam,folder):
  host=str(ipaddress.ip_address(os.environ['HOME_PUBLIC_IP']));user=quote(os.environ['CAM_USER'],safe='');password=quote(os.environ['CAM_PASS'],safe='')
  config="ffconcat version 1.0\nfile 'rtsp://"+user+':'+password+'@'+host+':'+str(15540+int(cam[-2:]))+"/mpeg4cif'\noption rtsp_transport tcp\noption timeout 10000000\n"
  cpus=','.join(map(str,sorted(os.sched_getaffinity(0))[-2:]))
  args=['/usr/bin/nice','-n','15','/usr/bin/taskset','-c',cpus,'/usr/bin/ffmpeg','-nostdin','-hide_banner','-loglevel','quiet','-threads','2','-analyzeduration','1000000','-probesize','131072','-f','concat','-safe','0','-protocol_whitelist','file,pipe,rtsp,tcp,udp,rtp','-i','pipe:0','-map','0:v:0','-map','0:a:0?','-map_metadata','-1','-map_chapters','-1','-vf','fps=15','-c:v','libx264','-preset','veryfast','-tune','zerolatency','-crf','27','-maxrate','700k','-bufsize','1400k','-pix_fmt','yuv420p','-threads','2','-filter_threads','1','-g','15','-keyint_min','15','-sc_threshold','0','-c:a','aac','-b:a','48k','-ar','16000','-ac','1','-threads:a','1','-f','hls','-hls_time','1','-hls_list_size','4','-hls_flags','delete_segments+temp_file+program_date_time','-hls_segment_filename',str(folder/'seg%05d.ts'),str(folder/'index.m3u8')]
  env={k:v for k,v in os.environ.items() if k not in ('HOME_PUBLIC_IP','CAM_USER','CAM_PASS')}
  p=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env,start_new_session=True)
  try:p.stdin.write(config.encode());p.stdin.close()
  except Exception:self.terminate(p);raise RuntimeError('Live startup failed') from None
  return p
 def terminate(self,p):
  if p.poll() is None:
   try:os.killpg(p.pid,signal.SIGTERM)
   except ProcessLookupError:pass
   try:p.wait(timeout=3)
   except subprocess.TimeoutExpired:
    try:os.killpg(p.pid,signal.SIGKILL)
    except ProcessLookupError:pass
    p.wait()
 def drop(self,cam):
  pipeline=self.pipelines.pop(cam,None)
  if pipeline:self.terminate(pipeline['process']);shutil.rmtree(pipeline['folder']);LOG.info('Live pipeline stopped')
 def pressure(self):
  try:
   available=int(next(x for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')).split()[1])*1024
   return available<2*1024**3 or os.getloadavg()[0]>8
  except OSError:return True
 def reap(self):
  with self.lock:
   now=self.clock()
   if self.pressure():
    for cam in list(self.pipelines):self.drop(cam)
    self.leases.clear();return
   for key,lease in list(self.leases.items()):
    if lease['expires']<=now:self.leases.pop(key)
   for cam,pipeline in list(self.pipelines.items()):
    viewers=any(l['camera']==cam for l in self.leases.values())
    if viewers:pipeline['idle_since']=None
    elif pipeline['idle_since'] is None:pipeline['idle_since']=now
    if not viewers and now-pipeline['idle_since']>=self.idle:self.drop(cam)
    elif pipeline['process'].poll() is not None and not viewers:self.drop(cam)
 def session(self,cam):
  self.check(cam)
  with self.lock:
   self.reap()
   if self.pressure():raise HTTPException(429,'Server live capacity reached')
   now=self.clock();pipeline=self.pipelines.get(cam)
   if pipeline and pipeline['process'].poll() is not None:
    self.drop(cam);pipeline=None
   if pipeline is None:
    idle_cameras=[name for name in self.pipelines if not any(lease['camera']==name for lease in self.leases.values())]
    if len(self.pipelines)>=self.max and idle_cameras:self.drop(idle_cameras[0])
    if len(self.pipelines)>=self.max or now-self.last_start<0.25:raise HTTPException(429,'Server live capacity reached')
    if len(self.leases)>=64:raise HTTPException(429,'Server live capacity reached')
    generation=secrets.token_hex(16);folder=self.root/cam/generation;folder.mkdir(parents=True,mode=0o750)
    try:process=self.factory(cam,folder)
    except Exception:shutil.rmtree(folder);raise HTTPException(503,'Camera unavailable') from None
    pipeline={'process':process,'generation':generation,'folder':folder,'started':now,'idle_since':None};self.pipelines[cam]=pipeline;self.last_start=now;LOG.info('Live pipeline starting')
   if len(self.leases)>=64:raise HTTPException(429,'Server live capacity reached')
   lease=secrets.token_hex(16);self.leases[lease]={'camera':cam,'generation':pipeline['generation'],'expires':now+self.ttl};pipeline['idle_since']=None
   return self.state(cam,lease)
 def state(self,cam,lease,renew=False):
  self.check(cam)
  with self.lock:
   self.reap();item=self.leases.get(lease);pipeline=self.pipelines.get(cam)
   if not TOKEN.fullmatch(lease) or not item or item['camera']!=cam or not pipeline or item['generation']!=pipeline['generation']:raise HTTPException(404,'Live session expired')
   if renew:item['expires']=self.clock()+self.ttl
   state='error' if pipeline['process'].poll() is not None else ('live' if (pipeline['folder']/'index.m3u8').is_file() else 'starting')
   if (state=='starting' and self.clock()-pipeline['started']>20) or (state=='live' and time.time()-(pipeline['folder']/'index.m3u8').stat().st_mtime>12):state='error';self.terminate(pipeline['process'])
   return {'state':state,'lease':lease,'generation':pipeline['generation'],'media_url':f"/live-media/{cam}/{pipeline['generation']}/{lease}/index.m3u8",'message':'Camera unavailable' if state=='error' else 'Live stream starting' if state=='starting' else 'Live'}
 def release(self,cam,lease):
  self.check(cam)
  with self.lock:
   if self.leases.get(lease,{}).get('camera')==cam:self.leases.pop(lease)
   self.reap();return {'state':'idle'}
 def media(self,cam,generation,lease,name):
  self.check(cam)
  if not TOKEN.fullmatch(generation) or not TOKEN.fullmatch(lease) or not FILE.fullmatch(name):raise HTTPException(404,'Live media unavailable')
  with self.lock:
   result=self.state(cam,lease)
   if result['generation']!=generation or result['state']!='live':raise HTTPException(404,'Live media unavailable')
   path=self.pipelines[cam]['folder']/name
   try:
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
     info=os.fstat(fd)
     if not stat.S_ISREG(info.st_mode):raise OSError('Not a media file')
     size=info.st_size
    finally:os.close(fd)
   except OSError:raise HTTPException(404,'Live media unavailable') from None
   return {'internal':f'/_cctv_live/{cam}/{generation}/{name}','size':size}
 def close(self):
  self.stop.set()
  with self.lock:
   for cam in list(self.pipelines):self.drop(cam)
 def start(self):
  self.root.mkdir(parents=True,exist_ok=True)
  for cam in self.root.iterdir():
   if CAMERA.fullmatch(cam.name) and cam.is_dir() and not cam.is_symlink():
    for folder in cam.iterdir():
     if TOKEN.fullmatch(folder.name) and folder.is_dir() and not folder.is_symlink():shutil.rmtree(folder)
  def loop():
   while not self.stop.wait(2):self.reap()
  threading.Thread(target=loop,daemon=True).start()
manager=LiveManager()
@asynccontextmanager
async def lifespan(app):
 manager.start()
 try:yield
 finally:manager.close()
app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
@app.post('/sessions/{cam}')
def session(cam:str):return manager.session(cam)
@app.post('/sessions/{cam}/{lease}')
def heartbeat(cam:str,lease:str):return manager.state(cam,lease,True)
@app.delete('/sessions/{cam}/{lease}')
def release(cam:str,lease:str):return manager.release(cam,lease)
@app.get('/media/{cam}/{generation}/{lease}/{name}')
def media(cam:str,generation:str,lease:str,name:str):return manager.media(cam,generation,lease,name)
