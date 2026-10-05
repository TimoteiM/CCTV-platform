# CCTV platform

FastAPI/Jinja2 application in `/opt/cctv-web`. The recorder, retention, firewall, and camera credential environment remain untouched.
A branded login page now protects access through revocable browser sessions.
The production service and socket are enabled and running exclusively on localhost.

## Production operation

```
systemctl status cctv-web.service cctv-web.socket
systemctl restart cctv-web.service
journalctl -u cctv-web.service --since '1 hour ago'
```

To stop delivery completely, stop both `cctv-web.socket` and `cctv-web.service`;
an active socket can activate its service again. Use one application process;
the in-process queue and cache lock require it. Do not launch another development
server against the production cache.

The dedicated `cctv-web` account has no login or sudo. Code, configuration and
virtual environment remain root-owned and read-only to the service. Only the
cache (2750 cctv-web:www-data, validated MP4s 0640; metadata/partials 0600), `/run/cctv-web` (0700), `/var/lib/cctv-auth` (0700), and isolated temporary
storage are writable. Original recordings are mounted read-only inside the service.
Supplementary group `cctv` is granted by the unit solely to read the originals;
`/etc/cctv`, nginx configuration and certificate directories are inaccessible.

`cctv-web.socket` owns only `127.0.0.1:8080`. The single Uvicorn process validates
and adopts the inherited socket. Seccomp permits only new AF_UNIX sockets, denying
new IPv4/IPv6 sockets to both Python and FFmpeg. This host's systemd lacks the BPF
framework, so BPF IP/bind restrictions were replaced with this enforced approach.
Startup preflight verifies filesystem and network restrictions on every start.
No debug mode or raw access logging is enabled; categorical request and playback
logs go to journald. See `deploy/` for the exact production configuration.

Memory high/max are 768 MiB/1 GiB, swap disabled, CPU quota 300%, task limit 128,
and file descriptor limit 1024. These leave headroom over the real sandboxed
conversion peak of about 235 MiB while bounding aggregate application/child usage.
Restart-on-failure waits three seconds; repeated failures are rate limited.
Graceful shutdown terminates only application-owned FFmpeg jobs and cleans partial
cache outputs. Enabled boot targets were verified without rebooting the recorder.

The application checks opaque sessions before serving pages, API responses or media.
Nginx additionally checks sessions on internal X-Accel media locations. The browser
Basic Auth prompt has been replaced by `/login`; both existing account passwords
are preserved through a root-owned copy of the existing hashes at
`/etc/cctv-web-auth/users` (root:cctv-web, 0640). This dedicated directory is readable
without granting access to camera credentials, nginx or certificates.

“Keep me signed in” sets an HTTPS-only, HttpOnly, SameSite=Lax, host-only cookie
for one year, renewed when visiting Live, Playback or Events. Unchecked sign-ins
use a browser-session cookie with a 24-hour server limit. Passwords are never put
in browser storage. Private SQLite state stores only SHA-256 digests of random
session tokens, survives restarts, and revokes the current session on Sign out.
Changing an account hash and restarting the service invalidates that account's
sessions. Clearing browser data or using another device requires signing in again.
Login forms use expiring CSRF tokens; logout uses a session-bound token. Cross-origin
mutating requests are rejected. Failed sign-ins are limited per IP and account
(10 attempts per 15 minutes), with limits surviving application restarts.

Production sets `CCTV_AUTH_CREDENTIALS` and `CCTV_AUTH_STATE`; a configured but
unreadable credential store fails startup. Isolated test instances omit this
setting. Never omit it from production. `deploy/nginx-cctv.conf.example` documents
session-protected delivery. Only loopback is trusted for overwritten forwarding
headers; nginx strips Authorization before proxying.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| CCTV_RECORDINGS_ROOT | /srv/cctv | Trusted original recording root |
| CCTV_CAMERA_NAMES | /opt/cctv-web/cameras.json | Eight display names independent of IDs |
| CCTV_MINIMUM_AGE | 60 | Minimum source age in seconds; minimum 60 |
| CCTV_DOWNLOAD_MODE | local | `local` streams; `nginx` emits internal X-Accel-Redirect |
| CCTV_CACHE_ROOT | /opt/cctv-web/var/cache | Flat, dedicated playback cache |
| CCTV_CACHE_MAX_AGE | 21600 | Six hours from generation, not last access |
| CCTV_CACHE_MAX_BYTES | 2147483648 | Two GiB for completed media and metadata |
| CCTV_PLAYBACK_QUEUE_SIZE | 3 | Waiting jobs; one additional active job |
| CCTV_CONVERSION_TIMEOUT | 900 | FFmpeg timeout in seconds |

