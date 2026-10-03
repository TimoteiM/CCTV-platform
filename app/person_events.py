"""Read-only person-event index; camera events are distinct from motion/coverage."""
import json
import math
import os
import stat
import secrets
import time
from pathlib import Path
from .recordings import camera, day

class PersonEvents:
    def __init__(self, root=None):
        self.root = Path(root) if root else None

    def request(self, cam, date, point=None):
        """An eight-file priority mailbox; an absent mailbox keeps camera feeds read-only."""
        camera(cam);day(date)
        if self.root is None:
            return False
        directory = None
        temporary = None
        try:
            directory = os.open(self.root/'requests', os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            temporary = cam+'.'+secrets.token_hex(8)+'.tmp'
            fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o640, dir_fd=directory)
            os.fchmod(fd,0o640)
            with os.fdopen(fd,'w') as output:
                json.dump({'camera':cam,'date':date,'time':point,'updated':time.time()},output)
            os.rename(temporary,cam+'.json',src_dir_fd=directory,dst_dir_fd=directory)
            temporary = None
            return True
        except OSError:
            return False
        finally:
            if directory is not None:
                if temporary is not None:
                    try:os.unlink(temporary,dir_fd=directory)
                    except OSError:pass
                os.close(directory)

    @staticmethod
    def merge_intervals(items):
        if not isinstance(items,list) or len(items)>10000:
            raise ValueError('Invalid analysis intervals')
        merged=[]
        checked=[]
        for item in items:
            start,end=item.get('start'),item.get('end')
            if isinstance(start,bool) or isinstance(end,bool) or not isinstance(start,(int,float)) or not isinstance(end,(int,float)) or not math.isfinite(start) or not math.isfinite(end) or not 0<=start<end<=86400:
                raise ValueError('Invalid analysis interval')
            checked.append({'start':start,'end':end})
        for item in sorted(checked,key=lambda item:item['start']):
            if merged and item['start']<=merged[-1]['end']:
                merged[-1]['end']=max(merged[-1]['end'],item['end'])
            else:merged.append(item)
        return merged

    def for_day(self, cam, date):
        camera(cam)
        date = day(date).isoformat()
        if self.root is None:
            return {'person_events': [], 'person_events_state': 'not_connected'}
        root_fd = camera_fd = event_fd = None
        try:
            root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            camera_fd = os.open(cam, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            event_fd = os.open(date+'.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=camera_fd)
            info = os.fstat(event_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024**2:
                raise ValueError('Invalid event index')
            data = json.loads(os.read(event_fd, info.st_size+1))
            if data.get('source') not in ('camera', 'server') or not isinstance(data.get('events'), list) or len(data['events']) > 10000:
                raise ValueError('Invalid event index')
            events = []
            for item in data['events']:
                if not isinstance(item, dict) or item.get('type') != 'person':
                    continue
                start, end = item.get('start'), item.get('end')
                if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, (int,float)) or not isinstance(end, (int,float)):
                    raise ValueError('Invalid event times')
                if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= 86400:
                    raise ValueError('Invalid event times')
                events.append({'start': start, 'end': end})
            merged = []
            for item in sorted(events, key=lambda item: item['start']):
                if merged and item['start'] <= merged[-1]['end']:
                    merged[-1]['end'] = max(item['end'], merged[-1]['end'])
                else:
                    merged.append(item)
            result={'person_events': merged, 'person_events_state': 'available', 'person_events_source': data['source']}
            if data['source']=='server':
                result['person_analysis_intervals']=self.merge_intervals(data.get('analyzed',[]))
            return result
        except FileNotFoundError:
            return {'person_events': [], 'person_events_state': 'not_indexed'}
        except (OSError, ValueError, TypeError, AttributeError):
            return {'person_events': [], 'person_events_state': 'unavailable'}
        finally:
            for fd in (event_fd, camera_fd, root_fd):
                if fd is not None:
                    os.close(fd)
