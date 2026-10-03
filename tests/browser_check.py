"""Development-only real browser checks against localhost with a ready real segment."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

NAME='2026-10-01_19-55-20.mkv'
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
    results=[]
    for width,height in [(320,700),(390,844),(768,1024),(1440,900)]:
        page=browser.new_page(viewport={'width':width,'height':height})
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        for route in ['/', '/camera/cam01?date=2026-10-01', '/camera/cam01?date=2025-01-01']:
            response=page.goto('http://127.0.0.1:8080'+route)
            assert response.status==200
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),(width,route)
            results.append({'width':width,'route':route,'no_horizontal_overflow':True})
        page.goto('http://127.0.0.1:8080/camera/cam01?date=2026-10-01')
        page.locator(f'.play[data-recording="{NAME}"]').click()
        page.wait_for_function("() => document.querySelector('#playback-status').textContent.startsWith('Ready.')",timeout=180000)
        page.wait_for_function("() => document.querySelector('#video').readyState >= 2",timeout=30000)
        assert page.locator('#playback').is_visible()
        assert not page.locator('#retry-playback').is_visible()
        page.evaluate("document.querySelector('#video').play()")
        page.wait_for_function("() => document.querySelector('#video').currentTime > 0.5",timeout=30000)
        page.evaluate("document.querySelector('#video').currentTime = 120")
        page.wait_for_function("() => document.querySelector('#video').currentTime > 120.2 && !document.querySelector('#video').seeking",timeout=30000)
        details=page.evaluate("""() => {
          const v=document.querySelector('#video');
          return {videoWidth:v.videoWidth,videoHeight:v.videoHeight,duration:v.duration,currentTime:v.currentTime,
            h264:v.canPlayType('video/mp4; codecs="avc1.64001f,mp4a.40.2"'),
            hevc:v.canPlayType('video/mp4; codecs="hvc1.1.6.L150.B0,mp4a.40.2"')};
        }""")
        assert details['videoWidth']==1280 and details['videoHeight']==720
        assert details['h264']
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        results.append({'width':width,'playback_and_seek':True,**details})
        if width==390:page.screenshot(path='/opt/cctv-web/mobile-preview.png')
        page.locator('#close-playback').click()
        assert not page.locator('#playback').is_visible()
        assert not errors,errors
        page.close()
    browser.close()
    Path('/opt/cctv-web/browser-results.json').write_text(json.dumps(results,indent=2)+'\n')
    print('Browser checks passed: 12 page/viewport combinations; H.264 playback, frame dimensions, seeking and dialog at all 4 sizes; no JavaScript errors.')
