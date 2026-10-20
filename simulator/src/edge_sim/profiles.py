"""Profile maszyn: każdy typ ma inne pola, interwał i podatność na zaniki łączności."""

import math
import random
from dataclasses import dataclass


FAULT_NAMES = ("overheat", "ch4_leak", "stopped")


@dataclass
class Faults:
    """Awarie wywoływane ręcznie z panelu dyspozytora."""

    overheat: bool = False
    ch4_leak: bool = False
    stopped: bool = False

    def describe(self) -> str:
        names = [n for n in FAULT_NAMES if getattr(self, n)]
        return ",".join(names) or "none"


def approach(value: float, target: float, tau_s: float, dt: float) -> float:
    """Wykładnicze dążenie do wartości docelowej ze stałą czasową tau."""
    return target + (value - target) * math.exp(-dt / tau_s)


class Machine:
    machine_type: str
    prefix: str
    interval_s: float
    random_loss_p: float  # prawdopodobieństwo zaniku na jeden pomiar
    random_loss_s: tuple[float, float] = (5.0, 20.0)
    temp_field = "engine_temp"
    temp_nominal = 85.0
    ch4_baseline = 0.15
    specific_fields: dict[str, str]  # pola specific() -> typ w rekordzie binarnym (kod struct)

    @classmethod
    def record_fields(cls) -> dict[str, str]:
        return {cls.temp_field: "f", "ch4_pct": "f", **cls.specific_fields}

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.temp = self.temp_nominal - 10
        self.ch4 = self.ch4_baseline

    def step(self, dt: float, faults: Faults) -> dict:
        temp_target = 125.0 if faults.overheat else self.temp_nominal
        self.temp = approach(self.temp, temp_target, 25 if faults.overheat else 60, dt)
        ch4_target = 2.5 if faults.ch4_leak else self.ch4_target(faults)
        self.ch4 = max(0.0, approach(self.ch4, ch4_target, 20, dt) + self.rng.gauss(0, 0.01))
        fields = {
            self.temp_field: round(self.temp + self.rng.gauss(0, 0.3), 1),
            "ch4_pct": round(self.ch4, 3),
        }
        fields.update(self.specific(dt, faults))
        return fields

    def ch4_target(self, faults: Faults) -> float:
        return self.ch4_baseline

    def specific(self, dt: float, faults: Faults) -> dict:
        raise NotImplementedError

    def move_speed_mps(self) -> float:
        return 0.0


