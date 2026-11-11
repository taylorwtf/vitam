# Development notes

Hard-won platform rules for anyone modifying the client (`client/index.lua`)
or the daemon (`daemon/vita_bridge.py`). All of these were verified on real
hardware (PCH-2000) — violating them produces silent corruption or crashes,
not clean errors.

## Client (lpp-vita Lua)

- **Never call Graphics draw functions inside the mic-recording wait loop.**
  Drawing there starves the native mic callback; the capture buffer becomes
  uninitialized heap memory (random noise or zero-length WAV) or the app
  crashes with a data-abort. Polling `Controls.read()` alone in that loop is
  safe — that is how hold-to-talk (release-to-send, O-abort) works.
- **`Camera.getOutput` returns lpp's static camera texture** (`cam_buf`, an
  RGBA8 copy in the lpp runtime). Do not `Graphics.freeImage` it — the next
  capture dereferences freed memory and the app dies at `vita2d_free_texture`.
- **First camera frames are unexposed black.** The sceCam sensor needs ~10
  frame-times to reach exposure; the client discards warmup frames before the
  capture is considered live. (Verified: mean luminance of the first reads is
  ≤2/255, then rises to the real scene.)
- **Text layout is measured, not counted.** `Graphics.debugPrint` uses the
  duplex PGF font with per-glyph advances (`pen_x += advance_x >> 6` per
  UTF-8 codepoint). The client's wrap/fit engine uses an advance table
  measured on-device (pair-row differencing) — char-count budgets overflow
  proportional text.
- **`Graphics.debugPrint2` (shadowed variant) crashes** with "Attempt to index
  a nil value (field '?')" on lpp 0.8.1 — use `debugPrint` + `setLineSpacing`.

## Daemon / protocol

- **Vita sockets die ~10s after connect.** The client never blocks waiting
  for a reply; every request is a fresh short-lived connection, and results
  are parked server-side (`PENDING` dict) for short-budget polls. Don't
  "simplify" this into a persistent connection — it does not survive on
  this hardware.
- **WAV transfer is content-length framing** (`W <name> <len>` + raw bytes +
  a `c`-prefixed byte-sum line). The client reads its own file size to send
  an exact N; don't switch to line-based delimiters — WAV bytes contain
  newlines.
- **Bridge sends may arrive while the Vita is mid-audio-capture.** The
  mic-loop corruption above is client-side, but keep daemon replies
  unsolicited-pushable (park-and-poll covers this).

## Hardware notes

- PCH-2000 with a PCH-1000 TA (camera daughterboard) installed: the 1000 TA
  carries the rear camera and the GPS antenna; without it the Vita reports
  no camera at all.
- molecularShell FTP (SELECT) is the transport for VPK updates:
  `/ux0:/data/HERMES.vpk` for in-place title updates; full install of an
  already-installed title can fail `0x80101130` (title conflict) — prefer
  data-dir updates during development.
