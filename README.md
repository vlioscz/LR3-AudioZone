# LR3 AudioZone — Home Assistant Add-on

[![Přidat repozitář do Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fvlioscz%2FLR3-AudioZone)

**Spotify Connect → ELKO EP „LARA".** Zapneš na mobilu Spotify Connect zařízení, které vytvoří
tento addon, a hudba se **automaticky přehraje na LARA rádiích** — addon se tváří jako
**Slim server** a přepne LARA do její **audio zóny**. Když Spotify přestaneš poslouchat,
LARA se po nastavené prodlevě **zase vypne**. Žádné záložní rádio, žádné presety.

> Sesterský projekt **[LR3-Stream](https://github.com/vlioscz/LR3-stream-addon)** je čistý stabilní
> stream + Spotify Connect (bez ovládání rádií). **LR3-AudioZone** přidává výstup do LARA přes Slim.

```
Spotify Connect (librespot) ──► Liquidsoap ──► Icecast /default ─┐
                                                                 │  (LARA si stream stáhne)
        SlimProto server (:3483) ── strm ──► LARA rádia ◄────────┘
        LMS CLI server  (:9595) ◄─ stav + tlačítka ─┘
```

## Jak to funguje

1. **librespot** vytvoří Spotify Connect zařízení pojmenované podle `zone_name` (např. „Audio zóna").
2. Jeho zvuk teče přes **Liquidsoap** do **Icecast** mountu `/default`. Když Spotify nehraje,
   teče do mountu ticho — mount tak nikdy nespadne a LARA ho může kdykoli začít stahovat.
3. Addon je zároveň **Slim server** — dvě služby:
   - **SlimProto** na TCP `:3483` — přenos zvuku, hlasitost, zapnutí/vypnutí výstupů.
   - **LMS CLI** na TCP `:9595` — textový kanál, kterým se LARA ptá, co hraje, a kterým
     posílá stisky svých vlastních tlačítek zpět nám.
4. Když se Spotify rozehraje, addon pošle LAŘE `strm-s` → **LARA se přepne do audio zóny** a hraje.
   Posuvník hlasitosti v aplikaci Spotify přitom **ovládá přímo hlasitost LARY** — stream samotný
   se neztlumuje, takže nevznikne skrytý druhý regulátor, kterým by šlo omylem „vypnout zvuk".
5. Když Spotify přestane hrát a uplyne `idle_timeout`, addon pošle `strm-q`, ztlumí výstupy a
   **vrátí LARU na seznam rádií — zastavenou**, aby zóna nezůstala viset na displeji a rádio
   bylo připravené pro toho, kdo k němu přijde. Chování řídí `lara_off_action`.

## Předpoklad: nasměruj LARA na HA jako slim server

Každá LARA musí mít v konfiguraci zapnutou **„Audio zone function"**, jako IP slim serveru
adresu tvého HA a **CLI port** shodný s volbou `cli_port` (výchozí 9595). Nastavíš to buď
v **ELKO Configuratoru**, nebo přímo ve **webovém rozhraní LARA** (`http://<ip-lary>`,
přihlášení admin/heslo) → sekce **„Audio zone function"**. Port SlimProto je 3483.

## Konfigurace

| Volba | Výchozí | Popis |
|---|---|---|
| `port` | `8121` | Port Icecast streamu (odsud si LARA stáhne zvuk). |
| `source_password` | `changeme` | Interní heslo Icecastu. LARA ho nepotřebuje. |
| `bitrate` | `192` | Bitrate MP3 posílaného do LARA (kbps). |
| `spotify_bitrate` | `320` | Kvalita Spotify (96/160/320). |
| `zone_name` | `Audio zóna` | Název Spotify Connect zařízení = název audio zóny. |
| `zone_volume` | `90` | Výchozí hlasitost zóny, dokud nepohneš posuvníkem ve Spotify. `0` = hlasitost LARY neměnit. |
| `buffer_seconds` | `1.5` | Kolik sekund si LARA načte, než začne hrát = hlavní zdroj zpoždění. Níž = svižnější, ale hrozí výpadky. |
| `idle_timeout` | `20` | Sekundy nečinnosti Spotify, než se LARA vypne. |
| `control_mode` | `slimproto` | `slimproto` = řídit LARA. `off` = jen najít a logovat (test). |
| `cli_port` | `9595` | Port LMS CLI — musí sedět s „CLI port" v konfiguraci LARY. |
| `cli_username` / `cli_password` | prázdné | Přihlášení, které LARA na CLI posílá (pokud nějaké má). |
| `lara_off_action` | `radio` | `radio` = vrátit LARU na seznam rádií, nezapnutou. `slim` = jen zastavit stream (LARA zůstane svítit v prázdné zóně). `slim_elko` = slim + stop přes 61695. `none` = nechat být. |
| `lara_username` | `admin` | Uživatel LARA (potřeba pro `radio` / `slim_elko`). |
| `lara_password` | `elkoep` | Heslo LARA. |
| `lara_hosts` | `[]` | Ruční IP LARA, když je broadcast nenajde. |

> **Aktualizuješ z 0.1.x?** Volby `fallback_enabled`, `fallback_url` a `fallback_delay` zmizely.
> Pokud si addon po aktualizaci stěžuje na neznámé volby, otevři jeho **Configuration** a ulož ji
> znovu (Supervisor si drží dříve uložené volby). `fallback_delay` nahradil `idle_timeout`.

## Stav

- ✅ **Přehrávání ověřeno na reálné LAŘE** (fw 3.7.001): 60 s souvislého zvuku, žádný výpadek,
  čisté zastavení a vypnutí.
- ✅ **LARA se opravdu připojuje i na LMS CLI** (:9595) — přihlásí se, dotazuje se, co hraje,
  a hlásí polohu vlastního knoflíku hlasitosti.
- 🧪 **v0.2.0** — fallback rádio odstraněno, přidán LMS CLI server a vypínání zóny.
  Na HA ještě neproběhla celá smyčka „Spotify hraje → LARA hraje → pauza → LARA zhasne";
  obě její poloviny ale ověřené jsou.
- Testovací nástroj bez nasazení add-onu:
  `python tools/zone_test.py <ip-tohoto-stroje> --proxy <url-mp3-streamu>`
