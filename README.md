# vita-bridge

Voice + camera bridge between a modded PS Vita (HENkaku 3.60 + [lpp-vita](https://github.com/LuaPlayerPlus/lpp-vita) client) and the Hermes gateway. The Vita becomes a push-to-talk handheld agent: mic → faster-whisper STT → Hermes agent turn (optionally with a rear-camera frame for vision) → TTS reply (Underdog Woof) spoken through the device speaker, with the transcript shown on screen.

Two steps to running:

1. **Configure + start the bridge daemon** (runs on any LAN machine):
   - `hermes plugins install <owner>/vita-bridge && hermes plugins enable vita-bridge`
   - Set settings: `gateway_url` (Hermes gateway OpenAI endpoint), `python_bin` (interpreter that has `faster-whisper` + `numpy` + an `underdog-woof` checkout importable, or set `woof_dir`). Export the gateway bearer token into the env var named by `gateway_key_env` (default `HERMES_VITA_GATEWAY_KEY`) — the token is never stored in config or git.
   - `/vita-bridge check` then `/vita-bridge start`.
2. **Install the Vita client**: build `HERMES.vpk` from `client/` (see `docs/build-client.md`) or ship your own; in `index.lua` set `BRIDGE_IP` to the daemon host. Install via molecularShell (SELECT = FTP server, upload VPK, select + X). On the Vita: press X to talk, TRIANGLE to arm the camera.

## What it is / is not

- LAN-only by design: the daemon binds `0.0.0.0:<port>` on your network; no cloud, no outbound calls except to your configured Hermes gateway.
- The wire protocol is raw TCP (documented in `daemon/vita_bridge.py` header). Vita sockets die ~10s after connect, so results are parked server-side and polled on fresh short-lived connections — do not "simplify" this.
- Optional FTP-pull fallback (`ftp_user`/`ftp_pass` settings) for file transfer to/from the Vita; leave empty to disable.

## Limitations (honest)

- STT/TTS quality is what `base` whisper + a tiny TTS give on consumer hardware — intelligible, not studio.
- The Vita speaker feeds the mic during replies; keep the device away from the speaker or gate your mic window ~0.5s after playback ends.
- Push-to-talk 6s capture; no wake-word/hands-free mode (Vita cannot always-listen).
- Never call Lua draw functions inside the `Mic.isRecording()` wait loop: it starves the native mic callback and the capture buffer becomes uninitialized heap memory. `client/index.lua` ships the corrected pure-wait loop.
