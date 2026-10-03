import os
import socket
import pytest
from deploy.serve import inherited_listener

class Listener:
    def __init__(self,address=('127.0.0.1',8080),family=socket.AF_INET):
        self.family=family;self.type=socket.SOCK_STREAM;self.address=address;self.closed=False
    def getsockname(self):return self.address
    def close(self):self.closed=True

@pytest.mark.parametrize('pid,fds',[('0','1'),('self','0'),('self','2')])
def test_launcher_requires_one_systemd_socket(monkeypatch,pid,fds):
    monkeypatch.setenv('LISTEN_PID',str(os.getpid()) if pid=='self' else pid)
    monkeypatch.setenv('LISTEN_FDS',fds)
    with pytest.raises(RuntimeError):inherited_listener()

@pytest.mark.parametrize('address,family',[(('0.0.0.0',8080),socket.AF_INET),(('127.0.0.1',8081),socket.AF_INET),(('::1',8080),socket.AF_INET6)])
def test_launcher_rejects_other_bindings(monkeypatch,address,family):
    monkeypatch.setenv('LISTEN_PID',str(os.getpid()));monkeypatch.setenv('LISTEN_FDS','1')
    fake=Listener(address,family)
    monkeypatch.setattr('deploy.serve.socket.socket',lambda **kwargs:fake)
    with pytest.raises(RuntimeError):inherited_listener()
    assert fake.closed

def test_launcher_uses_existing_fd_without_new_ip_socket(monkeypatch):
    monkeypatch.setenv('LISTEN_PID',str(os.getpid()));monkeypatch.setenv('LISTEN_FDS','1')
    fake=Listener();calls=[]
    def factory(**kwargs):calls.append(kwargs);return fake
    monkeypatch.setattr('deploy.serve.socket.socket',factory)
    assert inherited_listener() is fake
    assert calls==[{'family':-1,'type':-1,'proto':-1,'fileno':3}]
    assert 'LISTEN_PID' not in os.environ and 'LISTEN_FDS' not in os.environ
