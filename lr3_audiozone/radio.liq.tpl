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
# Hlasitost řeší librespot softwarově (výchozí chování, tj. BEZ --volume-ctrl fixed), takže
# posuvník v aplikaci Spotify funguje a je to jediný ovladač hlasitosti.
# Historie, ať se to nezkouší znovu: --volume-ctrl fixed drží stream v plné úrovni, ale zároveň
# Connect zařízení odebere schopnost hlasitosti — posuvník v appce tím úplně zmizí. Šlo se na to
# proto, aby hlasitost patřila rádiu; jenže na LAŘE fw 3.7.001 tlačítka hlasitosti při přehrávání
# audio zóny fungují jen jako mute/unmute a `audg` se na výstupu neprojeví. Regulace na rádiu
# tedy reálně neexistuje a jediné funkční místo je tady.
# --onevent zapisuje stav do /tmp/spotify_state_<mount> a skladbu do /tmp/spotify_track_<mount>.
#
# %%LIBRESPOT_CACHE_ARGS%% skládá controller podle volby spotify_remote_access. Přihlášení a
# audio cache jsou schválně ve dvou adresářích (--system-cache vs --cache): uvolnění účtu tak
# neznamená zahodit až 1 GB audia na zónu. Při vypnutém vzdáleném přístupu se přidá
# --disable-credential-cache (v 0.8.0 nastaví cestu k přihlášení na None, takže se ani nečte,
# ani nezapisuje) a controller navíc uložené credentials.json smaže — aby auth blob cizího
# účtu nezůstal v /data a v zálohách HA.
spotify = input.external.rawaudio(
  id="spotify_%%MOUNT%%",
  restart=true, restart_on_error=true,
  buffer=0.4, max=0.8, log_overfull=false,
  'LR3_MOUNT=%%MOUNT%% librespot --name "%%ZONE_NAME%%" --device-type speaker --backend pipe --format S16 --bitrate %%SPOTIFY_BITRATE%% --initial-volume 100 %%LIBRESPOT_CACHE_ARGS%% --enable-volume-normalisation --onevent /etc/lr3/spotify_event.sh 2>>/tmp/librespot_%%MOUNT%%.log; sleep 3'
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
