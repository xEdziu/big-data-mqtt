"""Lokalny bufor (outbox) urządzenia brzegowego: log cykliczny na partycji flasha ESP32.

Plik emuluje surową partycję flasha: superblok + `capacity` slotów po SLOT_SIZE B.
Slot pomiaru o numerze `seq` leży zawsze pod indeksem `seq % capacity`, więc gdy bufor
jest pełny, nowy pomiar nadpisuje najstarszy niewysłany (polityka drop-oldest) i zwiększa
licznik `dropped`. Potwierdzenie (PUBACK) nie kasuje slotu, tylko zeruje bajt flagi -
na flashu to dozwolona zmiana bitów 1 -> 0 bez kasowania sektora. CRC wykrywa slot
przerwany zanikiem zasilania w trakcie zapisu. Po restarcie stan odtwarza skan slotów.
"""

import logging
import struct
import zlib
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)

SLOT_SIZE = 64
SUPERBLOCK = struct.Struct("<4sIII")  # magic, slot_size, capacity, dropped
MAGIC = b"RING"
SLOT_HEADER = struct.Struct("<IBBI")  # seq, flag, length, crc32(seq, length, payload)
MAX_PAYLOAD = SLOT_SIZE - SLOT_HEADER.size
PENDING, ACKED = 0xFF, 0x00
ERASED = b"\xff"  # skasowany flash czyta się jako same jedynki


def _crc(seq: int, payload: bytes) -> int:
    return zlib.crc32(struct.pack("<IB", seq, len(payload)) + payload)


class Outbox:
    def __init__(self, path: Path | str, capacity: int):
        if capacity < 1:
            raise ValueError("capacity musi być >= 1")
        self.capacity = capacity
        self._f = open(path, "r+b" if Path(path).exists() else "w+b", buffering=0)
        self._pending: deque[int] = deque()  # seq niewysłanych rekordów, rosnąco
        self._next_seq = 0
        self.dropped = 0

        old_capacity = self._read_superblock()
        if old_capacity == capacity:
            self._load()
        else:
            # zmiana rozmiaru partycji: przenieś niewysłane rekordy, najstarsze nadmiarowe przepadają
            carried = self._scan(old_capacity) if old_capacity else []
            if old_capacity:
                log.warning("[BUFFER] %s: zmiana pojemności %d -> %d", path, old_capacity, capacity)
            self._format()
            for seq, payload in carried[-capacity:]:
                self._write_slot(seq, payload)
                self._pending.append(seq)
            self.dropped += max(0, len(carried) - capacity)
            self._write_superblock()

    # --- format pliku ---

    def _read_superblock(self) -> int:
        self._f.seek(0)
        raw = self._f.read(SUPERBLOCK.size)
        if len(raw) < SUPERBLOCK.size:
            return 0
        magic, slot_size, capacity, dropped = SUPERBLOCK.unpack(raw)
        if magic != MAGIC or slot_size != SLOT_SIZE:
            return 0
        self.dropped = dropped
        return capacity

    def _write_superblock(self) -> None:
        self._f.seek(0)
        self._f.write(SUPERBLOCK.pack(MAGIC, SLOT_SIZE, self.capacity, self.dropped))

    def _format(self) -> None:
        self._f.seek(0)
        self._f.truncate()
        self._f.write(ERASED * (SLOT_SIZE * (self.capacity + 1)))

    def _offset(self, seq: int) -> int:
        return SLOT_SIZE * (1 + seq % self.capacity)

    def _scan(self, capacity: int) -> list[tuple[int, bytes]]:
        """Niewysłane rekordy partycji o danej pojemności (rosnąco po seq); ustawia też _next_seq."""
        records, self._next_seq = [], 0
        for i in range(capacity):
            self._f.seek(SLOT_SIZE * (1 + i))
            raw = self._f.read(SLOT_SIZE)
            if len(raw) < SLOT_HEADER.size:
                continue
            seq, flag, length, crc = SLOT_HEADER.unpack_from(raw)
            payload = raw[SLOT_HEADER.size:SLOT_HEADER.size + length]
            if length > MAX_PAYLOAD or crc != _crc(seq, payload):
                continue  # slot skasowany albo przerwany w trakcie zapisu
            self._next_seq = max(self._next_seq, seq + 1)
            if flag == PENDING:
                records.append((seq, payload))
        records.sort()
        return records

    def _load(self) -> None:
        self._pending.extend(seq for seq, _ in self._scan(self.capacity))

    def _write_slot(self, seq: int, payload: bytes) -> None:
        header = SLOT_HEADER.pack(seq, PENDING, len(payload), _crc(seq, payload))
        slot = header + payload
        self._f.seek(self._offset(seq))
        self._f.write(slot + ERASED * (SLOT_SIZE - len(slot)))

    # --- interfejs kolejki ---

    def append(self, payload: bytes) -> None:
        if len(payload) > MAX_PAYLOAD:
            raise ValueError(f"rekord {len(payload)} B nie mieści się w slocie ({MAX_PAYLOAD} B)")
        seq = self._next_seq
        overwritten = 0
        while self._pending and self._pending[0] <= seq - self.capacity:
            self._pending.popleft()
            overwritten += 1
        self._write_slot(seq, payload)
        self._pending.append(seq)
        self._next_seq = seq + 1
        if overwritten:
            self.dropped += overwritten
            self._write_superblock()

    def peek_oldest(self, limit: int) -> list[tuple[int, bytes]]:
        rows = []
        for seq in list(self._pending)[:limit]:
            self._f.seek(self._offset(seq))
            raw = self._f.read(SLOT_SIZE)
            _, _, length, _ = SLOT_HEADER.unpack_from(raw)
            rows.append((seq, raw[SLOT_HEADER.size:SLOT_HEADER.size + length]))
        return rows

    def ack_through(self, seq: int) -> None:
        """Oznacz jako wysłane wszystkie rekordy o numerze <= seq."""
        while self._pending and self._pending[0] <= seq:
            done = self._pending.popleft()
            self._f.seek(self._offset(done) + 4)
            self._f.write(bytes([ACKED]))

    def count(self) -> int:
        return len(self._pending)

    def close(self) -> None:
        self._f.close()
