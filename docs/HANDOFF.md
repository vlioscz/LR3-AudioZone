# LR3-AudioZone — hand-off

Everything a fresh session (or a fresh machine) needs. Pair with `CLAUDE.md` for the
architecture and `tools/README.md` for the scripts. State as of **v0.3.3, 2026-08-04**.

This is a *current-state* document, not a changelog — where a decision was reversed, only the
final answer and the reason are recorded. `git log` has the blow-by-blow.

---

## 1. What it does, and what is proven

Spotify Connect → audio on ELKO EP **LARA** radios. The add-on scans the LAN, offers **one
Spotify Connect device per radio** (named after the radio) plus **"LARA All"** when there are two
or more, and drives the radios by acting as a **Slim server**. When Spotify stops, each radio
returns to its own station list.

**Working on real hardware** (single LARA, fw 3.7.001):

| | |
|---|---|
| Discovery + naming | ✅ TCP sweep finds it, name "LARA Koupelna" read from the device |
| Spotify → LARA audio | ✅ 60 s continuous, 1.46 MB fetched, zero underruns |
| Latency | ✅ ~2 s after the buffer work (was ~4.5 s) |
| Stop → back to station list | ✅ noticed in ~5 s, parked after `idle_timeout` |
| Track title/artist on the display | ✅ |
| LMS CLI on :9595 | ✅ the LARA really uses it |
| Volume | ⚠️ Spotify slider only — the radio has no usable volume control (see §4) |
| **Multi-radio** | ❓ **never run against two physical radios.** Logic covered by offline tests only |

---

## 2. The environment this was built against

| thing | value |
|---|---|
| LARA | `10.0.0.98`, MAC `00:0a:59:f2:23:1c`, name "LARA Koupelna", fw **3.7.001**, hw 1 |
| Home Assistant | `10.0.0.99` (HA Green, arm64) — runs the add-on |
| Dev laptop | `10.0.0.80` (Wi-Fi) / `10.0.0.25` (Ethernet) |
| Icecast | TCP **8121** (`port`) |
| SlimProto | TCP **3483** (fixed) |
| LMS CLI | TCP **9595** (`cli_port`) |
| ELKO control + discovery | TCP/UDP **61695** (fixed), auth `admin` / `elkoep` |

Repo: <https://github.com/vlioscz/LR3-AudioZone> (public), add-on folder `lr3_audiozone/`.

---

## 3. Protocol findings — the expensive ones

These were all found the hard way against the real device. Changing any of them means
re-probing, not reasoning.

### Discovery: UDP is dead on this firmware, TCP works

fw 3.7.001 **never answers UDP discovery** on 61695. Verified with broadcast, directed
broadcast *and* unicast, and every variant byte 0–4. Nothing comes back — this is the device,
not a firewall (an earlier note blaming Windows Firewall was wrong).

The **same probe sent over TCP** answers instantly with the full record, and
`parse_discovery_reply` eats it unchanged:

```json
{"ip": "10.0.0.98", "name": "LARA Koupelna", "mac": "00:0a:59:f2:23:1c", "fw": 37001, "hw": 1}
```

So `discovery.find_radios()` = UDP broadcast (kept — other firmware may answer) + explicit
`lara_hosts` + a threaded **TCP sweep of the /24**. The sweep is the only thing that works here,
and the only source of the device name the Spotify devices are named after.

⚠️ When writing probe helpers: `socket.timeout` subclasses `OSError`, so a bare `except OSError`
around the read loop **throws away the reply that already arrived**. That bug made the sweep
return nothing while a hand-written one-shot probe worked.

### strm-s: `server_ip=0` is the whole ballgame

With an explicit address the LARA switches its source to "Audio zóna" and then sits in `STMc`
with `bytes_received=0` — zone lit, total silence. That is what made earlier sessions think the
push had failed. A probe of 8 variants isolated it: `autostart=1, thr=20, ip=ours` → nothing;
identical but `ip=0` → fetched within a second.

| field | value | why |
|---|---|---|
| `server_ip` | **0** | "use the control connection's IP". An explicit address is ignored. Fine for us — Icecast and SlimProto run on the same host. |
| `autostart` | `'1'` | this fw does not take the direct-streaming variants `'2'`/`'3'` |
| `threshold` | `buffer_seconds` × bitrate | the player reports a **131072 B** input buffer, so the old fixed 200 KB was unreachable; 20 KB underran within seconds |

### STAT frames are 51 bytes, not 53

This fw omits the trailing `error_code`. `_on_stat` tries the long layout then the short one.
`elapsed_seconds` is field 11, `elapsed_ms` field 13 — getting that index wrong silently reports
the output-buffer fullness as the elapsed time.

