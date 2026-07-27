# LR3 AudioZone — Home Assistant Add-on

[![Přidat repozitář do Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fvlioscz%2FLR3-AudioZone)

**Spotify Connect → ELKO EP „LARA".** Zapneš na mobilu Spotify Connect zařízení, které vytvoří
tento addon, a hudba se **automaticky přehraje na LARA rádiích** — addon je pošle na svůj stream
přes **Slim server (SlimProto)** a aktivuje jim „audio zónu". Když Spotify zastavíš, LARA hraje
záložní rádio, nebo se zastaví.

> Sesterský projekt **[LR3-Stream](https://github.com/vlioscz/LR3-stream-addon)** je čistý stabilní
> stream + Spotify Connect (bez ovládání rádií). **LR3-AudioZone** přidává výstup do LARA přes Slim.

```
Spotify Connect (librespot) ──► Liquidsoap ──► Icecast /default ─┐
                                                                 │  (LARA si stream stáhne)
        SlimProto server (:3483) ── strm ──► LARA rádia ◄────────┘
        (při „Spotify hraje" pushne LARA na /default a aktivuje audio zónu)
```

## Jak to funguje

1. **librespot** vytvoří Spotify Connect zařízení pojmenované podle `zone_name` (např. „Audio zóna").
2. Jeho zvuk teče přes **Liquidsoap** do **Icecast** mountu `/default` (MP3, nikdy nespadne).
3. Addon je zároveň **SlimProto (Squeezebox) server** na TCP `:3483` a **hledá LARA rádia** (UDP broadcast).
4. Když se Spotify rozehraje, addon pošle nalezeným LARA přes SlimProto příkaz, ať přehrají
   `http://<HA>:<port>/default` → LARA se přepne do **audio zóny** a hraje.
5. Pauza → po prodlevě záložní rádio (`fallback_enabled`), nebo se LARA zastaví.

## Předpoklad: nasměruj LARA na HA jako slim server

Každá LARA musí mít v konfiguraci zapnutou **„Audio zone function"** a jako IP slim serveru
adresu tvého HA. Nastavíš to buď v **ELKO Configuratoru**, nebo přímo ve **webovém rozhraní LARA**
(`http://<ip-lary>`, přihlášení admin/heslo) → sekce **„Audio zone function"** → zaškrtnout +
IP = adresa HA. Port SlimProto je 3483.

## Konfigurace

| Volba | Výchozí | Popis |
|---|---|---|
| `port` | `8121` | Port Icecast streamu (odsud si LARA stáhne zvuk). |
| `source_password` | `changeme` | Interní heslo Icecastu. LARA ho nepotřebuje. |
| `bitrate` | `192` | Bitrate MP3 posílaného do LARA (kbps). |
| `spotify_bitrate` | `320` | Kvalita Spotify (96/160/320). |
| `zone_name` | `Audio zóna` | Název Spotify Connect zařízení = název audio zóny. |
| `fallback_enabled` | `false` | Po pauze Spotify: `true` = záložní rádio, `false` = LARA stop. |
| `fallback_url` | `…fm-evropa2-128` | Záložní online rádio. |
| `fallback_delay` | `15` | Prodleva (s) ticha, než naskočí záloha / stop. |
| `control_mode` | `slimproto` | `slimproto` = pushovat přes SlimProto. `off` = jen najít a logovat (test). |
| `lara_username` | `admin` | Uživatel LARA. |
| `lara_password` | `elkoep` | Heslo LARA. |
| `lara_hosts` | `[]` | Ruční IP LARA, když je broadcast nenajde. |

## Stav

- ✅ **Ověřeno na reálné LAŘE** (fw 3.7.001): SlimProto HELO projde, LARA hlásí `mp3`, `strm-s`
  ji přepne do audio zóny.
- 🧪 **v0.1.0** — první scaffold. Automatický tok „Spotify hraje → LARA hraje" je potřeba doladit
  na zařízení (spolehlivé přepnutí, hlasitost, návrat po pauze, více rádií).
