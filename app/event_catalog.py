"""Validated local Frigate catalog and thumbnail delivery; no network access."""
import hashlib
import json
import math
import os
import re
import stat
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from .config import CAMERAS
from .recordings import camera, day

TZ = ZoneInfo('Europe/Bucharest')
TOKEN = re.compile(r'[a-f0-9]{64}')
RAW_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')


def event_token(identifier):
    if not isinstance(identifier, str) or not RAW_ID.fullmatch(identifier):
        raise ValueError('Invalid event identifier')
    return hashlib.sha256(identifier.encode()).hexdigest()


def event_metadata(event):
    data = event.get('data') if isinstance(event.get('data'), dict) else {}
    scores = [value for value in (data.get('top_score'), data.get('score'), event.get('top_score'))
              if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1]
    zones = event.get('zones') if isinstance(event.get('zones'), list) else []
    return {'token': event_token(event['id']), 'confidence': max(scores) if scores else None,
            'zones': [value[:64] for value in zones[:32] if isinstance(value, str)],
            'has_snapshot': event.get('has_snapshot') is True}


def read_private(root, parts, maximum):
    """Open only no-follow directories/files and bound all reads."""
    handles = []
    try:
        handles.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for part in parts[:-1]:
            handles.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handles[-1]))
        handles.append(os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=handles[-1]))
        info = os.fstat(handles[-1])
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError('Invalid event file')
        body = bytearray()
        while len(body) <= maximum:
            chunk = os.read(handles[-1], min(65536, maximum+1-len(body)))
            if not chunk:
                break
            body.extend(chunk)
        if len(body) > maximum:
            raise ValueError('Event file too large')
        return bytes(body), info
    finally:
        for fd in reversed(handles):
            os.close(fd)


class EventCatalog:
    def __init__(self, root=None, clock=time.time):
        self.root = Path(root) if root else None
        self.clock = clock

    def status(self):
        if self.root is None:
            return {'connected': False, 'cameras': [], 'updated': None}
        try:
            body, _ = read_private(self.root, ['status.json'], 4096)
            data = json.loads(body)
            updated = data.get('updated')
            cameras = data.get('cameras')
            if isinstance(updated, bool) or not isinstance(updated, (int, float)) or not math.isfinite(updated):
                raise ValueError('Invalid status')
            healthy = [cam for cam in CAMERAS if isinstance(cameras, list) and cam in cameras]
            return {'connected': data.get('connected') is True and 0 <= self.clock()-updated <= 30,
                    'cameras': healthy, 'updated': updated}
        except (OSError, ValueError, TypeError, AttributeError):
            return {'connected': False, 'cameras': [], 'updated': None}

    def thumbnail(self, token):
        if not TOKEN.fullmatch(token) or self.root is None:
            raise HTTPException(404, 'Thumbnail unavailable')
        try:
            body, info = read_private(self.root, ['thumbnails', token+'.jpg'], 512*1024)
            if self.clock()-info.st_mtime > 86400 or not body.startswith(b'\xff\xd8\xff') or not body.endswith(b'\xff\xd9'):
                raise ValueError('Expired or invalid image')
            return body
        except (OSError, ValueError):
            raise HTTPException(404, 'Thumbnail unavailable') from None

    def for_day(self, cam, date, with_thumbnails=True):
        camera(cam);day(date)
        if self.root is None:
            return [], 'not_connected'
        try:
            body, _ = read_private(self.root, [cam, date+'.events.json'], 4*1024**2)
            data = json.loads(body)
            if data.get('version') != 1 or not isinstance(data.get('events'), list) or len(data['events']) > 10000:
                raise ValueError('Invalid catalog')
            rows = []
            for item in data['events']:
                token = item.get('token')
                start, end = item.get('start'), item.get('end')
                left = item.get('point')
                if not isinstance(token, str) or not TOKEN.fullmatch(token):
                    raise ValueError('Invalid event')
                for value in (start, end, left):
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                        raise ValueError('Invalid event time')
                if not 0 < start < end or end-start > 7*86400 or not 0 <= left < 86400:
                    raise ValueError('Invalid event time')
                confidence = item.get('confidence')
                if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                    raise ValueError('Invalid confidence')
                zones = item.get('zones', [])
                if not isinstance(zones, list) or len(zones)>32 or any(not isinstance(value, str) or len(value)>64 for value in zones):
                    raise ValueError('Invalid zones')
                thumbnail_state = 'expired' if self.clock()-start > 86400 else 'pending'
                thumbnail_url = None
                if with_thumbnails and thumbnail_state != 'expired':
                    try:
                        self.thumbnail(token)
                        thumbnail_url = '/event-media/'+token+'.jpg'
                        thumbnail_state = 'available'
                    except HTTPException:
                        pass
                rows.append({'id': token, 'camera': cam, 'date': date, 'label': 'person',
                             'start': start, 'end': end, 'point': left, 'time': time.strftime('%H:%M:%S', time.gmtime(left)),
                             'start_local': datetime.fromtimestamp(start, TZ).isoformat(timespec='seconds'),
                             'end_local': None if item.get('active') is True else datetime.fromtimestamp(end, TZ).isoformat(timespec='seconds'),
                             'active': item.get('active') is True, 'duration': round(end-start, 1),
                             'confidence': confidence, 'zones': zones, 'thumbnail_url': thumbnail_url,
                             'thumbnail_state': thumbnail_state})
            return rows, 'available'
        except FileNotFoundError:
            return [], 'not_indexed'
        except (OSError, ValueError, TypeError, AttributeError, OverflowError):
            return [], 'unavailable'

    def hydrate_thumbnail(self, item):
        if item['thumbnail_state'] == 'expired':
            return
        try:
            self.thumbnail(item['id'])
            item['thumbnail_url'] = '/event-media/'+item['id']+'.jpg'
            item['thumbnail_state'] = 'available'
        except HTTPException:
            pass
