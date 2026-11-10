vita-bridge installed.

Next steps:
1. Point settings at your Hermes gateway: `gateway_url`, and a `python_bin` interpreter that has `faster-whisper`, `numpy`, and an `underdog-woof` checkout (or set `woof_dir`).
2. Export your gateway bearer token into `HERMES_VITA_GATEWAY_KEY` (or rename via `gateway_key_env`). The plugin fails closed if it is missing for a remote gateway.
3. `/vita-bridge check` → `/vita-bridge start` → `/vita-bridge status`.
4. Vita side: molecularShell FTP (SELECT) → upload `HERMES.vpk` → install → set `BRIDGE_IP` in `index.lua` to this host.

Logs: `/vita-bridge logs 100`. Stop: `/vita-bridge stop`.
