# tools/ — dev & on-device test scripts

Standalone helpers used to reverse-engineer and validate the LARA (they are **not** part of the
add-on image). They load the ELKO XOR mask / SlimProto code from `../lr3_audiozone/lr3ctl/` via a
path relative to the script, so they run from wherever the repo lives. See `../docs/HANDOFF.md` for
the full state + on-device checklist.

Prereqs on Windows: PowerShell 5.1 (built in) and Python 3.x for the `.py` tools. To receive
SlimProto/discovery on Windows you must allow the inbound ports (Admin PowerShell) — see HANDOFF.md.

| Tool | What it does |
|---|---|
| `scan_tcp.ps1 -Subnet 10.0.0.` | **Firewall-proof discovery**: TCP-scans 61695 across a /24, confirms each host with the ELKO test packet (fw/hw/mac). Use this first. |
| `discover.ps1` | UDP-broadcast discovery (mirrors `lr3ctl/discovery.py`). On Windows the reply is often firewall-dropped → falls back to "use scan_tcp". |
| `control_smoke.ps1 -Ip <ip>` | Read-only path-A control: test packet + status (source/volume/playing) + 40 presets. Never writes. |
| `slim_listen.ps1` | Minimal SlimProto listener on :3483 — logs the LARA's **HELO** and advertised **codecs** (the B2 gate). Point a LARA at this host first. |
| `check_stream.py <url>` | Confirms an MP3 stream URL is live (before pushing it to a LARA). |
| `play_test.py <our-ip> [url]` | Runs the **real** `slimproto.py` server + pushes `strm-s` so the LARA plays the URL. The actual play test. |
| `web_explore.py <ip> [user] [pass]` | Digest-auth crawl of the LARA web UI to locate the "Audio zone function" / slim-server fields (`audio_zone_ip`, `controll_bit_az`, CLI 9595). Read-only. |
| `_elko.ps1` | Shared ELKO protocol helpers dot-sourced by the PowerShell tools (not run directly). |

Typical on-device sequence:
```
powershell -File scan_tcp.ps1 -Subnet 10.0.0.        # find the LARA
powershell -File control_smoke.ps1 -Ip <lara-ip>     # confirm fw + read state
python web_explore.py <lara-ip>                       # find the slim config (then set it in a browser)
powershell -File slim_listen.ps1                      # (after pointing LARA at us) confirm HELO + mp3
python check_stream.py <mp3-url>
python play_test.py <our-ip> <mp3-url>                # real strm-s play test
```