Camera IDs are exactly cam01–cam08. Names are configured in `cameras.json`.
Dates and timestamps are checked strictly against the calendar. Only expected
`YYYY-MM-DD_HH-MM-SS.mkv` regular files are listed. Symlinks, malformed names,
zero-length files, recently modified files, and each camera's newest segment are
excluded. Listings use filename/stat metadata only, with no ffprobe or database.
Today uses Europe/Bucharest; recording timestamps follow filename convention.

## Playback policy and routes

Standard playback is H.264 High/AAC-LC MP4, at most 1280×720, preserving aspect
ratio without upscaling. This deliberately favors broad browser support, smaller
Internet transfers, and limited CPU/memory. Full-resolution originals remain
available for download. An explicit `format=hevc` API option copies the original
HEVC video, tags it hvc1, and encodes AAC audio; it is not selected automatically.
Actual HEVC profile/device compatibility still requires target-device validation.

- `GET /`: eight cameras.
- `GET /camera/{camera}?date=YYYY-MM-DD`: date navigation and chronological archive.
- `GET /api/camera/{camera}/recordings?date=YYYY-MM-DD`: filename/stat metadata.
- `GET /download/{camera}/{filename}`: original MKV, bounded streaming.
- `POST /api/playback/{camera}/{filename}?format=h264`: enqueue or reuse a job.
- `GET /api/playback/status/{job}`: queued/processing/ready/failed status.
- `GET` or `HEAD /media/playback/{job}`: prepared MP4 with single byte-range support.

Ready POSTs return 200; queued/processing return 202. Capacity exhaustion returns
429 with Retry-After. The frontend polls, shows preparation state, opens the HTML5
player, and offers retry/original download on failure. Closing the dialog stops
polling and playback; an already requested conversion continues for cache reuse.
For an HEVC client integration, failure must explicitly request H.264; never rely
on canPlayType alone to prove a device supports the entire recording.

## Conversion and cache safety

One worker serializes both transcodes and remuxes. A bounded waiting queue and
128-entry in-memory job map prevent unlimited jobs. Identical requests share a
job. FFmpeg receives only fixed argument arrays and validated source/output FDs
through `/proc/self/fd`, with `shell=False` behavior. Sources are opened read-only;
no source locks, writes, moves, permission changes, or deletion occur. Ordinary
reads may naturally update OS access times.

FFmpeg decoder/encoder pools are limited to two threads, filters/audio to one;
CPU affinity caps the whole process at two available CPUs. Nice 10 and best-effort
I/O priority 7 reduce contention. ffprobe operations have 30-second timeouts.
FFmpeg jobs default to 900 seconds. Each output has a reservation and hard size
limit: at most 512 MiB, further bounded by cache capacity and source size. An
unusually large/slow/corrupt source may fail safely and remain downloadable.
Shutdown terminates only the app's own subprocess group, with a forced-kill fallback.

Cache keys hash camera, filename, source device/inode/size/mtime/ctime, format,
and a conversion-settings version. Sources are revalidated before and after
conversion, before cache reuse, and before serving media. ffprobe checks container,
codecs, dimensions, pixel format, duration, audio presence, and size before publication.

Workflow: `.part` output -> successful conversion -> ffprobe validation -> fsync ->
atomic MP4 rename -> atomic JSON sidecar publication. Only the final MP4 plus its
valid sidecar can be used. Failed/interrupted outputs are removed; startup cleans
recognized orphan files. Sidecars persist cache hits across app restarts; queued
jobs are not persisted and clients must retry after a restart.

