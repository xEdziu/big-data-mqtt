"""Urządzenie brzegowe (ESP32): pomiar -> outbox we flashu -> MQTT, zwolnienie slotu dopiero po PUBACK."""

import json
import logging
import random
import threading
import time

from .buffer import Outbox
from .layout import MineLayout, Point, Route
from .profiles import Faults, Machine
from .record import RecordCodec

log = logging.getLogger(__name__)

BATCH_SIZE = 200
PUBACK_TIMEOUT_S = 5.0
MAX_MANUAL_LOSS_S = 600


class Device:
    def __init__(
        self,
        device_id: str,
        machine: Machine,
        layout: MineLayout,
        place: Route | Point,
        outbox: Outbox,
        client,
        rng: random.Random,
    ):
        self.device_id = device_id
        self.machine = machine
        self.layout = layout
        self.outbox = outbox
        self.codec = RecordCodec(machine.record_fields())
        self.client = client
        self.rng = rng
        self.topic = f"mine/{machine.machine_type}/{device_id}/telemetry"
        self.cmd_topic = f"mine/{machine.machine_type}/{device_id}/cmd"

        if isinstance(place, Route):
            self.route: Route | None = place
            self.odometer = rng.uniform(0, 2 * place.length)
            self.position = place.position_at(self.odometer)
        else:
            self.route = None
            self.position = place

        self.faults = Faults()
        self.manual_loss_until = 0.0
        self.random_loss_until = 0.0
        self.link = "online"

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

    # --- MQTT (wątek pętli paho) ---

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("[NET] %s połączono z brokerem", self.device_id)
            client.subscribe(self.cmd_topic, qos=1)
        else:
            log.warning("[NET] %s odmowa połączenia: %s", self.device_id, reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        log.warning("[NET] %s rozłączono z brokerem (%s)", self.device_id, reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            cmd = json.loads(msg.payload)
            action = cmd["action"]
        except (ValueError, TypeError, KeyError):
            log.warning("[CMD] %s niepoprawna komenda: %r", self.device_id, msg.payload[:200])
            return
        self.apply_command(action, cmd)

    def apply_command(self, action: str, cmd: dict) -> None:
        if action == "link_loss":
            try:
                seconds = min(max(float(cmd.get("seconds", 30)), 1.0), MAX_MANUAL_LOSS_S)
            except (TypeError, ValueError):
                seconds = 30.0
            self.manual_loss_until = time.monotonic() + seconds
        elif action == "overheat":
            self.faults.overheat = True
        elif action == "ch4_leak":
            self.faults.ch4_leak = True
        elif action == "stop":
            self.faults.stopped = True
        elif action == "reset":
            self.faults = Faults()
            self.manual_loss_until = 0.0
        else:
            log.warning("[CMD] %s nieznana akcja: %r", self.device_id, action)
            return
        log.info("[CMD] %s <- %s", self.device_id, action)

    # --- pętla urządzenia ---

    def _link_state(self, mono: float) -> str:
        if mono < self.manual_loss_until:
            return "manual"
        if self.layout.zone_at(self.position):
            return "zone"
        if mono < self.random_loss_until:
            return "random"
        if self.rng.random() < self.machine.random_loss_p:
            self.random_loss_until = mono + self.rng.uniform(*self.machine.random_loss_s)
            return "random"
        return "online"

    def tick(self, wall: float, mono: float, dt: float) -> None:
        """Wykonaj pomiar i zapisz go w outboxie (zawsze, niezależnie od łączności)."""
        fields = self.machine.step(dt, self.faults)
        if self.route is not None:
            self.odometer += self.machine.move_speed_mps() * dt
            self.position = self.route.position_at(self.odometer)

        link = self._link_state(mono)
        if link != self.link:
            if link == "online":
                log.info(
                    "[LINK] %s odzyskano zasięg, w buforze %d rekordów (utraconych łącznie: %d)",
                    self.device_id, self.outbox.count(), self.outbox.dropped,
                )
            else:
                log.info("[LINK] %s utrata zasięgu (%s), buforuję we flashu", self.device_id, link)
        self.link = link

        record = {
            "ts": int(wall * 1000),
            "link": link,
            "faults": self.faults.describe(),
            "x": round(self.position[0], 1),
            "y": round(self.position[1], 1),
            **fields,
            "buffer_size": min(self.outbox.count() + 1, self.outbox.capacity),
        }
        was_full = self.outbox.count() == self.outbox.capacity
        self.outbox.append(self.codec.encode(record))
        if not was_full and self.outbox.count() == self.outbox.capacity:
            log.warning("[BUFFER] %s bufor pełny (%d), nadpisuję najstarsze rekordy", self.device_id, self.outbox.capacity)

    def flush(self, deadline: float) -> int:
        """Wyślij zaległe rekordy (FIFO) i usuń tylko te potwierdzone przez broker."""
        if self.link != "online" or not self.client.is_connected():
            return 0
        live_ms = self.machine.interval_s * 1000 + 1000
        sent = 0
        while time.monotonic() < deadline:
            rows = self.outbox.peek_oldest(BATCH_SIZE)
            if not rows:
                break
            pending = []
            for seq, payload in rows:
                record = {"device_id": self.device_id, **self.codec.decode(payload), "seq": seq}
                now_ms = int(time.time() * 1000)
                record["sent_at"] = now_ms
                record["delay_ms"] = now_ms - record["ts"]
                record["buffered"] = int(record["delay_ms"] > live_ms)
                record["dropped"] = self.outbox.dropped
                pending.append((seq, self.client.publish(self.topic, json.dumps(record), qos=1)))
            acked = 0
            try:
                for seq, info in pending:
                    info.wait_for_publish(timeout=PUBACK_TIMEOUT_S)
                    if not info.is_published():
                        break
                    self.outbox.ack_through(seq)
                    acked += 1
            except (RuntimeError, ValueError) as exc:
                log.warning("[FORWARD] %s przerwano wysyłkę: %s", self.device_id, exc)
            sent += acked
            if acked < len(pending):
                break
        if sent > 1:
            log.info("[FORWARD] %s wysłano %d zaległych rekordów", self.device_id, sent)
        return sent

    def run(self, stop: threading.Event) -> None:
        interval = self.machine.interval_s
        stop.wait(self.rng.uniform(0, interval))  # rozsunięcie urządzeń w czasie
        last = time.monotonic()
        next_tick = last
        while not stop.is_set():
            mono = time.monotonic()
            self.tick(time.time(), mono, mono - last)
            last = mono
            next_tick += interval
            self.flush(deadline=next_tick)
            stop.wait(max(0.0, next_tick - time.monotonic()))
