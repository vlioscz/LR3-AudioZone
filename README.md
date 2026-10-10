# LR3 AudioZone — Home Assistant Add-on

**English** | [Česky](README.cs.md)

[![Add repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fvlioscz%2FLR3-AudioZone)

**Spotify Connect → ELKO EP "LARA".** The add-on finds LARA radios on your network and
**offers each of them in Spotify as its own Connect device**, named after that radio — plus
"LARA All", which plays to all of them at once. You pick where the music goes on your phone,
and the add-on switches the radio into its **audio zone** (acting as a **Slim server**). When
you stop playing, the radio is released back to its own controls. No fallback radio, no presets.

> **Where this comes from.** LR3 AudioZone grew out of
> **[LR3-Stream](https://github.com/vlioscz/LR3-stream-addon)**, which was just the stable
> Icecast stream plus Spotify Connect, with no control over the radios. **LR3-Stream is no
> longer maintained** (last change August 2026) — all the work happens here now. If you are
> running it, this add-on replaces it: the same stream, and it drives the LARAs as well.

```
 "LARA Bathroom"   librespot ─► pacer ─► ffmpeg ─► Icecast /lara_f2231c ──► LARA Bathroom
 "LARA Living rm"  librespot ─► pacer ─► ffmpeg ─► Icecast /lara_aabbcc ──► LARA Living room
 "LARA All"        librespot ─► pacer ─► ffmpeg ─► Icecast /all ─────────► both at once

        SlimProto server (:3483) ── strm ──► radios  (tells them what to fetch)
        LMS CLI server  (:9595) ◄── state + buttons ── radios
```

## How it works

1. On start-up the add-on **scans the network and finds LARA radios** along with their names
   (TCP 61695 sweep).
2. For **each radio** it starts its own **librespot** → the radio shows up in Spotify as a
   separate Connect device named after it (e.g. "LARA Bathroom").
   With **two or more** radios there is also **"LARA All"**, which plays to all of them at
   once, and you can define **your own groups** of hand-picked rooms (`groups`).
   With a single radio the group device is not shown — it would just be a second name for the
   same speaker.
3. Each zone's audio is encoded to MP3 and goes into its own **Icecast** mount. When Spotify
   is not playing, the mount carries silence — so it never goes down and a LARA can start
   fetching it at any time. It is sent **at the pace the radios actually play**, not by the
   clock: a LARA runs a fraction of a percent off real time, and audio sent in exact real time
   used to pile up in front of it until the delay reached tens of seconds (`rate_match`).
4. The add-on is also a **Slim server** — two services:
   - **SlimProto** on TCP `:3483` — audio transport, volume, powering outputs on/off.
   - **LMS CLI** on TCP `:9595` — the text channel the LARA uses to ask what is playing and to
     send its own button presses back to us. This is also how we feed it the **track title and
     artist**, so the display shows the playing track, not the zone name.
5. When Spotify starts playing, the add-on sends `strm-s` to the affected radios → they **switch
   into the audio zone** and play. A radio's own device takes precedence over any group: start
   music on "LARA Bathroom" in the middle of a group session and the bathroom leaves the group
   while the others keep playing.
6. **Volume is the slider in the Spotify app.** It is the only control: on firmware 3.7.001 the
   radio ignores volume sent over the network, and its own buttons only mute and unmute while a
   zone is playing.
7. When Spotify stops and `idle_timeout` passes, the add-on sends `strm-q` and mutes the
   outputs. The radio keeps showing the audio zone until somebody touches it; switching it back
   to the station list over port 61695 is **opt-in** (`park_on_zone_off`, off by default — see
   the note below).

> **New radio on the network?** The set of Connect devices is fixed at start-up — after adding
> a radio, **restart the add-on**. Until then the add-on does control it (it follows "LARA All"),
> but it gets no Spotify device of its own.

## Prerequisite: point the LARA at HA as its slim server

Every LARA must have **"Audio zone function"** enabled in its configuration, with the slim
server IP set to your HA address and the **CLI port** matching the `cli_port` option
(default 9595). Set it either in the **ELKO Configurator**, or directly in the **LARA web UI**
(`http://<lara-ip>`, admin/password login) → the **"Audio zone function"** section.
The SlimProto port is 3483.

## Configuration

| Option | Default | Description |
|---|---|---|
| `port` | `8121` | Icecast stream port (this is where the LARA fetches audio from). |
| `source_password` | `changeme` | Icecast internal password. The LARA does not need it. |
| `bitrate` | `192` | Bitrate of the MP3 sent to the LARA (kbps). |
| `spotify_bitrate` | `320` | Spotify quality (96/160/320). Needs Premium. |
| `samplerate` | `48000` | **Leave this alone.** At 44100 every LARA measured consumes audio about 39 B/s faster than we can deliver it, so its buffer bleeds out and the music stops roughly every 26 minutes — whatever the buffer is set to. 48000 removes the mismatch; 44100 is kept only for comparison. |
| `rate_match` | `auto` | **Leave at `auto`** (= on). Sends each zone at the pace its radios really play, so the delay stays at about 5 s instead of growing by 12 s an hour until the radio is dropped. `off` = the previous engine (Liquidsoap, exact real time). |
| `spotify_remote_access` | `false` | Off: no Spotify login is stored, zones are visible to everyone on your network and to nobody outside it (turning it off also deletes a login stored earlier). On: the last account to select a zone stays logged in and sees it from anywhere — which is not ownership, anyone on the network can still take the zone over. |
| `audio_cache_mb` | `200` | Spotify audio cached on disk **per zone** (0 = none). It was a fixed 1 GB each until 0.3.7 — four zones meant up to 4 GB written to the HA Green's soldered storage. |
| `zone_name` | `Audio zóna` | Fallback name — used only when no radio is found. |
| `group_name` | `LARA All` | Name of the device playing to all radios (only with 2+ radios). |
| `groups` | `[]` | Your own extra devices for hand-picked sets of rooms, e.g. "Inside" = bathroom + living room. Name the radios the way they appear in Spotify; the "LARA " prefix and capitals do not matter. A group needs at least two radios that exist and **must not list all of them** — that is what "LARA All" already is. |
| `lara_name_prefix` | `true` | Prefix names with "LARA " ("LARA Kitchen" vs. "Kitchen"). |
| `scan_subnet` | empty | Subnet to sweep, e.g. `10.0.0`. Empty = the one HA lives in. |
| `zone_volume` | `90` | Where the Spotify slider starts for each zone. `0` = leave it at full. Since 0.4.0 this sets the Spotify volume, not the radio's — the radio ignores volume sent over the network. |
| `buffer_seconds` | `2.7` | How much audio the radio collects before the first sound. **Above about 2.6 it has no effect**: the radios start at about 60 KB however long they are told to wait (every session on two installs set to 4.0 did). It does not cure music that cuts out either. |
| `idle_timeout` | `60` | Seconds of Spotify silence before the radio is released. **Do not set this low** — see the warning below. Under 45 the add-on warns you in the log. |
| `control_mode` | `slimproto` | `slimproto` = control the LARA. `off` = discover and log only; for short diagnostic windows only, see below. |
| `park_on_zone_off` | `false` | Off: when the music stops, only the stream is stopped and muted; the radio keeps showing the audio zone until somebody touches it. On: also switch the source back to the station list over port 61695. Read the note below before turning it on. |
| `cli_port` | `9595` | LMS CLI port — must match "CLI port" in the LARA's configuration. |
| `cli_username` / `cli_password` | empty | Login the LARA sends on the CLI. Leave empty if it has none. |
| `lara_username` | `admin` | LARA user — only needed for `park_on_zone_off` (port 61695). |
| `lara_password` | `elkoep` | LARA password. |
| `lara_hosts` | `[]` | Manual LARA IPs for when the scan can't find them. |

> **Updating from 0.1.x?** The `fallback_enabled`, `fallback_url` and `fallback_delay` options
> are gone. If the add-on complains about unknown options after updating, open its
> **Configuration** and save it again (the Supervisor keeps previously saved options).
> `fallback_delay` was replaced by `idle_timeout`.

## Home Assistant sensors

Every radio gets a sensor, `sensor.lr3_lara_xxxxxx` (the last six characters of its MAC; the
exact names are listed in the add-on log at start-up). Its state is the **Spotify device that
radio is playing** — "LARA Terrace", "LARA All", one of your groups — or `off`. Use it in
automations: for instance, one LARA driving several speakers through relays can switch them
according to which Spotify device was picked. A step-by-step guide with a ready-made
automation (in Czech): [docs/rele-podle-zony.md](docs/rele-podle-zony.md).

## ⚠️ A short `idle_timeout` cuts the ends off songs

Ending a zone tells the radio to throw away everything it has received and not yet played, and
the radio is always several seconds behind the app — about five with `rate_match` on, and up
to half a minute with it off, once a long session has built up a backlog. If the timeout is
shorter than that lag, the last seconds of whatever was playing are discarded. One site ran 20 s and the
customer reported that *no song ever finished*. **60 is a good value**, and the only cost is the
zone lingering on the display a little longer after the music stops.

## ⚠️ Radios locking up (open issue)

At one three-radio site a LARA has locked up repeatedly, hard enough to need the mains pulled —
buttons dead, web page dead, invisible on the network. **The cause is not identified.** What is
known: it is one particular unit (the oldest hardware generation of the three), it happens
**while the add-on is sending it nothing but the 5-second heartbeat**, and it follows somebody
using the radio itself rather than anything we do during playback. Everything the add-on does
that a normal Slim server would not is now either off by default (`park_on_zone_off`) or
bounded, and since 0.4.2 the log reports whether a radio that drops off still answers on its
web page and its control port — which is what tells a frozen unit from one that merely dropped
its connection.

If it happens to you: **before** pulling the mains, try a short press of the RESET pin and see
whether `http://<radio-ip>` still loads — that answer is worth more than anything else. To take
the radios completely out of the loop, un-tick "Audio zone function" in each radio's own web UI.
Do **not** use `control_mode: off` for that: with the add-on's ports closed the radios retry
them constantly (measured at ~40 fresh connections an hour against ~0.35 in normal operation),
which is harder on them than running normally. **Set it back to `slimproto` afterwards** — while
it is `off` the radios never play, and the only sign of it is a warning in the log.

## Status

- ✅ **In daily use at several installations**, including two three-radio sites.
- ✅ **The whole loop works on real hardware** (fw 3.7.001): Spotify starts playing → the radio
  switches into the audio zone and plays; stop the music → after `idle_timeout` it is released.
- ✅ **The dropouts every ~26 minutes are fixed** (0.4.0, confirmed in the field): the output
  moved to 48 kHz. Thirteen dropouts the day before the change, none in the four days after,
  and the radios now hold a full buffer for over an hour instead of slowly running dry.
- ✅ **Moving music from one room to another** no longer leaves a radio silent (0.4.3) and no
  longer stutters (0.4.4).
- ✅ **The display shows the playing track** — title and artist go out over the LMS CLI (:9595).
- ✅ **The delay no longer grows during long sessions** (0.5.0, confirmed in the field). It
  used to grow by 12 s an hour until the radio was dropped after 2 h 16 min. Since the
  add-on started sending at the radios' own pace, a three-radio site has held a steady
  buffer and zero backlog for hours, including a 2 h 41 min session.
- 🔎 **Open:** the freeze above.
- Test tool that needs no add-on deployment:
  `python tools/zone_test.py <this-machine-ip> --proxy <mp3-stream-url>`
