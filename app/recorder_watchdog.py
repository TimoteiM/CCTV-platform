"""Detect missing recording writes; restart only fixed, allowlisted recorder units."""
import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

CAMERAS = tuple(f'cam{i:02}' for i in range(1, 9))
FILE = re.compile(r'\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.mkv\Z', re.ASCII)
STALE = 120
GRACE = 120
COOLDOWN = 180

def latest_write(folder):
    latest = None
    with os.scandir(folder) as entries:
        for entry in entries:
            if not FILE.fullmatch(entry.name): continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_size > 0:
                latest = max(latest or 0, info.st_mtime)
    return latest

def decide(last_write, active, uptime, last_restart, now, disk_ok=True):
    if not disk_ok: return 'storage-low', False
    if active and last_write is not None and now - last_write <= STALE:
        return 'healthy', False
    if active and uptime < GRACE: return 'starting', False
    if last_restart and now - last_restart < COOLDOWN: return 'recovering', False
    return ('stale' if active else 'offline'), True

def unit_state(cam):
    result = subprocess.run(['/usr/bin/systemctl', 'show', f'cctv-{cam}.service', '--property=ActiveState,ActiveEnterTimestampMonotonic'], capture_output=True, text=True, timeout=5, check=True)
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    entered = int(fields.get('ActiveEnterTimestampMonotonic', '0')) / 1_000_000
    return fields.get('ActiveState') == 'active', max(0, time.monotonic() - entered)

def atomic_json(path, data):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, 'w') as handle:
        json.dump(data, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)

def run(root=Path('/srv/cctv'), state_root=Path('/var/lib/cctv-recorder-watchdog')):
    now = time.time()
    state_root.mkdir(mode=0o750, parents=True, exist_ok=True)
    state_path = state_root / 'restarts.json'
    try: restarts = json.loads(state_path.read_text())
    except (FileNotFoundError, ValueError): restarts = {}
    disk = shutil.disk_usage(root)
    disk_ok = disk.free >= 2 * 1024**3
    rows = []
    for cam in CAMERAS:
        try:
            last_write = latest_write(root / cam)
            active, uptime = unit_state(cam)
            health, restart = decide(last_write, active, uptime, restarts.get(cam, 0), now, disk_ok)
            if restart:
                result = subprocess.run(['/usr/bin/systemctl', 'restart', '--no-block', f'cctv-{cam}.service'], capture_output=True, timeout=5)
                restarts[cam] = now
                health = 'recovering' if result.returncode == 0 else 'offline'
                print(f'Recorder {cam}: {health}', flush=True)
            rows.append({'camera': cam, 'status': health, 'last_write': last_write})
        except (OSError, ValueError, subprocess.SubprocessError):
            rows.append({'camera': cam, 'status': 'monitor-error', 'last_write': None})
            print(f'Recorder {cam}: monitoring failed', flush=True)
    atomic_json(state_path, restarts)
    atomic_json(state_root / 'status.json', {'checked_at': now, 'cameras': rows})

if __name__ == '__main__': run()
