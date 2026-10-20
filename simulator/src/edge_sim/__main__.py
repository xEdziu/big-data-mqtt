"""Uruchamia N urządzeń ESP32 jednego typu, każde w osobnym wątku z własnym klientem MQTT i buforem."""

import argparse
import logging
import os
import random
import signal
import threading
from pathlib import Path

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from .buffer import Outbox
from .device import Device
from .layout import MineLayout
from .profiles import PROFILES

DEFAULT_LAYOUT = Path(__file__).resolve().parents[3] / "config" / "mine_layout.json"


def parse_args() -> argparse.Namespace:
    env = os.environ.get
    p = argparse.ArgumentParser(prog="edge-sim", description=__doc__)
    p.add_argument("--type", default=env("MACHINE_TYPE", "haul_truck"), choices=sorted(PROFILES))
    p.add_argument("--devices", type=int, default=int(env("DEVICES", "3")))
    p.add_argument("--broker", default=env("MQTT_HOST", "localhost"))
    p.add_argument("--port", type=int, default=int(env("MQTT_PORT", "1883")))
    p.add_argument("--layout", type=Path, default=Path(env("LAYOUT", DEFAULT_LAYOUT)))
    p.add_argument("--buffer-dir", type=Path, default=Path(env("BUFFER_DIR", "buffers")))
    # 24576 slotów x 64 B = partycja 1,5 MB na 4 MB flashu ESP32
    p.add_argument("--buffer-records", type=int, default=int(env("BUFFER_RECORDS", "24576")))
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    layout = MineLayout.load(args.layout)
    machine_cls = PROFILES[args.type]
    args.buffer_dir.mkdir(parents=True, exist_ok=True)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    routes = layout.routes.get(args.type)
    stations = layout.stations.get(args.type)
    threads, clients = [], []
    for i in range(args.devices):
        device_id = f"{machine_cls.prefix}-{i + 1:02d}"
        rng = random.Random(device_id)
        place = routes[i % len(routes)] if routes else stations[i % len(stations)]

        client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=f"edge-{device_id}")
        client.reconnect_delay_set(min_delay=1, max_delay=10)
        device = Device(
            device_id, machine_cls(rng), layout, place,
            Outbox(args.buffer_dir / f"{device_id}.ring", args.buffer_records), client, rng,
        )
        client.connect_async(args.broker, args.port, keepalive=15)
        client.loop_start()
        clients.append(client)

        t = threading.Thread(target=device.run, args=(stop,), name=device_id)
        t.start()
        threads.append(t)

    logging.info("Uruchomiono %d x %s (broker %s:%d)", args.devices, args.type, args.broker, args.port)
    for t in threads:
        while t.is_alive():
            t.join(timeout=1.0)
    for client in clients:
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    main()
