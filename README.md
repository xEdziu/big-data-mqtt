# Telemetria maszyn górniczych z buforowaniem na krawędzi (Edge Store-and-Forward)

Projekt z przedmiotu *Big Data w Przemyśle 4.0*. Maszyny pracujące pod ziemią (wozidła, ładowarki LHD,
wiertnice, wentylatory) mają czujniki na ESP32 i tylko ESP32 publikuje telemetrię do MQTT, bez bramki
pośredniej. W chodnikach bez zasięgu ESP zapisuje pomiary w buforze cyklicznym we flashu i po odzyskaniu
łączności dosyła je z oryginalnym czasem pomiaru. Dopóki bufor się nie przepełni, żaden pomiar nie ginie,
a utratę po przepełnieniu widać jawnie (`seq`, `dropped`). Urządzenia ESP32 są symulowane w Pythonie
(`simulator/`) z tymi samymi ograniczeniami: stała pojemność, rekordy binarne, brak bazy danych.

```
[sim-haul-truck x8] ┐                                             ┌─> bucket telemetry (7 dni)
[sim-lhd-loader x5] ├─ MQTT QoS 1 ─> Mosquitto ─> Telegraf ─> InfluxDB ─┤
[sim-drill-rig  x4] │                   │                              └─> task 1m ─> bucket telemetry_1m (∞)
[sim-vent-fan   x4] ┘                   │ WebSockets                           │
                                        └──> panel dyspozytora (:8080)    Grafana (:3000) + alert CH4
```

## Uruchomienie

Wymagany jest Docker z Docker Compose.

```bash
docker compose up -d --build
```

| Usługa | Adres | Uwagi |
|---|---|---|
| Panel dyspozytora (mapa + awarie) | http://localhost:8080 | |
| Grafana | http://localhost:3000 | dashboard otwiera się bez logowania; admin/admin |
| InfluxDB | http://localhost:8086 | admin / `INFLUX_ADMIN_PASSWORD` z `.env` |
| MQTT | `localhost:1883`, WebSockets `localhost:9001` | |

Pełny reset danych: `docker compose down -v`.

Liczbę maszyn każdego typu ustawia zmienna `DEVICES` w `compose.yaml`. Każdy typ działa w osobnym
kontenerze i ma inne pola, inny interwał pomiaru i inną podatność na zaniki łączności.

## Scenariusz demonstracji (ok. 5 min)

1. **Normalna praca:** w panelu maszyny jeżdżą po chodnikach, a w Grafanie dane płyną na bieżąco.
2. **Strefa bez zasięgu:** wozidło wjeżdżające w czerwoną strefę robi się pomarańczowe (brak danych).
   Po wyjeździe jest przez chwilę niebieskie, bo dosyła zaległe pomiary, a potem znów zielone. W Grafanie dziura
   w wykresach wypełnia się wstecz, na wykresie opóźnienia pojawia się „trójkąt”, a na osi stanów łączności
   widać, gdzie i dlaczego dane były buforowane.
3. **Awaria wywołana ręcznie:** wybierz maszynę w panelu i kliknij *Utrata łączności* (np. 60 s).
4. **Wyciek metanu:** *Wyciek CH4* na wentylatorze. Po ok. 30 s w Grafanie odpala się alert *Metan CH4 > 1%*.
   *Przegrzanie* i *Zatrzymaj* działają podobnie, a *Reset* przywraca maszynę do normy.
5. **Prawdziwa awaria infrastruktury:**
   ```bash
   docker compose stop mosquitto   # odczekaj ~30 s; w logach symulatorów: [NET] rozłączono
   docker compose start mosquitto  # w logach: [FORWARD] wysłano N zaległych rekordów
   docker compose logs -f sim-haul-truck
   ```
