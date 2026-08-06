# Changelog

## 0.3.3

- **Volume is back in the Spotify app** — librespot handles it in software again and the
  slider is the single control. `--volume-ctrl fixed` is gone: it strips the Connect device
  of its volume capability entirely (that's why the slider had disappeared), and the radio
  has nothing to hand volume to — on fw 3.7.001 its buttons only mute/unmute during zone
  playback and `audg` has no audible effect.
- `mixer volume` / `mixer muting` from the radio are recorded as state and logged at INFO,
  as evidence for anyone who wants to give those buttons a real effect one day.

## 0.3.2

- Attempt to make the LARA's volume buttons drive the volume (answering their
  `mixer volume` with `audg`). On fw 3.7.001 this turned out to change nothing audible —
  superseded by 0.3.3.

## 0.3.1

- **The display shows the playing track** (title + artist) instead of the zone name.
  The track comes from the librespot event hook and is served over the LMS CLI, which is
  literally the LARA's two display lines.

## 0.3.0

- **One Spotify Connect device per radio**, named after the radio's own configured name;
  with two or more radios also a group device (**"LARA All"**) that plays to all of them.
  A radio's own device takes precedence over the group.
- Discovery: fw 3.7.001 never answers the UDP probe, so radios are found by a TCP sweep
  of the /24 on port 61695 — also the only source of their names.
- The controller now owns the per-zone Liquidsoap processes (starts, restarts, stops them).
- New options: `group_name`, `lara_name_prefix`, `scan_subnet`. `zone_name` is now only the
  fallback used when no radio is found.

## 0.2.2

- One way to leave the zone: `strm-q` + mute + return to the station list, always.
  The `lara_off_action` option is gone — every other value left a dead zone on the display.
- `idle_timeout` default lowered to 8 s. A track change does not count as a pause.

## 0.2.1

- Latency cut from ~4.5 s to ~2 s: the LARA's buffer is now derived from the new
  `buffer_seconds` option, Icecast burst-on-connect is off, Liquidsoap's input buffer
  is smaller.
- On zone-off the LARA is parked on its station list over port 61695 (this is the one
  path that needs `lara_username` / `lara_password`), because `aude 0 0` alone only mutes.

## 0.2.0

- **The fallback radio is gone.** The add-on drives the LARA directly: Spotify plays →
  zone on; idle for `idle_timeout` → zone off. Options `fallback_enabled`, `fallback_url`,
  `fallback_delay` removed — re-save the add-on configuration if the update complains.
- **LMS CLI server** on :9595 — the LARA really does log in there and poll for state;
  serving it is required for its display and buttons.
- `strm-s` parameters verified on a real LARA (fw 3.7.001): `server_ip=0` is what makes
  the radio actually fetch the stream; 60 s of continuous playback, zero underruns.
- New options: `idle_timeout`, `zone_volume`, `cli_port`, `cli_username`, `cli_password`.

## 0.1.0

- Initial scaffold: Spotify Connect (librespot → Liquidsoap → Icecast) + a minimal Slim
  server (SlimProto :3483) that pushes the stream to LARA radios.
