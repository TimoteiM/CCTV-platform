"""Generate root-only Frigate configuration; never print camera credentials."""
import ipaddress
import json
import os
import shlex
from pathlib import Path
from urllib.parse import quote


def provision(source=Path('/etc/cctv/cameras.env'), destination=Path('/var/lib/cctv-frigate/config/config.yml')):
    values = {}
    for line in source.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            values[key.strip()] = shlex.split(value)[0]
    host = str(ipaddress.ip_address(values['HOME_PUBLIC_IP']))
    user, password = (quote(values[key], safe='') for key in ('CAM_USER', 'CAM_PASS'))
    template = Path(__file__).with_name('config.example.yaml').read_text().split('cameras:\n')[0]
    cameras = {}
    for index in range(1, 9):
        cameras[f'cam{index:02}'] = {'ffmpeg': {'inputs': [{
            'path': f'rtsp://{user}:{password}@{host}:{15540+index}/mpeg4cif',
            'input_args': 'preset-rtsp-generic', 'roles': ['detect']}]}}
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.parent.chmod(0o700)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(template + 'cameras: ' + json.dumps(cameras) + '\n')


if __name__ == '__main__':
    provision()
    print('Frigate configuration provisioned for eight cameras (private).')
