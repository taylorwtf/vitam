# Building the Vita client (HERMES.vpk)

1. Get lpp-vita's prebuilt eboot (unsafe-homebrew build) — upstream CI publishes it; do not compile your own unless you maintain a vitasdk toolchain.
2. `index.lua` is the whole app. Set `BRIDGE_IP` (top of file) to your vita-bridge host.
3. Package: a VPK is a zip of `eboot.bin`, `index.lua`, `sce_sys/param.sfo`, and the app assets (`bg.png`, `avatar.png`, `blink.png`, `talk.png`).
4. Install: molecularShell → SELECT (FTP server) → upload VPK to ux0: → select it, press X.
5. Hard rule baked into the app: zero Graphics draw calls inside the `Mic.isRecording()` wait loop — drawing there corrupts captures (uninitialized mic buffer) or crashes the app.
