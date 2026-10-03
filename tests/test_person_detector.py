import json
import os
import time
from pathlib import Path
import pytest
from app.config import Settings,CAMERAS
from app.person_detector import DetectionIndex,samples_to_events
from app.person_events import PersonEvents

class Analyzer:
    step=2;threshold=.55
    def __init__(self,fail=False,change=None):self.fail=fail;self.change=change
    def analyze(self,fd):
        assert os.read(fd,8)==b'original'
        if self.change:self.change()
        if self.fail:raise RuntimeError('private internal path')
        return 300,[{'type':'person','start':10,'end':20,'confidence':.8}]
    def cancel(self):pass

@pytest.fixture
def worker(tmp_path):
    source=tmp_path/'source';source.mkdir()
    for cam in CAMERAS:
        folder=source/cam;folder.mkdir()
        for name in ['2026-10-01_23-58-00.mkv','2026-10-02_12-00-00.mkv','2026-10-02_12-05-00.mkv']:
            file=folder/name;file.write_bytes(b'original');os.utime(file,(time.time()-1200,time.time()-1200))
    root=tmp_path/'events';root.mkdir();(root/'requests').mkdir()
    settings=Settings(recordings_root=source,cache_root=tmp_path/'cache')
    index=DetectionIndex(settings,root,Analyzer())
    yield index
    index.close()

def test_sample_intervals_exclude_low_confidence_and_merge():
    events=samples_to_events([(0,.8),(2,.7),(4,.2),(6,.8),(20,.9)],25)
    assert [(item['start'],item['end']) for item in events]==[(0,7),(19,21)]
    assert events[0]['confidence']==.8

def test_atomic_index_and_resume_skip(worker):
    assert worker.process('cam01','2026-10-02_12-00-00.mkv')
    result=PersonEvents(worker.root).for_day('cam01','2026-10-02')
    assert result['person_events']==[{'start':43210,'end':43220}]
    assert result['person_analysis_intervals']==[{'start':43200,'end':43500}]
    assert ('cam01','2026-10-02_12-00-00.mkv') not in worker.candidates()
    assert not list((worker.root/'cam01').glob('*.tmp'))
    assert (worker.settings.recordings_root/'cam01'/'2026-10-02_12-00-00.mkv').read_bytes()==b'original'

def test_requested_camera_and_timestamp_prioritized(worker):
    feed=PersonEvents(worker.root)
    assert feed.request('cam08','2026-10-02',43260)
    assert worker.candidates()[0]==('cam08','2026-10-02_12-00-00.mkv')
    assert len(list((worker.root/'requests').glob('*.json')))==1
    assert feed.request('cam08','2026-10-01',86399)
    assert worker.candidates()[0]==('cam08','2026-10-01_23-58-00.mkv')

def test_failed_analysis_is_not_indexed_as_no_people(worker):
    worker.analyzer=Analyzer(fail=True)
    assert not worker.process('cam01','2026-10-02_12-00-00.mkv')
    assert PersonEvents(worker.root).for_day('cam01','2026-10-02')['person_events_state']=='not_indexed'
    assert ('cam01','2026-10-02_12-00-00.mkv') not in worker.candidates()

def test_changed_recording_not_published(worker):
    file=worker.settings.recordings_root/'cam01'/'2026-10-02_12-00-00.mkv'
    worker.analyzer=Analyzer(change=lambda:file.write_bytes(b'changed'))
    assert not worker.process('cam01',file.name)
    assert not (worker.root/'cam01').exists()

def test_retention_removes_detection_access(worker):
    name='2026-10-02_12-00-00.mkv'
    worker.process('cam01',name)
    (worker.settings.recordings_root/'cam01'/name).unlink()
    worker.last_cleanup=0;worker.candidates()
    data=PersonEvents(worker.root).for_day('cam01','2026-10-02')
    assert data['person_events']==[] and data['person_analysis_intervals']==[]

def test_midnight_recording_splits_analysis_days(worker):
    worker.analyzer.analyze=lambda fd:(300,[{'type':'person','start':100,'end':160,'confidence':.8}])
    worker.process('cam01','2026-10-01_23-58-00.mkv')
    today=PersonEvents(worker.root).for_day('cam01','2026-10-01')
    tomorrow=PersonEvents(worker.root).for_day('cam01','2026-10-02')
    assert today['person_events']==[{'start':86380,'end':86400}]
    assert tomorrow['person_events']==[{'start':0,'end':40}]
    assert tomorrow['person_analysis_intervals']==[{'start':0,'end':180}]

def test_worker_lock_excludes_second_detector(worker):
    with pytest.raises(BlockingIOError):DetectionIndex(worker.settings,worker.root,Analyzer())


def test_priority_mailbox_readable_under_private_web_umask(worker):
    previous=os.umask(0o077)
    try:assert PersonEvents(worker.root).request('cam08','2026-10-02',43260)
    finally:os.umask(previous)
    assert (worker.root/'requests'/'cam08.json').stat().st_mode & 0o777==0o640
    assert worker.requests()['cam08']['time']==43260


def test_recent_cameras_take_turns_instead_of_one_camera_starving_others(worker):
    for cam in CAMERAS:
        for file in (worker.settings.recordings_root/cam).iterdir():
            os.utime(file,(time.time()-120,time.time()-120))
    file=worker.settings.recordings_root/'cam05'/'2026-10-02_12-00-00.mkv'
    os.utime(file,(time.time()-90,time.time()-90))
    assert worker.candidates()[0]==('cam05',file.name)
    assert worker.process('cam05',file.name)
    assert worker.candidates()[0][0]!='cam05'
