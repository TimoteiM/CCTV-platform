"""Fixed FFmpeg profiles, bounded process lifetime, validated open input/output FDs."""
import json
import logging
import math
import os
import signal
import subprocess
import threading

LOG = logging.getLogger('cctv.playback')
# Include this signature in cache keys whenever conversion settings change.
PROFILE_VERSION = 'v2-hevc-original-h264-720p-veryfast-crf26-high-yuv420p-aac48k64k-faststart'

class ConversionError(Exception):
    pass

def arguments(source_fd, output_fd, mode, output_limit):
    args=['/usr/bin/ffmpeg','-hide_banner','-nostdin','-nostats','-loglevel','error','-y',
          '-threads','2','-filter_threads','1','-f','matroska','-i',f'/proc/self/fd/{source_fd}',
          '-map','0:v:0','-map','0:a:0?','-map_metadata','-1','-map_chapters','-1']
    if mode=='hevc':
        args += ['-c:v','copy','-tag:v','hvc1']
    elif mode=='h264':
        args += ['-c:v','libx264','-preset','veryfast','-crf','26','-threads','2',
                 '-vf', 'scale=w=min(1280\\,iw):h=min(720\\,ih):force_original_aspect_ratio=decrease:force_divisible_by=2:out_range=tv',
                 '-pix_fmt','yuv420p','-profile:v','high']
    else:
        raise ConversionError('Unsupported playback format')
    return args+['-c:a','aac','-b:a','64k','-ar','48000','-ac','1','-threads:a','1',
                 '-movflags','+faststart','-avoid_negative_ts','make_zero','-fs',str(output_limit),
                 '-f','mp4',f'/proc/self/fd/{output_fd}']

class FFmpegConverter:
    def __init__(self, timeout=900):
        self.timeout=timeout
        self.priority=10
        self.lock=threading.Lock()
        self.process=None
        self.stopped=threading.Event()

    def execute(self, args, fds, timeout, capture=False):
        with self.lock:
            if self.stopped.is_set(): raise ConversionError('Preparation stopped')
            process=subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, pass_fds=tuple(fds), start_new_session=True)
            self.process=process
        try:
            stdout,_=process.communicate(timeout=timeout)
            if process.returncode:
                LOG.warning('Media tool failed (exit=%s)',process.returncode)
                raise ConversionError('Media preparation failed')
            return stdout
        except subprocess.TimeoutExpired:
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            process.communicate()
            LOG.warning('Media tool timed out')
            raise ConversionError('Preparation timed out') from None
        finally:
            with self.lock:
                if self.process is process: self.process=None

    def probe(self, fd, source=False):
        try:
            data=self.execute(['/usr/bin/ffprobe','-v','error','-show_entries',
                               'format=duration,size,format_name:stream=codec_type,codec_name,codec_tag_string,width,height,pix_fmt',
                               '-f', 'matroska' if source else 'mov', '-of','json',f'/proc/self/fd/{fd}'],[fd],30,True)
            result=json.loads(data)
            duration=float(result['format']['duration'])
            if not math.isfinite(duration) or duration<=0: raise ValueError()
            return result
        except (ValueError,KeyError,TypeError):
            raise ConversionError('Media validation failed') from None

    def probe_duration(self, fd):
        """Read finalized Matroska duration without probing/decoding video frames."""
        data=self.execute(['/usr/bin/ionice','-c','3','/usr/bin/nice','-n','19',
            '/usr/bin/ffprobe','-v','error','-threads','1','-probesize','32768',
            '-analyzeduration','0','-show_entries','format=duration','-f','matroska',
            '-of','json',f'/proc/self/fd/{fd}'],[fd],30,True)
        try:
            duration=float(json.loads(data)['format']['duration'])
            if not math.isfinite(duration) or not 0<duration<=600:raise ValueError()
            return duration
        except (ValueError,KeyError,TypeError):
            raise ConversionError('Recording duration unavailable') from None

    def convert(self, source_fd, output_fd, mode, output_limit):
        source=self.probe(source_fd,source=True)
        # Separate decoder/encoder pools can use about three cores despite -threads 2.
        # Affinity bounds the entire conversion process to two available CPUs.
        cpus=sorted(os.sched_getaffinity(0))[-2:]
        command=['/usr/bin/ionice','-c','2','-n','7','/usr/bin/nice','-n',str(self.priority),
                 '/usr/bin/taskset','-c',','.join(map(str,cpus)),*arguments(source_fd,output_fd,mode,output_limit)]
        self.execute(command,[source_fd,output_fd],self.timeout)
        output=self.probe(output_fd)
        expected='hevc' if mode=='hevc' else 'h264'
        videos=[s for s in output['streams'] if s['codec_type']=='video']
        audio=[s for s in output['streams'] if s['codec_type']=='audio']
        source_video=next(s for s in source['streams'] if s['codec_type']=='video')
        if len(videos)!=1 or videos[0]['codec_name']!=expected or any(s['codec_name']!='aac' for s in audio):
            raise ConversionError('Unexpected playback codecs')
        if any(s['codec_type']=='audio' for s in source['streams']) and not audio:
            raise ConversionError('Playback audio missing')
        if mode=='hevc' and videos[0].get('codec_tag_string')!='hvc1':
            raise ConversionError('Invalid HEVC tag')
        if mode=='h264' and videos[0].get('pix_fmt')!='yuv420p':
            raise ConversionError('Invalid pixel format')
        if mode=='hevc' and (videos[0]['width'],videos[0]['height'])!=(source_video['width'],source_video['height']):
            raise ConversionError('Playback resolution changed')
        if mode=='h264' and (videos[0]['width']>min(1280,source_video['width']) or videos[0]['height']>min(720,source_video['height'])):
            raise ConversionError('Invalid playback resolution')
        duration=float(output['format']['duration'])
        if abs(duration-float(source['format']['duration']))>max(1,float(source['format']['duration'])*.01):
            raise ConversionError('Playback duration mismatch')
        if not 0<os.fstat(output_fd).st_size<=output_limit:
            raise ConversionError('Playback size limit exceeded')
        return {'duration':duration,'video_codec':expected,'audio_codec':'aac' if audio else None,
                'width':videos[0]['width'],'height':videos[0]['height']}

    def cancel(self, force=False):
        self.stopped.set()
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                try: os.killpg(self.process.pid,signal.SIGKILL if force else signal.SIGTERM)
                except ProcessLookupError: pass
