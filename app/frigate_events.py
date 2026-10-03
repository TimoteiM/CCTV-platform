"""Import local Frigate person events into the existing timeline's read-only index."""
import json
import logging
import math
import os
import signal
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .config import CAMERAS
from .person_events import PersonEvents

TZ = ZoneInfo('Europe/Bucharest')
LOG = logging.getLogger('cctv.frigate')


def split_event(start, end):
    """Map Unix timestamps to local day/time, splitting events across midnight."""
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in (start, end)):
        raise ValueError('Invalid timestamps')
    if not 0 < start < end or end-start > 7*86400:
        raise ValueError('Invalid event duration')
    position = start
    result = []
    while position < end:
        cursor = datetime.fromtimestamp(position, TZ)
        midnight = datetime.combine(cursor.date()+timedelta(days=1), datetime.min.time(), TZ)
        boundary = min(end, midnight.timestamp())
        last = datetime.fromtimestamp(boundary, TZ)
        left = cursor.hour*3600+cursor.minute*60+cursor.second+cursor.microsecond/1e6
        right = 86400 if boundary == midnight.timestamp() else last.hour*3600+last.minute*60+last.second+last.microsecond/1e6
        # The repeated autumn hour has one position on the existing 24-hour timeline.
        if right <= left:
            right = min(86400, left+(boundary-position))
        if right > left:
            result.append((cursor.date().isoformat(), left, right))
        position = boundary
    return result


