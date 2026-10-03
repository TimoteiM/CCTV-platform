"""Bounded local media delivery with single byte-range and HEAD support."""
import os
import re
from fastapi.responses import Response, StreamingResponse

class SafeStreamingResponse(StreamingResponse):
    def __init__(self, content, *, file, **kwargs):
        self.file=file
        super().__init__(content,**kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope,receive,send)
        finally:
            self.file.close()

def media_response(request, fd, size, filename):
    headers={'Accept-Ranges':'bytes','Content-Type':'video/mp4','Content-Disposition':f'inline; filename="{filename}"'}
    start,end,status=0,size-1,200
    value=request.headers.get('range')
    if value:
        match=re.fullmatch(r'bytes=([0-9]{0,20})-([0-9]{0,20})',value)
        try:
            if not match or not any(match.groups()):raise ValueError()
            left,right=match.groups()
            if left:
                start=int(left);end=min(int(right),size-1) if right else size-1
            else:
                suffix=int(right)
                if suffix<=0:raise ValueError()
                start=max(0,size-suffix)
            if start>end or start>=size:raise ValueError()
        except ValueError:
            os.close(fd)
            return Response(status_code=416,headers={**headers,'Content-Range':f'bytes */{size}'})
        status=206
        headers['Content-Range']=f'bytes {start}-{end}/{size}'
    headers['Content-Length']=str(end-start+1)
    if request.method=='HEAD':
        os.close(fd)
        return Response(status_code=status,headers=headers)
    file=os.fdopen(fd,'rb')
    def chunks():
        try:
            file.seek(start)
            remaining=end-start+1
            while remaining:
                block=file.read(min(256*1024,remaining))
                if not block:break
                remaining-=len(block)
                yield block
        finally:file.close()
    return SafeStreamingResponse(chunks(),file=file,status_code=status,headers=headers)
