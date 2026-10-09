# Changelog

## 0.4.9

- **A radio that stops reporting itself is now visible in the log.** One of the radios at a
  customer's site went quiet in a way nothing noticed: it stayed connected and kept playing,
  but stopped sending the status messages everything else depends on. The result was over a
  day with no buffer figures, no playing state, and the automatic recovery from dropouts
  silently switched off — because it waits for a status message that never came. The add-on
  now says so after two minutes, and also reports once what the radio *is* sending instead.

## 0.4.8

- Stopping or restarting the add-on no longer leaves three `Task was destroyed but it is
  pending!` errors in the log. They came from the checks 0.4.2 runs on a radio that drops out:
  every radio drops out when the add-on stops, so every shutdown logged errors about a
  perfectly clean stop.
- If the add-on cannot reach the streaming server's statistics, it now says so once instead of
  staying quiet — otherwise the backlog measurement added in 0.4.7 could simply never appear
  and nobody would know why.

## 0.4.7

- **The log now measures how far behind the radios are getting.** After a long listening
  session a radio can end up tens of seconds behind the app — noticeable when you switch
  rooms, and cleared by stopping and starting playback. Part of that lag builds up inside
  the streaming server where nothing could see it. Every five minutes the add-on now reports
  whether that backlog is growing, and by how many seconds of audio per hour. No behaviour
  change; this is the measurement needed before the lag can sensibly be fixed.

## 0.4.6

- **A short idle timeout cuts the ends off songs — the add-on now says so.** Switching a zone
  off tells the radio to throw away everything it has buffered and not yet played, and the
  radio runs several seconds behind the app (sometimes tens of seconds after a long session).
  If the timeout is shorter than that lag, the last seconds of whatever was playing are
  discarded. Anything under 45 seconds now produces a warning in the log, and the setting's
  description explains the trade-off.

## 0.4.5

- A group that lists **every** radio is now refused, with a note in the log saying why: that
  is exactly what "LARA All" already does, and a second device for it would only be another
  name for the same thing. Groups are for picking *some* of the rooms.

## 0.4.4

- **New: your own groups of rooms.** "LARA All" has always been all-or-nothing. You can now
  define extra Spotify devices for hand-picked sets — say one called "Inside" with just the
  bathroom and the living room. Name the radios the way they appear in the Spotify app;
  capitals and the "LARA " prefix do not matter. Choosing a single room still takes priority
  over any group it belongs to.
- **Switching rooms no longer stutters.** Handing a Spotify session from one room to another
  briefly leaves both looking active, and the add-on was acting on that — switching the radio
  across and straight back a second later. Harmless before 0.4.3; since then each bounce
  re-buffers the radio, which is why switching started to feel slow. A change now has to hold
  for three seconds before it counts. Starting and stopping are unaffected.

## 0.4.3

- **Moving music from one room to another, or into "LARA All", no longer leaves a radio
  silent.** Switching a radio that was already playing used to hand it the new stream without
  stopping the old one, and this firmware does not cope: it stopped fetching altogether and
  went quiet while everything still looked correct. The old stream is now ended first.
- The log shows the add-on version again. It has been printing a blank since 0.4.0.

**And the 48 kHz change in 0.4.0 is confirmed working.** On a three-radio site: thirteen
dropouts on the day before the update, and none at all in the four days since. The radios now
fill their buffers to the brim and hold them there for over an hour instead of slowly running
dry every twenty-six minutes.

## 0.4.2

- **When a radio drops out, the add-on now checks by itself whether the unit is still alive**
  and writes the answer in the log. Until now the only way to tell a radio that has lost just
  its audio-zone connection from one that has locked up completely was for somebody to walk up
  to it before pulling the breaker. The add-on now tries its web page and its control port a
  few seconds, half a minute and two minutes after the drop, and says which it is. It stops
  as soon as the radio comes back, and it is only a connection attempt — nothing is sent.

## 0.4.1

- The first "playing" line in the log now appears about twenty seconds after a zone starts,
  instead of after five minutes. That first line is the only place you can see how much audio
  the radio actually took before it began — which is what decides whether raising the buffer
  setting achieves anything, and it was missing exactly when it was needed.

## 0.4.0

**The music should stop cutting out.** Every radio we have measured, at two unrelated sites,
consumed audio a fraction faster than the add-on could deliver it — about 39 bytes a second.
That is tiny, but it is relentless: the radio's buffer drained steadily and ran dry roughly
every 26 minutes, the sound stopped, and sometimes it did not come back on its own. It never
lined up with a track change, which is why it looked random for so long.