class FrigateIndex:
    def __init__(self, root, client=None, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.client = client or httpx.Client(base_url='http://127.0.0.1:5000', timeout=10, trust_env=False)
        self.clock = clock
        self.db = sqlite3.connect(self.root/'events.sqlite')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, camera TEXT, start REAL, end REAL, active INTEGER)')
        self.db.execute('CREATE TABLE IF NOT EXISTS coverage (camera TEXT, date TEXT, start REAL, end REAL, PRIMARY KEY(camera,date,start))')
        self.previous = None
        self.previous_cameras = set()
        self.since = self.clock()-31*86400
        self.dirty = set()

    def get(self, path, params=None):
        with self.client.stream('GET', path, params=params) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 4*1024**2:
                    raise ValueError('Frigate response too large')
        return json.loads(body)

    def ingest(self, event, now):
        if not isinstance(event, dict) or event.get('label') != 'person' or event.get('camera') not in CAMERAS:
            return
        identifier = event.get('id')
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 128:
            return
        if event.get('false_positive'):
            old = self.db.execute('SELECT camera,start,end FROM events WHERE id=?', (identifier,)).fetchone()
            if old:
                self.dirty.update((old[0], day) for day, _, _ in split_event(old[1], old[2]))
                self.db.execute('DELETE FROM events WHERE id=?', (identifier,))
            return
        start, end = event.get('start_time'), event.get('end_time')
        active = end is None
        end = now if active else end
        try:
            parts = split_event(start, end)
            if start > now or end > now+60 or start < now-31*86400:
                return
        except (ValueError, OverflowError, OSError, TypeError):
            return
        old = self.db.execute('SELECT start,end FROM events WHERE id=?', (identifier,)).fetchone()
        if old:
            self.dirty.update((event['camera'], day) for day, _, _ in split_event(*old))
        self.db.execute('INSERT OR REPLACE INTO events VALUES (?,?,?,?,?)', (identifier, event['camera'], start, end, int(active)))
        self.dirty.update((event['camera'], day) for day, _, _ in parts)

    def atomic(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        temporary = path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
        os.replace(temporary, path)

    def publish(self, now):
        for cam, day in sorted(self.dirty):
            midnight = datetime.fromisoformat(day).replace(tzinfo=TZ)
            begin = midnight.timestamp()
            finish = (midnight+timedelta(days=1)).timestamp()
            intervals = []
            for start, end in self.db.execute('SELECT start,end FROM events WHERE camera=? AND start<? AND end>?', (cam, finish, begin)):
                intervals.extend({'start': left, 'end': right} for date, left, right in split_event(start, end) if date == day)
            covered = [{'start': start, 'end': end} for start, end in self.db.execute('SELECT start,end FROM coverage WHERE camera=? AND date=?', (cam, day))]
            self.atomic(self.root/cam/(day+'.json'), {'source': 'frigate', 'events': [{'type': 'person', **item} for item in PersonEvents.merge_intervals(intervals)], 'analyzed': PersonEvents.merge_intervals(covered), 'updated': now})
        self.dirty.clear()

    def poll(self):
        now = self.clock()
        before = None
        for _ in range(100):
            params = {'labels': 'person', 'after': self.since, 'limit': 500, 'include_thumbnails': 0}
            if before is not None:
                params['before'] = before
            events = self.get('/api/events', params)
            if not isinstance(events, list) or len(events) > 500:
                raise ValueError('Invalid Frigate events response')
            for event in events:
                self.ingest(event, now)
            if len(events) < 500:
                break
            next_before = min(event['start_time'] for event in events)
            if before is not None and next_before >= before:
                raise ValueError('Frigate pagination did not advance')
            before = next_before
        else:
            raise ValueError('Frigate event backlog exceeds limit')
        # Include long-running tracked people even after the incremental cursor advances.
        ongoing = self.get('/api/events', {'labels': 'person', 'in_progress': 1, 'limit': 500, 'include_thumbnails': 0})
        if not isinstance(ongoing, list) or len(ongoing) > 500:
            raise ValueError('Invalid active events response')
        for event in ongoing:
            self.ingest(event, now)
        stats = self.get('/api/stats')
        if not isinstance(stats, dict) or not isinstance(stats.get('cameras'), dict):
            raise ValueError('Invalid Frigate stats')
        healthy = {cam for cam, info in stats.get('cameras', {}).items() if cam in CAMERAS and isinstance(info, dict) and info.get('camera_fps', 0)>0 and info.get('process_fps', 0)>0 and info.get('detection_enabled', True)}
        if self.previous is not None and 0 < now-self.previous <= 30:
            for cam in healthy & self.previous_cameras:
                for day, start, end in split_event(self.previous, now):
                    rows = self.db.execute('SELECT start,end FROM coverage WHERE camera=? AND date=?', (cam,day)).fetchall()
                    merged = PersonEvents.merge_intervals([{'start': left, 'end': right} for left, right in rows]+[{'start': start, 'end': end}])
                    self.db.execute('DELETE FROM coverage WHERE camera=? AND date=?', (cam,day))
                    self.db.executemany('INSERT INTO coverage VALUES (?,?,?,?)', [(cam,day,item['start'],item['end']) for item in merged])
                    self.dirty.add((cam,day))
        self.previous, self.previous_cameras = now, healthy
        self.db.execute('DELETE FROM events WHERE end<?', (now-31*86400,))
        cutoff = datetime.fromtimestamp(now-31*86400,TZ).date().isoformat()
        self.db.execute('DELETE FROM coverage WHERE date<?', (cutoff,))
        self.db.commit()
        for cam in CAMERAS:
            for path in (self.root/cam).glob('????-??-??.json'):
                if path.stem < cutoff:
                    path.unlink()
        self.publish(now)
        active_start = self.db.execute("SELECT MIN(start) FROM events WHERE active=1").fetchone()[0]
        self.since = min(now-3600, active_start-1) if active_start is not None else now-3600
        self.atomic(self.root/'status.json', {'connected': True, 'updated': now, 'cameras': sorted(healthy)})
        return healthy

    def close(self):
        self.db.close()
        self.client.close()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    stop = threading.Event()
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, lambda *_: stop.set())
    worker = FrigateIndex(os.getenv('CCTV_FRIGATE_EVENTS_ROOT', '/var/lib/cctv-frigate-events'))
    try:
        while not stop.is_set():
            try:
                healthy = worker.poll()
                LOG.info('Frigate index synchronized (%s/8 cameras)', len(healthy))
            except (httpx.HTTPError, ValueError, TypeError, KeyError, sqlite3.Error, OSError):
                worker.previous = None
                LOG.warning('Frigate unavailable; preserving existing person events')
                worker.atomic(worker.root/'status.json', {'connected': False, 'updated': worker.clock()})
            stop.wait(5)
    finally:
        worker.close()


if __name__ == '__main__':
    main()
