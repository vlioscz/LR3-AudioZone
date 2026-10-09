# Přepínání reproduktorů podle zvoleného Spotify zařízení

Typická situace: na jedné LAŘE visí víc reproduktorů a relé (třeba v iNELSu) určují, které
z nich hrají. Cíl je, aby o tom rozhodovalo, **které zařízení si člověk vybere ve Spotify**.
Například „LARA Terasa“ pustí jen reproduktory u sezení, „LARA All“ k tomu přidá i ty
u bazénu.

Addon od verze 0.5.0 dává do Home Assistantu ke každému rádiu senzor. Relé pak přepne
obyčejná automatizace v HA.

## 1. Senzor

Každé rádio má senzor `sensor.lr3_lara_XXXXXX`, kde `XXXXXX` je posledních šest znaků MAC
adresy rádia (stejně jako jméno mountu v logu, např. `lara_1030d9`).

| | |
|---|---|
| **stav** | jméno Spotify zařízení, které do rádia právě hraje, přesně jak ho ukazuje aplikace Spotify (`LARA Terasa`, `LARA All`, název vlastní skupiny…), nebo `off` |
| `radio` | jméno rádia |
| `mount` | technické jméno zóny (`lara_1030d9`, `all`, `grp1`…) |
| `spotify` | `playing`, když Spotify do té zóny právě hraje, `idle` při pauze |
| `mac`, `ip` | adresa rádia |

**Kde najdu přesná jména senzorů:** v logu addonu hned po startu je řádek
`Home Assistant sensors: sensor.lr3_lara_1030d9 (LARA Terasa), …`. Nebo v HA
**Vývojářské nástroje → Stavy** a do filtru napsat `lr3`.

Pár věcí, které je dobré vědět:

- Stav se mění ve chvíli, kdy addon pošle rádiu nový stream, a vrací se na `off`, když zónu
  vypne (po `idle_timeout`). Pauza ve Spotify stav **nemění**, změní se jen atribut
  `spotify` na `idle`. Relé tedy necvaká při každé pauze.
- Senzory vytváří addon přes API, takže v HA nejsou v registru entit: nejdou přejmenovat
  v UI a nemají zařízení. V automatizacích se ale používají normálně podle `entity_id`.
- Po restartu Home Assistantu senzory na pár minut zmizí, než je addon znovu pošle (dělá to
  každých 5 minut). Když se addon vypíná, nastaví je na `unavailable`.

## 2. Jaká zařízení ve Spotify použít

Pro rádio na terase přicházejí v úvahu:

- jeho **vlastní zařízení** („LARA Terasa“), které hraje jen na terase,
- **„LARA All“** (volba `group_name`, lze přejmenovat), které hraje všude včetně terasy,
- **vlastní skupiny** z volby `groups`, které terasu obsahují.

Každé z nich může znamenat jinou sadu reproduktorů. Senzor terasy vždy řekne, které právě
hraje.

> Skupina v hlavním addonu musí mít aspoň dvě rádia a nesmí obsahovat všechna (to je
> „LARA All“). Dvě zařízení pro úplně stejnou sadu rádií tedy udělat nejde. Pro terasu
> obvykle stačí rozlišit „jen terasa“ (vlastní zařízení) a „všude“ („LARA All“).

## 3. Automatizace

V HA: **Nastavení → Automatizace → Vytvořit → ⋮ → Upravit v YAML** a vložit (entity
přepsat na svoje):

```yaml
alias: Terasa – reproduktory podle Spotify
mode: queued
trigger:
  - platform: state
    entity_id: sensor.lr3_lara_1030d9
action:
  - choose:
      # Jen terasa → hrají všechny tři reproduktory u sezení
      - conditions:
          - condition: state
            entity_id: sensor.lr3_lara_1030d9
            state: "LARA Terasa"
        sequence:
          - service: switch.turn_on
            target:
              entity_id:
                - switch.terasa_repro_2
                - switch.terasa_repro_3
      # Celý dům → na terase jen hlavní reproduktor
      - conditions:
          - condition: state
            entity_id: sensor.lr3_lara_1030d9
            state: "LARA All"
        sequence:
          - service: switch.turn_off
            target:
              entity_id:
                - switch.terasa_repro_2
                - switch.terasa_repro_3
      # Zóna vypnutá → výchozí stav
      - conditions:
          - condition: state
            entity_id: sensor.lr3_lara_1030d9
            state: "off"
        sequence:
          - service: switch.turn_off
            target:
              entity_id:
                - switch.terasa_repro_2
                - switch.terasa_repro_3
```

Stav `unavailable` (addon se vypíná nebo restartuje) nespadá do žádné větve, takže se
nestane nic a relé zůstanou, jak byla.

Stav senzoru je jméno zařízení **přesně** tak, jak je ve Spotify, včetně velkých písmen.
Když skupinu nebo `group_name` přejmenuješ, musíš jméno přepsat i v automatizaci.

## 4. Načasování

Při přepnutí na jiné zařízení addon rádiu nejdřív zastaví starý stream. Rádio zahodí, co
mělo v bufferu, a nový začne hrát až po zhruba dvou a půl vteřinách načítání. Senzor se
změní na začátku téhle pauzy, takže relé přepíná v tichu, ne uprostřed písničky.
Při vypnutí zóny je to stejné.

## 5. Když senzory nejsou vidět

V logu addonu hledej řádek `cannot update the Home Assistant sensors (…)`. V závorce je
důvod. Addon potřebuje přístup k API Home Assistantu, který si od verze 0.5.0 sám vyžádá
v konfiguraci. Pokud to po aktualizaci nefunguje, pomůže addon jednou restartovat.
Přehrávání chyba senzorů nijak neovlivní.
