"""Single-worker, CPU-bounded local analysis of immutable recording segments."""
import fcntl
import hashlib
import json
import logging
import math
import os
import secrets
import signal
import sqlite3
import stat
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from .config import CAMERAS, Settings
from .recordings import Store, filename
from .playback import identity

LOG = logging.getLogger('cctv.persons')
MODEL_SHA256 = '88d9096e76c0bf6ecf364041f5a218d65a3509146de36a82c22d8392d72c0500'
SIZE = 640
FRAME_BYTES = SIZE * SIZE * 3


def samples_to_events(samples, duration, step=2, threshold=.55):
    """Sampling yields approximate intervals, padded by half a sample on each side."""
    events = []
    for offset, score in samples:
        if score < threshold:
            continue
        start, end = max(0, offset-step/2), min(duration, offset+step/2)
        if end <= start:
            continue
        if events and start <= events[-1]['end']+step:
            events[-1]['end'] = end
            events[-1]['confidence'] = max(events[-1]['confidence'], score)
        else:
            events.append({'type': 'person', 'start': start, 'end': end, 'confidence': score})
    return events


class PersonModel:
    def __init__(self, model):
        import onnxruntime as ort
        ort.disable_telemetry_events()
        if hashlib.sha256(Path(model).read_bytes()).hexdigest() != MODEL_SHA256:
            raise RuntimeError('Unexpected person model checksum')
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 3
        self.session = ort.InferenceSession(str(model), sess_options=options, providers=['CPUExecutionProvider'])
        inputs = self.session.get_inputs()
        if len(inputs) != 1 or inputs[0].shape != [1, 3, SIZE, SIZE]:
            raise RuntimeError('Unexpected person model input')
        self.input = inputs[0].name
        self.half = inputs[0].type == 'tensor(float16)'

    def score(self, rgb):
        import numpy as np
        image = np.frombuffer(rgb, dtype=np.uint8).reshape(SIZE, SIZE, 3)
        tensor = np.ascontiguousarray(image.transpose(2, 0, 1), dtype=np.float16 if self.half else np.float32)[None] / 255.0
        predictions = self.session.run(None, {self.input: tensor})[0][0]
        # COCO class zero is person. Do not relabel higher-scoring animal/vehicle proposals.
        selected = predictions[np.argmax(predictions[:, 5:], axis=1) == 0]
        if not len(selected):
            return 0.0
        scores = selected[:, 4] * selected[:, 5]
        scores = scores[np.isfinite(scores)]
        return min(1.0, max(0.0, float(scores.max()))) if len(scores) else 0.0