class HaulTruck(Machine):
    """Wozidło: odwozi urobek z pól eksploatacyjnych do szybu."""

    machine_type, prefix, interval_s = "haul_truck", "TRK", 1.0
    random_loss_p = 0.004
    specific_fields = {"speed_kmh": "f", "payload_t": "f", "fuel_pct": "f"}

    def __init__(self, rng):
        super().__init__(rng)
        self.t = rng.uniform(0, 240)
        self.speed = 0.0
        self.payload = 0.0
        self.fuel = rng.uniform(40, 100)

    def specific(self, dt, faults):
        self.t += dt
        loaded = int(self.t // 120) % 2 == 1
        target = 0.0 if faults.stopped else (18.0 if loaded else 25.0)
        self.speed = max(0.0, approach(self.speed, target, 4, dt) + self.rng.gauss(0, 0.4))
        self.payload = approach(self.payload, 40.0 if loaded else 0.0, 10, dt)
        self.fuel -= dt * (0.01 if self.speed > 1 else 0.002)
        if self.fuel < 10:
            self.fuel = 100.0  # tankowanie
        return {
            "speed_kmh": round(self.speed, 1),
            "payload_t": round(self.payload, 1),
            "fuel_pct": round(self.fuel, 2),
        }

    def move_speed_mps(self):
        return self.speed / 3.6


class LhdLoader(Machine):
    """Ładowarka LHD: cykle nabierania urobku w przodkach."""

    machine_type, prefix, interval_s = "lhd_loader", "LHD", 1.0
    random_loss_p = 0.002
    temp_nominal = 82.0
    specific_fields = {"speed_kmh": "f", "bucket_load_t": "f", "hydraulic_bar": "f", "cycles": "I"}

    def __init__(self, rng):
        super().__init__(rng)
        self.t = rng.uniform(0, 60)
        self.speed = 0.0
        self.load = 0.0
        self.cycles = 0
        self.was_loaded = False

    def specific(self, dt, faults):
        self.t += dt
        loaded = (self.t % 60) > 30
        if self.was_loaded and not loaded:
            self.cycles += 1
        self.was_loaded = loaded
        target = 0.0 if faults.stopped else 10.0
        self.speed = max(0.0, approach(self.speed, target, 3, dt) + self.rng.gauss(0, 0.3))
        self.load = approach(self.load, 14.0 if loaded else 0.0, 3, dt)
        hydraulic = 0.0 if faults.stopped else 160 + 6 * self.load + self.rng.gauss(0, 3)
        return {
            "speed_kmh": round(self.speed, 1),
            "bucket_load_t": round(self.load, 2),
            "hydraulic_bar": round(hydraulic, 1),
            "cycles": self.cycles,
        }

    def move_speed_mps(self):
        return self.speed / 3.6


class DrillRig(Machine):
    """Wiertnica: wierci otwory strzałowe, między przodkami przejeżdża powoli."""

    machine_type, prefix, interval_s = "drill_rig", "DRL", 2.0
    random_loss_p = 0.002
    temp_field, temp_nominal = "motor_temp", 70.0
    specific_fields = {"rpm": "H", "feed_bar": "f", "vibration_mm_s": "f", "hole_depth_m": "f"}

    hole_depth = 5.0
    tram_s = 45.0  # czas przejazdu do kolejnego otworu

    def __init__(self, rng):
        super().__init__(rng)
        self.depth = rng.uniform(0, self.hole_depth)
        self.rpm = 0.0
        self.tram_left_s = 0.0
        self.stopped = False

    def specific(self, dt, faults):
        self.stopped = faults.stopped
        drilling = not faults.stopped and self.tram_left_s <= 0
        self.rpm = max(0.0, approach(self.rpm, 2200.0 if drilling else 0.0, 5, dt) + self.rng.gauss(0, 20))
        if drilling:
            self.depth += dt * 0.04
            if self.depth > self.hole_depth:
                self.depth = 0.0  # otwór gotowy -> przejazd do następnego
                self.tram_left_s = self.tram_s
        elif not faults.stopped:
            self.tram_left_s -= dt
        vibration = 1.0 + self.rpm / 500 + (3.0 if self.temp > 100 else 0.0) + abs(self.rng.gauss(0, 0.3))
        return {
            "rpm": round(self.rpm),
            "feed_bar": round(120 * self.rpm / 2200 + self.rng.gauss(0, 2), 1),
            "vibration_mm_s": round(vibration, 2),
            "hole_depth_m": round(self.depth, 2),
        }

    def move_speed_mps(self):
        return 1.0 if self.tram_left_s > 0 and not self.stopped else 0.0


class VentFan(Machine):
    """Wentylator lutniowy: stacjonarny, przewodowy (bez zaników), mierzy metan."""

    machine_type, prefix, interval_s = "vent_fan", "FAN", 5.0
    random_loss_p = 0.0
    temp_field, temp_nominal = "motor_temp", 65.0
    ch4_baseline = 0.3
    specific_fields = {"airflow_m3_s": "f", "motor_current_a": "f", "vibration_mm_s": "f"}

    def __init__(self, rng):
        super().__init__(rng)
        self.airflow = 45.0
        self.outburst_left_s = 0.0

    def ch4_target(self, faults):
        if self.outburst_left_s > 0:
            return 1.6  # samoistny wypływ metanu
        return self.ch4_baseline + (1.5 if faults.stopped else 0.0)

    def specific(self, dt, faults):
        self.outburst_left_s = max(0.0, self.outburst_left_s - dt)
        if self.rng.random() < 0.002:
            self.outburst_left_s = 60.0
        self.airflow = max(0.0, approach(self.airflow, 0.0 if faults.stopped else 45.0, 8, dt) + self.rng.gauss(0, 0.4))
        return {
            "airflow_m3_s": round(self.airflow, 1),
            "motor_current_a": round(max(0.0, 4 * self.airflow + self.rng.gauss(0, 2)), 1),
            "vibration_mm_s": round(2.5 + abs(self.rng.gauss(0, 0.2)), 2),
        }


PROFILES: dict[str, type[Machine]] = {
    cls.machine_type: cls for cls in (HaulTruck, LhdLoader, DrillRig, VentFan)
}
