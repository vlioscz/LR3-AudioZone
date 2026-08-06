# LR3 AudioZone — Home Assistant Add-on

**English** | [Česky](README.cs.md)

[![Add repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fvlioscz%2FLR3-AudioZone)

**Spotify Connect → ELKO EP "LARA".** The add-on finds LARA radios on your network and
**offers each of them in Spotify as its own Connect device**, named after that radio — plus
"LARA All", which plays to all of them at once. You pick where the music goes on your phone,
and the add-on switches the radio into its **audio zone** (acting as a **Slim server**). When
you stop playing, the radio returns to its station list. No fallback radio, no presets.

> The sister project **[LR3-Stream](https://github.com/vlioscz/LR3-stream-addon)** is just the
> stable stream + Spotify Connect (no radio control). **LR3-AudioZone** adds output to LARA via Slim.

```
 "LARA Bathroom"   librespot ─► Liquidsoap ─► Icecast /lara_f2231c ──► LARA Bathroom
 "LARA Living rm"  librespot ─► Liquidsoap ─► Icecast /lara_aabbcc ──► LARA Living room
 "LARA All"        librespot ─► Liquidsoap ─► Icecast /all ─────────► both at once

        SlimProto server (:3483) ── strm ──► radios  (tells them what to fetch)
        LMS CLI server  (:9595) ◄── state + buttons ── radios
```

## How it works

1. On start-up the add-on **scans the network and finds LARA radios** along with their names
   (TCP 61695 sweep).
2. For **each radio** it starts its own **librespot** → the radio shows up in Spotify as a
   separate Connect device named after it (e.g. "LARA Bathroom").
   With **two or more** radios there is also **"LARA All"**, which plays to all of them at once.
   With a single radio the group device is not shown — it would just be a second name for the
   same speaker.
3. Each zone's audio flows through **Liquidsoap** into its own **Icecast** mount. When Spotify
   is not playing, the mount carries silence — so it never goes down and a LARA can start
   fetching it at any time.
4. The add-on is also a **Slim server** — two services:
   - **SlimProto** on TCP `:3483` — audio transport, volume, powering outputs on/off.
   - **LMS CLI** on TCP `:9595` — the text channel the LARA uses to ask what is playing and to
     send its own button presses back to us. This is also how we feed it the **track title and
     artist**, so the display shows the playing track, not the zone name.
5. When Spotify starts playing, the add-on sends `strm-s` to the affected radios → they **switch
   into the audio zone** and play. A radio's own device takes precedence over the group: start
   music on "LARA Bathroom" in the middle of a group session and the bathroom leaves the group
   while the others keep playing.
6. **Volume is controlled by the slider in the Spotify app.** It is the only control — during
   audio-zone playback the LARA's own volume buttons only mute/unmute, so volume cannot be set
   on the unit itself.
7. When Spotify stops and `idle_timeout` passes, the add-on sends `strm-q`, mutes the outputs
   and, over port 61695, **returns the radio to its station list — stopped**, so the zone does
   not hang on the display and the radio is ready for whoever walks up to it.

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
| `spotify_bitrate` | `320` | Spotify quality (96/160/320). |
| `zone_name` | `Audio zóna` | Fallback name — used only when no radio is found. |
| `group_name` | `LARA All` | Name of the device playing to all radios (only with 2+ radios). |
| `lara_name_prefix` | `true` | Prefix names with "LARA " ("LARA Kitchen" vs. "Kitchen"). |
| `scan_subnet` | empty | Subnet to sweep, e.g. `10.0.0`. Empty = the one HA lives in. |
| `zone_volume` | `90` | Volume set on the radio when the zone turns on. `0` = leave the radio's volume alone. |
| `buffer_seconds` | `1.5` | How many seconds the LARA buffers before playing = the main source of latency. Lower = snappier, but risks dropouts. |
| `idle_timeout` | `8` | Seconds of Spotify inactivity before the LARA leaves the zone = how long the zone lingers on the display after the music stops. |
| `control_mode` | `slimproto` | `slimproto` = control the LARA. `off` = discover and log only (testing). |
| `cli_port` | `9595` | LMS CLI port — must match "CLI port" in the LARA's configuration. |
| `cli_username` / `cli_password` | empty | Login the LARA sends on the CLI (if it has one). |
| `lara_username` | `admin` | LARA user — required for the return to the station list (port 61695). |
| `lara_password` | `elkoep` | LARA password. |
| `lara_hosts` | `[]` | Manual LARA IPs for when the broadcast can't find them. |

> **Updating from 0.1.x?** The `fallback_enabled`, `fallback_url` and `fallback_delay` options
> are gone. If the add-on complains about unknown options after updating, open its
> **Configuration** and save it again (the Supervisor keeps previously saved options).
> `fallback_delay` was replaced by `idle_timeout`.

## Status

- ✅ **The whole loop verified on HA with a real LARA** (fw 3.7.001): Spotify starts playing →
  the LARA switches into the audio zone and plays (~2 s behind); stop the music → after
  `idle_timeout` the LARA returns to its station list.
- ✅ **The display shows the playing track** — title and artist go out over the LMS CLI (:9595).
- ✅ **Volume is the Spotify slider** — the one control. On this firmware the radio has no
  usable volume path of its own: its buttons only mute/unmute during zone playback.
- 🧪 **Not yet exercised:** several LARAs playing at once (the multi-radio code shipped
  in 0.3.0) and calibration of the `zone_volume` scale.
- Test tool that needs no add-on deployment:
  `python tools/zone_test.py <this-machine-ip> --proxy <mp3-stream-url>`
