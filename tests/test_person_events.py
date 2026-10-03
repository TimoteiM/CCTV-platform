import json
from dataclasses import replace
from pathlib import Path
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.person_events import PersonEvents
from app.main import create_app
from app.config import Settings
from app.playback import PendingPlayback

@pytest.fixture
def events(tmp_path):
    root=tmp_path/'events';folder=root/'cam01';folder.mkdir(parents=True)
    file=folder/'2026-10-02.json'
    file.write_text(json.dumps({'source':'camera','events':[
        {'type':'person','start':43258,'end':43263},
        {'type':'person','start':43260,'end':43270},
        {'type':'motion','start':40000,'end':50000},
        {'type':'person','start':80000,'end':80002}]}))
    return PersonEvents(root),file

def test_only_people_and_overlapping_intervals(events):
    result=events[0].for_day('cam01','2026-10-02')
    assert result=={'person_events':[{'start':43258,'end':43270},{'start':80000,'end':80002}], 'person_events_state':'available','person_events_source':'camera'}

def test_no_feed_is_distinct_from_no_detections(events):
    assert PersonEvents().for_day('cam01','2026-10-02')['person_events_state']=='not_connected'
    assert events[0].for_day('cam02','2026-10-02')['person_events_state']=='not_indexed'
    events[1].write_text('{"source":"camera","events":[]}')
    assert events[0].for_day('cam01','2026-10-02')['person_events_state']=='available'

@pytest.mark.parametrize('start,end',[(True,2),(-1,2),(1,86401),(2,1),(float('nan'),2)])
def test_bad_event_times_never_become_markers(events,start,end):
    events[1].write_text(json.dumps({'source':'camera','events':[{'type':'person','start':start,'end':end}]}))
    assert events[0].for_day('cam01','2026-10-02')['person_events_state']=='unavailable'

@pytest.mark.parametrize('cam,date',[('../cam01','2026-10-02'),('cam09','2026-10-02'),('cam01','../../secret')])
def test_event_path_validation(events,cam,date):
    with pytest.raises(HTTPException):events[0].for_day(cam,date)

def test_event_index_symlinks_not_followed(events,tmp_path):
    file=events[1];file.unlink();secret=tmp_path/'secret';secret.write_text('{"source":"camera","events":[]}');file.symlink_to(secret)
    assert events[0].for_day('cam01','2026-10-02')['person_events_state']=='unavailable'

def test_timeline_api_reports_real_event_index(events,tmp_path):
    recordings=tmp_path/'recordings';(recordings/'cam01').mkdir(parents=True)
    settings=Settings(recordings_root=recordings, cache_root=tmp_path/'cache',person_events_root=events[0].root)
    response=TestClient(create_app(settings,PendingPlayback())).get('/api/timeline/cam01?date=2026-10-02')
    assert response.status_code==200
    assert response.json()['person_events'][0]=={'start':43258,'end':43270}
