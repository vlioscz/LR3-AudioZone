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
  LARA *audibly* plays, and back to off on pause). The add-on (v0.2.0) is complete and its parts
  are proven individually; the end-to-end loop needs one more on-device session.

## 0.2.0 — what changed (2026-07-27)

The fallback radio is **gone**. It was the LR3-Stream-era workaround (keep a stream alive so the
LARA always has something to play); with the Slim path working we drive the LARA directly instead:

- `radio.liq.tpl` — only Spotify → silence. The silence exists purely so the Icecast mount never
  dies, because the LARA has to be able to fetch it the instant we send `strm-s`.
- `slimproto.py` — added LMS-style **power** (`aude 1 1` / `aude 0 0`; a player starts powered
  **down** at HELO), **STAT parsing** (mode + elapsed, 53-byte struct), `pause()`, `stream_url()`,
  and `on_disconnect`/`on_state` callbacks.
- `lmscli.py` — **new**: the LMS CLI server on :9595 (see below).
- `controller.py` — single mount, idle→off state machine (`idle_timeout`), radios learned from
  SlimProto connections as well as UDP discovery, LARA button presses routed back from the CLI.
- options: `fallback_*` removed; added `idle_timeout`, `zone_volume`, `cli_port`, `cli_username`,
  `cli_password`, `lara_off_action`.

⚠️ Supervisor keeps previously saved options. If the add-on refuses to start after the update
because of the removed `fallback_*` keys, open its Configuration tab and save it again.

## Proven vs. left

**Proven**
- Discovery works (by **TCP scan of 61695** — UDP broadcast is dropped by Windows Firewall; on HA/Linux it's fine).
- SlimProto HELO + `mp3` codec advertised.
- The **real** `lr3_audiozone/lr3ctl/slimproto.py` (full handshake `vers`/`setd`/`aude`/`audg` + `strm-t`
  heartbeat) keeps the LARA connected; a minimal listener drops it after ~17 s.
- `push_stream` (`strm-s`) makes the LARA switch its source to "Audio zóna".

**Proven on the device 2026-07-27 (the 0.2.0 session)**
- **Audio actually plays.** 60 s continuous, `bytes_rx` 1.46 MB, input buffer steady at ~62 KB of
  the 128 KB the player reports, **zero underruns**. `strm-q` + `aude 0 0` closes the audio
  connection (`STMf`) and the LARA stays off.
- **This required `server_ip=0` in `strm-s`.** With an explicit address the LARA sits in `STMc`
  with `bytes_received=0` — source switches to "Audio zóna", but silence. Probe of 8 variants:
  `autostart=1, thr=20, ip=ours` failed; the same with `ip=0` fetched within a second. Also
  `autostart='1'` (not `'3'` — no direct streaming) and `threshold=64` KB (200 exceeded the
  player's buffer; 20 underran).
- **The LARA opens the LMS CLI on :9595** — open question #4 answered, it is a real dependency.
  Verbatim session: `login admin elkoep`, `<mac> artist ?`, `<mac> stop`, `<mac> mixer volume 95`,
  then `playlist tracks ?` every 5 s. It never sends `listen 1` (polls instead of subscribing).
  The initial `stop` is state sync — treating it as a button made the controller flap, so
  `lmscli.HANDSHAKE_GRACE` ignores transport commands for the first 5 s of a session.
- STAT frames are **51 B** on this fw (no trailing `error_code`).

Also proven offline (unit-level): `strm` body is 24 B + the embedded HTTP request, STAT parses to
mode/elapsed, the CLI dispatch answers every implemented command on a single line, and the
controller state machine handles on/idle/off/resume plus SlimProto-only discovery.

**Ran on HA, 0.2.1 fixes what it found**
- The whole loop works: Spotify → LARA plays; disconnect → it notices in ~5 s and stops.
- **`aude 0 0` only mutes.** The unit stayed lit with a dead audio zone on the display for ~10 s
  after. Hence the new default `lara_off_action: radio` — after `strm-q` we send
  `select_source(RADIO)` + `stop` over 61695 so it lands on the station list, prepared but not
  playing. (Needs `lara_username`/`lara_password`; 61695 is the only authenticated path we use.)
- **~4.5 s of lag.** Fixed: Icecast burst 16 KB → 0, Liquidsoap buffer 1.0 → 0.4 s, and the LARA
  `threshold` is now `buffer_seconds` × bitrate (1.5 s ≈ 36 KB @192 kbps) instead of a fixed
  64 KB (2.7 s). Expect ~2 s total.
- **The Spotify slider was a hidden second volume control** — it attenuated the stream while the
  LARA sat at its own level. Now `--volume-ctrl fixed` + the slider is forwarded as `audg`.

**Left**
1. Verify on HA that 0.2.1 actually lands: lag ~2 s without dropouts, the LARA returns to the
   radio list, and the Spotify slider moves the LARA's volume.
2. **Does this librespot build emit a volume event at all** with `--volume-ctrl fixed`? The hook
   matches `*volume*` (the name differs between versions: `volume_set` / `volume_changed`) and
   writes `/tmp/spotify_volume_default`. If that file never appears, the slider is simply inert —
   still safe, but the mapping does nothing and needs another route.
3. **Volume scale.** We sent `audg` 30, the LARA reported 50 back over the CLI, so its scale is
   not ours. Calibrate by ear.
4. **Multiple LARAs** at once; mount-switch latency.

Testing without deploying the add-on:
```
python tools/zone_test.py <this-ip> --port 8121 --proxy http://<some-icecast>/mount
```
`--proxy` serves the upstream stream from *this* host, which is what the LARA requires (see
`server_ip=0` above) and logs every audio fetch. Then type `on` / `off` / `vol 40` / `status`.

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
| **Full zone test (SlimProto + LMS CLI)** | `python tools/zone_test.py <our-ip>` — the real `slimproto.py` + `lmscli.py`, verbose; type `on`/`off`/`vol 60`/`status` |

## Windows dev-env notes (same laptop)

- **Python 3.12**: `%LOCALAPPDATA%\Programs\Python\Python312\python.exe` (pyyaml installed).
- **Windows Firewall** blocks inbound by default → to receive SlimProto/discovery you must allow the
  ports (run an **Administrator** PowerShell):
  ```
  New-NetFirewallRule -DisplayName "LR3 SlimProto 3483" -Direction Inbound -Protocol TCP -LocalPort 3483 -Action Allow -Profile Any
  New-NetFirewallRule -DisplayName "LR3 SlimProto UDP 3483" -Direction Inbound -Protocol UDP -LocalPort 3483 -Action Allow -Profile Any
  New-NetFirewallRule -DisplayName "LR3 LMS CLI 9595" -Direction Inbound -Protocol TCP -LocalPort 9595 -Action Allow -Profile Any
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
