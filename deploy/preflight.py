"""Check the effective systemd sandbox without writing or changing any original."""
import errno
import os
import socket
import sys
from pathlib import Path
sys.path.insert(0, '/opt/cctv-web')
from app.config import Settings, CAMERAS
from app.recordings import Store


def require(ok, message):
    if not ok:raise RuntimeError(message)


def main():
    settings=Settings.from_env()
    require(os.geteuid()!=0,'Service must run unprivileged')
    require(os.statvfs(settings.recordings_root).f_flag & os.ST_RDONLY,'Source mount must be read-only')
    require(os.statvfs('/opt/cctv-web/app').f_flag & os.ST_RDONLY,'Code mount must be read-only')
    require(not os.statvfs(settings.cache_root).f_flag & os.ST_RDONLY,'Cache mount must be writable')
    require(not os.access(settings.recordings_root,os.W_OK),'Source directory must not be writable')
    require(not os.access('/opt/cctv-web/app/main.py',os.W_OK),'Source code must not be writable')
    store=Store(settings)
    for cam in CAMERAS:
        with store.directory(cam):pass
        require(not os.access(settings.recordings_root/cam,os.W_OK),'Camera directory must not be writable')
    for path in ['/etc/cctv','/etc/nginx','/etc/letsencrypt','/root']:
        require(not os.access(path,os.R_OK|os.X_OK),'Sensitive directories must be inaccessible')
    require(not os.path.exists('/proc/1'),'Other-user processes must be hidden')
    for folder in [settings.cache_root,Path('/run/cctv-web')]:
        path=folder/'.preflight-write-check'
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:os.write(fd,b'check')
        finally:os.close(fd);path.unlink()
    for family in (socket.AF_INET,socket.AF_INET6):
        try:test=socket.socket(family,socket.SOCK_STREAM)
        except OSError as error:
            require(error.errno in (errno.EAFNOSUPPORT,errno.EACCES,errno.EPERM),'Unexpected network-family denial')
        else:
            test.close()
            raise RuntimeError('New IP sockets must be prohibited')
    print('Preflight passed: unprivileged; all eight camera directories readable; source/code read-only; cache/runtime writable; sensitive directories/processes hidden; new IPv4/IPv6 sockets prohibited.',flush=True)

if __name__=='__main__':
    try:main()
    except Exception as error:
        # Controlled reasons only; no source paths, credentials, or traceback.
        message=str(error) if isinstance(error,RuntimeError) else type(error).__name__
        print('Service preflight failed: '+message,file=sys.stderr,flush=True)
        sys.exit(1)