Cleanup runs at startup, around conversions, on explicit cleanup, and every five
minutes while the worker is idle. It removes expired/invalid entries and evicts
oldest generated entries to respect the size budget. Original retention expiry
makes cached media unavailable immediately on access; periodic cleanup removes it.
A stopped application runs no cleanup; its next startup removes expired cache.

Cleanup uses no-follow directory FDs and recognizes only flat hashed cache filenames.
It never recurses or follows symlinks. Configuration rejects overlap with both the
configured source and `/srv/cctv`. Unrelated files are left alone. No second original
retention mechanism is implemented. Streaming owns validated FDs and closes them
on completion or disconnect; open streams can finish safely if cache files are evicted.

## Later nginx integration

Production uses `CCTV_DOWNLOAD_MODE=nginx`. Nginx mode emits internal paths only
for already validated requests: `/_cctv_originals/{camera}/{filename}` and
`/_cctv_playback/{hash}.mp4`. Installed nginx locations are `internal`, authenticated
through the main proxy boundary, forbid symlinks and autoindex, and serve ranges.
They reject direct external requests and disable symlinks/autoindex. The backend remains bound to 127.0.0.1.

## Tests and evidence

```
cd /opt/cctv-web
.venv/bin/python -m pytest -q
.venv/bin/pip check
```

Tests mock FFmpeg and use temporary fixture directories. They never write `/srv/cctv`.
Real recording benchmarks and one real uncached on-demand job were run separately.
See `benchmarks/PHASE3-REPORT.md`, `benchmarks/results.json`, `benchmarks/tests.txt`,
`benchmarks/local-results.json`, `browser-results.json`, and `mobile-preview.png`.
The Starlette/httpx test adapter currently emits one deprecation warning.

Optional development browser checks require the benchmark segment to still exist:

```
.venv/bin/pip install -r requirements-browser.txt
PLAYWRIGHT_BROWSERS_PATH=/opt/cctv-web/.browser-test .venv/bin/python -m playwright install --only-shell chromium
PLAYWRIGHT_BROWSERS_PATH=/opt/cctv-web/.browser-test .venv/bin/python tests/browser_check.py
```

Browser checks use cached playback if ready; they can request preparation when cold.
Browser binaries are removed at phase completion. No Windows, Android, or Apple
hardware is available here; Linux headless Chromium viewport tests do not substitute
for testing those devices, audio audibility, or authenticated public delivery.

## Operations after approved production deployment

```
systemctl status cctv-web
journalctl -u cctv-web --since '1 hour ago'
nginx -t
systemctl status nginx
df -h /
du -sh /opt/cctv-web/var/cache
systemctl is-active cctv-cam{01..08}.service cctv-retention.timer
```

The service and socket are enabled. Do not reboot the live recorder to test persistence
without separate approval. Never read or expose camera credentials. Logs report
safe state/format/failure classes and tool exit codes, not credentials or tool URLs.

Phase 4 evidence is in `phase4/PHASE4-REPORT.md`. Phase 5 configuration, exact diff,
backup path, rollback commands and validation status are in `phase5/PHASE5-REPORT.md`.
Validated completed media receives read-only www-data group access before its
identity is recorded; partial outputs and JSON metadata remain private. nginx
cannot write the cache or originals. Authenticated dashboard, all eight cameras, cold playback, nginx media delivery,
ranges, download hash and authorization tests passed. The temporary credential
file was removed after validation. Phase 5 is complete.
No target Windows/Android/iPhone device validation has been performed here.

## NVR Live and Playback (Phase 6)

The primary landing page is Live; Playback uses camera/date/time and a day timeline.
Camera names in cameras.json are Acasă1–8, independent of unchanged cam IDs.
New separate cctv-live.service runs demand-driven H.264/AAC substream HLS: maximum
eight pipelines shared across viewers, 20s leases and 30s idle grace. The eight-camera
layout is the default; visible cameras start 300ms apart. HLS segments are one second. No RTSP/camera credentials reach browsers.
Control is private UDS, media is validated and delivered by an nginx internal route.

