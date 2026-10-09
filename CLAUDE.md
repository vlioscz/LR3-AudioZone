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
- Spotify idle for `idle_timeout` s → `zone_off()`: `strm-q` + `aude 0 0`, and — **only if
  `park_on_zone_off` is on, which it is not by default since 0.3.6** — `select_source(RADIO)`
  + `stop` over 61695 (`laradev.park_on_radio`). SlimProto alone only **mutes**, so the unit
  stays lit showing a dead audio zone; parking it on the station list is what a person walking
  up to the radio expects. That was the default until 0.3.6 and it is now opt-in, because it
  is the only thing we do that no Slim server does and it sits inside the sequence before two
  customer radios froze (see below). 61695 is also the **only** authenticated path we use
  (`lara_username`/`lara_password`); discovery, SlimProto and the CLI are unauthenticated.

## The freezes (2026-08, unresolved)

At a 3-radio site two LARAs locked up so hard they needed the mains pulled — **buttons dead,
web UI dead, invisible on every port**, one stuck on the station list and one in the audio
zone. Never happened in the year before the add-on, during which the **audio-zone function was
switched off**, i.e. the radio's SlimProto client had never run at all. The mechanism is **not
identified**. What the evidence does say, from a 1539-line log:

- **Not our socket handling.** One radio held exactly one CLI and one SlimProto socket for 8.5
  days and stayed healthy; every stale socket in the record was reaped.
- **Not the 0.3.4 flap fix or anything in it.** Freeze #1 predates 0.3.4; freeze #2 happened
  **under 0.3.5** with both fixes live. `recover_if_stalled` has never fired.
- **Not 61695 failing.** Zero failed transactions in 82 parks.
- **A runaway on the radio itself**: before freeze #1 that unit burned ~640 outbound source
  ports in two hours (49250 → 49893) against ~50 in the preceding six days. It ends with a
  device that completes a handshake and instantly drops it — the classic out-of-sockets
  endgame. Whatever starts that is upstream of what we can see.
- In both deaths the radio **closed its SlimProto socket** while leaving the **CLI socket
  ESTABLISHED**. One task went away and the box then wedged — not a clean whole-stack crash.

**Third freeze, 2026-09-23, timed exactly** (the 0.3.6 silence reaper is what caught it):
Obývák reported `STMu` at 19:43:57 UTC and **never sent another STAT**. One second later
`recover_if_stalled` pushed it a fresh `strm-s`; at 19:45:27 the 90 s reaper declared the
session dead. The unit stayed dead for **38 hours**, black display, until its mains lead was
pulled. Unproven as cause — 52 other underruns that month recovered through that same re-push —
but since 0.3.8 the re-push requires a **STAT after the underrun**, so a radio whose last word
was "I stopped" is never pushed again (`Player.stat_seq` vs `Controller._stall_seq`).

## ⚠️ The underruns are a constant source-clock deficit, ~1640 ppm (2026-09-30)

Measured from the `playing:` progress lines 0.3.8 added, on a third-party 3-radio install
(172.16.0.x, not one of our sites). The LARA's input buffer **drains linearly** the whole time
it plays:

```
12:18:17  306 s in,  6.8 MB fetched, in_buf=49696 B
12:23:17  619 s in, 13.7 MB fetched, in_buf=38104 B   -38.6 B/s
12:28:17  931 s in, 20.5 MB fetched, in_buf=25912 B   -40.6 B/s
12:33:17 1244 s in, 27.4 MB fetched, in_buf=14432 B   -38.3 B/s
12:38:17 1556 s in, 34.3 MB fetched, in_buf= 2344 B   -40.3 B/s
12:39:01  STMu -> mode=stop
```

**All three radios drain at the same ~39 B/s (1570–1850 ppm, mean ~1640).** Three independent
crystals would not agree to within a few percent of each other, so this is **our source running
slow**, not the players: at 192 kbps we owe 24000 B/s and deliver about 23961.

Consequences, and they matter:

- A 64 KB buffer empties in **~28 min**, which is exactly the observed underrun cadence and
  exactly site 5's "54 underruns in a month, not tied to track boundaries".
- **Raising `buffer_seconds` does not fix this — it only lengthens the cycle** (4 s ≈ 42 min).
  That was the advice given after the 2026-09-23 freeze and it is a palliative, not a cure.
- **Not Liquidsoap's clock** — that was the first theory and it is wrong. `src/clock.ml` sleeps
  to an *absolute* deadline (`t0 + frame_duration*ticks - time()`), so a late wake-up is paid
  back by the next one and error cannot accumulate. Two unrelated hosts also cannot agree on
  the same 39 B/s; one firmware can.
- **Reported fixed in the field on 2026-10-02** ("problém s bufferem se zdá být vyřešen") after
  0.4.1 went on the third-party 3-radio install. Not yet confirmed from a log: the proof is the
  `playing:` lines holding `in_buf` level across half an hour, and that log has not been read.
