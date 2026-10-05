import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.config import CAMERAS, Settings
from app.event_catalog import EventCatalog, event_token, event_metadata
from app.frigate_events import FrigateIndex
from app.main import create_app
from app.playback import PendingPlayback

TZ=ZoneInfo('Europe/Bucharest')
JPEG=b'\xff\xd8\xffsmall-fixture\xff\xd9'

def epoch(text):return datetime.fromisoformat(text).replace(tzinfo=TZ).timestamp()

def row(identifier='person-event',start=epoch('2026-10-05T10:01:00'),end=None):
 return {'token':event_token(identifier),'start':start,'end':end or start+12,'point':36060,'active':False,'confidence':.83,'zones':['entrance']}

@pytest.fixture
def setup(tmp_path):
 root=tmp_path/'index';root.mkdir()
 recordings=tmp_path/'recordings';recordings.mkdir()
 for cam in CAMERAS:
  (root/cam).mkdir();(recordings/cam).mkdir()
 for start in ['10-00-00','10-05-00']:
  path=recordings/'cam01'/('2026-10-05_'+start+'.mkv');path.write_bytes(b'original');os.utime(path,(time.time()-600,time.time()-600))
 (root/'cam01'/'2026-10-05.events.json').write_text(json.dumps({'version':1,'events':[row()]}))
 (root/'status.json').write_text(json.dumps({'connected':True,'updated':time.time(),'cameras':list(CAMERAS)}))
 settings=Settings(recordings_root=recordings,cache_root=tmp_path/'cache',frigate_events_root=root,exclude_newest=False)
 with TestClient(create_app(settings,PendingPlayback())) as client:yield root,client

def test_event_page_and_playback_link(setup):
 root,client=setup
 response=client.get('/events?cam=cam01&date=2026-10-05')
 assert response.status_code==200 and 'aria-current="page"' in response.text and 'id="events-grid"' in response.text
 data=client.get('/api/events?date=2026-10-05').json()
 assert data['total']==1 and data['status']['connected']
 event=data['events'][0]
 assert event['camera']=='cam01' and event['confidence']==.83 and event['duration']==12
 assert event['recording_available'] and event['playback_url']=='/playback?cam=cam01&date=2026-10-05&t=10:00:57'
 assert 'rtsp://' not in json.dumps(data)
 assert client.get(event['playback_url']).status_code==200

@pytest.mark.parametrize('query,status',[('date=../private',400),('date=2026-02-30',400),('date=2026-10-05&cam=cam09',404),('date=2026-10-05&offset=-1',422),('date=2026-10-05&limit=1000',422)])
def test_event_query_validation(setup,query,status):
 assert setup[1].get('/api/events?'+query).status_code==status

def test_pagination_and_camera_filter(setup):
 root,client=setup
 events=[row(str(i),epoch('2026-10-05T10:01:00')+i) for i in range(27)]
 (root/'cam01'/'2026-10-05.events.json').write_text(json.dumps({'version':1,'events':events}))
 first=client.get('/api/events?date=2026-10-05&cam=cam01').json()
 second=client.get('/api/events?date=2026-10-05&cam=cam01&offset=24').json()
 assert first['total']==27 and len(first['events'])==24 and first['next_offset']==24
 assert len(second['events'])==3 and second['next_offset'] is None
 assert not set(e['id'] for e in first['events']) & set(e['id'] for e in second['events'])
 assert client.get('/api/events?date=2026-10-05&cam=cam02').json()['total']==0

def test_thumbnail_no_follow_mime_size_and_expiration(setup):
 root,client=setup
 folder=root/'thumbnails';folder.mkdir()
 token=event_token('person-event');path=folder/(token+'.jpg');path.write_bytes(JPEG)
 response=client.get('/event-media/'+token+'.jpg')
 assert response.status_code==200 and response.content==JPEG and response.headers['content-type']=='image/jpeg'
 assert response.headers['cache-control']=='no-store'
 head=client.head('/event-media/'+token+'.jpg');assert head.status_code==200 and head.content==b'' and int(head.headers['content-length'])==len(JPEG)
 path.unlink();outside=root.parent/'outside';outside.write_bytes(JPEG);path.symlink_to(outside)
 assert client.get('/event-media/'+token+'.jpg').status_code==404
 path.unlink();path.write_bytes(b'<svg>script</svg>')
 assert client.get('/event-media/'+token+'.jpg').status_code==404
 path.write_bytes(JPEG);os.utime(path,(time.time()-86500,time.time()-86500))
 assert client.get('/event-media/'+token+'.jpg').status_code==404
 path.write_bytes(b'x'*(512*1024+1))
 assert client.get('/event-media/'+token+'.jpg').status_code==404