class VideoAnalyzer:
    def __init__(self, model, step=2, threshold=.55, timeout=600):
        self.model = model
        self.step = step
        self.threshold = threshold
        self.timeout = timeout
        self.stop = threading.Event()
        self.process = None
        self.lock = threading.RLock()

    @staticmethod
    def terminate(process):
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                pass

    def cancel(self):
        self.stop.set()
        with self.lock:
            self.terminate(self.process)

    def analyze(self, fd):
        probe = subprocess.run(['/usr/bin/ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                '-f', 'matroska', '-of', 'json', f'/proc/self/fd/{fd}'],
                               pass_fds=(fd,), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=20, check=True)
        duration = float(json.loads(probe.stdout)['format']['duration'])
        if not math.isfinite(duration) or not 0 < duration <= 600:
            raise ValueError('Unsupported recording duration')
        args = ['/usr/bin/ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
                '-threads', '1', '-filter_threads', '1', '-protocol_whitelist', 'file,pipe',
                '-f', 'matroska', '-i', f'/proc/self/fd/{fd}', '-t', str(duration),
                '-map', '0:v:0', '-an', '-sn', '-dn', '-vf',
                f'fps=1/{self.step}:start_time=0,scale={SIZE}:{SIZE}:force_original_aspect_ratio=decrease:flags=bilinear,pad={SIZE}:{SIZE}:(ow-iw)/2:(oh-ih)/2:color=0x727272',
                '-pix_fmt', 'rgb24', '-threads', '1', '-f', 'rawvideo', 'pipe:1']
        with self.lock:
            if self.stop.is_set():
                raise RuntimeError('Detector stopping')
            process = subprocess.Popen(args, pass_fds=(fd,), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       bufsize=FRAME_BYTES, start_new_session=True)
            self.process = process
        watchdog = threading.Timer(self.timeout, self.terminate, args=(process,))
        watchdog.start()
        samples = []
        try:
            for index in range(math.ceil(duration/self.step)+2):
                if self.stop.is_set():
                    raise RuntimeError('Detector stopping')
                frame = process.stdout.read(FRAME_BYTES)
                if not frame:
                    break
                if len(frame) != FRAME_BYTES:
                    raise ValueError('Incomplete detection frame')
                samples.append((index*self.step, self.model.score(frame)))
            if process.wait(timeout=5) or not samples:
                raise RuntimeError('Recording analysis failed')
            return duration, samples_to_events(samples, duration, self.step, self.threshold)
        finally:
            watchdog.cancel()
            self.terminate(process)
            process.stdout.close()
            with self.lock:
                if self.process is process:
                    self.process = None


class DetectionIndex:
    def __init__(self, settings, root, analyzer):
        self.settings = settings
        self.store = Store(settings)
        self.root = Path(root)
        source = settings.recordings_root.resolve()
        target = self.root.resolve()
        if source == target or source in target.parents or target in source.parents:
            raise ValueError('Detection index must be separate from recordings')
        self.analyzer = analyzer
        self.profile = f'yolov5n-v7-{MODEL_SHA256}-sample{analyzer.step}-confidence{analyzer.threshold}-v1'
        self.root.mkdir(parents=True, mode=0o750, exist_ok=True)
        if self.root.is_symlink():
            raise RuntimeError('Invalid detection directory')
        self.lock_fd = os.open(self.root/'.worker.lock', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.db = sqlite3.connect(self.root/'.state.sqlite')
            os.chmod(self.root/'.state.sqlite', 0o600)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('CREATE TABLE IF NOT EXISTS recordings (camera TEXT, recording TEXT, identity TEXT, profile TEXT, duration REAL, events TEXT, PRIMARY KEY(camera,recording))')
            self.db.commit()
        except Exception:
            os.close(self.lock_fd)
            raise
        self.failed_until = {}
        self.last_cleanup = 0

    def close(self):
        self.analyzer.cancel()
        self.db.close()
        os.close(self.lock_fd)

    def requests(self):
        result = {}
        for cam in CAMERAS:
            fd = None
            try:
                fd = os.open(self.root/'requests'/(cam+'.json'), os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > 1024:
                    continue
                data = json.loads(os.read(fd, 1025))
                if data['camera'] == cam and 0 <= time.time()-data['updated'] < 7200:
                    # Validate the date even though the producer is the local app.
                    from .recordings import day
                    day(data['date'])
                    point = data.get('time')
                    if point is None or isinstance(point, (float,int)) and not isinstance(point, bool) and 0 <= point < 86400:
                        result[cam] = data
            except (OSError, ValueError, KeyError, TypeError):
                pass
            finally:
                if fd is not None:
                    os.close(fd)
        return result

    def candidates(self):
        rows = []
        all_sources = set()
        requests = self.requests()
        now = time.time()
        turns=dict(self.db.execute('SELECT camera,MAX(rowid) FROM recordings WHERE profile=? GROUP BY camera',(self.profile,)))
        # Sources have local wall-clock names; use their modification times for age priority.
        for cam in CAMERAS:
            try:
                with self.store.directory(cam) as folder:
                    entries = self.store.entries(folder)
            except Exception:
                LOG.warning('Recording discovery unavailable (camera=%s)', cam)
                continue
            newest = entries[-1][0] if entries else None
            request = requests.get(cam)
            target_name = None
            if request and request.get('time') is not None:
                target = datetime.fromisoformat(request['date'])+timedelta(seconds=request['time'])
                earlier = [(started,name) for name,started,info in entries if started<=target and self.store.eligible((name,started,info),newest)]
                if earlier:
                    point,name = max(earlier)
                    if (target-point).total_seconds()<=600:
                        target_name = name
            for name, started, info in entries:
                all_sources.add((cam,name))
                if not self.store.eligible((name,started,info),newest):
                    continue
                existing = self.db.execute('SELECT identity,profile FROM recordings WHERE camera=? AND recording=?', (cam,name)).fetchone()
                if existing == (json.dumps(identity(info)), self.profile) or self.failed_until.get((cam,name),0) > now:
                    continue
                priority, distance = 3, 0
                request = requests.get(cam)
                if request and started.date().isoformat() == request['date']:
                    priority = 3
                    if request.get('time') is not None:
                        value = started.hour*3600+started.minute*60+started.second
                        delta = request['time']-value
                        if 0 <= delta <= 600:
                            priority = 0
                            distance = delta
                        elif abs(delta) <= 900:
                            priority = 2
                            distance = abs(delta)
                        else:
                            priority = 3
                if priority==0:priority=2
                if name==target_name:priority=0;distance=0
                if now-info.st_mtime < 900 and priority > 1:
                    priority = 1;distance=0
                rows.append(((priority,distance,turns.get(cam,0) if priority==1 else 0,-info.st_mtime,cam),cam,name))
        if now-self.last_cleanup > 60:
            removed = []
            for cam,name in self.db.execute('SELECT camera,recording FROM recordings').fetchall():
                if (cam,name) not in all_sources:
                    self.db.execute('DELETE FROM recordings WHERE camera=? AND recording=?',(cam,name))
                    removed.append((cam,name[:10]))
            self.db.commit()
            for cam,date in set(removed):
                self.publish(cam,date)
                self.publish(cam,(datetime.fromisoformat(date)+timedelta(days=1)).date().isoformat())
            self.failed_until = {key:value for key,value in self.failed_until.items() if value > now and key in all_sources}
            self.last_cleanup = now
        return [(cam,name) for _,cam,name in sorted(rows)]

    def process(self, cam, name):
        fd = None
        started = time.monotonic()
        try:
            fd, source = self.store.open(cam,name)
            duration, events = self.analyzer.analyze(fd)
            check, latest = self.store.open(cam,name)
            os.close(check)
            if identity(source) != identity(os.fstat(fd)) or identity(source) != identity(latest):
                raise ValueError('Recording changed during detection')
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO recordings VALUES(?,?,?,?,?,?)',
                                (cam,name,json.dumps(identity(source)),self.profile,duration,json.dumps(events)))
            self.publish(cam,name[:10])
            tomorrow = (filename(name).date()+timedelta(days=1)).isoformat()
            if filename(name).hour*3600+filename(name).minute*60+filename(name).second+duration > 86400:
                self.publish(cam,tomorrow)
            LOG.info('Recording analyzed (camera=%s,person_intervals=%s,elapsed=%.1fs)',cam,len(events),time.monotonic()-started)
            return True
        except Exception as error:
            self.failed_until[(cam,name)] = time.time()+300
            LOG.warning('Recording analysis deferred (camera=%s,reason=%s)',cam,type(error).__name__)
            return False
        finally:
            if fd is not None:
                os.close(fd)

    def publish(self, cam, date):
        selected = datetime.fromisoformat(date)
        previous = (selected-timedelta(days=1)).date().isoformat()
        rows = self.db.execute('SELECT recording,duration,events FROM recordings WHERE camera=? AND profile=? AND (recording LIKE ? OR recording LIKE ?)',
                               (cam,self.profile,date+'_%',previous+'_%')).fetchall()
        events, analyzed = [], []
        for name,duration,raw in rows:
            offset = (filename(name)-selected).total_seconds()
            left, right = max(0,offset), min(86400,offset+duration)
            if right <= left:
                continue
            analyzed.append({'start':left,'end':right})
            for event in json.loads(raw):
                left, right = max(0,offset+event['start']), min(86400,offset+event['end'])
                if right > left:
                    events.append({**event,'start':left,'end':right})
        folder = self.root/cam
        folder.mkdir(mode=0o750, exist_ok=True)
        if folder.is_symlink():
            raise RuntimeError('Invalid camera index directory')
        payload = {'source':'server','model':'YOLOv5n','sample_seconds':self.analyzer.step,
                   'threshold':self.analyzer.threshold,'events':events,'analyzed':analyzed,
                   'updated':time.time()}
        data = json.dumps(payload,separators=(',',':')).encode()
        if len(data) > 2*1024**2:
            raise RuntimeError('Detection index capacity reached')
        temporary = folder/(date+'.'+secrets.token_hex(8)+'.tmp')
        handle = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o640)
        try:
            with os.fdopen(handle,'wb') as output:
                output.write(data);output.flush();os.fsync(output.fileno())
            os.replace(temporary,folder/(date+'.json'))
        finally:
            if temporary.exists():
                temporary.unlink()


def main():
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(name)s: %(message)s')
    settings = Settings(recordings_root=Path(os.getenv('CCTV_RECORDINGS_ROOT','/srv/cctv')))
    model = PersonModel(os.getenv('CCTV_PERSON_MODEL','/opt/cctv-web/models/yolov5n-v7.0-fp32.onnx'))
    analyzer = VideoAnalyzer(model)
    worker = DetectionIndex(settings,os.getenv('CCTV_PERSON_EVENTS_ROOT','/var/lib/cctv-persons'),analyzer)
    stopping = threading.Event()
    def stop(*args):
        stopping.set();analyzer.cancel()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    LOG.info('Person detection ready (model=YOLOv5n,sampling=2s,threshold=0.55)')
    try:
        while not stopping.is_set():
            jobs = worker.candidates()
            if jobs:
                worker.process(*jobs[0])
            else:
                stopping.wait(20)
    finally:
        worker.close()

if __name__ == '__main__':
    main()
