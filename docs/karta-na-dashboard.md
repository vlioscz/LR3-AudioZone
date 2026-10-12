# Karta na dashboard: verze, aktualizace, restart

Od verze **0.5.8** posílá addon do Home Assistantu i senzor o sobě,
`sensor.lr3_audiozone`. Ukazuje, která verze běží, kolikáté vydání addonu to je a kolik
verzí už na dané instalaci běželo. Klient tak na dashboardu vidí, že se na addonu pořád
pracuje.

Jsou to **dvě samostatné karty**:

- **Informace** (oddíl 2): verze, vydání, počet verzí, datum aktualizace. Stačí ji vložit,
  nic dalšího se nenastavuje.
- **Restart** (oddíl 3): volitelná. Kde ji nechceš, prostě ji nevkládej.

## 1. Senzor

Vznikne sám po aktualizaci na 0.5.8.

| | |
|---|---|
| **stav** | verze, která běží (`0.5.8`) |
| `release` | kolikáté vydání addonu to je, počítáno přes všechna vydání (0.5.8 je 34.) |
| `versions_here` | kolik verzí už na **této** instalaci běželo, včetně té současné |
| `counted_since` | od které verze se na této instalaci počítá |
| `updated` | kdy současná verze na této instalaci poprvé naběhla (datum aktualizace) |
| `history` | posledních 10 verzí s datem, nejnovější první |
| `running_since` | kdy addon naposledy nastartoval; po restartu se změní |
| `radios` | kolik rádií addon našel |
| `addon` | identifikátor addonu pro restart (viz oddíl 3) |

Restart se jako nová verze nepočítá, do historie se zapíše jen změna verze. Historie je
uložená v addonu (`/data/versions.json`), takže přežije restart i aktualizaci a je
i v zálohách HA.

> **Počítá se až od 0.5.8.** Starší verze nikam nezapisovaly, kdy byly nainstalované,
> a Home Assistant ani Supervisor si to nepamatují. Po aktualizaci na 0.5.8 proto každá
> instalace začne na `versions_here: 1` a dál už přičítá. Celkový počet vydání (`release`)
> platí hned.

## 2. Karta s informacemi

Na dashboardu **Upravit → Přidat kartu → Ruční** (YAML) a vložit:

```yaml
type: entities
title: Hudba ze Spotify
entities:
  - entity: sensor.lr3_audiozone
    name: Verze
  - type: attribute
    entity: sensor.lr3_audiozone
    attribute: release
    name: Vydání číslo
  - type: attribute
    entity: sensor.lr3_audiozone
    attribute: versions_here
    name: Verzí na této instalaci
  - type: attribute
    entity: sensor.lr3_audiozone
    attribute: updated
    name: Poslední aktualizace
  - type: attribute
    entity: sensor.lr3_audiozone
    attribute: running_since
    name: Běží od
```

Data HA sám zobrazí podle jazyka uživatele, např. „12. října 2026“. Do karty lze přidat
i senzory jednotlivých rádií (`sensor.lr3_lara_…`), které ukazují, co kde hraje
([rele-podle-zony.md](rele-podle-zony.md)).

## 3. Karta s restartem (volitelná)

Restart addonu je v HA akce `hassio.addon_restart`. **Od HA 2026.5 ji smí spustit jen
administrátor.** Podle toho, kdo bude tlačítko mačkat, jsou dvě varianty.

Co klient po restartu uvidí: senzor na pár vteřin přejde na `nedostupné`, Spotify
zařízení zmizí a hudba ve všech místnostech se zastaví. Po naběhnutí se zařízení vrátí
a „Běží od“ na informační kartě ukáže nový čas. Hudbu je potom potřeba pustit znovu.

**Restart jen pro někoho:** v editoru karty na záložce **Viditelnost** přidej podmínku
**Uživatel** a vyber, kdo kartu uvidí (třeba jen ty, klient ne).

### Identifikátor addonu (pro obě varianty)

Je ve tvaru `xxxxxxxx_lr3_audiozone`. Když byl repozitář přidaný tlačítkem z README
(přesně `https://github.com/vlioscz/LR3-AudioZone`), je to **`162678ec_lr3_audiozone`**.
Jinak napsaná adresa (lomítko na konci, `.git`) dá jiný. Přesnou hodnotu najdeš:

- v logu addonu hned po startu:
  `restart button for a dashboard: action hassio.addon_restart, addon: 162678ec_lr3_audiozone`,
- v atributu `addon` senzoru `sensor.lr3_audiozone` (**Vývojářské nástroje → Stavy**).

