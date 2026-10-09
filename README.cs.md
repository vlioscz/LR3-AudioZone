# LR3 AudioZone — Home Assistant Add-on

[English](README.md) | **Česky**

[![Přidat repozitář do Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fvlioscz%2FLR3-AudioZone)

**Spotify Connect → ELKO EP „LARA".** Addon najde LARA rádia v síti a **každé z nich nabídne
ve Spotify jako samostatné Connect zařízení** pojmenované podle toho rádia — plus „LARA All",
které hraje do všech naráz. Vybereš si v mobilu, kam pustit hudbu, a addon rádio přepne do jeho
**audio zóny** (tváří se jako **Slim server**). Když dohraješ, rádio se vrátí pod vlastní
ovládání. Žádné záložní rádio, žádné presety.

> **Odkud to vzniklo.** LR3 AudioZone vyrostl z projektu
> **[LR3-Stream](https://github.com/vlioscz/LR3-stream-addon)**, což byl jen stabilní Icecast
> stream plus Spotify Connect, bez jakéhokoli ovládání rádií. **LR3-Stream se už neudržuje**
> (poslední změna srpen 2026) — veškerá práce se teď děje tady. Pokud ho máš nasazený, tenhle
> addon ho nahrazuje: stejný stream a navíc řídí LARY.

```
 „LARA Koupelna"  librespot ─► pacer ─► ffmpeg ─► Icecast /lara_f2231c ──► LARA Koupelna
 „LARA Obývák"    librespot ─► pacer ─► ffmpeg ─► Icecast /lara_aabbcc ──► LARA Obývák
 „LARA All"       librespot ─► pacer ─► ffmpeg ─► Icecast /all ─────────► obě zároveň

        SlimProto server (:3483) ── strm ──► rádia  (řekne jim, co stáhnout)
        LMS CLI server  (:9595) ◄── stav + tlačítka ── rádia
```

## Jak to funguje

1. Při startu addon **projde síť a najde LARA rádia** i s jejich jmény (sken TCP 61695).
2. Pro **každé rádio** spustí vlastní **librespot** → ve Spotify se objeví jako samostatné
   Connect zařízení pojmenované podle toho rádia (např. „LARA Koupelna").
   Když jsou rádia **dvě a víc**, přibude ještě **„LARA All"**, které hraje do všech najednou,
   a můžeš si nadefinovat **vlastní skupiny** ručně vybraných místností (`groups`).
   U jediného rádia se skupinové zařízení nezobrazí — byl by to jen druhý název pro totéž.
3. Zvuk každé zóny se zakóduje do MP3 a teče do vlastního **Icecast** mountu. Když Spotify
   nehraje, teče do mountu ticho — mount tak nikdy nespadne a LARA ho může kdykoli začít
   stahovat. Posílá se **tempem, jakým rádia skutečně hrají**, ne podle hodin: LARA jede
   o zlomek procenta mimo reálný čas a zvuk posílaný přesně v reálném čase se před ní dřív
   hromadil, až zpoždění narostlo na desítky vteřin (`rate_match`).
4. Addon je zároveň **Slim server** — dvě služby:
   - **SlimProto** na TCP `:3483` — přenos zvuku, hlasitost, zapnutí/vypnutí výstupů.
   - **LMS CLI** na TCP `:9595` — textový kanál, kterým se LARA ptá, co hraje, a kterým
     posílá stisky svých vlastních tlačítek zpět nám. Tudy jí posíláme i **název skladby
     a interpreta**, takže na displeji běží hrající skladba, ne název zóny.
5. Když se Spotify rozehraje, addon pošle dotčeným rádiům `strm-s` → **přepnou se do audio zóny**
   a hrají. Vlastní zařízení rádia má přednost před skupinovým: pustíš-li hudbu do „LARA Koupelna"
   uprostřed skupinového poslechu, koupelna se odpojí a ostatní hrají dál.
6. **Hlasitost je posuvník v aplikaci Spotify.** Je to jediný ovladač: firmware 3.7.001
   hlasitost poslanou po síti ignoruje a vlastní tlačítka rádia během přehrávání zóny jen
   ztlumí a obnoví.
7. Když Spotify přestane hrát a uplyne `idle_timeout`, addon pošle `strm-q` a ztlumí výstupy.
   Rádio zůstane ukazovat audio zónu, dokud se ho někdo nedotkne; vracení na seznam stanic
   přes port 61695 je **volitelné** (`park_on_zone_off`, standardně vypnuté — viz poznámka níže).

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
| `spotify_bitrate` | `320` | Kvalita Spotify (96/160/320). Vyžaduje Premium. |
| `samplerate` | `48000` | **Nech být.** Při 44100 každá změřená LARA spotřebovává zvuk asi o 39 B/s rychleji, než ho stíháme dodávat — buffer se jí vypustí a hudba se zhruba po 26 minutách zastaví, ať je buffer nastavený jakkoli. 48000 ten nesoulad maže; 44100 zůstává jen kvůli srovnání. |
| `rate_match` | `auto` | **Nech `auto`** (= zapnuto). Posílá každou zónu tempem, jakým její rádia opravdu hrají — při 48 kHz jsou o ~0,33 % pomalejší — takže zpoždění zůstane kolem 5 s, místo aby rostlo o 12 s za hodinu, dokud rádio neodpojí. `off` = dosavadní engine (Liquidsoap, přesně reálný čas). |
| `spotify_remote_access` | `false` | Vypnuto: neukládá se žádné přihlášení ke Spotify, zóny vidí všichni na tvé síti a nikdo mimo ni (vypnutím se dřív uložené přihlášení i smaže). Zapnuto: účet, který zónu vybral jako poslední, zůstane přihlášený a vidí ji odkudkoli — není to ale vlastnictví, zónu může kdokoli v síti pořád převzít. |
| `audio_cache_mb` | `200` | Cache staženého audia na disku **na každou zónu** (0 = žádná). Do 0.3.7 to byl pevný 1 GB na každou — čtyři zóny znamenaly až 4 GB zápisů na připájené úložiště HA Green. |
| `zone_name` | `Audio zóna` | Náhradní název — použije se, jen když se nenajde žádné rádio. |
| `group_name` | `LARA All` | Název zařízení hrajícího do všech rádií (jen při 2+ rádiích). |
| `groups` | `[]` | Vlastní zařízení pro ručně vybrané sady místností, např. „Dovnitř" = koupelna + obývák. Rádia pojmenuj tak, jak se jmenují ve Spotify; na předponě „LARA " ani na velikosti písmen nezáleží. Skupina potřebuje aspoň dvě existující rádia a **nesmí vyjmenovat všechna** — to už je přesně „LARA All". |
| `lara_name_prefix` | `true` | Předsadit názvům „LARA " („LARA Kuchyň" vs. „Kuchyň"). |
| `scan_subnet` | prázdné | Podsíť k prohledání, např. `10.0.0`. Prázdné = ta, ve které je HA. |
| `zone_volume` | `90` | Kde u každé zóny začíná posuvník ve Spotify. `0` = nechat naplno. Od 0.4.0 nastavuje hlasitost Spotify, ne rádia — rádio hlasitost poslanou po síti ignoruje. |
| `buffer_seconds` | `2.7` | Kolik zvuku si rádio nabere, než se ozve první tón. **Nad zhruba 2,6 už to nemá žádný vliv**: rádia začnou hrát kolem 60 KB, ať jim řekneme cokoli (tak začala každá relace na dvou instalacích nastavených na 4,0). Vypadávající hudbu to nevyléčí. |
| `idle_timeout` | `60` | Vteřin ticha ve Spotify, než se rádio uvolní. **Nedávej sem málo** — viz varování níže. Pod 45 tě addon upozorní v logu. |
| `control_mode` | `slimproto` | `slimproto` = řídit LARA. `off` = jen najít a logovat; výhradně na krátkou diagnostiku, viz níže. |
| `park_on_zone_off` | `false` | Vypnuto: po skončení hudby se stream jen zastaví a ztlumí, rádio zůstane ukazovat audio zónu, dokud se ho někdo nedotkne. Zapnuto: navíc přepne zdroj zpět na seznam stanic přes port 61695. Než to zapneš, přečti si poznámku níže. |
| `cli_port` | `9595` | Port LMS CLI — musí sedět s „CLI port" v konfiguraci LARY. |
| `cli_username` / `cli_password` | prázdné | Přihlášení, které LARA na CLI posílá. Nech prázdné, pokud žádné nemá. |
| `lara_username` | `admin` | Uživatel LARA — potřebný jen pro `park_on_zone_off` (port 61695). |
| `lara_password` | `elkoep` | Heslo LARA. |
| `lara_hosts` | `[]` | Ruční IP LARA, když je sken nenajde. |

> **Aktualizuješ z 0.1.x?** Volby `fallback_enabled`, `fallback_url` a `fallback_delay` zmizely.
> Pokud si addon po aktualizaci stěžuje na neznámé volby, otevři jeho **Configuration** a ulož ji
> znovu (Supervisor si drží dříve uložené volby). `fallback_delay` nahradil `idle_timeout`.

## Senzory v Home Assistantu

Každé rádio má senzor `sensor.lr3_lara_xxxxxx` (posledních šest znaků jeho MAC; přesná
jména vypíše addon do logu po startu). Jeho stav je **Spotify zařízení, které do rádia
právě hraje** — „LARA Terasa", „LARA All", některá z tvých skupin — nebo `off`. Hodí se do
automatizací: třeba jedna LARA s několika reproduktory za relé je může přepínat podle toho,
které zařízení si kdo ve Spotify vybral. Postup krok za krokem i s hotovou automatizací:
[docs/rele-podle-zony.md](docs/rele-podle-zony.md).

## ⚠️ Krátký `idle_timeout` usekává konce skladeb

Ukončení zóny rádiu zahodí všechno, co už přijalo a ještě nepřehrálo — a rádio je vždycky
několik vteřin pozadu za aplikací: se zapnutým `rate_match` asi pět, s vypnutým po dlouhém
poslechu až půl minuty. Když je čas kratší než tohle zpoždění,
posledních několik vteřin skladby se zahodí. Jedna instalace jela na 20 s a zákazník hlásil,
že *žádná písnička nedohraje do konce*. **60 je rozumná hodnota** a jediná cena je, že zóna po
zastavení hudby ještě chvíli visí na displeji.

## ⚠️ Zatuhávání rádií (nevyřešeno)

Na jedné instalaci se třemi LARAmi jedno rádio opakovaně zatuhlo tak, že bylo nutné vytáhnout
je ze zásuvky — mrtvá tlačítka, nedostupná webová stránka, neviditelné na síti. **Příčina není
známá.** Co se ví: je to vždy jedna konkrétní jednotka (nejstarší hardwarová generace ze tří),
děje se to **ve chvíli, kdy jí addon neposílá nic než pětisekundový heartbeat**, a navazuje to
na to, že rádio někdo fyzicky použil — ne na nic, co děláme během přehrávání. Všechno, co addon
dělá nad rámec běžného Slim serveru, je dnes buď vypnuté (`park_on_zone_off`), nebo omezené, a
od 0.4.2 log hlásí, jestli rádio, které se odpojilo, ještě odpovídá na webové stránce a na
řídicím portu — tím se pozná zatuhlá jednotka od té, která jen ztratila spojení.

Když se to stane tobě: **než** vytáhneš napájení, zkus krátký stisk RESET a jestli se načte
`http://<ip-lary>` — tahle odpověď má větší cenu než cokoli jiného. Úplně mimo hru dostaneš
rádia vypnutím „Audio zone function" ve webovém rozhraní každé LARY. **Nepoužívej na to
`control_mode: off`** — když jsou porty addonu zavřené, rádia se na ně dobývají pořád dokola
(naměřeno ~40 nových spojení za hodinu proti ~0,35 při běžném provozu), což je pro ně horší než
normální běh. **Nezapomeň pak `control_mode` vrátit na `slimproto`** — dokud je `off`, rádia
nikdy nehrají a jediné, co to prozradí, je varování v logu.

## Stav

- ✅ **Běží v denním provozu na několika instalacích**, včetně dvou se třemi rádii.
- ✅ **Celá smyčka funguje na reálném hardwaru** (fw 3.7.001): Spotify se rozehraje → rádio se
  přepne do audio zóny a hraje; hudbu zastavíš → po `idle_timeout` se rádio uvolní.
- ✅ **Vypadávání hudby po ~26 minutách je vyřešené** (0.4.0, potvrzeno v provozu): výstup
  přešel na 48 kHz. Den před změnou třináct výpadků, čtyři dny po ní ani jeden, a rádia teď
  drží plný buffer přes hodinu, místo aby se pomalu vypouštěla.
- ✅ **Přehození hudby z místnosti do místnosti** už nenechá rádio ztichnout (0.4.3) ani
  neškobrtá (0.4.4).
- ✅ **Na displeji běží hrající skladba** — název a interpret jdou přes LMS CLI (:9595).
- 🔧 **Zpoždění, které rostlo během dlouhého poslechu** — 12 s za hodinu, dokud rádio po
  2 h 16 min nevypadlo — řeší 0.5.0 posíláním tempem rádií. Změřeno, nasimulováno
  a vyzkoušeno na celé cestě; čeká se na potvrzení z reálného dlouhého poslechu.
- 🔎 **Otevřené:** zatuhávání výše.
- Testovací nástroj bez nasazení add-onu:
  `python tools/zone_test.py <ip-tohoto-stroje> --proxy <url-mp3-streamu>`
