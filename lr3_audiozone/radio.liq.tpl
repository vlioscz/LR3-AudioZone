# LR3 AudioZone — zóna "%%ZONE_NAME%%"  ->  mount /%%MOUNT%%
# Do mountu teče POUZE Spotify. Když Spotify nehraje, teče ticho — mount tím nikdy nespadne
# (LARA si ho musí umět stáhnout v okamžiku, kdy jí pošleme strm-s). Žádné záložní rádio:
# o to, aby LARA při nečinnosti zhasla, se stará SlimProto controller (strm-q + aude off).

settings.log.stdout.set(true)
settings.log.level.set(3)
# Kontejner addonu běží jako root; Liquidsoap by se jinak z bezpečnosti ukončil.
settings.init.allow_root.set(true)

# --- Vzorkovací frekvence výstupu ---
# ⚠️ Tohle je oprava podtékajícího bufferu, ne kosmetika. Změřeno na dvou nezávislých
# instalacích: při 44,1 kHz ubývá LAŘE vstupní buffer stále o ~39 B/s, tedy ~1650 ppm, a za
# 26 minut je prázdný — pokaždé. Taktování Liquidsoapu v tom nejede, ten spí do absolutního
# termínu a chybu nekumuluje (src/clock.ml: t0 + frame_duration*ticks - time()). Dva různé
# hostitele se navíc nemohou shodnout na stejných 39 B/s; firmware stejného modelu ano.
# Při 48 kHz vychází MP3 rámec na přesně 144*bitrate/48000 bajtů (192k → 576 B) bez jediného
# vycpávkového bitu, a 48 kHz je zároveň frekvence, kterou umí odvodit běžný 12,288MHz
# audio krystal beze zbytku. Když deficit zmizí, byla příčina tady.
settings.frame.audio.samplerate.set(%%SAMPLERATE%%)

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
  # librespot posílá VŽDY 44100 Hz S16. Musí se to říct explicitně, protože jinak se převezme
  # globální frame.audio.samplerate — a při 48 kHz by se PCM přečetlo o 8,8 % rychleji.
  # Převzorkování na výstupní frekvenci si Liquidsoap udělá sám (libsamplerate).
  samplerate=44100,
  'LR3_MOUNT=%%MOUNT%% librespot --name "%%ZONE_NAME%%" --device-type speaker --backend pipe --format S16 --bitrate %%SPOTIFY_BITRATE%% --initial-volume %%INITIAL_VOLUME%% %%LIBRESPOT_CACHE_ARGS%% --enable-volume-normalisation --onevent /etc/lr3/spotify_event.sh 2>>/tmp/librespot_%%MOUNT%%.log; sleep 3'
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
