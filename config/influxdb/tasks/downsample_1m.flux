// Średnie minutowe do bucketu z długą retencją.
// Okno 15 min (a nie tylko ostatnia minuta) - dane dosłane z buforów edge po
// utracie zasięgu też trafią do agregatów; ponowny zapis nadpisuje punkty (idempotentnie).
option task = {name: "downsample_1m", every: 1m, offset: 5s}

from(bucket: "telemetry")
    |> range(start: -15m)
    |> filter(fn: (r) => r._field != "link" and r._field != "faults")
    |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
    |> to(bucket: "telemetry_1m")
