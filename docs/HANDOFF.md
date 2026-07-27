# LR3-AudioZone — hand-off / continue here

Everything a fresh session needs to continue this project. Pair with `CLAUDE.md` (full protocol +
architecture) and the runnable scripts in `tools/`. Written 2026-07-27 after the first real-LARA test.

## TL;DR — where we are

- **Goal:** turn on Spotify Connect on the phone → the add-on plays it on ELKO EP **LARA** radios by
  acting as a **Slim server (SlimProto)** and pushing the LARA to fetch our Icecast MP3 mount.
- **Proven on a real LARA** (fw **3.7.001**, MAC `00:0A:59:F2:23:1C`, CSModel `squeezeslave`):
  SlimProto **HELO gate PASSED**, LARA advertises **`mp3`**, and a pushed **`strm-s` switched the
  LARA to its "Audio zóna" source**.
- **Not yet verified on-device:** the fully **automatic** flow (Spotify-active → discover → push →
  LARA *audibly* plays, and back to fallback/stop on pause). The add-on (v0.1.0) is scaffolded and
  its parts are proven individually; the end-to-end loop needs one more on-device session.

## Proven vs. left

**Proven**
- Discovery works (by **TCP scan of 61695** — UDP broadcast is dropped by Windows Firewall; on HA/Linux it's fine).
- SlimProto HELO + `mp3` codec advertised.
- The **real** `lr3_audiozone/lr3ctl/slimproto.py` (full handshake `vers`/`setd`/`aude`/`audg` + `strm-t`
  heartbeat) keeps the LARA connected; a minimal listener drops it after ~17 s.
- `push_stream` (`strm-s`) makes the LARA switch its source to "Audio zóna".

**Left (do these on-device)**
1. Confirm **audible** playback of `/default` via the auto controller (`control_mode: slimproto`),
   i.e. run the add-on (or `tools/play_test.py` first), play Spotify to the "Audio zóna" device, hear it.
2. Pause → after `fallback_delay` either fallback radio (LARA keeps playing) or **stop** the LARA;
   resume → instant. Verify `stop` (`strm-q`) and `set_volume` (`audg`).
3. Mount-switch **latency**; **multiple LARAs** at once (mount `default` → all discovered radios).
4. **LMS CLI (port 9595):** the LARA's "Audio zone" config also has a CLI port + LMS user/pass.
   Open question whether the LARA needs the CLI for full zone control/sync, or SlimProto `strm`
   alone suffices (basic play worked over 3483 without us serving 9595). If needed, add a minimal
   CLI responder (telnet-style LMS command interface).
5. Does some firmware also need a **61695 SOURCE-select** alongside `strm`? (Ours didn't — `strm`
   alone switched it.)

## Device-side prerequisite (must be set on each LARA)

The LARA must have **"Audio zone function"** enabled and its **slim-server IP = the HA/host IP**.
Two ways to set it (no way to do it safely over 61695 — a config write there Saves the whole config):
- **ELKO Configurator** (Windows app), or
- the LARA **web UI**: `http://<lara-ip>` (HTTP **Digest** auth, realm "LARA", default `admin`/`elkoep`).
  It's an SPA ("LARA configurator", `index.html`+`index.js`). Section **"Audio zone function"** =
  checkbox `controll_bit_az` (config field `audio_zone_enabled`) + IP fields `slim_ip_1..4`
  (config field `audio_zone_ip`). That section also holds a **CLI port (9595) + username/password**.
  Set it there and **Save** (its own POST serializes the full config correctly).
- Use `tools/web_explore.py <lara-ip>` to inspect the web UI / confirm the fields.

## How to test — `tools/`

Run from the repo root. PowerShell scripts load the XOR mask from `lr3_audiozone/lr3ctl/elkoproto.py`
via a path relative to the script, so they work wherever the repo lives. See `tools/README.md`.

| Step | Command |
|---|---|
| Find LARAs (firewall-proof) | `tools/scan_tcp.ps1 -Subnet 10.0.0.` |
| Find LARAs (UDP broadcast) | `tools/discover.ps1`  *(may be firewall-blocked on Windows)* |
| Read-only control smoke | `tools/control_smoke.ps1 -Ip <lara-ip>`  (fw/hw, status, presets) |
| SlimProto HELO gate | `tools/slim_listen.ps1`  (listen :3483, log HELO + caps) — point a LARA at this host first |
| Verify an MP3 stream is live | `python tools/check_stream.py <url>` |
| Full play test (real slimproto.py) | `python tools/play_test.py <our-ip> <mp3-url>` — runs the real server + pushes `strm-s` |

## Windows dev-env notes (same laptop)

- **Python 3.12**: `%LOCALAPPDATA%\Programs\Python\Python312\python.exe` (pyyaml installed).
- **Windows Firewall** blocks inbound by default → to receive SlimProto/discovery you must allow the
  ports (run an **Administrator** PowerShell):
  ```
  New-NetFirewallRule -DisplayName "LR3 SlimProto 3483" -Direction Inbound -Protocol TCP -LocalPort 3483 -Action Allow -Profile Any
  New-NetFirewallRule -DisplayName "LR3 SlimProto UDP 3483" -Direction Inbound -Protocol UDP -LocalPort 3483 -Action Allow -Profile Any
  ```
  (LARA UDP discovery replies come to :61695 — allow UDP 61695 too if you rely on `discover.ps1`.)
  Clean up later: `Remove-NetFirewallRule -DisplayName "LR3 SlimProto 3483"` (etc.).
- Antivirus occasionally **EPERM**-blocks rapid PowerShell socket spawns — run scripts via `-File`,
  prefer the Python tools for sockets.
- **None** of this applies on the real HA (Linux) — the add-on's own dbus/avahi + host_network handle it.

## Raw captures (reference)

**HELO** from the LARA (decoded payload text):
```
CSModel=squeezeslave,ModelName=LARA,Firmware=3.7.001,wma,mp3,HasDigitalOut=0
```
dev_id=12. Caps sit at ~byte 34 of the HELO payload (NOT 24 — `slimproto.py` now finds the printable run).

**TCP test-packet reply** (61695, unauth), decoded: `ff fa fa ff 0e 1e 10 40 01 00 03 00 90 89 01 …`
→ `d[8..10]=1,0,3` identifies an ELKO device; `fw = d[11]<<16 | d[12]<<8 | d[13] = 37001` (=3.7.001), `hw=d[14]=1`.
⚠️ In a PowerShell port, cast bytes to `[int]` before `-shl` (a `[byte] -shl 8` truncates to 0). The Python code is fine.

**Status reply** (61695), decoded head: `… 00 c1 01 01 00 00 55 00 01` → `d[7..10]=0,193,1,1`.
The reference lib expects `d[10]==0` but this fw returns **1** (payload offsets unchanged) — `elkoproto.py`
`parse_status_reply`/`parse_stations_reply` no longer match on `d[10]`. Parsed: source=0, station=0, volume=0x55, playing=1.
```

Next natural step: get the LARA pointed at the HA (Audio zone function + IP), then `tools/play_test.py`,
then run the actual add-on with `control_mode: slimproto` and walk the on-device checklist above.
