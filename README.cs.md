# LR3 AudioZone — Home Assistant Add-on

[English](README.md) | **Česky**

[![Přidat repozitář do Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fvlioscz%2FLR3-AudioZone)

**Spotify Connect → ELKO EP „LARA".** Addon najde LARA rádia v síti a **každé z nich nabídne
ve Spotify jako samostatné Connect zařízení** pojmenované podle toho rádia — plus „LARA All",
které hraje do všech naráz. Vybereš si v mobilu, kam pustit hudbu, a addon rádio přepne do jeho
**audio zóny** (tváří se jako **Slim server**). Když dohraješ, rádio se vrátí na seznam stanic.
Žádné záložní rádio, žádné presety.

> Sesterský projekt **[LR3-Stream](https://github.com/vlioscz/LR3-stream-addon)** je čistý stabilní
> stream + Spotify Connect (bez ovládání rádií). **LR3-AudioZone** přidává výstup do LARA přes Slim.

```
 „LARA Koupelna"  librespot ─► Liquidsoap ─► Icecast /lara_f2231c ──► LARA Koupelna
 „LARA Obývák"    librespot ─► Liquidsoap ─► Icecast /lara_aabbcc ──► LARA Obývák
 „LARA All"       librespot ─► Liquidsoap ─► Icecast /all ─────────► obě zároveň

        SlimProto server (:3483) ── strm ──► rádia  (řekne jim, co stáhnout)
        LMS CLI server  (:9595) ◄── stav + tlačítka ── rádia
```

## Jak to funguje

1. Při startu addon **projde síť a najde LARA rádia** i s jejich jmény (sken TCP 61695).
2. Pro **každé rádio** spustí vlastní **librespot** → ve Spotify se objeví jako samostatné
   Connect zařízení pojmenované podle toho rádia (např. „LARA Koupelna").
   Když jsou rádia **dvě a víc**, přibude ještě **„LARA All"**, které hraje do všech najednou.
   U jediného rádia se skupinové zařízení nezobrazí — byl by to jen druhý název pro totéž.
3. Zvuk každé zóny teče přes **Liquidsoap** do vlastního **Icecast** mountu. Když Spotify nehraje,
   teče do mountu ticho — mount tak nikdy nespadne a LARA ho může kdykoli začít stahovat.
4. Addon je zároveň **Slim server** — dvě služby:
   - **SlimProto** na TCP `:3483` — přenos zvuku, hlasitost, zapnutí/vypnutí výstupů.
   - **LMS CLI** na TCP `:9595` — textový kanál, kterým se LARA ptá, co hraje, a kterým
     posílá stisky svých vlastních tlačítek zpět nám. Tudy jí posíláme i **název skladby
     a interpreta**, takže na displeji běží hrající skladba, ne název zóny.
5. Když se Spotify rozehraje, addon pošle dotčeným rádiům `strm-s` → **přepnou se do audio zóny**
   a hrají. Vlastní zařízení rádia má přednost před skupinovým: pustíš-li hudbu do „LARA Koupelna"
   uprostřed skupinového poslechu, koupelna se odpojí a ostatní hrají dál.
6. **Hlasitost se řídí posuvníkem v aplikaci Spotify.** Je to jediný ovladač — na LAŘE fungují
   tlačítka hlasitosti během přehrávání audio zóny jen jako ztlumit/obnovit, takže tam se
   hlasitost nastavit nedá.
7. Když Spotify přestane hrát a uplyne `idle_timeout`, addon pošle `strm-q`, ztlumí výstupy a
   přes port 61695 **vrátí rádio na seznam stanic — zastavené**, aby zóna nezůstala viset na
   displeji a rádio bylo připravené pro toho, kdo k němu přijde.

> **Nové rádio v síti?** Sada Connect zařízení se určuje při startu — po přidání rádia
> **restartuj add-on**. Do té doby ho addon sice řídí (jede v „LARA All"), ale vlastní
> zařízení ve Spotify nedostane.

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
| `spotify_remote_access` | `false` | Vypnuto: neukládá se žádné přihlášení ke Spotify, zóny vidí všichni na tvé síti a nikdo mimo ni (vypnutím se dřív uložené přihlášení i smaže). Zapnuto: účet, který zónu vybral jako poslední, zůstane přihlášený a vidí ji odkudkoli. |
| `zone_name` | `Audio zóna` | Náhradní název — použije se, jen když se nenajde žádné rádio. |
| `group_name` | `LARA All` | Název zařízení hrajícího do všech rádií (jen při 2+ rádiích). |
| `lara_name_prefix` | `true` | Předsadit názvům „LARA " („LARA Kuchyň" vs. „Kuchyň"). |
| `scan_subnet` | prázdné | Podsíť k prohledání, např. `10.0.0`. Prázdné = ta, ve které je HA. |
| `zone_volume` | `90` | Hlasitost nastavená rádiu při zapnutí zóny. `0` = hlasitost rádia neměnit. |
| `buffer_seconds` | `1.5` | Kolik sekund si LARA načte, než začne hrát = hlavní zdroj zpoždění. Níž = svižnější, ale hrozí výpadky. |
| `idle_timeout` | `8` | Sekundy nečinnosti Spotify, než LARA opustí zónu = jak dlouho zóna po zastavení hudby ještě visí na displeji. |
| `control_mode` | `slimproto` | `slimproto` = řídit LARA. `off` = jen najít a logovat (test). |
| `park_on_zone_off` | `false` | Vypnuto: po skončení hudby se stream jen zastaví a ztlumí, rádio zůstane ukazovat audio zónu, dokud se ho někdo nedotkne. Zapnuto: navíc přepne zdroj zpět na seznam stanic přes port 61695. Než to zapneš, přečti si poznámku níže. |
| `cli_port` | `9595` | Port LMS CLI — musí sedět s „CLI port" v konfiguraci LARY. |
| `cli_username` / `cli_password` | prázdné | Přihlášení, které LARA na CLI posílá (pokud nějaké má). |
| `lara_username` | `admin` | Uživatel LARA — nutný pro návrat na seznam rádií (port 61695). |
| `lara_password` | `elkoep` | Heslo LARA. |
| `lara_hosts` | `[]` | Ruční IP LARA, když je broadcast nenajde. |

> **Aktualizuješ z 0.1.x?** Volby `fallback_enabled`, `fallback_url` a `fallback_delay` zmizely.
> Pokud si addon po aktualizaci stěžuje na neznámé volby, otevři jeho **Configuration** a ulož ji
> znovu (Supervisor si drží dříve uložené volby). `fallback_delay` nahradil `idle_timeout`.

## ⚠️ Zatuhávání rádií (nevyřešeno)

Na jedné instalaci se třemi LARAmi dvě rádia zatuhla tak, že bylo nutné vytáhnout je ze
zásuvky — mrtvá tlačítka, nedostupná webová stránka, neviditelná na síti. **Příčina není
známá.** Verze 0.3.6 vypíná nebo omezuje všechno, co addon dělá nad rámec běžného Slim serveru;
především je nově vypnuté vracení rádia na seznam stanic přes port 61695 (`park_on_zone_off`).

Když se to stane tobě: vytáhni napájení, ale **předtím** zkus krátký stisk RESET a jestli se
načte `http://<ip-lary>` — tahle odpověď má větší cenu než cokoli jiného. Úplně mimo hru dostaneš
rádia nastavením `control_mode: off` a vypnutím „Audio zone function" ve webovém rozhraní každé LARY.

## Stav

- ✅ **Celá smyčka ověřena na HA s reálnou LAROU** (fw 3.7.001): Spotify se rozehraje →
  LARA se přepne do audio zóny a hraje (zpoždění ~2 s); hudbu zastavíš → po `idle_timeout`
  se LARA vrátí na seznam stanic.
- ✅ **Na displeji běží hrající skladba** — název a interpret jdou přes LMS CLI (:9595).
- ✅ **Hlasitost je posuvník ve Spotify** — jediný ovladač. Rádio na tomhle firmwaru žádnou
  vlastní cestu k hlasitosti nemá: jeho tlačítka během zóny jen ztlumí/obnoví.
- 🧪 **Zatím nevyzkoušeno:** víc LAR hrajících naráz (multi-radio kód je v 0.3.0)
  a kalibrace stupnice `zone_volume`.
- Testovací nástroj bez nasazení add-onu:
  `python tools/zone_test.py <ip-tohoto-stroje> --proxy <url-mp3-streamu>`
