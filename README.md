# vitam

![vitam banner](assets/banner.png)

Turn a modded PS Vita into a voice-powered Hermes handheld. Hold **X**, talk, release — the Vita sends your voice to your own Hermes gateway, and the agent answers out loud through the Vita's speaker with the text on screen. Press **R** and the next message includes a photo from the Vita's rear camera, so the agent can see what you're pointing at.

Everything runs on your network. The Vita talks only to the bridge on your machine; the bridge talks only to your Hermes gateway. No cloud services, no accounts, no phone app.

## What you need

- A PS Vita or PS TV running HENkaku / h-encore on 3.60 ([setup guide](https://henkaku.xyz))
- A computer on the same network — the bridge runs there
- Python 3 with `faster-whisper` and `numpy` installed on that computer

## Setup — about 5 minutes

### 1. The bridge (your computer)

```
hermes plugins install taylorwtf/vitam
hermes plugins enable vitam
```

Point it at your Hermes gateway:

```
hermes plugins config vitam
```

| Setting | What to put |
|---|---|
| `gateway_url` | Your Hermes gateway endpoint (default `http://127.0.0.1:8642/v1/chat/completions`) |
| `python_bin` | Path to a Python 3 that has `faster-whisper` + `numpy` |

Export your gateway token (it is never stored in config or git):

```
export HERMES_VITA_GATEWAY_KEY=<your-gateway-token>
```

Then start it:

```
/vitam check     # deps, process, reachability
/vitam start
```

### 2. The Vita

**Easiest:** download the prebuilt `HERMES.vpk` from [Releases](https://github.com/taylorwtf/vitam/releases), install it with molecularShell (press **SELECT** to start the FTP server, upload the VPK, select it, press **X**), then open molecularShell's editor on `ux0:/app/LPPV00001/index.lua`, set `BRIDGE_IP` (line 26) to your computer's LAN address, and restart the app. Until that's set, the app will show `connect_fail` — that's expected.

**Build your own** (if you want to modify the client): build from the `client/` folder — [docs/build-client.md](docs/build-client.md) walks through it in five steps. Before building, set `BRIDGE_IP` in `client/index.lua`.

## Using it

| Button | Action |
|---|---|
| **Hold X** | Record; release to send (up to 30s, at least ~0.6s) |
| **R** | Arm the rear camera — the next message gets a photo |
| **O** | Cancel recording / stop speaking |
| **D-pad** | Scroll the chat log |
| **START** | Quit |

On-screen boxes tell you what's happening at every moment: `REC` while you hold, `SENDING`, `THINKING...` with a live timer, `SPK` while the reply plays. `O` aborts anything mid-flight.

## Good to know

- Speech recognition and text-to-speech run locally on your computer with small free models — intelligible, not studio quality.
- The Vita's mic can hear its own speaker; set the device down away from the speaker while it talks, or tap **O** before replying if you want to talk over it.
- There is no wake word or hands-free mode — the Vita only listens while you hold X. That's deliberate: nothing is captured unless you ask.

## When something goes wrong

- `connect_fail` on the Vita → `BRIDGE_IP` doesn't match the machine running `/vitam start`, or a firewall is blocking the port (default 8643).
- `/vitam check` shows what's up and what isn't.
- `/vitam logs 100` shows what the bridge did.
- `/vitam stop` shuts the daemon down.

## For developers

The client is a single-file Lua app for [lpp-vita](https://github.com/LuaPlayerPlus/lpp-vita); the daemon is one Python file with the wire protocol documented in its header. If you plan to modify either, read [docs/development-notes.md](docs/development-notes.md) first — it collects the platform-specific traps (mic loop, sockets, camera buffers) that cost real debugging time to find.