```
systemctl status cctv-live cctv-web
journalctl -u cctv-live --since '1 hour ago'
```

No viewer means no live FFmpeg process; active viewers keep shared pipelines alive.
Stopping all live delivery requires stopping cctv-live; web/playback can continue.
See phase6/PHASE6A-DECISION.md and phase6/PHASE6-REPORT.md for measurements, exact
unit/nginx diff, safety results and pending authenticated checks. Phase6 final
production validation is not marked complete. Do not launch an additional phase.


## Streaming performance update — 2026-10-02

The timeline player now requests `stream=true` on seek/next. `InstantPlayback`
starts an H.264/AAC EVENT HLS session at the requested source offset, publishing
one-second segments atomically while the remaining recording is converted.
Playback no longer waits for an entire five-minute MP4. Timeline seeks create a
new session at the requested time; the next contiguous recording starts through
the same path. The original download and legacy validated MP4 cache remain.

At most two progressive playback sessions can coexist. A session expires after
60 seconds without media requests/heartbeats; explicit seek, camera change and
page exit release it immediately. Conversion duration is bounded to recordings
up to 600 seconds, output video to 1280×720/20fps/2Mbps, and process runtime to the
configured timeout. Temporary HLS is separate from the persistent MP4 cache and
is deleted on release/expiry/restart. Original files are read-only, opened with
no-follow FDs and revalidated before every media response; changed/deleted
recordings revoke access. These sessions use no camera credentials or network
inputs. Session/filename validation precedes media access.

The live service now permits all eight cameras and has a two-core CPU quota,
1GiB MemoryHigh and 1.5GiB MemoryMax. Web/playback has a three-core CPU quota.
The host has six cores and ample available RAM. Live retries use 1.2 seconds
instead of eight seconds and startup polling uses 500ms. Low-memory/high-load
protection and demand-driven shutdown remain in place.

Both nginx and the application allow `blob:` only in `media-src`, enabling the
MediaSource buffer used by hls.js. Script and connection restrictions remain
same-origin. The progressive player prefers hls.js where supported, with native
HLS as the fallback. This avoids Chromium's native EVENT-HLS parsing failure.

Real-camera browser checks and results are in `benchmarks/performance_check.py`
and `benchmarks/performance-results.json`. They run against the private localhost
app and emulate nginx's existing X-Accel-Redirect delivery for live media; they
do not bypass or change public authentication. Local checks measured progressive playback startup at about 1–2s and a ten-second
timestamp seek at about 2s, versus observed historical
40–46s whole-file preparation. All eight live streams played together and
remained playing during playback. Camera switching, pause/resume and selected
range stopping and automatic recording-boundary transitions were checked without
JavaScript errors. Seek requests retain their target timestamp while the old
video is stopped, and the clock does not overwrite time-field edits. These are local server
browser measurements; remote connection and device performance may differ.

## Timeline zoom and person-event markers — 2026-10-02

Playback now has six visible time spans: 24h, 6h, 1h, 15m, 5m and 1m.
Plus/minus zoom around the selected playback timestamp; Full day resets the
view, Center brings the playback position into view, and earlier/later move
half a window. Drag pans without starting a stream; wheel zooms around the
pointer and two-finger pinch supports touchscreens. Clicking seeks to the
nearest second in the visible window. Arrow keys step one second at 15m or
closer, ten seconds at wider scales; Shift+arrow always steps ten seconds.
Dynamic axis labels, visible-window bounds and a pointer time readout show
which portion of the day is displayed. Zoom/pan only redraw local data and do
not request additional video streams.

Green remains recording availability. Yellow is a separate person-event band;
ordinary motion events are not rendered as person detections. The cursor/active
playback position is white so it cannot be confused with a person-event bar.

