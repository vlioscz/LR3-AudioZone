# Changelog

## 0.3.6

Two radios at a customer's site locked up hard enough to need the mains pulled — dead buttons,
dead web page, invisible on the network. The cause is **not identified**. This release removes
or bounds everything the add-on does that a normal Slim server would not, because that is where
an untested firmware path is most likely to be.

- **The switch back to the station list over port 61695 is now OFF by default**
  (`park_on_zone_off`). It is the one thing here that no Logitech server does — a write on the
  vendor's configuration port, into a unit that is mid-teardown of its audio zone — and it sits
  inside the sequence before both freezes. With it off, a zone that stops just leaves the audio
  zone on the display until somebody touches the radio. That is untidy; a frozen radio is a
  trip to the wall unit.
- **A radio that is not being driven is never switched off.** A LARA reports `stop` on the CLI
  as state sync after it connects, and that was taken for a button press: measured on the
  customer's log, **48 of 82 switch-offs fired on a radio the add-on had never pushed** — and
  one of those was the last thing ever sent to a unit that then froze.
- **A CLI command naming an unknown radio no longer executes against a different one.** The
  lookup fell back to "the first radio in the list", so one radio's stop, play or volume could
  land on another. It needs two or more radios to bite.
- **Timestamps on every add-on log line.** Reconstructing the freezes meant inferring the time
  of each line from the Liquidsoap output around it; the best answer available was a two-hour
  bracket.
- **Dead sessions are detected and closed.** A radio answers our 5 s heartbeat, so silence for
  90 s (SlimProto) or 120 s (CLI) means the socket is abandoned however healthy TCP thinks it
  is. One such socket stayed open for eight hours against a radio that had frozen, with nothing
  in the log to say so. This is diagnostics, not a cure — nothing was accumulating.
- A radio that reconnects now has its previous SlimProto session closed, as real LMS does.
- **Default buffer raised to 2.7 s (64 KB at 192 kbps)** — the only value ever validated on
  hardware. 1.5 s shipped since 0.2.1 and was never probed. **Default `idle_timeout` raised to
  60 s**, so every zone transition — the code path under suspicion — happens far less often.
  These two are changed defaults, so **existing installations keep their old values**: set them
  by hand in Configuration.

If your radios have never locked up, `park_on_zone_off` can be turned back on to keep the old
tidy-up behaviour.

## 0.3.5

- **New option: "Spotify access from outside the network", off by default.** Until now the
  add-on stored the Spotify login of whoever first selected a zone, which quietly registered
  that zone with Spotify's servers: from then on **that one account saw the radio from
  anywhere in the world**, while the rest of the household only ever saw what local discovery
  gave them. With the option off, no login is stored, and the zones are offered to everyone
  on your own network and to nobody outside it.
- **Turning it off also releases an account that is already stored** — every saved login is
  deleted when the add-on starts, including those of radios that happen to be switched off at
  the time. This is the way to hand a system over to its owner after setting it up with your
  own phone.
- **Updating changes behaviour**: the option is new, so existing installs get the new default
  and their stored login is deleted at the first start. Every zone then has to be selected in
  the Spotify app once more. To keep the old behaviour, turn the option **on** in
  Configuration before or right after updating.
- One trade-off worth knowing before you leave it off: if librespot restarts — it does, when
  the connection to Spotify drops — the zone comes back unclaimed and somebody has to pick it
  in the app again. With the option on, the stored login let it rejoin by itself.
- The Spotify login and the cached audio now live in separate directories, so releasing a
  login no longer throws away up to 1 GB of cached audio per zone.
- Worth knowing either way: having the login stored is **not** ownership. Anyone on the
  network can take a zone over, and doing so replaces the stored login with theirs.

## 0.3.4

- **Music no longer stops mid-album and start again a few seconds later.** The idle countdown
  was started by the first non-playing moment of a session and then never restarted, because
  the code that cleared it only ran when a radio was pushed to a *new* mount. From
  `idle_timeout` seconds after that first moment onwards, a single one-second gap — the pause
  between two tracks — switched the zone off instantly, and the next tick switched it back on.
  On a customer's install this fired dozens of times a day. The countdown now restarts on
  every tick the zone is playing, so only a real pause of `idle_timeout` seconds ends it.
- **Switching a zone off no longer happens twice.** Our own `strm-q` makes the LARA report
  `stop` back over the LMS CLI, which was taken for a button press: the radio was parked on
  its station list twice, and a late echo could kill a zone that had just started again.
  Stops coming from a radio we ourselves stopped within the last few seconds are now ignored;
  a stop genuinely pressed on the radio still works.
- **An underrun no longer leaves a radio silent for minutes.** When the LARA stops playing but
  keeps its control connection, nothing noticed — the add-on still believed it was playing and
  never pushed the stream again, so the radio stayed quiet until it happened to reconnect.
  It is now detected and the stream is pushed again (at most once every 15 s).
- **librespot's own log now appears in the add-on log.** It was written to a file inside the
  container, where nothing outside a shell could see it — and it is where "Published zeroconf
  service", "Authenticated as …" and connection failures to Spotify are reported, i.e. the
  answers to why a zone is missing from the Spotify app or keeps dropping out.

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
