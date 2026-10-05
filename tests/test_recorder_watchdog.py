import os
from pathlib import Path
from app.recorder_watchdog import decide, latest_write

def test_recent_write_is_healthy_even_after_long_uptime():
    assert decide(980, True, 99999, 0, 1000) == ('healthy', False)

def test_running_but_stale_recorder_restarts():
    assert decide(800, True, 500, 0, 1000) == ('stale', True)

def test_empty_recorder_restarts_after_startup_grace():
    assert decide(None, True, 121, 0, 1000) == ('stale', True)
    assert decide(None, True, 80, 0, 1000) == ('starting', False)

def test_stopped_recorder_restarts():
    assert decide(800, False, 500, 0, 1000) == ('offline', True)

def test_recovery_cooldown_prevents_restart_loop():
    assert decide(800, True, 500, 950, 1000) == ('recovering', False)
    assert decide(800, False, 500, 950, 1000) == ('recovering', False)

def test_low_storage_does_not_restart_all_cameras():
    assert decide(800, True, 500, 0, 1000, False) == ('storage-low', False)

def test_latest_write_ignores_empty_symlink_and_unrelated_files(tmp_path):
    old=tmp_path/'2026-10-05_12-00-00.mkv';old.write_bytes(b'video');os.utime(old,(500,500))
    (tmp_path/'2026-10-05_13-00-00.mkv').touch()
    other=tmp_path/'unrelated';other.write_bytes(b'other')
    (tmp_path/'2026-10-05_14-00-00.mkv').symlink_to(other)
    assert latest_write(tmp_path)==500

def test_watchdog_restarts_only_stale_camera_and_preserves_footage(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    import app.recorder_watchdog as watchdog
    root=tmp_path/'recordings';state=tmp_path/'state';before={}
    for cam in watchdog.CAMERAS:
        folder=root/cam;folder.mkdir(parents=True)
        file=folder/'2026-10-05_12-00-00.mkv';file.write_bytes(b'original footage')
        timestamp=700 if cam=='cam03' else 980
        os.utime(file,(timestamp,timestamp));before[file]=(file.read_bytes(),file.stat().st_mtime_ns)
    calls=[]
    monkeypatch.setattr(watchdog.time,'time',lambda:1000)
    monkeypatch.setattr(watchdog,'unit_state',lambda cam:(True,500))
    monkeypatch.setattr(watchdog.shutil,'disk_usage',lambda root:SimpleNamespace(free=4*1024**3))
    monkeypatch.setattr(watchdog.subprocess,'run',lambda command,**kwargs:(calls.append(command) or SimpleNamespace(returncode=0)))
    watchdog.run(root,state)
    assert calls==[['/usr/bin/systemctl','restart','--no-block','cctv-cam03.service']]
    status=json.loads((state/'status.json').read_text())
    assert [r['camera'] for r in status['cameras'] if r['status']=='recovering']==['cam03']
    watchdog.run(root,state)
    assert len(calls)==1
    for file,identity in before.items():assert (file.read_bytes(),file.stat().st_mtime_ns)==identity
    file=root/'cam03'/'2026-10-05_12-00-00.mkv';os.utime(file,(990,990))
    watchdog.run(root,state)
    assert all(r['status']=='healthy' for r in json.loads((state/'status.json').read_text())['cameras'])
