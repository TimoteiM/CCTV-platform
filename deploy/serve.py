"""One ASGI process accepting only the verified systemd loopback socket."""
import os
import socket
import sys
from pathlib import Path
import uvicorn


def inherited_listener():
    if os.environ.get('LISTEN_PID')!=str(os.getpid()) or os.environ.get('LISTEN_FDS')!='1':
        raise RuntimeError('Exactly one systemd listener is required')
    # Autodetect the existing FD's real family; no new IP socket is created.
    listener=socket.socket(family=-1,type=-1,proto=-1,fileno=3)
    if listener.family!=socket.AF_INET or listener.type!=socket.SOCK_STREAM or listener.getsockname()!=('127.0.0.1',8080):
        listener.close()
        raise RuntimeError('Listener must be IPv4 loopback port 8080')
    for name in ('LISTEN_PID','LISTEN_FDS','LISTEN_FDNAMES'):os.environ.pop(name,None)
    return listener


def main():
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
    listener=inherited_listener()
    config=uvicorn.Config('app.main:app',host='127.0.0.1',port=8080,workers=1,
                          access_log=False,proxy_headers=True,forwarded_allow_ips='127.0.0.1',
                          log_config='/opt/cctv-web/deploy/logging.json',
                          limit_concurrency=64,timeout_keep_alive=5,
                          timeout_graceful_shutdown=20)
    try:uvicorn.Server(config).run(sockets=[listener])
    finally:listener.close()

if __name__=='__main__':main()
