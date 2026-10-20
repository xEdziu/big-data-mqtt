"""Binarny format pomiaru w buforze, odpowiednik struktury C w firmware ESP32.

JSON powstaje dopiero przy wysyłce. Rekord binarny zajmuje 40-46 B zamiast ok. 250 B,
więc w tej samej partycji flasha mieści się kilka razy więcej pomiarów.
"""

import struct

from .profiles import FAULT_NAMES

LINKS = ("online", "zone", "random", "manual")
HEADER = "<QBBffI"  # ts [ms], link, maska awarii, x, y, buffer_size


def _f32(v: float) -> float:
    return float(f"{v:.7g}")  # float32 ma ok. 7 cyfr znaczących, odcinamy szum konwersji


class RecordCodec:
    def __init__(self, fields: dict[str, str]):
        """fields: nazwa pola -> kod typu modułu struct ("f" = float32, "H"/"I" = liczby całkowite)."""
        self._fields = dict(fields)
        self._struct = struct.Struct(HEADER + "".join(self._fields.values()))
        self.size = self._struct.size

    def encode(self, record: dict) -> bytes:
        faults = record["faults"].split(",")
        mask = sum(1 << i for i, name in enumerate(FAULT_NAMES) if name in faults)
        return self._struct.pack(
            record["ts"], LINKS.index(record["link"]), mask,
            record["x"], record["y"], record["buffer_size"],
            *(record[name] for name in self._fields),
        )

    def decode(self, data: bytes) -> dict:
        ts, link, mask, x, y, buffer_size, *values = self._struct.unpack(data)
        faults = ",".join(n for i, n in enumerate(FAULT_NAMES) if mask >> i & 1) or "none"
        record = {"ts": ts, "link": LINKS[link], "faults": faults, "x": _f32(x), "y": _f32(y)}
        for (name, code), value in zip(self._fields.items(), values):
            record[name] = _f32(value) if code == "f" else value
        record["buffer_size"] = buffer_size
        return record
