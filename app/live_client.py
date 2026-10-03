"""Private Unix-socket client; the web app receives no RTSP credentials."""
import httpx
from fastapi import HTTPException
from .recordings import camera
class LiveClient:
 def call(self,path,method='GET',data=None):
  try:
   with httpx.Client(transport=httpx.HTTPTransport(uds='/run/cctv-live/api.sock'),base_url='http://live',timeout=3) as client:r=client.request(method,path,json=data)
   if r.status_code>=400:raise HTTPException(r.status_code,'Live stream unavailable' if r.status_code!=429 else 'Server live capacity reached')
   return r.json()
  except httpx.HTTPError:raise HTTPException(503,'Live stream unavailable') from None
