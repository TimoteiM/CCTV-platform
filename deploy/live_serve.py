"""Private UDS listener; deny all subsequent bind calls before starting ASGI/FFmpeg."""
import os,socket,sys,ctypes,ctypes.util
sys.path.insert(0,'/opt/cctv-web')
import uvicorn
path='/run/cctv-live/api.sock'
try:os.unlink(path)
except FileNotFoundError:pass
listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);listener.bind(path);os.chmod(path,0o660);listener.listen(128)
lib=ctypes.CDLL(ctypes.util.find_library('seccomp'));lib.seccomp_init.argtypes=[ctypes.c_uint32];lib.seccomp_init.restype=ctypes.c_void_p;lib.seccomp_syscall_resolve_name.argtypes=[ctypes.c_char_p];lib.seccomp_rule_add.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_int,ctypes.c_uint];lib.seccomp_load.argtypes=[ctypes.c_void_p];lib.seccomp_release.argtypes=[ctypes.c_void_p]
context=lib.seccomp_init(0x7fff0000)
if not context or lib.seccomp_rule_add(context,0x00050001,lib.seccomp_syscall_resolve_name(b'bind'),0)!=0 or lib.seccomp_load(context)!=0:raise RuntimeError('Live socket restrictions unavailable')
lib.seccomp_release(context)
# Test that a new listener cannot bind; camera TCP connect remains allowed.
s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
try:
 try:s.bind(('127.0.0.1',0))
 except PermissionError:pass
 else:raise RuntimeError('Live socket restrictions not enforced')
finally:s.close()
uvicorn.Server(uvicorn.Config('app.live_engine:app',workers=1,access_log=False,proxy_headers=False,log_config='/opt/cctv-web/deploy/logging.json',timeout_graceful_shutdown=15,limit_concurrency=64)).run(sockets=[listener])