### Varianta A: mačká admin — jen karta

Nic dalšího se nenastavuje. Vložit jako ruční kartu (identifikátor přepsat na svůj):

```yaml
type: button
name: Restart LR3 AudioZone
icon: mdi:restart
tap_action:
  action: perform-action
  perform_action: hassio.addon_restart
  data:
    addon: 162678ec_lr3_audiozone
  confirmation:
    text: >-
      Restartovat LR3 AudioZone? Hudba ze Spotify se na chvíli přeruší ve všech
      místnostech.
```

Uživateli, který admin není, tahle karta jen napíše „Unauthorized“.

### Varianta B: mačká kdokoli — pomocník + automatizace + karta

Uživatel, který není admin, restart addonu spustit nesmí. Nepomůže ani zabalit ho do
skriptu, protože skript běží pod tím, kdo ho spustil. Automatizace ale běží bez
uživatele, takže se jí kontrola admina netýká. Tlačítko tedy jen spustí automatizaci
a restart udělá ona. Nastavuje se jednou:

**1. Pomocník.** **Nastavení → Zařízení a služby → Pomocníci → Vytvořit pomocníka →
Tlačítko**, název `Restart LR3 AudioZone`, ikona `mdi:restart`. Vznikne entita
`input_button.restart_lr3_audiozone`.

**2. Automatizace.** **Nastavení → Automatizace → Vytvořit → ⋮ → Upravit v YAML**
a vložit (identifikátor přepsat na svůj):

```yaml
alias: LR3 AudioZone – restart z dashboardu
mode: single
max_exceeded: silent
triggers:
  - trigger: state
    entity_id: input_button.restart_lr3_audiozone
    to: ~
conditions:
  - condition: template
    value_template: >-
      {{ trigger.from_state is not none and trigger.to_state is not none
         and trigger.from_state.state != 'unavailable'
         and trigger.to_state.state not in ['unavailable', 'unknown']
         and trigger.from_state.state != trigger.to_state.state }}
actions:
  - action: hassio.addon_restart
    data:
      addon: 162678ec_lr3_audiozone
```

Spustí ji jen skutečné zmáčknutí. Stav pomocníka je čas posledního zmáčknutí, takže se
mění jen tehdy. `to: ~` a poslední řádek podmínky zajistí, že addon nerestartuje
přejmenování pomocníka ani změna jeho ikony: změní se tím jen atributy, ne stav. Zbytek
podmínky odfiltruje přidání nebo znovunačtení pomocníka a jeho návrat ze stavu
`nedostupné`. Stav `unknown` u `from` je povolený schválně: tlačítko, které ještě nikdo
nezmáčkl, je `unknown`, a první zmáčknutí musí fungovat. `mode: single` spolkne druhé
zmáčknutí, dokud první restart běží.

**3. Karta.** Vložit jako ruční kartu:

```yaml
type: entities
entities:
  - entity: input_button.restart_lr3_audiozone
    name: Restart LR3 AudioZone
    icon: mdi:restart
    confirmation:
      text: >-
        Restartovat LR3 AudioZone? Hudba ze Spotify se na chvíli přeruší ve všech
        místnostech.
```

## 4. Proč na kartě není „vypnout“

Vypnout addon jde (integrace Supervisor má přepínač `switch.lr3_audiozone`, od HA 2025.10,
ve výchozím stavu zakázaný), ale **na klientský dashboard ho nedávej**:

- Když addon neběží, rádia se k němu pořád dokola zkoušejí připojit: naměřeno asi
  **40 nových spojení za hodinu** místo 0,35 za běžného provozu. Přesně tohle chování
  předcházelo zamrzání rádií.
- Spotify zařízení zmizí a samo od sebe se nevrátí, dokud addon někdo zase nezapne.

Na vypnutí hudby addon vypínat není potřeba: stačí **pauza ve Spotify**, nebo **stop na
rádiu**. Ten od 0.5.7 zastaví i Spotify, když je to poslední rádio, do kterého ten
Spotify hraje. Rádia z addonu úplně vyřadíš odškrtnutím **„Audio zone function“**
v konfiguraci každého rádia.

## 5. Když senzor chybí

Senzor existuje až od **0.5.8**. V logu addonu po startu musí být
`Home Assistant sensors: sensor.lr3_audiozone (LR3 AudioZone), sensor.lr3_lara_…`. Když chybí,
platí totéž co u senzorů rádií ([rele-podle-zony.md](rele-podle-zony.md), oddíl 5).
Po restartu HA senzor na pár minut zmizí, než ho addon znovu pošle (dělá to každých
5 minut).