6. **Przepełnienie bufora ESP32:** zmniejsz bufor do 120 rekordów (2 min przy pomiarze co 1 s)
   i wywołaj na wozidle *Utrata łączności* na 300 s:
   ```bash
   BUFFER_RECORDS=120 docker compose up -d   # PowerShell: $env:BUFFER_RECORDS=120; docker compose up -d
   ```
   W logach pojawi się `[BUFFER] ... bufor pełny`, po powrocie zasięgu dosyłane są tylko 2 ostatnie minuty,
   a w Grafanie *Utracone pomiary (24 h)* pokazuje ok. 180. Powrót do pełnego bufora: `docker compose up -d`.

   Sprawdzenie, że nic nie zginęło: największy odstęp między kolejnymi pomiarami każdego urządzenia
   jest równy jego interwałowi.
   ```bash
   docker compose exec influxdb influx query 'from(bucket:"telemetry") |> range(start:-5m)
     |> filter(fn:(r)=>r._field=="delay_ms") |> elapsed(unit:1ms) |> max(column:"elapsed")
     |> keep(columns:["device_id","elapsed"]) |> group()'
   ```

## Jak to działa i dlaczego tak

**Wzorzec Outbox na urządzeniu** (`simulator/src/edge_sim/device.py`). Każdy pomiar jest zawsze
najpierw zapisywany w buforze we flashu. Osobna pętla wysyła rekordy od najstarszego (QoS 1) i zwalnia rekord
dopiero po potwierdzeniu odbioru przez broker (PUBACK). Prostsze podejście „jeśli połączony, to wyślij,
a jeśli nie, to zapisz” jest dziurawe: klient MQTT (paho-mqtt w symulatorze, ESP-MQTT na ESP32) przy zerwanym
połączeniu nie zgłasza błędu, tylko odkłada wiadomość do kolejki w RAM, a ta ginie przy restarcie urządzenia.

**Bufor ESP32 zamiast bazy danych** (`simulator/src/edge_sim/buffer.py`, `record.py`). ESP32 ma ok. 520 KB RAM
i 4 MB flasha, bez systemu plików z prawdziwego zdarzenia. SQLite jest tu za ciężki i zużywa flash
przepisywaniem stron przy każdym commicie, a potrzebna jest tylko kolejka FIFO. Bufor jest więc logiem
cyklicznym na partycji flasha (w symulatorze: plik `<device_id>.ring`):

- partycja 1,5 MB = superblok + 24 576 slotów po 64 B. Pomiar `seq` leży zawsze w slocie `seq % pojemność`;
- pomiar jest zapisany binarnie (odpowiednik struktury C: `ts`, stan łącza, maska awarii, `x`, `y`, pola maszyny
  jako float32/uint), co daje 42-46 B zamiast ok. 250 B JSON-a. JSON powstaje dopiero przy wysyłce;
- nagłówek slotu: `seq`, flaga, długość, CRC32. Po PUBACK zeruje się tylko bajt flagi, bo na flashu zmiana
  bitów 1 → 0 nie wymaga kasowania sektora. CRC odrzuca slot przerwany zanikiem zasilania;
- po restarcie stan (kolejka niewysłanych rekordów, następny `seq`) odtwarza się ze skanu slotów;
- pełny bufor nadpisuje najstarszy niewysłany pomiar (drop-oldest), bo dla dyspozytora najcenniejsze są
  najświeższe dane. Licznik `dropped` jest trwały (superblok), a ciągły `seq` pozwala wykryć dziurę po stronie
  serwera.

Pojemność: 24 576 pomiarów to ok. 6,8 h bez zasięgu przy pomiarze co 1 s (wozidła, ładowarki), 13,6 h
co 2 s (wiertnice) i 34 h co 5 s (wentylatory). Zmienia ją `BUFFER_RECORDS`. Przy zmianie pojemności
niewysłane rekordy są przenoszone, a jeśli się nie mieszczą, przepadają najstarsze.

Czym prawdziwy firmware różniłby się od symulatora:

- zapis przez `esp_partition_write` z kasowaniem sektora 4 KB (64 sloty) przed zawinięciem bufora;
- pomiary zbierane w RAM i zrzucane na flash porcjami (np. co 10), żeby oszczędzać flash. Ceną jest utrata
  ostatniej porcji przy nagłym zaniku zasilania;