@pytest.mark.parametrize('token',['..','not-a-token','a'*63,'A'*64])
def test_thumbnail_identifier_validation(setup,token):
 assert setup[1].get('/event-media/'+token+'.jpg').status_code==404

def test_malformed_catalog_is_reported_unavailable(setup):
 root,client=setup
 (root/'cam01'/'2026-10-05.events.json').write_text(json.dumps({'version':1,'events':[{**row(),'point':float('nan')}]}))
 assert client.get('/api/events?date=2026-10-05').json()['catalog_state']=='unavailable'

def test_catalog_symlink_cannot_read_outside(setup):
 root,client=setup
 path=root/'cam01'/'2026-10-05.events.json';path.unlink()
 outside=root.parent/'private.json';outside.write_text(json.dumps({'version':1,'events':[row()]}));path.symlink_to(outside)
 assert client.get('/api/events?date=2026-10-05').json()['events']==[]

def test_missing_recording_is_disabled(setup):
 root,client=setup
 for path in (root.parent/'recordings'/'cam01').glob('*.mkv'):path.unlink()
 event=client.get('/api/events?date=2026-10-05').json()['events'][0]
 assert event['playback_url'] is None and not event['recording_available']

@pytest.mark.parametrize('identifier',['../private','/etc/passwd','..','person?x=1','person\n'])
def test_raw_frigate_identifier_validation(identifier):
 with pytest.raises(ValueError):event_token(identifier)

def test_metadata_only_exports_valid_confidence_and_zones():
 data=event_metadata({'id':'valid-event','data':{'score':float('nan'),'top_score':True},'zones':['gate',3], 'has_snapshot':True})
 assert data['confidence'] is None and data['zones']==['gate'] and data['has_snapshot']


def test_importer_exports_real_metadata_and_caches_bounded_jpeg(tmp_path):
 now=epoch('2026-10-05T10:01:30')
 event={'id':'valid-person','camera':'cam01','label':'person','start_time':now-30,'end_time':now-10,'has_snapshot':True,'data':{'top_score':.91},'zones':['front-gate']}
 calls=[]
 def handler(request):
  calls.append(request.url.path)
  if request.url.path.endswith('/thumbnail.jpg'):return httpx.Response(200,content=JPEG,headers={'content-type':'image/jpeg'})
  if request.url.path=='/api/stats':return httpx.Response(200,json={'cameras':{'cam01':{'camera_fps':2,'process_fps':2}}})
  return httpx.Response(200,json=[event])
 client=httpx.Client(base_url='http://127.0.0.1:5000',transport=httpx.MockTransport(handler))
 worker=FrigateIndex(tmp_path,client,clock=lambda:now);worker.poll()
 catalog=EventCatalog(tmp_path,clock=lambda:now)
 rows,state=catalog.for_day('cam01','2026-10-05')
 assert state=='available' and rows[0]['confidence']==.91 and rows[0]['zones']==['front-gate']
 assert rows[0]['thumbnail_state']=='available'
 assert catalog.thumbnail(event_token(event['id']))==JPEG
 worker.poll();assert calls.count('/api/events/valid-person/thumbnail.jpg')==1
 worker.close()


def test_existing_database_migrates_without_losing_events(tmp_path):
 import sqlite3
 db=sqlite3.connect(tmp_path/'events.sqlite')
 db.execute('CREATE TABLE events (id TEXT PRIMARY KEY,camera TEXT,start REAL,end REAL,active INTEGER)')
 db.execute('INSERT INTO events VALUES (?,?,?,?,?)',('old','cam01',epoch('2026-10-05T10:00:00'),epoch('2026-10-05T10:00:10'),0));db.commit();db.close()
 worker=FrigateIndex(tmp_path,httpx.Client(base_url='http://127.0.0.1:5000'),clock=lambda:epoch('2026-10-05T12:00:00'))
 assert worker.db.execute('SELECT count(*) FROM events').fetchone()[0]==1
 worker.publish(worker.clock())
 rows,state=EventCatalog(tmp_path).for_day('cam01','2026-10-05');assert state=='available' and len(rows)==1 and rows[0]['confidence'] is None
 worker.close()
