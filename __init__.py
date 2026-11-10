"""vita-bridge plugin: lifecycle manager for the Vita voice/camera bridge daemon.

Commands:  /vita-bridge start|stop|status|logs [N]|check
The daemon itself (daemon/vita_bridge.py) runs under a *host* Python you point
at via the `python_bin` setting — Hermes' own venv stays untouched. All
configuration comes from plugin settings; secrets come only from the ENV var
named by `gateway_key_env` (and optional ftp settings), never from git.
"""
import os, signal, subprocess, sys, urllib.request

_CMD = 'vita-bridge'
_HERE = os.path.dirname(os.path.abspath(__file__))


def _data_dir(ctx):
    try:
        from hermes_cli.plugin_storage import plugin_data_dir
        return plugin_data_dir(_CMD)
    except Exception:
        d = os.path.join(_HERE, 'var')
        os.makedirs(d, exist_ok=True)
        return d


def _env_for(ctx):
    cfg = lambda k, d='': ctx.get_config(k, d)
    env = dict(os.environ)
    env['VB_GATEWAY_URL']    = cfg('gateway_url', 'http://127.0.0.1:8642/v1/chat/completions')
    env['VB_SESSION_KEY']    = cfg('session_key', 'vita-handheld')
    env['VB_STT_MODEL']      = cfg('stt_model', 'base')
    env['VB_STT_DEVICE']     = cfg('stt_device', 'auto')
    env['VB_WOOF_DIR']       = cfg('woof_dir', '')
    env['VB_TTS_CAP']        = str(cfg('tts_cap_chars', 400))
    env['VB_PORT']           = str(cfg('port', 8643))
    env['VB_FTP_USER']       = cfg('ftp_user', '')
    env['VB_FTP_PASS']       = cfg('ftp_pass', '')
    key_env = cfg('gateway_key_env', 'HERMES_VITA_GATEWAY_KEY')
    if key_env not in env and not cfg('gateway_url', '').startswith('http://127'):
        # fail closed on the transport secret: the daemon refuses to start
        # against a remote gateway without it rather than sending anonymous traffic
        return env, 'missing env %s (gateway bearer token)' % key_env
    env['VB_GATEWAY_KEY'] = env.get(key_env, '')
    return env, None


def _pid(path):
    try:
        return int(open(path).read().strip())
    except Exception:
        return None


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _handle(ctx, args):
    dd = _data_dir(ctx)
    pidf = os.path.join(dd, 'bridge.pid')
    logf = os.path.join(dd, 'bridge.log')
    sub = (args or '').split()
    cmd = sub[0] if sub else 'status'

    if cmd == 'start':
        pid = _pid(pidf)
        if _alive(pid):
            return 'already running (pid %d)' % pid
        env, err = _env_for(ctx)
        if err:
            return 'refusing to start: %s' % err
        python_bin = ctx.get_config('python_bin', '') or sys.executable
        with open(logf, 'ab') as lf:
            p = subprocess.Popen([python_bin, os.path.join(_HERE, 'daemon', 'vita_bridge.py')],
                                 stdout=lf, stderr=lf, env=env, cwd=dd)
        open(pidf, 'w').write(str(p.pid))
        return 'started pid %d (log: %s)' % (p.pid, logf)

    if cmd == 'stop':
        pid = _pid(pidf)
        if not _alive(pid):
            return 'not running'
        os.kill(pid, signal.SIGTERM)
        return 'SIGTERM -> pid %d' % pid

    if cmd == 'logs':
        n = int(sub[1]) if len(sub) > 1 and sub[1].isdigit() else 40
        try:
            tail = open(logf, 'r', errors='replace').read().splitlines()[-n:]
            return '\n'.join(tail) if tail else 'log empty'
        except FileNotFoundError:
            return 'no log yet'

    if cmd == 'check':
        out = []
        python_bin = ctx.get_config('python_bin', '') or sys.executable
        r = subprocess.run([python_bin, '-c', 'import faster_whisper, numpy'],
                           capture_output=True, text=True, timeout=60)
        out.append('daemon deps under %s: %s' % (python_bin, 'ok' if r.returncode == 0 else 'MISSING (%s)' % r.stderr.strip()[:120]))
        pid = _pid(pidf)
        out.append('daemon process: %s' % ('running pid %d' % pid if _alive(pid) else 'not running'))
        port = ctx.get_config('port', 8643)
        try:
            b = urllib.request.urlopen('http://127.0.0.1:%s/ping' % port, timeout=3).read()
            out.append('/ping: %s' % b.decode(errors='replace'))
        except Exception as e:
            out.append('/ping: unreachable (%s)' % type(e).__name__)
        return '\n'.join(out)

    if cmd == 'status':
        pid = _pid(pidf)
        return 'running (pid %d)' % pid if _alive(pid) else 'not running'

    return 'usage: /vita-bridge start|stop|status|logs [N]|check'


def register(ctx):
    ctx.register_command(
        _CMD, _handle,
        description='Manage the PS Vita voice/camera bridge daemon.',
        args_hint='start|stop|status|logs [N]|check')