- **Fixed in 0.4.0 by moving the output to 48 kHz** (`samplerate`, default 48000). At 48 kHz an
  MP3 frame is exactly `144*bitrate/48000` bytes (576 at 192 kbps) with no padding bit, and
  48 kHz is what a standard 12.288 MHz audio crystal divides down to exactly, while 44.1 kHz
  needs 11.2896 MHz. Either mechanism predicts the same fix. **Confirm from the field before
  believing it**: the `playing:` lines show `in_buf` directly, so a flat `in_buf` over half an
  hour is the proof, and a still-falling one means the theory was wrong.
- ⚠️ `input.external.rawaudio` must keep `samplerate=44100` explicitly — librespot always emits
  44100 and that parameter defaults to the *frame* rate, so without it the PCM is read 8.8 %
  too fast.
- ✅ **48 kHz CONFIRMED on hardware (2026-10-04).** Third-party 3-radio install, same log,
  before and after:

  ```
  09-30 (44.1 kHz)  in_buf 49616 → 37648 → 25840 → 14128 → 2264   -39 B/s, STMu x13 that day
  10-03 (48 kHz)    in_buf 62400 → 85312 → 108224 → 130312 → ... → 126972 over 75 min
  ```

  The buffer now **fills** from the start threshold to the player's full 131072 B and holds
  there. **Underruns: 13 on the day before the change, 0 in the four days since.**
- ❌ **The "~62 KB ceiling" was wrong** and is retracted. ~62 KB is just the start threshold
  (`buffer_seconds` × bitrate); the radio grows past it to ~130 KB once the source can keep up.
  At 44.1 kHz it never could, so the number only ever went down from there and looked like a
  cap. `buffer_seconds` therefore sets the pre-roll (and the wait before the first sound) only;
  the radio manages its own buffer after that.

## Site 5 freeze statistics, 2026-08-13 → 2026-10-04 (one log, 7327 lines)

Counted by SlimProto session drops and by CLI source ports restarting near 49152, which only
happens when a unit reboots:

| radio | session drops | power cycles |
|---|---|---|
| **KP2 / koup. patro (00:0a:59:f2:2c:3c)** | **20** | **4** |
| Obývák (2c:6a:6f:10:3a:ce) | 4 | 2 |
| koupelna dole (2c:6a:6f:10:3a:c6) | **1** | **0** |

(Three further all-three-at-once reboots on 08-16 and 08-21 are site-wide power events, not
freezes.) KP2 is the outlier on every measure, and it is also the only unit of the older
hardware generation — OUI `00:0a:59` against `2c:6a:6f` for the other two.

**The 2026-10-03 freeze, exactly:**

```
06:01:41  LARA 00:0a:59:f2:2c:3c reports volume=50
06:01:42  CLI command from 00:0a:59:f2:2c:3c: stop
06:01:42  CLI stop ... while its zone is off — recorded, nothing to do
06:02:01  LARA 00:0a:59:f2:2c:3c disconnected
06:30:36  LARA connected (CLI source port 49153 → it was power-cycled)
```

The add-on **sent it nothing** in that window — no push, no park, no recovery; it had not played
since 09-30 and only the 5 s `strm-t` heartbeat was going out. The same
`reports volume` → `stop` → session drops shape appears on 09-30, 10-01 and twice on 10-02, so
it is the radio being *used* (someone changing source or volume on the unit), after which its
audio-zone session ends — normally harmless, occasionally fatal.

So the freeze is **not** caused by anything we send during playback, which rules out the whole
family of theories built around pushes, parks and recovery. 0.4.2 adds the measurement that was
missing: a liveness probe on :80 and :61695 after every session drop, so the log itself says
whether the unit was still alive.

## ⚠️ `idle_timeout` must exceed the pipeline lag, or songs get cut off

`zone_off` ends with `strm-q`, and this firmware answers `STMf` — it **flushes** everything it
has buffered and not yet played. The listener is always behind the app:

| stage | lag |
|---|---|
| Liquidsoap `input.external` | 0.4 s |
| Icecast backlog | grows; ~25 s measured after an hour |
| the LARA's own `in_buf` (pinned at ~130 KB since 48 kHz) | 5.4 s @ 192 kbps |

So if the timeout is shorter than that total, the tail of whatever was playing is thrown away.
Site 5 ran `idle_timeout` 20 s and the customer reported *"no song ever finishes, the sound
always stops before the end"* — the lag was ~30 s, so the last ~10 s of each track went in the
flush. It only bites when something interrupts playback mid-track (there, librespot losing its
Spotify context), because that is when the timeout fires at all.

0.4.6 warns below `IDLE_TIMEOUT_FLOOR` (45 s). **Note the interaction with 48 kHz**: before it,
the buffer was always near-empty and the lag was ~2 s, so a short timeout was harmless. Fixing
the underruns is what made the radio sit on a full buffer — and made this visible.

## Switching a playing radio to another mount (fixed in 0.4.3)