### The LMS CLI on :9595 is a real dependency

The LARA opens it and uses it. Observed session, verbatim:

```
login admin elkoep
<mac> artist ?
<mac> stop
<mac> mixer volume 95
<mac> playlist tracks ?      ← then every 5 s, forever
```

- It **never sends `listen 1`** — it polls rather than subscribing, so `LmsCliServer.notify()`
  is dead weight on this firmware (kept for others).
- The `stop` right after `login` is **state sync, not a button press**. Acting on it made the
  controller flap (push → "stop" → power off → next tick pushes again). Hence
  `lmscli.HANDSHAKE_GRACE` = 5 s.
- It opens a **new** CLI connection on every reconnect without closing the old one.
- While playing it also polls `current_title ?` and `artist ?` — **those two answers are its two
  display lines.** That is how the track title gets on the display.

### Leaving the zone: SlimProto alone only mutes

`strm-q` + `aude 0 0` silences the unit but leaves it lit showing a dead audio zone. So
`zone_off()` follows up over 61695 with `select_source(RADIO)` + `stop`, which lands it on the
station list, prepared but not playing. **This is the only authenticated path in the add-on** —
discovery, SlimProto and the CLI are all unauthenticated; this one needs
`lara_username`/`lara_password`.

### Other device quirks already handled in code

- **HELO caps offset varies** — caps sit at ~byte 34, not 24. `_on_helo` finds the first long
  printable run instead of assuming an offset.
- **`d[10]` in status/stations replies** is `1` on this fw where the reference lib expects `0`;
  the parsers no longer match on it.
- **A minimal listener drops the player after ~17 s.** The full handshake
  (`vers`/`setd`/`aude`/`audg`) **plus** the `strm-t` heartbeat is required to hold the connection.
- ⚠️ A **config read** over 61695 returns plaintext passwords — never log raw packets. Never
  blind-write presets either: a write Saves the whole list.

---

## 4. Decisions that should not be re-litigated

**Volume lives in Spotify.** Three attempts; this is the end state.
- `--volume-ctrl fixed` keeps the stream at full scale but **strips the Connect device of its
  volume capability**, so the slider vanishes from the app entirely. It is *not* "report volume
  but don't apply it" — no stock librespot mixer does that.
- The LARA's own volume buttons only **mute/unmute** while an audio zone plays, and `audg` has
  no audible effect on this firmware's output. Answering `mixer volume` with `audg` changed
  nothing. So there is no usable control on the radio to hand volume to.
- ⇒ librespot's software volume (no flag), slider visible, single control. CLI volume values are
  recorded as state and logged at INFO in case someone later decodes what those buttons send.

**No fallback radio.** The mount carries Spotify or silence. The silence exists only so the
Icecast mount never dies — the LARA must be able to fetch it the instant `strm-s` arrives.

**One way to leave the zone.** `lara_off_action` existed briefly with four values; every
alternative left the zone hanging on the display, so the option was removed.

**The device set is fixed at start-up.** A radio that appears later is driven (it follows the
group) but gets no Connect device until the add-on restarts. The alternative churns processes.

**`idle_timeout` is the "zone still on the display" delay** (default 8 s), not a leftover
fallback timer — there are none. A track change is not a pause.

---

## 5. Moving to another machine

Everything needed is in git; the working folder holds nothing else of value.

```bash
git clone https://github.com/vlioscz/LR3-AudioZone.git
cd LR3-AudioZone
```

**Prerequisites for the dev tools** (the add-on itself needs none of this — it builds in Docker):
- **Python 3.12+**. On the old laptop: `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`.
  Only the stdlib is needed for `lr3ctl/` and the tests; `pyyaml` is handy for config checks.
- **Git** and, for release work, the **`gh`** CLI authenticated to the `vlioscz` account.

**Windows Firewall** — the LARA connects *to* you, so inbound must be allowed. Run an
**Administrator** PowerShell:

```powershell
New-NetFirewallRule -DisplayName "LR3 SlimProto 3483" -Direction Inbound -Protocol TCP -LocalPort 3483 -Action Allow -Profile Any
New-NetFirewallRule -DisplayName "LR3 SlimProto UDP 3483" -Direction Inbound -Protocol UDP -LocalPort 3483 -Action Allow -Profile Any
New-NetFirewallRule -DisplayName "LR3 LMS CLI 9595" -Direction Inbound -Protocol TCP -LocalPort 9595 -Action Allow -Profile Any
```

Remove later with `Remove-NetFirewallRule -DisplayName "LR3 SlimProto 3483"` (etc.).
None of this applies on the HA itself — the add-on runs with `host_network: true`.