- **The stream now goes out at 48 kHz instead of 44.1 kHz**, which is what removes the
  mismatch. If anything sounds wrong, Output sample rate can be put back to 44100.
- **Raising the buffer was never going to fix this** and the option now says so: a radio never
  holds more than about 62 KB no matter what it is told, so a bigger setting only makes you
  wait longer before the first sound.
- **"Starting volume" now works.** It used to be sent to the radio, which ignores it on this
  firmware, while the Spotify slider stayed at full — so setting 50 % changed nothing. It now
  sets where the Spotify slider starts, which is the only volume that does anything here.
- The log says which version is running again. 0.3.10 tried to and printed `v?`.
- A zone that has just been switched on no longer reports that it has "fetched nothing" for
  however long it had been idle beforehand.

If the music still stops after half an hour, the log now shows exactly why: look for the
`playing:` lines and whether `in_buf` falls steadily.

## 0.3.10

- **The log now says which version is running.** Until now nothing did, so answering "did the
  update actually land?" meant guessing from the wording of other messages. The first line of
  the log is now `LR3 AudioZone v0.3.10 — mode=slimproto …`, which also shows at a glance
  whether the radios are being controlled at all.

## 0.3.9

- **"Ovládání LARA = off" now says so when it matters.** In that mode the add-on streams
  Spotify but never switches a radio into its audio zone — which looks exactly like a fault:
  the phone hands playback over, the Spotify device behaves perfectly, and the radio just sits
  there, off or still playing whatever station it was on. It is meant as a temporary
  diagnostic setting, and left on by accident it is silent. It now warns loudly at start-up,
  and again, naming the zone, whenever Spotify is actually playing into the void.

## 0.3.8

A radio at a customer's site stopped playing on a Wednesday evening and was still dead 38 hours
later, with a black display, until its mains lead was pulled. The add-on log now times that
precisely, and the picture it gives changes what this release does.

- **A radio that has gone quiet is no longer pushed a fresh stream.** When a LARA runs out of
  audio it reports an underrun and stops, and since 0.3.4 the add-on has immediately sent it
  the stream again. That works — 52 times in the month covered by the log. The 53rd time, the
  underrun was the last thing the radio ever said, and a second later we pushed a new stream
  into a unit that was already gone. The add-on now waits for the radio to speak again before
  pushing, so a unit that has fallen silent is simply left alone. The cost is a few seconds
  more silence when the radio is fine; the gain is that nothing is sent to one that is not.
- **The log now says when a radio is "playing" but has stopped fetching audio.** This is the
  reported symptom where the music stops while the phone still shows Spotify streaming to the
  room — and it would, because Spotify talks to the add-on, not to the radio, and has no idea
  what the radio is doing. The counters that prove it were previously invisible.
- A short progress line per playing radio every five minutes: how far in, how much fetched,
  and how full its buffer is.

**Worth checking your buffer setting.** Underruns are the root of both symptoms, and they are
frequent: 54 of them in a month at that site, almost all on the one radio that gets daily use.
It was running a buffer of 2.6 s, just under the 2.7 s that is the only value ever verified on
real hardware. If a radio of yours cuts out, raise **"How long LARA buffers"** to 4-5 seconds.
Sound then starts a few seconds later after you press play, and stops dropping out.

## 0.3.7

- **Fixes a mistake in 0.3.6: idle CLI connections are no longer closed.** 0.3.6 closed any
  control connection that had said nothing for two minutes, believing the radio polls it every
  five seconds for ever. It does not — it polls while it is playing and goes quiet otherwise.
  At a customer's site that meant **280 connections closed and 283 reopened in under four
  hours**, with every radio taking a fresh network port every two minutes. That is a slower
  version of the very behaviour seen just before a radio froze solid, so this is worth
  updating for even though nothing visibly misbehaved.
- The problem 0.3.6 was aiming at — the radio opening a second control connection and never
  closing the first — is now handled properly: the old connection is closed when the
  replacement arrives, never merely because a connection is quiet.
- **New option: how much Spotify audio each zone caches on disk** (`audio_cache_mb`, default
  200 MB). It used to be a fixed 1 GB *per zone*, so a house with three radios could put 4 GB
  of streamed music on the storage of a Home Assistant Green — which is soldered in, and gets
  written to for every track played, for a cache that only helps if the same track comes round
  again soon. Set it to 0 to switch it off; playback is unaffected either way.

Good news from the same site, on 0.3.6: all three radios stayed connected for four hours with
no dropouts, the zone stopped switching itself off between tracks (five switch-ons against one
switch-off, where an earlier log had 37 against 80), and nothing was written to the radios'
configuration port at all.

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
