// Panel dyspozytora: mapa poziomu kopalni + wywoływanie awarii przez MQTT over WebSockets.

// Interwały pomiarów - zgodne z profilami w simulator/src/edge_sim/profiles.py
const INTERVAL_S = { haul_truck: 1, lhd_loader: 1, drill_rig: 2, vent_fan: 5 };
const TYPE_LABEL = {
  haul_truck: "Wozidła",
  lhd_loader: "Ładowarki LHD",
  drill_rig: "Wiertnice",
  vent_fan: "Wentylatory",
};
const BACKFILL_MS = 3000;
const ALARM_TEMP = 105;
const ALARM_CH4 = 1.0;
const COLORS = {
  online: "#3fb950",
  silent: "#f0883e",
  backfill: "#58a6ff",
  alarm: "#f85149",
};
const STATUS_TEXT = {
  online: "online",
  silent: "brak danych - buforuje na urządzeniu",
  backfill: "dosyła zaległe dane",
};
const FIELD_LABEL = {
  ts: "czas pomiaru",
  link: "łącze (przy pomiarze)",
  faults: "awarie",
  x: "x [m]",
  y: "y [m]",
  speed_kmh: "prędkość [km/h]",
  payload_t: "ładunek [t]",
  fuel_pct: "paliwo [%]",
  engine_temp: "temp. silnika [°C]",
  motor_temp: "temp. silnika [°C]",
  ch4_pct: "CH4 [%]",
  bucket_load_t: "łyżka [t]",
  hydraulic_bar: "hydraulika [bar]",
  cycles: "cykle",
  rpm: "obroty [rpm]",
  feed_bar: "posuw [bar]",
  vibration_mm_s: "drgania [mm/s]",
  hole_depth_m: "głęb. otworu [m]",
  airflow_m3_s: "przepływ [m³/s]",
  motor_current_a: "prąd [A]",
  buffer_size: "bufor edge",
  delay_ms: "opóźnienie [ms]",
  buffered: "z bufora",
  seq: "nr pomiaru (seq)",
  dropped: "utracone (pełny bufor)",
};

const devices = new Map();
let selectedId = null;

// ---------- mapa ----------
const map = L.map("map", {
  crs: L.CRS.Simple,
  minZoom: -2,
  maxZoom: 3,
  zoomSnap: 0.25,
  attributionControl: false,
});
const xy = ([x, y]) => [y, x];

fetch("mine_layout.json")
  .then((r) => r.json())
  .then((layout) => {
    document.getElementById("mine-name").textContent = layout.name;
    const [[x0, y0], [x1, y1]] = layout.bounds;
    map.fitBounds([xy([x0, y0]), xy([x1, y1])]);
    for (const t of layout.tunnels) {
      const pts = t.points.map(xy);
      L.polyline(pts, {
        color: "#4a5363",
        weight: 16,
        lineCap: "square",
        interactive: false,
      }).addTo(map);
      L.polyline(pts, { color: "#5d6779", weight: 1, dashArray: "4 6" })
        .addTo(map)
        .bindTooltip(t.name, { sticky: true });
    }
    for (const z of layout.zones) {
      L.polygon(z.polygon.map(xy), {
        color: "#f85149",
        weight: 1,
        fillOpacity: 0.18,
        dashArray: "3 3",
      })
        .addTo(map)
        .bindTooltip(`brak zasięgu: ${z.name}`, {
          permanent: true,
          direction: "center",
          className: "zone-label",
        });
    }
    L.circleMarker(xy(layout.shaft.point), {
      radius: 10,
      color: "#dfe5ee",
      weight: 2,
      fillOpacity: 0,
    })
      .addTo(map)
      .bindTooltip(layout.shaft.name, {
        permanent: true,
        direction: "left",
        className: "dev-label",
      });
  });

