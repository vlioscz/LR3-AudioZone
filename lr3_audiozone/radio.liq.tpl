# LR3 AudioZone — zóna "%%ZONE_NAME%%"  ->  mount /%%MOUNT%%
# Do mountu teče POUZE Spotify. Když Spotify nehraje, teče ticho — mount tím nikdy nespadne
# (LARA si ho musí umět stáhnout v okamžiku, kdy jí pošleme strm-s). Žádné záložní rádio:
# o to, aby LARA při nečinnosti zhasla, se stará SlimProto controller (strm-q + aude off).

settings.log.stdout.set(true)
settings.log.level.set(3)
# Kontejner addonu běží jako root; Liquidsoap by se jinak z bezpečnosti ukončil.
settings.init.allow_root.set(true)

# --- Spotify Connect přes librespot ---
# librespot se přes avahi objeví na LAN jako Spotify zařízení "%%ZONE_NAME%%"
# a posílá raw S16 PCM na stdout. Píše RYCHLEJI než realtime, takže bez omezení
# se buffer plní až na 'max' a tam trvale stojí — to je zdroj latence i "dojezdu" při stopu.
# max=1.5 → krátký ocas; rezervu proti jitteru drží vnitřní buffer librespotu, ne tenhle FIFO.
# --onevent zapisuje play/stop stav do /tmp/spotify_state_<mount> (čte ho SlimProto controller).
spotify = input.external.rawaudio(
  id="spotify_%%MOUNT%%",
  restart=true, restart_on_error=true,
  buffer=1.0, max=1.5, log_overfull=false,
  'LR3_MOUNT=%%MOUNT%% librespot --name "%%ZONE_NAME%%" --device-type speaker --backend pipe --format S16 --bitrate %%SPOTIFY_BITRATE%% --initial-volume 100 --cache /data/librespot_%%MOUNT%% --cache-size-limit 1G --enable-volume-normalisation --onevent /etc/lr3/spotify_event.sh 2>>/tmp/librespot_%%MOUNT%%.log; sleep 3'
)

# --- Ticho, aby byl mount vždy krmený ---
# librespot při pauze PŘESTANE zapisovat (nevydává ticho), takže zdroj zmizí a naskočí tohle.
# track_sensitive=false → přepnutí nastane v okamžiku, kdy zdroj (ne)naskočí.
silent = blank(id="silence_%%MOUNT%%", duration=-1.)
main = fallback(id="main_%%MOUNT%%", track_sensitive=false, [spotify, silent])

# Jeden trvalý enkodér + výstup do Icecastu. `main` je infallible (ticho vždy),
# takže výstup zůstane připojený napořád.
output.icecast(
  %mp3(bitrate=%%BITRATE%%),
  id="out_%%MOUNT%%",
  host="localhost",
  port=%%PORT%%,
  password="%%SOURCE_PASSWORD%%",
  mount="/%%MOUNT%%",
  name="%%ZONE_NAME%%",
  description="LR3 AudioZone",
  genre="Various",
  fallible=false,
  main
)
