#!/usr/bin/env python3
# vita-bridge daemon — PS Vita handheld <-> Hermes gateway voice/camera bridge.
#
# All configuration arrives via VB_* environment variables (set by the plugin
# lifecycle manager); nothing is hardcoded: no credentials, no LAN IPs, no
# absolute paths. Wire protocol (raw TCP over the HTTP port; Vita lpp sockets
# partial-send, so every frame is acked and the pipeline NEVER holds one
# connection — results are parked and fetched on fresh short-lived sockets).
# EVERY socket connection authenticates first: the first line must be
# "K <pair-token>\n" (token auto-created on first start in the plugin data
# dir, compared with hmac.compare_digest); anything else gets "E pair" then
# the connection is dropped:
#   "U <id>\n" open | "C <n>\n"+bytes (n<=4MiB, <=16MiB buffered per session)
#     -> "A\n" | "T <id> <wavlen> <pnglen>\n" -> "G\n" then result via
#     P <id> -> "W"(pending) | "E <msg>" | "R <alen> <tlen>\n"+body
#   "D <id>\n" discard | "X\n" abort
# HTTP: /ping open; /talk /abort /files/<name> require header "X-Pair-Token: <token>"
import os, sys, io, json, wave, base64, socket, threading, time
import hmac, secrets
import urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GATEWAY_URL  = os.environ.get('VB_GATEWAY_URL', 'http://127.0.0.1:8642/v1/chat/completions')
GATEWAY_KEY  = os.environ.get('VB_GATEWAY_KEY', '')
SESSION_KEY  = os.environ.get('VB_SESSION_KEY', 'vita-handheld')
STT_MODEL    = os.environ.get('VB_STT_MODEL', 'base')
STT_DEVICE   = os.environ.get('VB_STT_DEVICE', 'auto')
WOOF_DIR     = os.environ.get('VB_WOOF_DIR', '')
TTS_CAP      = int(os.environ.get('VB_TTS_CAP', '400'))
PORT         = int(os.environ.get('VB_PORT', '8643'))
FILES_DIR    = os.environ.get('VB_FILES_DIR') or os.path.join(os.getcwd(), 'files')
TOKEN_FILE   = os.environ.get('VB_TOKEN_FILE') or os.path.join(os.getcwd(), 'pair.token')

MAX_CHUNK         = 4 << 20    # hard cap on a single "C <n>" frame
MAX_SESSION_BYTES = 16 << 20   # hard cap on total buffered bytes per session


def load_token():
    """Pairing token: created 0600 in the plugin data dir on first start,
    then reused. The Vita client must present it on every connection."""
    for _ in range(2):
        try:
            t = open(TOKEN_FILE).read().strip()
            if t:
                return t
        except FileNotFoundError:
            pass
        try:
            fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(secrets.token_urlsafe(24) + '\n')
        except FileExistsError:
            continue
        return open(TOKEN_FILE).read().strip()
    raise RuntimeError('cannot read/create pairing token at %s' % TOKEN_FILE)


PAIR_TOKEN = load_token()


def ok_token(candidate):
    return hmac.compare_digest(candidate.encode('utf-8', 'ignore'),
                               PAIR_TOKEN.encode('utf-8', 'ignore'))


sys.path.insert(0, os.path.join(FILES_DIR, 'whisper'))
if WOOF_DIR:
    sys.path.insert(0, WOOF_DIR)

from faster_whisper import WhisperModel
try:
    import ctranslate2
    HAVE_CUDA = ctranslate2.get_cuda_device_count() > 0
except Exception:
    HAVE_CUDA = False
# compute_type per device: fp16 only exists on CUDA; CPU gets int8. 'auto'
# maps to CPU when there is no CUDA (no point building the CUDA path).
DEVICE = 'cuda' if (STT_DEVICE == 'cuda' or (STT_DEVICE == 'auto' and HAVE_CUDA)) else 'cpu'
model = WhisperModel(STT_MODEL, device=DEVICE,
                     compute_type='float16' if DEVICE == 'cuda' else 'int8')
from underdog_woof import Woof
tts = Woof()
print('bridge warm: whisper+tts loaded', flush=True)

sessions = {}
REPLIES = {}
slock = threading.Lock()
ABORT = threading.Event()


def now(): return '%.3f' % time.time()


def stt(wav_bytes):
    segments, _info = model.transcribe(io.BytesIO(wav_bytes), language='en', vad_filter=True)
    return ' '.join(s.text.strip() for s in segments).strip()


VITA_SYSTEM = (
    'You are Hermes aboard the user\'s PS Vita handheld: they speak to you via '
    'push-to-talk voice (transcribed) and can attach a rear-camera frame. '
    'Message tags: [voice] audio only, [voice+camera] audio with a camera frame '
    'attached, [camera] silent, camera frame attached. Answer CONVERSATIONALLY '
    'to what they actually said; describe the image only when a frame is '
    'attached. Keep replies under 60 words, plain text, no markdown: they are '
    'spoken aloud through the handheld\'s speaker.')