// ---------- MQTT ----------
const client = mqtt.connect(`ws://${location.hostname || "localhost"}:9001`, {
  reconnectPeriod: 2000,
});
const connEl = document.getElementById("conn");
client.on("connect", () => {
  client.subscribe("mine/+/+/telemetry", { qos: 0 });
  connEl.textContent = "broker: połączono";
  connEl.className = "conn on";
});
client.on("close", () => {
  connEl.textContent = "broker: rozłączono";
  connEl.className = "conn off";
});
client.on("message", (topic, payload) => {
  const [, type, id] = topic.split("/");
  let rec;
  try {
    rec = JSON.parse(payload.toString());
  } catch {
    return;
  }
  if (!(type in INTERVAL_S) || typeof rec.ts !== "number") return;
  onRecord(type, id, rec);
});

function onRecord(type, id, rec) {
  let dev = devices.get(id);
  if (!dev) {
    dev = {
      id,
      type,
      last: null,
      lastBufferedAt: 0,
      status: null,
      alarm: false,
    };
    dev.marker = L.circleMarker([0, 0], {
      radius: type === "vent_fan" ? 8 : 6,
      weight: 2,
      fillOpacity: 0.9,
    })
      .addTo(map)
      .bindTooltip(id, {
        permanent: true,
        direction: "right",
        offset: [6, 0],
        className: "dev-label",
      })
      .on("click", () => select(id));
    devices.set(id, dev);
    buildList();
  }
  if (rec.buffered) dev.lastBufferedAt = Date.now();
  if (!dev.last || rec.ts >= dev.last.ts) dev.last = rec; // przy backfillu liczy się najnowszy pomiar
}

// ---------- stan urządzeń ----------
function statusOf(dev) {
  const age = Date.now() - dev.last.ts;
  if (age > 3 * INTERVAL_S[dev.type] * 1000 + 1500) return "silent";
  if (Date.now() - dev.lastBufferedAt < BACKFILL_MS) return "backfill";
  return "online";
}

function alarmOf(rec) {
  const temp = rec.engine_temp ?? rec.motor_temp ?? 0;
  return (
    rec.faults !== "none" || rec.ch4_pct >= ALARM_CH4 || temp >= ALARM_TEMP
  );
}

function keyMetric(dev) {
  const r = dev.last;
  switch (dev.type) {
    case "haul_truck":
      return `${r.speed_kmh} km/h · ${r.payload_t} t`;
    case "lhd_loader":
      return `${r.bucket_load_t} t · ${r.hydraulic_bar} bar`;
    case "drill_rig":
      return `${r.rpm} rpm · ${r.hole_depth_m} m`;
    case "vent_fan":
      return `${r.airflow_m3_s} m³/s · CH4 ${r.ch4_pct}%`;
  }
  return "";
}

function fmtAge(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}

function logEvent(text) {
  const li = document.createElement("li");
  const t = document.createElement("time");
  t.textContent = new Date().toLocaleTimeString("pl-PL");
  li.append(t, text);
  const log = document.getElementById("log");
  log.prepend(li);
  while (log.children.length > 100) log.lastChild.remove();
}

function render() {
  const counts = { online: 0, silent: 0, backfill: 0, alarm: 0 };
  for (const dev of devices.values()) {
    if (!dev.last) continue;
    const status = statusOf(dev);
    const alarm = alarmOf(dev.last);
    if (dev.status && status !== dev.status)
      logEvent(`${dev.id}: ${STATUS_TEXT[status]}`);
    if (alarm && !dev.alarm)
      logEvent(`${dev.id}: ALARM (${describeAlarm(dev.last)})`);
    dev.status = status;
    dev.alarm = alarm;
    counts[status]++;
    if (alarm) counts.alarm++;

    dev.marker.setLatLng(xy([dev.last.x, dev.last.y]));
    dev.marker.setStyle({
      fillColor: COLORS[status],
      color: alarm
        ? COLORS.alarm
        : dev.id === selectedId
          ? "#ffffff"
          : "#12151b",
      weight: alarm || dev.id === selectedId ? 4 : 2,
    });

    const row = document.getElementById(`row-${dev.id}`);
    if (row) {
      row.querySelector(".dot").style.background = COLORS[status];
      row.querySelector(".metric").textContent = keyMetric(dev);
      row.querySelector(".age").textContent = fmtAge(Date.now() - dev.last.ts);
      row.classList.toggle("alarm", alarm);
    }
  }
  document.getElementById("counters").textContent =
    `${devices.size} urządzeń · online ${counts.online} · buforuje ${counts.silent} · dosyła ${counts.backfill} · alarmy ${counts.alarm}`;
  renderDetails();
}

