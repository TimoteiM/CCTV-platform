"""Single-worker playback jobs and a flat, strictly separate, atomic media cache."""
import fcntl
import hashlib
import json
import logging
import os
import queue
import re
import stat
import threading
import time
from dataclasses import dataclass, field
from fastapi import HTTPException
from .converter import FFmpegConverter, PROFILE_VERSION
from .recordings import Store

LOG=logging.getLogger('cctv.playback')
KEY=re.compile(r'[0-9a-f]{64}')
CACHE_FILE=re.compile(r'[0-9a-f]{64}\.(?:mp4|json|part|json\.part)')

def identity(s):
    return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns]

@dataclass
class Job:
    key: str
    camera: str
    recording: str
    identity: list
    mode: str
    optional: bool=False
    canceled: bool=False
    state: str='queued'
    created: float=field(default_factory=time.time)
    media: dict=field(default_factory=dict)
    message: str='Waiting for playback preparation.'

class PendingPlayback:
    """Explicit disabled provider for isolated listing/download tests."""
    def prepare(self, camera, recording, mode='h264'):
        return {'state':'unavailable','message':'Browser playback preparation is not configured yet.'}
    def status(self, job):
        raise HTTPException(404,'Playback job not found')

class PlaybackManager:
    def __init__(self, settings, converter=None):
        self.settings=settings
        self.store=Store(settings)
        self.converter=converter or FFmpegConverter(settings.conversion_timeout)
        self.jobs={}
        self.durations={}
        self.duration_probe_lock=threading.Lock()
        self.duration_queue=queue.Queue(maxsize=8)
        self.duration_pending=set()
        self.duration_finished={}
        self.duration_thread=None
        self.duration_converter=FFmpegConverter(timeout=30)
        self.queue=queue.Queue(maxsize=settings.playback_queue_size)
        self.lock=threading.RLock()
        self.stop=threading.Event()
        self.thread=None
        self.fd=None
        self.lock_fd=None
        self.last_cleanup=0

    def _cache_directory(self):
        # Walk from / using no-follow FDs, including parents; cleanup never follows links.
        parts=os.path.abspath(self.settings.cache_root).split('/')[1:]
        fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
        try:
            for part in parts:
                try: child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                except FileNotFoundError:
                    try:os.mkdir(part,0o700,dir_fd=fd)
                    except FileExistsError:pass
                    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                os.close(fd);fd=child
            return fd
        except Exception:
            os.close(fd)
            raise

    def start(self):
        with self.lock:
            if self.thread is not None: return
            if self.stop.is_set(): raise RuntimeError('Playback manager has stopped')
            self.settings.__post_init__()
            self.fd=self._cache_directory()
            try:
                self.lock_fd=os.open('.worker.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=self.fd)
                if not stat.S_ISREG(os.fstat(self.lock_fd).st_mode): raise RuntimeError('Invalid worker lock')
                fcntl.flock(self.lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self._cleanup(startup=True)
                self.thread=threading.Thread(target=self._worker,name='playback-worker',daemon=True)
                self.thread.start()
                self.duration_thread=threading.Thread(target=self._duration_worker,name='recording-metadata',daemon=True)
                self.duration_thread.start()
            except Exception:
                if self.lock_fd is not None: os.close(self.lock_fd); self.lock_fd=None
                os.close(self.fd); self.fd=None
                raise

    def close(self):
        self.stop.set()
        self.converter.cancel()
        self.duration_converter.cancel()
        if self.duration_thread is not None:self.duration_thread.join(timeout=5)
        if self.thread is not None:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                self.converter.cancel(force=True)
                self.thread.join(timeout=5)
            if self.thread.is_alive():
                LOG.error('Playback worker did not stop; retaining exclusive cache lock')
                raise RuntimeError('Playback shutdown incomplete')
        with self.lock:
            for job in self.jobs.values():
                if job.state in ('queued','processing'):
                    job.state='failed';job.message='Playback preparation interrupted. Please retry.'
            if self.lock_fd is not None: os.close(self.lock_fd);self.lock_fd=None
            if self.fd is not None: os.close(self.fd);self.fd=None

    def _key(self,cam,recording,source,mode):
        data=[cam,recording,source,mode,PROFILE_VERSION]
        return hashlib.sha256(json.dumps(data,separators=(',',':')).encode()).hexdigest()

    def _source_identity(self,cam,recording):
        fd,s=self.store.open(cam,recording)
        os.close(fd)
        return identity(s)

    def _read_meta(self,key):
        fd=None
        try:
            fd=os.open(key+'.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=self.fd)
            s=os.fstat(fd)
            if not stat.S_ISREG(s.st_mode) or s.st_size>16384:return None
            data=json.loads(os.read(fd,16385))
            if data['key']!=key or data['mode'] not in ('h264','hevc') or data['version']!=PROFILE_VERSION:return None
            if self._key(data['camera'],data['recording'],data['source_identity'],data['mode'])!=key:return None
            return data
        except (OSError,ValueError,KeyError,TypeError):
            return None
        finally:
            if fd is not None: os.close(fd)

    def _valid(self,meta,check_source=True):
        try:
            age=time.time()-float(meta['created'])
            if not 0<=age<self.settings.cache_max_age:return False
            s=os.stat(meta['key']+'.mp4',dir_fd=self.fd,follow_symlinks=False)
            if not stat.S_ISREG(s.st_mode) or identity(s)!=meta['cache_identity']:return False
            if check_source and self._source_identity(meta['camera'],meta['recording'])!=meta['source_identity']:return False
            return True
        except (OSError,HTTPException,ValueError,KeyError,TypeError):
            return False

    def _remove(self,key):
        for extension in ('.mp4','.json','.part','.json.part'):
            try: os.unlink(key+extension,dir_fd=self.fd)
            except (FileNotFoundError,IsADirectoryError):pass

    def _public(self,job,cached=False):
        result={'job_id':job.key,'state':job.state,'message':job.message,'format':job.mode}
        if job.state=='ready':
            result.update(media_url='/media/playback/'+job.key,duration=job.media['duration'],cached=cached)
        return result

    def prepare(self,cam,recording,mode='h264',optional=False):
        if mode not in ('h264','hevc'):raise HTTPException(400,'Invalid playback format')
        source=self._source_identity(cam,recording)
        self.start()
        key=self._key(cam,recording,source,mode)
        with self.lock:
            meta=self._read_meta(key)
            if meta and self._valid(meta):
                job=Job(key,cam,recording,source,mode,state='ready',media=meta,message='Recording ready.')
                self.jobs[key]=job
                self._trim_jobs()
                LOG.info('Playback cache hit (format=%s)',mode)
                return self._public(job,True)
            existing=self.jobs.get(key)
            if existing and existing.state in ('queued','processing'):
                if not optional:existing.optional=False
                return self._public(existing)
            self._trim_jobs()
            if self.queue.full() or len(self.jobs)>=128:
                LOG.info('Playback capacity reached')
                return {'state':'busy','message':'Playback capacity is full. Please retry shortly.','retry_after':8}
            if not optional:
                for pending in self.jobs.values():
                    if pending.optional and pending.state in ('queued','processing'):
                        pending.canceled=True
                        if pending.state=='processing':self.converter.cancel(force=True)
            job=Job(key,cam,recording,source,mode,optional=optional)
            self.jobs[key]=job
            self.queue.put_nowait(job)
            LOG.info('Playback request queued (format=%s)',mode)
            return self._public(job)

    def known_duration(self,cam,recording,source_identity=None):
        with self.lock:
            if self.fd is None:return None
            try:
                key=self._key(cam,recording,source_identity if source_identity is not None else self._source_identity(cam,recording),'h264')
                meta=self._read_meta(key)
                return meta['duration'] if meta and self._valid(meta,check_source=source_identity is None) else self.durations.get(key)
            except (HTTPException,KeyError):return None

    def recording_duration(self,cam,recording,background=False):
        known=self.known_duration(cam,recording)
        if known is not None:return known
        if not self.duration_probe_lock.acquire(timeout=.1 if background else 5):return None
        fd=None
        try:
            fd,source=self.store.open(cam,recording)
            # Dedicated probe instance cannot overwrite the active conversion handle.
            duration=(self.duration_converter if background else FFmpegConverter(timeout=30)).probe_duration(fd)
            with self.lock:
                if len(self.durations)>=8192:self.durations.pop(next(iter(self.durations)))
                self.durations[self._key(cam,recording,identity(source),'h264')]=duration
            return duration
        except Exception:return None
        finally:
            if fd is not None:os.close(fd)
            self.duration_probe_lock.release()

    def schedule_duration_index(self,cam,selected,rows):
        key=(cam,selected.isoformat())
        signature=tuple((row['id'],tuple(row['_identity'])) for row in rows)
        with self.lock:
            if self.stop.is_set() or key in self.duration_pending or self.duration_finished.get(key)==signature:return
            try:self.duration_queue.put_nowait((key,signature,rows))
            except queue.Full:return
            self.duration_pending.add(key)

    def _duration_worker(self):
        while not self.stop.is_set():
            try:key,signature,rows=self.duration_queue.get(timeout=.5)
            except queue.Empty:continue
            complete=True
            try:
                for row in rows:
                    if self.stop.is_set():complete=False;break
                    if self.known_duration(key[0],row['id'],row['_identity']) is None:
                        if self.recording_duration(key[0],row['id'],background=True) is None:complete=False
                        if self.stop.wait(.03):complete=False;break
                if complete:
                    with self.lock:
                        if len(self.duration_finished)>=32:self.duration_finished.pop(next(iter(self.duration_finished)))
                        self.duration_finished[key]=signature
            finally:
                with self.lock:self.duration_pending.discard(key)
                self.duration_queue.task_done()

    def prefetch(self,cam,recording):
        with self.lock:
            # Optional work never joins a queue or competes with an active request.
            if any(job.state in ('queued','processing') for job in self.jobs.values()):
                return {'state':'deferred','message':'Playback preparation is busy'}
            return self.prepare(cam,recording,optional=True)

    def _trim_jobs(self):
        completed=sorted((j for j in self.jobs.values() if j.state in ('ready','failed')),key=lambda j:j.created)
        for job in completed:
            if len(self.jobs)>=128 or time.time()-job.created>3600:
                self.jobs.pop(job.key,None)

    def _job(self,key):
        if not KEY.fullmatch(key):raise HTTPException(404,'Playback job not found')
        job=self.jobs.get(key)
        if job is None:
            meta=self._read_meta(key)
            if meta and self._valid(meta):
                job=Job(key,meta['camera'],meta['recording'],meta['source_identity'],meta['mode'],state='ready',media=meta,message='Recording ready.')
                self.jobs[key]=job
                self._trim_jobs()
        if job is None:raise HTTPException(404,'Playback job not found')
        if job.state=='ready' and not self._valid(job.media):
            self._remove(key)
            job.state='failed';job.message='Recording is no longer available. Select another segment.'
        return job

    def status(self,key):
        self.start()
        with self.lock:return self._public(self._job(key))

    def open_media(self,key):
        self.start()
        with self.lock:
            job=self._job(key)
            if job.state!='ready':raise HTTPException(404,'Playback media unavailable')
            try:
                fd=os.open(key+'.mp4',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=self.fd)
                s=os.fstat(fd)
                if not stat.S_ISREG(s.st_mode) or identity(s)!=job.media['cache_identity']:
                    os.close(fd);raise HTTPException(404,'Playback media unavailable')
                return fd,s,job.camera+'_'+job.recording[:-4]+'.mp4'
            except OSError:
                raise HTTPException(404,'Playback media unavailable') from None

    def cleanup(self):
        self.start()
        with self.lock:self._cleanup()

    def _cleanup(self,startup=False,reserve=0):
        active={j.key for j in self.jobs.values() if j.state in ('queued','processing')}
        names=os.listdir(self.fd)
        candidates=[]
        for name in names:
            if not CACHE_FILE.fullmatch(name):continue
            key=name[:64]
            if key in active and not startup:continue
            if name.endswith('.part'):
                try:os.unlink(name,dir_fd=self.fd)
                except (FileNotFoundError,IsADirectoryError):pass
            elif name.endswith('.json'):
                meta=self._read_meta(key)
                if not meta or not self._valid(meta):self._remove(key)
                else:candidates.append(meta)
            elif name.endswith('.mp4') and key+'.json' not in names:
                self._remove(key)
        def cost(meta):
            return meta['cache_identity'][2]+os.stat(meta['key']+'.json',dir_fd=self.fd,follow_symlinks=False).st_size
        size=sum(cost(m) for m in candidates)
        for meta in sorted(candidates,key=lambda m:m['created']):
            if size+reserve<=self.settings.cache_max_bytes:break
            size-=cost(meta);self._remove(meta['key'])
        self._trim_jobs()
        self.last_cleanup=time.time()

    def _write_meta(self,key,meta):
        name=key+'.json.part'
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=self.fd)
        try:
            data=json.dumps(meta,separators=(',',':')).encode()
            with os.fdopen(fd,'wb') as file:
                file.write(data);file.flush();os.fsync(file.fileno())
            os.rename(name,key+'.json',src_dir_fd=self.fd,dst_dir_fd=self.fd)
            os.fsync(self.fd)
        except Exception:
            try:os.unlink(name,dir_fd=self.fd)
            except FileNotFoundError:pass
            raise

    def _worker(self):
        while not self.stop.is_set():
            try:job=self.queue.get(timeout=1)
            except queue.Empty:
                with self.lock:
                    if time.time()-self.last_cleanup>300:
                        try:self._cleanup()
                        except Exception:LOG.warning('Playback cache cleanup failed')
                continue
            with self.lock:
                if job.canceled:
                    job.state='failed';self.queue.task_done();continue
                job.state='processing';job.message='Preparing recording. This may take a minute or two.'
            source_fd=output_fd=None
            try:
                source_fd,s=self.store.open(job.camera,job.recording)
                if identity(s)!=job.identity:raise ValueError('Source changed')
                limit=min(self.settings.cache_max_bytes-16384,512*1024**2,max(64*1024**2,s.st_size*8))
                if limit<=0:raise ValueError('Cache capacity too small')
                with self.lock:
                    self._cleanup(reserve=limit+16384)
                    self._remove(job.key)
                    output_fd=os.open(job.key+'.part',os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=self.fd)
                if hasattr(self.converter,'priority'):self.converter.priority=19 if job.optional else 10
                media=self.converter.convert(source_fd,output_fd,job.mode,limit)
                if self.stop.is_set() or job.canceled or identity(os.fstat(source_fd))!=job.identity or self._source_identity(job.camera,job.recording)!=job.identity:
                    raise ValueError('Source changed or preparation stopped')
                # Grant nginx read access only to validated media; partials and sidecars stay private.
                if self.settings.download_mode == 'nginx': os.fchmod(output_fd, 0o640)
                os.fsync(output_fd)
                with self.lock:
                    os.rename(job.key+'.part',job.key+'.mp4',src_dir_fd=self.fd,dst_dir_fd=self.fd)
                    meta={**media,'key':job.key,'camera':job.camera,'recording':job.recording,'mode':job.mode,
                          'version':PROFILE_VERSION,'source_identity':job.identity,'created':time.time(),
                          'cache_identity':identity(os.stat(job.key+'.mp4',dir_fd=self.fd,follow_symlinks=False))}
                    self._write_meta(job.key,meta)
                    job.media=meta;job.state='ready';job.message='Recording ready.'
                    self._cleanup()
                LOG.info('Playback preparation completed (format=%s)',job.mode)
            except Exception as error:
                with self.lock:
                    self._remove(job.key)
                    job.state='failed';job.message='Unable to prepare this recording. Please retry or download the original.'
                LOG.warning('Playback preparation failed (reason=%s)',type(error).__name__)
            finally:
                if source_fd is not None:os.close(source_fd)
                if output_fd is not None:os.close(output_fd)
                if job.canceled and not self.stop.is_set() and hasattr(self.converter,'stopped'):self.converter.stopped.clear()
                self.queue.task_done()
