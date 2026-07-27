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
# Držíme ho krátký (0.4/0.8 s); rezervu proti jitteru drží vnitřní buffer librespotu
# a práh v LAŘE (viz buffer_seconds), ne tenhle FIFO.
#
# Hlasitost patří JEN rádiu — vědomé rozhodnutí. --volume-ctrl fixed znamená, že stream jde
# vždy v plné úrovni a posuvník v aplikaci Spotify se vůbec nezobrazí (ten přepínač Connect
# zařízení schopnost hlasitosti odebere; není to „hlas hlas, ale neaplikuj ho"). Regulace tak
# zůstává výhradně na LAŘE: její tlačítka nejedou lokálně, ale posílají `mixer volume` na náš
# LMS CLI a čekají na `audg` — viz lmscli.py. Bez té odpovědi jsou tlačítka mrtvá.
# Dva nezávislé stupně (posuvník na stream + tlačítka na hardware) by znamenaly, že se dá zvuk
# ztlumit na dvou místech a nikdo nepozná kde — proto je tu jen jeden.
# --onevent zapisuje stav do /tmp/spotify_state_<mount> a skladbu do /tmp/spotify_track_<mount>.
spotify = input.external.rawaudio(
  id="spotify_%%MOUNT%%",
  restart=true, restart_on_error=true,
  buffer=0.4, max=0.8, log_overfull=false,
  'LR3_MOUNT=%%MOUNT%% librespot --name "%%ZONE_NAME%%" --device-type speaker --backend pipe --format S16 --bitrate %%SPOTIFY_BITRATE%% --volume-ctrl fixed --initial-volume 100 --cache /data/librespot_%%MOUNT%% --cache-size-limit 1G --enable-volume-normalisation --onevent /etc/lr3/spotify_event.sh 2>>/tmp/librespot_%%MOUNT%%.log; sleep 3'
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
