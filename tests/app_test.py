"""Orbi phone app: listening tests in headless Chromium, with a stand-in brain
and a scripted microphone. Run:  python3 tests/app_test.py   (needs playwright)."""
import json, struct, math, threading, http.server, functools, os, sys
from playwright.sync_api import sync_playwright

WEB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8791
class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k): pass
srv = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), functools.partial(Quiet, directory=WEB))
threading.Thread(target=srv.serve_forever, daemon=True).start()

pcm = b''.join(struct.pack('<h', int(6000 * math.sin(i / 8))) for i in range(6400))  # 0.4 s of "speech"
turns, replies = [], []
CORS = {'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*'}

def brain(route):
    req = route.request
    path = req.url.split('orbit-brain')[1]
    if req.method == 'OPTIONS': return route.fulfill(status=200, headers=CORS, body='ok')
    if path == '/hello': return route.fulfill(status=200, headers=CORS, json={'ok': True, 'device': 'Orbi (phone)'})
    if path == '/turn':
        body = req.post_data_buffer or b''
        ctype = req.headers.get('content-type', '')
        t = {'bytes': len(body), 'mode': req.headers.get('x-orbit-mode', ''), 'multipart': ctype.startswith('multipart/form-data')}
        if t['multipart']:
            # Which parts came, and whether the image really is a JPEG.
            t['parts'] = sorted(set(x.decode() for x in __import__('re').findall(rb'name="(\w+)"', body)))
            i = body.find(b'\xff\xd8\xff')
            t['jpeg'] = i >= 0
            t['jpeg_bytes'] = len(body) - i if i >= 0 else 0
        turns.append(t)
        text = replies.pop(0) if replies else 'Okay.'
        lines = [{'t': 'heard', 'text': 'something'}, {'t': 'reply', 'text': text}, {'t': 'audio'}]
        return route.fulfill(status=200, headers={**CORS, 'Content-Type': 'application/x-ndjson'},
                             body=''.join(json.dumps(l) + '\n' for l in lines).encode() + pcm)
    route.fulfill(status=404, headers=CORS, body='{}')

# A scripted microphone: the page's real capture is replaced, and the test feeds sound in.
MIC = """
window.__opens = 0;
openMic = async () => { window.__opens++; audioNow();
  mic = mic || { stream: { getAudioTracks: () => [{ readyState: 'live', muted: false }], getTracks: () => [] },
                 proc: { disconnect() {} }, src: { disconnect() {} }, sink: { disconnect() {} } };
  return mic; };
window.say = (level, ms) => { const n = 2048, blocks = Math.ceil(ms / (n / ac.sampleRate * 1000));
  for (let b = 0; b < blocks; b++) { const d = new Float32Array(n); for (let i = 0; i < n; i++) d[i] = (i % 2 ? 1 : -1) * level; onMicBlock(d); } };
0;
"""
failures = []
def check(name, cond, detail=''):
    print(('ok   ' if cond else 'FAIL ') + name + (('  -> ' + str(detail)) if detail and not cond else ''))
    if not cond: failures.append(name)

