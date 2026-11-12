# Building the Vita client (HERMES.vpk)

1. Get lpp-vita's prebuilt eboot (unsafe-homebrew build) — upstream CI publishes it; do not compile your own unless you maintain a vitasdk toolchain.
2. `index.lua` is the whole app. Set `BRIDGE_IP` (top of file) to your vitam host and `PAIR_TOKEN` to the token printed by `/vitam start` (or `/vitam token`).
3. Package: a VPK is a zip of `eboot.bin`, `index.lua`, `sce_sys/param.sfo`, the app art (`bg.png`, `splash.png`, `avatar.png`, `half.png`, `blink.png`, `smile.png`, `talk_a.png`, `talk_b.png`), and any sound assets.
4. Install: molecularShell → SELECT (FTP server) → upload VPK (or place it in `ux0:/data/` for an in-place update) → install.
5. Hard rules baked into the app: zero Graphics draw calls in the recording wait loop (drawing there corrupts captures or crashes the app); `Controls.read()` polling alone is safe. `Camera.getOutput` returns lpp's static texture — never `Graphics.freeImage` it, or the next capture crashes.
