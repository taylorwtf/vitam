#!/usr/bin/env python3
# vita-bridge daemon — PS Vita handheld <-> Hermes gateway voice/camera bridge.
#
# All configuration arrives via VB_* environment variables (set by the plugin
# lifecycle manager); nothing is hardcoded: no credentials, no LAN IPs, no
# absolute paths. Wire protocol (raw TCP over the HTTP port; Vita lpp sockets
# partial-send, so every frame is acked and the pipeline NEVER holds one
# connection — results are parked and fetched on fresh short-lived sockets):
#   "U <id>\n" open | "C <n>\n"+bytes -> "A\n" | "T <id> <wavlen> <pnglen>\n"
#     -> "G\n" then result via  P <id> -> "W"(pending) | "E <msg>" | "R <alen> <tlen>\n"+body
#   "T <id> F <wavname> <pngname|-> [H <ip>]"  FTP-pull fallback (needs ftp_user/pass)
#   "D <id>\n" discard | "X\n" abort | HTTP /ping /files/<name> /talk /abort
import os, sys, io, json, wave, base64, socket, threading, time
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
FTP_USER     = os.environ.get('VB_FTP_USER', '')
FTP_PASS     = os.environ.get('VB_FTP_PASS', '')

sys.path.insert(0, os.path.join(FILES_DIR, 'whisper'))
if WOOF_DIR:
    sys.path.insert(0, WOOF_DIR)

from faster_whisper import WhisperModel
model = WhisperModel(STT_MODEL, device=('cuda' if STT_DEVICE == 'auto' else STT_DEVICE),
                     compute_type='float16')
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
    req = urllib.request.Request(GATEWAY_URL, json.dumps(body).encode(), {
        'Content-Type': 'application/json',
        'Authorization': '***' + GATEWAY_KEY,
        'X-Hermes-Session-Key': SESSION_KEY})
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


def ftp_pull(peer_ip, name, timeout=30):
    # Config-gated fallback (plugin settings ftp_user/ftp_pass). Push-mode is the
    # default path; FTP-pull exists only for clients that store-then-notify.
    if not FTP_USER:
        raise RuntimeError('ftp-pull disabled: set ftp_user/ftp_pass in plugin settings')
    import ftplib
    f = ftplib.FTP()
    try:
        f.connect(peer_ip, 1337, timeout=timeout)
        f.login(FTP_USER, FTP_PASS)
        buf = bytearray()
        f.retrbinary('RETR /ux0:/data/' + name, buf.extend)
        try:
            f.quit()
        except Exception:
            pass
        return bytes(buf)
    finally:
        try:
            f.sock.close()
        except Exception:
            pass


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
            data = readn(conn, int(p[1]))
            if data is None:
                break
            with slock:
                sessions.setdefault(sid, bytearray()).extend(data)
            conn.sendall(b'A\n')
        elif p[0] == 'T' and len(p) >= 3:
            if p[2] == 'F':
                try:
                    host = addr
                    if 'H' in p:
                        host = p[p.index('H') + 1]
                    wavb = ftp_pull(host, p[3].split(':')[-1])
                    pngb = b''
                    if len(p) > 4 and p[4] != '-':
                        pngb = ftp_pull(host, p[4].split(':')[-1])
                    log('FTP-PULL %s=%d png=%d' % (p[3], len(wavb), len(pngb)))
                except Exception as e:
                    log('FTP-ERR %s' % e)
                    wavb, pngb = b'', b''
                with slock:
                    sessions.pop(p[1], None)
            else:
                with slock:
                    blob = bytes(sessions.pop(p[1] if len(p) > 1 else sid, b''))
                wlen = int(p[2]); plen = int(p[3]) if len(p) > 3 else 0
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

    def handle(self):
        try:
            first = self.connection.recv(4, socket.MSG_PEEK)
        except Exception:
            first = b''
        if first.startswith(b'P '):
            try:
                line = readline(self.connection)
                sid = line.split()[1] if line and len(line.split()) > 1 else 'x'
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
            except Exception as e:
                self.log('P-ERR %s' % e)
            finally:
                try: self.connection.close()
                except Exception: pass
            return
        if first.startswith(b'U '):
            try:
                line = readline(self.connection)
                sid = line.split()[1] if line and len(line.split()) > 1 else 'x'
                handle_session(self.connection, sid, self.client_address[0])
            except Exception as e:
                self.log('SESS-ERR %s' % e)
            finally:
                try: self.connection.close()
                except Exception: pass
            return
        BaseHTTPRequestHandler.handle(self)

    def do_GET(self):
        if self.path == '/ping':
            b = b'HERMES-REACHABLE'
            self.send_response(200); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
            return
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
        n = int(self.headers.get('Content-Length', 0))
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
    ThreadingHTTPServer(('0.0.0.0', PORT), H).serve_forever()
