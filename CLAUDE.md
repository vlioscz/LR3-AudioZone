# LR3 AudioZone — project context

Hand-off / working context for this repo. (Claude Code loads this automatically.)
Human-facing usage docs are in `README.md`; this file is the technical state + decisions.

## What this is

A **Home Assistant add-on** that turns **Spotify Connect into audio on ELKO EP "LARA" radios**.
Spotify goes in (librespot → Liquidsoap → Icecast MP3 mount `/default`); the add-on is also a
minimal **SlimProto (Squeezebox) server** on TCP 3483 that discovers LARAs and, when Spotify is
playing, pushes them (`strm`) to fetch+play the mount — activating the LARA "audio zone".

Split off from **LR3-Stream** (https://github.com/vlioscz/LR3-stream-addon), which kept just the
stable stream + Spotify Connect. This repo owns everything about **driving LARA via the Slim server**.

- Repo: https://github.com/vlioscz/LR3-AudioZone (public). Add-on folder: `lr3_audiozone/`.
- Target HW: **HA Green / arm64** (also amd64). Default port **8121**, SlimProto **3483**.
- Owner communicates in **Czech**; keep replies in Czech.

## Current status

- ✅ **Validated on a real LARA** (fw **3.7.001**, MAC 00:0A:59:F2:23:1C, CSModel=squeezeslave):
  the SlimProto **HELO gate PASSED** — LARA connects to :3483, advertises **`mp3`** (+wma), and a
  pushed `strm-s` switched it to the "Audio zóna" source. So the existing Icecast **MP3** stack
  works directly (no FLAC/PCM mount needed).
- 🧪 **v0.1.0** — scaffolded from LR3-Stream's Phase-2 controller. The end-to-end auto flow
  (Spotify-active → discover → push → LARA plays, and back) needs on-device polish.

## Repo layout

```
lr3_audiozone/
  config.yaml        add-on manifest + options (port, zone_name, control_mode, lara_*, fallback_*)
  build.yaml         arm64/amd64 Debian base images
  Dockerfile         apt: icecast2 liquidsoap ffmpeg jq dbus avahi-daemon python3
                     + librespot from the raspotify .deb; COPY lr3ctl -> /opt/lr3ctl
  run.sh             PID 1: dbus+avahi, Icecast, one Liquidsoap (Spotify -> /default),
                     the SlimProto controller; writes the librespot --onevent hook
  icecast.xml.tpl    Icecast config template
  radio.liq.tpl      Liquidsoap: librespot (Spotify) -> fallback radio -> silence, --onevent hook
  translations/      config UI labels (cs, en)
  lr3ctl/            the SlimProto controller (Python, stdlib only)
```

## How routing works

`controller.py` runs an asyncio loop: every 60 s it discovers LARAs (UDP broadcast); every 1 s it
`tick()`s. The per-mount Spotify-active flag comes from `librespot --onevent` writing
`/tmp/spotify_state_default` ("playing"/"paused"/…). Mount `default` → **all** discovered radios.
When `/default` is Spotify-active → `slim.push_stream(mac, "default")` (strm-s to
`http://<our_ip>:<port>/default`). Not active → if `fallback_enabled` the LARA keeps playing the
mount (which is now the fallback radio), else `slim.stop(mac)`.

`control_mode`: `slimproto` (default) or `off` (discover + log only; safe for testing). Preset
control (path A over 61695) still exists in `laradev.py` but is not the focus here.

## LARA protocol (reverse-engineered; verified on a real device)

Implemented in **`lr3_audiozone/lr3ctl/elkoproto.py`** (self-tested against captured packets).

- **Obfuscation**: whole packet XORed with a fixed 1024-byte mask (embedded base64 in elkoproto.py),
  keyed by a random 0–699 int; magic header `FF FA FA FF`. `admin`/`elkoep` defaults.
- **Discovery = UDP broadcast** to `255.255.255.255:61695`; reply → DeviceID==3 = LARA; gives
  ip/name(win-1250)/mac/fw. Key radios by **MAC** (stable across DHCP).
- **Control = TCP 61695** (connect-per-command): select_source (RADIO=1/AUX=3/DLNA=4),
  select_station(index), play/stop/volume, read status/stations. ⚠️ config-read leaks plaintext
  passwords → never log raw packets. ⚠️ never blind-write presets (a write Saves the whole list).
- **SlimProto = TCP 3483** (the Slim server): player HELO → server pushes `strm` (arbitrary URL +
  control). Byte layouts in `slimproto.py`, verified vs squeezelite/aioslimproto AND a real LARA.

### Real-device findings (fw 3.7.001) — already applied in code

- **HELO caps offset varies**: caps ("CSModel=…,mp3,…") sit at ~byte 34, not 24. `_on_helo` now
  finds the first long printable run (`re.search(rb"[ -~]{8,}", data[8:])`) instead of a fixed offset.
- **Status/stations `d[10]`**: this fw returns `d[10]==1` where the reference lib expects `0`;
  `parse_status_reply`/`parse_stations_reply` no longer match on `d[10]` (payload offsets unchanged).
- **Minimal listener drops the player after ~17 s** — the full handshake (`vers`/`setd`/`aude`/`audg`)
  **plus the `strm-t` heartbeat** in `slimproto.py` is required to hold the connection.

### Pointing a LARA at us (the required device-side config)

Enable **"Audio zone function"** and set the slim-server IP = HA. Two ways:
- **ELKO Configurator** (Windows), or
- the LARA **web UI** (`http://<lara-ip>`, HTTP **Digest** auth, realm "LARA"): SPA "LARA
  configurator" (index.html/index.js), section **"Audio zone function"** = checkbox
  `controll_bit_az` (config `audio_zone_enabled`) + IP fields `slim_ip_1..4` (config `audio_zone_ip`).
  Saved via its own POST — set it there, don't blind-write config over 61695.
- There is also a **CLI port 9595 + LMS username/password** (the LMS CLI). Open question: whether
  the LARA needs the CLI for full zone control/sync, or SlimProto `strm` alone suffices (basic
  play worked over 3483 without us serving 9595).

## Phase — on-device validation (what's left)

1. Confirm the full auto flow on the real LARA: Spotify play on the "Audio zóna" device →
   controller discovers + pushes → LARA audibly plays `/default`; pause → fallback/stop; resume.
2. Volume (`audg`) and mount-switch latency; multiple LARAs at once.
3. Decide if the LMS CLI (9595) is needed. If so, add a minimal CLI responder.
4. Does `strm` alone activate the speaker, or is a 61695 SOURCE-select also needed on some fw?

## Build / dev conventions

- HA keeps saved options across updates → new config.yaml defaults don't auto-apply.
- Line endings: `.gitattributes` forces **LF** (Linux container). `*.png` binary.
- Commit only when the user asks; end commit messages with `Co-Authored-By: Claude Opus 4.8`.
  main is the release branch the add-on installs from; push there directly.
- Bump `version:` in `config.yaml` on each shippable change (currently 0.1.0, scheme 0.x in dev).
