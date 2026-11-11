vitam installed.

Next steps:
1. Point settings at your Hermes gateway: `gateway_url`, and a `python_bin` interpreter that has `faster-whisper`, `numpy`, and an `underdog-woof` checkout (or set `woof_dir`).
2. Export your gateway bearer token into `HERMES_VITA_GATEWAY_KEY` (or rename via `gateway_key_env`). The plugin fails closed if it is missing for a remote gateway.
3. `/vitam check` → `/vitam start` → `/vitam status`.
4. Vita side: build `HERMES.vpk` from `client/` (see `docs/build-client.md`), set `BRIDGE_IP` in `index.lua` to this host, install via molecularShell (SELECT = FTP server).

Logs: `/vitam logs 100`. Stop: `/vitam stop`.