- licznik `dropped` w NVS zamiast w superbloku;
- czas: ESP32 nie ma podtrzymywanego zegara, więc `ts` wymaga synchronizacji SNTP po starcie albo modułu RTC
  z baterią (np. DS3231). Bez tego po restarcie w strefie bez zasięgu urządzenie nie zna godziny pomiaru.
  Komendy awarii z panelu (`cmd`) są funkcją demonstracyjną symulatora, nie firmware'u.

**At-least-once i idempotencja.** Przy QoS 1 duplikaty są możliwe, ale InfluxDB nadpisuje punkt o tej samej
serii i znaczniku czasu. W efekcie w bazie każdy pomiar jest dokładnie raz.

**Buforowanie na każdej warstwie:**

| Warstwa | Co chroni |
|---|---|
| Bufor cykliczny we flashu ESP32 | brak zasięgu, awarię brokera, restart i zanik zasilania urządzenia |
| Mosquitto (trwała sesja QoS 1 Telegrafa, `persistence`) | chwilową niedostępność Telegrafa |
| Telegraf (`metric_buffer_limit`, potwierdzanie po zapisie) | niedostępność InfluxDB |

**Dane spóźnione a agregacja.** Task downsamplingu co minutę przelicza **ostatnie 15 minut**, a nie tylko
ostatnią minutę, więc pomiary dosłane z buforów też trafiają do średnich minutowych.

**Czas zdarzenia a czas wysyłki.** Rekord niesie `ts` (moment pomiaru, używany jako czas punktu
w InfluxDB) oraz `sent_at` i `delay_ms` (opóźnienie dostarczenia). Pole `link` zapisuje stan łączności
w chwili pomiaru: `online`, `zone`, `random` albo `manual`. Pola `seq` (kolejny numer pomiaru urządzenia)
i `dropped` (liczba pomiarów nadpisanych w pełnym buforze) pokazują, czy coś zginęło po drodze.

## Big Data (model 5V)

- **Volume:** 21 urządzeń daje ok. 1,3 mln rekordów na dobę. Retencja jest dwupoziomowa: surowe dane
  przez 7 dni, agregaty minutowe bezterminowo.
- **Velocity:** strumień ok. 15 pomiarów/s plus serie danych dosyłanych z buforów po odzyskaniu zasięgu.
  Skalowanie polega na zwiększeniu `DEVICES`.
- **Variety:** cztery typy maszyn o różnych schematach i częstotliwościach; pola liczbowe i tekstowe (stan łącza, awarie).
- **Veracity:** czas zdarzenia zamiast czasu odbioru, brak duplikatów, brak strat do pojemności bufora
  i jawnie policzone straty powyżej niej, informacja, że rekord przyszedł z bufora.
- **Value:** alert bezpieczeństwa (metan), monitorowanie temperatur i wykrywanie martwych stref łączności.

## Struktura

```
compose.yaml                 cały stos (9 kontenerów)
.env                         dane dostępowe środowiska demonstracyjnego
config/mine_layout.json      układ kopalni: chodniki, trasy, strefy bez zasięgu (wspólny dla symulatora i panelu)
config/mosquitto/            broker: MQTT 1883 + WebSockets 9001, persistence
config/telegraf/             MQTT -> InfluxDB (topic -> measurement + tag device_id)
config/influxdb/             skrypt init (bucket telemetry_1m + task downsamplingu)
config/grafana/              provisioning: datasource, dashboard, alert CH4
config/nginx/                serwowanie panelu
simulator/                   symulator urządzeń ESP32 (Python, uv, paho-mqtt)
web/                         panel dyspozytora (Leaflet + MQTT.js, bez backendu)
```

## Praca z symulatorem lokalnie (uv)

```bash
cd simulator
uv run pytest                                          # testy bufora i logiki store-and-forward
uv run edge-sim --type haul_truck --devices 3          # wymaga brokera na localhost:1883
```
