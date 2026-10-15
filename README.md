# Telemetria maszyn górniczych z buforowaniem na krawędzi (Edge Store-and-Forward)

Projekt z przedmiotu *Big Data w Przemyśle 4.0*. Maszyny pracujące pod ziemią (wozidła, ładowarki LHD,
wiertnice, wentylatory) wysyłają telemetrię przez MQTT. W chodnikach bez zasięgu każda maszyna zapisuje
pomiary w lokalnej bazie SQLite i po odzyskaniu łączności dosyła je z oryginalnym czasem pomiaru,
więc żaden pomiar nie ginie.

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
   Sprawdzenie, że nic nie zginęło: największy odstęp między kolejnymi pomiarami każdego urządzenia
   jest równy jego interwałowi.
   ```bash
   docker compose exec influxdb influx query 'from(bucket:"telemetry") |> range(start:-5m)
     |> filter(fn:(r)=>r._field=="delay_ms") |> elapsed(unit:1ms) |> max(column:"elapsed")
     |> keep(columns:["device_id","elapsed"]) |> group()'
   ```

## Jak to działa i dlaczego tak

**Wzorzec Outbox na urządzeniu** (`simulator/src/edge_sim/device.py`). Każdy pomiar jest zawsze
najpierw zapisywany w SQLite. Osobna pętla wysyła rekordy od najstarszego (QoS 1) i usuwa rekord dopiero
po potwierdzeniu odbioru przez broker (PUBACK). Prostsze podejście „jeśli połączony, to wyślij, a jeśli nie,
to zapisz” jest dziurawe: biblioteka paho-mqtt przy zerwanym połączeniu nie zgłasza błędu, tylko odkłada
wiadomość do kolejki w RAM, a ta ginie przy restarcie urządzenia.

**At-least-once i idempotencja.** Przy QoS 1 duplikaty są możliwe, ale InfluxDB nadpisuje punkt o tej samej
serii i znaczniku czasu. W efekcie w bazie każdy pomiar jest dokładnie raz.

**Buforowanie na każdej warstwie:**

| Warstwa | Co chroni |
|---|---|
| SQLite na urządzeniu | brak zasięgu, awarię brokera, restart urządzenia |
| Mosquitto (trwała sesja QoS 1 Telegrafa, `persistence`) | chwilową niedostępność Telegrafa |
| Telegraf (`metric_buffer_limit`, potwierdzanie po zapisie) | niedostępność InfluxDB |

**Dane spóźnione a agregacja.** Task downsamplingu co minutę przelicza **ostatnie 15 minut**, a nie tylko
ostatnią minutę, więc pomiary dosłane z buforów też trafiają do średnich minutowych.

**Czas zdarzenia a czas wysyłki.** Rekord niesie `ts` (moment pomiaru, używany jako czas punktu
w InfluxDB) oraz `sent_at` i `delay_ms` (opóźnienie dostarczenia). Pole `link` zapisuje stan łączności
w chwili pomiaru: `online`, `zone`, `random` albo `manual`.

## Big Data (model 5V)

- **Volume:** 21 urządzeń daje ok. 1,3 mln rekordów na dobę. Retencja jest dwupoziomowa: surowe dane
  przez 7 dni, agregaty minutowe bezterminowo.
- **Velocity:** strumień ok. 15 pomiarów/s plus serie danych dosyłanych z buforów po odzyskaniu zasięgu.
  Skalowanie polega na zwiększeniu `DEVICES`.
- **Variety:** cztery typy maszyn o różnych schematach i częstotliwościach; pola liczbowe i tekstowe (stan łącza, awarie).
- **Veracity:** czas zdarzenia zamiast czasu odbioru, brak strat i duplikatów, jawna informacja,
  że rekord przyszedł z bufora.
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
simulator/                   symulator urządzeń (Python, uv, paho-mqtt)
web/                         panel dyspozytora (Leaflet + MQTT.js, bez backendu)
```

## Praca z symulatorem lokalnie (uv)

```bash
cd simulator
uv run pytest                                          # testy bufora i logiki store-and-forward
uv run edge-sim --type haul_truck --devices 3          # wymaga brokera na localhost:1883
```
