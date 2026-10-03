import json
import os
from dataclasses import dataclass, field
from pathlib import Path

CAMERAS = tuple(f'cam{i:02}' for i in range(1, 9))

@dataclass(frozen=True)
class Settings:
    recordings_root: Path = Path('/srv/cctv')
    minimum_age: int = 60
    exclude_newest: bool = True
    download_mode: str = 'local'
    cache_root: Path = Path('/opt/cctv-web/var/cache')
    cache_max_age: int = 21600
    cache_max_bytes: int = 2 * 1024**3
    playback_queue_size: int = 3
    conversion_timeout: int = 900
    person_events_root: Path | None = None
    camera_names: dict = field(default_factory=lambda: {c: f'Acasă {i}' for i,c in enumerate(CAMERAS,1)})

    def __post_init__(self):
        if self.minimum_age < 60 or self.download_mode not in ('local', 'nginx'):
            raise ValueError('Invalid application configuration')
        source = self.recordings_root.resolve()
        cache = self.cache_root.resolve()
        protected = (source, Path('/srv/cctv').resolve())
        if any(cache == root or cache in root.parents or root in cache.parents for root in protected) or len(cache.parts) < 3:
            raise ValueError('Playback cache must be separate from recordings')
        if self.cache_max_age <= 0 or self.cache_max_bytes <= 0 or not 1 <= self.playback_queue_size <= 16 or self.conversion_timeout <= 0:
            raise ValueError('Invalid playback limits')
        if set(self.camera_names) != set(CAMERAS):
            raise ValueError('Configure exactly cam01 through cam08')
        if any(not isinstance(n, str) or not n.strip() for n in self.camera_names.values()):
            raise ValueError('Camera names must be nonempty strings')

    @classmethod
    def from_env(cls):
        names_path = Path(os.getenv('CCTV_CAMERA_NAMES', '/opt/cctv-web/cameras.json'))
        names = json.loads(names_path.read_text()) if names_path.exists() else {c: f'Acasă {i}' for i,c in enumerate(CAMERAS,1)}
        return cls(person_events_root=Path(os.environ['CCTV_PERSON_EVENTS_ROOT']) if os.getenv('CCTV_PERSON_EVENTS_ROOT') else None, recordings_root=Path(os.getenv('CCTV_RECORDINGS_ROOT', '/srv/cctv')), minimum_age=int(os.getenv('CCTV_MINIMUM_AGE', '60')), download_mode=os.getenv('CCTV_DOWNLOAD_MODE', 'local'), camera_names=names, cache_root=Path(os.getenv('CCTV_CACHE_ROOT', '/opt/cctv-web/var/cache')), cache_max_age=int(os.getenv('CCTV_CACHE_MAX_AGE', '21600')), cache_max_bytes=int(os.getenv('CCTV_CACHE_MAX_BYTES', str(2 * 1024**3))), playback_queue_size=int(os.getenv('CCTV_PLAYBACK_QUEUE_SIZE', '3')), conversion_timeout=int(os.getenv('CCTV_CONVERSION_TIMEOUT', '900')))