`push_stream` used to send `strm-s` with the new URL straight at a player that was already
streaming. This firmware does not replace the stream: it stops fetching entirely. Caught in the
field on 2026-10-02 — Obývák was 4079 s / 106.8 MB into `/lara_10318e`, got `-> play /all`, and
`bytes_received` froze on the spot while `in_buf` went 126528 → 0 with no sound.

**Every stall warning in that log — 18 of them — lands 30–31 s after a push**, i.e. the radio
fetched nothing for the whole detection window following a mount change. It is not specific to
the group: individual→individual switches do it too. 0.4.3 sends `strm-q` first when
`current_mount` is set and differs, both commands back to back on the same connection.

A re-push of the **same** mount (the underrun recovery) deliberately does *not* send `strm-q` —
that path should not get more aggressive. If the zombie state ever reappears, note that a plain
re-push never cleared it while a push to a *different* mount did within two seconds, so adding
the terminate there is the next thing to try.

## Group → individual transitions (open, 2026-10-02)

Hypothesis from the field: leaving **LARA All** for a radio's own zone is where a radio gets
stuck fetching nothing. Two of the three observations offered for it are **not** evidence, and
the third is:

- `main_all → silence_all` is a **Liquidsoap fallback event on the /all mount**, not anything
  the controller does. It fires when the "LARA All" librespot goes inactive. Its ordering
  against our push is simply which librespot reported first; nothing overlaps in the controller.
- A radio still reporting `/all` with a full buffer *after* the group went silent is **by
  design**: nothing has told it to stop, the mount now carries silence, and `tick()` only calls
  `zone_off` after `idle_timeout`. That is the idle branch, not a leaked stream.
- **Real:** `push_stream` sends `aude 1 1` + `strm-s` and **never `strm-q`**, even when
  `p.current_mount` is already set (slimproto.py). A mount change therefore overwrites the
  stream in place, where a real LMS terminates/flushes first. Already raised in the September
  adversarial review as P1 #14 and deliberately deferred ("adds a packet to the very teardown
  path we suspect; ship only after the park is gone and the backoff is in"). The park is gone.

⚠️ Do **not** implement the suggested "stop everything, wait for confirmation, then push" — that
is more packets and more waiting on the path that is already suspect. The minimal change is a
single `strm-q` immediately before `strm-s` when `current_mount` is set, which is what LMS does.
**Wait for a log that contains the failure.** The 0.4.1 first `playing:` line lands ~20 s after a
switch, so a radio that did not pick up shows `in_buf=0` with frozen bytes right there.

**The zombie state.** After an underrun a radio can come back claiming to play while fetching
nothing at all — `bytes_rx` frozen, `in_buf=0`, `elapsed` still climbing, no sound. Two radios
sat like that for **4875 s** in this log. A re-push **to the same mount does not clear it**; the
one thing that did was a push to a *different* mount (the group zone), after which all three
fetched again within two seconds. This is what a user reports as "it played all morning and now
it will not pick up Spotify". Without 0.3.8's stall warning it is completely invisible.

⚠️ **Underruns are the thing to fix, not the recovery.** 54 in one month at that site, 52 of
them on the single radio in daily use, and they are *not* tied to track boundaries or cache
eviction (33 of 54 are >60 s from either). They are what the customer experiences as "the music
cuts out and comes back, or stops altogether". That site ran `buffer_seconds` 2.6 (62 KB) —
under the 64 KB the hardware probe found steady. Raise the buffer before touching anything else.

⚠️ **`control_mode: off` is NOT a safe parking state**, despite having been recommended as one.
With the servers down the radios keep dialling :3483 and :9595 and get connection refused, and
this firmware has no reconnect backoff — each refused attempt burns an outbound source port.
Measured at site 5 over ~47 h in that mode: Obývák went 49154 → 51290, **~40 ports/hour**
against a normal-operation baseline of ~0.35/hour, i.e. two orders of magnitude more of exactly
the behaviour that preceded a freeze. To take radios out of the loop for real, un-tick **"Audio
zone function"** on each device; `off` is for a short diagnostic window only.

⚠️ Do **not** "fix" this by adding retries, shortening cooldowns or making recovery more
aggressive: every extra attempt asks a device that is visibly running out of sockets for
another one. Do not blind-write anything else over 61695.

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
  `<mac> mixer volume 95` → then `<mac> playlist tracks ?` **while it is playing**, roughly every
  5 s, plus `artist ?` / `current_title ?`. It **never sends `listen 1`** — this fw polls rather
  than subscribing, so `LmsCliServer.notify()` is dead weight here (kept for other firmware).
  - ⚠️ **It goes completely quiet when it is not playing** — "every 5 s forever" was wrong and
    0.3.6 believed it, closing any connection silent for 120 s. Every radio then reopened one
    every two minutes (280 closes / 283 opens in 4 h at a customer's site), burning source ports
    in the same shape as the runaway that preceded a freeze. **Never put an idle timeout on a
    CLI connection.** Close a connection only when the same host opens a replacement.
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
