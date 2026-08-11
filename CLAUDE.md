# LR3 AudioZone — project context

Hand-off / working context for this repo. (Claude Code loads this automatically.)
Human-facing usage docs are in `README.md`; this file is the technical state + decisions.

## What this is

A **Home Assistant add-on** that turns **Spotify Connect into audio on ELKO EP "LARA" radios**.
Spotify goes in (librespot → Liquidsoap → Icecast MP3 mount `/default`); the add-on is also a
minimal **Slim server** — **SlimProto** on TCP 3483 (audio transport) plus an **LMS CLI** on TCP
9595 (the LARA's control/display channel) — that discovers LARAs and, when Spotify is playing,
pushes them (`strm`) to fetch+play the mount, activating the LARA "audio zone". When Spotify
goes idle the add-on stops the stream and powers the LARA back down.

**There is no fallback radio.** The mount carries Spotify or silence; the LARA is in the zone
only while Spotify actually plays. (Removed in 0.2.0 — the fallback-radio/preset approach was
the LR3-Stream-era workaround.)

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
- ✅ **Audio proven on the real LARA** (2026-07-27): 60 s of continuous playback, zero underruns,
  and a clean stop. Getting there needed `server_ip=0` in `strm-s` (see the table below) — that
  single field is why the zone used to switch on but stay silent.
- ✅ **The LARA really does use the LMS CLI on :9595** — it logs in, polls, and reports its own
  volume there. Serving it is not optional if you want its display/buttons to behave.
- 🧪 **v0.2.0** — fallback radio removed; added the **LMS CLI server** (`lmscli.py`, :9595),
  power on/off (`aude`), STAT parsing (mode/elapsed) and the idle→off state machine.
  What has **not** run on hardware yet is the controller's Spotify-driven state machine
  (both of its ends are verified; only the add-on-level loop is untested).

## Repo layout

```
lr3_audiozone/
  config.yaml        add-on manifest + options (port, zone_name, idle_timeout, cli_*, lara_*)
  build.yaml         arm64/amd64 Debian base images
  Dockerfile         apt: icecast2 liquidsoap ffmpeg jq dbus avahi-daemon python3
                     + librespot from the raspotify .deb; COPY lr3ctl -> /opt/lr3ctl
  run.sh             PID 1: dbus+avahi, Icecast, the controller; writes the librespot
                     --onevent hook. Liquidsoap is started by the controller, per zone.
  icecast.xml.tpl    Icecast config template
  radio.liq.tpl      Liquidsoap: librespot (Spotify) -> silence. Nothing else.
  translations/      config UI labels (cs, en)
  lr3ctl/            the controller (Python, stdlib only)
    controller.py    zones (one per radio + group), pipeline supervision, the on/off machine
    slimproto.py     SlimProto server :3483 — strm/aude/audg, STAT parsing
    lmscli.py        LMS CLI server :9595 — the LARA's control/display channel
    discovery.py     UDP broadcast + the TCP /24 sweep that actually finds them
    elkoproto.py     ELKO 61695 protocol (obfuscation, builders, parsers)
    laradev.py       one LARA over 61695 — `park_on_radio()` on zone-off
```

## Zones — one Spotify device per radio

Since 0.3.0 the add-on is **multi-radio**. At start-up `controller.py` sweeps the LAN
(`discovery.find_radios`), reads each LARA's own name, and brings up **one pipeline per radio**:
its own librespot (→ its own Spotify Connect device), Liquidsoap and Icecast mount.

| radios found | Spotify devices |
|---|---|
| 0 | one, named `zone_name` — drives whatever dials in later (discovery may be blocked) |
| 1 | just that radio (a group device would be a second name for the same speaker) |
| ≥2 | one per radio **plus** `group_name` ("LARA All") feeding all of them |

Mounts: `lara_<last 6 MAC hex>` per radio, `all` for the group, `default` for the no-radio
fallback. Device names come from the radio's config, optionally prefixed "LARA "
(`lara_name_prefix`; never doubled if the name already starts with LARA). Duplicate names get a
MAC tail. `Controller.render_liq()` fills `radio.liq.tpl` per zone — **`run.sh` no longer starts
Liquidsoap**, the controller owns those processes and restarts any that die (`supervise_zones`).

**The device set is fixed at start-up** (deliberate — the alternative churns processes). A radio
that only turns up later on SlimProto is added to the inventory and follows the group zone, but
gets no device of its own until the add-on restarts; the log says so.

## How routing works

`controller.py` `tick()`s every second. The per-zone Spotify-active flag comes from
`librespot --onevent` writing `/tmp/spotify_state_<mount>`. For each radio, `zone_for()` walks
the zones — **specific zones first, group last** — and takes the first active one that covers it,
so playing to a radio's own device pulls it out of a group session without touching the others.
A LARA that dials in on :3483 is **added to the inventory even if the scan never saw it**
(`on_slim_connect`), so the add-on works on networks that eat broadcasts.

- Spotify active → `slim.push_stream(mac, "default")`: `aude 1 1` (power on) + `strm-s` to
  `http://<our_ip>:<port>/default` + `audg`.
- Spotify idle for `idle_timeout` s → `zone_off()`: `strm-q` + `aude 0 0`, then
  `select_source(RADIO)` + `stop` over 61695 (`laradev.park_on_radio`). SlimProto alone only
  **mutes** — the unit stays lit showing a dead audio zone, confirmed on hardware. Parking it
  on the station list is what a person walking up to the radio expects, and Spotify is always
  started from the phone, so nothing is lost by leaving the zone. This is the **only**
  authenticated path we use (61695 needs `lara_username`/`lara_password`); everything else —
  discovery, SlimProto, the CLI — is unauthenticated. There is deliberately no option to pick
  a weaker behaviour: the alternatives all left the zone hanging on the display.

## Spotify availability (`spotify_remote_access`, 0.3.5)

Spotify Connect reaches a device two ways, and they are independent: **zeroconf/mDNS** on the
LAN, and **Spotify's own backend** once the device is logged in. librespot logs in by itself
whenever a cached credentials blob exists — so storing the login silently publishes the zone
to that one account *worldwide*, which is how an installer ended up seeing a customer's radio
from mobile data. Facts established from the librespot 0.8.0 source, against earlier folklore:

- Cached credentials do **not** suppress zeroconf. `no_discovery_reason` depends only on the
  compiled-in backend and `--disable-discovery`; login state never enters it.
- A logged-in librespot keeps serving the zeroconf pairing endpoint and `handle_add_user`
  builds credentials straight from the request, with no check against the active or cached
  user — **any** account on the LAN can take a zone over, and the takeover rewrites the
  stored blob. So "it remembers my login" is **not** ownership, it is the opposite.
- A blob can only arrive via a zeroconf `addUser` from the LAN. So `Authenticated as` in a
  librespot log is proof that mDNS worked at that site for at least one phone.
- `--disable-credential-cache` sets the credential path to `None` in 0.8.0, so librespot
  neither writes **nor reads** one: the flag is what actually releases an account. Deleting
  the file (`purge_stored_logins`, `prepare_credentials`) is belt-and-braces — it keeps an
  auth blob out of `/data` and every HA backup, and it is the only protection left if that
  flag is ever unavailable. Delete by **glob, not per zone**: a radio unplugged while the
  switch is flipped is not in `self.zones` and would keep its login for ever.
- `probe_cred_cache_flag()` fails **towards passing the flag**. Guessing "unsupported" would
  silently store logins while the UI promises it does not; guessing "supported" wrongly makes
  librespot exit at once, which is loud and now visible in the log.
- Spotify's "Sign out everywhere" explicitly excludes speaker-class devices. It does **not**
  release a librespot blob; only deleting the file does.

Login and audio cache are deliberately separate dirs (`--system-cache` vs `--cache`) so
releasing a login does not discard up to 1 GB of audio per zone.

**Volume lives in Spotify** — librespot's software volume, i.e. **no** `--volume-ctrl` flag.
One control, because two stages mean the sound can be turned down in two places and nobody can
tell which. This took three attempts; the history is here so it is not re-litigated:
- `--volume-ctrl fixed` (0.2.1–0.3.2) keeps the stream at full scale, but it also **strips the
  Connect device of its volume capability**, so the slider disappears from the Spotify app
  entirely. It is not "report volume but don't apply it" — no stock librespot mixer does that,
  which is why "the Spotify slider drives the LARA's hardware volume" is **not buildable**.
- Driving the LARA's hardware volume instead does not work either: on fw 3.7.001 its volume
  buttons only **mute/unmute** while an audio zone plays, and `audg` has no audible effect on
  the output. Answering `<mac> mixer volume <n>` with `audg` (0.3.2) changed nothing. So the
  CLI value is recorded as state only, and logged at INFO — if we ever learn what those buttons
  really send, that log is the evidence.
- `zone_volume` still sends one `audg` when the zone switches on (`0` = never touch it). On this
  firmware that appears to be a no-op; it is kept because it is the only hardware-level hook we
  have and it costs one packet.

⚠️ The event hook must NOT write volume events into `spotify_state_*`: everything outside
`ACTIVE_EVENTS` reads as "not playing", so a volume nudge mid-song would switch the zone off.

⚠️ **The idle countdown must be cleared on every playing tick** (`tick()`), not only when a
radio is pushed to a new mount — `route()` returns early once the radio is already on that
mount, so it never reaches its own `idle_since.pop()`. Getting this wrong (0.3.0–0.3.3) froze
the timestamp at the session's first blip, after which one idle tick — the gap between two
tracks — switched the zone off at once. Symptom: music stops mid-album and resumes seconds
later; in the log `zone OFF` immediately followed by `zone ON` with the *same* track.

`zone_off()` is **idempotent** (`_parked`): our `strm-q` makes the LARA echo `stop` back on the
CLI, which is not a button press. Without the guard every switch-off parked the radio twice
and a late echo could kill a zone that had already restarted.

**Latency** is a stack of buffers; keep them in mind before adding another:
Liquidsoap `input.external` (0.4 s) → mp3 encode → Icecast burst (**0**, `burst-on-connect 0`) →
the LARA's own `threshold` (`buffer_seconds` × bitrate, default 1.5 s). That last one dominates —
it was a fixed 64 KB (2.7 s at 192 kbps) and total lag ran ~4.5 s.
- The LARA's own buttons arrive over the LMS CLI (`play`/`stop`/`power`/`button`) and are routed
  back into the same two actions via `Controller.on_cli_command`.

**What the LARA displays.** It polls `<mac> current_title ?` and `<mac> artist ?` every few
seconds while playing, so those two answers *are* its two display lines. The librespot event
hook writes the track from `track_changed` into `/tmp/spotify_track_<mount>` (line 1 title,
line 2 artists joined with ", "), `Controller.update_now_playing()` copies it onto the Player,
and `lmscli._title()`/`_artist()` serve it — falling back to the zone name when no track has
been reported, which is better than a blank display. Env var names differ across librespot
versions, so the hook tries `NAME`/`TRACK_NAME`/`ITEM_NAME` and `ARTISTS`/`ARTIST`/`ALBUM_ARTISTS`.

`control_mode`: `slimproto` (default) or `off` (discover + log only; safe for testing).
Preset control (path A over 61695) still exists in `laradev.py` but is no longer wired up.

## LARA protocol (reverse-engineered; verified on a real device)

Implemented in **`lr3_audiozone/lr3ctl/elkoproto.py`** (self-tested against captured packets).

- **Obfuscation**: whole packet XORed with a fixed 1024-byte mask (embedded base64 in elkoproto.py),
  keyed by a random 0–699 int; magic header `FF FA FA FF`. `admin`/`elkoep` defaults.
- **Discovery**: the probe is documented as a **UDP broadcast** to `255.255.255.255:61695`
  (reply → DeviceID==3 = LARA; gives ip/name(win-1250)/mac/fw). ⚠️ **fw 3.7.001 never answers
  it** — not broadcast, not directed broadcast, not unicast, no variant byte helps. The identical
  probe sent over **TCP 61695** answers instantly and `parse_discovery_reply` takes it unchanged.
  That TCP probe is the **only** source of the device's user-assigned name (e.g. "LARA Koupelna"),
  which the add-on needs to name Spotify devices — hence `discovery.find_radios()` sweeps the
  local /24 on TCP. Key radios by **MAC** (stable across DHCP).
- **Control = TCP 61695** (connect-per-command): select_source (RADIO=1/AUX=3/DLNA=4),
  select_station(index), play/stop/volume, read status/stations. ⚠️ config-read leaks plaintext
  passwords → never log raw packets. ⚠️ never blind-write presets (a write Saves the whole list).
- **SlimProto = TCP 3483** (the Slim server): player HELO → server pushes `strm` (arbitrary URL +
  control). Byte layouts in `slimproto.py`, verified vs squeezelite/aioslimproto AND a real LARA.

### strm-s parameters — DO NOT change without re-probing (fw 3.7.001)

Found by probing 8 variants against the real LARA (2026-07-27). Everything else left it stuck in
`STMc` with `bytes_received=0` — the source switched to "Audio zóna" but no audio was ever fetched,
which is why earlier sessions saw the zone light up yet stay silent.

| field | value | why |
|---|---|---|
| `server_ip` | **0** | **The fix.** A LARA ignores an explicit address and only fetches when told "use the control connection's IP". Probe B (`autostart=1, thr=20, ip=ours`) failed, C (identical but `ip=0`) fetched instantly. |
| `autostart` | `'1'` | This fw does not take the "direct streaming" variants `'2'`/`'3'`. |
| `threshold` | `64` (KB) | The player reports a 131072 B input buffer, so the old `200` was unreachable. `20` underran (`STMu`) within seconds; 64 KB (~2.7 s @192 kbps) holds steady. |

Verified: 60 s continuous play, `bytes_rx` 1.46 MB, `in_buf` steady ~62 KB, zero underruns; `strm-q`
+ `aude 0 0` closes the audio connection (`STMf`) and the LARA stays off.

### Real-device findings (fw 3.7.001) — already applied in code

- **HELO caps offset varies**: caps ("CSModel=…,mp3,…") sit at ~byte 34, not 24. `_on_helo` now
  finds the first long printable run (`re.search(rb"[ -~]{8,}", data[8:])`) instead of a fixed offset.
- **Status/stations `d[10]`**: this fw returns `d[10]==1` where the reference lib expects `0`;
  `parse_status_reply`/`parse_stations_reply` no longer match on `d[10]` (payload offsets unchanged).
- **Minimal listener drops the player after ~17 s** — the full handshake (`vers`/`setd`/`aude`/`audg`)
  **plus the `strm-t` heartbeat** in `slimproto.py` is required to hold the connection.
- **STAT frames are 51 bytes, not 53** — this fw omits the trailing `error_code`. `_on_stat` tries
  the long layout, then the short one. (`elapsed_seconds` is field 11, `elapsed_ms` field 13.)
- **The LARA does open the LMS CLI connection on :9595** — confirmed, open question #4 answered.
  Observed session, verbatim: `login admin elkoep` → `<mac> artist ?` → `<mac> stop` →
  `<mac> mixer volume 95` → then `<mac> playlist tracks ?` **every 5 s** forever. While playing it
  also polls `artist ?` / `current_title ?`. It **never sends `listen 1`** — this fw polls rather
  than subscribing, so `LmsCliServer.notify()` is dead weight here (kept for other firmware).
  - That `stop` right after login is **state sync, not a button press**. Acting on it made the
    controller flap (push → "stop" → power off → next tick pushes again), hence `HANDSHAKE_GRACE`.
  - `mixer volume <n>` is the LARA reporting its own knob. It is recorded as state; echoing an
    `audg` back would fight the knob.
  - It opens a **new** CLI connection on every reconnect without closing the old one.

### Pointing a LARA at us (the required device-side config)

Enable **"Audio zone function"** and set the slim-server IP = HA. Two ways:
- **ELKO Configurator** (Windows), or
- the LARA **web UI** (`http://<lara-ip>`, HTTP **Digest** auth, realm "LARA"): SPA "LARA
  configurator" (index.html/index.js), section **"Audio zone function"** = checkbox
  `controll_bit_az` (config `audio_zone_enabled`) + IP fields `slim_ip_1..4` (config `audio_zone_ip`).
  Saved via its own POST — set it there, don't blind-write config over 61695.
- There is also a **CLI port 9595 + LMS username/password** (the LMS CLI). As of 0.2.0 we
  **serve it** (`lmscli.py`): line-based, space-separated, per-token URL-encoded; the server
  echoes the request with the answer appended (a trailing `?` is replaced by the value);
  `listen 1` turns the connection into a subscriber for pushed events. Implemented: `login`,
  `version`, `players`, `player`, `serverstatus`, `<mac> status/mode/power/mixer volume/time/
  title/playlist …`, plus `play`/`stop`/`pause`/`button` mapped back onto the zone actions.
  Unknown verbs are echoed unchanged and logged once — a real device teaches us the rest.

## Phase — on-device validation

**Done (2026-07-27, against the real LARA):** CLI connection confirmed; `strm-s` parameters found
and playback verified for 60 s; `strm-q` + `aude 0 0` stops the fetch and keeps it off; `audg`
volume applied; the CLI handshake `stop` no longer causes a flap. `strm` alone is enough — no
61695 SOURCE-select was needed.

**Done on HA (0.2.1 / 0.2.2)** — the whole loop runs: Spotify plays → LARA plays (latency now
~2 s after the buffer work); disconnect → noticed in ~5 s → after `idle_timeout` the LARA goes
back to the station list, verified working. `aude 0 0` was confirmed to only **mute**, which is
why the 61695 source switch is unconditional now.

**Done on a customer's 3-radio install (0.3.4)** — multi-radio runs, but its log exposed the
idle-countdown flap above (76 `zone OFF` against 37 `zone ON` in one log) and two
`STMu` underruns after which the LARA dropped the SlimProto connection and only came back
minutes later. The flap is fixed; the underruns are not yet explained.

**Left**
1. `zone_volume` scale: we sent `audg` 30, the LARA reported 50 back over the CLI. Calibrate.
2. The `STMu` underruns above — whether the flap caused them or CPU/network contention does.
3. Whether every phone in a household actually sees a zone over mDNS. Unconfirmed at the
   1-radio install (its owner was away); at the 3-radio install it is proven indirectly —
   two of its three zones had a stored login, and a login can only arrive through a zeroconf
   `addUser` from the LAN, so discovery demonstrably worked there for at least one phone.

## Build / dev conventions

- HA keeps saved options across updates → new config.yaml defaults don't auto-apply.
- Line endings: `.gitattributes` forces **LF** (Linux container). `*.png` binary.
- Commit only when the user asks; end commit messages with `Co-Authored-By: Claude Opus 4.8`.
  main is the release branch the add-on installs from; push there directly.
- Bump `version:` in `config.yaml` on each shippable change (currently 0.1.0, scheme 0.x in dev),
  and add the matching entry to `lr3_audiozone/CHANGELOG.md` (HA shows it in the add-on's
  Changelog tab and when offering the update). English, one `## <version>` section per release.
