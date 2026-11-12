vitam installed.

Next steps:
1. Point settings at your Hermes gateway: `gateway_url`, a `python_bin` interpreter that has `faster-whisper`, `numpy`, and `underdog-woof` available (a checkout dir — `hf download ConwayResearch/Underdog-Woof --local-dir underdog-woof` — referenced by `woof_dir`, or pip-importable), and an Underdog Woof install for TTS.
2. Export your gateway bearer token into `HERMES_VITA_GATEWAY_KEY` (or rename via `gateway_key_env`). The plugin fails closed if it is missing for a remote gateway.
3. `/vitam check` → `/vitam start` → `/vitam status`. `start` prints the pairing token; `/vitam token` re-shows it.
4. Vita side: build `HERMES.vpk` from `client/` (see `docs/build-client.md`), set `BRIDGE_IP` and `PAIR_TOKEN` in `index.lua`, install via molecularShell (SELECT = FTP server). For an already-installed VPK, edit `ux0:/app/LPPV00001/index.lua` in molecularShell's editor instead.

Every bridge connection (socket or HTTP) is gated on that pairing token — an unpaired Vita gets `E pair` / HTTP 403.

Logs: `/vitam logs 100`. Stop: `/vitam stop`.
