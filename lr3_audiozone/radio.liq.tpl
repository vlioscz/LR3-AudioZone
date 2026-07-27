# LR3 AudioZone — zóna "%%ZONE_NAME%%"  ->  mount /%%MOUNT%%
# Priorita: Spotify Connect > online rádio > ticho. Mount NIKDY nespadne.

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
spotify_raw = input.external.rawaudio(
  id="spotify_%%MOUNT%%",
  restart=true, restart_on_error=true,
  buffer=1.0, max=1.5, log_overfull=false,
  'LR3_MOUNT=%%MOUNT%% librespot --name "%%ZONE_NAME%%" --device-type speaker --backend pipe --format S16 --bitrate %%SPOTIFY_BITRATE%% --initial-volume 100 --cache /data/librespot_%%MOUNT%% --cache-size-limit 1G --enable-volume-normalisation --onevent /etc/lr3/spotify_event.sh 2>>/tmp/librespot_%%MOUNT%%.log; sleep 3'
)

# librespot při pauze PŘESTANE zapisovat (nevydává ticho). Obalíme ho proto tichem,
# aby byl zdroj VŽDY dostupný a blank.strip měl co měřit. Díky tomu se při pauze drží
# ticho po dobu %%FALLBACK_DELAY%%s a teprve PAK naskočí rádio — krátká pauza nebo
# přechod mezi skladbami rádio nespustí. Obnovení Spotify přepne zpět okamžitě.
spotify_hold = fallback(id="spotify_hold_%%MOUNT%%", track_sensitive=false,
                        [spotify_raw, blank(id="pause_%%MOUNT%%", duration=-1.)])
spotify = blank.strip(id="spotify_live_%%MOUNT%%", max_blank=%%FALLBACK_DELAY%%., threshold=-50., spotify_hold)

# --- Záložní online rádio (např. Evropa 2), reconnectuje se samo ---
# Když je fallback vypnutý, tenhle zdroj se nezapojí do grafu a vůbec se nepřipojuje.
radio = input.http(id="fallback_%%MOUNT%%", "%%FALLBACK_URL%%")

# --- Poslední záchrana: nekonečné ticho, aby byl mount vždy krmený ---
silent = blank(id="silence_%%MOUNT%%", duration=-1.)

# Priorita. track_sensitive=false → přepne v okamžiku, kdy zdroj (ne)naskočí.
# S vypnutým fallbackem zůstane po prodlevě ticho (a SlimProto controller pošle LARA stop).
sources = if %%FALLBACK_ENABLED%% then [spotify, radio, silent] else [spotify, silent] end
main = fallback(id="main_%%MOUNT%%", track_sensitive=false, sources)

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
