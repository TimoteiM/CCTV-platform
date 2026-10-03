import json
from datetime import datetime
from zoneinfo import ZoneInfo
import httpx
import pytest
from app.frigate_events import FrigateIndex, split_event
from app.person_events import PersonEvents
from app.config import Settings

TZ=ZoneInfo('Europe/Bucharest')

def epoch(text):return datetime.fromisoformat(text).replace(tzinfo=TZ).timestamp()

def test_event_splits_local_midnight():
 start=epoch('2026-10-02T23:59:55')
 assert split_event(start,start+15)==[('2026-10-02',86395,86400),('2026-10-03',0,10)]

@pytest.mark.parametrize('start,end',[(True,2),(float('nan'),2),(1,float('inf')),(2,1),(1,8*86400)])
def test_invalid_event_timestamps(start,end):
 with pytest.raises(ValueError):split_event(start,end)

def test_import_filters_merges_and_updates_without_duplicates(tmp_path):
 now=epoch('2026-10-03T12:00:30')
 responses=[{'id':'one','camera':'cam01','label':'person','start_time':now-20,'end_time':None,'false_positive':False},
 {'id':'vehicle','camera':'cam01','label':'car','start_time':now-20,'end_time':now},
 {'id':'false','camera':'cam01','label':'person','start_time':now-20,'end_time':now,'false_positive':True},
 {'id':'bad','camera':'../../outside','label':'person','start_time':now-20,'end_time':now}]
 def handler(request):
  if request.url.path=='/api/stats':return httpx.Response(200,json={'cameras':{'cam01':{'camera_fps':3,'process_fps':3}}})
  return httpx.Response(200,json=responses)
 client=httpx.Client(transport=httpx.MockTransport(handler),base_url='http://127.0.0.1:5000')
 worker=FrigateIndex(tmp_path/'frigate',client=client,clock=lambda:now)
 assert worker.poll()=={'cam01'}
 now+=5
 worker.poll()
 assert worker.db.execute('SELECT count(*) FROM events').fetchone()[0]==1
 result=PersonEvents(tmp_path/'frigate').for_day('cam01','2026-10-03')
 assert result['person_events_source']=='frigate'
 assert result['person_events']==[{'start':43210,'end':43235}]
 assert result['person_analysis_intervals']==[{'start':43230,'end':43235}]
 responses[0]['end_time']=now-1
 now+=5;worker.poll()
 assert PersonEvents(tmp_path/'frigate').for_day('cam01','2026-10-03')['person_events'][0]['end']==43234
 worker.close()

def test_merge_preserves_historical_events(tmp_path):
 for folder,source,events in [('old','server',[{'type':'person','start':10,'end':20}]),('new','frigate',[{'type':'person','start':18,'end':30}])]:
  path=tmp_path/folder/'cam01';path.mkdir(parents=True)
  (path/'2026-10-03.json').write_text(json.dumps({'source':source,'events':events}))
 data=PersonEvents(tmp_path/'old',tmp_path/'new').for_day('cam01','2026-10-03')
 assert data['person_events']==[{'start':10,'end':30}]
 assert data['historical_person_events_source']=='server'
 assert data['person_events_source']=='frigate'

def test_frigate_unavailable_preserves_legacy(tmp_path):
 path=tmp_path/'old'/'cam01';path.mkdir(parents=True)
 (path/'2026-10-03.json').write_text(json.dumps({'source':'server','events':[{'type':'person','start':10,'end':20}]}))
 data=PersonEvents(tmp_path/'old',tmp_path/'absent').for_day('cam01','2026-10-03')
 assert data['person_events']==[{'start':10,'end':20}]
 assert data['frigate_events_state']=='not_indexed'

def test_config_uses_frigate_index_env(monkeypatch,tmp_path):
 monkeypatch.setenv('CCTV_FRIGATE_EVENTS_ROOT',str(tmp_path))
 assert Settings.from_env().frigate_events_root==tmp_path

def test_poll_outage_does_not_claim_analysis_coverage(tmp_path):
 now=epoch('2026-10-03T12:00:00')
 ready=True
 def handler(request):
  if request.url.path=='/api/stats':return httpx.Response(200,json={'cameras':{'cam01':{'camera_fps':3 if ready else 0,'process_fps':3}}})
  return httpx.Response(200,json=[])
 worker=FrigateIndex(tmp_path,httpx.Client(transport=httpx.MockTransport(handler),base_url='http://127.0.0.1:5000'),clock=lambda:now)
 worker.poll();now+=5;ready=False;worker.poll();now+=5;ready=True;worker.poll();now+=5;worker.poll()
 result=PersonEvents(tmp_path).for_day('cam01','2026-10-03')
 assert result['person_analysis_intervals']==[{'start':43210,'end':43215}]
 worker.close()


def test_fractional_epoch_split_always_advances():
 start=1791024267.157871
 result=split_event(start,start+5.2345678)
 assert len(result)==1 and result[0][2]>result[0][1]

def test_live_pressure_ignores_unrelated_host_load(monkeypatch,tmp_path):
 from app.live_engine import LiveManager
 from pathlib import Path
 monkeypatch.setattr('os.getloadavg',lambda:(100,100,100))
 def read(path,*args,**kwargs):
  if str(path)=='/proc/meminfo':return 'MemAvailable: 8388608 kB\n'
  return 'some avg10=99 avg60=99 avg300=99 total=1\nfull avg10=2 avg60=4 avg300=5 total=1\n'
 monkeypatch.setattr(Path,'read_text',read)
 manager=LiveManager(tmp_path/'live')
 assert not manager.pressure()
 def stalled(path,*args,**kwargs):
  if str(path)=='/proc/meminfo':return 'MemAvailable: 8388608 kB\n'
  return 'full avg10=99 avg60=99 avg300=99 total=1\n'
 monkeypatch.setattr(Path,'read_text',stalled)
 assert manager.pressure()

def test_frigate_connection_status_expires(tmp_path):
 import time
 folder=tmp_path/'cam01';folder.mkdir()
 (folder/'2026-10-03.json').write_text(json.dumps({'source':'frigate','events':[]}))
 (tmp_path/'status.json').write_text(json.dumps({'connected':True,'updated':time.time(),'cameras':['cam01']}))
 assert PersonEvents(None,tmp_path).for_day('cam01','2026-10-03')['frigate_connected']
 (tmp_path/'status.json').write_text(json.dumps({'connected':True,'updated':time.time()-60,'cameras':['cam01']}))
 assert not PersonEvents(None,tmp_path).for_day('cam01','2026-10-03')['frigate_connected']