with sync_playwright() as p:
    b = p.chromium.launch(args=['--autoplay-policy=no-user-gesture-required', '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'])
    ctx = b.new_context(viewport={'width': 390, 'height': 844})
    pg = ctx.new_page(); errs = []
    pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.add_init_script("localStorage.setItem('orbit.token', JSON.stringify('x'.repeat(40)))")
    pg.route('**/functions/v1/orbit-brain/**', brain)
    pg.route('**/fonts.googleapis.com/**', lambda r: r.fulfill(status=200, content_type='text/css', body=''))
    pg.goto(f'http://127.0.0.1:{PORT}/index.html'); pg.wait_for_timeout(400)
    pg.evaluate(MIC)
    box = pg.locator('#talk').bounding_box(); bx, by = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
    mode = lambda: pg.evaluate('mode')
    def idle(): pg.wait_for_function("mode === 'idle'", timeout=8000)
    def down(): pg.mouse.move(bx, by); pg.mouse.down()
    def up(): pg.mouse.up()
    def tap(): down(); pg.wait_for_timeout(80); up()

    # 1. First question: hold, talk, let go.
    down(); pg.wait_for_timeout(150); pg.evaluate("say(0.1, 1500)"); pg.wait_for_timeout(350); up()
    pg.wait_for_function("mode === 'speak'", timeout=5000)
    check('first question is sent', len(turns) == 1, turns); idle()

    # 2. Second question, hands-free, talking from the very first instant (the mic is already open).
    tap(); pg.wait_for_timeout(50)
    check('tap starts hands-free listening', mode() == 'listen' and pg.evaluate('rec.tap'))
    pg.evaluate("say(0.1, 2000)")
    check('speech that starts immediately is heard', pg.evaluate('rec && rec.vad.heard'))
    pg.evaluate("say(0.003, 1500)"); pg.wait_for_timeout(300)
    check('second question is sent when you pause', len(turns) == 2, turns)
    pg.wait_for_function("mode === 'speak'", timeout=5000); idle()

    # 3. Third and fourth in a row, held.
    for n in (3, 4):
        down(); pg.wait_for_timeout(60); pg.evaluate("say(0.12, 1200)"); pg.wait_for_timeout(450); up()
        pg.wait_for_function("mode === 'speak'", timeout=5000)
        check(f'question {n} in a row is sent', len(turns) == n, turns); idle()

    # 4. Orbi asks something and listens on its own; pressing the button to answer must not cut you off.
    replies.append('Which day do you want?')
    down(); pg.wait_for_timeout(60); pg.evaluate("say(0.1, 1000)"); pg.wait_for_timeout(450); up()
    pg.wait_for_function("mode === 'listen' && rec && rec.followUp", timeout=9000)
    down(); pg.wait_for_timeout(100)
    check('pressing while it listens keeps listening', mode() == 'listen')
    pg.evaluate("say(0.1, 1200)"); pg.wait_for_timeout(450); up()
    pg.wait_for_function("mode === 'speak'", timeout=5000)
    check('the held answer is sent', len(turns) == 6, turns); idle()

    # 5. Same, but a quick tap before talking: it keeps listening, then sends at the pause.
    replies.append('Morning or afternoon?')
    down(); pg.wait_for_timeout(60); pg.evaluate("say(0.1, 1000)"); pg.wait_for_timeout(450); up()
    pg.wait_for_function("mode === 'listen' && rec && rec.followUp", timeout=9000)
    tap(); pg.wait_for_timeout(60)
    check('a tap before talking keeps listening', mode() == 'listen' and pg.evaluate('!!rec && rec.tap'))
    pg.evaluate("say(0.1, 1200); say(0.003, 1500)"); pg.wait_for_timeout(300)
    check('and the answer is sent at the pause', len(turns) == 8, turns)
    pg.wait_for_function("mode === 'speak'", timeout=5000); idle()

    # 6. Noise isn't speech; silence is never sent; tapping the face cancels.
    tap(); pg.wait_for_timeout(50); pg.evaluate("say(0.006, 3000)")
    check('room noise is not mistaken for speech', pg.evaluate('rec && !rec.vad.heard'))
    pg.locator('#face').click(); pg.wait_for_timeout(100)
    check('tapping the face cancels', mode() == 'idle' and len(turns) == 8, (mode(), len(turns)))
    tap(); pg.wait_for_timeout(50); pg.evaluate("say(0.002, 7200)"); pg.wait_for_timeout(200)
    check('silence gets a notice, not a request', mode() == 'notice' and len(turns) == 8, (mode(), len(turns)))
    pg.locator('#face').click()

    # 7. Loud jobsite, first listen there: a steady rumble, then a voice over it, then the rumble again.
    tap(); pg.wait_for_timeout(50); pg.evaluate("say(0.03, 1000); say(0.25, 1500); say(0.03, 1700)"); pg.wait_for_timeout(300)
    check('a voice over steady noise is sent', len(turns) == 9, turns[-1:])
    check('and it stopped soon after the voice did', 100000 < turns[-1]['bytes'] < 150000, turns[-1:])
    pg.wait_for_function("mode === 'speak'", timeout=5000); idle()
    # A roar alone can't hold the recording open until the 20-second cap.
    tap(); pg.wait_for_timeout(50); pg.evaluate("say(0.05, 6000)"); pg.wait_for_timeout(300)
    check('a roar alone ends by itself', mode() != 'listen', mode())
    pg.wait_for_function("mode === 'idle' || mode === 'speak'", timeout=5000); idle(); n = len(turns)
    # Once it has heard the room between questions, the rumble isn't mistaken for speech at all.
    pg.evaluate("say(0.03, 4000)")
    tap(); pg.wait_for_timeout(50); pg.evaluate("say(0.03, 2500)")
    check('a known rumble is not mistaken for speech', pg.evaluate('rec && !rec.vad.heard'), pg.evaluate('rec && rec.vad'))
    pg.evaluate("say(0.25, 1500); say(0.03, 1600)"); pg.wait_for_timeout(300)
    check('and the voice over it is still sent', len(turns) == n + 1, turns[-1:])
    pg.wait_for_function("mode === 'speak'", timeout=5000); idle()
    pg.evaluate("ambient = 0")

    # 8. The microphone goes dead while listening: it is reopened, and gives up politely if that fails.
    opens = pg.evaluate('__opens')
    tap(); pg.wait_for_timeout(2300)
    check('a dead microphone is reopened', pg.evaluate('__opens') > opens and mode() == 'listen', (pg.evaluate('__opens'), opens, mode()))
    pg.wait_for_timeout(5500)
    check('and it stops trying after a few goes', mode() in ('notice', 'idle'), mode())

    # 9. Seeing: the camera button, live look, What's this?, a picked photo, typing with a photo.
    pg.evaluate("stopTurn(true)"); idle()
    pg.locator('#camBtn').click()
    pg.wait_for_function("look.stream && document.getElementById('cam').videoWidth > 0", timeout=5000)
    check('the camera button opens a live camera in place of the face',
          pg.evaluate("!document.getElementById('look').hidden && document.getElementById('face').hidden"))
    n = len(turns)
    down(); pg.wait_for_timeout(60); pg.evaluate("say(0.12, 1200)"); pg.wait_for_timeout(450); up()
    pg.wait_for_function("mode === 'think' || mode === 'speak'", timeout=5000)
    check('a spoken question while the camera is open shows what Orbi saw',
          pg.evaluate("!document.getElementById('still').hidden && !!look.sentUrl"))
    pg.wait_for_function("mode === 'speak'", timeout=5000)
    t = turns[-1]
    check('and is sent with the speech and a JPEG', len(turns) == n + 1 and t['multipart'] and t['parts'] == ['audio', 'image'] and t['jpeg'], t)
    idle()
    pg.locator('#camAsk').click(); pg.wait_for_function("mode === 'speak'", timeout=5000)
    t = turns[-1]
    check("What's this? sends just the picture", t.get('parts') == ['image'] and t['jpeg'], t); idle()
    # A big photo from the library is shrunk to 1280 px before it goes anywhere.
    big = pg.evaluate('''async () => { const c = document.createElement('canvas'); c.width = 4032; c.height = 3024;
      const x = c.getContext('2d'); for (let i = 0; i < 400; i++) { x.fillStyle = `hsl(${i * 7},70%,50%)`; x.fillRect((i * 97) % 4000, (i * 61) % 3000, 300, 200); }
      const b = await new Promise((r) => c.toBlob(r, 'image/jpeg', 0.95));
      return Array.from(new Uint8Array(await b.arrayBuffer())); }''')
    pg.set_input_files('#pick', files=[{'name': 'IMG_0001.jpg', 'mimeType': 'image/jpeg', 'buffer': bytes(big)}])
    pg.wait_for_function("!!look.still", timeout=5000)
    dims = pg.evaluate('''async () => { const b = await createImageBitmap(look.still); return [b.width, b.height, look.still.size]; }''')
    check('a picked photo is shown and shrunk to 1280 px', dims[0] == 1280 and dims[1] == 960 and pg.evaluate("!document.getElementById('still').hidden"), dims)
    check('the live camera is released while a photo is picked', pg.evaluate("!look.stream && !document.getElementById('camLive').hidden"))
    pg.evaluate("document.getElementById('typeBtn').click()")
    pg.fill('#typed', 'what brand is this'); pg.press('#typed', 'Enter')
    pg.wait_for_function("mode === 'speak'", timeout=5000)
    t = turns[-1]
    check('a typed question carries the picked photo', t.get('parts') == ['image', 'text'] and t['jpeg'] and t['jpeg_bytes'] < 600000, t); idle()
    # Translator: What's this? goes out marked for translating.
    pg.evaluate("setTranslating(true)")
    pg.locator('#camAsk').click(); pg.wait_for_function("mode === 'speak'", timeout=5000)
    check('in translator mode the photo goes to the translator', turns[-1]['mode'] == 'translate' and turns[-1].get('parts') == ['image'], turns[-1])
    idle(); pg.evaluate("setTranslating(false)")
    # Closing gives the face back and stops the camera.
    pg.locator('#camClose').click()
    check('closing the camera brings the face back and turns the camera off',
          pg.evaluate("document.getElementById('look').hidden && !document.getElementById('face').hidden && !look.stream && !look.still"))
    n = len(turns)
    down(); pg.wait_for_timeout(60); pg.evaluate("say(0.12, 1200)"); pg.wait_for_timeout(450); up()
    pg.wait_for_function("mode === 'speak'", timeout=5000)
    check('with the camera closed, questions go without a photo', len(turns) == n + 1 and not turns[-1]['multipart'], turns[-1]); idle()
    # A camera that can't be opened falls back to taking or picking a photo.
    pg.evaluate("navigator.mediaDevices.getUserMedia = () => Promise.reject(Object.assign(new Error('no'), { name: 'NotAllowedError' })); 0")
    pg.locator('#camBtn').click(); pg.wait_for_timeout(300)
    check('a blocked camera offers Take photo and Photos instead',
          pg.evaluate("look.noLive && !document.getElementById('camShoot').hidden && !document.getElementById('camPick').hidden && document.getElementById('camAsk').hidden"))
    pg.locator('#camClose').click()

    check('no page errors', not errs, errs)
    b.close()
srv.shutdown()
print('\n%d failed' % len(failures) if failures else '\nall passed')
sys.exit(1 if failures else 0)