The timeline API includes `person_events` and `person_events_state`. Production
uses local server detection through `CCTV_PERSON_EVENTS_ROOT=/var/lib/cctv-persons`.
Missing connectivity, a date not indexed, a feed error and an indexed date with
no detections have distinct UI messages. Existing recordings contain no person
metadata; the background worker analyzes their video without modifying originals.

An adapter should atomically write `<root>/cam01/2026-10-02.json`, for example:

```json
{"source":"camera","events":[{"type":"person","start":43258,"end":43263}]}
```

`start`/`end` are seconds from midnight on that filename's date, in
Europe/Bucharest. `source` is `camera` or `server`. The loader merges overlapping
person intervals, ignores other event types, bounds size/count/timestamps,
validates camera/date paths and refuses symlink event files. This is an event
index contract, not an implemented Seetong/ONVIF acquisition adapter.

`benchmarks/timeline_zoom_check.py` checks all zoom levels, correct timestamp
mapping, keyboard steps, dragging without playback, wheel and genuine Chromium
touch pinch events at 320px, 390px and 1440px widths. It renders explicitly mocked
person events for its yellow-band checks, without writing detections to
production. Results are in `benchmarks/timeline-zoom-results.json`; automated
coverage is 203 passing tests.

## Local person detection — 2026-10-02

`cctv-persons.service` is enabled in production. It runs YOLOv5n v7.0 with
ONNX Runtime on CPU, sampling recordings every two seconds with a 0.55 person
confidence threshold. Yellow intervals are approximate AI detections, not a
guarantee that every person is found. No frames are uploaded or retained by the
worker. The service has no network access or camera credentials.