function describeAlarm(rec) {
  const parts = [];
  if (rec.faults !== "none") parts.push(rec.faults);
  if (rec.ch4_pct >= ALARM_CH4) parts.push(`CH4 ${rec.ch4_pct}%`);
  const temp = rec.engine_temp ?? rec.motor_temp;
  if (temp >= ALARM_TEMP) parts.push(`${temp}°C`);
  return parts.join(", ");
}

// ---------- panel boczny ----------
function buildList() {
  const list = document.getElementById("list");
  list.replaceChildren();
  for (const type of Object.keys(TYPE_LABEL)) {
    const devs = [...devices.values()]
      .filter((d) => d.type === type)
      .sort((a, b) => a.id.localeCompare(b.id));
    if (!devs.length) continue;
    const group = document.createElement("div");
    group.className = "group";
    const h = document.createElement("h3");
    h.textContent = TYPE_LABEL[type];
    group.append(h);
    for (const d of devs) {
      const row = document.createElement("button");
      row.className = "dev" + (d.id === selectedId ? " selected" : "");
      row.id = `row-${d.id}`;
      row.innerHTML =
        '<i class="dot"></i><strong></strong><span class="metric"></span><span class="age"></span>';
      row.querySelector("strong").textContent = d.id;
      row.addEventListener("click", () => select(d.id));
      group.append(row);
    }
    list.append(group);
  }
}

function select(id) {
  selectedId = id;
  const dev = devices.get(id);
  document
    .querySelectorAll(".dev")
    .forEach((r) => r.classList.toggle("selected", r.id === `row-${id}`));
  document.getElementById("details").hidden = false;
  document.getElementById("d-title").textContent =
    `${id} - ${TYPE_LABEL[dev.type]}`;
  map.panTo(xy([dev.last.x, dev.last.y]));
  render();
}

function renderDetails() {
  const dev = devices.get(selectedId);
  if (!dev) return;
  const dl = document.getElementById("d-fields");
  dl.replaceChildren();
  const add = (k, v) => {
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = k;
    dd.textContent = v;
    dl.append(dt, dd);
  };
  add("stan", STATUS_TEXT[dev.status] ?? "");
  add("ostatni pomiar", `${fmtAge(Date.now() - dev.last.ts)} temu`);
  for (const [k, v] of Object.entries(dev.last)) {
    if (k === "device_id" || k === "ts" || k === "sent_at") continue;
    add(FIELD_LABEL[k] ?? k, v);
  }
}

document.querySelectorAll("#details [data-action]").forEach((btn) =>
  btn.addEventListener("click", () => {
    const dev = devices.get(selectedId);
    if (!dev) return;
    const cmd = { action: btn.dataset.action };
    if (cmd.action === "link_loss")
      cmd.seconds = Number(document.getElementById("d-seconds").value) || 60;
    client.publish(`mine/${dev.type}/${dev.id}/cmd`, JSON.stringify(cmd), {
      qos: 1,
    });
    logEvent(
      `→ ${dev.id}: ${cmd.action}${cmd.seconds ? ` ${cmd.seconds} s` : ""}`,
    );
  }),
);

setInterval(render, 500);
