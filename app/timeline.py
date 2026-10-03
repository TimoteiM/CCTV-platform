"""Calendar-day coverage; cached durations refine conservative filename estimates."""
import math,re
from datetime import datetime
from fastapi import HTTPException
from .recordings import camera,day

def second(value):
 if not isinstance(value,str) or not re.fullmatch(r'[0-9]{2}:[0-9]{2}:[0-9]{2}',value):raise HTTPException(400,'Invalid time')
 try:t=datetime.strptime(value,'%H:%M:%S')
 except ValueError:raise HTTPException(400,'Invalid time') from None
 return t.hour*3600+t.minute*60+t.second

def coverage(store,provider,cam,date):
 camera(cam);selected=day(date);rows=store.list(cam,selected,with_identity=True,previous_day=True);segments=[]
 today=[row for row in rows if row['start'][:10]==selected.isoformat()]
 previous=[row for row in rows if row['start'][:10]<selected.isoformat()]
 rows=previous[-1:]+today
 if hasattr(provider,'schedule_duration_index'):provider.schedule_duration_index(cam,selected,rows)
 for index,row in enumerate(rows):
  start=int((datetime.fromisoformat(row['start'])-datetime.combine(selected,datetime.min.time())).total_seconds());duration=provider.known_duration(cam,row['id'],row['_identity']) if hasattr(provider,'known_duration') else None
  reliable=isinstance(duration,(float,int)) and math.isfinite(duration) and duration>0
  estimate=300
  if index+1<len(rows):
   spacing=(datetime.fromisoformat(rows[index+1]['start'])-datetime.fromisoformat(row['start'])).total_seconds()
   if 240<=spacing<=360:estimate=spacing
  end=min(86400,start+(duration if reliable else estimate))
  segments.append({'recording':row['id'],'start':start,'end':end,'estimated':not reliable})
 intervals=[]
 for segment in segments:
  if segment['end']<=0:continue
  if intervals and segment['start']<=intervals[-1]['end']+3:intervals[-1]['end']=max(intervals[-1]['end'],segment['end']);intervals[-1]['estimated']|=segment['estimated']
  else:intervals.append({'start':max(0,segment['start']),'end':segment['end'],'estimated':segment['estimated']})
 return {'camera':cam,'date':selected.isoformat(),'intervals':intervals,'segments':segments}

def locate(data,time):
 value=second(time)
 for segment in reversed(data['segments']):
  if segment['start']<=value<segment['end']:return segment,value-segment['start']
 raise HTTPException(404,'No recording at this time')

def following(data,current,duration=None):
 segments=data['segments']
 for i,segment in enumerate(segments):
  if segment['recording']==current and i+1<len(segments):
   next=segments[i+1];end=segment['start']+duration if duration is not None else segment['end']
   return next if abs(next['start']-end)<=3 else None
 return None
