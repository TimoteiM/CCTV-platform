import os,time
from pathlib import Path
from fastapi.testclient import TestClient
from fastapi import HTTPException
import pytest
from app.config import Settings
from app.instant_playback import InstantPlayback
from app.main import create_app

class Process:
 def poll(self):return None
class Provider:
 def start(self):pass
 def close(self):pass
 def known_duration(self,*args):return 300
 def recording_duration(self,*args):return 300

@pytest.fixture
def setup(tmp_path,monkeypatch):
 folder=tmp_path/'recordings'/'cam01';folder.mkdir(parents=True)
 for name in ['2026-10-01_10-00-00.mkv','2026-10-01_10-05-00.mkv','2026-10-01_10-12-00.mkv']:
  path=folder/name;path.write_bytes(b'original');os.utime(path,(time.time()-600,time.time()-600))
 settings=Settings(recordings_root=folder.parent,cache_root=tmp_path/'cache',exclude_newest=False)
 manager=InstantPlayback(settings);manager.terminate=lambda process:None
 calls=[]
 def spawn(fd,folder,offset,duration,*,sources=None):
  for handle,length in sources:assert os.pread(handle,8,0)==b'original'
  calls.append((len(sources),offset,duration));(folder/'index.m3u8').write_text('#EXTM3U\n')
  return Process()
 manager.spawn=spawn;monkeypatch.setattr('app.main.InstantPlayback',lambda settings:manager)
 with TestClient(create_app(settings,Provider())) as client:yield client,manager,folder,calls

def test_window_has_back_buffer_and_multiple_segments_but_stops_at_gap(setup):
 client,manager,folder,calls=setup
 response=client.post('/api/timeline/cam01/seek?date=2026-10-01&time=10:01:00&stream=true&window=true')
 assert response.status_code==200
 data=response.json();assert data['segment_start']==36050 and data['offset']==10
 assert data['window_end']==36600 and len(data['segments'])==2
 assert calls==[(2,50,550)]
 assert client.delete('/api/instant/'+data['stream_id']).status_code==200
 assert len(list(folder.glob('*.mkv')))==3

def test_change_to_any_source_revokes_window(setup):
 client,manager,folder,calls=setup
 data=client.post('/api/timeline/cam01/seek?date=2026-10-01&time=10:01:00&stream=true&window=true').json()
 (folder/'2026-10-01_10-05-00.mkv').unlink()
 assert client.get(data['media_url']).status_code==404
 assert not manager.sessions

def test_calendar_latest_and_url_alias(setup):
 client,manager,folder,calls=setup
 data=client.get('/api/camera/cam01/calendar').json();assert data['days']==['2026-10-01']
 assert data['latest']=={'date':'2026-10-01','time':'10:16:30'}
 page=client.get('/playback?cam=cam01&date=2026-10-01&t=10:15:45')
 assert page.status_code==200 and 'data-time="10:15:45"' in page.text
 assert 'id="nvr-video" playsinline muted' in page.text
 assert client.get('/playback?t=25:00:00').status_code==400

def test_export_rejects_gaps_and_unbounded_work(setup):
 client,manager,folder,calls=setup
 assert client.get('/api/timeline/cam01/export?date=2026-10-01&start=10:09:00&end=10:13:00').status_code==400
 assert client.get('/api/timeline/cam01/export?date=2026-10-01&start=10:00:00&end=11:00:00').status_code==400
 assert client.get('/api/timeline/cam01/export?date=2026-10-01&start=10:00:00&end=09:00:00').status_code==400
 assert not manager.sessions

def test_window_gap_seeks_do_not_start_jobs(setup):
 client,manager,folder,calls=setup
 assert client.post('/api/timeline/cam01/seek?date=2026-10-01&time=10:11:00&stream=true&window=true').status_code==404
 assert not manager.sessions

def test_back_buffer_cannot_exclude_the_requested_next_file(setup):
 client,manager,folder,calls=setup
 data=client.post('/api/timeline/cam01/seek?date=2026-10-01&time=10:05:01&stream=true&window=true&window_seconds=300').json()
 assert data['state']=='ready'
 assert len(data['segments'])==2
 assert data['segment_start']==36291 and data['window_end']==36600
 assert data['duration']>data['offset']+200

def test_window_clips_at_midnight_for_next_day_handoff(setup):
 client,manager,folder,calls=setup
 path=folder/'2026-10-01_23-57-00.mkv';path.write_bytes(b'original');os.utime(path,(time.time()-600,time.time()-600))
 data=client.post('/api/timeline/cam01/seek?date=2026-10-01&time=23:59:58&stream=true&window=true').json()
 assert data['window_end']==86400 and data['duration']==12
 assert data['segments'][0]['duration']==180

def test_window_length_is_bounded(setup):
 client,manager,folder,calls=setup
 for value in [1,299,2401,999999]:
  assert client.post(f'/api/timeline/cam01/seek?date=2026-10-01&time=10:01:00&stream=true&window=true&window_seconds={value}').status_code==400
 assert not manager.sessions


def test_optional_prefetch_defers_when_sessions_are_full(setup):
 client,manager,folder,calls=setup
 query='/api/timeline/cam01/seek?date=2026-10-01&time=10:01:00&stream=true&window=true'
 for _ in range(2):assert client.post(query).status_code==200
 response=client.post(query+'&prefetch=true')
 assert response.status_code==200 and response.json()=={'state':'deferred'}
 assert len(manager.sessions)==2 and len(calls)==2
 assert client.post(query).status_code==429

def test_export_validation_reports_busy_and_recovers(setup):
 client,manager,folder,calls=setup
 tokens=[]
 for _ in range(2):
  data=client.post('/api/timeline/cam01/seek?date=2026-10-01&time=10:01:00&stream=true&window=true').json()
  tokens.append(data['stream_id'])
 url='/api/timeline/cam01/export/validate?date=2026-10-01&start=10:01:00&end=10:01:06'
 response=client.post(url)
 assert response.status_code==200 and response.json()['state']=='busy'
 client.delete('/api/instant/'+tokens.pop())
 assert client.post(url).json()['state']=='ready'