def hermes(text, img_b64):
    content = [{'type': 'text', 'text': text}]
    if img_b64:
        content.append({'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + img_b64}})
    body = {'model': 'hermes-agent', 'messages': [
        {'role': 'system', 'content': VITA_SYSTEM},
        {'role': 'user', 'content': content}]}
    body['user'] = SESSION_KEY
    headers = {'Content-Type': 'application/json',
               'X-Hermes-Session-Key': SESSION_KEY}
    if GATEWAY_KEY:
        headers['Authorization'] = 'Bearer ' + GATEWAY_KEY
    req = urllib.request.Request(GATEWAY_URL, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    return d['choices'][0]['message']['content']


def say(text):
    audio = tts(text[:TTS_CAP], seed=0)
    buf = io.BytesIO()
    w = wave.open(buf, 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
    w.writeframes((audio * 32767).astype('int16').tobytes()); w.close()
    return buf.getvalue()


def readn(conn, n):
    buf = b''
    while len(buf) < n:
        c = conn.recv(min(1 << 16, n - len(buf)))
        if not c:
            return None
        buf += c
    return buf


def readline(conn):
    # byte-accurate: recv(64) would swallow binary payload after the newline
    buf = bytearray()
    while True:
        c = conn.recv(1)
        if not c:
            return None
        if c == b'\n':
            break
        buf += c
    return buf.decode(errors='replace').strip()


def run_pipeline(wav, png, log):
    try:
        text = stt(wav) if wav else ''
    except Exception as e:
        log('STT-ERR %s' % e); text = ''
    log('STT "%s" png=%d' % (text[:80], len(png)))
    if not text and not png:
        return 'I did not hear anything.', say('I did not hear anything.')
    # Modality tags: the transcript IS the prompt. A silent camera frame asks
    # what to do; it must never default to an unprompted whole-image caption.
    if text and png:
        tag, q = '[voice+camera] ', text
    elif text:
        tag, q = '[voice] ', text
    else:
        tag, q = '[camera] ', ('A camera frame arrived but no speech was '
                               'captured with it. Do NOT describe the frame '
                               'unprompted; briefly ask the user what to do '
                               'with it.')
    log('TAG %s png=%d' % (tag.strip(), len(png)))
    try:
        reply = hermes(tag + q,
                       base64.b64encode(png).decode() if png else None)
    except Exception as e:
        log('HERMES-ERR %s' % e)
        reply = 'The brain is unreachable.'
    log('REPLY "%s"' % reply[:80])
    return reply, say(reply)


def handle_session(conn, sid, addr):
    def log(m): print('%s %s %s' % (now(), addr, m), flush=True)
    log('SESSION %s' % sid)
    conn.settimeout(600)
    while True:
        line = readline(conn)
        if line is None:
            break
        p = line.split()
        if not p:
            continue
        if p[0] == 'C' and len(p) == 2:
            try:
                n = int(p[1])
            except ValueError:
                n = -1
            if n < 0 or n > MAX_CHUNK:
                log('C-REJECT n=%r' % p[1])
                conn.sendall(b'E oversize\n')
                break
            data = readn(conn, n)
            if data is None:
                break
            with slock:
                buf = sessions.setdefault(sid, bytearray())
                if len(buf) + n > MAX_SESSION_BYTES:
                    sessions.pop(sid, None)
                    log('C-CAP sid=%s %dB' % (sid, len(buf)))
                    conn.sendall(b'E oversize\n')
                    break
                buf.extend(data)
            conn.sendall(b'A\n')
        elif p[0] == 'T' and len(p) >= 4:
            with slock:
                blob = bytes(sessions.pop(p[1], b''))
            wlen = int(p[2]); plen = int(p[3])
            wavb, pngb = blob[:wlen], blob[wlen:wlen + plen]
            if not wavb:
                conn.sendall(b'E no-audio\n')
                continue
            conn.sendall(b'G\n')
            # Vita sceNet drops TCP ~8-11s after connect (measured). NEVER hold a
            # connection across the pipeline: the result is parked in REPLIES and
            # the client fetches it on a fresh short-lived P connection.
            with slock:
                REPLIES[sid] = {'state': 'pending'}

            def worker(w=wavb, g=pngb, s=sid):
                try:
                    reply, out = run_pipeline(w, g, log)
                    with slock:
                        REPLIES[s] = {'state': 'done', 'wav': out, 'text': reply}
                    log('PIPE-DONE %s %dB' % (s, len(out)))
                except Exception as e:
                    with slock:
                        REPLIES[s] = {'state': 'error', 'err': str(e)[:60]}
                    log('PIPE-ERR %s %s' % (s, e))
            threading.Thread(target=worker, daemon=True).start()
            return
        elif p[0] == 'D':
            with slock: sessions.pop(sid, None); conn.sendall(b'A\n')
        elif p[0] == 'X':
            ABORT.set(); conn.sendall(b'A\n')
        else:
            conn.sendall(b'A\n')
    with slock: sessions.pop(sid, None)


class H(BaseHTTPRequestHandler):
    def log(self, m):
        print('%s %s %s' % (now(), self.client_address[0], m), flush=True)

    def _authed(self):
        return ok_token(self.headers.get('X-Pair-Token', ''))

    def _pull(self, sid):
        with slock:
            d = REPLIES.get(sid)
        if d is None:
            self.connection.sendall(b'E unknown-session\n')
        elif d['state'] == 'pending':
            self.connection.sendall(b'W\n')
        elif d['state'] == 'error':
            self.connection.sendall(('E pipeline: ' + d['err'] + '\n').encode())
        else:
            wav = d['wav']
            txt = (d.get('text') or '')[:300].encode('utf-8', 'ignore')
            self.connection.sendall(('R %d %d\n' % (len(wav), len(txt))).encode())
            for i in range(0, len(wav), 2048):
                self.connection.sendall(wav[i:i+2048])
                time.sleep(0.04)
            if txt:
                self.connection.sendall(txt)
                time.sleep(0.04)
            with slock:
                REPLIES.pop(sid, None)
            self.log('PULL-R %s %d bytes' % (sid, len(wav)))

    def _reject(self, reason=b'E pair\n'):
        # drain whatever the client already pipelined, THEN answer and close;
        # closing over unread bytes RSTs the socket and eats our reply
        try:
            self.connection.settimeout(0.5)
            while self.connection.recv(4096):
                pass
        except Exception:
            pass
        try:
            self.connection.sendall(reason)
        except Exception:
            pass

    def handle(self):
        try:
            first = self.connection.recv(2, socket.MSG_PEEK)
        except Exception:
            first = b''
        if first.startswith(b'K'):
            # every socket connection authenticates before it can do anything
            # (MSG_PEEK may have returned only b'K' — readline waits for the line)
            try:
                line = readline(self.connection)
                if not ok_token((line or '')[2:]):
                    self.log('AUTH-REJECT')
                    self._reject()
                    return
                nxt = readline(self.connection)
                q = (nxt or '').split()
                if len(q) >= 2 and q[0] == 'U':
                    handle_session(self.connection, q[1], self.client_address[0])
                elif len(q) >= 2 and q[0] == 'P':
                    self._pull(q[1])
                else:
                    self.connection.sendall(b'E bad-cmd\n')
            except Exception as e:
                self.log('SESS-ERR %s' % e)
            finally:
                try: self.connection.close()
                except Exception: pass
            return
        if first in (b'U ', b'P '):
            # unauthenticated socket hello (pre-pairing clients): refuse, don't serve
            self.log('AUTH-REJECT no-token')
            self._reject()
            return
        BaseHTTPRequestHandler.handle(self)

    def do_GET(self):
        if self.path == '/ping':
            b = b'HERMES-REACHABLE'
            self.send_response(200); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
            return
        if not self._authed():
            self.send_error(403); return
        if self.path.startswith('/files/'):
            name = self.path.split('/')[-1].replace('..', '')
            fp = os.path.join(FILES_DIR, name)
            if os.path.isfile(fp):
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(os.path.getsize(fp)))
                self.end_headers()
                with open(fp, 'rb') as fh:
                    while True:
                        chunk = fh.read(1 << 20)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return
            self.send_error(404); return
        self.send_error(404)

    def do_POST(self):
        # auth BEFORE touching the body: reading Content-Length bytes first
        # would let an unauthenticated peer force allocation of any size
        if not self._authed():
            self.send_error(403); return
        n = int(self.headers.get('Content-Length', 0))
        if n < 0 or n > MAX_SESSION_BYTES:
            self.send_error(400); return
        body = self.rfile.read(n)
        if self.path == '/abort':
            ABORT.set(); self.log('ABORT'); b = b'ok'
            self.send_response(200); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
            return
        if self.path != '/talk' or n < 100:
            self.send_error(400); return
        wl = int(self.headers.get('X-Wav-Len', n))
        wav, png = body[:wl], body[wl:]
        reply, w2 = run_pipeline(wav, png, self.log.__call__)
        self._reply(w2, reply)

    def _reply(self, wav, text):
        self.send_response(200)
        self.send_header('Content-Type', 'audio/wav')
        self.send_header('Content-Length', str(len(wav)))
        self.send_header('X-Reply', urllib.parse.quote(text[:4000]))
        self.end_headers()
        self.wfile.write(wav)

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    os.makedirs(FILES_DIR, exist_ok=True)
    print('pairing enforced: token at %s (0600)' % TOKEN_FILE, flush=True)
    ThreadingHTTPServer(('0.0.0.0', PORT), H).serve_forever()
