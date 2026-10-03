"""Read-only discovery; all opens are relative to trusted, no-follow directory FDs."""
import os
import re
import stat
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from fastapi import HTTPException
from .config import CAMERAS

PATTERN = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}\.mkv', re.ASCII)

def camera(value):
    if value not in CAMERAS:
        raise HTTPException(404, 'Camera not found')
    return value

def day(value):
    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise HTTPException(400, 'Invalid date')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, 'Invalid date') from None

def filename(value):
    if not PATTERN.fullmatch(value):
        raise HTTPException(404, 'Recording not found')
    try:
        return datetime.strptime(value, '%Y-%m-%d_%H-%M-%S.mkv')
    except ValueError:
        raise HTTPException(404, 'Recording not found') from None

class Store:
    def __init__(self, settings):
        self.settings = settings

    @contextmanager
    def directory(self, cam):
        camera(cam)
        root = folder = None
        try:
            root = os.open(self.settings.recordings_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            folder = os.open(cam, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            yield folder
        except OSError:
            raise HTTPException(404, 'Recordings unavailable') from None
        finally:
            if folder is not None: os.close(folder)
            if root is not None: os.close(root)

    def entries(self, fd):
        entries = []
        for name in os.listdir(fd):
            try:
                start = filename(name)
                s = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISREG(s.st_mode):
                    entries.append((name, start, s))
            except (HTTPException, OSError):
                continue
        return sorted(entries, key=lambda x: x[0])

    def eligible(self, item, newest):
        name, _, s = item
        return s.st_size > 0 and time.time() - s.st_mtime >= self.settings.minimum_age and (not self.settings.exclude_newest or name != newest)

    def list(self, cam, selected, with_identity=False, previous_day=False):
        with self.directory(cam) as fd:
            entries = self.entries(fd)
            newest = entries[-1][0] if entries else None
            dates={selected}
            if previous_day and selected!=date.min:dates.add(selected-timedelta(days=1))
            return [dict(id=n, start=t.isoformat(), time=t.strftime('%H:%M:%S'), size=s.st_size, modified=s.st_mtime, **({'_identity':[s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns]} if with_identity else {})) for n,t,s in entries if t.date() in dates and self.eligible((n,t,s), newest)]

    def open(self, cam, name):
        filename(name)
        with self.directory(cam) as fd:
            entries = self.entries(fd)
            newest = entries[-1][0] if entries else None
            item = next((e for e in entries if e[0] == name), None)
            if item is None or not self.eligible(item, newest):
                raise HTTPException(404, 'Recording unavailable')
            handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            s = os.fstat(handle)
            if not stat.S_ISREG(s.st_mode) or s.st_ino != item[2].st_ino or s.st_dev != item[2].st_dev or s.st_size != item[2].st_size or s.st_mtime_ns != item[2].st_mtime_ns:
                os.close(handle)
                raise HTTPException(404, 'Recording unavailable')
            return handle, s
