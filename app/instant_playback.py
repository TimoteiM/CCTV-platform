"""Short-lived progressive HLS playback from validated, read-only recordings."""
import math
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path
from fastapi import HTTPException
from .recordings import Store
from .playback import identity

TOKEN = re.compile(r'[a-f0-9]{32}')
FILE = re.compile(r'index\.m3u8|seg[0-9]{5,}\.ts')

class InstantPlayback:
    def __init__(self, settings, factory=None, clock=time.monotonic):
        self.settings = settings
        self.store = Store(settings)
        self.root = Path(settings.cache_root) / 'streaming'
        self.factory = factory or self.spawn
        self.clock = clock
        self.lock = threading.RLock()
        self.sessions = {}
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        with self.lock:
            if self.thread is not None:
                return
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.root.is_symlink():
                raise RuntimeError('Invalid streaming directory')
            for folder in self.root.iterdir():
                if TOKEN.fullmatch(folder.name) and folder.is_dir() and not folder.is_symlink():
                    shutil.rmtree(folder)
            self.thread = threading.Thread(target=self._reaper, daemon=True)
            self.thread.start()

    @staticmethod
    def terminate(process):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                pass

    def drop(self, token):
        item = self.sessions.pop(token)
        self.terminate(item['process'])
        shutil.rmtree(item['folder'])

    def reap(self):
        with self.lock:
            for token, item in list(self.sessions.items()):
                if self.clock() - item['seen'] > 60:
                    self.drop(token)
                elif self.clock() - item['started'] > self.settings.conversion_timeout and item['process'].poll() is None:
                    self.terminate(item['process'])

    def _reaper(self):
        while not self.stop.wait(5):
            self.reap()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=6)
        with self.lock:
            for token in list(self.sessions):
                self.drop(token)

    def spawn(self, fd, folder, offset, duration, *, sources=None):
        args = ['/usr/bin/nice', '-n', '5', '/usr/bin/ffmpeg', '-nostdin', '-hide_banner',
                '-loglevel', 'error', '-threads', '2', '-ss', str(offset), '-f', 'matroska',
                '-i', f'/proc/self/fd/{fd}', '-t', str(duration), '-map', '0:v:0', '-map', '0:a:0?',
                '-map_metadata', '-1', '-map_chapters', '-1', '-vf',
                'scale=w=min(1280\\,iw):h=min(720\\,ih):force_original_aspect_ratio=decrease:force_divisible_by=2,fps=20',
                '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency', '-crf', '26',
                '-maxrate', '2000k', '-bufsize', '4000k', '-pix_fmt', 'yuv420p', '-threads', '2', '-filter_threads', '1',
                '-g', '20', '-keyint_min', '20', '-sc_threshold', '0',
                '-c:a', 'aac', '-b:a', '64k', '-ar', '48000', '-ac', '1', '-threads:a', '1',
                '-f', 'hls', '-hls_time', '1', '-hls_playlist_type', 'event',
                '-hls_flags', 'temp_file+independent_segments', '-hls_segment_filename',
                str(folder / 'seg%05d.ts'), str(folder / 'index.m3u8')]
        if sources:
            listing = folder / 'sources.ffconcat'
            listing.write_text('ffconcat version 1.0\n' + ''.join(f"file '/proc/self/fd/{handle}'\nduration {length:.6f}\n" for handle, length in sources))
            start = args.index('-ss')
            args[start:args.index('-t')] = ['-ss', str(offset), '-f', 'concat', '-safe', '0',
                '-protocol_whitelist', 'file,pipe', '-i', str(listing)]
        return subprocess.Popen(args, pass_fds=tuple(handle for handle, _ in sources) if sources else (fd,), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def prepare(self, cam, recording, offset, duration):
        if not math.isfinite(offset) or not math.isfinite(duration) or not 0 <= offset < duration <= 600:
            raise HTTPException(400, 'Invalid playback offset or recording duration')
        fd, source = self.store.open(cam, recording)
        try:
            self.start()
            with self.lock:
                if len(self.sessions) >= 2:
                    raise HTTPException(429, 'Two playback sessions are already open. Close another playback tab.')
                token = secrets.token_hex(16)
                folder = self.root / token
                folder.mkdir(mode=0o700)
                try:
                    process = self.factory(fd, folder, offset, duration-offset)
                except Exception:
                    shutil.rmtree(folder)
                    raise HTTPException(503, 'Unable to start playback') from None
                self.sessions[token] = dict(process=process, folder=folder, camera=cam, recording=recording,
                                            identity=identity(source), seen=self.clock(), started=self.clock(), duration=duration-offset)
                return self.state(token)
        finally:
            os.close(fd)

    def prepare_window(self, cam, sources, offset):
        """A bounded, continuous window of verified originals, never across real gaps."""
        if not 1 <= len(sources) <= 8 or not math.isfinite(offset) or offset < 0:
            raise HTTPException(400, 'Invalid playback window')
        opened = []
        try:
            for recording, duration in sources:
                if not math.isfinite(duration) or not 0 < duration <= 600:
                    raise HTTPException(400, 'Invalid recording duration')
                fd, info = self.store.open(cam, recording)
                opened.append((fd, duration, recording, identity(info)))
            total = sum(item[1] for item in opened)
            if offset >= total or total > 2400:
                raise HTTPException(400, 'Invalid playback window')
            self.start()
            with self.lock:
                if len(self.sessions) >= 2:
                    raise HTTPException(429, 'Two playback sessions are already open. Close another playback tab.')
                token = secrets.token_hex(16)
                folder = self.root / token
                folder.mkdir(mode=0o700)
                try:
                    process = self.spawn(opened[0][0], folder, offset, total-offset,
                                         sources=[(item[0], item[1]) for item in opened])
                except Exception:
                    shutil.rmtree(folder)
                    raise HTTPException(503, 'Unable to start playback') from None
                self.sessions[token] = dict(process=process, folder=folder, camera=cam,
                    recording=opened[0][2], identity=opened[0][3],
                    sources=[(item[2], item[3]) for item in opened], seen=self.clock(),
                    started=self.clock(), duration=total-offset)
                return self.state(token)
        finally:
            for item in opened:
                os.close(item[0])

    def _item(self, token):
        if not TOKEN.fullmatch(token) or token not in self.sessions:
            raise HTTPException(404, 'Playback session expired')
        item = self.sessions[token]
        try:
            for recording, expected in item.get('sources', [(item['recording'], item['identity'])]):
                fd, source = self.store.open(item['camera'], recording)
                os.close(fd)
                if identity(source) != expected:
                    raise HTTPException(404, 'Recording changed')
        except HTTPException:
            self.drop(token)
            raise
        item['seen'] = self.clock()
        return item

    def state(self, token):
        with self.lock:
            item = self._item(token)
            code = item['process'].poll()
            ready = (item['folder'] / 'index.m3u8').is_file()
            if code not in (None, 0) or (not ready and (code == 0 or self.clock()-item['started'] > 20)):
                self.terminate(item['process'])
                return {'state': 'failed', 'message': 'Unable to stream this recording. Try recording details.', 'stream_id': token}
            return {'state': 'ready' if ready else 'starting', 'stream_id': token,
                    'media_url': f'/instant-media/{token}/index.m3u8', 'duration': item['duration'], 'streaming': True}

    def release(self, token):
        if not TOKEN.fullmatch(token):
            raise HTTPException(404, 'Playback session expired')
        with self.lock:
            if token in self.sessions:
                self.drop(token)
        return {'state': 'stopped'}

    def open_media(self, token, name):
        if not FILE.fullmatch(name):
            raise HTTPException(404, 'Playback media unavailable')
        with self.lock:
            item = self._item(token)
            fd = None
            try:
                fd = os.open(item['folder'] / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024**2:
                    raise OSError('Invalid media')
                return fd, info.st_size
            except OSError:
                if fd is not None:
                    os.close(fd)
                raise HTTPException(404, 'Playback media unavailable') from None