The official model comes from the [YOLOv5 v7.0 release](https://github.com/ultralytics/yolov5/releases/tag/v7.0).
Its FP16 graph was converted to FP32 for CPU execution. Original and runtime
SHA256 checksums and conversion details are in `models/person-model-provenance.json`;
the worker verifies the runtime checksum on startup. The upstream license is
bundled in `models/YOLOv5-LICENSE`. Dependencies are isolated in `.detect-venv`
and pinned in `requirements-detection.lock`; the web environment is unchanged.

The worker uses one inference/decode thread, idle I/O priority, nice 19 and a
150% CPU quota (at most 1.5 CPU cores), with a 512 MiB memory limit. Read access
is limited to recordings/model files; writes go to `/var/lib/cctv-persons`.
SQLite tracks recording identities and completed analysis across restarts.
Atomic JSON exports include analyzed intervals and person events; changed or
removed originals invalidate their results, and midnight intervals are split
between dates. An exclusive lock prevents duplicate workers.

A small group-readable request mailbox prioritizes the camera and timestamp
being viewed. New recordings are scheduled fairly across cameras, followed by
requested neighboring recordings and historical backfill. Full historical
analysis takes time. The timeline shows the analyzed fraction of available
recordings and refreshes every 20 seconds while visible; absence of yellow bars
in an unprocessed interval does not mean no person was present.

For installation, use the unit in `deploy/cctv-persons.service`, create the
`cctv-detect` system user with primary group `cctv-web` and supplementary group
`cctv`, and install the detection lockfile into `.detect-venv`. The state root is
owned by `cctv-detect:cctv-web` with mode 0750; its `requests` directory is owned
by `cctv-web:cctv-web` with mode 0750. The web service needs write access only to
that mailbox. Manage the worker with `systemctl status cctv-persons`,
`systemctl restart cctv-persons` or `systemctl stop cctv-persons`.

Validation includes positive/empty model images, real recording analysis,
persistent index/request/retention tests, and a genuine cam08 person event.
`benchmarks/person_detection_check.py` verifies the real yellow bar and playback
at that timestamp without mocked events; results are in
`benchmarks/person-detection-results.json`. Internal validation images are private.

## NVR client redesign — 2026-10-03

Live and Playback use a shared dark design system, inline SVG controls, keyboard
help and connection indicator. Mobile navigation is a bottom tab bar with safe
area padding; all mobile controls have at least 44px targets and reduced-motion
preferences are respected. A manifest and local 192/512px icons enable standalone
home-screen launch. No service worker caches private footage. Authenticated delivery,
CSP, frame blocking and security headers are unchanged.

Live has saved single/four/automatic layouts, compact mute/history/expand/fullscreen
controls, tile tap to expand, double tap to fullscreen, single-camera arrows/swipe,
and Escape/back navigation. Connection badges track Connecting, Live, Reconnecting
and Offline consistently. Consecutive failures use exponential backoff and stop
after five retries, with explicit Retry and last-seen time. All cameras selected in the grid keep their leases and buffers when scrolled
off screen, including all eight in the mobile All cameras view. IntersectionObserver
only resumes browser-paused video on re-entry and catches up to the live edge.
HLS player errors get up to two local recovery attempts before reconnecting the
camera session. Hidden tabs, cameras excluded by the selected layout and page departure release
their leases. Keeping eight streams connected increases mobile data and battery use.
Desktop automatic layout fits eight cameras in two rows at 1280px and wider.

Playback opens near the end of the latest finalized recording. Existing camera
and date bookmarks remain supported; `t=HH:MM:SS` restores the selected time and
replaces the legacy `time` parameter in new links. Live history/navigation links
request now minus one minute for their camera; if that footage is still being
written, they open the latest finalized footage and update the URL to its actual
position. The selected timestamp remains stable on subsequent refresh. Time inputs are masked/validated
24-hour text, and the recording clock stays blank until loaded. The custom player
controls include speed, mute/volume, snapshot, original download and details.
A desktop camera/thumbnail/calendar sidebar highlights recording dates. On mobile,
the player is sticky and date navigation appears beneath the timeline.

The timeline supports 24h/12h/6h/2h/1h/30m/10m zoom levels, plus 5m/1m for precise
seeking. Wheel/pinch zoom around the pointer; drag scrubs on the recording bar,
and the pan toggle/Shift-drag moves the window. Touch dragging pans unless started
near the playhead. Blue export handles have 44px hit regions and are prefilled
around the current position. A playhead label, pointer time and today's marker
show position; green recording bars merge adjacent intervals and yellow person
bars remain separate. Person legends appear only when events exist.

Backend additions preserve the existing endpoints and legacy single-file mode:

- `/playback` accepts `t` while retaining `time`, `start` and `end` bookmarks.
- Timeline responses include source `segments` alongside existing coverage/events.
- Streaming seek accepts `window=true` and a bounded `window_seconds` (300–2400).
  The client requests roughly 10 minutes on phones and 20 minutes on desktop.
  Windows concatenate validated read-only file descriptors, include ten seconds
  before the requested time and stop at actual gaps or midnight. Original identity
  checks cover every source; the two-session capacity limit is preserved. Optional
  `prefetch=true` returns a successful deferred state when capacity is occupied,
  avoiding spurious error logs while another playback tab is open.
- The player waits for the requested timestamp to be encoded and buffers from the
  window start before seeking, preventing an early EVENT playlist from clamping
  playback to zero. Buffered seeks reuse the video; a second, preloaded video
  handles the next window. The next available recording continues automatically.
- `/api/camera/{cam}/calendar` lists available dates and the latest playback point;
  `/api/camera/{cam}/thumbnail` serves a bounded, cached recording preview.
- `/api/timeline/{cam}/export/validate` checks a selected continuous range, and
  `/api/timeline/{cam}/export` downloads a fragmented H.264/AAC MP4 (maximum 15min).
  Export shares the playback session budget and cleans up its process on departure.
- A single bounded metadata worker refines actual file durations for viewed dates,
  using an idle-priority, duration-only FFprobe. Source-identity cache entries are
  bounded. Initial filename cadence estimates avoid artificial five-minute stripes;
  actual measured gaps remain visible as the index fills.

The cameras' burned-in OSD remains about six hours ahead. The web timeline uses
correct recording timestamps. Fix this in the cameras' NTP/timezone settings,
not by shifting web timestamps. The app does not infer camera clock differences
from image OCR.

Backups are in `var/backups/nvr-redesign-20261002`. Validation and before/after
screenshots are in `benchmarks/redesign`. The screenshot runner renders the saved
original frontend in its browser without rolling back the deployed app, and
captures exact 390×844/1440×900 viewports plus full-page companions. Automated tests
cover window boundaries, back buffers, source invalidation, midnight, export gaps
and size limits; real browser checks cover both directions of buffered skipping,
continuous handoff, scrubbing, URL restore, live retries, off-screen suspension,
hidden-tab/page-departure release and mobile overflow. Chromium and WebKit checks
use local recordings; physical iOS/Android devices are not part of this host.

Final validation: 213 Python tests passed. Chromium browser checks passed at
390, 1440 and 360 pixels; WebKit playback checks passed. Lighthouse accessibility
scored 100 for Live and Playback at both requested sizes. The only test warning
is the existing Starlette/httpx deprecation. Optional export validation reports
capacity limits before starting a download; export releases its own spare buffer.

## Frigate integration — 2026-10-03

The pre-Frigate source is preserved on GitHub at commit
`313b34e8622d078369f19ead8c7ab248cdd98ad4`. Credentials, recordings, screenshots,
virtual environments, runtime databases and model binaries are excluded from Git.
`deploy/cctv-web.env.example` documents application settings; copy it to the
ignored `deploy/cctv-web.env` when installing on a new host.

Frigate 0.17.2 runs as `cctv-frigate`, using the official image pinned by digest in
`deploy/frigate/compose.yaml`. It tracks people continuously on all eight camera
substreams, with OpenVINO CPU inference and the bundled SSDLite MobileNet model.
This host has six vCPUs and no exposed GPU/accelerator. Detection uses 640×360 at
2 fps, CPU cores 0–2, a 2.5-core quota, lower CPU shares, a 3 GiB memory limit and
256 MiB shared memory. Adjust the CPU set on hosts with different hardware.
The separate live engine continues to use its existing CPU allocation. Its overload
guard measures sustained stalls in its own cgroup, instead of global host load,
so unrelated AI startup threads do not tear down healthy live streams.

Frigate supplies detection to the existing UI. The recorder, original MKVs,
retention, live endpoints, playback windows and export remain in the current
system. Frigate recording is disabled, preventing a second video archive. Event snapshots
are enabled with one-day retention because Frigate persists tracked events only
when clips or snapshots are enabled. These private images stay in its media volume.
Its configuration/database and media directory live under `/var/lib/cctv-frigate`;
it never mounts the original `/srv/cctv` recordings. The internal API is published
only at `127.0.0.1:5000`; its UI, RTSP and WebRTC ports are not publicly published.

`deploy/frigate/provision.py` reads the existing root-only camera environment and
writes a root-only Frigate configuration without logging credentials. Real camera
URLs never enter Git or the browser. `config.example.yaml` contains placeholders.
Frigate itself necessarily knows camera credentials: restrict access to Docker
and its private logs/configuration just as you restrict the recorder's account.

Install Docker/Compose, then:

```sh
python3 deploy/frigate/provision.py
docker compose -f deploy/frigate/compose.yaml up -d
cp deploy/cctv-frigate-events.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cctv-frigate-events
systemctl restart cctv-web
```

Configure `CCTV_FRIGATE_EVENTS_ROOT=/var/lib/cctv-frigate-events` for the web app.
The importer runs as `cctv-detect`, calls only the local Frigate API, polls every
five seconds, validates/paginates events, excludes false positives, updates active
tracks and stores a bounded 31-day index. It writes atomic, read-only timeline
JSON files, splitting Unix timestamps at local Europe/Bucharest midnight. Camera
OSD clock offsets are not used. Successful camera processing intervals are indexed
separately; outages do not imply that no person was present.

The web service retains its network restrictions and reads these local files.
Yellow markers merge genuine new Frigate events with historical YOLO detections;
the old index is preserved. The UI identifies Frigate and reports loss of its
connection while retaining recorded markers. Frigate analyzes live streams from
installation onward; historical files are not retroactively analyzed by Frigate.
The original `cctv-persons` worker is disabled to avoid duplicate inference.

```sh
systemctl status cctv-frigate-events
docker compose -f deploy/frigate/compose.yaml ps
docker stats --no-stream cctv-frigate
journalctl -u cctv-frigate-events --since '10 minutes ago'
```

To roll back detection, stop the importer and Frigate, remove the
`CCTV_FRIGATE_EVENTS_ROOT` setting, restart the web app and re-enable
`cctv-persons`. The original recording and historical indexes remain available.

Tests cover local midnight, fractional timestamps, event filtering/deduplication,
active-track updates, camera outages, historical merging and configuration.
The integration is validated using known person footage and browser checks with
Frigate running. Production and validation footage are never committed.

Frigate validation: 227 Python tests passed. The official OpenVINO model detected
the person in a known camera-region fixture at 0.897 confidence; the complete
Frigate motion/tracking pipeline produced a real person event at 0.748 confidence
from a private replay of recorded footage. The importer converted its actual API
events into the expected timeline intervals. The isolated validation container was
removed afterward; no replay events were added to the production index.

The combined Chromium check kept all eight live feeds and playback running while
Frigate processed all eight streams at approximately 1.5–2 fps, without JavaScript
errors. Mobile/desktop live and playback checks also passed with Frigate enabled;
buffered skips continued to reuse their existing sessions. Normal Frigate memory
usage was about 1 GiB; CPU use varied by motion and stayed under its quota.

## Events page — 2026-10-05

`/events` adds a visible Frigate event browser to the existing Live/Playback
navigation. It includes camera/date filters, newest-first pagination, person
thumbnails, confidence, event duration and ongoing status. Details show the camera,
local start/end times, configured zones and detection source. Playback links open
the existing player at the matching event with up to three seconds of pre-roll.
A Watch live link opens that camera in single-camera view. Recent recordings that
are not yet finalized are clearly marked; expired/missing recordings have no Play
link. Saved filters and pagination are represented in the URL. Visible first-page
results refresh every 15 seconds; dialog reading and hidden tabs are not interrupted.

The existing importer migrates its SQLite metadata in place and writes atomic
per-camera/day event catalogs. Existing event rows are retained, including older
rows whose confidence or thumbnail was not captured. The web service reads these
validated local JSON files without gaining network access or direct access to
Frigate configuration. It continues to merge historical YOLO markers on Playback;
the Events page lists individual Frigate tracks rather than inventing thumbnails
for historical YOLO intervals.

Thumbnails are copied from the internal Frigate API into the importer's private
state directory, use hashed file identifiers, expire after one day and are bounded
by a 128 MiB cache. Thumbnail fetch attempts have a per-poll deadline and size/time
limits. Missing or expired images have explicit placeholders. Public JPEG delivery
uses validated no-follow file descriptors, bounded reads, strict identifiers and
session authentication and no-store headers. Raw Frigate identifiers,
camera credentials and internal API addresses are not exposed in the event data.

New authenticated application routes:

- `GET /events?cam=all&date=YYYY-MM-DD`
- `GET /api/events?cam=all&date=YYYY-MM-DD&offset=0&limit=24` (up to 48 per page)
- `GET/HEAD /event-media/{hashed_identifier}.jpg`

The page uses the existing design system, a mobile bottom Events tab, keyboard
shortcut E, accessible detail dialog and responsive card layout. Tests cover event
pagination/filtering, playback availability, metadata migration, thumbnail expiry,
symlinks, bounded media, invalid identifiers and malformed catalogs. Real Chromium
checks passed at 390, 1440 and 360 pixels, including genuine thumbnails, event
playback, camera navigation and date/empty-state handling. Lighthouse accessibility
scored 100 for Events at both mobile and desktop sizes. Screenshots and browser
results remain private under `benchmarks/` and the workspace `review/` directory.

Final Events validation: 250 Python tests passed; targeted tests passed again after
the pagination read optimization. Chromium and WebKit checks passed with real
thumbnails and no JavaScript errors. Both new public routes returned 401 without
nginx authentication, and the service's no-network preflight continued to pass.