**Antivirus** occasionally EPERM-blocks rapid PowerShell socket spawns. Prefer the Python tools.

**Line endings**: `.gitattributes` forces LF (the container is Linux). Do not let an editor
convert `run.sh` or the templates to CRLF.

**Sanity check on the new machine** — with a LARA on the LAN:

```bash
python lr3_audiozone/lr3ctl/discovery.py     # should print the radio with its name
python tools/tests/test_controller.py        # offline, no device needed
python tools/tests/test_lmscli.py            # offline, no device needed
```

---

## 6. Device-side prerequisite (per LARA, set once)

Each LARA needs **"Audio zone function"** enabled, **slim-server IP = the HA address**, and
**CLI port = 9595**. Two ways:

- **ELKO Configurator** (Windows app), or
- the LARA **web UI**: `http://<lara-ip>`, HTTP **Digest** auth, realm "LARA", default
  `admin`/`elkoep`. It is an SPA ("LARA configurator"); the section is **"Audio zone function"** —
  checkbox `controll_bit_az` (config `audio_zone_enabled`) + IP fields `slim_ip_1..4`
  (config `audio_zone_ip`), plus the CLI port and credentials. Set it there and **Save**; its own
  POST serialises the whole config correctly. Do **not** write config over 61695.
- `python tools/web_explore.py <lara-ip>` inspects the web UI read-only.

A LARA pointed at the wrong host is the single most likely reason "nothing happens" — it was the
cause once already, after the add-on moved from the laptop to the HA.

---

## 7. Tools

Run from the repo root. See `tools/README.md` for the full table.

| Step | Command |
|---|---|
| Find LARAs + names (the real path) | `python lr3_audiozone/lr3ctl/discovery.py` |
| Read-only control smoke | `tools/control_smoke.ps1 -Ip <lara-ip>` |
| Verify an MP3 stream is live | `python tools/check_stream.py <url>` |
| **Full zone test** (SlimProto + LMS CLI) | `python tools/zone_test.py <this-ip> --port 8121 --proxy http://<icecast>/mount` |
| Offline regression tests | `python tools/tests/test_controller.py`, `python tools/tests/test_lmscli.py` |

`zone_test.py --proxy` serves an upstream stream from *this* host — which is what the LARA
requires (see `server_ip=0`) — and logs every audio fetch, so you can tell "it fetched" from
"it went quiet" without guessing. Interactive: `on` / `off` / `vol 40` / `status` / `quit`.

Point the LARA at the test machine first, and remember to point it back at the HA afterwards.

---

## 8. Raw captures (reference)

**HELO** payload text: `CSModel=squeezeslave,ModelName=LARA,Firmware=3.7.001,wma,mp3,HasDigitalOut=0`
(dev_id 12, caps at ~byte 34).

**TCP test-packet reply** (61695, unauthenticated), decoded:
`ff fa fa ff 0e 1e 10 40 01 00 03 00 90 89 01 …` → `d[8..10]=1,0,3` identifies an ELKO device;
`fw = d[11]<<16 | d[12]<<8 | d[13] = 37001`, `hw = d[14] = 1`.
⚠️ In a PowerShell port, cast to `[int]` before `-shl` — `[byte] -shl 8` truncates to 0.

**Status reply** (61695), decoded head: `… 00 c1 01 01 00 00 55 00 01` → `d[7..10] = 0,193,1,1`;
parsed source=0, station=0, volume=0x55, playing=1.

**Healthy playback STAT** (`STMt`): `out_buf=4990/5000 in_buf=61896/131072 bytes_rx=262162
elapsed=8.5` — input buffer parked at the threshold is what "it is streaming fine" looks like.

---

## 9. Open items

1. **Multi-radio on real hardware.** Never tested with two LARAs. Watch: CPU with N+1 MP3
   encoders on the HA Green, whether two radios stay in sync on the group mount, and the latency
   when a radio switches between its own zone and the group.
2. **Volume scale.** We sent `audg` 30 and the LARA reported 50 back over the CLI, so its scale
   is not ours. Moot while `audg` has no effect, but it is the loose end if that changes.
3. **What the volume buttons actually send.** Now logged at INFO (`mixer volume`, `mixer muting`,
   unhandled `mixer` verbs). If someone wants those buttons to work, the log is the evidence.
4. **librespot metadata variable names** are unverified for this build — the hook tries
   `NAME`/`TRACK_NAME`/`ITEM_NAME` and `ARTISTS`/`ARTIST`/`ALBUM_ARTISTS`. Titles do appear on the
   display, so at least one pair matches; if a future librespot changes them, that is where to look.
